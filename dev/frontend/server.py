### Dependency installation check

import sys
import subprocess
import importlib.util
import shutil

# On Windows, the console often uses a non-UTF8 codepage (e.g. cp1252), and the
# emoji print()s in this file (✅⚠️❌) raise UnicodeEncodeError and crash the
# whole server. Force UTF-8 so those prints can never bring it down.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')

def install_package(package):
    """Check whether a package is installed. If not, try to install it with pip."""
    if importlib.util.find_spec(package) is None:
        print(f"⚠️ {package} not found. Trying to install it...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", package], check=True)
            print(f"✅ {package} installed.")
        except subprocess.CalledProcessError as e:
            print(f"❌ Could not install {package}: {e}")
            sys.exit(1)

def check_and_install_pip():
    """Check whether pip is installed and install it if necessary."""
    try:
        subprocess.run([sys.executable, "-m", "pip", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        #print("✅ pip is already installed.")
    except subprocess.CalledProcessError:
        print("⚠️ pip not found. Trying to install it...")
        try:
            if sys.platform.startswith("linux"):
                package_manager = shutil.which("apt") or shutil.which("dnf") or shutil.which("yum")
                if package_manager:
                    subprocess.run(["sudo", package_manager, "install", "-y", "python3-pip"], check=True)
                else:
                    print("❌ Could not determine the Linux package manager.")
                    sys.exit(1)
            elif sys.platform == "win32":
                subprocess.run([sys.executable, "-m", "ensurepip"], check=True)
                subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=True)
            else:
                print(f"❌ Unsupported operating system: {sys.platform}")
                sys.exit(1)
            print("✅ pip installed.")
        except subprocess.CalledProcessError as e:
            print(f"❌ Could not install pip: {e}")
            sys.exit(1)

# List of required packages
required_packages = ["flask", "psutil", "numpy", "openpyxl"]

# Started by the launcher, the packages are already there (uv, without pip):
# pip is only looked for when something is missing, with a Python of one's own
if any(importlib.util.find_spec(p) is None for p in required_packages):
    check_and_install_pip()
    for package in required_packages:
        install_package(package)


from flask import Flask, render_template, request, jsonify, send_file, send_from_directory, Response
from functools import lru_cache
import sqlite3
import os
import gc
import pathlib
import time
import psutil
import io
import csv
import re
import shutil
import unicodedata
import uuid
import zipfile
import threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

import genbank_batch
import seq_analysis
import seq_distance
import seq_network
import seq_popgen
import seq_diversity
import seq_identify
# What bold_db_creator.py writes and this viewer reads: file and table names,
# the version and the database layout version
import db_contract
import layout

__version__ = db_contract.APP_VERSION

app = Flask(__name__)

# Where everything lives: see layout.py
_PROJECT_ROOT     = layout.ROOT
_EXPORTS_ROOT     = layout.EXPORTS_DIR
CSV_EXPORTS_DIR   = os.path.join(_EXPORTS_ROOT, 'csv_exports')
FASTA_EXPORTS_DIR = os.path.join(_EXPORTS_ROOT, 'fasta_exports')
BATCH_EXPORTS_DIR = os.path.join(_EXPORTS_ROOT, 'batch_exports')
GENBANK_EXPORTS_DIR = os.path.join(_EXPORTS_ROOT, 'genbank_exports')

os.makedirs(CSV_EXPORTS_DIR,   exist_ok=True)
os.makedirs(FASTA_EXPORTS_DIR, exist_ok=True)
os.makedirs(BATCH_EXPORTS_DIR, exist_ok=True)
os.makedirs(GENBANK_EXPORTS_DIR, exist_ok=True)

# File retention in export folders:
# after generating a new file, only the EXPORT_KEEP most recent files per
# folder are kept and the oldest ones are deleted, so they don't accumulate indefinitely.
EXPORT_KEEP = 20

def _prune_export_dir(directory, keep=EXPORT_KEEP):
    """Keep only the `keep` most recent files in `directory`; delete the rest."""
    try:
        files = [
            p for p in (os.path.join(directory, f) for f in os.listdir(directory))
            if os.path.isfile(p)
        ]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass

# Export configuration for records and FASTA
BATCH_SIZE = 50000  # Increased from 500 to 50,000 for FASTA
FASTA_BATCH_SIZE = 50000  # Specific to FASTA
UPDATE_INTERVAL = 10000  # Update status every 10k records
BUFFER_SIZE = 1024 * 1024  # 1MB buffer for writing
EXPORT_TIMEOUT = 3600  # 1 hour timeout


def _expected_total(data):
    """Record count the frontend already got from the search, if any.

    Only an estimate for the progress bar (the form may have changed since
    that search), which is why progress is capped at 99% until the export
    really finishes. Counting again here would cost a second full query.
    """
    try:
        n = int(data.get('expected_total') or 0)
    except (TypeError, ValueError):
        return 0
    return max(n, 0)


def _export_progress(status):
    total = status.get('total_rows') or 0
    if status.get('status') == 'completed':
        return 100
    if total <= 0:
        return 0
    return min(99, int(status.get('rows_read', 0) / total * 100))

# Export state (in memory)
fasta_exports  = {}
csv_exports    = {}
batch_exports  = {}
genbank_jobs   = {}
exports_lock   = threading.Lock()

# Active searches: {search_id: connection} so they can be interrupted
active_searches = {}
active_searches_lock = threading.Lock()

# Entries are never removed once an export finishes (only the on-disk files
# get pruned, via _prune_export_dir), so a long-lived server would otherwise
# accumulate one dict entry per export ever started. Called right after
# adding a new entry, under exports_lock, so pruning can't race a status read.
EXPORT_STATUS_KEEP = 200

def _prune_export_status(status_dict, keep=EXPORT_STATUS_KEEP):
    """Drops the oldest finished entries once there are more than `keep`.
    Entries still 'processing' are never dropped."""
    with exports_lock:
        finished = [
            (eid, st) for eid, st in status_dict.items()
            if st.get('status') != 'processing'
        ]
        if len(finished) <= keep:
            return
        finished.sort(key=lambda kv: kv[1].get('start_time') or datetime.min)
        for eid, _ in finished[:len(finished) - keep]:
            status_dict.pop(eid, None)

DATABASE_PATH = layout.database_path()

# Field name mapping: technical name -> friendly name.
#
# Covers all 76 columns of the public BOLD data package (see BOLD_FIELDS in
# dev/bold_db_creator.py), not just the ones selected by default: the creator's
# "Fields" panel lets you choose any subset, and get_friendly_name() only
# translates what's listed here — a missing field falls back to its technical
# name ('col_habitat' instead of 'Hábitat'). Filling it in completely up front
# avoids having to edit this file again every time the selection changes.
#
# The order of this dictionary is the column order in the results table (see
# index()) and in the viewer's "Fields" dropdown (index.html: const
# ORDERED_COLUMNS, generated from here). The columns that were selected by
# default until now (23) keep exactly their original order — they aren't
# touched when new fields are added. Each new field is inserted right after
# its closest neighbor among those 23, based on both fields' actual position
# in the BOLD TSV, rather than grouping them all at the end: that way a new
# column appears where someone familiar with the TSV would expect to find it,
# not buried at the bottom of the table.
FIELD_MAPPING = {
    'col_processid': 'ID BOLD',
    'col_sampleid': 'ID Muestra',
    'col_fieldid': 'ID Campo',
    'col_museumid': 'ID Museo',
    'col_record_id': 'ID Registro',
    'col_specimenid': 'ID Espécimen',
    'col_processid_minted_date': 'Fecha asignación ID',
    'col_bin_uri': 'BIN',
    'col_bin_created_date': 'Fecha creación BIN',
    'col_collection_code': 'Código colección',
    'col_inst': 'Institución',
    'col_sovereign_inst': 'Institución responsable',
    'col_taxid': 'ID Taxonómico',
    'col_identification': 'Identificación',
    'col_identification_method': 'Método identificación',
    'col_identification_rank': 'ID Nivel',
    'col_identified_by': 'Identificado por',
    'col_identifier_email': 'Correo identificador',
    'col_taxonomy_notes': 'Notas taxonómicas',
    'col_sex': 'Sexo',
    'col_reproduction': 'Reproducción',
    'col_life_stage': 'Estadio de vida',
    'col_short_note': 'Nota breve',
    'col_notes': 'Notas',
    'col_voucher_type': 'Tipo voucher',
    'col_tissue_type': 'Tipo tejido',
    'col_specimen_linkout': 'Enlace espécimen',
    'col_associated_specimens': 'Especímenes asociados',
    'col_associated_taxa': 'Taxones asociados',
    'col_collectors': 'Colectores',
    'col_collection_date_start': 'Fecha colecta inicio',
    'col_collection_date_end': 'Fecha colecta fin',
    'col_collection_event_id': 'ID Evento colecta',
    'col_collection_time': 'Hora colecta',
    'col_collection_notes': 'Notas colecta',
    'col_geoid': 'ID Geográfico',
    'col_marker_code': 'Marcador',
    'col_primers_forward': 'Primer forward',
    'col_primers_reverse': 'Primer reverse',
    'col_sequence_run_site': 'Sitio secuenciación',
    'col_nuc_basecount': 'Bases',
    'col_insdc_acs': 'Acceso INSDC',
    'col_funding_src': 'Fuente financiamiento',
    'col_kingdom': 'Reino',
    'col_phylum': 'Filo',
    'col_class': 'Clase',
    'col_order': 'Orden',
    'col_family': 'Familia',
    'col_subfamily': 'Subfamilia',
    'col_tribe': 'Tribu',
    'col_genus': 'Género',
    'col_species': 'Especie',
    'col_subspecies': 'Subespecie',
    'col_species_reference': 'Referencia especie',
    'col_country_ocean': 'País',
    'col_country_iso': 'Código País',
    'col_province_state': 'Estado',
    'col_region': 'Región',
    'col_sector': 'Sector',
    'col_site': 'Sitio',
    'col_site_code': 'Código sitio',
    'col_coord': 'Coordenadas',
    'col_coord_accuracy': 'Precisión coordenadas',
    'col_coord_source': 'Fuente coordenadas',
    'col_elev': 'Elevación',
    'col_elev_accuracy': 'Precisión elevación',
    'col_depth': 'Profundidad',
    'col_depth_accuracy': 'Precisión profundidad',
    'col_habitat': 'Hábitat',
    'col_realm': 'Reino biogeográfico',
    'col_biome': 'Bioma',
    'col_ecoregion': 'Ecorregión',
    'col_sampling_protocol': 'Protocolo muestreo',
    'col_sequence_upload_date': 'Fecha secuencia',
    'col_bold_recordset_code_arr': 'Conjuntos de datos BOLD',
    'col_nuc': 'Secuencia',
}

# English counterpart of FIELD_MAPPING, same keys, used when the active
# language is 'en' (see get_friendly_name()). Order doesn't matter here: the
# column order for the UI is always taken from FIELD_MAPPING.
FIELD_MAPPING_EN = {
    'col_processid': 'BOLD ID',
    'col_sampleid': 'Sample ID',
    'col_fieldid': 'Field ID',
    'col_museumid': 'Museum ID',
    'col_record_id': 'Record ID',
    'col_specimenid': 'Specimen ID',
    'col_processid_minted_date': 'ID Assignment Date',
    'col_bin_uri': 'BIN',
    'col_bin_created_date': 'BIN Creation Date',
    'col_collection_code': 'Collection Code',
    'col_inst': 'Institution',
    'col_sovereign_inst': 'Sovereign Institution',
    'col_taxid': 'Taxonomic ID',
    'col_identification': 'Identification',
    'col_identification_method': 'Identification Method',
    'col_identification_rank': 'ID Rank',
    'col_identified_by': 'Identified By',
    'col_identifier_email': 'Identifier Email',
    'col_taxonomy_notes': 'Taxonomy Notes',
    'col_sex': 'Sex',
    'col_reproduction': 'Reproduction',
    'col_life_stage': 'Life Stage',
    'col_short_note': 'Short Note',
    'col_notes': 'Notes',
    'col_voucher_type': 'Voucher Type',
    'col_tissue_type': 'Tissue Type',
    'col_specimen_linkout': 'Specimen Linkout',
    'col_associated_specimens': 'Associated Specimens',
    'col_associated_taxa': 'Associated Taxa',
    'col_collectors': 'Collectors',
    'col_collection_date_start': 'Collection Date Start',
    'col_collection_date_end': 'Collection Date End',
    'col_collection_event_id': 'Collection Event ID',
    'col_collection_time': 'Collection Time',
    'col_collection_notes': 'Collection Notes',
    'col_geoid': 'Geo ID',
    'col_marker_code': 'Marker',
    'col_primers_forward': 'Forward Primer',
    'col_primers_reverse': 'Reverse Primer',
    'col_sequence_run_site': 'Sequencing Site',
    'col_nuc_basecount': 'Base Count',
    'col_insdc_acs': 'INSDC Accession',
    'col_funding_src': 'Funding Source',
    'col_kingdom': 'Kingdom',
    'col_phylum': 'Phylum',
    'col_class': 'Class',
    'col_order': 'Order',
    'col_family': 'Family',
    'col_subfamily': 'Subfamily',
    'col_tribe': 'Tribe',
    'col_genus': 'Genus',
    'col_species': 'Species',
    'col_subspecies': 'Subspecies',
    'col_species_reference': 'Species Reference',
    'col_country_ocean': 'Country',
    'col_country_iso': 'Country Code',
    'col_province_state': 'State/Province',
    'col_region': 'Region',
    'col_sector': 'Sector',
    'col_site': 'Site',
    'col_site_code': 'Site Code',
    'col_coord': 'Coordinates',
    'col_coord_accuracy': 'Coordinate Accuracy',
    'col_coord_source': 'Coordinate Source',
    'col_elev': 'Elevation',
    'col_elev_accuracy': 'Elevation Accuracy',
    'col_depth': 'Depth',
    'col_depth_accuracy': 'Depth Accuracy',
    'col_habitat': 'Habitat',
    'col_realm': 'Biogeographic Realm',
    'col_biome': 'Biome',
    'col_ecoregion': 'Ecoregion',
    'col_sampling_protocol': 'Sampling Protocol',
    'col_sequence_upload_date': 'Sequence Date',
    'col_bold_recordset_code_arr': 'BOLD Dataset Codes',
    'col_nuc': 'Sequence',
}

# Reverse mapping to convert friendly names back to technical names. Combines
# both languages: the frontend sends the friendly name in the active language
# as "column", so we need to be able to reverse either one.
REVERSE_MAPPING = {v: k for k, v in FIELD_MAPPING.items()}
REVERSE_MAPPING.update({v: k for k, v in FIELD_MAPPING_EN.items()})

# Columns excluded from the search selector
EXCLUDED_COLUMNS = {'col_subspecies'}

# Columns offered as summary metrics in the batch search.
# The list is deliberately short: these are low-cardinality categorical
# fields, where "how many distinct values and which ones" is a meaningful
# question and the answer fits in a cell. Excluded are identifiers
# (BIN, BOLD ID, Sample ID, Field ID, Museum ID), coordinates, and the
# sequence: there, almost every record has its own value, so the count
# would equal the record count and the list would be unreadable.
#
# The whitelist also serves as SQL injection protection: these names are
# interpolated into the metrics queries, and nothing outside this list ever is.
#
# The order is what the user sees in the panel and the order of the columns
# added to summary.csv: first the most-queried fields (marker and geography),
# then taxonomy from specific to general, and finally the occasionally-used fields.
METRIC_COLUMNS = [
    'col_marker_code',
    'col_country_ocean',
    'col_province_state',
    'col_species', 'col_genus', 'col_family', 'col_order',
    'col_class', 'col_phylum', 'col_kingdom',
    'col_sequence_upload_date',
    'col_identification_rank',
]

# Labels specific to the summary report. Outside this section the friendly
# names are shared with the results table and with the frontend's
# ORDERED_COLUMNS, so they can't be renamed in FIELD_MAPPING without breaking
# the column selector; here they can, because the label only reaches the
# batch panel and the headers of the CSVs that panel generates.
METRIC_LABELS = {
    'col_identification_rank': 'Nivel taxonómico',
}
METRIC_LABELS_EN = {
    'col_identification_rank': 'Taxonomic Rank',
}

# Fields that head the "Select field..." dropdowns in the batch search, since
# they're the ones most used as term lists. The remaining fields are added
# afterward, in the usual results-table order. This is a presentation list
# only: it doesn't restrict anything, any field remains selectable.
BATCH_FIELD_PRIORITY = [
    'col_marker_code',
    'col_country_ocean',
    'col_province_state',
    'col_species', 'col_genus', 'col_family', 'col_order',
    'col_class', 'col_phylum', 'col_kingdom',
    'col_sequence_upload_date',
]

def get_metric_label(technical_name):
    """Name of a field within the batch search's summary report."""
    labels = METRIC_LABELS if get_lang() == 'es' else METRIC_LABELS_EN
    return labels.get(technical_name) or get_friendly_name(technical_name)

# Measures offered by the on-demand report panel next to the results table
# ("Filas"/"Columnas"/"Valor"). Like METRIC_COLUMNS, this is a fixed
# whitelist: the SQL snippet is never built from user input, only picked
# from here by key.
REPORT_VALUES = {
    'records': ('COUNT(*)',
                {'es': 'Nº de registros', 'en': 'Number of records'}),
    'species': ('COUNT(DISTINCT r.col_species)',
                {'es': 'Nº de especies distintas', 'en': 'Number of distinct species'}),
    'bins':    ('COUNT(DISTINCT r.col_bin_uri)',
                {'es': 'Nº de BINs distintos', 'en': 'Number of distinct BINs'}),
}

# Distinct row/column categories the report keeps. Rows beyond the cap are
# simply left out (the UI notes it); columns beyond the cap are folded into
# a single "Other" column, since unbounded columns would break the table's
# layout rather than just make it longer.
REPORT_ROW_CAP = 300
REPORT_COL_CAP = 15

# Largest report the Excel export writes as a sheet. The format itself allows
# far more (16,384 columns, 1M rows), but such a file is unmanageable; a
# report beyond any of these goes out as CSV instead.
XLSX_MAX_COLS = 500
XLSX_MAX_ROWS = 50000
XLSX_MAX_CELLS = 2000000
MAX_BOOK_TABLES = 20


def report_excel_limit(n_rows, n_cols):
    """None if a report of n_rows x n_cols categories fits an Excel sheet, or
    the limit it breaks ('cols', 'rows' or 'cells'), in the orientation the
    export will use: a table much wider than tall is turned on its side."""
    if n_cols > 2 * n_rows:
        n_rows, n_cols = n_cols, n_rows
    if n_cols > XLSX_MAX_COLS:
        return 'cols'
    if n_rows > XLSX_MAX_ROWS:
        return 'rows'
    if n_rows * max(n_cols, 1) > XLSX_MAX_CELLS:
        return 'cells'
    return None

# Cap on distinct values accumulated in memory per (group, column). It's not
# an accuracy limit: once exceeded, that group's counter is abandoned and the
# figure is recalculated with an exact query (see `_metric_exact`). It only
# bounds how much RAM the accumulator can use before falling back to SQLite.
METRIC_MAX_DISTINCT = 50000

# Maximum values in a list cell when the user didn't set their own limit and
# the group overflowed. Avoids rebuilding in memory what the cap had just
# avoided.
METRIC_OVERFLOW_LIST = 1000

# With two fields, summary.csv also lists the term × value combinations with
# no records, at 0. Above this many they're left out: Excel can't open more
# than 1,048,576 rows, and the information is still in the not_found.txt files.
SUMMARY_ZERO_ROWS_MAX = 1_000_000

FTS_TABLE_NAME = db_contract.FTS_TABLE

def _connect_readonly(timeout):
    """Read-only connection to the database.

    The viewer never writes to it. Opening it read-write (and switching it
    to WAL, a setting that persists in the file) let the viewer alter a
    database bold_db_creator.py might be rebuilding at that very moment, and
    connecting to a missing file silently created an empty bold_db.db.
    Temporary tables (batch search) still work: they live in a separate,
    writable temp database.
    """
    uri = pathlib.Path(DATABASE_PATH).as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def get_db_connection():
    """Optimized connection for interactive searches"""
    conn = _connect_readonly(60.0)

    cursor = conn.cursor()
    cursor.execute('PRAGMA cache_size = -262144')  # 256MB cache
    cursor.execute('PRAGMA temp_store = MEMORY')
    cursor.execute('PRAGMA mmap_size = 1073741824')  # 1GB memory-mapped

    return conn

MAIN_TABLE_NAME = db_contract.MAIN_TABLE

@lru_cache(maxsize=1)
def get_column_names():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Looked up by name. It used to take "the first table that isn't the
        # FTS one" with no ORDER BY: that only worked because bold_records
        # happens to be created before the FTS index's shadow tables
        # (bold_records_fts_data, _idx, ...), which the filter didn't exclude.
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                       (MAIN_TABLE_NAME,))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(t('no_table_found'))
        table_name = row[0]
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [row[1] for row in cursor.fetchall()]
        return columns, table_name
    finally:
        conn.close()

@lru_cache(maxsize=1)
def get_numeric_columns():
    """Columns declared with a numeric type (today only col_nuc_basecount).

    Their range comparisons are written against the bare column so SQLite
    can use its index: CAST(col AS REAL) > ? turned the comparison into an
    expression no index covers, and every such search scanned the table.
    """
    columns, table_name = get_column_names()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        numeric = set()
        for row in cursor.fetchall():
            decl = (row[2] or '').upper()
            if any(k in decl for k in ('INT', 'REAL', 'FLOA', 'DOUB', 'NUM')):
                numeric.add(row[1])
        return numeric
    finally:
        conn.close()


def resolve_sort(data, technical_columns):
    """Validates the request's sort_col/sort_dir, returning (column, SQL direction).

    Returns (None, None) when the caller didn't ask to sort. Raises
    ValueError for a column that doesn't exist or isn't indexed — the same
    way build_where_clause() rejects an unknown column — rather than
    silently ignoring it or running the slow unindexed sort. Sorting is
    restricted to indexed columns (get_indexed_columns(), defined with the
    autocomplete code below) because ordering the whole matching set
    through a non-leading column means SQLite sorts it in a temp B-tree
    instead of walking an index — on a ~20M-row database the same order of
    magnitude as building an index from scratch, i.e. minutes (see
    bold_db_creator.py's index_cols()).
    """
    sort_col = data.get('sort_col')
    if not sort_col:
        return None, None
    column = get_technical_name(sort_col)
    if column not in technical_columns or column not in get_indexed_columns():
        raise ValueError(t('invalid_sort_column'))
    direction = 'DESC' if str(data.get('sort_dir', 'asc')).upper() == 'DESC' else 'ASC'
    return column, direction


@lru_cache(maxsize=1)
def fts_table_available():
    """Indicates whether the database includes the full-text search index.

    The FTS index is optional: `bold_db_creator.py` can build the database
    without it, in which case the full-text search tab has nothing to work
    with. Cached (lru_cache) until the database file changes: see db_problem().
    """
    try:
        conn = get_db_connection()
    except sqlite3.Error:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (FTS_TABLE_NAME,),
        )
        return cursor.fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def get_friendly_name(technical_name):
    """Converts a technical name to its friendly name, in the active language"""
    mapping = FIELD_MAPPING if get_lang() == 'es' else FIELD_MAPPING_EN
    return mapping.get(technical_name, technical_name)

def get_technical_name(friendly_name):
    """Converts a friendly name to its technical name"""
    return REVERSE_MAPPING.get(friendly_name, friendly_name)


