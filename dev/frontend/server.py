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
        print(f"⚠️ {package} no encontrado. Intentando instalarlo...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", package], check=True)
            print(f"✅ {package} instalado correctamente.")
        except subprocess.CalledProcessError as e:
            print(f"❌ No se pudo instalar {package}: {e}")
            sys.exit(1)

def check_and_install_pip():
    """Check whether pip is installed and install it if necessary."""
    try:
        subprocess.run([sys.executable, "-m", "pip", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        #print("✅ pip ya está instalado.")
    except subprocess.CalledProcessError:
        print("⚠️ pip no encontrado. Intentando instalar...")
        try:
            if sys.platform.startswith("linux"):
                package_manager = shutil.which("apt") or shutil.which("dnf") or shutil.which("yum")
                if package_manager:
                    subprocess.run(["sudo", package_manager, "install", "-y", "python3-pip"], check=True)
                else:
                    print("❌ No se pudo determinar el gestor de paquetes en Linux.")
                    sys.exit(1)
            elif sys.platform == "win32":
                subprocess.run([sys.executable, "-m", "ensurepip"], check=True)
                subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=True)
            else:
                print(f"❌ Sistema operativo no soportado: {sys.platform}")
                sys.exit(1)
            print("✅ pip instalado correctamente.")
        except subprocess.CalledProcessError as e:
            print(f"❌ No se pudo instalar pip: {e}")
            sys.exit(1)

# List of required packages
required_packages = ["flask", "psutil"]

# Check and install pip if necessary
check_and_install_pip()

# Check and install packages
for package in required_packages:
    install_package(package)


from flask import Flask, render_template, request, jsonify, send_file
from functools import lru_cache
import sqlite3
import os
import gc
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

__version__ = "1.1.2"

app = Flask(__name__)

def _find_project_root():
    p = os.path.dirname(os.path.abspath(__file__))
    for _ in range(3):
        if os.path.isdir(os.path.join(p, "data", "raw")) or os.path.isdir(os.path.join(p, "app")):
            return p
        p = os.path.dirname(p)
    return os.path.dirname(os.path.abspath(__file__))

_PROJECT_ROOT     = _find_project_root()
_EXPORTS_ROOT     = os.path.join(_PROJECT_ROOT, "data", "exports")
CSV_EXPORTS_DIR   = os.path.join(_EXPORTS_ROOT, 'csv_exports')
FASTA_EXPORTS_DIR = os.path.join(_EXPORTS_ROOT, 'fasta_exports')
BATCH_EXPORTS_DIR = os.path.join(_EXPORTS_ROOT, 'batch_exports')

os.makedirs(CSV_EXPORTS_DIR,   exist_ok=True)
os.makedirs(FASTA_EXPORTS_DIR, exist_ok=True)
os.makedirs(BATCH_EXPORTS_DIR, exist_ok=True)

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

# Export state (in memory)
fasta_exports  = {}
csv_exports    = {}
batch_exports  = {}
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

DATABASE_PATH = os.path.join(_PROJECT_ROOT, "app", "bold_db.db")

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
    'col_country_ocean': 'Country/Ocean',
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

# Cap on distinct values accumulated in memory per (group, column). It's not
# an accuracy limit: once exceeded, that group's counter is abandoned and the
# figure is recalculated with an exact query (see `_metric_exact`). It only
# bounds how much RAM the accumulator can use before falling back to SQLite.
METRIC_MAX_DISTINCT = 50000

# Maximum values in a list cell when the user didn't set their own limit and
# the group overflowed. Avoids rebuilding in memory what the cap had just
# avoided.
METRIC_OVERFLOW_LIST = 1000

# Name of the FTS table
FTS_TABLE_NAME = 'bold_records_fts'  # Adjust to match your actual FTS table name

def get_db_connection():
    """Optimized connection for interactive searches"""
    conn = sqlite3.connect(DATABASE_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row

    cursor = conn.cursor()
    cursor.execute('PRAGMA synchronous = OFF')
    cursor.execute('PRAGMA journal_mode = WAL')
    cursor.execute('PRAGMA cache_size = -262144')  # 256MB cache
    cursor.execute('PRAGMA temp_store = MEMORY')
    cursor.execute('PRAGMA mmap_size = 1073741824')  # 1GB memory-mapped

    return conn

@lru_cache(maxsize=1)
def get_column_names():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != ? LIMIT 1", (FTS_TABLE_NAME,))
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
def fts_table_available():
    """Indicates whether the database includes the full-text search index.

    The FTS index is optional: `bold_db_creator.py` can build the database
    without it, in which case the full-text search tab has nothing to work
    with. Resolved only once (lru_cache) because a table doesn't appear or
    disappear while the server is running.
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
        condition_query = build_single_condition(column, operator, value, parameters, value2)
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


def build_single_condition(column, operator, value, parameters, value2=None):
    """Builds a single condition"""
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
        return f"CAST({column} AS REAL) > CAST(? AS REAL)"
    elif operator == 'LESS_THAN':
        parameters.append(value)
        return f"CAST({column} AS REAL) < CAST(? AS REAL)"
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
        return f"CAST({column} AS REAL) BETWEEN CAST(? AS REAL) AND CAST(? AS REAL)"
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
        'invalid_search_conditions': 'Condiciones de búsqueda inválidas',
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
    },
    'en': {
        'no_table_found': 'No table found in the database',
        'invalid_search_conditions': 'Invalid search conditions',
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


@app.route('/')
def index():
    technical_columns, _ = get_column_names()

    # Create [technical_name, friendly_name] pairs to send to the template
    columns = []

    # First add the columns that are in FIELD_MAPPING, in their defined order
    # (FIELD_MAPPING sets the order; the friendly name is resolved separately
    # so it comes out in the active language).
    for tech_name in FIELD_MAPPING:
        if tech_name in technical_columns and tech_name not in EXCLUDED_COLUMNS:
            columns.append({
                'technical': tech_name,
                'friendly': get_friendly_name(tech_name)
            })

    # Then add any additional column that isn't in FIELD_MAPPING
    for col in technical_columns:
        if col not in FIELD_MAPPING and col not in EXCLUDED_COLUMNS:
            columns.append({
                'technical': col,
                'friendly': col  # If not in the mapping, use the technical name
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
        fts_available=fts_table_available(),
        fts_table_name=FTS_TABLE_NAME,
        initial_lang=get_lang(),
        version=__version__,
    )



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

        offset = (page - 1) * per_page
        data_query = f"SELECT * FROM {table_name} WHERE {where_clause} LIMIT ? OFFSET ?"
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



_FTS_SPECIAL = str.maketrans({
    '-': '"-"', '+': '"+"', '|': '"|"', '&': '"&"',
    '!': '"!"', '(': '"("', ')': '")"', '^': '"^"',
    '"': '""',  '*': '"*"', ':': '":"', '.': '"."'
})

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
    """
    stripped = query.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        phrase = stripped[1:-1].replace('"', '""')
        return '"{}"'.format(phrase)
    return query.translate(_FTS_SPECIAL)


_FTS_BOOLEAN_KEYWORDS = {'AND', 'OR', 'NOT'}

def extract_fts_terms(query: str):
    """Pulls the literal search terms out of a raw FTS query string, dropping
    the AND/OR/NOT keywords and grouping parentheses.

    Used for case-sensitive matching: the trigram tokenizer's MATCH is
    always case-insensitive (see escape_fts_query), so "sensitive" search is
    done by re-checking these literal terms against the actual row data
    after MATCH has already narrowed things down.
    """
    terms = []
    for m in re.finditer(r'"([^"]*)"|(\S+)', query):
        phrase, word = m.groups()
        if phrase is not None:
            if phrase:
                terms.append(phrase)
            continue
        token = word.strip('()')
        if token and token not in _FTS_BOOLEAN_KEYWORDS:
            terms.append(token)
    return terms


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

    cs_clause, cs_params = ('', [])
    if data.get('case_sensitive'):
        cs_clause, cs_params = build_case_sensitive_filter(raw_query, 'm')

    conn = get_db_connection()
    if search_id:
        with active_searches_lock:
            active_searches[search_id] = conn

    cursor = conn.cursor()
    _, main_table = get_column_names()

    skip_count = data.get('skip_count', False)

    try:
        total_count = None
        if not skip_count:
            count_query = f"""
                SELECT COUNT(*)
                FROM {main_table} m
                INNER JOIN {FTS_TABLE_NAME} f ON m.rowid = f.rowid
                WHERE f.{FTS_TABLE_NAME} MATCH ?{cs_clause}
            """
            cursor.execute(count_query, (query, *cs_params))
            total_count = cursor.fetchone()[0]

        offset = (page - 1) * per_page
        data_query = f"""
            SELECT m.*
            FROM {main_table} m
            INNER JOIN {FTS_TABLE_NAME} f ON m.rowid = f.rowid
            WHERE f.{FTS_TABLE_NAME} MATCH ?{cs_clause}
            LIMIT ? OFFSET ?
        """
        cursor.execute(data_query, (query, *cs_params, per_page, offset))
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

    except sqlite3.Error as e:
        if 'interrupted' in str(e).lower():
            return jsonify({'cancelled': True}), 200
        return jsonify({'error': str(e)}), 500
    finally:
        if search_id:
            with active_searches_lock:
                active_searches.pop(search_id, None)
        conn.close()
        



AUTOCOMPLETE_COLUMNS = [
    'col_marker_code', 'col_species', 'col_genus', 'col_family',
    'col_order', 'col_class', 'col_phylum', 'col_country_ocean',
    'col_province_state', 'col_region', 'col_identification'
]

@app.route('/api/autocomplete')
def autocomplete():
    query = request.args.get("query", "").strip()
    if not query:
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

            sql_query = f"""
                SELECT {', '.join(columns)}
                FROM {FTS_TABLE_NAME}
                WHERE {' OR '.join([f'{col} LIKE ?' for col in columns])}
                LIMIT 50
            """
            params = [f"%{query}%"] * len(columns)
            cursor.execute(sql_query, params)
            results = cursor.fetchall()

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
            results = cursor.fetchall()

        seen_values = set()
        formatted_results = []
        for row in results:
            for col, value in zip(columns, row):
                if value and value not in seen_values:
                    if query.lower() in str(value).lower():
                        seen_values.add(value)
                        formatted_results.append({
                            "field": get_friendly_name(col),
                            "value": value
                        })

        formatted_results.sort(key=lambda x: len(x['value']))
        return jsonify({"results": formatted_results[:10]})

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
            'total_rows': 0,
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

    # Calculate progress
    progress = 0
    if status['total_rows'] > 0:
        progress = min(100, int((status['rows_processed'] / status['total_rows']) * 100))

    return jsonify({
        'status': status['status'],
        'rows_processed': status['rows_processed'],
        'total_rows': status['total_rows'],
        'progress': progress,
        'message': status['message'],
        'download_url': f'/api/download_fasta/{export_id}' if status['filename'] else None
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

    if request_data.get('search_type') == 'fts':
        raw_query = request_data.get('query', '').strip()
        if not raw_query:
            raise ValueError(t('no_search_term'))
        if not fts_table_available():
            raise ValueError(t('fts_unavailable_short'))

        # Accent-stripped like search_fts() does — see the comment there.
        raw_query = _strip_accents(raw_query)

        query = escape_fts_query(raw_query)

        cs_clause, cs_params = ('', [])
        if request_data.get('case_sensitive'):
            cs_clause, cs_params = build_case_sensitive_filter(raw_query, 'r')

        data_query = f"""
            SELECT r.col_processid, r.col_sampleid, r.col_bin_uri,
                   r.col_species, r.col_genus, r.col_family, r.col_order,
                   r.col_class, r.col_phylum, r.col_kingdom,
                   r.col_marker_code, r.col_nuc
            FROM bold_records r
            INNER JOIN bold_records_fts fts ON r.rowid = fts.rowid
            WHERE fts.bold_records_fts MATCH ?
            AND r.col_nuc IS NOT NULL AND r.col_nuc != ''{cs_clause}
        """

        return data_query, (query, *cs_params)

    else:
        conditions = request_data.get('conditions', [])
        if not conditions:
            raise ValueError(t('no_search_conditions'))

        technical_columns, _ = get_column_names()
        where_clause, params = build_where_clause(conditions, technical_columns)

        if where_clause:
            where_clause += " AND (col_nuc IS NOT NULL AND col_nuc != '')"
        else:
            where_clause = "(col_nuc IS NOT NULL AND col_nuc != '')"

        data_query = f"""
            SELECT col_processid, col_sampleid, col_bin_uri,
                   col_species, col_genus, col_family, col_order,
                   col_class, col_phylum, col_kingdom,
                   col_marker_code, col_nuc
            FROM bold_records
            WHERE {where_clause}
        """

        return data_query, params
        
        

@contextmanager
def get_export_connection():
    """Context manager for export connections optimized for bulk reads"""
    conn = sqlite3.connect(DATABASE_PATH, timeout=300.0)
    conn.row_factory = sqlite3.Row

    cursor = conn.cursor()
    cursor.execute('PRAGMA synchronous = OFF')
    cursor.execute('PRAGMA journal_mode = WAL')
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
        fasta_exports[export_id]['filename'] = filename

        data_query, params = build_optimized_fasta_query(request_data)

        with get_export_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(data_query, params)

            rows_processed = 0
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
                    fasta_entries = []

                    for row in batch:
                        sequence = row[11] or ''
                        if not sequence:
                            continue

                        sequence = sequence.replace('-', '').strip('Nn')
                        if len(sequence) < 50:
                            continue

                        # Most specific taxon available: Species > Genus > Family > Order > Class > Phylum > Kingdom
                        taxon = get_best_taxon(row[3], row[4], row[5], row[6], row[7], row[8], row[9])

                        header_parts = []
                        if row[0]: header_parts.append(str(row[0]))
                        if row[1]: header_parts.append(str(row[1]).replace(' ', '_'))
                        if row[2]: header_parts.append(str(row[2]))
                        if taxon: header_parts.append(taxon)
                        if row[10]: header_parts.append(str(row[10]))
                        fasta_entries.append(f">{'|'.join(header_parts)}\n{sequence}\n")

                    if fasta_entries:
                        fasta_file.write(''.join(fasta_entries))
                        rows_processed += len(fasta_entries)

                    if rows_processed - last_update >= UPDATE_INTERVAL:
                        elapsed = (datetime.now() - fasta_exports[export_id]['start_time']).total_seconds()
                        rate = int(rows_processed / elapsed) if elapsed > 0 else 0
                        fasta_exports[export_id].update({
                            'rows_processed': rows_processed,
                            'message': t('fasta_progress', count=f'{rows_processed:,}', rate=f'{rate:,}'),
                            'last_update': datetime.now()
                        })
                        last_update = rows_processed

                    batch = cursor.fetchmany(FASTA_BATCH_SIZE)

        if fasta_exports[export_id].get('status') == 'cancelled':
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
            return

        duration = datetime.now() - fasta_exports[export_id]['start_time']
        duration_str = f"{int(duration.total_seconds() // 60)}m {int(duration.total_seconds() % 60)}s"
        fasta_exports[export_id].update({
            'status': 'completed',
            'rows_processed': rows_processed,
            'message': t('fasta_completed', count=f'{rows_processed:,}', duration=duration_str),
            'last_update': datetime.now()
        })
        _prune_export_dir(FASTA_EXPORTS_DIR)
        print(f"FASTA Export OK — {export_id} | {rows_processed:,} seqs | {batch_count} lotes | {duration_str}")

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
                tech_order = [REVERSE_MAPPING.get(fn) for fn in export_columns]
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
    if include_nuc:
        col_select = '*'
        col_select_join = 'r.*'
    else:
        technical_columns, _ = get_column_names()
        cols = [c for c in technical_columns if c != 'col_nuc']
        col_select = ', '.join(f'"{c}"' for c in cols)
        col_select_join = ', '.join(f'r."{c}"' for c in cols)

    if data.get('search_type') == 'fts':
        raw_query = data.get('query', '').strip()
        if not raw_query:
            raise ValueError(t('no_search_term'))
        if not fts_table_available():
            raise ValueError(t('fts_unavailable_short'))

        # Accent-stripped like search_fts() does — see the comment there.
        raw_query = _strip_accents(raw_query)
        query = escape_fts_query(raw_query)

        cs_clause, cs_params = ('', [])
        if data.get('case_sensitive'):
            cs_clause, cs_params = build_case_sensitive_filter(raw_query, 'r')

        data_query = f"""
            SELECT {col_select_join} FROM bold_records r
            INNER JOIN bold_records_fts fts ON r.rowid = fts.rowid
            WHERE fts.bold_records_fts MATCH ?{cs_clause}
        """
        parameters = (query, *cs_params)

    else:
        conditions = data.get('conditions', [])
        if not conditions:
            raise ValueError(t('no_search_conditions'))

        technical_columns, _ = get_column_names()
        where_clause, parameters = build_where_clause(conditions, technical_columns)
        data_query = f"SELECT {col_select} FROM bold_records WHERE {where_clause}"

    return data_query, parameters



@app.route('/api/export_status/<export_id>', methods=['GET'])
def export_status(export_id):
    if export_id not in csv_exports:
        return jsonify({'error': t('invalid_export_id')}), 404

    status = csv_exports[export_id]

    return jsonify({
        'status': status['status'],
        'rows_processed': status['rows_processed'],
        'progress': 0,
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
    query = data.get('query', '').strip()

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
        

# Function to get the total record count
def get_total_records():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM bold_records")
    total = cursor.fetchone()[0]
    conn.close()
    return total

@app.route('/total_records', methods=['GET'])
def total_records():
    return jsonify({"total": get_total_records()})
    

def clear_export_folders():
    """On server startup, keeps only the EXPORT_KEEP most recent files per
    export folder (rolling retention), instead of deleting them entirely."""
    folders = [CSV_EXPORTS_DIR, FASTA_EXPORTS_DIR, BATCH_EXPORTS_DIR]
    for folder in folders:
        _prune_export_dir(folder)

# Run on server startup
clear_export_folders()


def _report_fts_availability():
    """Logs to the console whether the FTS index is available.

    Checked at startup so the index's absence shows up here rather than as an
    error when clicking "Search": the full-text tab is already served
    disabled. A failure opening the database doesn't stop startup — the rest
    of the app reports that problem on its own.
    """
    try:
        if fts_table_available():
            print(f"✅ Índice FTS disponible ({FTS_TABLE_NAME}).")
        else:
            print(f"⚠️ Sin índice FTS ({FTS_TABLE_NAME}): la búsqueda de texto "
                  "completo se mostrará deshabilitada.")
    except Exception as e:
        print(f"⚠️ No se pudo comprobar el índice FTS: {e}")

_report_fts_availability()

@app.route('/list-static-files')
def list_static_files():
    folder = os.path.join(os.path.dirname(__file__), 'static')
    files = [f"static/{f}" for f in os.listdir(folder) if f.endswith(".tsv")]
    return jsonify(files)


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Shut down the server and terminal window when the browser tab is closed"""
    print("✅ Shutdown request received")
    shutdown_server = request.environ.get("werkzeug.server.shutdown")
    if shutdown_server:
        print("✅ Shutdown signal processed successfully")
        shutdown_server()
        return "Server stopped."
    else:
        print("⚠️ Error: shutdown method not found")
        # For more recent versions of Flask/Werkzeug
        try:
            import os, signal
            print("⏳ Attempting to terminate process...")
            os.kill(os.getpid(), signal.SIGINT)
            return "Server stopped with SIGINT."
        except Exception:
            return "⚠ Error: could not stop the server."

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
    table and on the bold_records columns.
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
        for variant in (canonical, cleaned):
            vkey = (key, variant.lower())
            if vkey in seen_variants:
                continue
            seen_variants.add(vkey)
            variants.append((variant, canonical_by_key[key]))
    return sorted(canonical_by_key.values()), variants

def _safe_name(s, max_len=80):
    return re.sub(r'[^\w\s-]', '', str(s)).replace(' ', '_').strip('_')[:max_len] or 'unknown'


_FASTA_HEADER_COLS = [
    ('col_processid',   False),
    ('col_sampleid',    True),
    ('col_bin_uri',     False),
    ('col_species',     True),
    ('col_marker_code', False),
]

def _row_to_fasta_entry(row, keys):
    """Converts an sqlite3.Row to a FASTA entry, or '' if not applicable."""
    seq = row['col_nuc'] or ''
    seq = seq.replace('-', '').strip('Nn')
    if len(seq) < 50:
        return ''
    parts = []
    for col, underscore in _FASTA_HEADER_COLS:
        val = row[col] if col in keys else ''
        if val:
            val = str(val)
            parts.append(val.replace(' ', '_') if underscore else val)
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
    conn = sqlite3.connect(DATABASE_PATH, timeout=300.0)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode = WAL')
    cursor.execute('PRAGMA cache_size = -262144')   # 256MB
    cursor.execute('PRAGMA temp_store = FILE')
    cursor.execute('PRAGMA mmap_size = 2147483648')  # 2GB
    return conn


def _batch_check_cancel(export_id):
    if batch_exports.get(export_id, {}).get('cancel'):
        raise _BatchCancelled()


def _batch_match_sql(col1, is_taxonomy):
    """SQL that populates the temporary table `_m` with (rowid, term).

    For taxonomy, a UNION ALL with one branch per level is emitted instead of
    an OR inside the JOIN: the OR forces SQLite to scan bold_records entirely
    (SCAN r × SCAN v1), whereas each UNION ALL branch is resolved using the
    corresponding level's index.

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

    return 'INSERT INTO _m (rid, term)\n' + '\nUNION ALL\n'.join(selects)


def _batch_apply_field2(cursor, col2):
    """Restricts `_m` to records that also match field 2.

    Resolved using `col2`'s index (if it exists) over a temporary table of
    rowids, without touching bold_records again.
    """
    cursor.execute('CREATE TEMP TABLE _f2 (rid INTEGER PRIMARY KEY, val TEXT)')
    cursor.execute(
        f'INSERT OR IGNORE INTO _f2 (rid, val) '
        f'SELECT r.rowid, v2.term FROM bold_records r '
        f'INNER JOIN _bv2 v2 ON r."{col2}" = v2.val'
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
        text = f'{text}; … y {rest:,} más' if text else f'… y {rest:,} más'
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
    spool_path = None
    try:
        technical_columns, _ = get_column_names()

        col1        = fields_data[0]['technical']
        raw_vals1   = fields_data[0].get('values', [])
        is_taxonomy = col1 in _TAXONOMY_COLS

        values1, variants1 = _batch_terms(raw_vals1, is_taxonomy)

        has_field2 = len(fields_data) > 1 and fields_data[1].get('values')
        col2, values2, variants2 = None, [], []
        if has_field2:
            col2 = fields_data[1]['technical']
            values2, variants2 = _batch_terms(
                fields_data[1]['values'], col2 in _TAXONOMY_COLS
            )

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
            'message':  t('searching_terms', count=f'{len(values1):,}'),
            'progress': 5,
        })

        timestamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
        zip_filename = f'batch_{timestamp}.zip'
        zip_path     = os.path.join(BATCH_EXPORTS_DIR, zip_filename)
        spool_path   = os.path.join(BATCH_EXPORTS_DIR, f'.all_{export_id}.tsv')

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
        _batch_check_cancel(export_id)
        cursor.execute(_batch_match_sql(col1, is_taxonomy))

        _batch_check_cancel(export_id)
        if has_field2:
            batch_exports[export_id].update({
                'message':  t('filtering_by', field=get_friendly_name(col2)),
                'progress': 12,
            })
            _batch_apply_field2(cursor, col2)
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

        total_rows = sum(counts.values())

        # No ZIP is generated when there are no matches: its three files would
        # be a not_found.txt with the full input list, a summary.csv with
        # everything at zero, and an all_results.tsv with only the header.
        # The search finishes successfully (it's not an error), but without a
        # `filename`, so the frontend doesn't offer a download.
        if total_rows == 0:
            batch_exports[export_id].update({
                'status':     'completed',
                'progress':   100,
                'total_rows': 0,
                'message':    t('no_records_for_terms', count=f'{len(values1):,}'),
            })
            return

        batch_exports[export_id].update({
            'total_rows': total_rows,
            'message':    t('records_found_packing', count=f'{total_rows:,}'),
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

        def stream_group_tsv(zf, arcname, params, spool_writer):
            """Dumps a group to `arcname` (and to the all_results spool) without
            materializing the rows in memory.

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
                        spool_writer.writerow(rec)
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
            wrote = False
            # No BOM: FASTA files are consumed by bioinformatics tools, which
            # would interpret the BOM as part of the first header.
            with io.TextIOWrapper(zf.open(arcname, 'w', force_zip64=True),
                                  encoding='utf-8', newline='') as fh:
                rows = first
                while rows:
                    chunk = ''.join(_row_to_fasta_entry(r, keys) for r in rows)
                    if chunk:
                        fh.write(chunk)
                        wrote = True
                    rows = gcur.fetchmany(2000)
            gcur.close()
            if not wrote:
                # Empty entry: it's left in place regardless (ZIP doesn't allow
                # deleting entries), but the case is marginal (all sequences < 50 bp).
                pass

        with open(spool_path, 'w', encoding='utf-8-sig', newline='', buffering=BUFFER_SIZE) as spool_fh:
            spool_writer = csv.writer(spool_fh, delimiter='\t')
            spool_writer.writerow(friendly_cols)

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                if not has_field2:
                    found_terms = sorted(counts)
                    found_lower = {t.lower() for t in found_terms}
                    not_found   = [v for v in values1 if v.lower() not in found_lower]

                    for i, term in enumerate(found_terms):
                        _batch_check_cancel(export_id)
                        sn = _safe_name(term)
                        stream_group_tsv(zf, f'individual/{sn}.tsv', (term,), spool_writer)
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
                    w.writerow([get_friendly_name(col1), 'Registros'] + metric_head)
                    for term in found_terms:
                        w.writerow(
                            [term, counts[term]]
                            + _metric_cells((term,), metrics, tally, exact, report_opts)
                        )
                    for term in not_found:
                        w.writerow([term, 0] + _empty_metric_cells(metrics))
                    zf.writestr('summary.csv', buf.getvalue().encode('utf-8-sig'))

                else:
                    # counts: {(field2_value, term): n}
                    by_k2 = {}
                    for (k2, term), n in counts.items():
                        by_k2.setdefault('' if k2 is None else str(k2), {})[term] = (n, k2)

                    summary_rows = []
                    for i, val2 in enumerate(values2):
                        _batch_check_cancel(export_id)
                        actual_key = next((k for k in by_k2 if k.lower() == val2.lower()), None)
                        dir_name   = _safe_name(val2)
                        val2_data  = by_k2.get(actual_key, {}) if actual_key else {}

                        found_lower    = {t.lower() for t in val2_data}
                        not_found_here = [v for v in values1 if v.lower() not in found_lower]

                        for term, (n, raw_k2) in sorted(val2_data.items()):
                            sn = _safe_name(term)
                            stream_group_tsv(zf, f'{dir_name}/{sn}.tsv', (raw_k2, term), spool_writer)
                            if include_fasta:
                                stream_group_fasta(zf, f'{dir_name}/{sn}.fasta', (raw_k2, term))
                            # The group key is stored: metrics are accumulated
                            # per (field 2, term) pair, not per term.
                            summary_rows.append((term, val2, n, (raw_k2, term)))

                        if not_found_here:
                            zf.writestr(f'{dir_name}/not_found.txt', '\n'.join(not_found_here))

                        batch_exports[export_id]['progress'] = \
                            20 + int((i + 1) / max(len(values2), 1) * 75)

                    resolve_overflow()

                    # summary.csv
                    buf = io.StringIO()
                    w   = csv.writer(buf)
                    w.writerow(
                        [get_friendly_name(col1), get_friendly_name(col2), 'Registros']
                        + metric_head
                    )
                    for v1, v2, cnt, gkey in sorted(summary_rows, key=lambda r: (r[0], r[1])):
                        w.writerow(
                            [v1, v2, cnt]
                            + _metric_cells(gkey, metrics, tally, exact, report_opts)
                        )
                    zf.writestr('summary.csv', buf.getvalue().encode('utf-8-sig'))

                    # summary_by_field2.csv
                    buf = io.StringIO()
                    w   = csv.writer(buf)
                    w.writerow([
                        get_friendly_name(col2), 'Total Registros',
                        f'{get_friendly_name(col1)} Encontrados',
                        f'{get_friendly_name(col1)} No Encontrados',
                    ])
                    for val2 in values2:
                        actual_key = next((k for k in by_k2 if k.lower() == val2.lower()), None)
                        val2_data  = by_k2.get(actual_key, {}) if actual_key else {}
                        found_cnt  = len(val2_data)
                        total_cnt  = sum(n for n, _ in val2_data.values())
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
                        w.writerow([get_metric_label(bcol), 'Registros'])
                        for value, n in bcur:
                            w.writerow([value, n])
                    finally:
                        bcur.close()
                    slug = _safe_name(_strip_accents(get_metric_label(bcol))).lower()
                    zf.writestr(f'summary_by_{slug}.csv',
                                buf.getvalue().encode('utf-8-sig'))

        # all_results.tsv was accumulated on disk during the run; it's added
        # at the end because zipfile doesn't allow two open entries at once.
        batch_exports[export_id].update({'message': t('adding_all_results'), 'progress': 96})
        with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_DEFLATED) as zf:
            zf.write(spool_path, 'all_results.tsv')

        conn.close()
        conn = None
        try:
            os.remove(spool_path)
        except Exception:
            pass
        spool_path = None

        batch_exports[export_id].update({
            'status':     'completed',
            'progress':   100,
            'message':    t('records_found', count=f'{total_rows:,}'),
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
        for path in (spool_path,):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        if batch_exports.get(export_id, {}).get('status') in ('error', 'cancelled'):
            if zip_path and os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except Exception:
                    pass


@app.route('/api/batch_search', methods=['POST'])
def start_batch_search():
    try:
        data         = request.get_json()
        fields_data  = data.get('fields', [])
        include_fasta = bool(data.get('include_fasta', False))
        # Optional: without it, the summary comes out as usual (see _parse_report_spec)
        report        = data.get('report') or None

        if not fields_data or not fields_data[0].get('values'):
            return jsonify({'error': t('field_required')}), 400

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


if __name__ == '__main__':
    app.run(debug=True, port=5001, threaded=True)