def build_where_clause(conditions, technical_columns):
    """
    Builds a WHERE clause with support for AND, OR, NOT and parenthesized grouping.

    `technical_columns` (the real column names from get_column_names()) is
    required: it's the whitelist that keeps a condition's column name from
    being interpolated into the SQL as a bare, unchecked identifier.

    Expected structure:
    [
        {
            'column': 'friendly_name',
            'operator': 'EQUALS',
            'value': 'value',
            'condition_type': 'AND'  # AND, OR, NOT
        },
        {
            'condition_type': 'GROUP_START'  # Start of group
        },
        {
            'column': 'friendly_name',
            'operator': 'EQUALS',
            'value': 'value',
            'condition_type': 'OR'
        },
        {
            'condition_type': 'GROUP_END'  # End of group
        }
    ]
    """
    query_parts = []
    parameters = []
    group_level = 0
    _first_cond = True

    for i, condition in enumerate(conditions):
        condition_type = condition.get('condition_type', 'AND')

        # Handle group start/end
        if condition_type == 'GROUP_START':
            query_parts.append('(')
            group_level += 1
            continue
        elif condition_type == 'GROUP_END':
            if group_level > 0:
                group_level -= 1
                if query_parts and query_parts[-1] == '(':
                    # Nothing was added inside: drop the empty "()" rather
                    # than emitting invalid SQL.
                    query_parts.pop()
                else:
                    query_parts.append(')')
            continue

        # Normal conditions
        column_friendly = condition.get('column')
        column = get_technical_name(column_friendly) if column_friendly else None
        operator = condition.get('operator')
        value = _strip_accents(condition.get('value')) if isinstance(condition.get('value'), str) else condition.get('value')

        if not all([column, operator]):
            continue

        if column not in technical_columns:
            raise ValueError(t('invalid_search_conditions'))

        # Build the individual condition. Values are accent-stripped above:
        # bold_db_creator.py strips accents from every text field on import
        # (see _strip_accents there), so the table never has an accented
        # value to match against — 'México' typed here has to become
        # 'Mexico' or it would silently match nothing.
        value2 = condition.get('value2')
        value2 = _strip_accents(value2) if isinstance(value2, str) else value2
        condition_query = build_single_condition(
            column, operator, value, parameters, value2,
            numeric=column in get_numeric_columns())
        if not condition_query:
            continue

        # Determine the logical operator
        if _first_cond:
            query_parts.append(condition_query)
            _first_cond = False
        else:
            if condition_type == 'OR':
                query_parts.append(' OR ')
            elif condition_type == 'NOT':
                query_parts.append(' AND NOT ')
            else:  # AND by default
                query_parts.append(' AND ')

            query_parts.append(condition_query)

    # Close any group left open
    while group_level > 0:
        group_level -= 1
        if query_parts and query_parts[-1] == '(':
            query_parts.pop()
        else:
            query_parts.append(')')

    if not query_parts:
        raise ValueError(t('invalid_search_conditions'))

    return ''.join(query_parts), parameters


def build_single_condition(column, operator, value, parameters, value2=None, numeric=False):
    """Builds a single condition.

    `numeric`: the column has a numeric type, so range comparisons use the
    bare column (indexable) instead of CAST(column AS REAL). Text columns
    keep the CAST: their values have to be converted to compare as numbers.
    """
    lhs = column if numeric else f"CAST({column} AS REAL)"
    if operator == 'IS_NOT_EMPTY':
        return f"({column} IS NOT NULL AND {column} != '')"
    elif operator == 'IS_EMPTY':
        return f"({column} IS NULL OR {column} = '')"
    elif operator == 'LIKE':
        parameters.append(f"%{value}%")
        return f"{column} COLLATE NOCASE LIKE ?"
    elif operator == 'NOT_LIKE':
        parameters.append(f"%{value}%")
        return f"{column} COLLATE NOCASE NOT LIKE ?"
    elif operator == 'EQUALS':
        parameters.append(value)
        return f"{column} COLLATE NOCASE = ?"
    elif operator == 'NOT_EQUALS':
        parameters.append(value)
        return f"{column} COLLATE NOCASE != ?"
    elif operator == 'STARTS_WITH':
        parameters.append(f"{value}%")
        return f"{column} COLLATE NOCASE LIKE ?"
    elif operator == 'ENDS_WITH':
        parameters.append(f"%{value}")
        return f"{column} COLLATE NOCASE LIKE ?"
    elif operator == 'GREATER_THAN':
        parameters.append(value)
        return f"{lhs} > CAST(? AS REAL)"
    elif operator == 'LESS_THAN':
        parameters.append(value)
        return f"{lhs} < CAST(? AS REAL)"
    elif operator == 'BETWEEN':
        if value in (None, '') or value2 in (None, ''):
            return None
        try:
            v1, v2 = float(value), float(value2)
        except (ValueError, TypeError):
            v1, v2 = value, value2
        if str(v1) != value or str(v2) != value2:
            # At least one is numeric: sort
            v1, v2 = (v1, v2) if v1 <= v2 else (v2, v1)
        parameters.append(v1)
        parameters.append(v2)
        return f"{lhs} BETWEEN CAST(? AS REAL) AND CAST(? AS REAL)"
    else:
        return None


# ── Internationalization ──────────────────────────────────────────────────────
#
# All the text the backend returns to the browser (error messages, progress,
# status) lives here, instead of being scattered as literals across the
# routes. The active language is decided by the `lang` cookie set by the
# frontend (see `setLang()` in index.html); Spanish by default.

STRINGS = {
    'es': {
        'no_table_found': 'No se encontró ninguna tabla en la base de datos',
        'db_problem_title': 'La base de datos no está disponible',
        'db_missing': 'Todavía no hay base de datos. Créala con BOLD-Local-DB (acceso directo «BOLD-Local-DB» o «Start BOLD-Local-DB» en la carpeta del programa) y recarga esta página.',
        'db_unreadable': 'No se pudo abrir la base de datos: puede estar a medio crear o dañada. Vuelve a crearla con BOLD-Local-DB (acceso directo «BOLD-Local-DB» o «Start BOLD-Local-DB» en la carpeta del programa).',
        'db_schema_too_old': 'Esta base de datos tiene un formato que esta versión del visor ya no lee. Reconstrúyela con BOLD-Local-DB (acceso directo «BOLD-Local-DB» o «Start BOLD-Local-DB» en la carpeta del programa).',
        'db_schema_too_new': 'Esta base de datos se creó con una versión más reciente del programa. Actualiza el proyecto para poder abrirla.',
        'invalid_search_conditions': 'Condiciones de búsqueda inválidas',
        'invalid_sort_column': 'No se puede ordenar por esa columna',
        'no_search_conditions': 'No se proporcionaron condiciones de búsqueda',
        'no_search_query': 'No se proporcionó ninguna consulta de búsqueda',
        'fts_unavailable': 'Esta base de datos no incluye el índice de búsqueda de texto completo. Usa la búsqueda por campos.',
        'fts_unavailable_short': 'Esta base de datos no incluye el índice de búsqueda de texto completo',
        'no_data_provided': 'No se proporcionaron datos',
        'fasta_preparing': 'Preparando exportación FASTA...',
        'fasta_export_started': 'Exportación FASTA iniciada',
        'unexpected_error': 'Error inesperado: {error}',
        'invalid_export_id': 'ID de exportación no válido',
        'fasta_not_available': 'Archivo FASTA no disponible',
        'export_not_completed': 'Exportación no completada',
        'file_not_found': 'Archivo no encontrado',
        'no_search_term': 'No se proporcionó término de búsqueda',
        'exporting_sequences': 'Exportando secuencias...',
        'no_sequences_found': 'No se encontraron secuencias para exportar.',
        'fasta_progress': '{count} secuencias exportadas ({rate}/s)',
        'fasta_completed': 'Exportación completada: {count} secuencias en {duration}',
        'fasta_export_error': 'Error en exportación FASTA: {error}',
        'csv_preparing': 'Preparando exportación CSV...',
        'csv_export_started': 'Exportación CSV iniciada',
        'no_results_found': 'No se encontraron resultados para exportar.',
        'csv_progress': '{count} registros exportados ({rate}/s)',
        'csv_completed': 'Exportación completada: {count} registros en {duration}',
        'csv_export_error': 'Error en la exportación: {error}',
        'csv_not_available': 'Archivo CSV no disponible',
        'file_not_found_system': 'Archivo no encontrado en el sistema',
        'invalid_search_conditions_alt': 'Condiciones de búsqueda no válidas',
        'searching_terms': 'Buscando {count} términos...',
        'searching_terms_one': 'Buscando {count} término...',
        'no_records_for_term': 'No se encontraron registros para «{term}». No se generó ningún archivo.',
        'no_records_after_filter': 'El campo 1 tiene registros, pero no coinciden con {target}. No se generó ningún archivo.',
        'filter_target_one': 'el valor de {field} «{value}»',
        'filter_target_many': 'ninguno de los {count} valores de {field}',
        'records_found_packing_one': '{count} registro encontrado. Empaquetando...',
        'records_found_one': '{count} registro encontrado.',
        'summary_zeros_omitted': ' summary.csv no incluye las {count} combinaciones sin registros (demasiadas para Excel): se deducen de los not_found.txt y de summary_by_field2.csv.',
        'field2_no_values': 'El archivo del campo 2 no tiene ningún valor',
        'filtering_by': 'Filtrando por {field}...',
        'no_records_for_terms': 'No se encontraron registros para ninguno de los {count} términos. No se generó ningún archivo.',
        'records_found_packing': '{count} registros encontrados. Empaquetando...',
        'recalculating_metrics': 'Recalculando métricas de alta cardinalidad...',
        'adding_all_results': 'Añadiendo all_results.tsv...',
        'records_found': '{count} registros encontrados.',
        'search_cancelled': 'Búsqueda cancelada.',
        'generic_error': 'Error: {error}',
        'field_required': 'Se requiere al menos un campo con valores',
        'starting_search': 'Iniciando búsqueda...',
        'invalid_id': 'ID no válido',
        'not_available': 'No disponible',
        'invalid_field': 'Campo no válido: {field}',
        'col_records': 'Registros',
        'col_total_records': 'Total Registros',
        'col_found': '{field} Encontrados',
        'col_not_found': '{field} No Encontrados',
        'and_n_more': '… y {n} más',
        'genbank_terms_required': 'Introduce al menos un término para el criterio 1',
        'genbank_field2_no_terms': 'El criterio 2 está activado pero no tiene términos',
        'genbank_invalid_field': 'Campo de NCBI no válido: {field}',
        'genbank_already_running': 'Ya hay una búsqueda en GenBank en curso. Espera a que termine o detenla.',
        'genbank_log_count': '{query}: {n} registro(s)',
        'genbank_log_capped': ' — descargados solo {n} (límite por consulta)',
        'genbank_log_chunks_failed': ' — ⚠ {n} bloque(s) no se pudieron descargar',
        'genbank_log_failed': '{query}: ⚠ ERROR — NCBI no respondió tras varios intentos',
        'genbank_packing': 'Empaquetando resultados...',
        'genbank_done': 'Listo: {done} consulta(s), {records} registro(s) descargados.',
        'genbank_done_failed': ' {n} consulta(s) fallaron (marcadas como ERROR en genbank_counts.csv).',
        'genbank_stopped': 'Detenida tras {done} de {total} consultas. El ZIP contiene los resultados parciales.',
        'analysis_invalid_request': 'Solicitud de análisis no válida',
        'analysis_no_sequences': 'No hay secuencias que analizar',
        'analysis_fasta_too_large': 'El archivo FASTA supera el máximo de {mb} MB',
        'analysis_distance_input_too_big': 'La entrada tiene más de {limit} secuencias de este marcador. Acota la búsqueda: un análisis de distancias sobre una parte arbitraria sería engañoso.',
        'analysis_too_many': 'Hay {n} secuencias y el máximo para distancias con este marcador es {limit}. Limita las secuencias por especie o acota la búsqueda.',
        'analysis_too_few': 'Se necesitan al menos {limit} secuencias alineables; hay {n}.',
        'analysis_too_few_stops': ' Otras {k} quedaron fuera por codones de parada: con tantas, lo más probable es que el código genético elegido no corresponda a este grupo. Revísalo en «Código genético» (o déjalo en automático) y repite.',
        'analysis_tree_too_many': 'Árbol no calculado: hay {n} secuencias y el máximo es {limit}. Limita las secuencias por especie.',
        'analysis_tree_too_few': 'Árbol no calculado: solo {n} secuencias tienen distancias con todas las demás.',
        'analysis_tree_group': 'Grupo {k}: ',
        'analysis_delimit_too_few': 'Delimitación no calculada: solo {n} secuencias tienen distancias con todas las demás.',
        'analysis_network_too_many': 'Hay {n} secuencias y una red admite {limit}. Elige una especie o limita la búsqueda.',
        'analysis_network_too_few': 'Hacen falta al menos {limit} secuencias alineables para una red (hay {n}).',
        'analysis_network_too_many_haplotypes': 'Hay {n} haplotipos y este método admite {limit}. Elige una especie o usa MSN, que admite más.',
        'analysis_network_too_many_species': 'Esta especie tiene {n} secuencias y una red admite {limit}. Limita la búsqueda (por país, por ejemplo).',
        'analysis_network_too_many_haplotypes_species': 'Esta especie tiene {n} haplotipos y TCS admite {limit}. Usa MSN, que admite más, o limita la búsqueda (por país, por ejemplo).',
        'analysis_network_too_many_haplotypes_msn': 'Hay {n} haplotipos y MSN admite {limit}. Elige una especie o limita la búsqueda.',
        'analysis_network_too_many_haplotypes_msn_species': 'Esta especie tiene {n} haplotipos y MSN admite {limit}. Limita la búsqueda (por país, por ejemplo).',
        'analysis_network_short_core': 'Las secuencias solo comparten {n} posiciones completas (mínimo {limit}): son de regiones distintas del marcador o tienen muchas N.',
        'analysis_delimit_too_many': 'Delimitación no calculada: hay {n} secuencias y el máximo es {limit}. Limita las secuencias por especie.',
        'identify_index_failed': 'No se pudo crear el índice: {error}',
        'identify_index_busy': 'Ya se está creando un índice. Espera a que termine.',
        'identify_bad_thresholds': 'Los umbrales deben estar entre 50 y 100 % y cumplir especie ≥ género ≥ familia.',
        'identify_index_missing': 'Este marcador aún no tiene índice de identificación. Créalo primero.',
        'identify_index_stale': 'El índice de este marcador es de una versión anterior de la base de datos. Vuelve a crearlo.',
        'identify_index_building': 'El índice de este marcador se está creando. Espera a que termine.',
        'identify_too_many': 'Hay {n} secuencias y el máximo por análisis es {limit}.',
        'report_row_field_required': 'Elige un campo para «Filas»',
        'report_invalid_value': 'Medida no válida: {value}',
        'report_other_label': 'Otros',
        'report_total_label': 'Total',
        'report_empty_value': '(vacío)',
    },
    'en': {
        'no_table_found': 'No table found in the database',
        'db_problem_title': 'The database is not available',
        'db_missing': 'There is no database yet. Create it with BOLD-Local-DB (the "BOLD-Local-DB" shortcut, or "Start BOLD-Local-DB" in the program folder) and reload this page.',
        'db_unreadable': 'The database could not be opened: it may be half-built or damaged. Build it again with BOLD-Local-DB (the "BOLD-Local-DB" shortcut, or "Start BOLD-Local-DB" in the program folder).',
        'db_schema_too_old': 'This database has a layout this version of the viewer no longer reads. Rebuild it with BOLD-Local-DB (the "BOLD-Local-DB" shortcut, or "Start BOLD-Local-DB" in the program folder).',
        'db_schema_too_new': 'This database was built by a newer version of the program. Update the project to open it.',
        'invalid_search_conditions': 'Invalid search conditions',
        'invalid_sort_column': 'Cannot sort by that column',
        'no_search_conditions': 'No search conditions provided',
        'no_search_query': 'No search query provided',
        'fts_unavailable': 'This database does not include the full-text search index. Use the field search instead.',
        'fts_unavailable_short': 'This database does not include the full-text search index',
        'no_data_provided': 'No data provided',
        'fasta_preparing': 'Preparing FASTA export...',
        'fasta_export_started': 'FASTA export started',
        'unexpected_error': 'Unexpected error: {error}',
        'invalid_export_id': 'Invalid export ID',
        'fasta_not_available': 'FASTA file not available',
        'export_not_completed': 'Export not completed',
        'file_not_found': 'File not found',
        'no_search_term': 'No search term provided',
        'exporting_sequences': 'Exporting sequences...',
        'no_sequences_found': 'No sequences found to export.',
        'fasta_progress': '{count} sequences exported ({rate}/s)',
        'fasta_completed': 'Export completed: {count} sequences in {duration}',
        'fasta_export_error': 'Error during FASTA export: {error}',
        'csv_preparing': 'Preparing CSV export...',
        'csv_export_started': 'CSV export started',
        'no_results_found': 'No results found to export.',
        'csv_progress': '{count} records exported ({rate}/s)',
        'csv_completed': 'Export completed: {count} records in {duration}',
        'csv_export_error': 'Error during export: {error}',
        'csv_not_available': 'CSV file not available',
        'file_not_found_system': 'File not found on the system',
        'invalid_search_conditions_alt': 'Invalid search conditions',
        'searching_terms': 'Searching {count} terms...',
        'searching_terms_one': 'Searching {count} term...',
        'no_records_for_term': 'No records found for "{term}". No file was generated.',
        'no_records_after_filter': 'Field 1 has records, but none of them match {target}. No file was generated.',
        'filter_target_one': 'the {field} value "{value}"',
        'filter_target_many': 'any of the {count} {field} values',
        'records_found_packing_one': '{count} record found. Packaging...',
        'records_found_one': '{count} record found.',
        'summary_zeros_omitted': ' summary.csv leaves out the {count} combinations with no records (too many for Excel): they can be derived from the not_found.txt files and summary_by_field2.csv.',
        'field2_no_values': 'The Field 2 file has no values',
        'filtering_by': 'Filtering by {field}...',
        'no_records_for_terms': 'No records found for any of the {count} terms. No file was generated.',
        'records_found_packing': '{count} records found. Packaging...',
        'recalculating_metrics': 'Recalculating high-cardinality metrics...',
        'adding_all_results': 'Adding all_results.tsv...',
        'records_found': '{count} records found.',
        'search_cancelled': 'Search cancelled.',
        'generic_error': 'Error: {error}',
        'field_required': 'At least one field with values is required',
        'starting_search': 'Starting search...',
        'invalid_id': 'Invalid ID',
        'not_available': 'Not available',
        'invalid_field': 'Invalid field: {field}',
        'col_records': 'Records',
        'col_total_records': 'Total Records',
        'col_found': '{field} Found',
        'col_not_found': '{field} Not Found',
        'and_n_more': '… and {n} more',
        'genbank_terms_required': 'Enter at least one term for Criterion 1',
        'genbank_field2_no_terms': 'Criterion 2 is enabled but has no terms',
        'genbank_invalid_field': 'Invalid NCBI field: {field}',
        'genbank_already_running': 'A GenBank search is already running. Wait for it to finish or stop it.',
        'genbank_log_count': '{query}: {n} record(s)',
        'genbank_log_capped': ' — only {n} downloaded (per-query cap)',
        'genbank_log_chunks_failed': ' — ⚠ {n} batch(es) could not be downloaded',
        'genbank_log_failed': '{query}: ⚠ ERROR — NCBI did not respond after several attempts',
        'genbank_packing': 'Packaging results...',
        'genbank_done': 'Done: {done} query(ies), {records} record(s) downloaded.',
        'genbank_done_failed': ' {n} query(ies) failed (marked as ERROR in genbank_counts.csv).',
        'genbank_stopped': 'Stopped after {done} of {total} queries. The ZIP contains the partial results.',
        'analysis_invalid_request': 'Invalid analysis request',
        'analysis_no_sequences': 'There are no sequences to analyse',
        'analysis_fasta_too_large': 'The FASTA file exceeds the {mb} MB maximum',
        'analysis_distance_input_too_big': 'The input has more than {limit} sequences of this marker. Narrow the search: a distance analysis of an arbitrary part of it would mislead.',
        'analysis_too_many': 'There are {n} sequences and the most for distances with this marker is {limit}. Limit the sequences per species or narrow the search.',
        'analysis_too_few': 'At least {limit} alignable sequences are needed; there are {n}.',
        'analysis_too_few_stops': ' Another {k} were left out for stop codons: with so many, the genetic code chosen most likely doesn\'t suit this group. Check it in "Genetic code" (or leave it automatic) and run again.',
        'analysis_tree_too_many': 'No tree: there are {n} sequences and the most is {limit}. Limit the sequences per species.',
        'analysis_tree_too_few': 'No tree: only {n} sequences have distances to all the others.',
        'analysis_tree_group': 'Group {k}: ',
        'analysis_delimit_too_few': 'No delimitation: only {n} sequences have distances to all the others.',
        'analysis_network_too_many': 'There are {n} sequences and a network takes {limit}. Choose a species or narrow the search.',
        'analysis_network_too_few': 'A network needs at least {limit} sequences that align (there are {n}).',
        'analysis_network_too_many_haplotypes': 'There are {n} haplotypes and this method takes {limit}. Choose a species or use MSN, which takes more.',
        'analysis_network_too_many_species': 'This species has {n} sequences and a network takes {limit}. Narrow the search (by country, for instance).',
        'analysis_network_too_many_haplotypes_species': 'This species has {n} haplotypes and TCS takes {limit}. Use MSN, which takes more, or narrow the search (by country, for instance).',
        'analysis_network_too_many_haplotypes_msn': 'There are {n} haplotypes and MSN takes {limit}. Choose a species or narrow the search.',
        'analysis_network_too_many_haplotypes_msn_species': 'This species has {n} haplotypes and MSN takes {limit}. Narrow the search (by country, for instance).',
        'analysis_network_short_core': 'The sequences only share {n} complete positions (minimum {limit}): they cover different regions of the marker or have many Ns.',
        'analysis_delimit_too_many': 'No delimitation: there are {n} sequences and the most is {limit}. Limit the sequences per species.',
        'identify_index_failed': 'The index could not be built: {error}',
        'identify_index_busy': 'An index is already being built. Wait for it to finish.',
        'identify_bad_thresholds': 'Thresholds must be between 50 and 100 % with species ≥ genus ≥ family.',
        'identify_index_missing': 'This marker has no identification index yet. Build it first.',
        'identify_index_stale': "This marker's index is from an earlier version of the database. Build it again.",
        'identify_index_building': "This marker's index is being built. Wait for it to finish.",
        'identify_too_many': 'There are {n} sequences and the most per run is {limit}.',
        'report_row_field_required': 'Choose a field for "Rows"',
        'report_invalid_value': 'Invalid measure: {value}',
        'report_other_label': 'Other',
        'report_total_label': 'Total',
        'report_empty_value': '(empty)',
    },
}

_lang_override = threading.local()

def get_lang():
    # Background export/search threads have no Flask request context, so
    # `request.cookies` would raise RuntimeError there. start_background()
    # captures the caller's language and stashes it here before the thread
    # runs, so t() keeps working once the request context is gone.
    override = getattr(_lang_override, 'value', None)
    if override:
        return override
    lang = request.cookies.get('lang', 'en')
    return lang if lang in STRINGS else 'en'

def t(key, **kwargs):
    template = STRINGS[get_lang()].get(key) or STRINGS['en'].get(key, key)
    return template.format(**kwargs) if kwargs else template

def tn(key, n, **kwargs):
    """t() for a message that depends on a count: uses `<key>_one` when n == 1
    ("1 term", not "1 terms") and passes n, formatted, as `count`."""
    one = f'{key}_one'
    if n == 1 and (one in STRINGS[get_lang()] or one in STRINGS['en']):
        key = one
    return t(key, count=f'{n:,}', **kwargs)

def start_background(target, *args, **kwargs):
    """Starts a daemon thread carrying over the current request's language.

    Plain threading.Thread() gives the new thread no Flask request context,
    so any t()/get_lang() call inside it would crash with "Working outside
    of request context" (or, before that was noticed, would silently die on
    the first status update and leave the export stuck at 0% forever).
    """
    lang = get_lang()

    def runner():
        _lang_override.value = lang
        target(*args, **kwargs)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread


# ── Database state ────────────────────────────────────────────────────────────
#
# The viewer can stay open while the Creator rebuilds or updates the database.
# Whatever is cached about it (columns, FTS index) is dropped as soon as the
# file changes, and its layout version is checked against the range this
# viewer reads (db_contract). Before, an incompatible or missing database only
# showed up as an SQL error in the middle of a search.

_db_state = {"checked": False, "sig": None, "problem": None}
_db_state_lock = threading.Lock()

# Routes that query the database. Status, download and GenBank routes don't.
_DB_ROUTES = ('/api/search', '/api/autocomplete', '/api/export_fasta',
              '/api/export_csv', '/api/explain', '/api/batch_search',
              '/total_records')


def _db_signature():
    """Changes whenever the Creator writes to the database or replaces it."""
    try:
        st = os.stat(DATABASE_PATH)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _check_db():
    """STRINGS key describing why the database can't be used, or None."""
    if not os.path.exists(DATABASE_PATH):
        return 'db_missing'
    try:
        conn = _connect_readonly(5.0)
        try:
            version = db_contract.schema_version(conn)
            conn.execute(f"SELECT 1 FROM {MAIN_TABLE_NAME} LIMIT 0")
        finally:
            conn.close()
    except sqlite3.Error:
        return 'db_unreadable'
    return {'too_old': 'db_schema_too_old',
            'too_new': 'db_schema_too_new'}.get(db_contract.schema_status(version))


def db_problem():
    """Why the database can't be used right now (a STRINGS key), or None.

    Re-checked only when the file changes, which also clears the caches
    built from the previous one.
    """
    sig = _db_signature()
    with _db_state_lock:
        if _db_state["checked"] and _db_state["sig"] == sig:
            return _db_state["problem"]
        for cached in (get_column_names, get_numeric_columns,
                       fts_table_available, get_fts_columns,
                       get_indexed_columns):
            cached.cache_clear()
        problem = _check_db()
        _db_state.update(checked=True, sig=sig, problem=problem)
        return problem


_DB_PROBLEM_PAGE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BOLD Database v{version}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f5f7fa;color:#1b2235;margin:0;padding:64px 16px}}
main{{max-width:560px;margin:0 auto;background:#fff;border:1px solid #dde2ec;border-radius:10px;padding:28px 32px}}
h1{{font-size:1.25rem;margin:0 0 12px;color:#2c4685}}
p{{line-height:1.5;margin:0}}
</style></head>
<body><main><h1>{title}</h1><p>{message}</p></main></body></html>"""


@app.before_request
def _guard_database():
    if request.path != '/' and not request.path.startswith(_DB_ROUTES):
        return None
    problem = db_problem()
    if problem is None:
        return None
    if request.path == '/':
        return _DB_PROBLEM_PAGE.format(
            lang=get_lang(), version=__version__,
            title=t('db_problem_title'), message=t(problem)), 503
    return jsonify({'error': t(problem)}), 503


@app.route('/')
def index():
    technical_columns, _ = get_column_names()
    indexed_columns = get_indexed_columns()

    # Create [technical_name, friendly_name] pairs to send to the template
    columns = []

    # First add the columns that are in FIELD_MAPPING, in their defined order
    # (FIELD_MAPPING sets the order; the friendly name is resolved separately
    # so it comes out in the active language).
    for tech_name in FIELD_MAPPING:
        if tech_name in technical_columns and tech_name not in EXCLUDED_COLUMNS:
            columns.append({
                'technical': tech_name,
                'friendly': get_friendly_name(tech_name),
                # Sorting an unindexed column (col_nuc, coord) means SQLite
                # sorts every matching row instead of walking a B-tree —
                # minutes on a ~20M-row database (see get_indexed_columns()).
                # Its header stays, but as disabled with a tooltip.
                'indexed': tech_name in indexed_columns
            })

    # Then add any additional column that isn't in FIELD_MAPPING
    for col in technical_columns:
        if col not in FIELD_MAPPING and col not in EXCLUDED_COLUMNS:
            columns.append({
                'technical': col,
                'friendly': col,  # If not in the mapping, use the technical name
                'indexed': col in indexed_columns
            })

    # Fields offered as summary metrics in the batch search
    metric_columns = [
        {'technical': c, 'friendly': get_metric_label(c)}
        for c in METRIC_COLUMNS if c in technical_columns
    ]

    # Batch search field selectors: same fields as above, but with the
    # common ones up front. `columns` keeps the FIELD_MAPPING order (the
    # results-table order) because the field-based advanced search selectors
    # depend on it.
    by_tech       = {c['technical']: c for c in columns}
    batch_columns = [by_tech[c] for c in BATCH_FIELD_PRIORITY if c in by_tech]
    batch_columns += [c for c in columns if c['technical'] not in BATCH_FIELD_PRIORITY]

    return render_template(
        'index.html',
        columns=columns,
        batch_columns=batch_columns,
        metric_columns=metric_columns,
        genbank_fields=genbank_batch.FIELD_OPTIONS,
        genbank_report_columns=genbank_batch.REPORT_COLUMNS,
        fts_available=fts_table_available(),
        fts_has_processid=fts_has_processid() if fts_table_available() else True,
        fts_table_name=FTS_TABLE_NAME,
        initial_lang=get_lang(),
        version=__version__,
        analysis_limit=seq_analysis.MAX_SEQUENCES,
        analysis_fasta_max_mb=seq_analysis.MAX_FASTA_CHARS // 1_000_000,
        analysis_marker_kinds=seq_analysis.MARKER_KINDS,
        analysis_distance_limit=seq_distance.MAX_DISTANCE_SEQUENCES,
        analysis_tree_limit=seq_distance.MAX_TREE_SEQUENCES,
        analysis_delimit_limit=seq_distance.MAX_DELIMIT_SEQUENCES,
        analysis_network_limits={'seqs': seq_network.MAX_NETWORK_SEQUENCES,
                                 'tcs': seq_network.MAX_TCS_HAPLOTYPES,
                                 'msn': seq_network.MAX_MSN_HAPLOTYPES},
        analysis_per_species=ANALYSIS_PER_SPECIES,
        identify_enabled=IDENTIFY_ENABLED,
    )



@app.route('/guide/<path:filename>')
def guide(filename):
    """The user guide, opened at a section by the panels' help icons."""
    return send_from_directory(layout.GUIDE_DIR, filename)


@app.route('/api/search', methods=['POST'])
def search():
    start_time = time.time()
    data = request.get_json()
    search_id = data.get('search_id')

    if data.get('search_type') == 'fts':
        return search_fts(data, start_time, search_id)

    conditions = data.get('conditions', [])
    page = data.get('page', 1)
    per_page = data.get('per_page', 100)
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 100
    if per_page < 1:
        per_page = 100

    if not conditions:
        return jsonify({'error': t('no_search_conditions')}), 400

    conn = get_db_connection()
    if search_id:
        with active_searches_lock:
            active_searches[search_id] = conn

    cursor = conn.cursor()
    technical_columns, table_name = get_column_names()
    skip_count = data.get('skip_count', False)

    try:
        where_clause, parameters = build_where_clause(conditions, technical_columns)
        sort_col, sort_dir = resolve_sort(data, technical_columns)
        order_sql = f' ORDER BY "{sort_col}" {sort_dir}' if sort_col else ''

        offset = (page - 1) * per_page
        data_query = f"SELECT * FROM {table_name} WHERE {where_clause}{order_sql} LIMIT ? OFFSET ?"
        data_parameters = parameters + [per_page, offset]

        total_count = None
        if not skip_count:
            cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {where_clause}", parameters)
            total_count = cursor.fetchone()[0]

        cursor.execute(data_query, data_parameters)
        results_raw = cursor.fetchall()

        ordered_columns = [get_friendly_name(col) for col in technical_columns]
        results = [
            {friendly_col: row[get_technical_name(friendly_col)]
             for friendly_col in ordered_columns}
            for row in results_raw
        ]

        execution_time = time.time() - start_time
        return jsonify({
            'results': results,
            'count': total_count,
            'page': page,
            'total_pages': (total_count + per_page - 1) // per_page if total_count is not None else None,
            'execution_time': round(execution_time, 3)
        })

    except (ValueError, sqlite3.Error) as e:
        if 'interrupted' in str(e).lower():
            return jsonify({'cancelled': True}), 200
        return jsonify({'error': str(e)}), 500
    finally:
        if search_id:
            with active_searches_lock:
                active_searches.pop(search_id, None)
        conn.close()



_FTS_BOOLEAN_KEYWORDS = {'AND', 'OR', 'NOT'}

# A quoted phrase (an unterminated one runs to the end), a parenthesis, or a
# bare word: anything up to whitespace, a parenthesis or a quote. Commas and
# semicolons outside quotes separate words ("Panthera, onca"), like spaces.
_FTS_TOKEN_RE = re.compile(r'"([^"]*)"?|([()])|([^\s()",;]+)')


def _fts_tokens(query: str):
    """Splits the user's query into ('phrase' | 'op' | 'paren', text) tokens,
    keeping AND/OR/NOT and parentheses only where FTS5 can parse them.

    Every word ends up quoted as an FTS5 string, so no character the user
    types can be read as syntax: a bare `Cote d'Ivoire`, `Peru/Bolivia` or
    `Panthera, onca` used to raise "fts5: syntax error". An operator with no
    operand on one side ("AND Panthera", "Panthera OR") and unbalanced or
    empty parentheses are demoted to literal text / dropped for the same
    reason.
    """
    raw = []
    for m in _FTS_TOKEN_RE.finditer(query):
        phrase, paren, word = m.groups()
        if paren:
            raw.append(('paren', paren))
        elif word is not None:
            raw.append(('op', word) if word in _FTS_BOOLEAN_KEYWORDS else ('phrase', word))
        elif phrase and phrase.strip():
            raw.append(('phrase', phrase))

    # Parentheses are kept only if they're balanced and none of the groups is empty
    depth, prev = 0, None
    parens_ok = True
    for kind, text in raw:
        if kind == 'paren':
            if text == '(':
                depth += 1
            else:
                depth -= 1
                if depth < 0 or prev == ('paren', '('):
                    parens_ok = False
                    break
        prev = (kind, text)
    if depth != 0:
        parens_ok = False
    if not parens_ok:
        raw = [tok for tok in raw if tok[0] != 'paren']

    # "a AND NOT b" means "a NOT b": FTS5's NOT is already binary, and
    # keeping both would turn NOT into a literal word below.
    raw = [tok for i, tok in enumerate(raw)
           if not (tok == ('op', 'AND') and i + 1 < len(raw) and raw[i + 1] == ('op', 'NOT'))]

    # An operator needs an operand on each side; otherwise it's just a word
    out = []
    for i, (kind, text) in enumerate(raw):
        if kind == 'op':
            before = out[-1] if out else None
            after  = raw[i + 1] if i + 1 < len(raw) else None
            left_ok  = before is not None and (before[0] == 'phrase' or before == ('paren', ')'))
            right_ok = after is not None and (after[0] in ('phrase', 'op') or after == ('paren', '('))
            if not (left_ok and right_ok):
                kind = 'phrase'
        # FTS5's implicit AND only works between two phrases: next to a
        # parenthesis ("Panthera (onca)", "(a) (b)") it must be written out.
        if out and (out[-1][0] == 'phrase' or out[-1] == ('paren', ')')) \
               and (kind == 'phrase' and out[-1] == ('paren', ')')
                    or (kind, text) == ('paren', '(')):
            out.append(('op', 'AND'))
        out.append((kind, text))
    return out


def escape_fts_query(query: str) -> str:
    """Translates the user's query into FTS5 syntax.

    Wrapped in quotes ("Ara macao") it's treated as an exact phrase: with the
    trigram tokenizer, that requires the substring to appear complete and
    contiguous within a single field. Without quotes, each word is searched
    separately with an implicit AND across any of the indexed fields — by
    design, to allow combining criteria like "Panthera Colombia" — but that
    allows false positives when each word matches in a different, unrelated
    field (e.g. "Ara macao" would match both the genus "Hylarana", which
    contains "ara" as a substring, and a region called "Macao").
    AND/OR/NOT and parentheses keep working as operators (see _fts_tokens).
    """
    parts = []
    for kind, text in _fts_tokens(query):
        if kind == 'phrase':
            parts.append('"{}"'.format(text.replace('"', '""')))
        else:
            parts.append(text)
    return ' '.join(parts)


def extract_fts_terms(query: str):
    """Pulls the literal search terms out of a raw FTS query string, dropping
    the AND/OR/NOT keywords and grouping parentheses.

    Used for case-sensitive matching: the trigram tokenizer's MATCH is
    always case-insensitive (see escape_fts_query), so "sensitive" search is
    done by re-checking these literal terms against the actual row data
    after MATCH has already narrowed things down. Shares the tokenizer with
    escape_fts_query, so both always agree on what is a term.
    """
    return [text for kind, text in _fts_tokens(query) if kind == 'phrase']


@lru_cache(maxsize=1)
def get_fts_columns():
    """Column names actually indexed by the FTS5 table (col_nuc excluded).

    Read from the table itself rather than hardcoded, so this can't drift
    from however bold_db_creator.py built it.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {FTS_TABLE_NAME} LIMIT 0")
        return [d[0] for d in cursor.description]
    finally:
        conn.close()


def build_case_sensitive_filter(raw_query: str, table_alias: str = ''):
    """Builds an extra SQL condition (and its parameters) that re-checks the
    literal terms of an FTS query case-sensitively against the real row data.

    This is a cheap post-filter on top of the (already narrow) MATCH result,
    using instr() — case-sensitive in SQLite, unlike LIKE/GLOB folding rules —
    instead of building a second, case-sensitive FTS5 index. A second trigram
    index would roughly double both the FTS build time and its disk
    footprint (the current one already adds ~12 GB / ~30% of the database).

    Returns ('', []) if the query has no literal terms to check.
    """
    terms = extract_fts_terms(raw_query)
    if not terms:
        return '', []

    prefix = f'{table_alias}.' if table_alias else ''
    columns = get_fts_columns()
    params = []
    term_clauses = []
    for term in terms:
        col_checks = ' OR '.join(f'instr({prefix}{col}, ?) > 0' for col in columns)
        term_clauses.append(f'({col_checks})')
        params.extend([term] * len(columns))

    return ' AND ' + ' AND '.join(term_clauses), params


# FTS indexes built before bold_db_creator added col_processid don't hold the
# BOLD ID: a full-text search for "GBVA049-11" found nothing although the
# record was there. Until the index is rebuilt, a query made of a single word
# that starts some processid is looked up there too, through its own index
# (the whole ID or its beginning; a fragment from inside it is not found).
_PID_TERM_RE = re.compile(r'^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9._-]{5,}$')
# Above every character a processid can hold: closes the prefix range
_PID_RANGE_END = '\U0010ffff'


def fts_has_processid():
    """Whether the FTS index covers col_processid (True if it can't be told)."""
    try:
        return 'col_processid' in get_fts_columns()
    except sqlite3.Error:
        return True


def _processid_prefix(raw_query):
    """The BOLD ID, or its beginning, a full-text query consists of when the
    FTS index can't find it by itself; None otherwise."""
    tokens = _fts_tokens(raw_query)
    if len(tokens) != 1 or tokens[0][0] != 'phrase':
        return None
    term = tokens[0][1].strip()
    if not _PID_TERM_RE.match(term) or fts_has_processid():
        return None
    conn = get_db_connection()
    try:
        hit = conn.execute(
            f"SELECT 1 FROM {MAIN_TABLE_NAME} WHERE col_processid >= ? AND col_processid < ? LIMIT 1",
            (term, term + _PID_RANGE_END)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return term if hit else None


def fts_source(raw_query, alias, case_sensitive=False):
    """FROM and WHERE, with its parameters, of a full-text search over
    MAIN_TABLE_NAME as `alias` (raw_query already accent-stripped).

    Normally a join with the FTS table. When the query is a BOLD ID the index
    lacks (see _processid_prefix), the FTS matches OR the records whose
    processid starts with it: SQLite answers each side with its own index.
    """
    query = escape_fts_query(raw_query)
    if not query:
        raise ValueError(t('no_search_term'))
    cs_clause, cs_params = ('', [])
    if case_sensitive:
        cs_clause, cs_params = build_case_sensitive_filter(raw_query, alias)
    pid = _processid_prefix(raw_query)
    if not pid:
        return (f'{MAIN_TABLE_NAME} {alias} INNER JOIN {FTS_TABLE_NAME} fts ON {alias}.rowid = fts.rowid',
                f'fts.{FTS_TABLE_NAME} MATCH ?{cs_clause}', [query, *cs_params])
    # The processid column folds case (NOCASE): instr() keeps the exact one
    pid_cs, pid_cs_params = (f' AND instr({alias}.col_processid, ?) = 1', [pid]) if case_sensitive else ('', [])
    where = (f'({alias}.rowid IN (SELECT rowid FROM {FTS_TABLE_NAME} WHERE {FTS_TABLE_NAME} MATCH ?){cs_clause}'
             f' OR ({alias}.col_processid >= ? AND {alias}.col_processid < ?{pid_cs}))')
    return (f'{MAIN_TABLE_NAME} {alias}', where,
            [query, *cs_params, pid, pid + _PID_RANGE_END, *pid_cs_params])


def search_fts(data, start_time, search_id=None):
    """Optimized FTS implementation"""
    # Accent-stripped like every text field was on import (see
    # _strip_accents / bold_db_creator.py's normalize_field): the trigram
    # tokenizer doesn't fold diacritics, so 'México' would otherwise match
    # nothing against the accent-free 'Mexico' actually stored.
    raw_query = _strip_accents(data.get('query', '').strip())
    query = raw_query
    page = data.get('page', 1)
    per_page = data.get('per_page', 100)
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 100
    if per_page < 1:
        per_page = 100

    if not query:
        return jsonify({'error': t('no_search_query')}), 400

    # Checked before opening the connection: this way the error path never
    # leaves a connection unclosed, and the UI already has the tab disabled,
    # so reaching this point only happens if someone calls the API directly.
    if not fts_table_available():
        return jsonify({'error': t('fts_unavailable')}), 400

    query = escape_fts_query(query)
    # Nothing searchable left (e.g. the query was just "()")
    if not query:
        return jsonify({'error': t('no_search_query')}), 400

    from_sql, where_sql, params = fts_source(raw_query, 'm', bool(data.get('case_sensitive')))
    technical_columns, _ = get_column_names()

    conn = get_db_connection()
    if search_id:
        with active_searches_lock:
            active_searches[search_id] = conn

    cursor = conn.cursor()

    skip_count = data.get('skip_count', False)

    try:
        sort_col, sort_dir = resolve_sort(data, technical_columns)
        order_sql = f' ORDER BY m."{sort_col}" {sort_dir}' if sort_col else ''

        total_count = None
        if not skip_count:
            count_query = f"SELECT COUNT(*) FROM {from_sql} WHERE {where_sql}"
            cursor.execute(count_query, params)
            total_count = cursor.fetchone()[0]

        offset = (page - 1) * per_page
        data_query = f"SELECT m.* FROM {from_sql} WHERE {where_sql}{order_sql} LIMIT ? OFFSET ?"
        cursor.execute(data_query, (*params, per_page, offset))
        results_raw = cursor.fetchall()

        results = [
            {get_friendly_name(tech_col): value for tech_col, value in dict(row).items()}
            for row in results_raw
        ]

        execution_time = time.time() - start_time
        return jsonify({
            'results': results,
            'count': total_count,
            'page': page,
            'total_pages': (total_count + per_page - 1) // per_page if total_count is not None else None,
            'execution_time': round(execution_time, 3)
        })

    except (ValueError, sqlite3.Error) as e:
        if 'interrupted' in str(e).lower():
            return jsonify({'cancelled': True}), 200
        return jsonify({'error': str(e)}), 500
    finally:
        if search_id:
            with active_searches_lock:
                active_searches.pop(search_id, None)
        conn.close()
        



AUTOCOMPLETE_MIN_CHARS = 3

AUTOCOMPLETE_COLUMNS = [
    'col_marker_code', 'col_species', 'col_genus', 'col_family',
    'col_order', 'col_class', 'col_phylum', 'col_country_ocean',
    'col_province_state', 'col_region', 'col_identification'
]

# Searching column by column finds the rarer values a common one hides (a
# genus "Mexi..." behind thousands of "Mexico"), but a column where the term
# is rare costs a walk over every row that has it elsewhere: "Costa Ri" in
# col_genus reads all the Costa Rica records. So each column gets a time
# slice, and whatever didn't fit is left out of the suggestions.
AUTOCOMPLETE_COLUMN_BUDGET_S = 0.05
AUTOCOMPLETE_TOTAL_BUDGET_S = 0.4
AUTOCOMPLETE_ENOUGH_VALUES = 40
# Distinct values read per column (and per capitalization) from its index
AUTOCOMPLETE_PREFIX_PER_COLUMN = 8
AUTOCOMPLETE_LIMIT = 10
# Past this many records a value counts as "very common": enough to rank it
AUTOCOMPLETE_FREQUENCY_CAP = 1000


@lru_cache(maxsize=1)
def get_indexed_columns():
    """Columns of the main table that lead a B-tree index (Step 3)."""
    conn = get_db_connection()
    try:
        cols = set()
        for idx in conn.execute(f"PRAGMA index_list({MAIN_TABLE_NAME})").fetchall():
            info = conn.execute(f'PRAGMA index_info("{idx[1]}")').fetchall()
            if info:
                cols.add(info[0][2])
        return frozenset(cols)
    finally:
        conn.close()


def _autocomplete_prefix(conn, columns, query):
    """Rows (aligned with `columns`) with the values that start with `query`.

    Read from each column's index by seeking value to value, so it costs a
    few lookups however many records share a value: "Colom" finds Colombia
    right away, where a substring search sees Scolomys and Discolomatidae
    first. The index compares case-sensitively, hence the capitalizations.
    """
    indexed = get_indexed_columns()
    variants = dict.fromkeys([query, query[:1].upper() + query[1:],
                              query.lower(), query.upper()])
    rows = []
    for i, col in enumerate(columns):
        if col not in indexed:
            continue  # without its index, a range here would scan the table
        first = (f"SELECT {col} FROM {MAIN_TABLE_NAME} WHERE {col} >= ? AND {col} < ? "
                 f"ORDER BY {col} LIMIT 1")
        after = (f"SELECT {col} FROM {MAIN_TABLE_NAME} WHERE {col} > ? AND {col} < ? "
                 f"ORDER BY {col} LIMIT 1")
        for low in variants:
            high = low[:-1] + chr(ord(low[-1]) + 1)
            hit = conn.execute(first, (low, high)).fetchone()
            for _ in range(AUTOCOMPLETE_PREFIX_PER_COLUMN):
                if hit is None:
                    break
                row = [None] * len(columns)
                row[i] = hit[0]
                rows.append(tuple(row))
                hit = conn.execute(after, (hit[0], high)).fetchone()
    return rows


def _value_frequency(conn, col, value):
    """Records with this value, counted up to AUTOCOMPLETE_FREQUENCY_CAP.

    Counted on the column's index and capped, so a value in a million
    records costs no more than one in a thousand. 0 without an index.
    """
    if col not in get_indexed_columns():
        return 0
    return conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM {MAIN_TABLE_NAME} WHERE {col} = ? LIMIT ?)",
        (value, AUTOCOMPLETE_FREQUENCY_CAP)).fetchone()[0]


def _suggestion_rank(value, query, frequency=0):
    """Sort key: values that start with the query, then those with a word
    that does ("Lake Ontario" for "ontario"), then the rest. Within each,
    the most common first (Colombia before Colombo), then the shortest."""
    text = str(value).lower()
    q = query.lower()
    if text.startswith(q):
        tier = 0
    elif re.search(r'[\s,;/()\-_.]' + re.escape(q), text):
        tier = 1
    else:
        tier = 2
    return (tier, -frequency, len(text), text)


def _autocomplete_fts(conn, columns, query):
    """Rows (tuples aligned with `columns`) whose values contain `query`.

    It used to be one `col LIKE '%q%' OR ...` over all the columns: SQLite
    runs that as a full trigram scan per column before LIMIT applies, which
    took seconds for common terms ("Costa Ri" ~10 s). A MATCH stops at LIMIT.
    """
    phrase = '"' + query.replace('"', '""') + '"'
    # One pass over all the columns at once: milliseconds, and it brings the
    # most common values
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM {FTS_TABLE_NAME} "
        f"WHERE {FTS_TABLE_NAME} MATCH ? LIMIT 50",
        ['{' + ' '.join(columns) + '} : ' + phrase]).fetchall()

    found = {v for row in rows for v in row if v}
    deadline = time.monotonic() + AUTOCOMPLETE_TOTAL_BUDGET_S
    try:
        for i, col in enumerate(columns):
            now = time.monotonic()
            if now >= deadline or len(found) >= AUTOCOMPLETE_ENOUGH_VALUES:
                break
            stop = min(deadline, now + AUTOCOMPLETE_COLUMN_BUDGET_S)
            conn.set_progress_handler(lambda: time.monotonic() > stop, 1000)
            try:
                values = conn.execute(
                    f"SELECT {col} FROM {FTS_TABLE_NAME} "
                    f"WHERE {FTS_TABLE_NAME} MATCH ? LIMIT 20",
                    [f'{col} : {phrase}']).fetchall()
            except sqlite3.OperationalError:
                continue  # out of time for this column
            for (v,) in values:
                if v and v not in found:
                    found.add(v)
                    row = [None] * len(columns)
                    row[i] = v
                    rows.append(tuple(row))
    finally:
        conn.set_progress_handler(None, 0)
    return rows


@app.route('/api/autocomplete')
def autocomplete():
    query = request.args.get("query", "").strip()
    # Under 3 characters the trigram index can't help (it has no n-gram to
    # look up) and LIKE '%xy%' scans all ~20M rows on every keystroke.
    if len(query) < AUTOCOMPLETE_MIN_CHARS:
        return jsonify({"results": []})

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (FTS_TABLE_NAME,))
    fts_exists = cursor.fetchone()

    try:
        if fts_exists:
            cursor.execute(f"PRAGMA table_info({FTS_TABLE_NAME})")
            columns = [row[1] for row in cursor.fetchall() if row[1] in AUTOCOMPLETE_COLUMNS]
            if not columns:
                return jsonify({"results": []})

            results = (_autocomplete_prefix(conn, columns, query)
                       + _autocomplete_fts(conn, columns, query))

        else:
            technical_columns, main_table = get_column_names()
            columns = [c for c in AUTOCOMPLETE_COLUMNS if c in technical_columns]
            if not columns:
                return jsonify({"results": []})
            sql_query = f"""
                SELECT {', '.join(columns)}
                FROM {main_table}
                WHERE {' OR '.join([f'{col} LIKE ?' for col in columns])}
                LIMIT 50
            """
            params = [f"%{query}%"] * len(columns)
            cursor.execute(sql_query, params)
            results = _autocomplete_prefix(conn, columns, query) + cursor.fetchall()

        candidates = {}  # value -> column it was found in (the first one)
        for row in results:
            for col, value in zip(columns, row):
                if value and value not in candidates and query.lower() in str(value).lower():
                    candidates[value] = col

        ranked = sorted(candidates.items(), key=lambda kv: _suggestion_rank(
            kv[0], query, _value_frequency(conn, kv[1], kv[0])))
        return jsonify({"results": [{"field": get_friendly_name(col), "value": value}
                                    for value, col in ranked[:AUTOCOMPLETE_LIMIT]]})

    except sqlite3.Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/export_fasta', methods=['POST'])
def start_fasta_export():
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': t('no_data_provided')}), 400

        # Unique ID for the export
        export_id = str(uuid.uuid4())

        # Initialize state
        fasta_exports[export_id] = {
            'status': 'processing',
            'rows_processed': 0,
            'rows_read': 0,
            'total_rows': _expected_total(data),
            'message': t('fasta_preparing'),
            'filename': None,
            'start_time': datetime.now(),
            'last_update': datetime.now()
        }
        _prune_export_status(fasta_exports)

        # Start export thread
        start_background(process_fasta_export, export_id, data)

        return jsonify({
            'export_id': export_id,
            'message': t('fasta_export_started')
        })

    except Exception as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500


@app.route('/api/fasta_export_status/<export_id>', methods=['GET'])
def get_fasta_export_status(export_id):
    if export_id not in fasta_exports:
        return jsonify({'error': t('invalid_export_id')}), 404

    status = fasta_exports[export_id]

    return jsonify({
        'status': status['status'],
        'rows_processed': status['rows_processed'],
        'total_rows': status['total_rows'],
        'progress': _export_progress(status),
        'message': status['message'],
        'download_url': f'/api/download_fasta/{export_id}'
                        if status['filename'] and status['status'] == 'completed' else None
    })


@app.route('/api/cancel_fasta_export/<export_id>', methods=['POST'])
def cancel_fasta_export(export_id):
    """Marks a running FASTA export as cancelled; process_fasta_export checks
    this status between batches and stops (see line ~1237)."""
    if export_id not in fasta_exports:
        return jsonify({'ok': False, 'error': t('invalid_export_id')}), 404
    fasta_exports[export_id]['status'] = 'cancelled'
    return jsonify({'ok': True})


@app.route('/api/download_fasta/<export_id>', methods=['GET'])
def download_fasta(export_id):
    if export_id not in fasta_exports or not fasta_exports[export_id]['filename']:
        return jsonify({'error': t('fasta_not_available')}), 404

    if fasta_exports[export_id]['status'] != 'completed':
        return jsonify({'error': t('export_not_completed')}), 400
    
    filepath = os.path.join(FASTA_EXPORTS_DIR, fasta_exports[export_id]['filename'])
    
    if not os.path.exists(filepath):
        return jsonify({'error': t('file_not_found')}), 404
    
    return send_file(filepath, as_attachment=True)


@app.route('/api/cancel_search/<search_id>', methods=['POST'])
def cancel_search(search_id):
    with active_searches_lock:
        conn = active_searches.get(search_id)
    if conn:
        try:
            conn.interrupt()
        except Exception:
            pass
        return jsonify({'ok': True})
    return jsonify({'ok': False})


def get_best_taxon(species, genus, family, order, class_, phylum, kingdom):
    """Return the most specific taxon available, falling back up the hierarchy.

    Order of preference: Species > Genus > Family > Order > Class > Phylum > Kingdom.
    Spaces are replaced with underscores so the value fits in a FASTA header.
    Returns an empty string if no taxonomic value is available.
    """
    for value in (species, genus, family, order, class_, phylum, kingdom):
        if value and str(value).strip():
            return str(value).strip().replace(' ', '_')
    return ''


def build_optimized_fasta_query(request_data):
    """Build an optimized query for FASTA export"""
    technical_columns, _ = get_column_names()
    sort_col, sort_dir = resolve_sort(request_data, technical_columns)

    if request_data.get('search_type') == 'fts':
        raw_query = request_data.get('query', '').strip()
        if not raw_query:
            raise ValueError(t('no_search_term'))
        if not fts_table_available():
            raise ValueError(t('fts_unavailable_short'))

        # Accent-stripped like search_fts() does — see the comment there.
        raw_query = _strip_accents(raw_query)

        from_sql, where_sql, params = fts_source(raw_query, 'r', bool(request_data.get('case_sensitive')))
        order_sql = f' ORDER BY r."{sort_col}" {sort_dir}' if sort_col else ''

        data_query = f"""
            SELECT r.col_processid, r.col_sampleid, r.col_bin_uri,
                   r.col_species, r.col_genus, r.col_family, r.col_order,
                   r.col_class, r.col_phylum, r.col_kingdom,
                   r.col_marker_code, r.col_nuc
            FROM {from_sql}
            WHERE {where_sql}
            AND r.col_nuc IS NOT NULL AND r.col_nuc != ''
            {order_sql}
        """

        return data_query, tuple(params)

    else:
        conditions = request_data.get('conditions', [])
        if not conditions:
            raise ValueError(t('no_search_conditions'))

        where_clause, params = build_where_clause(conditions, technical_columns)

        # In parentheses: the conditions may be joined by OR at the top level,
        # and without them the sequence filter only applied to the last one
        if where_clause:
            where_clause = f"({where_clause}) AND (col_nuc IS NOT NULL AND col_nuc != '')"
        else:
            where_clause = "(col_nuc IS NOT NULL AND col_nuc != '')"

        order_sql = f' ORDER BY "{sort_col}" {sort_dir}' if sort_col else ''
        data_query = f"""
            SELECT col_processid, col_sampleid, col_bin_uri,
                   col_species, col_genus, col_family, col_order,
                   col_class, col_phylum, col_kingdom,
                   col_marker_code, col_nuc
            FROM bold_records
            WHERE {where_clause}
            {order_sql}
        """

        return data_query, params
        
        

@contextmanager
def get_export_connection():
    """Context manager for export connections optimized for bulk reads"""
    conn = _connect_readonly(300.0)

    cursor = conn.cursor()
    cursor.execute('PRAGMA cache_size = -512000')  # 512MB
    cursor.execute('PRAGMA temp_store = MEMORY')
    cursor.execute('PRAGMA mmap_size = 2147483648')  # 2GB

    try:
        yield conn
    finally:
        conn.close()

def process_fasta_export(export_id, request_data):
    """Optimized FASTA export: a single query, fetchmany in batches, no upfront COUNT."""
    filepath = None

    try:
        fasta_exports[export_id].update({
            'status': 'processing',
            'message': t('exporting_sequences'),
            'start_time': datetime.now()
        })

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'bold_sequences_{timestamp}.fasta'
        filepath = os.path.join(FASTA_EXPORTS_DIR, filename)
        # 'filename' is only set once the file is complete (see the end):
        # set here, an export with no sequences finished offering a
        # download of a file that was never written.

        data_query, params = build_optimized_fasta_query(request_data)

        with get_export_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(data_query, params)

            rows_processed = 0
            rows_read = 0
            batch_count = 0
            last_update = 0

            batch = cursor.fetchmany(FASTA_BATCH_SIZE)
            if not batch:
                fasta_exports[export_id].update({
                    'status': 'completed',
                    'message': t('no_sequences_found')
                })
                return

            with open(filepath, 'w', encoding='utf-8', buffering=BUFFER_SIZE) as fasta_file:
                while batch:
                    if fasta_exports[export_id].get('status') == 'cancelled':
                        break

                    batch_count += 1
                    rows_read += len(batch)
                    keys = batch[0].keys()
                    fasta_entries = [e for e in (_row_to_fasta_entry(row, keys) for row in batch) if e]

                    if fasta_entries:
                        fasta_file.write(''.join(fasta_entries))
                        rows_processed += len(fasta_entries)

                    if rows_read - last_update >= UPDATE_INTERVAL:
                        elapsed = (datetime.now() - fasta_exports[export_id]['start_time']).total_seconds()
                        rate = int(rows_processed / elapsed) if elapsed > 0 else 0
                        fasta_exports[export_id].update({
                            'rows_processed': rows_processed,
                            'rows_read': rows_read,
                            'message': t('fasta_progress', count=f'{rows_processed:,}', rate=f'{rate:,}'),
                            'last_update': datetime.now()
                        })
                        last_update = rows_read

                    batch = cursor.fetchmany(FASTA_BATCH_SIZE)

        if fasta_exports[export_id].get('status') == 'cancelled':
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
            return

        # Records matched, but none had a usable sequence (all under 50 bp):
        # same outcome as no records at all, rather than an empty download.
        if rows_processed == 0:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
            fasta_exports[export_id].update({
                'status': 'completed',
                'message': t('no_sequences_found'),
            })
            return

        duration = datetime.now() - fasta_exports[export_id]['start_time']
        duration_str = f"{int(duration.total_seconds() // 60)}m {int(duration.total_seconds() % 60)}s"
        fasta_exports[export_id].update({
            'status': 'completed',
            'filename': filename,
            'rows_processed': rows_processed,
            'message': t('fasta_completed', count=f'{rows_processed:,}', duration=duration_str),
            'last_update': datetime.now()
        })
        _prune_export_dir(FASTA_EXPORTS_DIR)
        print(f"FASTA Export OK — {export_id} | {rows_processed:,} seqs | {batch_count} batches | {duration_str}")

    except Exception as e:
        error_msg = t('fasta_export_error', error=str(e))
        fasta_exports[export_id].update({
            'status': 'error',
            'message': error_msg,
            'last_update': datetime.now()
        })
        print(f"FASTA Export ERROR — {export_id}: {error_msg}")
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
        import traceback
        traceback.print_exc()
            

@app.route('/api/export_csv', methods=['POST'])
def export_csv():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': t('no_data_provided')}), 400

        export_id = str(uuid.uuid4())
        export_filename = f"export_{export_id}.csv"
        export_path = os.path.join(CSV_EXPORTS_DIR, export_filename)

        include_nuc = data.get('include_nuc', True)
        export_columns = data.get('export_columns', None)

        csv_exports[export_id] = {
            'status': 'processing',
            'rows_processed': 0,
            'rows_read': 0,
            'total_rows': _expected_total(data),
            'message': t('csv_preparing'),
            'filename': None,
            'include_nuc': include_nuc,
            'start_time': datetime.now(),
            'last_update': datetime.now()
        }
        _prune_export_status(csv_exports)

        start_background(process_export, data, export_path, export_id, export_filename, include_nuc, export_columns)

        return jsonify({
            'export_id': export_id,
            'message': t('csv_export_started')
        })

    except Exception as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500

def process_export(data, export_path, export_id, export_filename, include_nuc=True, export_columns=None):
    try:
        data_query, parameters = prepare_query(data, include_nuc)

        with get_export_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(data_query, parameters)

            technical_headers = [desc[0] for desc in cursor.description]

            if export_columns is not None:
                # Filter and reorder to only the user-selected visible columns
                # get_technical_name falls back to the name itself: a column
                # with no friendly name (a new BOLD field) is shown under its
                # technical name, and REVERSE_MAPPING.get() dropped it here.
                tech_order = [get_technical_name(fn) for fn in export_columns]
                col_indices = [technical_headers.index(tc) for tc in tech_order if tc and tc in technical_headers]
            else:
                col_indices = list(range(len(technical_headers)))

            friendly_headers = [get_friendly_name(technical_headers[i]) for i in col_indices]

            batch = cursor.fetchmany(BATCH_SIZE)
            if not batch:
                csv_exports[export_id].update({
                    'status': 'completed',
                    'message': t('no_results_found')
                })
                return

            total_rows = 0
            last_update_time = time.time()

            with open(export_path, 'w', newline='', encoding='utf-8-sig', buffering=BUFFER_SIZE) as csv_file:
                csv_writer = csv.writer(csv_file)
                csv_writer.writerow(friendly_headers)

                while batch:
                    if csv_exports[export_id].get('status') == 'cancelled':
                        break

                    for row in batch:
                        csv_writer.writerow('' if row[i] is None else str(row[i]) for i in col_indices)
                        total_rows += 1

                    current_time = time.time()
                    if total_rows % 10000 == 0 or (current_time - last_update_time) > 5:
                        elapsed = (datetime.now() - csv_exports[export_id]['start_time']).total_seconds()
                        rate = int(total_rows / elapsed) if elapsed > 0 else 0
                        csv_exports[export_id].update({
                            'rows_processed': total_rows,
                            'rows_read': total_rows,
                            'message': t('csv_progress', count=f'{total_rows:,}', rate=f'{rate:,}'),
                            'last_update': datetime.now()
                        })
                        last_update_time = current_time

                    batch = cursor.fetchmany(BATCH_SIZE)

        if csv_exports[export_id].get('status') == 'cancelled':
            if os.path.exists(export_path):
                os.remove(export_path)
            return

        duration = datetime.now() - csv_exports[export_id]['start_time']
        duration_str = f"{int(duration.total_seconds() // 60)}m {int(duration.total_seconds() % 60)}s"
        csv_exports[export_id].update({
            'status': 'completed',
            'rows_processed': total_rows,
            'filename': export_filename,
            'message': t('csv_completed', count=f'{total_rows:,}', duration=duration_str),
            'last_update': datetime.now()
        })
        _prune_export_dir(CSV_EXPORTS_DIR)
        print(f"CSV Export OK — {export_id} | {total_rows:,} rows | {duration_str}")

    except Exception as e:
        csv_exports[export_id].update({
            'status': 'error',
            'message': t('csv_export_error', error=str(e)),
            'last_update': datetime.now()
        })
        import traceback
        traceback.print_exc()


def prepare_query(data, include_nuc=True):
    technical_columns, _ = get_column_names()
    if include_nuc:
        col_select = '*'
        col_select_join = 'r.*'
    else:
        cols = [c for c in technical_columns if c != 'col_nuc']
        col_select = ', '.join(f'"{c}"' for c in cols)
        col_select_join = ', '.join(f'r."{c}"' for c in cols)

    sort_col, sort_dir = resolve_sort(data, technical_columns)

    if data.get('search_type') == 'fts':
        raw_query = data.get('query', '').strip()
        if not raw_query:
            raise ValueError(t('no_search_term'))
        if not fts_table_available():
            raise ValueError(t('fts_unavailable_short'))

        # Accent-stripped like search_fts() does — see the comment there.
        raw_query = _strip_accents(raw_query)
        from_sql, where_sql, params = fts_source(raw_query, 'r', bool(data.get('case_sensitive')))
        order_sql = f' ORDER BY r."{sort_col}" {sort_dir}' if sort_col else ''
        data_query = f"SELECT {col_select_join} FROM {from_sql} WHERE {where_sql}{order_sql}"
        parameters = tuple(params)

    else:
        conditions = data.get('conditions', [])
        if not conditions:
            raise ValueError(t('no_search_conditions'))

        where_clause, parameters = build_where_clause(conditions, technical_columns)
        order_sql = f' ORDER BY "{sort_col}" {sort_dir}' if sort_col else ''
        data_query = f"SELECT {col_select} FROM bold_records WHERE {where_clause}{order_sql}"

    return data_query, parameters


def report_source(data, technical_columns):
    """FROM/WHERE (always aliased "r", on both branches) for the on-demand
    report: the same filters as the search that produced the results on
    screen — field conditions or full-text — reusing build_where_clause()
    and fts_source() exactly as prepare_query() does, so a report always
    matches what's in the results table."""
    if data.get('search_type') == 'fts':
        raw_query = data.get('query', '').strip()
        if not raw_query:
            raise ValueError(t('no_search_term'))
        if not fts_table_available():
            raise ValueError(t('fts_unavailable_short'))
        raw_query = _strip_accents(raw_query)
        from_sql, where_sql, params = fts_source(raw_query, 'r', bool(data.get('case_sensitive')))
        return from_sql, where_sql, list(params)

    conditions = data.get('conditions', [])
    if not conditions:
        raise ValueError(t('no_search_conditions'))
    where_clause, params = build_where_clause(conditions, technical_columns)
    return f'{MAIN_TABLE_NAME} r', where_clause, params


def compute_report(data, full=False):
    """Runs the report panel's aggregation (GROUP BY row[, column]) over the
    current search's filters and returns a dict ready for jsonify. The xlsx
    export reuses this too, so the two always agree.

    `row_field`/`col_field` must be real column names from METRIC_COLUMNS
    (sent by the frontend as-is, from the same list the server renders into
    the panel's selects) — that whitelist is what makes it safe to
    interpolate them into the SQL below. Raises ValueError on bad input,
    which the routes turn into a 400.
    """
    technical_columns, _ = get_column_names()

    row_field = (data.get('row_field') or '').strip()
    col_field = (data.get('col_field') or '').strip() or None
    value = (data.get('value') or 'records').strip()

    if row_field not in METRIC_COLUMNS or row_field not in technical_columns:
        raise ValueError(t('report_row_field_required'))
    if col_field and (col_field not in METRIC_COLUMNS or col_field not in technical_columns
                       or col_field == row_field):
        col_field = None
    if value not in REPORT_VALUES:
        raise ValueError(t('report_invalid_value', value=value))

    metric_sql, metric_labels = REPORT_VALUES[value]
    metric_label = metric_labels.get(get_lang(), metric_labels['en'])
    other_label = t('report_other_label')

    from_sql, where_sql, params = report_source(data, technical_columns)

    with get_export_connection() as conn:
        cursor = conn.cursor()

        col_values, col_truncated, total_cols = [], False, None
        if col_field and not full:
            # Discover the column axis's domain first: which categories
            # exist (within the current filter) and whether there are more
            # than REPORT_COL_CAP of them, so the main query below can fold
            # the rest into "Other" instead of returning one real SQL column
            # per distinct value.
            cursor.execute(
                f'SELECT r.{col_field} AS c, COUNT(*) AS n FROM {from_sql} '
                f'WHERE {where_sql} GROUP BY r.{col_field} ORDER BY n DESC LIMIT ?',
                [*params, REPORT_COL_CAP + 1]
            )
            domain = cursor.fetchall()
            col_truncated = len(domain) > REPORT_COL_CAP
            col_values = [row['c'] for row in domain[:REPORT_COL_CAP]]
            if col_truncated:
                cursor.execute(f'SELECT COUNT(*) AS n FROM (SELECT 1 FROM {from_sql} WHERE {where_sql} GROUP BY r.{col_field})', params)
                total_cols = cursor.fetchone()['n']

        if col_field:
            if col_truncated:
                placeholders = ', '.join('?' for _ in col_values)
                col_expr = f'CASE WHEN r.{col_field} IN ({placeholders}) THEN r.{col_field} ELSE ? END'
                col_params = [*col_values, other_label]
            else:
                col_expr = f'r.{col_field}'
                col_params = []
            # col_expr's placeholders sit in the SELECT clause, which comes
            # before WHERE in the SQL text — they must be bound first.
            cursor.execute(
                f'SELECT r.{row_field} AS rr, {col_expr} AS cc, {metric_sql} AS v '
                f'FROM {from_sql} WHERE {where_sql} GROUP BY rr, cc',
                [*col_params, *params]
            )
        else:
            cursor.execute(
                f'SELECT r.{row_field} AS rr, {metric_sql} AS v '
                f'FROM {from_sql} WHERE {where_sql} GROUP BY rr',
                params
            )
        cells = cursor.fetchall()

    # The matrix is assembled here, not in SQL: a row's total (which decides
    # its place and whether it survives REPORT_ROW_CAP) isn't known until
    # every one of its cells has been seen, so there's no single GROUP BY
    # that could apply the cap on its own.
    row_totals = defaultdict(int)
    col_totals = defaultdict(int)
    matrix = defaultdict(lambda: defaultdict(int))
    grand_total = 0
    for cell in cells:
        rr, v = cell['rr'], cell['v'] or 0
        if col_field:
            cc = cell['cc']
            matrix[rr][cc] += v
            col_totals[cc] += v
        row_totals[rr] += v
        grand_total += v

    ordered_rows = sorted(row_totals, key=lambda k: row_totals[k], reverse=True)
    # full=True (the Excel export) keeps every row and column; the caps
    # only keep the on-screen table readable.
    row_truncated = not full and len(ordered_rows) > REPORT_ROW_CAP
    if not full:
        ordered_rows = ordered_rows[:REPORT_ROW_CAP]

    ordered_cols = []
    if col_field:
        ordered_cols = sorted(
            (c for c in col_totals if c != other_label),
            key=lambda k: col_totals[k], reverse=True
        )
        if col_truncated:
            ordered_cols.append(other_label)

    def label(v):
        return v if v not in (None, '') else t('report_empty_value')

    _limit = report_excel_limit(len(row_totals), total_cols if total_cols is not None else len(col_totals))

    return {
        'row_field': get_metric_label(row_field),
        'col_field': get_metric_label(col_field) if col_field else None,
        'value_label': metric_label,
        'rows': [label(r) for r in ordered_rows],
        'cols': [label(c) for c in ordered_cols],
        'matrix': [[matrix[r].get(c, 0) for c in ordered_cols] for r in ordered_rows] if col_field else [],
        'row_totals': [row_totals[r] for r in ordered_rows],
        'col_totals': [col_totals[c] for c in ordered_cols] if col_field else [],
        'grand_total': grand_total,
        'row_truncated': row_truncated,
        'col_truncated': col_truncated,
        'total_distinct_rows': len(row_totals),
        'total_distinct_cols': total_cols if total_cols is not None else len(col_totals),
        'excel_limit': _limit,
        'excel_fits': _limit is None,
    }


@app.route('/api/report', methods=['POST'])
def api_report():
    """On-demand summary table (the "Crear informe" panel next to the
    results table): GROUP BY over the same filters as the search on screen,
    capped so it stays a summary rather than a second copy of the data."""
    data = request.get_json() or {}
    try:
        report = compute_report(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500
    return jsonify(report)


def _prepare_export_report(data):
    """The full (uncapped) report, turned on its side when much wider than tall."""
    report = compute_report(data, full=True)
    if report['col_field'] and len(report['cols']) > 2 * len(report['rows']):
        report = {
            **report,
            'row_field': report['col_field'], 'col_field': report['row_field'],
            'rows': report['cols'], 'cols': report['rows'],
            'matrix': [list(r) for r in zip(*report['matrix'])],
            'row_totals': report['col_totals'], 'col_totals': report['row_totals'],
        }
    return report


def _report_table_rows(report):
    """The report as a list of rows: header, body, totals."""
    total_label = t('report_total_label')
    if report['col_field']:
        rows = [[f"{report['row_field']} \\ {report['col_field']}", *report['cols'], total_label]]
        for i, row_label in enumerate(report['rows']):
            rows.append([row_label, *report['matrix'][i], report['row_totals'][i]])
        rows.append([total_label, *report['col_totals'], report['grand_total']])
    else:
        rows = [[report['row_field'], report['value_label']]]
        rows.extend([row_label, n] for row_label, n in zip(report['rows'], report['row_totals']))
        rows.append([total_label, report['grand_total']])
    return rows


def _report_csv_bytes(report):
    buf = io.StringIO()
    csv.writer(buf).writerows(_report_table_rows(report))
    return ('\ufeff' + buf.getvalue()).encode('utf-8')


def _fill_report_sheet(ws, report):
    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='2E3846', end_color='2E3846', fill_type='solid')
    total_font = Font(bold=True)
    total_fill = PatternFill(start_color='E3E8EF', end_color='E3E8EF', fill_type='solid')
    for row in _report_table_rows(report):
        ws.append(row)

    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')
    for cell in ws[ws.max_row]:
        cell.font = total_font
        cell.fill = total_fill
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        row[0].font = total_font
        if report['col_field']:
            row[-1].font = total_font
            row[-1].fill = total_fill

    ws.freeze_panes = 'B2'
    # Widths from the first rows only: scanning every cell of a big sheet is slow
    for col_idx, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 200)), start=1):
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 3, 40)


def _sheet_title(report, used):
    """A unique sheet name: Excel allows 31 characters and none of []:*?/ and backslash."""
    name = f"{report['row_field']} x {report['col_field']}" if report['col_field'] else report['row_field']
    name = ''.join(' ' if c in '[]:*?/' + chr(92) else c for c in name).strip()[:31] or 'Informe'
    base, n = name, 2
    while name.lower() in used:
        suffix = f' ({n})'
        name = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(name.lower())
    return name


def _xlsx_response(wb, filename):
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )


@app.route('/api/report/export', methods=['POST'])
def export_report():
    """The report panel's own "Exportar a Excel" button: the same
    aggregation as /api/report but uncapped (the screen folds the tail into
    "Other"; the file keeps everything), written to an in-memory .xlsx, or
    to CSV when it is too big to be a manageable sheet."""
    data = request.get_json() or {}
    try:
        report = _prepare_export_report(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if report_excel_limit(len(report['rows']), len(report['cols'])):
        return Response(
            _report_csv_bytes(report),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename="informe_{stamp}.csv"'}
        )

    wb = Workbook()
    ws = wb.active
    ws.title = _sheet_title(report, set())
    _fill_report_sheet(ws, report)
    return _xlsx_response(wb, f'informe_{stamp}.xlsx')


@app.route('/api/report/export_book', methods=['POST'])
def export_report_book():
    """Several saved report tables in one download: a workbook with one
    sheet per table. A table too big to be a sheet goes alongside as CSV,
    which turns the download into a .zip."""
    tables = (request.get_json() or {}).get('tables') or []
    if not tables or len(tables) > MAX_BOOK_TABLES:
        return jsonify({'error': t('unexpected_error', error='tables')}), 400
    try:
        reports = [_prepare_export_report(tbl) for tbl in tables]
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    wb, used, big = Workbook(), set(), []
    wb.remove(wb.active)
    for i, report in enumerate(reports, start=1):
        if report_excel_limit(len(report['rows']), len(report['cols'])):
            big.append((i, report))
            continue
        _fill_report_sheet(wb.create_sheet(_sheet_title(report, used)), report)

    if not big:
        return _xlsx_response(wb, f'informe_{stamp}.xlsx')

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, 'w', zipfile.ZIP_DEFLATED) as zf:
        if wb.sheetnames:
            xbuf = io.BytesIO()
            wb.save(xbuf)
            zf.writestr(f'informe_{stamp}.xlsx', xbuf.getvalue())
        for i, report in big:
            slug = re.sub(r'[^A-Za-z0-9]+', '_', f"{report['row_field']}_{report['col_field'] or ''}").strip('_')
            zf.writestr(f'tabla_{i}_{slug}.csv', _report_csv_bytes(report))
    zbuf.seek(0)
    return send_file(zbuf, mimetype='application/zip', as_attachment=True,
                     download_name=f'informe_{stamp}.zip')


@app.route('/api/export_status/<export_id>', methods=['GET'])
def export_status(export_id):
    if export_id not in csv_exports:
        return jsonify({'error': t('invalid_export_id')}), 404

    status = csv_exports[export_id]

    return jsonify({
        'status': status['status'],
        'rows_processed': status['rows_processed'],
        'progress': _export_progress(status),
        'message': status['message'],
        'download_url': f'/api/download_export/{export_id}' if status.get('filename') else None
    })


@app.route('/api/cancel_export/<export_id>', methods=['POST'])
def cancel_csv_export(export_id):
    """Marks a running CSV export as cancelled; process_export checks this
    status between batches and stops."""
    if export_id not in csv_exports:
        return jsonify({'ok': False, 'error': t('invalid_export_id')}), 404
    csv_exports[export_id]['status'] = 'cancelled'
    return jsonify({'ok': True})


@app.route('/api/download_export/<export_id>', methods=['GET'])
def download_export(export_id):
    if export_id not in csv_exports or not csv_exports[export_id].get('filename'):
        return jsonify({'error': t('csv_not_available')}), 404

    if csv_exports[export_id]['status'] != 'completed':
        return jsonify({'error': t('export_not_completed')}), 400

    filename = csv_exports[export_id]['filename']
    file_path = os.path.join(CSV_EXPORTS_DIR, filename)

    if not os.path.exists(file_path):
        return jsonify({'error': t('file_not_found_system')}), 404

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    suffix = '' if csv_exports[export_id].get('include_nuc', True) else '_sin_secuencia'
    download_filename = f"bold_records{suffix}_{timestamp}.csv"

    return send_file(
        file_path,
        mimetype='text/csv',
        as_attachment=True,
        download_name=download_filename
    )


@app.route('/api/explain', methods=['POST'])
def explain_query():
    """Endpoint to explain the query's execution plan"""
    data = request.get_json()

    # If it's an FTS search
    if data.get('search_type') == 'fts':
        return explain_fts_query(data)

    # If it's a field-based search (original)
    conditions = data.get('conditions', [])
    
    if not conditions:
        return jsonify({'error': t('no_search_conditions')}), 400
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Reuse the exact same clause builder /api/search uses, instead of a
        # separate hand-rolled one: keeps the shown plan from silently
        # diverging from the query that actually runs (missing operators,
        # missing COLLATE NOCASE, ignored OR/NOT/grouping) and keeps the
        # column whitelist check in one place.
        technical_columns, table_name = get_column_names()
        where_clause, parameters = build_where_clause(conditions, technical_columns)

        explain_sql = f"EXPLAIN QUERY PLAN SELECT * FROM {table_name} WHERE {where_clause}"

        cursor.execute(explain_sql, parameters)
        explanation = cursor.fetchall()
        plan = [dict(row) for row in explanation]
        return jsonify({'plan': plan})
    except (ValueError, sqlite3.Error) as e:
        return jsonify({'error': str(e)}), 400 if isinstance(e, ValueError) else 500
    finally:
        conn.close()

def explain_fts_query(data):
    """Endpoint to explain the FTS query's execution plan"""
    query = _strip_accents(data.get('query', '').strip())

    if not query:
        return jsonify({'error': t('no_search_query')}), 400
    # Same translation search_fts() applies: explaining the raw text would
    # fail on any query that the real search accepts.
    query = escape_fts_query(query)
    if not query:
        return jsonify({'error': t('no_search_query')}), 400

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        _, main_table = get_column_names()

        explain_query = f"""
            EXPLAIN QUERY PLAN
            SELECT * FROM {main_table}
            WHERE rowid IN (
                SELECT rowid FROM {FTS_TABLE_NAME}
                WHERE {FTS_TABLE_NAME} MATCH ?
            )
        """

        cursor.execute(explain_query, (query,))
        explanation = cursor.fetchall()
        plan = [dict(row) for row in explanation]
        return jsonify({'plan': plan})
    except sqlite3.Error as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
        

_total_cache = {"key": None, "value": None}


def get_total_records():
    """Total records in the database, shown in the page header.

    Read from the bold_meta table that bold_db_creator.py fills at the end
    of Step 2 (and keeps up to date when updating), instead of a COUNT(*)
    over ~20M rows on every page load. Databases built before that table
    existed fall back to COUNT(*). Cached until the database file changes:
    the viewer can stay open while the database is updated.
    """
    try:
        key = os.path.getmtime(DATABASE_PATH)
    except OSError:
        key = None
    if key is not None and _total_cache["key"] == key:
        return _total_cache["value"]
    value = _count_total_records()
    _total_cache.update(key=key, value=value)
    return value


def _count_total_records():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Missing in databases built before bold_meta existed
        value = db_contract.read_meta(conn).get('record_count')
        if value is not None and str(value).isdigit():
            return int(value)
        cursor.execute(f"SELECT COUNT(*) FROM {MAIN_TABLE_NAME}")
        return cursor.fetchone()[0]
    finally:
        conn.close()

@app.route('/total_records', methods=['GET'])
def total_records():
    return jsonify({"total": get_total_records()})
    

def clear_export_folders():
    """Empties the export folders on startup. The viewer only serves the files
    it registered in memory during the session, so those of earlier sessions
    can't be downloaded any more: the browser already left its copy in the
    downloads folder."""
    folders = [CSV_EXPORTS_DIR, FASTA_EXPORTS_DIR, BATCH_EXPORTS_DIR, GENBANK_EXPORTS_DIR]
    for folder in folders:
        _prune_export_dir(folder, keep=0)
    # Analysis ZIPs were stored here before they were built in memory
    shutil.rmtree(os.path.join(_EXPORTS_ROOT, 'analysis_exports'), ignore_errors=True)
    # Scratch folders of GenBank searches cut short by a server stop
    for name in os.listdir(GENBANK_EXPORTS_DIR):
        if name.startswith('_work_'):
            shutil.rmtree(os.path.join(GENBANK_EXPORTS_DIR, name), ignore_errors=True)



def _report_fts_availability():
    """Logs to the console whether the FTS index is available.

    Checked at startup so the index's absence shows up here rather than as an
    error when clicking "Search": the full-text tab is already served
    disabled. A failure opening the database doesn't stop startup — the rest
    of the app reports that problem on its own.
    """
    problem = db_problem()
    if problem:
        # Startup has no request, hence no language cookie
        print(f"❌ {STRINGS['en'][problem]}")
        return
    try:
        conn = get_db_connection()
        try:
            version = db_contract.schema_version(conn)
        finally:
            conn.close()
        if version < db_contract.SCHEMA_VERSION:
            print("⚠️ This database was built by an older version of the Creator that kept "
                  "only one sequence per specimen. Rebuild it to get every marker's sequences.")
    except sqlite3.Error:
        pass
    try:
        if fts_table_available():
            print(f"✅ Full-text index available ({FTS_TABLE_NAME}).")
        else:
            print(f"⚠️ No full-text index ({FTS_TABLE_NAME}): the full-text "
                  "search tab will be shown disabled.")
    except Exception as e:
        print(f"⚠️ Could not check the full-text index: {e}")

_report_fts_availability()

@app.route('/list-static-files')
def list_static_files():
    # The name of the BOLD package the data comes from, which the Creator
    # records as an empty "<package>.tsv" (the page reads the name off it)
    folder = layout.PACKAGE_DIR
    files = [f"static/{f}" for f in os.listdir(folder) if f.endswith(".tsv")] if os.path.isdir(folder) else []
    return jsonify(files)


# ── Server lifetime ───────────────────────────────────────────────────────────
#
# The server (and its terminal window) closes once no browser tab has it open.
# It used to stop on the page's `unload` event, which also fires on a reload:
# F5 killed the server under the page that was about to reload, and closing
# one of two tabs killed it for the other. Now every tab registers with its
# own id, re-registers periodically and says goodbye on `pagehide`; the
# server only exits after SHUTDOWN_GRACE_SECS with no tab registered, which
# comfortably covers a reload.
#
# On top of that, one tab per browser keeps a Server-Sent Events connection
# open (/api/client/stream): as soon as it drops, the server knows, even if
# the browser closed without saying goodbye. Closing the whole browser window
# often lost the goodbye, and the console stayed open ~3 min
# (CLIENT_STALE_SECS). Once the first stream connects, the server's lifetime
# depends on the streams alone; heartbeats remain for browsers without them.

SHUTDOWN_GRACE_SECS = 10
# How often the server writes on the open connection: writing is what finds
# out that the other end is gone.
STREAM_PING_SECS = 2
# Chrome throttles timers in hidden tabs to about once a minute, so a tab
# that goes quiet for less than this is still considered open. It only
# matters when the goodbye never arrives (browser crash, killed process).
CLIENT_STALE_SECS = 180

_clients       = {}      # client id -> time.monotonic() of its last message
_clients_lock  = threading.Lock()
_had_client    = False   # nothing shuts down before the first tab connects
_empty_since   = None
_streams       = {}      # client id -> number of open streams
_stream_mode   = False   # a stream has connected: lifetime follows _streams
_CLIENT_ID_RE  = re.compile(r'^[A-Za-z0-9-]{8,64}$')


def _is_local_origin():
    """Rejects cross-site requests: any web page can POST to 127.0.0.1,
    and the old /shutdown endpoint let any of them stop the server."""
    origin = request.headers.get('Origin')
    if origin is None:
        return True
    return re.match(r'^https?://(127\.0\.0\.1|localhost)(:\d+)?$', origin) is not None


@app.route('/api/client/<kind>/<client_id>', methods=['POST'])
def client_presence(kind, client_id):
    global _had_client, _empty_since
    if kind not in ('hello', 'bye') or not _CLIENT_ID_RE.match(client_id):
        return '', 400
    if not _is_local_origin():
        return '', 403
    with _clients_lock:
        if kind == 'hello':
            _clients[client_id] = time.monotonic()
            _had_client  = True
            _empty_since = None
        else:
            _clients.pop(client_id, None)
    return '', 204


@app.route('/api/client/stream/<client_id>')
def client_stream(client_id):
    """Connection kept open while the tab lives (see "Server lifetime")."""
    if not _CLIENT_ID_RE.match(client_id):
        return '', 400
    if not _is_local_origin():
        return '', 403

    def _events():
        global _had_client, _empty_since, _stream_mode
        with _clients_lock:
            _streams[client_id] = _streams.get(client_id, 0) + 1
            _had_client  = True
            _stream_mode = True
            _empty_since = None
        try:
            # retry: if the connection drops while the tab stays open, the
            # browser reconnects after 2 s, well within the shutdown grace.
            yield "retry: 2000\n\n"
            while True:
                time.sleep(STREAM_PING_SECS)
                yield ": ping\n\n"
        finally:
            # Werkzeug closes the generator once the write fails because the
            # browser is gone.
            with _clients_lock:
                n = _streams.get(client_id, 0) - 1
                if n > 0:
                    _streams[client_id] = n
                else:
                    _streams.pop(client_id, None)

    return Response(_events(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _lifetime_watchdog():
    global _empty_since
    while True:
        time.sleep(2)
        now = time.monotonic()
        with _clients_lock:
            for cid, seen in list(_clients.items()):
                if now - seen > CLIENT_STALE_SECS:
                    _clients.pop(cid, None)
            alive = bool(_streams) if _stream_mode else bool(_clients)
            if alive or not _had_client:
                _empty_since = None
                continue
            if _empty_since is None:
                _empty_since = now
            if now - _empty_since < SHUTDOWN_GRACE_SECS:
                continue
        print("✅ No browser tab open: stopping the server.")
        sys.stdout.flush()
        os._exit(0)

# ── Batch search ──────────────────────────────────────────────────────────────

# Taxonomy columns that trigger the smart level-based search
_TAXONOMY_COLS = {
    'col_kingdom', 'col_phylum', 'col_class', 'col_order',
    'col_family', 'col_genus', 'col_species',
}

# Levels queried for a single-word term, from most specific to most general.
# Each one has its own index in bold_records, so the search is resolved with
# indexed lookups instead of scanning the whole table.
_TAXONOMY_LEVELS = [
    'col_genus', 'col_family', 'col_order',
    'col_class', 'col_phylum', 'col_kingdom',
]

def _clean_taxa_term(text):
    """Normalizes a taxon: strip, replace _, collapse spaces, capitalize genus."""
    text = text.replace('\r', '').replace('_', ' ').strip()
    text = re.sub(r'\s+', ' ', text)
    parts = text.split(' ')
    if parts and parts[0]:
        parts[0] = parts[0][0].upper() + parts[0][1:].lower()
    return ' '.join(parts)


def _strip_accents(text):
    """Removes accents and diacritics: 'Mérida' -> 'Merida', 'Ñu' -> 'Nu'."""
    return ''.join(
        ch for ch in unicodedata.normalize('NFD', str(text))
        if not unicodedata.combining(ch)
    )


_ASCII_LOWER = {c: c + 32 for c in range(ord('A'), ord('Z') + 1)}

def _nocase_key(text):
    """Key equivalent to SQLite's COLLATE NOCASE, which only folds A-Z.

    'Mérida' and 'MÉRIDA' are different under NOCASE (É isn't folded), so
    they must be different here too when deduplicating variants.
    """
    return text.translate(_ASCII_LOWER)


def _batch_terms(raw_values, is_taxonomy):
    """Normalizes the list of terms for a batch field.

    Returns `(canonical, variants)`:

    - `canonical`: accent-stripped terms, deduplicated case-insensitively.
      These are what name the files in the ZIP and what appear in
      summary.csv / not_found.txt.
    - `variants`: (value_to_search, canonical_term) pairs that get inserted
      into the temporary table. Besides the accent-stripped form, the
      original is kept when it differs: today the database doesn't store any
      properly-encoded accented value, but if it's ever re-imported corrected,
      the accented form will still be found without touching the code. Both
      variants point to the same canonical term, so the result grouping isn't
      duplicated (the queries use DISTINCT rid).

    Case-insensitivity is provided by the COLLATE NOCASE on the temporary
    table and on the bold_records columns. NOCASE only folds ASCII, so terms
    with non-ASCII letters also get their lowercase and uppercase forms
    ('Mérida' finds 'MÉRIDA' and 'mérida').
    """
    canonical_by_key = {}
    seen_variants    = set()
    variants         = []
    for raw in raw_values:
        if not raw or not str(raw).strip():
            continue
        cleaned   = _clean_taxa_term(raw) if is_taxonomy else str(raw).strip()
        canonical = _strip_accents(cleaned)
        if not canonical:
            continue
        key = canonical.lower()
        canonical_by_key.setdefault(key, canonical)
        # Variants accumulate per term: a list containing both 'Merida' and
        # 'Mérida' should search both forms, not just the first one seen.
        forms = [canonical, cleaned]
        if not cleaned.isascii():
            forms += [cleaned.lower(), cleaned.upper()]
        for variant in forms:
            vkey = (key, _nocase_key(variant))
            if vkey in seen_variants:
                continue
            seen_variants.add(vkey)
            variants.append((variant, canonical_by_key[key]))
    return sorted(canonical_by_key.values()), variants

def _safe_name(s, max_len=80):
    return re.sub(r'[^\w\s-]', '', str(s)).replace(' ', '_').strip('_')[:max_len] or 'unknown'


def _unique_name(s, used):
    """`_safe_name(s)`, with a `_2`, `_3`... suffix if it was already taken.

    `_safe_name` drops punctuation and truncates, so different terms can
    collapse into the same name ('A.B' and 'AB', two long values sharing
    their first 80 characters, any symbol-only term -> 'unknown'). zipfile
    only warns about duplicate entries and writes them anyway; on extraction
    one silently overwrites the other. `used` holds the names already given
    in the same folder, compared case-insensitively because Windows and macOS
    filesystems are.
    """
    base = _safe_name(s)
    name, n = base, 1
    while name.lower() in used:
        n += 1
        name = f'{base}_{n}'
    used.add(name.lower())
    return name


def _indexed_columns(conn):
    """Columns of bold_records that lead an index (the creator's `indexed` list)."""
    cur = conn.cursor()
    try:
        cur.execute(f'PRAGMA index_list("{MAIN_TABLE_NAME}")')
        names = [row[1] for row in cur.fetchall()]
        cols = set()
        for name in names:
            cur.execute(f'PRAGMA index_info("{name}")')
            info = cur.fetchall()
            if info and info[0][2]:
                cols.add(info[0][2])
        return cols
    finally:
        cur.close()


_FASTA_TAXON_COLS = [
    'col_species', 'col_genus', 'col_family', 'col_order',
    'col_class', 'col_phylum', 'col_kingdom',
]

def _row_to_fasta_entry(row, keys):
    """Converts an sqlite3.Row to a FASTA entry, or '' if not applicable.

    Shared by the FASTA export and the batch search, so both write the same
    header: processid|sampleid|bin|taxon|marker, where taxon is the most
    specific level available (see get_best_taxon). The batch search used to
    write only the species, leaving records identified to genus or family
    with no taxon at all.
    """
    seq = row['col_nuc'] or ''
    seq = seq.replace('-', '').strip('Nn')
    if len(seq) < 50:
        return ''

    def val(col):
        return row[col] if col in keys else None

    taxon = get_best_taxon(*(val(c) for c in _FASTA_TAXON_COLS))
    parts = []
    for value, underscore in ((val('col_processid'), False),
                              (val('col_sampleid'), True),
                              (val('col_bin_uri'), False),
                              (taxon, False),
                              (val('col_marker_code'), False)):
        if value:
            value = str(value)
            parts.append(value.replace(' ', '_') if underscore else value)
    if not parts:
        return ''
    return f">{'|'.join(parts)}\n{seq}\n"


def _rows_to_fasta(rows):
    """Converts a list of sqlite3.Row to FASTA text. Returns '' if there are no sequences."""
    if not rows:
        return ''
    keys = rows[0].keys()
    if 'col_nuc' not in keys:
        return ''
    return ''.join(_row_to_fasta_entry(row, keys) for row in rows)


class _BatchCancelled(Exception):
    """Raised when the user cancels a batch search."""


def get_batch_connection():
    """Connection for batch search.

    Unlike `get_export_connection`, uses `temp_store = FILE`: the temporary
    matches table can have millions of rows and shouldn't compete for RAM
    with the rest of the process.
    """
    conn = _connect_readonly(300.0)
    cursor = conn.cursor()
    cursor.execute('PRAGMA cache_size = -262144')   # 256MB
    cursor.execute('PRAGMA temp_store = FILE')
    cursor.execute('PRAGMA mmap_size = 2147483648')  # 2GB
    return conn


def _batch_check_cancel(export_id):
    if batch_exports.get(export_id, {}).get('cancel'):
        raise _BatchCancelled()


def _batch_match_sql(col1, is_taxonomy, search_subspecies=False):
    """SQL that populates the temporary table `_m` with (rowid, term).

    For taxonomy, a UNION ALL with one branch per level is emitted instead of
    an OR inside the JOIN: the OR forces SQLite to scan bold_records entirely
    (SCAN r × SCAN v1), whereas each UNION ALL branch is resolved using the
    corresponding level's index.

    `search_subspecies` adds a branch for col_subspecies, so a trinomial
    ("Panthera leo persica") can be found: col_species only holds binomials.
    The caller enables it only when that column exists and is indexed;
    without an index the branch would scan the whole table.

    The second field's filter is applied afterward, on `_m` (see
    `_batch_apply_field2`): including it here would make the level indexes
    stop being covering indexes and SQLite would fall back to a full table scan.
    """
    def branch(on_clause):
        # v1.term is projected (the accent-stripped canonical form), not
        # v1.val: this way both variants of the same term land in the same
        # result group.
        return ('SELECT r.rowid, v1.term '
                'FROM bold_records r '
                f'INNER JOIN _bv1 v1 ON {on_clause}')

    if not is_taxonomy:
        selects = [branch(f'r."{col1}" = v1.val')]
    else:
        # A binomial term ("Panthera leo") can only match at species level;
        # a single-word term is also searched across the remaining levels.
        selects = [branch('r."col_species" = v1.val')]
        selects += [
            branch(f"instr(v1.val, ' ') = 0 AND r.\"{lvl}\" = v1.val")
            for lvl in _TAXONOMY_LEVELS
        ]
        if search_subspecies:
            selects.append(branch(
                "instr(v1.val, ' ') > 0 AND r.\"col_subspecies\" = v1.val"
            ))

    return 'INSERT INTO _m (rid, term)\n' + '\nUNION ALL\n'.join(selects)


# Above this many field-1 matches, an indexed field 2 is resolved through its
# own index instead of looking up each match by rowid (see _batch_apply_field2).
_FIELD2_FROM_MATCHES_MAX = 200_000


def _batch_apply_field2(cursor, col2, n_matches, col2_indexed):
    """Restricts `_m` to records that also match field 2.

    Two ways to find the records that match field 2:

    - From `_m`: one rowid lookup per field-1 match. Its cost depends only
      on how many matches there are, so it's the right one when they are few,
      or when `col2` has no index (the alternative would scan the whole table).
    - Through `col2`'s index: covering, but it walks every record with that
      value — millions for a common value such as a country — even if `_m`
      holds a handful. Only worth it for an indexed `col2` and many matches.
    """
    cursor.execute('CREATE TEMP TABLE _f2 (rid INTEGER PRIMARY KEY, val TEXT)')
    if col2_indexed and n_matches > _FIELD2_FROM_MATCHES_MAX:
        cursor.execute(
            f'INSERT OR IGNORE INTO _f2 (rid, val) '
            f'SELECT r.rowid, v2.term FROM bold_records r '
            f'INNER JOIN _bv2 v2 ON r."{col2}" = v2.val'
        )
    else:
        # CROSS JOIN pins the loop order (SQLite never reorders it): left to
        # itself, the planner may start from col2's index again. The index on
        # _bv2 (NOCASE, like the bold_records columns) turns the per-record
        # comparison into a lookup instead of a scan of _bv2.
        cursor.execute('CREATE INDEX _bv2_idx ON _bv2 (val)')
        cursor.execute(
            f'INSERT OR IGNORE INTO _f2 (rid, val) '
            f'SELECT r.rowid, v2.term FROM (SELECT DISTINCT rid FROM _m) m '
            f'CROSS JOIN bold_records r ON r.rowid = m.rid '
            f'CROSS JOIN _bv2 v2 ON r."{col2}" = v2.val'
        )
    cursor.execute('DELETE FROM _m WHERE rid NOT IN (SELECT rid FROM _f2)')
    cursor.execute('UPDATE _m SET k2 = (SELECT val FROM _f2 WHERE _f2.rid = _m.rid)')


def _parse_report_spec(report, technical_columns):
    """Validates the `report` block of the batch search payload.

    Returns `(metrics, breakdowns, opts)`. Without a block (or with everything
    empty) it returns empty lists, and the rest of the process then behaves
    exactly as it did before this function existed: summary.csv comes out
    identical and no additional query is executed.

    Anything not in METRIC_COLUMNS is silently discarded.
    """
    if not report:
        return [], [], {'with_counts': False, 'max_list': 0}

    allowed = {c for c in METRIC_COLUMNS if c in technical_columns}

    metrics, seen = [], set()
    for m in report.get('metrics') or []:
        if not isinstance(m, dict):
            continue
        col = m.get('column')
        if col not in allowed or col in seen:
            continue
        want_count, want_list = bool(m.get('count')), bool(m.get('list'))
        if not (want_count or want_list):
            continue
        seen.add(col)
        metrics.append({'column': col, 'count': want_count, 'list': want_list})

    breakdowns = [
        c for c in dict.fromkeys(report.get('breakdowns') or []) if c in allowed
    ]

    try:
        max_list = max(0, int(report.get('max_list') or 0))
    except (TypeError, ValueError):
        max_list = 0

    return metrics, breakdowns, {
        'with_counts': bool(report.get('with_counts')),
        'max_list':    max_list,
    }


def _metric_headers(metrics):
    """Headers that the metrics add to summary.csv, in order."""
    headers = []
    for spec in metrics:
        friendly = get_metric_label(spec['column'])
        if spec['count']:
            headers.append(f'# {friendly}')
        if spec['list']:
            headers.append(friendly)
    return headers


def _metric_exact(conn, where_rows, params, col, top_n):
    """Exact count and top-N for a (group, column) that overflowed the counter.

    SQLite groups millions of values without bringing them into the process,
    so the figure is exact and the top-N is sorted by actual frequency, not
    by arrival order. Only invoked for groups that exceeded METRIC_MAX_DISTINCT.
    """
    cur  = conn.cursor()
    base = (f'FROM bold_records r {where_rows} '
            f'AND r."{col}" IS NOT NULL AND r."{col}" != \'\'')
    try:
        cur.execute(f'SELECT COUNT(DISTINCT r."{col}") {base}', params)
        n_distinct = cur.fetchone()[0] or 0

        cur.execute(
            f'SELECT r."{col}", COUNT(*) AS n {base} GROUP BY 1 ORDER BY n DESC, 1 LIMIT ?',
            tuple(params) + (top_n,),
        )
        items = [(str(v), n) for v, n in cur.fetchall()]
    finally:
        cur.close()
    return n_distinct, items


def _format_metric_list(items, n_distinct, opts):
    """Formats the `which values` cell: values sorted by frequency.

    The truncation is a presentation decision (a cell with thousands of
    values is unusable), and it's always disclosed: the count in the `#`
    column remains the exact total.
    """
    max_list = opts.get('max_list', 0)
    shown    = items[:max_list] if max_list > 0 else items
    if opts.get('with_counts'):
        text = '; '.join(f'{v} ({n:,})' for v, n in shown)
    else:
        text = '; '.join(str(v) for v, n in shown)
    rest = n_distinct - len(shown)
    if rest > 0:
        more = t('and_n_more', n=f'{rest:,}')
        text = f'{text}; {more}' if text else more
    return text


def _metric_cells(gkey, metrics, tally, exact, opts):
    """Metric cells for a summary row, in header order."""
    cells = []
    for spec in metrics:
        col      = spec['column']
        resolved = exact.get((gkey, col))
        if resolved is not None:
            n_distinct, items = resolved
        else:
            counter = tally.get((gkey, col))
            if counter:
                n_distinct = len(counter)
                # By descending frequency; ties broken alphabetically (stable).
                items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
            else:
                n_distinct, items = 0, []
        if spec['count']:
            cells.append(n_distinct)
        if spec['list']:
            cells.append(_format_metric_list(items, n_distinct, opts))
    return cells


def _empty_metric_cells(metrics):
    """Cells for a term with no records: 0 distinct, empty list."""
    cells = []
    for spec in metrics:
        if spec['count']:
            cells.append(0)
        if spec['list']:
            cells.append('')
    return cells


def process_batch_search(export_id, fields_data, include_fasta=False, report=None):
    zip_path   = None
    conn       = None
    try:
        technical_columns, _ = get_column_names()

        col1        = fields_data[0]['technical']
        raw_vals1   = fields_data[0].get('values', [])
        is_taxonomy = col1 in _TAXONOMY_COLS

        values1, variants1 = _batch_terms(raw_vals1, is_taxonomy)
        if not values1:
            raise ValueError(t('field_required'))

        col2, values2, variants2 = None, [], []
        if len(fields_data) > 1 and fields_data[1].get('values'):
            col2 = fields_data[1]['technical']
            values2, variants2 = _batch_terms(
                fields_data[1]['values'], col2 in _TAXONOMY_COLS
            )
        # Decided after normalizing: a field 2 holding only blank lines used to
        # count as present, filter against an empty list and discard every
        # field-1 match, reporting "no records".
        has_field2 = bool(values2)

        # Validate column names to prevent SQL injection
        if col1 not in technical_columns:
            raise ValueError(t('invalid_field', field=col1))
        if has_field2 and col2 not in technical_columns:
            raise ValueError(t('invalid_field', field=col2))

        output_cols   = list(technical_columns)
        friendly_cols = [get_friendly_name(c) for c in output_cols]
        col_select    = ', '.join(f'r."{c}"' for c in output_cols)

        # ── Custom summary metrics ────────────────────────────────────────────
        # Accumulated during the ZIP dump (phase 2), which already iterates
        # every matching record: no extra query or read is needed.
        metrics, breakdowns, report_opts = _parse_report_spec(report, technical_columns)
        metric_idx    = [(m['column'], output_cols.index(m['column'])) for m in metrics]
        metric_head   = _metric_headers(metrics)
        tally         = defaultdict(Counter)   # (group, column) -> {value: n}
        overflow      = set()                  # groups that exceeded the cap
        exact         = {}                     # results recalculated with SQL

        batch_exports[export_id].update({
            'message':  tn('searching_terms', len(values1)),
            'progress': 5,
        })

        # The export_id suffix keeps two searches started in the same second
        # (a double click, two tabs) from writing — and, on failure,
        # deleting — the same file.
        timestamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
        zip_filename = f'batch_{timestamp}_{export_id[:8]}.zip'
        zip_path     = os.path.join(BATCH_EXPORTS_DIR, zip_filename)

        conn = get_batch_connection()
        batch_exports[export_id]['_conn'] = conn
        cursor = conn.cursor()

        # ── Phase 1: resolve matches using indexes ────────────────────────────
        # Temporary tables to avoid SQLite's variable limit
        cursor.execute('CREATE TEMP TABLE _bv1 (val TEXT COLLATE NOCASE, term TEXT)')
        cursor.executemany('INSERT INTO _bv1 (val, term) VALUES (?, ?)', variants1)
        if has_field2:
            cursor.execute('CREATE TEMP TABLE _bv2 (val TEXT COLLATE NOCASE, term TEXT)')
            cursor.executemany('INSERT INTO _bv2 (val, term) VALUES (?, ?)', variants2)

        cursor.execute('CREATE TEMP TABLE _m (rid INTEGER, term TEXT, k2 TEXT COLLATE NOCASE)')
        indexed_cols = _indexed_columns(conn)
        search_subspecies = (
            is_taxonomy
            and 'col_subspecies' in indexed_cols
            and any(v.count(' ') >= 2 for v, _ in variants1)
        )
        _batch_check_cancel(export_id)
        cursor.execute(_batch_match_sql(col1, is_taxonomy, search_subspecies))

        _batch_check_cancel(export_id)
        cursor.execute('SELECT COUNT(*) FROM _m')
        n_matches = cursor.fetchone()[0]
        if has_field2:
            batch_exports[export_id].update({
                'message':  t('filtering_by', field=get_friendly_name(col2)),
                'progress': 12,
            })
            _batch_apply_field2(cursor, col2, n_matches, col2 in indexed_cols)
            _batch_check_cancel(export_id)
            cursor.execute('CREATE INDEX _m_idx ON _m (k2, term)')
        else:
            cursor.execute('CREATE INDEX _m_idx ON _m (term)')

        # Counts per group: cheap over `_m` and allows reporting real progress
        if has_field2:
            cursor.execute('SELECT k2, term, COUNT(DISTINCT rid) FROM _m GROUP BY k2, term')
            counts = {(k2, term): n for k2, term, n in cursor.fetchall()}
        else:
            cursor.execute('SELECT term, COUNT(DISTINCT rid) FROM _m GROUP BY term')
            counts = {term: n for term, n in cursor.fetchall()}

        # Distinct records: one matching several terms counts once. The
        # per-term figures in summary.csv still count it under each term.
        cursor.execute('SELECT COUNT(DISTINCT rid) FROM _m')
        total_rows = cursor.fetchone()[0]

        # No ZIP is generated when there are no matches: its three files would
        # be a not_found.txt with the full input list, a summary.csv with
        # everything at zero, and an all_results.tsv with only the header.
        # The search finishes successfully (it's not an error), but without a
        # `filename`, so the frontend doesn't offer a download.
        if total_rows == 0:
            # A single term or value is named instead of counted.
            if has_field2 and n_matches:
                # Field 1 did find records; the field-2 filter discarded them.
                field2_name = get_friendly_name(col2)
                if len(values2) == 1:
                    target = t('filter_target_one', field=field2_name, value=values2[0])
                else:
                    target = tn('filter_target_many', len(values2), field=field2_name)
                message = t('no_records_after_filter', target=target)
            elif len(values1) == 1:
                message = t('no_records_for_term', term=values1[0])
            else:
                message = tn('no_records_for_terms', len(values1))
            batch_exports[export_id].update({
                'status':     'completed',
                'progress':   100,
                'total_rows': 0,
                'message':    message,
            })
            return

        batch_exports[export_id].update({
            'total_rows': total_rows,
            'message':    tn('records_found_packing', total_rows),
            'progress':   20,
        })

        # ── Phase 2: write the ZIP in streaming mode ──────────────────────────
        if has_field2:
            where_rows = 'WHERE r.rowid IN (SELECT rid FROM _m WHERE k2 IS ? AND term = ?)'
        else:
            where_rows = 'WHERE r.rowid IN (SELECT rid FROM _m WHERE term = ?)'
        rows_query  = f'SELECT {col_select} FROM bold_records r {where_rows}'
        fasta_query = f'SELECT r.* FROM bold_records r {where_rows}'

        def row_to_list(row):
            return ['' if row[c] is None else str(row[c]) for c in output_cols]

        def tally_row(gkey, rec):
            """Accumulates the metrics for an already-read record.

            Once the cap is exceeded, new values stop being admitted and the
            group is flagged: its figure will be recalculated later with SQL,
            because which values survive here is decided by arrival order,
            not frequency.
            """
            for col, ci in metric_idx:
                value = rec[ci]
                if not value:
                    continue
                counter = tally[(gkey, col)]
                if value in counter:
                    counter[value] += 1
                elif len(counter) < METRIC_MAX_DISTINCT:
                    counter[value] = 1
                else:
                    overflow.add((gkey, col))

        def resolve_overflow():
            """Recalculates with exact SQL the groups that overflowed the counter."""
            if not overflow:
                return
            batch_exports[export_id]['message'] = t('recalculating_metrics')
            list_cap = report_opts.get('max_list') or METRIC_OVERFLOW_LIST
            for gkey, col in overflow:
                if (gkey, col) in exact:
                    continue
                _batch_check_cancel(export_id)
                exact[(gkey, col)] = _metric_exact(conn, where_rows, gkey, col, list_cap)

        def stream_group_tsv(zf, arcname, params):
            """Dumps a group to `arcname` without materializing the rows in memory.

            `params` identifies the group — `(term,)` or `(field2, term)` —
            so it also serves as the key for the metrics accumulator."""
            gcur = conn.cursor()
            gcur.execute(rows_query, params)
            # utf-8-sig: the BOM makes Excel recognize the encoding. Without it,
            # it opens the file as cp1252 and 'País' shows up as 'PaÃ­s'.
            # force_zip64: since we're writing in streaming mode the final size
            # is unknown, and a single taxon can exceed 2GB (e.g. a country
            # with millions of sequences). Without this, zipfile aborts when
            # closing the entry.
            with io.TextIOWrapper(zf.open(arcname, 'w', force_zip64=True),
                                  encoding='utf-8-sig', newline='') as fh:
                w = csv.writer(fh, delimiter='\t')
                w.writerow(friendly_cols)
                while True:
                    rows = gcur.fetchmany(2000)
                    if not rows:
                        break
                    for row in rows:
                        rec = row_to_list(row)
                        w.writerow(rec)
                        if metric_idx:
                            tally_row(params, rec)
            gcur.close()

        def stream_group_fasta(zf, arcname, params):
            gcur = conn.cursor()
            gcur.execute(fasta_query, params)
            first = gcur.fetchmany(2000)
            if not first:
                gcur.close()
                return
            keys = first[0].keys()
            if 'col_nuc' not in keys:
                gcur.close()
                return
            # The entry is opened only once there is something to write: a ZIP
            # entry can't be deleted afterwards, and a group whose sequences
            # are all < 50 bp used to leave an empty .fasta behind.
            rows, chunk = first, ''
            while rows:
                chunk = ''.join(_row_to_fasta_entry(r, keys) for r in rows)
                if chunk:
                    break
                rows = gcur.fetchmany(2000)
            if not chunk:
                gcur.close()
                return
            # No BOM: FASTA files are consumed by bioinformatics tools, which
            # would interpret the BOM as part of the first header.
            with io.TextIOWrapper(zf.open(arcname, 'w', force_zip64=True),
                                  encoding='utf-8', newline='') as fh:
                fh.write(chunk)
                while True:
                    rows = gcur.fetchmany(2000)
                    if not rows:
                        break
                    fh.write(''.join(_row_to_fasta_entry(r, keys) for r in rows))
            gcur.close()

        zero_omitted = 0   # zero combinations that don't fit in summary.csv
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if not has_field2:
                found_terms = sorted(counts)
                found_lower = {t.lower() for t in found_terms}
                not_found   = [v for v in values1 if v.lower() not in found_lower]

                used_names = set()
                for i, term in enumerate(found_terms):
                    _batch_check_cancel(export_id)
                    sn = _unique_name(term, used_names)
                    stream_group_tsv(zf, f'individual/{sn}.tsv', (term,))
                    if include_fasta:
                        stream_group_fasta(zf, f'individual/{sn}.fasta', (term,))
                    batch_exports[export_id]['progress'] = \
                        20 + int((i + 1) / max(len(found_terms), 1) * 75)

                # not_found.txt is intentionally written without a BOM: its
                # content is always ASCII and it's the file the user
                # typically re-uploads as a term list.
                if not_found:
                    zf.writestr('not_found.txt', '\n'.join(not_found))

                resolve_overflow()

                buf = io.StringIO()
                w   = csv.writer(buf)
                w.writerow([get_friendly_name(col1), t('col_records')] + metric_head)
                for term in found_terms:
                    w.writerow(
                        [term, counts[term]]
                        + _metric_cells((term,), metrics, tally, exact, report_opts)
                    )
                for term in not_found:
                    w.writerow([term, 0] + _empty_metric_cells(metrics))
                zf.writestr('summary.csv', buf.getvalue().encode('utf-8-sig'))

            else:
                # counts: {(field2_value, term): n}. k2 is v2.term, i.e. the
                # same canonical string as in values2, so values2 indexes
                # by_k2 directly (no case-insensitive search needed).
                by_k2 = {}
                for (k2, term), n in counts.items():
                    by_k2.setdefault(k2, {})[term] = n

                found_anywhere = set()
                summary_rows   = []
                used_dirs      = set()
                for i, val2 in enumerate(values2):
                    _batch_check_cancel(export_id)
                    val2_data = by_k2.get(val2, {})
                    # A field-2 value with no records gets no folder: its
                    # not_found.txt would just repeat the whole field-1 list,
                    # and summary_by_field2.csv already reports it with 0.
                    if not val2_data:
                        continue
                    found_anywhere.update(val2_data)
                    dir_name   = _unique_name(val2, used_dirs)
                    used_names = set()

                    for term, n in sorted(val2_data.items()):
                        sn = _unique_name(term, used_names)
                        stream_group_tsv(zf, f'{dir_name}/{sn}.tsv', (val2, term))
                        if include_fasta:
                            stream_group_fasta(zf, f'{dir_name}/{sn}.fasta', (val2, term))
                        # The group key is stored: metrics are accumulated
                        # per (field 2, term) pair, not per term.
                        summary_rows.append((term, val2, n, (val2, term)))

                    not_found_here = [v for v in values1 if v not in val2_data]
                    if not_found_here:
                        zf.writestr(f'{dir_name}/not_found.txt', '\n'.join(not_found_here))

                    batch_exports[export_id]['progress'] = \
                        20 + int((i + 1) / max(len(values2), 1) * 75)

                # Terms with no records under any field-2 value: listed at the
                # root as in the single-field mode, so the file can be
                # re-uploaded as is.
                not_found = [v for v in values1 if v not in found_anywhere]
                if not_found:
                    zf.writestr('not_found.txt', '\n'.join(not_found))

                resolve_overflow()

                # summary.csv: as in the single-field mode, first the
                # combinations with records, then those without any, at 0:
                # the full term × value matrix, ready for a pivot table. It
                # can get very large, so it's streamed and, above the cap, the
                # zeros are left out (the final message says so).
                n_zero = len(values1) * len(values2) - len(summary_rows)
                if n_zero > SUMMARY_ZERO_ROWS_MAX:
                    zero_omitted = n_zero
                with io.TextIOWrapper(zf.open('summary.csv', 'w', force_zip64=True),
                                      encoding='utf-8-sig', newline='') as fh:
                    w = csv.writer(fh)
                    w.writerow(
                        [get_friendly_name(col1), get_friendly_name(col2), t('col_records')]
                        + metric_head
                    )
                    for v1, v2, cnt, gkey in sorted(summary_rows, key=lambda r: (r[0], r[1])):
                        w.writerow(
                            [v1, v2, cnt]
                            + _metric_cells(gkey, metrics, tally, exact, report_opts)
                        )
                    empty_cells = _empty_metric_cells(metrics)
                    for v1 in ([] if zero_omitted else values1):
                        _batch_check_cancel(export_id)
                        for v2 in values2:
                            if v1 not in by_k2.get(v2, {}):
                                w.writerow([v1, v2, 0] + empty_cells)

                # summary_by_field2.csv
                buf = io.StringIO()
                w   = csv.writer(buf)
                w.writerow([
                    get_friendly_name(col2), t('col_total_records'),
                    t('col_found', field=get_friendly_name(col1)),
                    t('col_not_found', field=get_friendly_name(col1)),
                ])
                for val2 in values2:
                    val2_data  = by_k2.get(val2, {})
                    found_cnt  = len(val2_data)
                    total_cnt  = sum(val2_data.values())
                    w.writerow([val2, total_cnt, found_cnt, len(values1) - found_cnt])
                zf.writestr('summary_by_field2.csv', buf.getvalue().encode('utf-8-sig'))

            # ── Breakdown reports ─────────────────────────────────────────
            # Total records for each field value, across the whole result.
            # Resolved in SQLite against `_m`, so they're exact and don't
            # depend on the in-memory accumulator. COUNT(DISTINCT rid) is
            # used because the same record can match more than one term
            # (e.g. 'Panthera' and 'Panthera leo').
            for bcol in breakdowns:
                _batch_check_cancel(export_id)
                bcur = conn.cursor()
                try:
                    bcur.execute(
                        f'SELECT r."{bcol}", COUNT(DISTINCT m.rid) AS n '
                        f'FROM _m m JOIN bold_records r ON r.rowid = m.rid '
                        f'WHERE r."{bcol}" IS NOT NULL AND r."{bcol}" != \'\' '
                        f'GROUP BY 1 ORDER BY n DESC, 1'
                    )
                    buf = io.StringIO()
                    w   = csv.writer(buf)
                    w.writerow([get_metric_label(bcol), t('col_records')])
                    for value, n in bcur:
                        w.writerow([value, n])
                finally:
                    bcur.close()
                slug = _safe_name(_strip_accents(get_metric_label(bcol))).lower()
                zf.writestr(f'summary_by_{slug}.csv',
                            buf.getvalue().encode('utf-8-sig'))

            # all_results.tsv: every matching record once, even if it matched
            # several terms ('Panthera' and 'Panthera leo'). It used to be the
            # concatenation of the per-term files, so such records appeared
            # once per term. Written last: zipfile allows one open entry at a time.
            _batch_check_cancel(export_id)
            batch_exports[export_id].update({'message': t('adding_all_results'), 'progress': 96})
            acur = conn.cursor()
            try:
                acur.execute(f'SELECT {col_select} FROM bold_records r '
                             f'WHERE r.rowid IN (SELECT rid FROM _m) ORDER BY r.rowid')
                with io.TextIOWrapper(zf.open('all_results.tsv', 'w', force_zip64=True),
                                      encoding='utf-8-sig', newline='') as fh:
                    w = csv.writer(fh, delimiter='\t')
                    w.writerow(friendly_cols)
                    while True:
                        rows = acur.fetchmany(2000)
                        if not rows:
                            break
                        w.writerows(row_to_list(r) for r in rows)
            finally:
                acur.close()

        conn.close()
        conn = None

        message = tn('records_found', total_rows)
        if zero_omitted:
            message += t('summary_zeros_omitted', count=f'{zero_omitted:,}')
        batch_exports[export_id].update({
            'status':     'completed',
            'progress':   100,
            'message':    message,
            'total_rows': total_rows,
            'filename':   zip_filename,
        })
        batch_exports[export_id].pop('_conn', None)
        _prune_export_dir(BATCH_EXPORTS_DIR)

    except _BatchCancelled:
        batch_exports[export_id].update({'status': 'cancelled', 'message': t('search_cancelled')})

    except Exception as e:
        import traceback
        if batch_exports.get(export_id, {}).get('cancel'):
            batch_exports[export_id].update({'status': 'cancelled', 'message': t('search_cancelled')})
        else:
            batch_exports[export_id].update({'status': 'error', 'message': t('generic_error', error=str(e))})
            traceback.print_exc()

    finally:
        batch_exports.get(export_id, {}).pop('_conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if batch_exports.get(export_id, {}).get('status') in ('error', 'cancelled'):
            if zip_path and os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except Exception:
                    pass


def _has_terms(values):
    """True if `values` is a list with at least one non-blank term."""
    return isinstance(values, list) and any(v is not None and str(v).strip() for v in values)


@app.route('/api/batch_search', methods=['POST'])
def start_batch_search():
    try:
        data         = request.get_json(silent=True) or {}
        fields_data  = data.get('fields') or []
        include_fasta = bool(data.get('include_fasta', False))
        # Optional: without it, the summary comes out as usual (see _parse_report_spec)
        report        = data.get('report') or None

        # Blank lines don't count: a list of only blanks used to start a
        # search for 0 terms instead of being rejected here.
        if (not isinstance(fields_data, list) or not fields_data
                or not isinstance(fields_data[0], dict)
                or not _has_terms(fields_data[0].get('values'))):
            return jsonify({'error': t('field_required')}), 400
        # A field-2 file with only blank lines is rejected too: silently
        # searching without the filter would answer a different question.
        if len(fields_data) > 1:
            f2 = fields_data[1]
            if not isinstance(f2, dict) or (f2.get('values') and not _has_terms(f2['values'])):
                return jsonify({'error': t('field2_no_values')}), 400

        export_id = str(uuid.uuid4())
        batch_exports[export_id] = {
            'status':     'processing',
            'progress':   0,
            'message':    t('starting_search'),
            'filename':   None,
            'total_rows': 0,
            'start_time': datetime.now(),
        }
        _prune_export_status(batch_exports)

        start_background(process_batch_search, export_id, fields_data, include_fasta, report)

        return jsonify({'export_id': export_id})

    except Exception as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500


@app.route('/api/batch_search_status/<export_id>')
def batch_search_status(export_id):
    if export_id not in batch_exports:
        return jsonify({'error': t('invalid_id')}), 404
    st = batch_exports[export_id]
    return jsonify({
        'status':       st['status'],
        'progress':     st.get('progress', 0),
        'message':      st['message'],
        'total_rows':   st.get('total_rows', 0),
        'download_url': f'/api/download_batch/{export_id}'
                        if st.get('filename') and st['status'] == 'completed' else None,
    })


@app.route('/api/cancel_batch_search/<export_id>', methods=['POST'])
def cancel_batch_search(export_id):
    """Marks the search as cancelled and interrupts the query in progress."""
    st = batch_exports.get(export_id)
    if not st:
        return jsonify({'ok': False, 'error': t('invalid_id')}), 404
    st['cancel'] = True
    conn = st.get('_conn')
    if conn is not None:
        try:
            conn.interrupt()
        except Exception:
            pass
    return jsonify({'ok': True})


@app.route('/api/download_batch/<export_id>')
def download_batch(export_id):
    if export_id not in batch_exports:
        return jsonify({'error': t('invalid_id')}), 404
    st = batch_exports[export_id]
    if st['status'] != 'completed' or not st.get('filename'):
        return jsonify({'error': t('not_available')}), 400
    filepath = os.path.join(BATCH_EXPORTS_DIR, st['filename'])
    if not os.path.exists(filepath):
        return jsonify({'error': t('file_not_found')}), 404
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return send_file(
        filepath,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'batch_results_{ts}.zip',
    )


# ── GenBank batch search ──────────────────────────────────────────────────────
#
# Queries NCBI GenBank online (not the local database); the logic lives in
# genbank_batch.py, ported from BarcodeSuite. One search at a time: NCBI
# penalizes clients that hammer it, and the rate limit is per job.

def _genbank_running():
    return any(st.get('status') == 'processing' for st in genbank_jobs.values())


def _parse_genbank_request(data):
    """Validates the request body into the job's cfg, or raises ValueError."""
    def term_list(key):
        raw = data.get(key) or []
        if isinstance(raw, str):
            raw = raw.splitlines()
        if not isinstance(raw, list):
            raw = []
        return genbank_batch.normalize_terms(raw)

    field1 = data.get('field1_tag')
    terms1 = term_list('field1_terms')
    if not terms1:
        raise ValueError(t('genbank_terms_required'))
    if field1 not in genbank_batch.FIELD_OPTIONS:
        raise ValueError(t('genbank_invalid_field', field=field1))

    field2_enabled = bool(data.get('field2_enabled'))
    field2, terms2 = None, []
    if field2_enabled:
        field2 = data.get('field2_tag')
        terms2 = term_list('field2_terms')
        if not terms2:
            raise ValueError(t('genbank_field2_no_terms'))
        if field2 not in genbank_batch.FIELD_OPTIONS:
            raise ValueError(t('genbank_invalid_field', field=field2))

    try:
        retmax = int(data.get('retmax', genbank_batch.RETMAX_DEFAULT))
    except (TypeError, ValueError):
        retmax = genbank_batch.RETMAX_DEFAULT
    retmax = max(genbank_batch.RETMAX_MIN, min(genbank_batch.RETMAX_MAX, retmax))

    # Report metrics: only whitelisted columns, in the whitelist's order
    allowed = [c for c, _ in genbank_batch.REPORT_COLUMNS]
    requested = {}
    for m in data.get('report_metrics') or []:
        if isinstance(m, dict) and m.get('column') in allowed:
            requested[m['column']] = m
    metrics = []
    for col in allowed:
        m = requested.get(col)
        if m and (m.get('count') or m.get('list')):
            metrics.append({
                'column':      col,
                'count':       bool(m.get('count')),
                'list':        bool(m.get('list')),
                'with_counts': bool(m.get('list') and m.get('with_counts')),
            })
    try:
        max_list = max(0, int(data.get('report_max_list') or 0))
    except (TypeError, ValueError):
        max_list = 0

    api_key = str(data.get('api_key') or '').strip()
    # An NCBI API key is alphanumeric; anything else would only make every
    # request fail (and must not be injected into the URL)
    if api_key and not re.fullmatch(r'[A-Za-z0-9]{1,64}', api_key):
        api_key = ''

    return {
        'api_key':         api_key,
        'field1_tag':      field1,
        'field1_terms':    terms1,
        'field2_enabled':  field2_enabled,
        'field2_tag':      field2,
        'field2_terms':    terms2,
        'retmax':          retmax,
        'want_metadata':   bool(data.get('want_metadata')),
        'want_fasta':      bool(data.get('want_fasta')),
        'report_metrics':  metrics,
        'report_max_list': max_list,
    }


def process_genbank_batch(job_id, cfg):
    st = genbank_jobs[job_id]
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    zip_name  = f'genbank_{timestamp}_{job_id[:8]}.zip'
    zip_path  = os.path.join(GENBANK_EXPORTS_DIR, zip_name)
    job = genbank_batch.GenbankBatchJob(
        cfg,
        workdir=os.path.join(GENBANK_EXPORTS_DIR, f'_work_{job_id}'),
        zip_path=zip_path, status=st, tr=t,
    )
    try:
        summary = job.run()
        if st.get('cancel'):
            message = t('genbank_stopped', done=f"{summary['queries_done']:,}",
                        total=f"{summary['queries_total']:,}")
            final = 'cancelled'
        else:
            message = t('genbank_done', done=f"{summary['queries_done']:,}",
                        records=f"{summary['records']:,}")
            final = 'completed'
        if summary['failed']:
            message += t('genbank_done_failed', n=f"{summary['failed']:,}")
        st.update({'status': final, 'progress': 100, 'message': message,
                   'filename': zip_name})
        _prune_export_dir(GENBANK_EXPORTS_DIR)
    except genbank_batch.GenbankCancelled:
        st.update({'status': 'cancelled', 'message': t('search_cancelled')})
    except Exception as e:
        import traceback
        traceback.print_exc()
        st.update({'status': 'error', 'message': t('generic_error', error=str(e))})
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass
    finally:
        job.cleanup()


@app.route('/api/genbank_batch', methods=['POST'])
def start_genbank_batch():
    try:
        data = request.get_json(silent=True) or {}
        try:
            cfg = _parse_genbank_request(data)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        with exports_lock:
            if _genbank_running():
                return jsonify({'error': t('genbank_already_running')}), 409
            job_id = str(uuid.uuid4())
            genbank_jobs[job_id] = {
                'status':     'processing',
                'progress':   0,
                'message':    t('starting_search'),
                'filename':   None,
                'log':        [],
                'log_offset': 0,
                'start_time': datetime.now(),
            }
        _prune_export_status(genbank_jobs)
        start_background(process_genbank_batch, job_id, cfg)
        n_queries = len(genbank_batch.build_pairs(cfg['field1_terms'], cfg['field2_terms']))
        return jsonify({'job_id': job_id, 'queries': n_queries})
    except Exception as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500


@app.route('/api/genbank_batch_status/<job_id>')
def genbank_batch_status(job_id):
    st = genbank_jobs.get(job_id)
    if not st:
        return jsonify({'error': t('invalid_id')}), 404
    # `since` = log lines the page already has (absolute index), so each
    # poll only carries the new ones.
    try:
        since = max(0, int(request.args.get('since', 0)))
    except ValueError:
        since = 0
    offset = st.get('log_offset', 0)
    log = list(st.get('log', []))
    return jsonify({
        'status':       st['status'],
        'progress':     st.get('progress', 0),
        'message':      st.get('message', ''),
        'log':          log[max(0, since - offset):],
        'log_total':    offset + len(log),
        'download_url': f'/api/download_genbank/{job_id}'
                        if st.get('filename') and st['status'] in ('completed', 'cancelled') else None,
    })


@app.route('/api/cancel_genbank_batch/<job_id>', methods=['POST'])
def cancel_genbank_batch(job_id):
    st = genbank_jobs.get(job_id)
    if not st:
        return jsonify({'ok': False, 'error': t('invalid_id')}), 404
    st['cancel'] = True
    return jsonify({'ok': True})


@app.route('/api/download_genbank/<job_id>')
def download_genbank(job_id):
    st = genbank_jobs.get(job_id)
    if not st:
        return jsonify({'error': t('invalid_id')}), 404
    if not st.get('filename'):
        return jsonify({'error': t('not_available')}), 400
    filepath = os.path.join(GENBANK_EXPORTS_DIR, st['filename'])
    if not os.path.exists(filepath):
        return jsonify({'error': t('file_not_found')}), 404
    return send_file(
        filepath,
        mimetype='application/zip',
        as_attachment=True,
        download_name=st['filename'],
    )


# ── Sequence analysis ─────────────────────────────────────────────────────────
#
# The "Sequence analysis" tab. The analyses themselves live in
# seq_analysis.py; this section only gathers the sequences (the checked
# records, sent by the page; the search on screen, read here; or a FASTA) and
# makes sure a run covers a single marker. They take a few seconds even at
# the cap, so they run within the request, like a search.

# Record keys seq_analysis.py reads, and the column each comes from
ANALYSIS_COLUMNS = [
    ('processid', 'col_processid'), ('sampleid', 'col_sampleid'),
    ('bin', 'col_bin_uri'), ('species', 'col_species'), ('genus', 'col_genus'),
    ('family', 'col_family'), ('order', 'col_order'), ('class', 'col_class'),
    ('phylum', 'col_phylum'), ('kingdom', 'col_kingdom'),
    ('country', 'col_country_ocean'), ('province', 'col_province_state'),
    ('region', 'col_region'), ('coord', 'col_coord'), ('marker', 'col_marker_code'),
    ('seq', 'col_nuc'),
]
_ANALYSIS_KEYS = {key for key, _ in ANALYSIS_COLUMNS}

# Rows of a search read to tell which markers it holds. Past this many the
# counts are a sample, and the page says so.
ANALYSIS_MARKER_SCAN = 200_000


def _analysis_search_source(data):
    """FROM and WHERE, with its parameters, of the search a request describes:
    full-text or by fields (as the FASTA export reads it)."""
    if data.get('search_type') == 'fts':
        raw_query = _strip_accents((data.get('query') or '').strip())
        query = escape_fts_query(raw_query) if raw_query else ''
        if not query:
            raise ValueError(t('no_search_term'))
        if not fts_table_available():
            raise ValueError(t('fts_unavailable_short'))
        return fts_source(raw_query, 'r', bool(data.get('case_sensitive')))

    conditions = data.get('conditions') or []
    if not conditions:
        raise ValueError(t('no_search_conditions'))
    technical_columns, _ = get_column_names()
    where_clause, params = build_where_clause(conditions, technical_columns)
    # In parentheses: the conditions may be joined by OR at the top level
    return f'{MAIN_TABLE_NAME} r', f'({where_clause})', list(params)


def _analysis_search_markers(conn, data):
    """[(marker, records with a sequence)] of a search, and whether the counts
    come from a sample of it."""
    from_sql, where_sql, params = _analysis_search_source(data)
    rows = conn.execute(f"""
        SELECT m, COUNT(*) FROM (
            SELECT COALESCE(r.col_marker_code, '') AS m FROM {from_sql}
            WHERE {where_sql} AND r.col_nuc IS NOT NULL AND r.col_nuc != ''
            LIMIT ?)
        GROUP BY m COLLATE NOCASE ORDER BY 2 DESC
    """, (*params, ANALYSIS_MARKER_SCAN)).fetchall()
    markers = [(m or '', n) for m, n in rows]
    return markers, sum(n for _, n in markers) >= ANALYSIS_MARKER_SCAN


def _analysis_search_records(conn, data, marker, cap):
    """Records of a search with a sequence of `marker`, up to `cap` plus one
    (so the caller can tell the search went past it)."""
    from_sql, where_sql, params = _analysis_search_source(data)
    technical_columns, _ = get_column_names()
    select = ', '.join(
        f'r.{col}' if col in technical_columns else 'NULL'
        for _, col in ANALYSIS_COLUMNS)
    marker_sql, marker_params = ("(r.col_marker_code IS NULL OR r.col_marker_code = '')", []) \
        if not marker else ('r.col_marker_code = ? COLLATE NOCASE', [marker])
    cursor = conn.execute(f"""
        SELECT {select} FROM {from_sql}
        WHERE {where_sql} AND {marker_sql}
          AND r.col_nuc IS NOT NULL AND r.col_nuc != ''
        LIMIT ?
    """, (*params, *marker_params, cap + 1))
    keys = [key for key, _ in ANALYSIS_COLUMNS]
    return [dict(zip(keys, row)) for row in cursor]


def _analysis_client_records(raw):
    """The checked records the page sent, keeping only the known keys."""
    if not isinstance(raw, list):
        raise ValueError(t('analysis_invalid_request'))
    records = []
    for item in raw:
        if isinstance(item, dict):
            records.append({k: str(v) for k, v in item.items()
                            if k in _ANALYSIS_KEYS and v not in (None, '')})
    return records


class _AnalysisReply(Exception):
    """A reply that ends an analysis request early: an error, or the markers
    found when the input holds several."""

    def __init__(self, body, status=200):
        super().__init__()
        self.body, self.status = body, status


def _analysis_error(key, status=400, **kwargs):
    return _AnalysisReply({'error': t(key, **kwargs)}, status)


def _analysis_genetic_code(data):
    """The genetic code the request forces, or None for the automatic one."""
    code = data.get('genetic_code', 'auto')
    if code in (None, '', 'auto'):
        return None
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None
    if code not in seq_analysis.GENETIC_CODES:
        raise _analysis_error('analysis_invalid_request')
    return code


def _analysis_input(data, cap=seq_analysis.MAX_SEQUENCES):
    """The sequences a request asks to analyse, all of one marker.

    Returns (records, marker, truncated, total): `truncated` when the input
    went past `cap` sequences, `total` the records of the marker before the
    cap (None for a search, which isn't counted).
    Without a marker and with several in the input, raises a reply listing
    them ({needs_marker: true, markers: [{marker, n}]}) so the page can ask
    which one. Records of other markers are left out.
    """
    source = data.get('source')
    marker = data.get('marker')
    marker = marker.strip() if isinstance(marker, str) else None

    if source == 'search':
        problem = db_problem()
        if problem:
            raise _analysis_error(problem, 503)
        search = data.get('search') or {}
        with get_export_connection() as conn:
            if marker is None:
                markers, approximate = _analysis_search_markers(conn, search)
                if not markers:
                    raise _analysis_error('analysis_no_sequences')
                if len(markers) > 1:
                    raise _AnalysisReply({
                        'needs_marker': True, 'approximate': approximate,
                        'markers': [{'marker': m, 'n': n} for m, n in markers]})
                marker = markers[0][0]
            records = _analysis_search_records(conn, search, marker, cap)
        total = None
    else:
        if source == 'fasta':
            text = data.get('fasta')
            if not isinstance(text, str):
                raise _analysis_error('analysis_invalid_request')
            if len(text) > seq_analysis.MAX_FASTA_CHARS:
                raise _analysis_error('analysis_fasta_too_large',
                                      mb=seq_analysis.MAX_FASTA_CHARS // 1_000_000)
            records = seq_analysis.parse_fasta(text, seq_analysis.clean_fasta_map(data.get('fasta_map')))
        elif source == 'records':
            records = _analysis_client_records(data.get('records'))
        else:
            raise _analysis_error('analysis_invalid_request')
        if not records:
            raise _analysis_error('analysis_no_sequences')

        markers = seq_analysis.marker_counts(records)
        labelled = [(m, n) for m, n in markers if m]
        if marker is None:
            # A FASTA without markers in its headers: only the user knows
            # which gene it is
            if len(labelled) != 1 or len(markers) > 1:
                raise _AnalysisReply({
                    'needs_marker': True, 'approximate': False,
                    'markers': [{'marker': m, 'n': n} for m, n in markers]})
            marker = labelled[0][0]
        # Records labelled with another marker are left out; unlabelled
        # ones (a plain FASTA) take the marker the user chose
        records = [r for r in records
                   if not r.get('marker') or seq_analysis.same_marker(r['marker'], marker)]
        total = len(records)
        if not records:
            raise _analysis_error('analysis_no_sequences')

    truncated = len(records) > cap
    return records[:cap], marker, truncated, total


@app.route('/api/analysis/basic', methods=['POST'])
def analysis_basic():
    """Composition, translation, haplotypes and species/BIN concordance.

    Body: {source: 'records'|'search'|'fasta', records | search | fasta,
    marker (optional), genetic_code ('auto' or a table number)}.
    """
    data = request.get_json(silent=True) or {}
    start = time.time()
    try:
        forced_code = _analysis_genetic_code(data)
        records, marker, truncated, total = _analysis_input(data)
        fasta_checks = None
        if data.get('source') == 'fasta':
            # Read before run_basic overwrites each record's marker
            ranks = seq_analysis.resolve_taxon_names(records, _analysis_taxon_ranks)
            fasta_checks = (seq_analysis.fasta_preview(records, ranks), seq_analysis.rank_counts(records, ranks))
        result = seq_analysis.run_basic(records, marker, forced_code, total,
                                        bin_lookup=_analysis_bin_species)
        # What the input brings, so the page can say what can't be done
        result['fields'] = seq_analysis.field_counts(records)
        if fasta_checks:
            result['fasta_preview'], result['fasta_ranks'] = fasta_checks
        result['source'] = data.get('source')
    except _AnalysisReply as reply:
        return jsonify(reply.body), reply.status
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except sqlite3.Error as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500

    result.update(truncated=truncated, limit=seq_analysis.MAX_SEQUENCES,
                  seconds=round(time.time() - start, 2))
    print(f"Analysis OK — {data.get('source')} | {marker or '?'} | "
          f"{result['summary']['analysed']:,} seqs | {result['seconds']}s")
    return jsonify(result)


# Levels a FASTA's taxon name is checked against, lowest first
_TAXON_RANK_COLUMNS = (('genus', 'col_genus'), ('family', 'col_family'), ('order', 'col_order'),
                       ('class', 'col_class'), ('phylum', 'col_phylum'))


def _analysis_taxon_ranks(names):
    """{name: rank} for the names the database knows at some level (genus
    up to phylum), to tell a genus from a class in a FASTA header. A name
    takes milliseconds on an indexed column; a column without an index (an old database) is skipped rather than scanned."""
    out = {}
    if not names:
        return out
    try:
        indexed = get_indexed_columns()
        with get_export_connection() as conn:
            for name in list(names)[:200]:
                for rank, col in _TAXON_RANK_COLUMNS:
                    if col not in indexed:
                        continue
                    if conn.execute(f'SELECT 1 FROM {MAIN_TABLE_NAME} WHERE {col} = ? LIMIT 1',
                                    (name,)).fetchone():
                        out[name] = rank
                        break
    except sqlite3.Error:
        pass
    return out


def _analysis_bin_species(bins):
    """The species the whole database files under each BIN (the five
    commonest, and how many records name one), for the species & BINs
    reading. BINs are indexed, so each takes milliseconds."""
    out = {}
    if db_problem():
        return out
    try:
        with get_export_connection() as conn:
            for b in bins:
                rows = conn.execute(
                    "SELECT col_species, COUNT(*) FROM bold_records WHERE col_bin_uri = ? "
                    "AND col_species IS NOT NULL AND col_species != '' "
                    "GROUP BY col_species ORDER BY 2 DESC", (b,)).fetchall()
                if rows:
                    out[b] = {'species': [[r[0], r[1]] for r in rows[:5]],
                              'total': sum(r[1] for r in rows)}
    except sqlite3.Error:
        pass
    return out


# Per-species subsample sizes the distance analysis accepts
ANALYSIS_PER_SPECIES = (1, 2, 3, 5, 10, 20, 50)

# Runs of the distance analysis whose ZIP can still be downloaded: the
# compressed ZIP in memory, nothing on disk (the full distance matrix of
# 2,000 sequences alone takes several MB, hence only the latest few)
ANALYSIS_EXPORTS_KEEP = 5
analysis_exports = {}


@app.route('/api/analysis/distance', methods=['POST'])
def analysis_distance():
    """Alignment, distances (K2P, Jukes-Cantor or p), barcode gap and NJ tree.

    Body: as /api/analysis/basic, plus model ('k2p'|'jc'|'p'), per_species
    (keep at most that many sequences per species; null for all), tree
    (bool) and delimit (bool: species delimitation with ASAP and ABGD). The alignment, matrix, tree and tables go into a ZIP, downloaded
    from /api/analysis/download/<id>.
    """
    data = request.get_json(silent=True) or {}
    model = data.get('model', 'k2p')
    per_species = data.get('per_species')
    if model not in ('k2p', 'jc', 'p') or (per_species is not None
                                     and per_species not in ANALYSIS_PER_SPECIES):
        return jsonify({'error': t('analysis_invalid_request')}), 400

    start = time.time()
    try:
        forced_code = _analysis_genetic_code(data)
        records, marker, truncated, _ = _analysis_input(
            data, seq_distance.MAX_INPUT_SEQUENCES)
        if truncated:
            # A distance analysis of an arbitrary part of the input would
            # mislead: the search has to be narrowed
            raise _analysis_error('analysis_distance_input_too_big',
                                  limit=f'{seq_distance.MAX_INPUT_SEQUENCES:,}')
        result, files = seq_distance.run_distance(
            records, marker, forced_code, model, per_species, bool(data.get('tree', True)),
            bool(data.get('delimit', False)))
    except _AnalysisReply as reply:
        return jsonify(reply.body), reply.status
    except seq_distance.AnalysisLimit as e:
        key = 'analysis_too_few' if e.kind == 'too_few' else 'analysis_too_many'
        message = t(key, n=f'{e.n:,}', limit=f'{e.limit:,}')
        if e.kind == 'too_few' and e.stops > e.n:
            message += t('analysis_too_few_stops', k=f'{e.stops:,}')
        return jsonify({'error': message}), 400
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except sqlite3.Error as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500

    export_id = uuid.uuid4().hex
    safe_marker = re.sub(r'[^\w.-]+', '_', marker or 'seq')
    filename = f"bold_analysis_{safe_marker}_{datetime.now():%Y%m%d_%H%M%S}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    analysis_exports[export_id] = (filename, buf.getvalue())
    while len(analysis_exports) > ANALYSIS_EXPORTS_KEEP:
        analysis_exports.pop(next(iter(analysis_exports)))

    notes = []
    for group, kind, n, limit in result['tree_notes']:
        key = 'analysis_tree_too_few' if kind == 'too_few' else 'analysis_tree_too_many'
        note = t(key, n=f'{n:,}', limit=f'{limit:,}')
        if len(result['groups']) > 1:
            note = t('analysis_tree_group', k=group + 1) + note
        notes.append(note)
    result['tree_notes'] = notes
    notes = []
    for group, kind, n, limit in result['delimit_notes']:
        key = 'analysis_delimit_too_few' if kind == 'too_few' else 'analysis_delimit_too_many'
        note = t(key, n=f'{n:,}', limit=f'{limit:,}')
        if len(result['groups']) > 1:
            note = t('analysis_tree_group', k=group + 1) + note
        notes.append(note)
    result['delimit_notes'] = notes
    result.update(download_id=export_id, files=sorted(files),
                  seconds=round(time.time() - start, 2))
    print(f"Distance analysis OK — {data.get('source')} | {marker or '?'} | "
          f"{result['analysed']:,} seqs | {len(result['groups'])} group(s) | "
          f"trees: {len(result['trees'])} | delimitations: {len(result['delimit'])} | "
          f"{result['seconds']}s")
    return jsonify(result)


def _network_bin_species(guide):
    """The species each lineage's BIN holds in the whole database (the two
    commonest), so the network's reading can tell a far sequence filed in
    another species' BIN. The BIN of every isolated sequence and the
    commonest BIN of every other lineage."""
    bins = []
    for x in guide['lineages']:
        for b, _ in (x['bins'] if x['ids'] else x['bins'][:1]):
            if b not in bins:
                bins.append(b)
    out = {}
    try:
        with get_export_connection() as conn:
            for b in bins[:seq_network.GUIDE_MAX_LINEAGES]:
                rows = conn.execute(
                    "SELECT col_species, COUNT(*) FROM bold_records WHERE col_bin_uri = ? "
                    "AND col_species IS NOT NULL AND col_species != '' "
                    "GROUP BY col_species ORDER BY 2 DESC LIMIT 8", (b,)).fetchall()
                # Whose the BIN is: provisional names ("Heliconius
                # petiveranaDHJ01", "sp.") don't say
                rows = [r for r in rows if not seq_analysis._provisional(r[0])][:2]
                if rows:
                    out[b] = [[r[0], r[1]] for r in rows]
    except sqlite3.Error:
        pass
    guide['bin_species'] = out


@app.route('/api/analysis/popgen', methods=['POST'])
def analysis_popgen():
    """Diversity of each species (pi, segregating sites, Tajima's D) and its
    structure between countries (PhiST, Mantel), for the Haplotypes tab.

    Body: as /api/analysis/basic. The page asks for it on its own once the
    basic results are shown.
    """
    data = request.get_json(silent=True) or {}
    start = time.time()
    try:
        forced_code = _analysis_genetic_code(data)
        records, marker, _, _ = _analysis_input(data)
        result = seq_popgen.run_popgen(records, marker, forced_code)
    except _AnalysisReply as reply:
        return jsonify(reply.body), reply.status
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except sqlite3.Error as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500

    result['seconds'] = round(time.time() - start, 2)
    print(f"Diversity OK — {data.get('source')} | {marker or '?'} | "
          f"{len(result['species']):,} species | {len(result['structure']):,} structured | {result['seconds']}s")
    return jsonify(result)


@app.route('/api/analysis/diversity', methods=['POST'])
def analysis_diversity():
    """Richness, completeness and composition by zone, for the Diversity tab.

    Body: as /api/analysis/basic, plus unit ('bin'|'species') and zoning
    ('country'|'province'|'region'|'grid').
    """
    data = request.get_json(silent=True) or {}
    unit, zoning = data.get('unit', 'bin'), data.get('zoning', 'country')
    if unit not in seq_diversity.UNITS or zoning not in seq_diversity.ZONINGS:
        return jsonify({'error': t('analysis_invalid_request')}), 400
    start = time.time()
    try:
        records, marker, _, _ = _analysis_input(data)
        result = seq_diversity.run_diversity(records, marker, unit, zoning)
    except _AnalysisReply as reply:
        return jsonify(reply.body), reply.status
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except sqlite3.Error as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500

    result['seconds'] = round(time.time() - start, 2)
    print(f"Diversity by zone OK — {data.get('source')} | {marker or '?'} | {unit} by {zoning} | "
          f"{len(result['zones']):,} zones | {result['seconds']}s")
    return jsonify(result)


@app.route('/api/analysis/network', methods=['POST'])
def analysis_network():
    """Haplotype network (TCS or MSN) of the analysis input, or of one
    species in it.

    Body: as /api/analysis/basic, plus method ('tcs'|'msn') and species
    (a name, or null for every sequence).
    """
    data = request.get_json(silent=True) or {}
    method = data.get('method', 'tcs')
    species = data.get('species')
    if method not in ('tcs', 'msn') or (species is not None and not isinstance(species, str)):
        return jsonify({'error': t('analysis_invalid_request')}), 400

    start = time.time()
    try:
        forced_code = _analysis_genetic_code(data)
        records, marker, truncated, _ = _analysis_input(
            data, seq_distance.MAX_INPUT_SEQUENCES)
        if truncated:
            raise _analysis_error('analysis_distance_input_too_big',
                                  limit=f'{seq_distance.MAX_INPUT_SEQUENCES:,}')
        if species is not None:
            # As the haplotype table names them
            records = [r for r in records if (r.get('species') or r.get('taxon') or '') == species]
        result = seq_network.run_network(records, marker, forced_code, method)
        _network_bin_species(result['guide'])
    except _AnalysisReply as reply:
        return jsonify(reply.body), reply.status
    except seq_network.NetworkLimit as e:
        # The advice depends on what was asked: one species already, or MSN
        key = 'analysis_network_' + e.kind
        if e.kind in ('too_many', 'too_many_haplotypes'):
            key += ('_msn' if e.kind == 'too_many_haplotypes' and method == 'msn' else '') + \
                   ('_species' if species is not None else '')
        message = t(key, n=f'{e.n:,}', limit=f'{e.limit:,}')
        if e.kind == 'too_few' and e.stops > e.n:
            message += t('analysis_too_few_stops', k=f'{e.stops:,}')
        return jsonify({'error': message}), 400
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except sqlite3.Error as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500

    result.update(species=species, seconds=round(time.time() - start, 2))
    print(f"Network OK — {data.get('source')} | {marker or '?'} | {species or 'all'} | {method} | "
          f"{result['analysed']:,} seqs, {result['haplotypes']} haplotypes | {result['seconds']}s")
    return jsonify(result)


@app.route('/api/analysis/download/<export_id>')
def analysis_download(export_id):
    entry = analysis_exports.get(export_id)
    if not entry:
        return jsonify({'error': t('file_not_found')}), 404
    filename, data = entry
    return send_file(io.BytesIO(data), mimetype='application/zip', as_attachment=True,
                     download_name=filename)


# ── Sequence identification ───────────────────────────────────────────────────
#
# The "Identify sequences" tab: query sequences (a FASTA) searched against the
# database, through an index per marker built once (seq_identify.py). Both
# the index build and the identification run in the background; the page
# polls their status.
#
# Switched off for now: the index of COI-5P takes ~6 minutes and ~1 GB to
# build, one per marker, and identification runs at ~0.3-0.7 s per query,
# far from BOLD's online engine (1,000 sequences in under 5 minutes). The
# code stays for a later version; with the switch off the tab isn't drawn
# and these routes answer 404.
IDENTIFY_ENABLED = False

IDENTIFY_INDEX_DIR = os.path.join(layout.DATA_DIR, "identify_index")


@app.before_request
def _identify_switch():
    if not IDENTIFY_ENABLED and request.path.startswith('/api/identify'):
        return jsonify({'error': t('not_available')}), 404
    return None

identify_index_jobs = {}     # MARKER -> status of its index build
identify_jobs = {}           # job id -> status (and results) of an identification
_identify_lock = threading.Lock()
_identify_markers_cache = {"sig": None, "markers": None}


def _identify_markers():
    """[(marker, records with a sequence)] of the database, most common first."""
    sig = _db_signature()
    if _identify_markers_cache["sig"] == sig:
        return _identify_markers_cache["markers"]
    with get_export_connection() as conn:
        rows = conn.execute(
            "SELECT col_marker_code, COUNT(*) FROM bold_records "
            "WHERE col_marker_code IS NOT NULL AND col_marker_code != '' "
            "GROUP BY col_marker_code COLLATE NOCASE ORDER BY 2 DESC").fetchall()
    _identify_markers_cache.update(sig=sig, markers=rows)
    return rows


def _identify_index_status(marker):
    job = identify_index_jobs.get(marker.upper())
    if job and job["status"] == "running":
        return {"state": "building", "phase": job["phase"], "done": job["done"],
                "total": job["total"], "started": job["started"]}
    status = {}
    if job and job["status"] == "error":
        status["error"] = job["message"]
    meta = seq_identify.read_meta(IDENTIFY_INDEX_DIR, marker)
    if meta is None:
        status["state"] = "missing"
    elif meta.get("db") != list(_db_signature() or ()):
        status.update(state="stale", meta=meta)
    else:
        status.update(state="ready", meta=meta)
    return status


@app.route('/api/identify/markers')
def identify_markers():
    try:
        markers = _identify_markers()
    except sqlite3.Error as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500
    return jsonify({'markers': [{'marker': m, 'n': n} for m, n in markers],
                    'max_queries': seq_identify.MAX_QUERIES,
                    'thresholds': seq_identify.DEFAULT_THRESHOLDS})


@app.route('/api/identify/index/<path:marker>')
def identify_index_status(marker):
    return jsonify(_identify_index_status(marker))


def _run_index_build(marker, n_marker):
    job = identify_index_jobs[marker.upper()]

    def progress(phase, done, total):
        job.update(phase=phase, done=done, total=total)

    try:
        with get_export_connection() as conn:
            table_rows = conn.execute(f"SELECT MAX(rowid) FROM {MAIN_TABLE_NAME}").fetchone()[0] or 0
        start = time.time()
        meta = seq_identify.build_index(
            get_export_connection, marker, n_marker, table_rows, IDENTIFY_INDEX_DIR,
            _db_signature(), progress, lambda: job["cancel"])
        job.update(status="done", message="")
        print(f"Identification index OK — {marker} | {meta['entries']:,} sequences | "
              f"{time.time() - start:.0f}s")
    except seq_identify.Cancelled:
        job.update(status="cancelled", message="")
    except Exception as e:
        job.update(status="error", message=t('identify_index_failed', error=str(e)))
        import traceback
        traceback.print_exc()


@app.route('/api/identify/index', methods=['POST'])
def identify_index_build():
    data = request.get_json(silent=True) or {}
    marker = (data.get('marker') or '').strip()
    counts = {m.upper(): (m, n) for m, n in _identify_markers()}
    if marker.upper() not in counts:
        return jsonify({'error': t('analysis_invalid_request')}), 400
    marker, n_marker = counts[marker.upper()]
    with _identify_lock:
        # One build at a time: the COI-5P one takes several GB of memory
        if any(j["status"] == "running" for j in identify_index_jobs.values()):
            return jsonify({'error': t('identify_index_busy')}), 409
        identify_index_jobs[marker.upper()] = {
            "status": "running", "phase": "read", "done": 0, "total": 0,
            "started": time.time(), "cancel": False, "message": ""}
    os.makedirs(IDENTIFY_INDEX_DIR, exist_ok=True)
    start_background(_run_index_build, marker, n_marker)
    return jsonify(_identify_index_status(marker))


@app.route('/api/identify/index/cancel', methods=['POST'])
def identify_index_cancel():
    data = request.get_json(silent=True) or {}
    job = identify_index_jobs.get((data.get('marker') or '').strip().upper())
    if job and job["status"] == "running":
        job["cancel"] = True
    return jsonify({'ok': True})


def _identify_thresholds(raw):
    thresholds = dict(seq_identify.DEFAULT_THRESHOLDS)
    for level in thresholds:
        value = (raw or {}).get(level)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(t('analysis_invalid_request'))
        if not 0.5 <= value <= 1:
            raise ValueError(t('identify_bad_thresholds'))
        thresholds[level] = value
    if not thresholds["species"] >= thresholds["genus"] >= thresholds["family"]:
        raise ValueError(t('identify_bad_thresholds'))
    return thresholds


def _run_identify(job_id, index, queries, marker, thresholds, max_hits):
    job = identify_jobs[job_id]

    def progress(phase, done, total):
        job.update(done=done, total=total)

    try:
        start = time.time()
        job["results"] = seq_identify.identify(
            index, get_export_connection, queries, marker, thresholds, max_hits,
            progress, lambda: job["cancel"])
        job.update(status="done", seconds=round(time.time() - start, 1))
        print(f"Identification OK — {marker} | {len(queries):,} queries | {job['seconds']}s")
    except seq_identify.Cancelled:
        job.update(status="cancelled")
    except Exception as e:
        job.update(status="error", message=t('unexpected_error', error=str(e)))
        import traceback
        traceback.print_exc()


@app.route('/api/identify/run', methods=['POST'])
def identify_run():
    data = request.get_json(silent=True) or {}
    marker = (data.get('marker') or '').strip()
    text = data.get('fasta')
    if not marker or not isinstance(text, str):
        return jsonify({'error': t('analysis_invalid_request')}), 400
    if len(text) > seq_analysis.MAX_FASTA_CHARS:
        return jsonify({'error': t('analysis_fasta_too_large',
                                   mb=seq_analysis.MAX_FASTA_CHARS // 1_000_000)}), 400
    try:
        thresholds = _identify_thresholds(data.get('thresholds'))
        max_hits = int(data.get('max_hits') or 10)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    max_hits = min(max(max_hits, 1), 50)

    status = _identify_index_status(marker)
    if status["state"] != "ready":
        return jsonify({'error': t('identify_index_' + status["state"]), 'index': status}), 409
    queries = [{'id': r.get('id') or f'seq{i + 1}', 'seq': r['seq']}
               for i, r in enumerate(seq_analysis.parse_fasta(text))]
    if not queries:
        return jsonify({'error': t('analysis_no_sequences')}), 400
    if len(queries) > seq_identify.MAX_QUERIES:
        return jsonify({'error': t('identify_too_many', n=f'{len(queries):,}',
                                   limit=f'{seq_identify.MAX_QUERIES:,}')}), 400
    try:
        index = seq_identify.load_index(IDENTIFY_INDEX_DIR, marker)
    except OSError as e:
        return jsonify({'error': t('unexpected_error', error=str(e))}), 500

    job_id = uuid.uuid4().hex
    identify_jobs[job_id] = {"status": "running", "done": 0, "total": len(queries),
                             "cancel": False, "results": None, "message": "",
                             "marker": marker, "thresholds": thresholds}
    while len(identify_jobs) > 5:
        identify_jobs.pop(next(iter(identify_jobs)))
    start_background(_run_identify, job_id, index, queries, marker, thresholds, max_hits)
    return jsonify({'job_id': job_id, 'total': len(queries)})


@app.route('/api/identify/status/<job_id>')
def identify_status(job_id):
    job = identify_jobs.get(job_id)
    if not job:
        return jsonify({'error': t('invalid_id')}), 404
    body = {k: job.get(k) for k in ('status', 'done', 'total', 'message', 'seconds',
                                     'marker', 'thresholds')}
    if job["status"] == "done":
        body["results"] = job["results"]
    return jsonify(body)


@app.route('/api/identify/cancel/<job_id>', methods=['POST'])
def identify_cancel(job_id):
    job = identify_jobs.get(job_id)
    if job and job["status"] == "running":
        job["cancel"] = True
    return jsonify({'ok': True})


@app.route('/api/identify/download/<job_id>')
def identify_download(job_id):
    job = identify_jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({'error': t('not_available')}), 404

    def table(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(header)
        w.writerows(rows)
        return "﻿" + buf.getvalue()

    pct = lambda x: "" if x is None else f"{100 * x:.2f}"
    summary = table(
        ["query", "length", "level", "taxon", "ambiguous_between", "best_identity_pct",
         "best_overlap", "best_match", "best_bin", "hits"],
        [[r["id"], r["length"], r["level"], r["taxon"], r.get("note", ""), pct(r.get("best")),
          r.get("best_overlap") or "", r.get("best_id", ""), r.get("best_bin", ""), len(r["hits"])]
         for r in job["results"]])
    hits = table(
        ["query", "rank", "identity_pct", "overlap", "processid", "species", "genus", "family",
         "order", "class", "phylum", "bin", "country"],
        [[r["id"], i + 1, pct(h["identity"]), h["overlap"], h["processid"], h["species"] or "",
          h["genus"] or "", h["family"] or "", h["order"] or "", h["class"] or "",
          h["phylum"] or "", h["bin"] or "", h["country"] or ""]
         for r in job["results"] for i, h in enumerate(r["hits"])])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("identification_summary.csv", summary)
        zf.writestr("identification_hits.csv", hits)
    buf.seek(0)
    marker = re.sub(r'[^\w.-]+', '_', job["marker"])
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"bold_identification_{marker}_{datetime.now():%Y%m%d_%H%M%S}.zip")


SERVER_PORT = 5001
SERVER_URL  = f'http://127.0.0.1:{SERVER_PORT}'


def _port_in_use(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def _open_browser_when_ready():
    """Opens the viewer once the server actually accepts connections.

    The launchers used to open the browser themselves, before (or a fixed
    second after) starting the server: with the startup checks in between,
    the first page load often hit a server that wasn't listening yet.
    """
    import webbrowser
    for _ in range(120):          # up to ~60 s
        if _port_in_use(SERVER_PORT):
            webbrowser.open(SERVER_URL)
            return
        time.sleep(0.5)


if __name__ == '__main__':
    open_browser = '--open-browser' in sys.argv[1:]
    # A viewer already running (a second click on the shortcut or on "Open
    # viewer"): starting another one would just fail on the busy port.
    if _port_in_use(SERVER_PORT):
        print(f"✅ The viewer is already running at {SERVER_URL}")
        if open_browser:
            import webbrowser
            webbrowser.open(SERVER_URL)
        sys.exit(0)
    # On startup rather than on exit: the idle shutdown (see
    # _lifetime_watchdog) ends with os._exit, which skips atexit. After the
    # check above, so a second launch doesn't empty the running viewer's.
    clear_export_folders()
    if open_browser:
        threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    threading.Thread(target=_lifetime_watchdog, daemon=True).start()
    # No debug mode: it served Werkzeug's interactive debugger (arbitrary
    # code execution from the browser on any unhandled error) and its
    # reloader ran this whole module twice, startup checks and pip included.
    app.run(debug=False, port=SERVER_PORT, threaded=True)

