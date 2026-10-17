#!/usr/bin/env python3
"""
BOLD Database Creator - unified GUI
Steps: 1-Filter TSV  2-TSV->SQLite  3-Index  4-FTS
"""

# ---- Imports -----------------------------------------------------------------
import sys, subprocess
import os, re, sqlite3, csv, glob, tarfile, time, threading, shutil, unicodedata, webbrowser
import json, hashlib, difflib

# Raise the CSV field limit to handle BOLD's long sequences
_csv_lim = sys.maxsize
while True:
    try:
        csv.field_size_limit(_csv_lim)
        break
    except OverflowError:
        _csv_lim = _csv_lim // 10
from datetime import datetime

# ---- Auto-install dependencies -----------------------------------------------

def _ensure_deps():
    # Started by the launcher, the packages are already in place (uv). This
    # covers running it with a Python of one's own. The module is checked,
    # not the distribution: the launcher installs PySide6-Essentials, which
    # provides the same PySide6 module as the full PySide6.
    import importlib.util
    pip_pkgs = {"PySide6": "PySide6-Essentials"}
    missing = [dist for mod, dist in pip_pkgs.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return

    # Shown before the language preference can be read (t() and LANG don't
    # exist yet at this point), so the message carries both languages.
    _TITLE = "BOLD DB Creator — Instalando / Installing"
    _MSG   = (
        "Instalando dependencias, por favor espere…\n"
        "Installing dependencies, please wait…\n\n"
        "Paquetes / Packages: " + ", ".join(missing) + "\n\n"
        "La aplicación iniciará automáticamente al terminar. No cierre esta ventana.\n"
        "The application will start automatically when done. Do not close this window."
    )

    if sys.platform == "win32":
        # PySide6 isn't installed yet, so a native MessageBox is used instead
        # of a Qt dialog while pip installs it in the background.
        import ctypes
        import threading

        # MB_ICONINFORMATION | MB_TOPMOST
        threading.Thread(
            target=ctypes.windll.user32.MessageBoxW,
            args=(0, _MSG, _TITLE, 0x00040040),
            daemon=True,
        ).start()
    else:
        print(f"{_TITLE}\n{_MSG}")

    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)
    finally:
        if sys.platform == "win32":
            # Close the dialog once installation finishes (or fails)
            for _ in range(10):
                hwnd = ctypes.windll.user32.FindWindowW(None, _TITLE)
                if hwnd:
                    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                    break
                time.sleep(0.2)

_ensure_deps()

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton,
    QLineEdit, QTextEdit, QProgressBar, QFileDialog, QMessageBox, QCheckBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QScrollArea, QSplitter,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSettings, QPoint, QSize
from PySide6.QtGui import QTextCursor, QColor, QTextCharFormat, QGuiApplication, QIcon

# ---- Project root --------------------------------------------------------------
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR
ICON_HOME    = os.path.join(SCRIPT_DIR, "icons", "home.png")

# What this program writes and the viewer reads: file and table names, the
# version and the database layout version (see dev/frontend/db_contract.py)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "frontend"))
import db_contract
import layout
import shortcuts
import updater
__version__ = db_contract.APP_VERSION

# Projects from 1.2.0 and earlier kept the database, the logs and the
# settings in app/: moved into data/ before any path below is worked out.
# The App logs the outcome once it has a log.
_MIGRATION = layout.migrate_legacy_layout()

# ---- Configuration constants ----------------------------------------------------
# Absolute: data/database/bold_db.db, or data/database/bold_db.db while a database
# that was open couldn't be moved yet
DB_FILE    = layout.database_path()
# The name of the BOLD package the data comes from, for the viewer's header
_PKG_DIR   = layout.PACKAGE_DIR
TABLE_NAME = db_contract.MAIN_TABLE
CHUNKSIZE  = 50_000

# Field whose value decides whether a row makes it into the database: without a
# nucleotide sequence the record is useless for anything the viewer does.
FILTER_FIELD = "nuc"


COL_DEFS = {
    # Not a key: a specimen has one row per sequence (see run_step2)
    "processid":                    "TEXT COLLATE NOCASE",
    "sampleid":                     "TEXT COLLATE NOCASE",
    "fieldid":                      "TEXT COLLATE NOCASE",
    "museumid":                     "TEXT COLLATE NOCASE",
    "bin_uri":                      "TEXT COLLATE NOCASE",
    "kingdom":                      "TEXT COLLATE NOCASE",
    "phylum":                       "TEXT COLLATE NOCASE",
    "class":                        "TEXT COLLATE NOCASE",
    "order":                        "TEXT COLLATE NOCASE",
    "family":                       "TEXT COLLATE NOCASE",
    "genus":                        "TEXT COLLATE NOCASE",
    "species":                      "TEXT COLLATE NOCASE",
    "identification":               "TEXT COLLATE NOCASE",
    "identification_rank":          "TEXT COLLATE NOCASE",
    "country_ocean":                "TEXT COLLATE NOCASE",
    "country_iso":                  "TEXT COLLATE NOCASE",
    "province_state":               "TEXT COLLATE NOCASE",
    "region":                       "TEXT COLLATE NOCASE",
    "coord":                        "TEXT COLLATE NOCASE",
    "marker_code":                  "TEXT COLLATE NOCASE",
    "sequence_upload_date":         "TEXT COLLATE NOCASE",
    "nuc_basecount":                "INTEGER",
    "nuc":                          "TEXT COLLATE NOCASE",
}

# ---- Field selection -------------------------------------------------------------
# Which columns of the BOLD TSV end up in the database is no longer hardcoded:
# it's read from dev/fields_config.json, which the "Fields" panel of the UI
# edits. The file is the source of truth; the UI is just its editor.
#
# The selection is resolved BY NAME against the TSV's actual header, never by
# position. The previous code used fixed indices (0,1,2,3,7,13,...): if BOLD
# inserts or reorders a column in a new package version, those indices remain
# "valid" and the filtering silently pulls in data from the wrong field without
# any error. By name, a field that disappears is detected and reported.


# The 76 columns of BOLD's public package, in their download order. Only used
# to populate the UI panel when there's no TSV yet to read the real header
# from: whenever one exists, the file's header always takes precedence.
BOLD_FIELDS = [
    "processid", "sampleid", "fieldid", "museumid", "record_id", "specimenid",
    "processid_minted_date", "bin_uri", "bin_created_date", "collection_code",
    "inst", "sovereign_inst", "taxid", "kingdom", "phylum", "class", "order",
    "family", "subfamily", "tribe", "genus", "species", "subspecies",
    "species_reference", "identification", "identification_method",
    "identification_rank", "identified_by", "identifier_email", "taxonomy_notes",
    "sex", "reproduction", "life_stage", "short_note", "notes", "voucher_type",
    "tissue_type", "specimen_linkout", "associated_specimens", "associated_taxa",
    "collectors", "collection_date_start", "collection_date_end",
    "collection_event_id", "collection_time", "collection_notes", "geoid",
    "country/ocean", "country_iso", "province/state", "region", "sector", "site",
    "site_code", "coord", "coord_accuracy", "coord_source", "elev",
    "elev_accuracy", "depth", "depth_accuracy", "habitat", "realm", "biome",
    "ecoregion", "sampling_protocol", "nuc", "nuc_basecount", "insdc_acs",
    "funding_src", "marker_code", "primers_forward", "primers_reverse",
    "sequence_run_site", "sequence_upload_date", "bold_recordset_code_arr",
]

# Fields the web viewer takes for granted: used by the FASTA exporter (header
# and sequence) and by batch search. Removing them doesn't yield a smaller
# database, it yields a database the viewer fails on, so the UI shows them
# checked and locked. The rest of the fields the viewer knows about are truly
# optional: server.py discovers them via PRAGMA table_info and skips whichever
# ones aren't present.
REQUIRED_FIELDS = [
    "processid", "sampleid", "bin_uri",
    "kingdom", "phylum", "class", "order", "family", "genus", "species",
    "marker_code", "nuc",
]

# Factory selection: the 23 columns databases have been built with until now,
# in the same order.
DEFAULT_FIELDS = [
    "processid", "sampleid", "fieldid", "museumid", "bin_uri",
    "kingdom", "phylum", "class", "order", "family", "genus", "species",
    "identification", "identification_rank",
    "country/ocean", "country_iso", "province/state", "region", "coord",
    "nuc", "nuc_basecount", "marker_code", "sequence_upload_date",
]

# Factory-indexed fields. Must cover every field the viewer offers as a Batch
# search criterion: without an index, each list of terms forces SQLite to
# scan the whole of bold_records.
# processid is deliberately omitted: Step 2 always indexes it (idx_processid),
# whatever this list says.
# nuc (sequence) and coord are left out: nobody searches by their exact value
# and indexing nuc would cost several GB.
DEFAULT_INDEXED = [
    "sampleid", "fieldid", "museumid", "bin_uri",
    "kingdom", "phylum", "class", "order", "family", "genus", "species",
    "identification", "identification_rank",
    "country/ocean", "country_iso", "province/state", "region",
    "nuc_basecount", "marker_code", "sequence_upload_date",
]


# ---- Internationalization --------------------------------------------------------
#
# The language is resolved ONCE at startup (read from data/state/ui_state.ini, the
# same QSettings that stores the window geometry) and doesn't change live:
# the language selector in the sidebar only saves the preference and asks for
# a restart. This avoids having to re-translate hundreds of already-built
# widgets on the fly, in exchange for a restart to see the change.

def _load_language():
    """Reads 'language' from data/state/ui_state.ini by hand, as plain text instead of via QSettings.

    Avoids building a separate QSettings instance here: the App creates its own
    (self._settings) for the window geometry, and two QSettings instances over the
    same file compete for their own in-memory cache — one of them can end up
    rewriting the entire file without the key the other just read, clobbering
    'language' or 'window/geometry'. With a plain-text read here and QSettings
    reserved solely for self._settings, each half of the state has a single
    owner writing it.
    """
    try:
        with open(layout.UI_STATE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("language"):
                    _, _, val = line.partition("=")
                    val = val.strip()
                    if val in ("es", "en"):
                        return val
    except OSError:
        pass
    return "en"

LANG = _load_language()

STRINGS_ES = {
    'field_renamed_hint': '{name} (¿ahora se llama «{hint}»?)',
    'err_file_not_found': 'Archivo o carpeta no encontrado',
    'err_action_check_files': 'Verifica que existan los archivos de entrada y que la ruta sea correcta',
    'err_permission': 'Archivo en uso o sin permisos de escritura',
    'err_action_close_file': 'Cierra el archivo si está abierto en otro programa',
    'err_memory': 'Memoria RAM insuficiente',
    'err_action_close_apps': 'Cierra otras aplicaciones e intenta de nuevo',
    'err_db_error': 'Error en la base de datos',
    'err_action_check_db': 'Verifica que la base de datos no esté abierta en otro proceso',
    'err_encoding': 'Error de codificación en el archivo',
    'err_action_utf8': 'El archivo de entrada debe estar guardado en UTF-8',
    'err_action_check_format': 'Verifica que el archivo de entrada tenga el formato esperado',
    'err_unexpected_value': 'Valor inesperado en los datos',
    'err_action_check_values': 'Revisa el archivo de entrada por valores inválidos',
    'err_timeout': 'Tiempo de espera agotado',
    'err_action_check_process': 'Verifica que el proceso externo responde correctamente',
    'err_module_not_installed': "Módulo no instalado: '{mod}'",
    'err_column_not_found': 'Columna o campo no encontrado: {raw}',
    'err_unexpected_type': 'Error inesperado ({type})',
    'err_action_pip_install': 'Ejecuta en la terminal:  pip install {mod}',
    'err_action_check_error_log': 'Revisa el detalle técnico en error.log',
    'err_step_line': '   Paso: {context}',
    'err_detail_saved': 'Detalle técnico guardado en data/state/logs/error.log',
    'unfiltered_tsv': 'TSV sin filtrar',
    'deleted_processed': '🗑 {label} eliminado (ya procesado): {name}',
    'could_not_delete_named': '⚠️ No se pudo eliminar {name}: {error}',
    'db_busy_retrying': '⏳ Base de datos ocupada (probablemente el antivirus escaneándola) — reintentando en {delay}s ({attempt}/{attempts})...',
    'could_not_delete': '⚠️ No se pudo eliminar: {error}',
    'deleting_old_version_file': '🗑 Eliminando archivo de versión anterior: {name}',
    'fields_changed_refiltering': '♻ La selección de campos cambió — rehaciendo el filtrado',
    'fields_changed_no_targz': '⚠️ La selección de campos cambió, pero el TSV filtrado actual se hizo con la anterior y ya no está el .tar.gz para rehacerlo. Vuelve a descargarlo de BOLD si quieres aplicar los campos nuevos.',
    'filtered_tsv_exists': '✅ TSV filtrado ya existe: {name} — omitiendo Paso 1',
    'tsv_already_extracted': '📁 TSV ya extraído: {name} — omitiendo descompresión',
    'no_targz_found': '❌ No se encontró ningún archivo *.tar.gz en ../data/raw/ ni TSV extraído en ../data/processed/',
    'decompressing': '⏳ Descomprimiendo: {name} ({mb:.0f} MB)',
    'extracted_progress': '\r💽 Extraído: {gb:.2f} GB',
    'extraction_interrupted': '⚠️ Extracción interrumpida — archivos parciales eliminados',
    'decompression_finished': '✅ Descompresión finalizada ({gb:.2f} GB escritos)',
    'no_tsv_found': '❌ No se encontró archivo .tsv en ../data/processed/',
    'multiple_tsv_found': '⚠️ Múltiples .tsv encontrados — usando el más grande',
    'tsv_file_label': '📁 Archivo TSV: {name}',
    'size_mb': '💽 Tamaño: {mb:.1f} MB',
    'filtering_info': '⏳ Filtrando información...',
    'empty_tsv_file': '❌ Archivo TSV vacío',
    'rows_processed_pct': '\r  {n:,} filas procesadas ({pct:.1f}%)',
    'filtering_interrupted': '⚠️ Filtrado interrumpido — archivo parcial eliminado',
    'filtered_records': '✅ Registros filtrados: {n:,}',
    'filter_summary': '📊 Filas del paquete: {read:,} · con secuencia (se guardan): {kept:,} · '
                      'sin secuencia (se descartan): {no_seq:,}',
    'filter_only_n': '🧹 Secuencias formadas solo por N (se descartan): {n:,}',
    'filter_malformed': '⚠️ {n:,} línea(s) del paquete están rotas (les faltan columnas, '
                        'probablemente por un salto de línea dentro de un campo) y no se pudieron leer',
    'file_label': '📁 Archivo: {name}',
    'ctx_generate_tsv': 'Generar TSV',
    'no_filt_tsv_found': '❌ No se encontró archivo *.filt.tsv en ../data/processed/',
    'db_already_built': '✅ BD ya construida desde {name} — omitiendo importación',
    'total_records_expected': '📋 Total registros esperados: {n:,}',
    'records_number_not_found': '⚠️ records.number no encontrado — ejecuta el Paso 1 primero',
    'columns_detected': '📋 Columnas detectadas: {n}',
    'table_created': '✅ Tabla creada con tipos y restricciones',
    'importing_data': '⏳ Importando datos (puede tardar)...',
    'import_progress': '\r  {processed:,} / {total:,}  ({pct:.1f}%)',
    'import_progress_nototal': '\r  {processed:,} filas importadas',
    'db_created': '✅ BD creada: {name}',
    'records_inserted': '✅ Registros insertados: {n:,}',
    'import_count_mismatch': '❌ Validación fallida: se leyeron {read:,} filas, se guardaron {stored:,} '
                             '(huellas: {hashed:,}) y se esperaban {expected:,}. La BD nueva se descarta; '
                             'la actual no se ha tocado. Repite el Paso 1 por si el .filt.tsv está dañado.',
    'import_irregular_rows': '⚠️ {n:,} fila(s) del .filt.tsv no tienen {cols} columnas; se guardaron '
                             'igualmente (las que faltan, vacías). Repite el Paso 1 si no esperabas esto.',
    'creating_processid_index': '⏳ Creando el índice de ID BOLD (processid)...',
    'sequences_per_specimen': '📊 {seqs:,} secuencias de {specimens:,} especímenes: {extra:,} son '
                              'secuencias adicionales de especímenes con más de un marcador',
    'import_interrupted': '⚠️ Importación interrumpida — se borró la BD a medio crear; la actual no se ha tocado',
    'db_build_target': '💾 La BD nueva se crea en {name}: el visor sigue usando la actual hasta que '
                       'termine y la reemplace',
    'db_build_space_warn': '⚠️ Espacio libre en disco: {free}. Mientras se crea, la BD nueva convive con '
                           'la actual y necesitará unos {need}. Si se llena el disco, el paso falla '
                           'y la BD actual queda intacta.',
    'db_build_promoted': '✅ BD nueva puesta en servicio: {name} (el visor ya la usa)',
    'db_build_orphan_removed': '🧹 Borrada una BD a medio crear ({name}) de una ejecución que no terminó',
    'db_build_promote_failed': '⚠️ La BD nueva está lista en {name}, pero no se pudo reemplazar la actual '
                               '({error}). Suele ser el visor en mitad de una búsqueda o exportación: '
                               'espera a que acabe y vuelve a ejecutar cualquier paso, o reinicia el Creador.',
    'db_build_pending_title': 'BD nueva pendiente',
    'db_build_pending_body': 'Hay una base de datos nueva terminada que todavía no ha reemplazado a la '
                             'actual (el visor la tenía ocupada). Espera a que el visor termine lo que '
                             'esté haciendo, o ciérralo, y vuelve a intentarlo.',
    'ctx_tsv_to_sqlite': 'TSV → SQLite',
    'db_not_found': '❌ BD no encontrada: {path}',
    'table_not_exists': '❌ Tabla bold_records no existe',
    'recreating_index': '♻ Recreando idx_col_nuc_basecount sin COLLATE NOCASE',
    'missing_index_fields': '⚠️ Campos configurados para indexar que no están en la BD (hay que rehacer los Pasos 1 y 2): {fields}',
    'no_index_fields_exist': '❌ Ninguno de los campos configurados para indexar existe en la BD',
    'all_columns_indexed': '✅ Todas las columnas ya están indexadas',
    'creating_indexes': '⏳ Creando {n} índices (omitiendo {skipped} ya existentes)...',
    'indexing_interrupted': '⚠️ Indexación interrumpida — {n} índices creados',
    'index_created_progress': '  ✅ [{i}/{n}] {name}',
    'index_error': '  ❌ [{i}/{n}] Error en {col}: {error}',
    'indexes_created': '✅ {n} índices creados',
    'ctx_index_fields': 'Indexar campos',
    'no_fts_columns': '❌ Ninguna columna FTS encontrada en bold_records',
    'fts_already_built': '✅ FTS5 ya construida para esta BD — omitiendo reconstrucción',
    'empty_table': '⚠️ Tabla vacía',
    'creating_fts_table': '⏳ Creando tabla FTS5 ({n} columnas, excluye col_nuc)...',
    'rebuilding_fts_index': '⏳ Reconstruyendo índice FTS5 ({n:,} registros) — operación única...',
    'fts_may_take_minutes': '   Puede tardar un buen rato. La barra se mueve de lado a lado sin marcar un porcentaje porque SQLite no informa del avance: es normal, no está bloqueado.',
    'still_working': '\r   ⏳ Sigue trabajando — {mins:.0f} min transcurridos (no cierre la ventana)',
    'rebuild_finished': '   Reconstrucción terminada en {mins:.0f} min',
    'fts_stopping': '⏳ Deteniendo la reconstrucción del índice FTS (puede tardar unos segundos)...',
    'fts_interrupted': '⚠️ Reconstrucción FTS interrumpida — no se creó la tabla FTS',
    'fts_created': '✅ FTS5 creada — {n} columnas, {total:,} registros',
    'ctx_create_fts': 'Crear FTS',
    'new_fields_found': 'ℹ️ Campos nuevos en este TSV que no estaban en versiones anteriores de BOLD ({n}): {fields}',
    'can_add_from_panel': '   Puedes añadirlos a la base de datos desde el panel «Campos».',
    'missing_required_fields': '❌ Faltan campos que el visor necesita: {fields}',
    'header_mismatch_hint': '   La cabecera de este TSV no coincide con la selección configurada. Si BOLD los ha renombrado, hay que actualizar el panel «Campos» (y el visor, que espera esos nombres de columna).',
    'missing_configured_fields': '⚠️ Campos configurados que no existen en este TSV (se omiten): {fields}',
    'missing_filter_field': "❌ La cabecera del TSV no tiene el campo '{field}', necesario para descartar los registros sin secuencia.",
    'no_configured_fields_exist': '❌ Ninguno de los campos configurados existe en el TSV descargado.',
    'btn_browse': 'Examinar',
    'tip_browse_disk': 'Buscar en el disco el archivo descargado de BOLD.',
    'btn_open_folder': 'Abrir carpeta',
    'tip_open_folder': 'Abrir {dir} en el explorador.',
    'file_loaded_label': '✓  {name}{extra}',
    'size_extra': '  —  {gb:.1f} GB',
    'invalid_file_title': 'Archivo no válido',
    'invalid_file_body': 'Se esperaba un archivo {ext}.\n\nEs el archivo comprimido que descargas del portal de BOLD, sin descomprimir.',
    'file_exists_title': 'El archivo ya existe',
    'file_exists_body': 'Ya hay un archivo con ese nombre en data/raw/.\n¿Reemplazarlo?',
    'copying_to_raw': '📁 Copiando a data/raw/: {name} ({gb:.1f} GB) — puede tardar unos minutos',
    'copying_hint': 'Copiando a data/raw/…',
    'file_copied': '✅ Archivo copiado a data/raw/: {name}',
    'copy_failed': '❌ No se pudo copiar el archivo: {info}',
    'select_downloaded_file': 'Seleccionar el archivo descargado de BOLD',
    'bold_file_filter': 'Archivo BOLD comprimido (*.tar.gz);;Todos (*.*)',
    'step1_name': 'Preparar datos',
    'step2_name': 'Construir base de datos',
    'step3_name': 'Acelerar búsquedas',
    'step4_name': 'Búsqueda por texto libre',
    'step1_desc': 'Descomprime el archivo que descargaste de BOLD y se queda solo con los campos '
                  'elegidos en «Campos». Es el paso más pesado en disco.\n'
                  'Entrada: data/raw/*.tar.gz   Salida: data/processed/*.filt.tsv',
    'step2_desc': 'Carga los datos preparados en la base de datos que consulta el visor. Al terminar ya '
                  'puedes buscar registros.\n'
                  'Entrada: data/processed/*.filt.tsv   Salida: data/database/bold_db.db',
    'step3_desc': 'Crea índices sobre los campos de búsqueda. Sin ellos cada consulta recorre la tabla '
                  'entera y el visor va muy lento.\n'
                  'Entrada: data/database/bold_db.db   Salida: índices en bold_records',
    'step4_desc': 'Añade búsqueda de texto completo (escribir cualquier palabra sin elegir campo, '
                  'incluso coincidencias parciales dentro de otra palabra). '
                  'Aumenta el tamaño de la base de datos y puede tardar bastante: '
                  'actívalo solo si vas a usar esa función.\n'
                  'Entrada: data/database/bold_db.db   Salida: tabla bold_records_fts',
    'step1_tech': 'Descomprimir .tar.gz → filtrar TSV',
    'step2_tech': 'TSV → SQLite (bold_records)',
    'step3_tech': 'CREATE INDEX sobre 20 columnas',
    'step4_tech': 'Tabla virtual FTS5 (bold_records_fts)',
    'badge_required': 'OBLIGATORIO',
    'badge_recommended': 'RECOMENDADO',
    'badge_optional': 'OPCIONAL',
    'sidebar_group_individual_steps': 'PASOS INDIVIDUALES',
    'sidebar_group_hint': 'Para ejecutar uno a uno. No hace falta si ya usaste el botón de Inicio.',
    'pipeline1_title': 'Opción 1',
    'pipeline1_label': 'Crear la base de datos',
    'pipeline1_label_done': 'Rehacer la base de datos',
    'pipeline1_hint': 'Pasos 1 a 3. Es lo que necesita la mayoría: podrás buscar por '
                      'cualquier campo (especie, país, BIN…) y el visor irá rápido.',
    'pipeline1_tip': 'Ejecuta seguidos los pasos 1, 2 y 3.',
    'pipeline2_title': 'Opción 2',
    'pipeline2_label': 'Crear la base de datos completa',
    'pipeline2_label_done': 'Rehacer la base de datos completa',
    'pipeline2_hint': 'Pasos 1 a 4. Añade la búsqueda por texto libre. Tarda más y '
                      'hace la base de datos más grande.',
    'pipeline2_tip': 'Ejecuta seguidos los pasos 1, 2, 3 y 4. Elige esta opción solo si '
                     'vas a buscar escribiendo palabras sueltas sin elegir campo.',
    'badge_tip_required': 'Sin este paso la base de datos no se puede crear.',
    'badge_tip_recommended': 'Se puede omitir, pero el visor irá notablemente más lento.',
    'badge_tip_optional': 'Solo si vas a usar la búsqueda por texto libre.',
    'bold_downloaded_file': 'Archivo descargado de BOLD',
    'drop_targz_hint': 'Arrastra aquí el archivo .tar.gz descargado de BOLD\n'
                       '(o pulsa Examinar). Se copiará a data/raw/ automáticamente.',
    'btn_open_input_folder': 'Abrir carpeta de entrada',
    'tip_open_input_folder': 'Abre en el explorador la carpeta donde este paso '
                             'busca sus archivos de entrada.',
    'btn_run_step': 'Ejecutar este paso',
    'btn_stop': 'Detener',
    'tip_stop': 'Interrumpe el paso y descarta lo que llevara hecho. '
               'No hay pérdida de datos ya guardados.',
    'toggle_advanced_options_open': '▾  Opciones avanzadas',
    'toggle_advanced_options_closed': '⚙  Opciones avanzadas',
    'tip_rarely_needed': 'Ajustes que rara vez hace falta tocar.',
    'records_per_batch_label': 'Registros por lote:',
    'chunk_size_tip': 'Cuántos registros se escriben de una vez en la base de datos.\n'
                      'Más alto = algo más rápido pero más memoria RAM.\n'
                      'Más bajo = más lento pero seguro en equipos con poca RAM.\n'
                      'Si no sabes qué poner, deja {n:,}.',
    'default_chunk_hint': '(por defecto {n:,})',
    'missing_bold_file': '● Falta el archivo de BOLD — arrástralo arriba o cópialo a  data/raw/',
    'missing_prepared_data': '● Faltan los datos preparados — ejecuta antes el Paso 1',
    'missing_database': '● Falta la base de datos — ejecuta antes el Paso 2',
    'step_done_already': '● Este paso ya está hecho — no necesitas volver a ejecutarlo '
                         '(solo si descargas datos nuevos)',
    'step_ready': '● Todo listo para ejecutar este paso',
    'input_not_found_generic': '● Entrada no encontrada — ejecuta el paso anterior primero',
    'other_step_running_tip': 'Hay otro paso en ejecución. Espera a que termine.',
    'step_done_files_deleted_tip': 'Este paso ya está hecho y sus archivos de entrada se borraron por '
                                   'espacio. Para rehacerlo necesitas volver a poner el archivo de BOLD.',
    'missing_input_files_tip': 'Faltan los archivos de entrada de este paso: {msg}',
    'run_only_step_tip': 'Ejecuta solo este paso.',
    'stopping_status': 'Deteniendo...',
    'running_status': 'Ejecutando...',
    'no_progress_note': 'Sin progreso medible — es normal en esta operación. Puede tardar un buen rato.',
    'already_running_tip': 'Este paso ya se está ejecutando.',
    'completed_status': '✓  Completado',
    'stopped_status': 'Detenido',
    'error_status': 'Error — mira el detalle en el registro de abajo',
    'home_title': 'Inicio',
    'home_intro': 'Esta herramienta convierte la descarga pública de BOLD en una base de datos '
                  'local que puedes consultar con el visor. Se hace una sola vez por cada versión '
                  'de los datos.',
    'home_req_note': 'Necesitas el <b>.tar.gz</b> del portal de BOLD (sin descomprimir) '
                     'y espacio libre suficiente en el disco. Los pasos 1-3 pueden tardar '
                     'un buen rato: puedes dejarlo trabajando y volver luego.',
    'btn_open_viewer': 'Abrir el visor',
    'tip_open_viewer': 'Arranca el visor web y lo abre en el navegador (http://127.0.0.1:{port}).',
    'btn_manual': 'Manual de uso',
    'tip_manual': 'Abre el manual de uso (carpeta manual/).',
    'btn_download_bold': 'Descargar datos de BOLD',
    'tip_open_browser': 'Abre en el navegador:\n{url}',
    'btn_open_raw_folder': 'Abrir carpeta data/raw',
    'tip_raw_folder': 'Aquí es donde debe estar el archivo .tar.gz.',
    'btn_free_space': 'Liberar espacio',
    'no_data_detected': 'sin datos detectados',
    'records_count_suffix': ' — {n:,} registros',
    'data_version_label': 'Versión de datos:',
    'status_step1_label': '1. Preparar datos',
    'status_step2_label': '2. Base de datos{cnt}',
    'status_step3_label': '3. Búsquedas aceleradas',
    'status_step4_label': '4. Texto libre <i>(opcional)</i>',
    'free_space_btn_with_size': 'Liberar espacio  ({size})',
    'no_intermediate_files': 'No hay archivos intermedios que borrar.',
    'available_after_db_built': 'Disponible cuando la base de datos esté construida: ahora mismo '
                                'estos archivos son la entrada del Paso 2.',
    'free_space_tooltip': 'Borra los archivos de trabajo del Paso 1 ({size}), que ya no hacen '
                          'falta. La base de datos no se toca.',
    'viewer_ready_tip': 'Arranca el visor web en http://127.0.0.1:{port}',
    'viewer_not_ready_tip': 'Primero hay que crear la base de datos (pasos 1 y 2).',
    'main_hint_ready': 'Ya puedes consultar los datos: pulsa «Abrir el visor». Cuando descargues '
                       'una versión nueva de BOLD, actualízala (Opción 3) o rehazla (Opción 1 o 2).',
    'main_hint_not_ready': 'Los pasos ya completados se omiten automáticamente, así que es seguro '
                          'pulsar la Opción 1 o la 2 aunque hayas empezado antes.',
    'src_tsv': 'TSV descargado',
    'src_catalog': 'catálogo de BOLD',
    'fields_panel_title': 'Selección de Campos del archivo TSV',
    'fields_panel_intro': 'El archivo .tsv de BOLD contiene 76 columnas, y la base de datos se genera con las '
                          'seleccionadas aquí. Menos campos = base de datos más pequeña y pasos más '
                          'rápidos; más campos = más información en el visor y en las exportaciones.',
    'fields_panel_warning': '⚠️  La selección se aplica al <b>preparar los datos (Paso 1)</b>. Si la '
                            'cambias con la base de datos ya hecha, hay que repetir los pasos desde el '
                            '1, y eso necesita el <b>.tar.gz</b> de BOLD otra vez (se borra al procesarlo, salvo que marques «Conservar el .tar.gz» en el Paso 1).',
    'col_header_field': 'Campo TSV',
    'tip_include_field': 'Incluye el campo en la base de datos.',
    'col_header_index': 'Indexar',
    'tip_create_index': 'Crea un índice: acelera las búsquedas por ese campo.',
    'btn_save_selection': 'Guardar selección',
    'btn_reset_fields': 'Restablecer campos',
    'tip_reset_fields': 'Marca los {n} campos de fábrica en la lista.\n'
                        'OJO: solo cambia lo que se ve; el archivo no se toca hasta que pulses '
                        '«Guardar selección».',
    'btn_check_tsv': 'Comprobar con el TSV',
    'tip_index_field': 'Crea un índice para buscar más rápido por «{name}».',
    'tip_required_field': 'El visor necesita este campo: no se puede quitar.',
    'tag_not_in_tsv': 'no está en el TSV',
    'tip_field_missing': 'Este campo no aparece en la cabecera del TSV descargado.',
    'tip_field_missing_hint': '\nEl más parecido que sí está: «{hint}».',
    'tag_new_in_bold': 'nuevo en BOLD',
    'tip_new_field': 'Campo presente en el TSV descargado que no existía en las versiones anteriores de BOLD.',
    'counts_text': '<b>{n}</b> campos seleccionados · <b>{m}</b> con indexación (búsqueda rápida)',
    'unsaved_changes_badge': "&nbsp;&nbsp;<span style='color:{c}'>⚠️ Cambios sin guardar</span>",
    'btn_save_selection_dirty': 'Guardar selección  •',
    'fields_source_text': 'Lista de campos leída del <b>{src}</b>. ',
    'stale_db_note': " <span style='color:{c}'>La base de datos actual se construyó "
                     "con otra selección.</span>",
    'tip_check_tsv_available': 'Vuelve a leer la cabecera del TSV y escribe el resultado en el registro.',
    'tip_check_tsv_unavailable': 'No hay ningún .tar.gz ni .tsv de BOLD en el proyecto todavía: aparece '
                                 'en cuanto descargues el paquete a data/raw/.',
    'tip_step_running': 'Hay un paso en marcha: espera a que termine.',
    'tip_save_selection': 'Escribe la selección en dev/fields_config.json.',
    'check_not_done': "<span style='color:{c}'>Sin comprobar: no hay ningún archivo de "
                      "BOLD todavía.</span>",
    'check_missing_required': "<span style='color:{c}'>❌ Faltan {n} campos que el visor necesita: "
                              "el Paso 1 no puede continuar.</span>",
    'check_missing_fields': "<span style='color:{c}'>⚠️ {n} campo{s} configurado{s} no "
                            "{verb} en este TSV</span>",
    'check_new_fields': "<span style='color:{c}'>{n} campo{s} nuevo{s} de BOLD sin usar</span>",
    'check_all_ok': "<span style='color:{c}'>✅ Los {n} campos configurados existen en el "
                    "TSV.</span>",
    'log_no_bold_file': 'ℹ️ Todavía no hay ningún .tar.gz ni .tsv de BOLD en el '
                        'proyecto con el que comparar los campos.',
    'log_verify_header': '🔍 Verificación de campos contra la cabecera del TSV descargado '
                         '({n} columnas):',
    'log_verify_present': '   ✅ {n} de {total} campos configurados existen en el TSV',
    'log_verify_missing_item': '   {marca} No está en el TSV: {hint}',
    'log_verify_new_fields': '   ℹ️ Campos que ofrece BOLD y no estás usando ({n}): {fields}',
    'log_verify_will_stop': '   → El Paso 1 se detendrá: son campos que el visor da por '
                            'hechos. Revisa si BOLD los ha renombrado.',
    'unsaved_title': 'Cambios sin guardar',
    'unsaved_text': 'Has cambiado la selección de campos y no la has guardado.',
    'unsaved_info': 'Si sales ahora, el archivo conserva la selección anterior.',
    'btn_save': 'Guardar',
    'btn_discard': 'Descartar',
    'btn_keep_editing': 'Seguir editando',
    'no_fields_title': 'Sin campos',
    'no_fields_body': 'Hay que guardar al menos un campo.',
    'save_failed_title': 'No se pudo guardar',
    'save_failed_body': 'No se pudo escribir fields_config.json:\n{error}',
    'log_selection_saved': '✅ Selección guardada: {n} campos, {m} con búsqueda rápida '
                           '(dev/fields_config.json)',
    'log_selection_stale': '⚠️ Los datos ya preparados usan la selección anterior. Para '
                           'aplicar la nueva hay que repetir el Paso 1 con el .tar.gz de BOLD.',
    'nav_home': ' Inicio',
    'tip_home_nav': 'Requerimientos y creación completa de BD.',
    'config_section_label': 'CONFIGURACIÓN',
    'nav_fields_selection': 'Selección de Campos (.tsv)',
    'tip_fields_nav': 'Selección de columnas del archivo .tsv de BOLD que se incluyen en la base de datos.',
    'tip_project_folder': 'Carpeta del proyecto:\n{path}',
    'output_label': 'Log',
    'btn_view_status': 'Ver estado',
    'tip_view_status': 'Vuelve a mostrar en qué punto está el proyecto.',
    'btn_clear': 'Limpiar',
    'tip_clear_log': 'Vacía este registro. No afecta a los archivos ni a los pasos.',
    'tip_log_widget': 'Registro de lo que va ocurriendo. Se guarda también en data/state/logs/',
    'viewer_not_found_title': 'Visor no encontrado',
    'viewer_not_found_body': 'No se encontró el visor (frontend/server.py) en la carpeta del programa.',
    'no_db_yet_title': 'Todavía no hay base de datos',
    'no_db_yet_body': 'Primero ejecuta los pasos 1 y 2 para crear la base de datos.',
    'log_viewer_started': '🚀 Visor iniciado — abriendo http://127.0.0.1:{port} en el navegador',
    'ctx_open_viewer': 'Abrir el visor',
    'process_running_title': 'Proceso en curso',
    'process_running_body': 'Espera a que termine el paso en ejecución antes de borrar archivos.',
    'nothing_to_free_title': 'Nada que liberar',
    'nothing_to_free_body': 'No hay archivos intermedios en data/processed/.',
    'db_not_built_title': 'La base de datos no está construida',
    'db_not_built_body': 'Estos archivos son la entrada del Paso 2. Si los borras ahora '
                         'perderás el trabajo del Paso 1 y tendrás que empezar de cero.',
    'file_list_item': '    • {name}   ({size})',
    'free_space_confirm_text': 'Se van a borrar {size} de archivos de trabajo.',
    'free_space_confirm_info': 'Se eliminarán de data/:\n\n{listado}\n\n'
                               '⚠️  ESTA ACCIÓN NO SE PUEDE DESHACER.\n\n'
                               'Qué NO se ve afectado:\n'
                               '    • La base de datos (data/database/bold_db.db) queda intacta.\n'
                               '    • El visor sigue funcionando exactamente igual.\n'
                               '    • Los pasos 3 y 4 se pueden ejecutar sin problema.\n\n'
                               'Qué pierdes:\n'
                               '    • Para volver a construir la base de datos tendrías que descargar '
                               'otra vez el archivo de BOLD y repetir el Paso 1.\n\n'
                               '¿Borrar estos archivos?',
    'btn_yes_delete': 'Sí, borrar',
    'btn_cancel': 'Cancelar',
    'log_deleted_file': '🗑 Eliminado: {name}  ({size})',
    'log_space_freed': '✅ Espacio liberado: {size}',
    'manual_not_found_title': 'Manual no encontrado',
    'manual_not_found_body': 'No se encontró manual/guia_de_uso.html.',
    'next_step1': 'Siguiente: Paso 1 — Preparar datos',
    'next_step2': 'Siguiente: Paso 2 — Construir base de datos',
    'next_step3': 'Siguiente: Paso 3 — Acelerar búsquedas (recomendado)',
    'next_step4_optional': 'Ya puedes usar el visor. El Paso 4 (búsqueda por texto libre) es opcional.',
    'next_all_done': 'Todo completo. Pulsa «Abrir el visor» en Inicio.',
    'log_stopping_pipeline': '⚠️ Deteniendo pipeline...',
    'log_stopping_step': '⚠️ Deteniendo paso...',
    'pipeline_running_title': 'Pipeline en curso',
    'pipeline_running_body': 'Ya hay un pipeline ejecutandose.',
    'step_running_title': 'Paso en ejecucion',
    'sequence_step_item': '  • {label}',
    'confirm_title': 'Confirmar',
    'run_sequence_confirm': 'Se ejecutarán estos pasos, uno detrás de otro:\n\n{pasos}\n\n'
                            'En total puede tardar un buen rato. Puedes dejar la ventana abierta y '
                            'seguir usando el equipo; también puedes detenerlo en cualquier momento.\n\n'
                            'Los pasos que ya estén hechos se omiten automáticamente.\n\n¿Empezar?',
    'log_pipeline_start': '🚀 === PIPELINE: {label} ===',
    'log_process_finished': '\n🎉 === PROCESO TERMINADO ===',
    'log_step_failed': '⚠️ El paso no se completó ({time}) — se detiene aquí. '
                       'Revisa el mensaje de error de arriba.',
    'log_step_completed_in': '  ✅ Completado en {time}',
    'header_step1': '📋 Paso 1 — Preparar datos',
    'input_line': '   Entrada:  {ok} {msg}',
    'n_targz_in_raw': '{n} .tar.gz en data/raw/',
    'no_targz_in_raw': 'sin .tar.gz en data/raw/',
    'output_line': '   Salida:   {ok} {msg}',
    'n_filt_tsv': '{n} .filt.tsv en data/processed/',
    'no_filt_tsv_run_step': 'sin .filt.tsv — ejecuta este paso',
    'header_step2': '\n💾 Paso 2 — Construir base de datos',
    'filt_tsv_available': 'filt.tsv disponible',
    'no_filt_tsv_run_step1': 'sin .filt.tsv — ejecuta el Paso 1',
    'dbfile_with_count': 'bold_db.db{cnt}',
    'db_not_created_run_step': 'BD no creada — ejecuta este paso',
    'header_step3': '\n🔎 Paso 3 — Acelerar búsquedas',
    'dbfile_present': 'bold_db.db presente',
    'db_not_found_run_step2': 'BD no encontrada — ejecuta el Paso 2',
    'all_indexes_created': 'Todos los índices creados',
    'indexes_pending': 'índices pendientes — ejecuta este paso',
    'header_step4': '\n📄 Paso 4 — Búsqueda por texto libre (opcional)',
    'fts_table_present': 'Tabla FTS presente',
    'fts_not_created': 'FTS no creada — ejecuta este paso',
    'all_complete': '✅ Todo completo — {n:,} registros listos.',
    'tap_open_viewer_home': '👉 Pulsa «Abrir el visor» en la pantalla de Inicio.',
    'records_count_paren': ' ({n:,} registros)',
    'db_exists_can_query': '📋 La base de datos ya existe{cnt} — puedes consultarla desde ahora '
                           'con «Abrir el visor».',
    'pending_step3': 'Paso 3 — Acelerar búsquedas  (recomendado: el visor irá mucho más rápido)',
    'pending_step4': 'Paso 4 — Búsqueda por texto libre  (opcional)',
    'pending_steps_header': '⚠️ Pasos pendientes:',
    'no_db_yet_log': '⚠️ Aún no hay base de datos.',
    'go_home_create_db': '👉 Ve a «Inicio» y pulsa «Crear la base de datos».',
    'process_running_close_title': 'Hay un proceso en curso',
    'process_running_close_body': 'Todavía hay un paso ejecutándose.\n\n'
                                  'Si cierras ahora se interrumpirá de golpe y el trabajo de ese paso se '
                                  'perderá; puede quedar una base de datos a medias que habrá que rehacer.\n\n'
                                  'Es preferible pulsar «Detener» y esperar a que termine de cerrar.\n\n'
                                  '¿Cerrar de todos modos?',
    'migr_database': '📦 Base de datos movida a {detail}',
    'migr_old_viewer_removed': '🧹 Eliminada la copia antigua del visor ({detail})',
    'migr_old_shortcut_removed': '🧹 Eliminado un acceso directo antiguo: {detail}',
    'migr_old_program_removed': '🧹 Eliminada la versión anterior del programa ({detail})',
    'migr_fields_kept': '📦 Tu selección de campos se conserva en {detail}',
    'migr_database_in_use': '⚠️ La base de datos sigue en app/ porque estaba abierta (¿un visor de la versión anterior?). '
                            'Ciérralo y vuelve a abrir el programa para moverla. Mientras tanto funciona igual. ({detail})',
    'migr_state_not_moved': '⚠️ No se pudo mover {detail}',
    'migr_old_viewer_not_removed': '⚠️ No se pudo borrar la copia antigua del visor: {detail}',
    'migr_old_program_not_removed': '⚠️ No se pudo borrar la versión anterior del programa: {detail}',
    'migr_app_not_empty': '⚠️ {detail} contiene archivos que no son del programa: no se ha borrado. Revísala y bórrala a mano.',
    'btn_open_exports': '📁 Carpeta de exportaciones',
    'tip_open_exports': 'Los CSV, FASTA y ZIP que exporta el visor (data/exports); se vacía al abrir el visor.',
    'sc_section': 'Accesos directos',
    'sc_intro': 'Para abrir BOLD-Local-DB o el visor sin entrar en esta carpeta.',
    'sc_desktop': 'Escritorio',
    'sc_menu_win32': 'Menú Inicio',
    'sc_menu_darwin': 'Aplicaciones (Launchpad)',
    'sc_menu_linux': 'Menú de aplicaciones',
    'sc_create': 'Crear accesos directos',
    'sc_recreate': 'Volver a crear',
    'sc_remove': 'Quitar',
    'sc_none': 'Todavía no hay accesos directos.',
    'sc_made': 'Creados: «BOLD-Local-DB» y «Visor BOLD-Local-DB» en {where}.',
    'sc_and': ' y ',
    'sc_pick_one': 'Marca al menos un sitio (escritorio o menú).',
    'sc_created_log': '✅ Accesos directos creados ({n})',
    'sc_removed_log': '🧹 Accesos directos quitados ({n})',
    'sc_error': '❌ No se pudieron crear los accesos directos: {error}',
    'sc_ask_title': 'Accesos directos',
    'sc_ask_body': '¿Crear accesos directos en el escritorio y en el menú?\n\n'
                   '• BOLD-Local-DB: esta ventana, para crear o actualizar la base.\n'
                   '• Visor BOLD-Local-DB: el visor, directamente en el navegador.\n\n'
                   'Puedes quitarlos o crearlos más tarde desde Inicio.',
    'sc_ask_yes': 'Crear',
    'sc_ask_no': 'Ahora no',
    'upd_available': 'Hay una versión nueva de BOLD-Local-DB: <b>{new}</b> (tienes la {cur}).',
    'upd_staged': 'La versión <b>{new}</b> está descargada y se instalará al reiniciar.',
    'upd_see': 'Ver cambios',
    'upd_install': 'Actualizar',
    'upd_restart': 'Reiniciar ahora',
    'upd_downloading': 'Descargando… {pct}',
    'upd_git_tip': 'Copia de desarrollo (git): actualízala con «git pull».',
    'upd_no_asset_tip': 'No hay descarga para tu sistema en esta versión: ábrela en la página.',
    'upd_busy': 'Espera a que termine el paso en marcha antes de actualizar.',
    'upd_ready_title': 'Actualización lista',
    'upd_ready_body': 'La versión {v} está descargada. BOLD-Local-DB se cerrará y se abrirá de nuevo con ella.\n\n'
                      'Tu base de datos, tus exportaciones y tu selección de campos no se tocan.',
    'upd_restart_later': 'Más tarde',
    'upd_failed': '❌ No se pudo descargar la actualización: {error}',
    'upd_downloaded_log': '✅ Versión {v} descargada: se instala al reiniciar el programa',
    'btn_uninstall': 'Desinstalar…',
    'uninstall_title': 'Desinstalar BOLD-Local-DB',
    'uninstall_body': 'Se quitarán los accesos directos y se abrirá la carpeta que contiene el programa, '
                      'para que la borres.\n\n'
                      'Tu base de datos y tus exportaciones están en:\n{data}  ({size})\n'
                      'Si quieres conservarlas, muévelas a otro sitio antes de borrar la carpeta.\n\n'
                      'Aparte de los accesos directos, todo lo que usa el programa está dentro de esa carpeta.',
    'uninstall_go': 'Quitar accesos y abrir la carpeta',
    'log_session_header': '# BOLD DB Creator — sesión {ts}',
    'language_label': 'Idioma',
    'restart_required_title': 'Reiniciar para aplicar',
    'restart_required_body': 'El cambio de idioma se aplica al reiniciar la aplicación.\n\n'
                             '¿Reiniciar ahora?',
    'btn_restart_now': 'Reiniciar ahora',
    'btn_restart_later': 'Más tarde',
    'restart_blocked_body': 'Hay un proceso en marcha. El idioma se ha guardado y '
                            'se aplicará la próxima vez que abras la aplicación.',
    'copy_cancelled': 'copia cancelada',
    'log_closing_after_stop': '⏳ Deteniendo el proceso de forma segura… '
                              'La ventana se cerrará sola al terminar.',
    'closing_wait_title': 'Cerrando',
    'closing_wait_body': 'Ya se está deteniendo el proceso. La ventana se cerrará '
                         'en cuanto termine de forma segura.',
    'pipeline_blocked_body': 'Hay otro paso en marcha. Espera a que termine o detenlo '
                             'antes de lanzar la secuencia.',
    'fields_verified': '✅ Campos verificados contra la cabecera del TSV: {n} de {configured} '
                       'configurados, sobre {total} disponibles',
    'other_targz_ignored': '⚠️ Hay más de un .tar.gz en data/raw/. Se usa el más reciente '
                           '({name}); se ignoran (y no se borran): {others}',
    'unsafe_tar_member': '⚠️ Entrada del .tar.gz omitida por seguridad: {name}',
    'old_targz_ignored': '⚠️ {names}: de otra versión y anterior a los datos ya preparados '
                         '({filt}). Se ignora (no se borra).',
    'indexes_dropped': '🗑 {n} índice(s) eliminado(s) porque su campo ya no está marcado '
                       'para indexar: {names}',
    'targz_kept': '📦 Se conserva {name} en data/raw/ (opción «Conservar el .tar.gz»)',
    'keep_targz_label': 'Conservar el .tar.gz al terminar',
    'keep_targz_tip': 'Por defecto el .tar.gz (~10 GB) se borra al terminar este paso.\n'
                      'Si lo conservas, cambiar los campos más adelante no obliga a volver a '
                      'descargarlo.\nPuedes borrarlo después con «Liberar espacio» en Inicio.',
    # ---- Update (run_update) ----
    'step_update_name': 'Actualizar base de datos',
    'step_update_tech': '.filt.tsv nuevo → diferencias → bold_records, índices y FTS',
    'step_update_desc': 'Aplica a la base de datos actual solo los registros nuevos, modificados y '
                        'eliminados del paquete nuevo de BOLD, en lugar de reconstruirla. Necesita '
                        'antes el Paso 1 con el .tar.gz nuevo (la opción «Actualizar» de Inicio hace '
                        'los dos seguidos).\n'
                        'Entrada: data/processed/*.filt.tsv + data/database/bold_db.db   Salida: data/database/bold_db.db actualizada',
    'missing_update_input': '● Faltan los datos preparados del paquete nuevo — ejecuta antes el Paso 1 '
                            '(la Opción 3 de Inicio hace los dos seguidos)',
    'missing_update_no_db': '● Todavía no hay base de datos que actualizar — créala primero con la '
                            'Opción 1 o 2 de Inicio',
    'missing_update_old_db': '● La base de datos se creó con una versión anterior del programa — '
                             'reconstrúyela una vez (Opción 1 o 2 de Inicio) para poder actualizarla',
    'db_outdated_rebuild': '⚠️ Esta base de datos se creó con una versión anterior que guardaba una sola '
                           'secuencia por espécimen y descartaba las de los demás marcadores. Reconstrúyela '
                           '(Opción 1 o 2 de Inicio) para que estén todas.',
    'update_count_mismatch': '❌ Validación fallida: el paquete tiene {package:,} filas, pero la BD '
                             'quedaría con {stored:,} (huellas: {hashed:,}; esperado: {expected:,}). '
                             'La actualización se deshace y la BD queda como estaba.',
    'pipeline3_title': 'Opción 3 · Actualizar',
    'pipeline3_label': 'Actualizar la base de datos',
    'pipeline3_hint': 'Para el paquete semanal de BOLD: aplica solo lo que ha cambiado en lugar de '
                      'rehacerlo todo. Rinde mejor con disco SSD y RAM de sobra; en un disco '
                      'mecánico o con poca RAM la ventaja se reduce. Este equipo: {ram}{db}. '
                      'Pasa el ratón por el botón para ver los detalles.',
    'pipeline3_tip': 'Ejecuta el Paso 1 con el .tar.gz nuevo y aplica las diferencias a la base '
                     'de datos actual. Los índices y la búsqueda por texto libre se actualizan '
                     'solos: no hay que repetir los pasos 3 y 4, que son la mayor parte del '
                     'tiempo de una reconstrucción.\n\n'
                     'Cuándo conviene:\n'
                     '• Con disco SSD y RAM suficiente para que quepan los índices (en torno al '
                     'tamaño de la base de datos o más), suele tardar una fracción de reconstruir.\n'
                     '• En un disco duro mecánico, o con mucha menos RAM que el tamaño de la base '
                     'de datos, cada cambio cuesta bastante más y la ventaja se reduce: '
                     'reconstruir puede tardar parecido.\n'
                     '• Si cambia más del {pct} % de los registros se detiene sin tocar nada y '
                     'recomienda reconstruir.\n'
                     '• Tras muchas actualizaciones conviene reconstruir de vez en cuando (p. ej. '
                     'una vez al mes): los índices se fragmentan.\n\n'
                     'Si falla o lo detienes, la base de datos queda exactamente como estaba, y el '
                     'visor puede seguir usándose mientras se actualiza.',
    'update_unavailable_tip': 'Tu base de datos se creó con una versión anterior del programa, en un '
                              'formato que la actualización no puede modificar. Reconstrúyela una vez '
                              'con la Opción 1 o 2; después ya podrás actualizarla.',
    'update_needs_package_tip': 'Arrastra primero el .tar.gz nuevo de BOLD a la zona de arriba (o '
                                'cópialo a data/raw/): la actualización compara ese paquete con tu base de datos.',
    'pipeline_needs_package_tip': 'Arrastra primero el .tar.gz descargado de BOLD a la zona de arriba '
                                  '(o cópialo a data/raw/).',
    'pipeline_done_needs_package_tip': 'Ya está hecho: no queda ningún paso pendiente. Para rehacer la '
                                       'base de datos con una versión nueva de BOLD, arrastra antes el '
                                       '.tar.gz nuevo a la zona de arriba.',
    'log_step_skipped_done': '⏭ {label}: ya hecho y sus archivos de entrada ya no están (se liberó '
                             'espacio) — se omite',
    'update_no_db_tip': 'Todavía no hay base de datos que actualizar. Créala primero con la Opción 1 '
                        'o 2; a partir de ahí podrás actualizarla con cada paquete nuevo de BOLD.',
    'ram_gb': '{n:.0f} GB de RAM',
    'ram_unknown': 'RAM no detectada',
    'db_size_part': '; base de datos actual: {size}',
    'ctx_update': 'Actualizar base de datos',
    'update_needs_rebuild_once': '❌ Esta base de datos se creó con una versión anterior del programa y '
                                 'no guarda las huellas de los registros que necesita la actualización. '
                                 'Reconstrúyela una vez («Crear la base de datos» en Inicio); a partir de '
                                 'ahí podrás actualizarla.',
    'update_schema_mismatch': '❌ Esta base de datos tiene el formato {have} y esta versión del programa '
                              'escribe el formato {want}: no se puede actualizar sin mezclar los dos. '
                              'Reconstrúyela una vez («Crear la base de datos» en Inicio).',
    'update_fields_mismatch': '❌ Los campos del paquete nuevo no coinciden con los de la base de datos '
                              '(¿cambiaste la selección de campos?). Hay que reconstruirla.\n'
                              '   Solo en el paquete: {extra}\n   Solo en la base de datos: {missing}',
    'update_pass1': '⏳ Calculando la huella de cada registro del paquete nuevo...',
    'update_pass1_progress': '\r   {n:,} registros leídos ({pct:.0f} %)',
    'update_diff': '🔍 Comparando con la base de datos actual...',
    'update_stats': '📊 Cambios: {new:,} nuevos · {chg:,} modificados · {gone:,} eliminados '
                    '— {pct:.2f} % de {total:,} registros',
    'update_too_many': '⚠️ Cambia más del {limit} % de los registros: reconstruir desde cero '
                       'probablemente sea más rápido. No se ha modificado nada; usa «Crear la base '
                       'de datos» en Inicio.',
    'update_nothing': '✅ La base de datos ya estaba al día: no hay cambios que aplicar.',
    'update_applying': '⏳ Aplicando los cambios (índices{fts} incluidos)...',
    'update_with_fts': ' y búsqueda por texto libre',
    'update_apply_progress': '\r   {done:,} de {total:,} cambios aplicados ({pct:.0f} %)',
    'update_done': '✅ Base de datos actualizada en {mins:.1f} min: {new:,} nuevos, {chg:,} '
                   'modificados, {gone:,} eliminados. Total: {total:,} registros.',
    'update_interrupted': '⚠️ Actualización detenida. La base de datos queda exactamente como estaba.',
    'update_failed_rolled_back': '   La base de datos no se ha modificado: los cambios se han descartado.',
}
STRINGS_EN = {
    'field_renamed_hint': '{name} (now called «{hint}»?)',
    'err_file_not_found': 'File or folder not found',
    'err_action_check_files': 'Check that the input files exist and the path is correct',
    'err_permission': 'File in use or no write permission',
    'err_action_close_file': 'Close the file if it is open in another program',
    'err_memory': 'Insufficient RAM',
    'err_action_close_apps': 'Close other applications and try again',
    'err_db_error': 'Database error',
    'err_action_check_db': 'Check that the database is not open in another process',
    'err_encoding': 'File encoding error',
    'err_action_utf8': 'The input file must be saved as UTF-8',
    'err_action_check_format': 'Check that the input file has the expected format',
    'err_unexpected_value': 'Unexpected value in the data',
    'err_action_check_values': 'Check the input file for invalid values',
    'err_timeout': 'Timeout',
    'err_action_check_process': 'Check that the external process is responding correctly',
    'err_module_not_installed': "Module not installed: '{mod}'",
    'err_column_not_found': 'Column or field not found: {raw}',
    'err_unexpected_type': 'Unexpected error ({type})',
    'err_action_pip_install': 'Run in the terminal:  pip install {mod}',
    'err_action_check_error_log': 'Check the technical detail in error.log',
    'err_step_line': '   Step: {context}',
    'err_detail_saved': 'Technical detail saved in data/state/logs/error.log',
    'unfiltered_tsv': 'Unfiltered TSV',
    'deleted_processed': '🗑 {label} deleted (already processed): {name}',
    'could_not_delete_named': '⚠️ Could not delete {name}: {error}',
    'db_busy_retrying': '⏳ Database busy (likely the antivirus scanning it) — retrying in {delay}s ({attempt}/{attempts})...',
    'could_not_delete': '⚠️ Could not delete: {error}',
    'deleting_old_version_file': '🗑 Deleting old version file: {name}',
    'fields_changed_refiltering': '♻ Field selection changed — redoing the filtering',
    'fields_changed_no_targz': "⚠️ The field selection changed, but the current filtered TSV was made with the previous one and the .tar.gz is no longer there to redo it. Download it again from BOLD if you want to apply the new fields.",
    'filtered_tsv_exists': '✅ Filtered TSV already exists: {name} — skipping Step 1',
    'tsv_already_extracted': '📁 TSV already extracted: {name} — skipping decompression',
    'no_targz_found': '❌ No *.tar.gz file found in ../data/raw/ nor an extracted TSV in ../data/processed/',
    'decompressing': '⏳ Decompressing: {name} ({mb:.0f} MB)',
    'extracted_progress': '\r💽 Extracted: {gb:.2f} GB',
    'extraction_interrupted': '⚠️ Extraction interrupted — partial files deleted',
    'decompression_finished': '✅ Decompression finished ({gb:.2f} GB written)',
    'no_tsv_found': '❌ No .tsv file found in ../data/processed/',
    'multiple_tsv_found': '⚠️ Multiple .tsv files found — using the largest one',
    'tsv_file_label': '📁 TSV file: {name}',
    'size_mb': '💽 Size: {mb:.1f} MB',
    'filtering_info': '⏳ Filtering data...',
    'empty_tsv_file': '❌ Empty TSV file',
    'rows_processed_pct': '\r  {n:,} rows processed ({pct:.1f}%)',
    'filtering_interrupted': '⚠️ Filtering interrupted — partial file deleted',
    'filtered_records': '✅ Filtered records: {n:,}',
    'filter_summary': '📊 Package rows: {read:,} · with a sequence (kept): {kept:,} · '
                      'without a sequence (left out): {no_seq:,}',
    'filter_only_n': '🧹 Sequences made only of Ns (left out): {n:,}',
    'filter_malformed': '⚠️ {n:,} line(s) of the package are broken (missing columns, probably '
                        'because of a line break inside a field) and couldn\'t be read',
    'file_label': '📁 File: {name}',
    'ctx_generate_tsv': 'Generate TSV',
    'no_filt_tsv_found': '❌ No *.filt.tsv file found in ../data/processed/',
    'db_already_built': '✅ DB already built from {name} — skipping import',
    'total_records_expected': '📋 Total records expected: {n:,}',
    'records_number_not_found': '⚠️ records.number not found — run Step 1 first',
    'columns_detected': '📋 Columns detected: {n}',
    'table_created': '✅ Table created with types and constraints',
    'importing_data': '⏳ Importing data (this may take a while)...',
    'import_progress': '\r  {processed:,} / {total:,}  ({pct:.1f}%)',
    'import_progress_nototal': '\r  {processed:,} rows imported',
    'db_created': '✅ DB created: {name}',
    'records_inserted': '✅ Records inserted: {n:,}',
    'import_count_mismatch': '❌ Validation failed: {read:,} rows were read, {stored:,} stored '
                             '(fingerprints: {hashed:,}) and {expected:,} expected. The new DB is '
                             'discarded; the current one was not touched. Repeat Step 1 in case the '
                             '.filt.tsv is damaged.',
    'import_irregular_rows': '⚠️ {n:,} row(s) of the .filt.tsv don\'t have {cols} columns; they were '
                             'stored anyway (missing ones empty). Repeat Step 1 if you didn\'t expect this.',
    'creating_processid_index': '⏳ Creating the BOLD ID (processid) index...',
    'sequences_per_specimen': '📊 {seqs:,} sequences from {specimens:,} specimens: {extra:,} are '
                              'additional sequences of specimens with more than one marker',
    'import_interrupted': '⚠️ Import interrupted — the half-built DB was deleted; the current one was not touched',
    'db_build_target': '💾 The new DB is built in {name}: the viewer keeps using the current one until '
                       'it finishes and replaces it',
    'db_build_space_warn': '⚠️ Free disk space: {free}. While it\'s being built, the new DB sits next to '
                           'the current one and will need about {need}. If the disk fills up, the '
                           'step fails and the current DB stays intact.',
    'db_build_promoted': '✅ New DB in service: {name} (the viewer now uses it)',
    'db_build_orphan_removed': '🧹 Deleted a half-built DB ({name}) left by a run that didn\'t finish',
    'db_build_promote_failed': '⚠️ The new DB is ready in {name}, but the current one couldn\'t be replaced '
                               '({error}). Usually the viewer is in the middle of a search or export: '
                               'wait for it to finish and run any step again, or restart the Creator.',
    'db_build_pending_title': 'New DB pending',
    'db_build_pending_body': 'A finished new database hasn\'t replaced the current one yet (the viewer '
                             'was using it). Wait for the viewer to finish what it\'s doing, or close '
                             'it, and try again.',
    'ctx_tsv_to_sqlite': 'TSV → SQLite',
    'db_not_found': '❌ DB not found: {path}',
    'table_not_exists': '❌ Table bold_records does not exist',
    'recreating_index': '♻ Recreating idx_col_nuc_basecount without COLLATE NOCASE',
    'missing_index_fields': '⚠️ Fields configured for indexing that are not in the DB (Steps 1 and 2 need to be redone): {fields}',
    'no_index_fields_exist': '❌ None of the fields configured for indexing exist in the DB',
    'all_columns_indexed': '✅ All columns are already indexed',
    'creating_indexes': '⏳ Creating {n} indexes (skipping {skipped} already existing)...',
    'indexing_interrupted': '⚠️ Indexing interrupted — {n} indexes created',
    'index_created_progress': '  ✅ [{i}/{n}] {name}',
    'index_error': '  ❌ [{i}/{n}] Error in {col}: {error}',
    'indexes_created': '✅ {n} indexes created',
    'ctx_index_fields': 'Index fields',
    'no_fts_columns': '❌ No FTS column found in bold_records',
    'fts_already_built': '✅ FTS5 already built for this DB — skipping rebuild',
    'empty_table': '⚠️ Empty table',
    'creating_fts_table': '⏳ Creating FTS5 table ({n} columns, excludes col_nuc)...',
    'rebuilding_fts_index': '⏳ Rebuilding FTS5 index ({n:,} records) — one-off operation...',
    'fts_may_take_minutes': "   This can take a while. The bar moves back and forth without showing a percentage because SQLite doesn't report progress: that's normal, it isn't stuck.",
    'still_working': '\r   ⏳ Still working — {mins:.0f} min elapsed (do not close the window)',
    'rebuild_finished': '   Rebuild finished in {mins:.0f} min',
    'fts_stopping': '⏳ Stopping the FTS index rebuild (may take a few seconds)...',
    'fts_interrupted': '⚠️ FTS rebuild interrupted — FTS table not created',
    'fts_created': '✅ FTS5 created — {n} columns, {total:,} records',
    'ctx_create_fts': 'Create FTS',
    'new_fields_found': 'ℹ️ New fields in this TSV that were not in previous BOLD versions ({n}): {fields}',
    'can_add_from_panel': '   You can add them to the database from the «Fields» panel.',
    'missing_required_fields': '❌ Missing fields the viewer needs: {fields}',
    'header_mismatch_hint': '   The header of this TSV does not match the configured selection. If BOLD renamed them, the «Fields» panel needs to be updated (and the viewer, which expects those column names).',
    'missing_configured_fields': '⚠️ Configured fields that do not exist in this TSV (skipped): {fields}',
    'missing_filter_field': "❌ The TSV header does not have the '{field}' field, needed to discard records without a sequence.",
    'no_configured_fields_exist': '❌ None of the configured fields exist in the downloaded TSV.',
    'btn_browse': 'Browse',
    'tip_browse_disk': 'Browse the disk for the file downloaded from BOLD.',
    'btn_open_folder': 'Open folder',
    'tip_open_folder': 'Open {dir} in the file explorer.',
    'file_loaded_label': '✓  {name}{extra}',
    'size_extra': '  —  {gb:.1f} GB',
    'invalid_file_title': 'Invalid file',
    'invalid_file_body': 'Expected a {ext} file.\n\nThat is the compressed file you download from the BOLD portal, without extracting it.',
    'file_exists_title': 'File already exists',
    'file_exists_body': 'There is already a file with that name in data/raw/.\nReplace it?',
    'copying_to_raw': '📁 Copying to data/raw/: {name} ({gb:.1f} GB) — this may take a few minutes',
    'copying_hint': 'Copying to data/raw/…',
    'file_copied': '✅ File copied to data/raw/: {name}',
    'copy_failed': '❌ Could not copy the file: {info}',
    'select_downloaded_file': 'Select the file downloaded from BOLD',
    'bold_file_filter': 'Compressed BOLD file (*.tar.gz);;All files (*.*)',
    'step1_name': 'Prepare data',
    'step2_name': 'Build database',
    'step3_name': 'Speed up searches',
    'step4_name': 'Full-text search',
    'step1_desc': 'Extracts the file you downloaded from BOLD and keeps only the fields '
                  'chosen in «Fields». It is the heaviest step on disk.\n'
                  'Input: data/raw/*.tar.gz   Output: data/processed/*.filt.tsv',
    'step2_desc': 'Loads the prepared data into the database the viewer queries. Once finished '
                  'you can already search records.\n'
                  'Input: data/processed/*.filt.tsv   Output: data/database/bold_db.db',
    'step3_desc': 'Creates indexes on the search fields. Without them every query scans the '
                  'whole table and the viewer is very slow.\n'
                  'Input: data/database/bold_db.db   Output: indexes on bold_records',
    'step4_desc': 'Adds full-text search (type any word without choosing a field, '
                  'including partial matches inside another word). '
                  'It makes the database larger and can take a while: '
                  'enable it only if you are going to use that feature.\n'
                  'Input: data/database/bold_db.db   Output: bold_records_fts table',
    'step1_tech': 'Extract .tar.gz → filter TSV',
    'step2_tech': 'TSV → SQLite (bold_records)',
    'step3_tech': 'CREATE INDEX on 20 columns',
    'step4_tech': 'FTS5 virtual table (bold_records_fts)',
    'badge_required': 'REQUIRED',
    'badge_recommended': 'RECOMMENDED',
    'badge_optional': 'OPTIONAL',
    'sidebar_group_individual_steps': 'INDIVIDUAL STEPS',
    'sidebar_group_hint': 'To run one at a time. Not needed if you already used the Start button.',
    'pipeline1_title': 'Option 1',
    'pipeline1_label': 'Create the database',
    'pipeline1_label_done': 'Redo the database',
    'pipeline1_hint': 'Steps 1 to 3. This is what most people need: you will be able to search by '
                      'any field (species, country, BIN…) and the viewer will be fast.',
    'pipeline1_tip': 'Runs steps 1, 2 and 3 in sequence.',
    'pipeline2_title': 'Option 2',
    'pipeline2_label': 'Create the full database',
    'pipeline2_label_done': 'Redo the full database',
    'pipeline2_hint': 'Steps 1 to 4. Adds full-text search. Takes longer and makes '
                      'the database larger.',
    'pipeline2_tip': 'Runs steps 1, 2, 3 and 4 in sequence. Choose this option only if you '
                     'are going to search by typing loose words without choosing a field.',
    'badge_tip_required': "Without this step the database can't be created.",
    'badge_tip_recommended': 'It can be skipped, but the viewer will be noticeably slower.',
    'badge_tip_optional': 'Only if you are going to use full-text search.',
    'bold_downloaded_file': 'File downloaded from BOLD',
    'drop_targz_hint': 'Drag the .tar.gz file downloaded from BOLD here\n'
                       '(or click Browse). It will be copied to data/raw/ automatically.',
    'btn_open_input_folder': 'Open input folder',
    'tip_open_input_folder': 'Opens in the file explorer the folder where this step '
                             'looks for its input files.',
    'btn_run_step': 'Run this step',
    'btn_stop': 'Stop',
    'tip_stop': 'Interrupts the step and discards whatever it had done. '
               'No already-saved data is lost.',
    'toggle_advanced_options_open': '▾  Advanced options',
    'toggle_advanced_options_closed': '⚙  Advanced options',
    'tip_rarely_needed': 'Settings you rarely need to touch.',
    'records_per_batch_label': 'Records per batch:',
    'chunk_size_tip': 'How many records are written to the database at once.\n'
                      'Higher = somewhat faster but more RAM.\n'
                      'Lower = slower but safer on machines with little RAM.\n'
                      "If you don't know what to put, leave {n:,}.",
    'default_chunk_hint': '(default {n:,})',
    'missing_bold_file': '● Missing the BOLD file — drag it above or copy it to  data/raw/',
    'missing_prepared_data': '● Missing the prepared data — run Step 1 first',
    'missing_database': '● Missing the database — run Step 2 first',
    'step_done_already': "● This step is already done — you don't need to run it again "
                         "(unless you download new data)",
    'step_ready': '● Everything ready to run this step',
    'input_not_found_generic': '● Input not found — run the previous step first',
    'other_step_running_tip': 'Another step is running. Wait for it to finish.',
    'step_done_files_deleted_tip': 'This step is already done and its input files were deleted for '
                                   'space. To redo it you need to put the BOLD file back.',
    'missing_input_files_tip': 'This step is missing its input files: {msg}',
    'run_only_step_tip': 'Runs only this step.',
    'stopping_status': 'Stopping...',
    'running_status': 'Running...',
    'no_progress_note': "No measurable progress — that's normal for this operation. It can take a good while.",
    'already_running_tip': 'This step is already running.',
    'completed_status': '✓  Completed',
    'stopped_status': 'Stopped',
    'error_status': 'Error — see the detail in the log below',
    'home_title': 'Start',
    'home_intro': "This tool converts BOLD's public download into a local database "
                  "you can query with the viewer. It's done once per data version.",
    'home_req_note': 'You need the <b>.tar.gz</b> from the BOLD portal (do not extract it) '
                     'and enough free space on this disk. Steps 1-3 can take a while: '
                     'you can leave it working and come back later.',
    'btn_open_viewer': 'Open the viewer',
    'tip_open_viewer': 'Starts the web viewer and opens it in the browser (http://127.0.0.1:{port}).',
    'btn_manual': 'User manual',
    'tip_manual': 'Opens the user manual (manual/ folder).',
    'btn_download_bold': 'Download data from BOLD',
    'tip_open_browser': 'Opens in the browser:\n{url}',
    'btn_open_raw_folder': 'Open data/raw folder',
    'tip_raw_folder': 'This is where the .tar.gz file must be.',
    'btn_free_space': 'Free up space',
    'no_data_detected': 'no data detected',
    'records_count_suffix': ' — {n:,} records',
    'data_version_label': 'Data version:',
    'status_step1_label': '1. Prepare data',
    'status_step2_label': '2. Database{cnt}',
    'status_step3_label': '3. Sped-up searches',
    'status_step4_label': '4. Full text <i>(optional)</i>',
    'free_space_btn_with_size': 'Free up space  ({size})',
    'no_intermediate_files': 'No intermediate files to delete.',
    'available_after_db_built': 'Available once the database is built: right now '
                                'these files are the input for Step 2.',
    'free_space_tooltip': 'Deletes the Step 1 working files ({size}), which are no longer '
                          'needed. The database is not touched.',
    'viewer_ready_tip': 'Starts the web viewer at http://127.0.0.1:{port}',
    'viewer_not_ready_tip': 'The database needs to be created first (steps 1 and 2).',
    'main_hint_ready': 'You can already browse the data: click «Open the viewer». When you download '
                       'a new version of BOLD, update it (Option 3) or redo it (Option 1 or 2).',
    'main_hint_not_ready': "Steps already completed are skipped automatically, so it's safe "
                          'to click Option 1 or 2 even if you started before.',
    'src_tsv': 'downloaded TSV',
    'src_catalog': 'BOLD catalog',
    'fields_panel_title': 'TSV File Field Selection',
    'fields_panel_intro': "BOLD's .tsv file has 76 columns, and the database is generated with the "
                          "ones selected here. Fewer fields = smaller database and faster steps; "
                          "more fields = more information in the viewer and exports.",
    'fields_panel_warning': '⚠️  The selection applies when <b>preparing the data (Step 1)</b>. If you '
                            'change it after the database is already built, the steps need to be repeated from '
                            'Step 1, and that needs the BOLD <b>.tar.gz</b> again (it is deleted once processed, unless you tick "Keep the .tar.gz" in Step 1).',
    'col_header_field': 'TSV Field',
    'tip_include_field': 'Includes the field in the database.',
    'col_header_index': 'Index',
    'tip_create_index': 'Creates an index: speeds up searches on that field.',
    'btn_save_selection': 'Save selection',
    'btn_reset_fields': 'Reset fields',
    'tip_reset_fields': 'Marks the {n} factory-default fields in the list.\n'
                        "NOTE: this only changes what you see; the file isn't touched until you press "
                        '«Save selection».',
    'btn_check_tsv': 'Check against TSV',
    'tip_index_field': 'Creates an index to search faster by «{name}».',
    'tip_required_field': "The viewer needs this field: it can't be removed.",
    'tag_not_in_tsv': 'not in the TSV',
    'tip_field_missing': 'This field does not appear in the header of the downloaded TSV.',
    'tip_field_missing_hint': '\nThe closest match that is there: «{hint}».',
    'tag_new_in_bold': 'new in BOLD',
    'tip_new_field': 'Field present in the downloaded TSV that did not exist in previous versions of BOLD.',
    'counts_text': '<b>{n}</b> fields selected · <b>{m}</b> with indexing (fast search)',
    'unsaved_changes_badge': "&nbsp;&nbsp;<span style='color:{c}'>⚠️ Unsaved changes</span>",
    'btn_save_selection_dirty': 'Save selection  •',
    'fields_source_text': 'Field list read from the <b>{src}</b>. ',
    'stale_db_note': " <span style='color:{c}'>The current database was built "
                     "with a different selection.</span>",
    'tip_check_tsv_available': 'Re-reads the TSV header and writes the result to the log.',
    'tip_check_tsv_unavailable': "There isn't a BOLD .tar.gz or .tsv in the project yet: it will appear "
                                 'as soon as you download the package into data/raw/.',
    'tip_step_running': 'A step is running: wait for it to finish.',
    'tip_save_selection': 'Writes the selection to dev/fields_config.json.',
    'check_not_done': "<span style='color:{c}'>Not checked: there is no BOLD "
                      "file yet.</span>",
    'check_missing_required': "<span style='color:{c}'>❌ Missing {n} fields the viewer needs: "
                              "Step 1 cannot continue.</span>",
    'check_missing_fields': "<span style='color:{c}'>⚠️ {n} configured field{s} "
                            "{verb} not in this TSV</span>",
    'check_new_fields': "<span style='color:{c}'>{n} new field{s} from BOLD not in use</span>",
    'check_all_ok': "<span style='color:{c}'>✅ All {n} configured fields exist in the "
                    "TSV.</span>",
    'log_no_bold_file': "ℹ️ There isn't a BOLD .tar.gz or .tsv in the "
                        'project yet to compare the fields against.',
    'log_verify_header': '🔍 Checking fields against the downloaded TSV header '
                         '({n} columns):',
    'log_verify_present': '   ✅ {n} of {total} configured fields exist in the TSV',
    'log_verify_missing_item': '   {marca} Not in the TSV: {hint}',
    'log_verify_new_fields': "   ℹ️ Fields BOLD offers that you aren't using ({n}): {fields}",
    'log_verify_will_stop': '   → Step 1 will stop: these are fields the viewer relies on. '
                            'Check whether BOLD renamed them.',
    'unsaved_title': 'Unsaved changes',
    'unsaved_text': "You've changed the field selection and haven't saved it.",
    'unsaved_info': 'If you leave now, the file keeps the previous selection.',
    'btn_save': 'Save',
    'btn_discard': 'Discard',
    'btn_keep_editing': 'Keep editing',
    'no_fields_title': 'No fields',
    'no_fields_body': 'At least one field must be saved.',
    'save_failed_title': 'Could not save',
    'save_failed_body': 'Could not write fields_config.json:\n{error}',
    'log_selection_saved': '✅ Selection saved: {n} fields, {m} with fast search '
                           '(dev/fields_config.json)',
    'log_selection_stale': '⚠️ The already-prepared data uses the previous selection. To '
                           'apply the new one, Step 1 needs to be repeated with the BOLD .tar.gz.',
    'nav_home': ' Start',
    'tip_home_nav': 'Requirements and full DB creation.',
    'config_section_label': 'SETTINGS',
    'nav_fields_selection': 'Field Selection (.tsv)',
    'tip_fields_nav': "Selection of columns from BOLD's .tsv file included in the database.",
    'tip_project_folder': 'Project folder:\n{path}',
    'output_label': 'Log',
    'btn_view_status': 'View status',
    'tip_view_status': 'Shows again where the project stands.',
    'btn_clear': 'Clear',
    'tip_clear_log': "Clears this log. Doesn't affect the files or the steps.",
    'tip_log_widget': 'Log of what is happening. Also saved to data/state/logs/',
    'viewer_not_found_title': 'Viewer not found',
    'viewer_not_found_body': 'The viewer (frontend/server.py) was not found in the program folder.',
    'no_db_yet_title': 'No database yet',
    'no_db_yet_body': 'Run steps 1 and 2 first to create the database.',
    'log_viewer_started': '🚀 Viewer started — opening http://127.0.0.1:{port} in the browser',
    'ctx_open_viewer': 'Open the viewer',
    'process_running_title': 'Process running',
    'process_running_body': 'Wait for the running step to finish before deleting files.',
    'nothing_to_free_title': 'Nothing to free up',
    'nothing_to_free_body': 'There are no intermediate files in data/processed/.',
    'db_not_built_title': 'The database is not built',
    'db_not_built_body': 'These files are the input for Step 2. If you delete them now '
                         "you'll lose Step 1's work and have to start over.",
    'file_list_item': '    • {name}   ({size})',
    'free_space_confirm_text': '{size} of working files are about to be deleted.',
    'free_space_confirm_info': 'These will be removed from data/:\n\n{listado}\n\n'
                               '⚠️  THIS ACTION CANNOT BE UNDONE.\n\n'
                               'What is NOT affected:\n'
                               '    • The database (data/database/bold_db.db) stays intact.\n'
                               '    • The viewer keeps working exactly the same.\n'
                               '    • Steps 3 and 4 can still be run without issue.\n\n'
                               'What you lose:\n'
                               '    • To rebuild the database you would need to download '
                               'the BOLD file again and repeat Step 1.\n\n'
                               'Delete these files?',
    'btn_yes_delete': 'Yes, delete',
    'btn_cancel': 'Cancel',
    'log_deleted_file': '🗑 Deleted: {name}  ({size})',
    'log_space_freed': '✅ Space freed: {size}',
    'manual_not_found_title': 'Manual not found',
    'manual_not_found_body': 'manual/guia_de_uso.html was not found.',
    'next_step1': 'Next: Step 1 — Prepare data',
    'next_step2': 'Next: Step 2 — Build database',
    'next_step3': 'Next: Step 3 — Speed up searches (recommended)',
    'next_step4_optional': 'You can already use the viewer. Step 4 (full-text search) is optional.',
    'next_all_done': 'All done. Click «Open the viewer» on Start.',
    'log_stopping_pipeline': '⚠️ Stopping pipeline...',
    'log_stopping_step': '⚠️ Stopping step...',
    'pipeline_running_title': 'Pipeline running',
    'pipeline_running_body': 'A pipeline is already running.',
    'step_running_title': 'Step running',
    'sequence_step_item': '  • {label}',
    'confirm_title': 'Confirm',
    'run_sequence_confirm': 'These steps will run one after another:\n\n{pasos}\n\n'
                            'In total this can take a while. You can leave the window open and '
                            'keep using the computer; you can also stop it at any time.\n\n'
                            'Steps already done are skipped automatically.\n\nStart?',
    'log_pipeline_start': '🚀 === PIPELINE: {label} ===',
    'log_process_finished': '\n🎉 === PROCESS FINISHED ===',
    'log_step_failed': '⚠️ The step did not complete ({time}) — stopping here. '
                       'Check the error message above.',
    'log_step_completed_in': '  ✅ Completed in {time}',
    'header_step1': '📋 Step 1 — Prepare data',
    'input_line': '   Input:  {ok} {msg}',
    'n_targz_in_raw': '{n} .tar.gz in data/raw/',
    'no_targz_in_raw': 'no .tar.gz in data/raw/',
    'output_line': '   Output:   {ok} {msg}',
    'n_filt_tsv': '{n} .filt.tsv in data/processed/',
    'no_filt_tsv_run_step': 'no .filt.tsv — run this step',
    'header_step2': '\n💾 Step 2 — Build database',
    'filt_tsv_available': 'filt.tsv available',
    'no_filt_tsv_run_step1': 'no .filt.tsv — run Step 1',
    'dbfile_with_count': 'bold_db.db{cnt}',
    'db_not_created_run_step': 'DB not created — run this step',
    'header_step3': '\n🔎 Step 3 — Speed up searches',
    'dbfile_present': 'bold_db.db present',
    'db_not_found_run_step2': 'DB not found — run Step 2',
    'all_indexes_created': 'All indexes created',
    'indexes_pending': 'indexes pending — run this step',
    'header_step4': '\n📄 Step 4 — Full-text search (optional)',
    'fts_table_present': 'FTS table present',
    'fts_not_created': 'FTS not created — run this step',
    'all_complete': '✅ All done — {n:,} records ready.',
    'tap_open_viewer_home': '👉 Click «Open the viewer» on the Start screen.',
    'records_count_paren': ' ({n:,} records)',
    'db_exists_can_query': '📋 The database already exists{cnt} — you can query it now '
                           'with «Open the viewer».',
    'pending_step3': 'Step 3 — Speed up searches  (recommended: the viewer will be much faster)',
    'pending_step4': 'Step 4 — Full-text search  (optional)',
    'pending_steps_header': '⚠️ Pending steps:',
    'no_db_yet_log': '⚠️ There is no database yet.',
    'go_home_create_db': '👉 Go to «Start» and click «Create the database».',
    'process_running_close_title': 'A process is running',
    'process_running_close_body': "A step is still running.\n\n"
                                  "If you close now it will be interrupted abruptly and that step's work will "
                                  'be lost; the database may be left half-built and need to be redone.\n\n'
                                  'It is better to click «Stop» and wait for it to finish closing.\n\n'
                                  'Close anyway?',
    'migr_database': '📦 Database moved to {detail}',
    'migr_old_viewer_removed': '🧹 Removed the old copy of the viewer ({detail})',
    'migr_old_shortcut_removed': '🧹 Removed an old shortcut: {detail}',
    'migr_old_program_removed': '🧹 Removed the previous version of the program ({detail})',
    'migr_fields_kept': '📦 Your field selection is kept in {detail}',
    'migr_database_in_use': '⚠️ The database is still in app/ because it was open (a viewer of the previous version?). '
                            'Close it and open the program again to move it. It works the same meanwhile. ({detail})',
    'migr_state_not_moved': '⚠️ Could not move {detail}',
    'migr_old_viewer_not_removed': '⚠️ Could not delete the old copy of the viewer: {detail}',
    'migr_old_program_not_removed': '⚠️ Could not delete the previous version of the program: {detail}',
    'migr_app_not_empty': '⚠️ {detail} holds files that aren\'t the program\'s: not deleted. Check it and delete it by hand.',
    'btn_open_exports': '📁 Exports folder',
    'tip_open_exports': 'The CSV, FASTA and ZIP files the viewer exports (data/exports); emptied when the viewer opens.',
    'sc_section': 'Shortcuts',
    'sc_intro': 'To open BOLD-Local-DB or the viewer without going into this folder.',
    'sc_desktop': 'Desktop',
    'sc_menu_win32': 'Start menu',
    'sc_menu_darwin': 'Applications (Launchpad)',
    'sc_menu_linux': 'Applications menu',
    'sc_create': 'Create shortcuts',
    'sc_recreate': 'Create again',
    'sc_remove': 'Remove',
    'sc_none': 'There are no shortcuts yet.',
    'sc_made': 'Made: "BOLD-Local-DB" and "BOLD-Local-DB Viewer" on {where}.',
    'sc_and': ' and ',
    'sc_pick_one': 'Tick at least one place (desktop or menu).',
    'sc_created_log': '✅ Shortcuts created ({n})',
    'sc_removed_log': '🧹 Shortcuts removed ({n})',
    'sc_error': '❌ The shortcuts could not be created: {error}',
    'sc_ask_title': 'Shortcuts',
    'sc_ask_body': 'Create shortcuts on the desktop and in the menu?\n\n'
                   '• BOLD-Local-DB: this window, to build or update the database.\n'
                   '• BOLD-Local-DB Viewer: the viewer, straight in the browser.\n\n'
                   'You can remove them or create them later from Start.',
    'sc_ask_yes': 'Create',
    'sc_ask_no': 'Not now',
    'upd_available': 'A new version of BOLD-Local-DB is out: <b>{new}</b> (you have {cur}).',
    'upd_staged': 'Version <b>{new}</b> is downloaded and will be installed on restart.',
    'upd_see': 'See changes',
    'upd_install': 'Update',
    'upd_restart': 'Restart now',
    'upd_downloading': 'Downloading… {pct}',
    'upd_git_tip': 'Development copy (git): update it with "git pull".',
    'upd_no_asset_tip': 'This version has no download for your system: open it on the page.',
    'upd_busy': 'Wait for the running step to finish before updating.',
    'upd_ready_title': 'Update ready',
    'upd_ready_body': 'Version {v} is downloaded. BOLD-Local-DB will close and open again with it.\n\n'
                      'Your database, your exports and your field selection are not touched.',
    'upd_restart_later': 'Later',
    'upd_failed': '❌ The update could not be downloaded: {error}',
    'upd_downloaded_log': '✅ Version {v} downloaded: it is installed when the program restarts',
    'btn_uninstall': 'Uninstall…',
    'uninstall_title': 'Uninstall BOLD-Local-DB',
    'uninstall_body': 'The shortcuts will be removed and the folder holding the program will open, '
                      'for you to delete it.\n\n'
                      'Your database and your exports are in:\n{data}  ({size})\n'
                      'To keep them, move them elsewhere before deleting the folder.\n\n'
                      'Apart from the shortcuts, everything the program uses is inside that folder.',
    'uninstall_go': 'Remove shortcuts and open the folder',
    'log_session_header': '# BOLD DB Creator — session {ts}',
    'language_label': 'Language',
    'restart_required_title': 'Restart to apply',
    'restart_required_body': 'The language change applies when the app restarts.\n\n'
                             'Restart now?',
    'btn_restart_now': 'Restart now',
    'btn_restart_later': 'Later',
    'restart_blocked_body': 'A process is running. The language has been saved and '
                            'will apply the next time you open the application.',
    'copy_cancelled': 'copy cancelled',
    'log_closing_after_stop': '⏳ Stopping the process safely… '
                              'The window will close by itself when it finishes.',
    'closing_wait_title': 'Closing',
    'closing_wait_body': 'The process is already stopping. The window will close '
                         'as soon as it has finished safely.',
    'pipeline_blocked_body': 'Another step is running. Wait for it to finish or stop '
                             'it before starting the sequence.',
    'fields_verified': '✅ Fields verified against the TSV header: {n} of {configured} '
                       'configured, out of {total} available',
    'other_targz_ignored': '⚠️ There is more than one .tar.gz in data/raw/. Using the most '
                           'recent one ({name}); ignored (not deleted): {others}',
    'unsafe_tar_member': '⚠️ .tar.gz entry skipped for safety: {name}',
    'old_targz_ignored': '⚠️ {names}: a different version, older than the data already '
                         'prepared ({filt}). Ignored (not deleted).',
    'indexes_dropped': '🗑 {n} index(es) dropped because their field is no longer marked '
                       'for indexing: {names}',
    'targz_kept': '📦 Keeping {name} in data/raw/ ("Keep the .tar.gz" option)',
    'keep_targz_label': 'Keep the .tar.gz when done',
    'keep_targz_tip': 'By default the .tar.gz (~10 GB) is deleted when this step finishes.\n'
                      'If you keep it, changing the fields later does not require downloading '
                      'it again.\nYou can delete it afterwards with "Free disk space" on Home.',
    # ---- Update (run_update) ----
    'step_update_name': 'Update database',
    'step_update_tech': 'New .filt.tsv → differences → bold_records, indexes and FTS',
    'step_update_desc': 'Applies to the current database only the records that are new, modified '
                        'or removed in the new BOLD package, instead of rebuilding it. Needs Step 1 '
                        'run first with the new .tar.gz (the "Update" option on Home runs both in '
                        'sequence).\n'
                        'Input: data/processed/*.filt.tsv + data/database/bold_db.db   Output: updated data/database/bold_db.db',
    'missing_update_input': '● The new package\'s prepared data is missing — run Step 1 first '
                            '(Option 3 on Home runs both in sequence)',
    'missing_update_no_db': '● There is no database to update yet — create it first with Option 1 '
                            'or 2 on Home',
    'missing_update_old_db': '● The database was built by an older version of the program — rebuild '
                             'it once (Option 1 or 2 on Home) to be able to update it',
    'db_outdated_rebuild': '⚠️ This database was built by an older version that kept a single sequence '
                           'per specimen and left out those of its other markers. Rebuild it (Option 1 '
                           'or 2 on Home) to get all of them.',
    'update_count_mismatch': '❌ Validation failed: the package has {package:,} rows, but the DB would '
                             'end up with {stored:,} (fingerprints: {hashed:,}; expected: {expected:,}). '
                             'The update is rolled back and the DB stays as it was.',
    'pipeline3_title': 'Option 3 · Update',
    'pipeline3_label': 'Update the database',
    'pipeline3_hint': 'For BOLD\'s weekly package: applies only what changed instead of redoing '
                      'everything. Works best with an SSD and plenty of RAM; on a mechanical disk '
                      'or with little RAM the advantage shrinks. This computer: {ram}{db}. '
                      'Hover over the button for details.',
    'pipeline3_tip': 'Runs Step 1 with the new .tar.gz and applies the differences to the current '
                     'database. Indexes and full-text search are updated along the way: Steps 3 '
                     'and 4, most of a rebuild\'s time, don\'t have to be repeated.\n\n'
                     'When it pays off:\n'
                     '• With an SSD and enough RAM for the indexes to fit (around the size of the '
                     'database or more), it usually takes a fraction of a rebuild.\n'
                     '• On a mechanical hard disk, or with much less RAM than the database size, '
                     'each change costs considerably more and the advantage shrinks: a rebuild '
                     'may take about as long.\n'
                     '• If more than {pct}% of the records changed, it stops without touching '
                     'anything and recommends a rebuild.\n'
                     '• After many updates, rebuild from scratch now and then (e.g. once a '
                     'month): the indexes get fragmented.\n\n'
                     'If it fails or you stop it, the database stays exactly as it was, and the '
                     'viewer can still be used while the update runs.',
    'update_unavailable_tip': 'Your database was built by an older version of the program, in a '
                              'layout the update can\'t modify. Rebuild it once with Option 1 or 2; '
                              'after that you can update it.',
    'update_needs_package_tip': 'First drop the new BOLD .tar.gz on the area above (or copy it to '
                                'data/raw/): the update compares that package with your database.',
    'pipeline_needs_package_tip': 'First drop the .tar.gz downloaded from BOLD on the area above (or '
                                  'copy it to data/raw/).',
    'pipeline_done_needs_package_tip': 'Already done: no step is pending. To rebuild the database with a '
                                       'new BOLD version, first drop the new .tar.gz on the area above.',
    'log_step_skipped_done': '⏭ {label}: already done and its input files are gone (disk space was '
                             'freed) — skipped',
    'update_no_db_tip': 'There is no database to update yet. Create it first with Option 1 or 2; '
                        'from then on you can update it with each new BOLD package.',
    'ram_gb': '{n:.0f} GB of RAM',
    'ram_unknown': 'RAM not detected',
    'db_size_part': '; current database: {size}',
    'ctx_update': 'Update database',
    'update_needs_rebuild_once': '❌ This database was built by an older version of the program and '
                                 'doesn\'t store the record fingerprints the update needs. Rebuild it '
                                 'once ("Create the database" on Home); from then on you can update it.',
    'update_schema_mismatch': '❌ This database has layout {have} and this version of the program '
                              'writes layout {want}: it can\'t be updated without mixing the two. '
                              'Rebuild it once ("Create the database" on Home).',
    'update_fields_mismatch': '❌ The fields of the new package don\'t match the database\'s (did you '
                              'change the field selection?). It has to be rebuilt.\n'
                              '   Only in the package: {extra}\n   Only in the database: {missing}',
    'update_pass1': '⏳ Fingerprinting every record of the new package...',
    'update_pass1_progress': '\r   {n:,} records read ({pct:.0f}%)',
    'update_diff': '🔍 Comparing with the current database...',
    'update_stats': '📊 Changes: {new:,} new · {chg:,} modified · {gone:,} removed '
                    '— {pct:.2f}% of {total:,} records',
    'update_too_many': '⚠️ More than {limit}% of the records changed: rebuilding from scratch is '
                       'probably faster. Nothing was modified; use "Create the database" on Home.',
    'update_nothing': '✅ The database was already up to date: no changes to apply.',
    'update_applying': '⏳ Applying the changes (indexes{fts} included)...',
    'update_with_fts': ' and full-text search',
    'update_apply_progress': '\r   {done:,} of {total:,} changes applied ({pct:.0f}%)',
    'update_done': '✅ Database updated in {mins:.1f} min: {new:,} new, {chg:,} modified, '
                   '{gone:,} removed. Total: {total:,} records.',
    'update_interrupted': '⚠️ Update stopped. The database stays exactly as it was.',
    'update_failed_rolled_back': '   The database was not modified: the changes were discarded.',
}

def t(key, **kwargs):
    """UI text in the active language. A missing `key` returns the key itself
    (a visible fallback that reveals a forgotten translation, instead of crashing)."""
    table = STRINGS_ES if LANG == 'es' else STRINGS_EN
    template = table.get(key) or STRINGS_ES.get(key, key)
    return template.format(**kwargs) if kwargs else template


def _norm_name(name):
    """Comparison key between field names.

    BOLD's header carries 'country/ocean' while the SQLite column name is
    'col_country_ocean': without normalizing, the same field written the two
    different ways isn't recognized as the same one.
    """
    return re.sub(r"[^\w]", "_", str(name).replace('"', "").strip()).lower()


def load_fields_cfg():
    """Reads the field selection (data/state/, else the program's default).

    Never raises: a corrupt or half-written file shouldn't prevent the
    application from starting, so it falls back to the defaults.
    """
    fields  = list(DEFAULT_FIELDS)
    indexed = list(DEFAULT_INDEXED)
    try:
        with open(layout.fields_config_path(), encoding="utf-8") as f:
            raw = json.load(f)
        got = [str(x) for x in (raw.get("fields") or []) if str(x).strip()]
        if got:
            fields  = got
            indexed = [str(x) for x in (raw.get("indexed") or []) if str(x).strip()]
    except (OSError, ValueError, TypeError, AttributeError):
        pass

    # Required fields are restored even if the file was hand-edited
    have = {_norm_name(f) for f in fields}
    for req in REQUIRED_FIELDS:
        if _norm_name(req) not in have:
            fields.append(req)
            have.add(_norm_name(req))
    # Indexing a field that isn't stored is meaningless
    indexed = [c for c in indexed if _norm_name(c) in have]
    return {"fields": fields, "indexed": indexed}


def save_fields_cfg(fields, indexed):
    """Writes the selection. The file carries its own explanation: it's meant to be hand-edited."""
    payload = {
        "_comment": [
            "Campos del TSV de BOLD que se guardan en la base de datos.",
            "Lo edita el panel «Campos» de BOLD DB Creator; se puede editar a mano.",
            "'fields' define además el orden de las columnas en la tabla.",
            "'indexed' acelera la busqueda por ese campo a cambio de espacio en disco.",
            "Cambiar esto obliga a repetir el Paso 1 con el .tar.gz de BOLD.",
        ],
        "fields":  list(fields),
        "indexed": [c for c in indexed],
    }
    # Into data/state/: the program's own fields_config.json is the default,
    # and an update replaces the program folder
    os.makedirs(os.path.dirname(layout.USER_FIELDS_FILE), exist_ok=True)
    tmp = layout.USER_FIELDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, layout.USER_FIELDS_FILE)


def index_cols(cfg=None):
    """SQLite column names to index, per the current configuration."""
    cfg = cfg or load_fields_cfg()
    return [_clean_col(c) for c in cfg["indexed"]]


def fields_fingerprint(cfg=None):
    """Fingerprint of the selection, to detect that a .filt.tsv has gone stale.

    Only the field list feeds into it: changing what's indexed doesn't invalidate
    the filtered TSV (indexes are created in Step 3, on the already-built database).
    """
    cfg = cfg or load_fields_cfg()
    key = "|".join(_norm_name(c) for c in cfg["fields"])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ---- Verification against the real TSV -------------------------------------------
# Selecting by name survives BOLD reordering columns, but not renaming or
# dropping one. And a field that no longer exists produces no SQL error at
# all: it simply stops being in the database, and the failure surfaces weeks
# later in the viewer. Hence the check is explicit, and a missing required
# field halts Step 1 instead of silently degrading.

def _closest_name(name, candidates):
    """Most likely name for a field that's gone missing, or None.

    Prefix match wins over overall similarity: a real rename almost always
    keeps the beginning ('region' -> 'region_name'), whereas plain similarity
    gets swayed by a shared suffix and proposes 'ecoregion', which is a
    different, already-existing field.
    """
    pref = [c for c in candidates if c.startswith(name) or name.startswith(c)]
    if pref:
        return min(pref, key=len)
    near = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    return near[0] if near else None


def verify_fields(headers, cfg=None):
    """Compares the configured selection against the TSV's actual header.

    Returns a report of what's missing, what BOLD has added, and for each
    missing field, the closest-matching name in the header: a rename
    ('country/ocean' -> 'country_ocean_name') is detected this way instead of
    showing up as a field that vanished with no explanation.
    """
    cfg  = cfg or load_fields_cfg()
    pos  = {}
    for h in headers:
        if h:
            pos.setdefault(_norm_name(h), h)
    req   = {_norm_name(c) for c in REQUIRED_FIELDS}
    known = {_norm_name(h) for h in BOLD_FIELDS}

    present, missing, missing_req = [], [], []
    for name in cfg["fields"]:
        if _norm_name(name) in pos:
            present.append(name)
        else:
            missing.append(name)
            if _norm_name(name) in req:
                missing_req.append(name)

    # Header fields not claimed by any configured field: among them is the
    # new name of whatever got renamed.
    taken  = {_norm_name(p) for p in present}
    unused = [_norm_name(h) for h in headers if h and _norm_name(h) not in taken]
    hints  = {}
    for m in missing:
        near = _closest_name(_norm_name(m), unused)
        if near:
            hints[m] = pos.get(near) or near

    return {
        "present":     present,
        "missing":     missing,
        "missing_req": missing_req,
        "added":       [h for h in headers if h and _norm_name(h) not in known],
        "hints":       hints,
        "total":       len([h for h in headers if h]),
    }


def _hint_txt(rep, name):
    """'field' -> 'field (now called other_field?)' when there's a clear match."""
    h = rep["hints"].get(name)
    return t('field_renamed_hint', name=name, hint=h) if h else name

# ---- Text normalization (Step 1) ---------------------------------------------
# BOLD's TSV arrives with mixed text: mostly pure ASCII, but a few values come
# double-encoded ("MÃ©rida" instead of "Mérida") and others carry hard spaces
# U+00A0. They're sanitized while filtering, before they enter SQLite:
#
#   1. Repair mojibake  — UTF-8 bytes read as latin-1.
#   2. Normalize spaces — U+00A0 and other Unicode separators -> regular space.
#   3. Strip accents    — 'Mérida' -> 'Merida'.
#
# Step 3 is deliberately destructive: the viewer searches by exact match, so
# an ASCII-only database guarantees that any term typed without accents still
# reaches its value. It doesn't affect taxonomy (scientific nomenclature is
# ASCII by definition); only free-text place names.

# Values that mean "no data" and must enter SQLite as NULL.
# BOLD exports the absence of data as the literal text "None".
_NULLISH = frozenset(('', 'None'))

# Unicode separators that must be converted to a regular space
_ODD_SPACES = dict.fromkeys(
    [0x00A0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006,
     0x2007, 0x2008, 0x2009, 0x200A, 0x200B, 0x202F, 0x205F, 0x3000, 0xFEFF],
    ' '
)
# Typographic quotes and the Hawaiian ʻokina ('Hawaiʻi') -> ASCII apostrophe
_ODD_SPACES.update(dict.fromkeys([0x2018, 0x2019, 0x02BB, 0x02BC, 0x2032], "'"))


def _repair_mojibake(text):
    """Undoes UTF-8->latin-1 double-encoding. Returns the original if it doesn't apply.

    'MÃ©rida' -> 'Mérida'. Text that's already correct ('Belém') fails to decode
    and is returned intact, which is exactly what we want.
    """
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


# Latin letters whose diacritic is NOT separated by NFD (stroke, ligature, etc.):
# they must be transliterated by hand or they'd fall outside ASCII.
_LATIN_MAP = str.maketrans({
    'ø': 'o',  'Ø': 'O',  'ł': 'l',  'Ł': 'L',  'ı': 'i',  'İ': 'I',
    'đ': 'd',  'Đ': 'D',  'ð': 'd',  'Ð': 'D',  'ŧ': 't',  'Ŧ': 'T',
    'ħ': 'h',  'Ħ': 'H',  'ŋ': 'n',  'Ŋ': 'N',  'ĸ': 'k',
    'ß': 'ss', 'ẞ': 'SS', 'æ': 'ae', 'Æ': 'AE', 'œ': 'oe', 'Œ': 'OE',
    'þ': 'th', 'Þ': 'TH',
})


def _strip_accents(text):
    """'Mérida' -> 'Merida', 'Südpfalz' -> 'Sudpfalz', 'Bømlo' -> 'Bomlo'.

    Doesn't touch non-Latin scripts (place names in Chinese, for example):
    there's no accentuation to strip there, and transliterating would destroy
    the value.
    """
    text = text.translate(_LATIN_MAP)
    return ''.join(
        ch for ch in unicodedata.normalize('NFD', text)
        if not unicodedata.combining(ch)
    )


def normalize_field(value):
    """Applies the three steps to a field. Only called with non-ASCII text."""
    value = _repair_mojibake(value)
    value = value.translate(_ODD_SPACES)
    value = _strip_accents(value)
    return value.strip()


def normalize_nuc(value):
    """Strips alignment gaps ('-') and uppercases the sequence.

    Applied unconditionally (unlike normalize_field), since nuc is pure
    ASCII and would otherwise never reach the non-ASCII normalization path.
    """
    return value.replace('-', '').upper()


# Columns included in FTS5 — the same ones that are indexed (so full-text
# search covers the same fields as field-by-field search), except
# col_country_iso. The 'trigram' tokenizer can't index text under 3
# characters (there's no way to form an n-gram), and a country ISO code is
# always 2 characters: any search on that column would always return 0
# results, even when the data exists — including it wouldn't add real
# coverage, just disk space and a false "no results" impression that's
# actually a technical limitation, not an absence of data. col_nuc_basecount
# is kept: the same limit affects it for 1-2 digit values, but most real
# sequence lengths (3-4 digits) are indexable. col_nuc (DNA sequence) and
# col_coord (coordinates) are outside the indexed columns and therefore also
# outside this set. col_processid is never in the indexed list (Step 2 gives
# it its own index), but it is added here: without it a full-text search for
# a BOLD ID ("GBVA049-11") found nothing although the record was there.
_FTS_EXCLUDE = {"col_country_iso"}


def fts_cols(cfg=None):
    cols = [c for c in index_cols(cfg) if c not in _FTS_EXCLUDE and c != "col_processid"]
    return ["col_processid"] + cols

# ---- DPI-aware font scaling --------------------------------------------------

_FONT_SCALE = [1.0]   # mutable: index-0 is set once at startup before App()

def _fs(pt: float) -> str:
    """Return a scaled CSS font-size string (e.g. '13pt' → '22pt' on 4K native)."""
    return f"{max(1, round(pt * _FONT_SCALE[0]))}pt"

def _px(n: float) -> int:
    """Return a layout pixel value scaled for the current DPI."""
    return max(1, round(n * _FONT_SCALE[0]))


def _compute_dpi_scale(app) -> float:
    """
    Only scales on genuinely high-DPI screens (>150 physical DPI) where the
    OS isn't already compensating (devicePixelRatio < 1.05, i.e. 100% scaling).
    Returns 1.0 on any standard 1080p screen.
    """
    screen = app.primaryScreen()
    if screen is None:
        return 1.0
    if screen.devicePixelRatio() < 1.05 and screen.physicalDotsPerInch() > 150:
        return min(screen.physicalDotsPerInch() / 96.0, 3.0)
    return 1.0

# ---- Utilities ------------------------------------------------------------------

class _TrackedFile:
    """Binary file wrapper that reports progress while reading compressed bytes."""
    def __init__(self, path, cb, scale):
        self._f     = open(path, "rb")
        self._total = os.path.getsize(path)
        self._cb    = cb
        self._scale = scale
        self._pos   = 0
        self._last  = -1.0
    def read(self, n=-1):
        data = self._f.read(n)
        self._pos += len(data)
        if self._total:
            # gzip reads in small blocks: without this threshold, hundreds of
            # thousands of cross-thread signals would fire over a multi-GB .tar.gz
            pct = min(self._pos / self._total * self._scale, self._scale)
            if pct - self._last >= 0.25:
                self._last = pct
                self._cb(pct)
        return data
    def seek(self, *a): return self._f.seek(*a)
    def tell(self):     return self._f.tell()
    def close(self):    self._f.close()
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def _clean_col(name):
    return "col_" + re.sub(r"[^\w]", "_", name.replace('"', ""))


def _connect_retry(path, pragmas, log, attempts=6, delay=1.5, **connect_kwargs):
    """Opens the DB and applies the initial PRAGMAs, retrying if it's busy.

    sqlite3.connect() is lazy: it doesn't touch the file until the first real
    operation, so "database is locked" shows up on the first PRAGMA, not on
    connect(). On Windows, antivirus or the search indexer can hold the file
    for a fraction of a second right after it's written — enough to fail here
    even though the file is free an instant later. Without this retry, that
    transient lock would take down the whole step over something that was no
    longer true by the time the user read the error.
    """
    for attempt in range(1, attempts + 1):
        conn = sqlite3.connect(path, **connect_kwargs)
        try:
            for pragma in pragmas:
                conn.execute(pragma)
            return conn
        except sqlite3.OperationalError as e:
            conn.close()
            if "locked" not in str(e).lower() or attempt == attempts:
                raise
            log(t('db_busy_retrying', delay=f'{delay:.0f}', attempt=attempt, attempts=attempts))
            time.sleep(delay)


DB_META_TABLE = db_contract.META_TABLE


def _write_db_meta(conn, values):
    """Stores facts about the build inside the database itself (key/value).

    The viewer reads record_count from here instead of running COUNT(*)
    over ~20M rows on every page load, and the data travels with the .db
    if it's copied elsewhere.
    """
    conn.execute('CREATE TABLE IF NOT EXISTS {0} (key TEXT PRIMARY KEY, value TEXT)'
                 .format(DB_META_TABLE))
    conn.executemany('INSERT OR REPLACE INTO {0} (key, value) VALUES (?, ?)'
                     .format(DB_META_TABLE),
                     [(k, str(v)) for k, v in values.items()])
    conn.commit()


# ---- Building next to the current database ---------------------------------------
#
# Step 2 builds the new database in "<db>.building" and Steps 3 and 4 index
# it there. It replaces the current one only when the run ends (the whole
# Home option, or the single step that was run), so the viewer never sees a
# half-built or unindexed database, and a rebuild that fails leaves the
# current one untouched. The update (run_update) doesn't need this: it works
# on the current database in a single transaction.

def _build_path(db_path):
    return db_path + db_contract.BUILD_SUFFIX


def _remove_db_files(path):
    """Deletes a database file together with its journal/WAL files."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        try: os.remove(path + suffix)
        except OSError: pass


def _target_db(cfg):
    """Database Steps 3 and 4 work on: the one Step 2 built in this run while
    it hasn't replaced the current one yet, otherwise the current one."""
    db_path = cfg.get("db_file", DB_FILE)
    build   = _build_path(db_path)
    return build if os.path.exists(build) else db_path


def _warn_build_space(log, db_path, tsv_path):
    """Warns when the disk looks too small to hold the new database next to
    the current one. Only a warning: the estimate is rough, and running out
    of space fails the step without touching the current database."""
    try:
        need = max(os.path.getsize(db_path) if os.path.exists(db_path) else 0,
                   os.path.getsize(tsv_path))
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free
    except OSError:
        return
    if free < need:
        log(t('db_build_space_warn', free=_fmt_size(free), need=_fmt_size(need)))


def _build_is_complete(path):
    """Step 2 writes the record count to META_TABLE as its last action, so a
    build without it was cut short."""
    import pathlib
    try:
        conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro",
                               uri=True, timeout=1)
        try:
            return "record_count" in db_contract.read_meta(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _promote_pending_build(log):
    """Puts a finished build in place of the current database.

    Called when nothing is running: at startup and before and after every
    run. An unfinished build (a crash or forced close during Step 2) is
    deleted. Returns False only when a finished build couldn't replace the
    current database: on Windows a file can't be replaced while another
    program has it open, which the viewer does for the length of a search
    or an export.
    """
    live  = layout.database_path()
    build = _build_path(live)
    if not os.path.exists(build):
        return True
    name = os.path.basename(build)
    if not _build_is_complete(build):
        _remove_db_files(build)
        log(t('db_build_orphan_removed', name=name))
        return True
    try:
        # A WAL left by an update whose last switch back to DELETE failed
        # (see run_update) belongs to the old file: next to the new one,
        # SQLite would apply it there.
        if os.path.exists(live + "-wal"):
            conn = sqlite3.connect(live, timeout=2)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode = DELETE")
            finally:
                conn.close()
        for attempt in range(8):
            try:
                os.replace(build, live)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.5)
    except (OSError, sqlite3.Error) as e:
        log(t('db_build_promote_failed', name=name, error=e))
        return False
    for suffix in ("-journal", "-wal", "-shm"):
        try: os.remove(live + suffix)
        except OSError: pass
    _invalidate_status()
    log(t('db_build_promoted', name=db_contract.DB_FILENAME))
    return True


# Fingerprint of every stored record, written by Step 2 and kept up to date by
# the update (run_update), which compares it against the new package. One row
# per row of bold_records (rid = its rowid), with the key the update matches
# records by: processid + marker. Exact text (BINARY), unlike the viewer's
# case-insensitive searches, so two IDs differing in case are never merged.
ROW_HASH_TABLE = db_contract.ROW_HASH_TABLE
_ROW_HASH_DDL  = ("CREATE TABLE {0} (rid INTEGER PRIMARY KEY, processid TEXT NOT NULL, "
                  "marker TEXT NOT NULL, h INTEGER NOT NULL)".format(ROW_HASH_TABLE))

# Index on processid, built by Step 2 (see run_step2)
PROCESSID_INDEX = "idx_processid"


def _row_key_idx(cols_clean):
    """Positions of processid and marker_code among the columns (None if absent)."""
    return tuple(cols_clean.index(c) if c in cols_clean else None
                 for c in ("col_processid", "col_marker_code"))


def _row_key(values, key_idx):
    """(processid, marker) of a row; a missing value counts as ''."""
    return tuple("" if i is None or values[i] is None else values[i] for i in key_idx)


def _tsv_row_values(row, n_cols):
    """A .filt.tsv row as it's stored: missing columns, empty strings and
    BOLD's literal "None" become None (SQL NULL). Shared by Step 2 and the
    update so both fingerprint exactly the same values."""
    if len(row) == n_cols:
        return tuple(None if v in _NULLISH else v for v in row)
    return tuple(
        None if (row[i] if i < len(row) else "") in _NULLISH else row[i]
        for i in range(n_cols)
    )


def _hash_order(cols_clean):
    """Column order for the fingerprint: by name, so the same record hashes
    the same even if the column order of the TSV changes."""
    return sorted(range(len(cols_clean)), key=lambda i: cols_clean[i])


def _row_hash(values, order):
    """64-bit fingerprint of a record's values (signed, to fit SQLite's INTEGER)."""
    data = "\x1f".join("" if values[i] is None else values[i] for i in order)
    return int.from_bytes(hashlib.blake2b(data.encode("utf-8"), digest_size=8).digest(),
                          "big", signed=True)


def _interrupt_on_stop(conn, stop):
    """Aborts the SQL statement in progress as soon as Stop is pressed.

    A single CREATE INDEX over ~20M rows takes minutes, and Stop was only
    checked between statements. Returns an Event the caller sets when done,
    which ends the watcher thread. conn.interrupt() is safe to call from
    another thread; the interrupted statement raises 'interrupted'.
    """
    done = threading.Event()
    if stop is None:
        return done

    def _watch():
        while not done.wait(0.5):
            if stop.is_set():
                try: conn.interrupt()
                except Exception: pass
                return

    threading.Thread(target=_watch, daemon=True).start()
    return done


# ---- Error handling ---------------------------------------------------------------

_ERROR_MAP = [
    (FileNotFoundError,          'err_file_not_found',      'err_action_check_files'),
    (PermissionError,            'err_permission',          'err_action_close_file'),
    (ModuleNotFoundError,        None,   None),
    (MemoryError,                'err_memory',              'err_action_close_apps'),
    (sqlite3.OperationalError,   'err_db_error',            'err_action_check_db'),
    (UnicodeDecodeError,         'err_encoding',            'err_action_utf8'),
    (KeyError,                   None,   'err_action_check_format'),
    (IndexError,                 None,   'err_action_check_format'),
    (ValueError,                 'err_unexpected_value',    'err_action_check_values'),
    (TimeoutError,               'err_timeout',             'err_action_check_process'),
]


def _cause_str(e):
    raw = str(e)
    if isinstance(e, ModuleNotFoundError):
        mod = getattr(e, "name", None) or raw
        return t('err_module_not_installed', mod=mod)
    if isinstance(e, (KeyError, IndexError)):
        return t('err_column_not_found', raw=raw)
    for exc_type, cause_key, _ in _ERROR_MAP:
        if isinstance(e, exc_type) and cause_key:
            return t(cause_key)
    return t('err_unexpected_type', type=type(e).__name__)


def _fmt_error(e, context=""):
    import traceback as _tb
    raw = str(e)
    if isinstance(e, ModuleNotFoundError):
        mod    = getattr(e, "name", None) or raw
        cause  = t('err_module_not_installed', mod=mod)
        action = t('err_action_pip_install', mod=mod)
    elif isinstance(e, (KeyError, IndexError)):
        cause  = t('err_column_not_found', raw=raw)
        action = t('err_action_check_format')
    else:
        cause  = _cause_str(e)
        action = next((t(a) for et, _, a in _ERROR_MAP if isinstance(e, et) and a),
                      t('err_action_check_error_log'))
    try:
        _logs_dir = layout.LOGS_DIR
        os.makedirs(_logs_dir, exist_ok=True)
        with open(os.path.join(_logs_dir, "error.log"), "a", encoding="utf-8") as _f:
            from datetime import datetime as _dt
            _f.write(f"\n[{_dt.now().isoformat()}]")
            if context:
                _f.write(f" — {context}")
            _f.write(f"\n{_tb.format_exc()}\n")
    except Exception:
        pass
    lines = [f"❌ {cause}"]
    if context:
        lines.append(t('err_step_line', context=context))
    short = (raw[:200] + "…") if len(raw) > 200 else raw
    if short and short.lower() != cause.lower():
        lines.append(f"   {short}")
    lines.append(f"   → {action}")
    lines.append(f"   ({t('err_detail_saved')})")
    return "\n".join(lines)


# ---- Step 1: Filter TSV --------------------------------------------------------

def _cleanup_raw_files(log, gz_path, tsv_path, keep_gz=False):
    """Deletes the .tar.gz and the unfiltered TSV once filtering is complete.

    keep_gz (the "Keep the .tar.gz" option in Step 1): the package stays in
    data/raw/, so changing the field selection later re-filters from it
    instead of requiring a new ~10 GB download. "Free disk space" on the
    Home screen can still delete it afterwards.
    """
    if keep_gz and gz_path and os.path.exists(gz_path):
        log(t('targz_kept', name=os.path.basename(gz_path)))
        gz_path = None
    for path, label in [(gz_path, ".tar.gz"), (tsv_path, t('unfiltered_tsv'))]:
        if path and os.path.exists(path):
            try:
                os.remove(path)
                log(t('deleted_processed', label=label, name=os.path.basename(path)))
            except OSError as e:
                log(t('could_not_delete_named', name=os.path.basename(path), error=str(e)))


_FIELDS_MARKER = "../data/processed/fields_source.txt"

_PKG_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _package_date(name):
    """Release date in a BOLD package name ("BOLD_Public.02-Oct-2026"), or None."""
    m = _PKG_DATE_RE.search(os.path.basename(name))
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


def _package_sort_key(path):
    """Newest package first: by the release date in its name, falling back to
    the file's modification time for names without one."""
    return _package_date(path) or datetime.fromtimestamp(os.path.getmtime(path))


def _resolve_fields(headers, log):
    """Matches the configured selection against the TSV's real header.

    Returns (columns, filter_index), where columns is a list of
    (name_in_header, index). Returns (None, None) if the filter field is
    missing: without it there's no way to decide which rows to keep.
    """
    pos = {}
    for i, h in enumerate(headers):
        pos.setdefault(_norm_name(h), i)

    cfg = load_fields_cfg()
    rep = verify_fields(headers, cfg)

    # ---- Pre-filtering verification -----------------------------------------
    if rep["added"]:
        log(t('new_fields_found', n=len(rep["added"]), fields=", ".join(rep["added"])))
        log(t('can_add_from_panel'))
    if rep["missing_req"]:
        # Continuing here produces a database missing the columns the viewer
        # takes for granted: it doesn't fail now, it fails exporting FASTA a month later.
        log(t('missing_required_fields', fields=", ".join(_hint_txt(rep, m) for m in rep["missing_req"])))
        log(t('header_mismatch_hint'))
        return None, None
    if rep["missing"]:
        log(t('missing_configured_fields', fields=", ".join(_hint_txt(rep, m) for m in rep["missing"])))

    cols = []
    for name in rep["present"]:
        i = pos[_norm_name(name)]
        cols.append((headers[i], i))

    filt_idx = pos.get(_norm_name(FILTER_FIELD))
    if filt_idx is None:
        log(t('missing_filter_field', field=FILTER_FIELD))
        return None, None
    if not cols:
        log(t('no_configured_fields_exist'))
        return None, None
    log(t('fields_verified', n=len(cols), configured=len(cfg["fields"]), total=rep["total"]))
    return cols, filt_idx


def run_step1(log, progress, cfg):
    os.chdir(PROJECT_ROOT)
    try:
        os.makedirs("../data/processed", exist_ok=True)

        # ---- Version detection and cleanup of obsolete files -----------------
        gz_files   = sorted(glob.glob("../data/raw/*.tar.gz"), key=_package_sort_key, reverse=True)
        gz_path    = gz_files[0] if gz_files else None
        gz_version = None

        # A package older than the .filt.tsv already on disk, and of another
        # version, is a leftover (old packages are no longer deleted, see
        # below), not a new download: letting it "win" would delete the newer
        # filtered data as if it were obsolete.
        _filt_now = glob.glob("../data/processed/*.filt.tsv")
        if gz_path and _filt_now:
            _filt_new  = max(_filt_now, key=os.path.getmtime)
            _filt_ver  = os.path.basename(_filt_new)[:-len(".filt.tsv")]
            _gz_name   = os.path.basename(gz_path)
            _gz_ver    = _gz_name[:-len(".tar.gz")]
            # By the release date in the name when both have one (BOLD names
            # packages "BOLD_Public.02-Oct-2026"); the file's modification
            # time is only a fallback: moving or extracting a file can keep
            # an old one.
            _gz_date, _filt_date = _package_date(_gz_name), _package_date(_filt_ver)
            if _gz_date and _filt_date:
                _gz_is_older = _gz_date < _filt_date
            else:
                _gz_is_older = os.path.getmtime(gz_path) <= os.path.getmtime(_filt_new)
            if _gz_ver != _filt_ver and _gz_is_older:
                log(t('old_targz_ignored', filt=os.path.basename(_filt_new),
                      names=", ".join(os.path.basename(f) for f in gz_files)))
                gz_files, gz_path = [], None

        if gz_path:
            # The other packages are no longer deleted: "most recent" is by
            # modification time, and copying a file in resets it, so bringing
            # in an older package made it the "newest" and the actually newer
            # 10 GB download was deleted without asking.
            if gz_files[1:]:
                log(t('other_targz_ignored', name=os.path.basename(gz_path),
                      others=", ".join(os.path.basename(f) for f in gz_files[1:])))
            name = os.path.basename(gz_path)
            gz_version = name[:-len(".tar.gz")] if name.endswith(".tar.gz") else os.path.splitext(name)[0]
            os.makedirs(_PKG_DIR, exist_ok=True)
            for f in glob.glob(os.path.join(_PKG_DIR, "*.tsv")):
                if os.path.splitext(os.path.basename(f))[0] != gz_version:
                    try: os.remove(f)
                    except OSError: pass
            for f in glob.glob("../data/processed/*.tsv"):
                fname = os.path.basename(f)
                ver   = fname[:-len(".filt.tsv")] if fname.endswith(".filt.tsv") else os.path.splitext(fname)[0]
                if ver != gz_version:
                    log(t('deleting_old_version_file', name=fname))
                    try: os.remove(f)
                    except OSError as e: log(t('could_not_delete', error=str(e)))

        # ---- Check whether Step 1 is already complete --------------------------
        raw_tsv       = [f for f in glob.glob("../data/processed/*.tsv") if not f.endswith(".filt.tsv")]
        existing_filt = glob.glob("../data/processed/*.filt.tsv")
        if existing_filt:
            # Was that .filt.tsv made with the current field selection? A TSV
            # that predates this check has no marker: it's accepted as-is, it
            # wouldn't make sense to force 40 minutes of re-filtering over that.
            stale = False
            if os.path.exists(_FIELDS_MARKER):
                try:
                    stale = open(_FIELDS_MARKER).read().strip() != fields_fingerprint()
                except OSError:
                    pass
            if stale and (raw_tsv or gz_path):
                log(t('fields_changed_refiltering'))
                for f in existing_filt:
                    try: os.remove(f)
                    except OSError as e: log(t('could_not_delete', error=str(e)))
                # The database and the FTS index are left built with the old
                # fields: without deleting their markers, Steps 2 and 4 would
                # skip themselves and the field change would never reach the database.
                for m in ("../data/processed/db_source.txt",
                          "../data/processed/fts_source.txt"):
                    try: os.remove(m)
                    except OSError: pass
                existing_filt = []
            elif stale:
                log(t('fields_changed_no_targz'))

        if existing_filt:
            filt_best  = max(existing_filt, key=os.path.getsize)
            tsv_marker = os.path.basename(filt_best).replace(".filt.tsv", ".tsv")
            os.makedirs(_PKG_DIR, exist_ok=True)
            for f in glob.glob(os.path.join(_PKG_DIR, "*.tsv")):
                try: os.remove(f)
                except OSError: pass
            open(os.path.join(_PKG_DIR, tsv_marker), "w").close()
            log(t('filtered_tsv_exists', name=os.path.basename(filt_best)))
            _cleanup_raw_files(log, gz_path, max(raw_tsv, key=os.path.getsize) if raw_tsv else None,
                               keep_gz=cfg.get("keep_targz", False))
            progress(100)
            return True

        # ---- Sub-step A: Extract .gz -> .tsv ------------------------------------
        if raw_tsv:
            archivo_tsv = max(raw_tsv, key=os.path.getsize)
            log(t('tsv_already_extracted', name=os.path.basename(archivo_tsv)))
            progress(50)
        else:
            if not gz_path:
                log(t('no_targz_found'))
                return False
            compressed_mb = os.path.getsize(gz_path) / (1024 ** 2)
            log(t('decompressing', name=os.path.basename(gz_path), mb=compressed_mb))

            _DCHUNK    = 64 * 1024
            _LOG_EVERY = 50 * 1024 * 1024
            extracted  = 0
            _log_at    = 0
            stop          = cfg.get("_stop")
            extract_abort = False

            # Don't use context managers: on abort we close tar/_tf in a daemon
            # thread so as not to block; closing gzip can read the entire .gz
            # to verify the CRC, which for 10 GB takes about 1 minute.
            _tf  = _TrackedFile(gz_path, progress, 49)
            _tar = tarfile.open(fileobj=_tf, mode="r:gz")
            out_root = os.path.realpath("../data/processed")
            try:
                for member in _tar:
                    if extract_abort:
                        break
                    # Only regular files and folders, and only inside
                    # data/processed. normpath + lstrip(os.sep) didn't stop a
                    # '../' name from writing outside it, and links or devices
                    # were extracted as-is. BOLD's package only holds plain
                    # files, so nothing legitimate is lost.
                    out_path = os.path.realpath(os.path.join(out_root, member.name))
                    try:
                        inside = os.path.commonpath([out_root, out_path]) == out_root
                    except ValueError:   # another drive (absolute name on Windows)
                        inside = False
                    if not inside or not (member.isfile() or member.isdir()):
                        log(t('unsafe_tar_member', name=member.name))
                        continue
                    if member.isdir():
                        os.makedirs(out_path, exist_ok=True)
                        continue
                    f_in = _tar.extractfile(member)
                    if f_in is None:
                        continue
                    os.makedirs(os.path.dirname(out_path), exist_ok=True)
                    with open(out_path, "wb") as f_out:
                        while True:
                            if stop and stop.is_set():
                                extract_abort = True
                                break
                            chunk = f_in.read(_DCHUNK)
                            if not chunk:
                                break
                            f_out.write(chunk)
                            extracted += len(chunk)
                            if extracted >= _log_at:
                                log(t('extracted_progress', gb=extracted / (1024**3)))
                                _log_at = extracted + _LOG_EVERY
                    if extract_abort:
                        break
            finally:
                if extract_abort:
                    # Close in a daemon thread — doesn't block the worker thread
                    def _bg_close(t, f):
                        try: t.close()
                        except Exception: pass
                        try: f.close()
                        except Exception: pass
                    threading.Thread(target=_bg_close, args=(_tar, _tf),
                                     daemon=True).start()
                else:
                    try: _tar.close()
                    except Exception: pass
                    try: _tf.close()
                    except Exception: pass

            if extract_abort:
                for f in glob.glob("../data/processed/*.tsv"):
                    if not f.endswith(".filt.tsv"):
                        try: os.remove(f)
                        except OSError: pass
                log(t('extraction_interrupted'))
                return False

            for f in glob.glob("../data/processed/*.json"):
                os.remove(f)
            log(t('decompression_finished', gb=extracted / (1024 ** 3)))

            raw_tsv = [f for f in glob.glob("../data/processed/*.tsv")
                       if not f.endswith(".filt.tsv")]
            if not raw_tsv:
                log(t('no_tsv_found'))
                return False
            if len(raw_tsv) > 1:
                log(t('multiple_tsv_found'))
                raw_tsv.sort(key=os.path.getsize, reverse=True)
            archivo_tsv = raw_tsv[0]

        tsv_filename = os.path.basename(archivo_tsv)
        log(t('tsv_file_label', name=tsv_filename))

        os.makedirs(_PKG_DIR, exist_ok=True)
        for f in glob.glob(os.path.join(_PKG_DIR, "*.tsv")):
            os.remove(f)
        open(os.path.join(_PKG_DIR, tsv_filename), "w").close()

        # ---- Sub-step B: Filter .tsv -> .filt.tsv ------------------------------
        # Only the extension is replaced: str.replace() changed every ".tsv"
        # in the path, folders included
        archivo_filtrado = archivo_tsv[:-len(".tsv")] + ".filt.tsv"
        file_size = os.path.getsize(archivo_tsv)
        log(t('size_mb', mb=file_size / (1024 * 1024)))
        log(t('filtering_info'))

        _IO_BUF   = 64 * 1024 * 1024
        _BATCH    = 50_000
        _STOP_CHK = 5_000

        # The header is read and verified BEFORE creating the output file:
        # aborting with the .filt.tsv already open left a 0-byte file that the
        # rest of the application mistook for a finished Step 1.
        with open(archivo_tsv, "r", encoding="utf-8", errors="replace") as fh:
            header_line = fh.readline()
        if not header_line:
            log(t('empty_tsv_file'))
            return False
        headers = header_line.rstrip('\r\n').split('\t')
        sel_cols, filt_idx = _resolve_fields(headers, log)
        if sel_cols is None:
            return False
        col_idx = [i for _, i in sel_cols]
        # split only up to the farthest column we need; the rest of the line
        # stays as a single undivided string -> avoids ~100 allocations per row
        _MAX_COL = max(col_idx + [filt_idx])
        nuc_pos = col_idx.index(filt_idx) if filt_idx in col_idx else None

        stop        = cfg.get("_stop")
        interrupted = False
        processed   = written = 0
        # What's left out, and why: rows without a sequence (or with only Ns), and
        # the counts are reported so nothing goes missing unnoticed
        no_seq = only_n = malformed = 0
        with open(archivo_tsv, "r", encoding="utf-8", errors="replace", buffering=_IO_BUF) as fin, \
             open(archivo_filtrado, "w", encoding="utf-8", newline="", buffering=_IO_BUF) as fout:
            writer = csv.writer(fout, delimiter="\t")

            fin.readline()   # skip the header, already read and verified
            writer.writerow([name for name, _ in sel_cols])
            written += 1
            processed += 1

            batch = []
            for line in fin:
                processed += 1
                parts = line.rstrip('\r\n').split('\t', _MAX_COL + 1)
                if len(parts) > filt_idx:
                    val = parts[filt_idx].strip()
                    # Normalized before deciding: a sequence made only of
                    # alignment gaps ('---') is non-empty as text but empty
                    # once the gaps go, and used to get in as a record
                    # without a sequence.
                    nuc = normalize_nuc(val) if val != "None" else ""
                    if nuc and not nuc.strip("N"):
                        # Only Ns: a placeholder with no information, kept
                        # out like a record without a sequence
                        only_n += 1
                    elif nuc:
                        # strip() on every row, not just the non-ASCII ones that
                        # go through normalize_field: otherwise a stray space
                        # around a plain-ASCII value made exact-match search miss it.
                        row = tuple(parts[c].strip() if c < len(parts) else "" for c in col_idx)
                        # str.isascii() is O(1) in CPython (a flag in the object's
                        # header), so the vast majority of rows — already pure
                        # ASCII — pay nothing for this check.
                        if not line.isascii():
                            row = tuple(normalize_field(v) for v in row)
                        if nuc_pos is not None:
                            row = row[:nuc_pos] + (nuc,) + row[nuc_pos + 1:]
                        batch.append(row)
                        written += 1
                        if len(batch) >= _BATCH:
                            writer.writerows(batch)
                            batch.clear()
                    else:
                        no_seq += 1
                elif line.strip():
                    # Too few columns to even reach the sequence: a line
                    # broken by a stray line break inside a field
                    malformed += 1
                if processed % _STOP_CHK == 0 and stop and stop.is_set():
                    interrupted = True
                    break
                if processed % 50_000 == 0:
                    pct = min(fin.buffer.tell() / file_size * 100, 100)
                    progress(50 + pct / 2)
                    if processed % 500_000 == 0:
                        log(t('rows_processed_pct', n=processed, pct=pct))

            if not interrupted and batch:
                writer.writerows(batch)

        if interrupted:
            try: os.remove(archivo_filtrado)
            except OSError: pass
            log(t('filtering_interrupted'))
            return False

        total_filtrado = written - 1
        with open("../data/processed/records.number", "w") as f:
            f.write(str(total_filtrado))
        # Records which selection this .filt.tsv was made with
        try:
            with open(_FIELDS_MARKER, "w") as f:
                f.write(fields_fingerprint())
        except OSError:
            pass
        log(t('filtered_records', n=total_filtrado))
        log(t('filter_summary', read=processed - 1, kept=total_filtrado, no_seq=no_seq))
        if only_n:
            log(t('filter_only_n', n=only_n))
        if malformed:
            log(t('filter_malformed', n=malformed))
        log(t('file_label', name=os.path.basename(archivo_filtrado)))
        _cleanup_raw_files(log, gz_path, archivo_tsv, keep_gz=cfg.get("keep_targz", False))
        progress(100)
        return True
    except Exception as e:
        log(_fmt_error(e, t('ctx_generate_tsv')))
        return False

# ---- Step 2: TSV -> SQLite -------------------------------------------------------

def run_step2(log, progress, cfg):
    os.chdir(PROJECT_ROOT)
    try:
        archivos = glob.glob("../data/processed/*.filt.tsv")
        if not archivos:
            log(t('no_filt_tsv_found'))
            return False
        archivo_tsv = max(archivos, key=os.path.getsize)

        # Skip if the .db was already built from this same .filt.tsv
        _marker = "../data/processed/db_source.txt"
        _db_path = cfg.get("db_file", DB_FILE)
        if os.path.exists(_db_path) and os.path.exists(_marker):
            try:
                if open(_marker).read().strip() == os.path.basename(archivo_tsv):
                    conn_chk = sqlite3.connect(_db_path, timeout=3)
                    cur_chk  = conn_chk.cursor()
                    cur_chk.execute("SELECT 1 FROM bold_records LIMIT 1")
                    # ...and in this version's layout: a database from before
                    # schema 2 is missing sequences and must be rebuilt even
                    # from the same .filt.tsv
                    _has_data = (bool(cur_chk.fetchone()) and
                                 db_contract.schema_version(conn_chk) == db_contract.SCHEMA_VERSION)
                    conn_chk.close()
                    if _has_data:
                        log(t('db_already_built', name=os.path.basename(archivo_tsv)))
                        progress(100)
                        return True
            except Exception:
                pass

        log(t('file_label', name=os.path.basename(archivo_tsv)))
        log(t('size_mb', mb=os.path.getsize(archivo_tsv) / (1024*1024)))

        total_lines = 0
        if os.path.exists("../data/processed/records.number"):
            try:
                total_lines = int(open("../data/processed/records.number").read().strip())
                log(t('total_records_expected', n=total_lines))
            except Exception:
                pass
        if total_lines == 0:
            log(t('records_number_not_found'))
            progress(-1)

        with open(archivo_tsv, "r", encoding="utf-8", errors="replace", newline="") as _f:
            raw_headers = next(csv.reader(_f, delimiter="\t"), [])
        log(t('columns_detected', n=len(raw_headers)))
        mapeo = {col: _clean_col(col) for col in raw_headers}

        # Build the schema with correct types and constraints. Lookup is by
        # normalized name: BOLD writes 'country/ocean' where COL_DEFS says
        # 'country_ocean', and without normalizing, the field always fell
        # back to the generic type.
        _types   = {_norm_name(k): v for k, v in COL_DEFS.items()}
        cols_sql = []
        for orig, clean in mapeo.items():
            tipo = _types.get(_norm_name(orig), "TEXT COLLATE NOCASE")
            cols_sql.append("{} {}".format(clean, tipo))

        # The new database is built next to the current one and only replaces
        # it when the run ends (see _promote_pending_build). Until then the
        # viewer keeps serving the previous version, and a failed or stopped
        # build leaves it untouched. Before, the table was dropped and refilled
        # in place: the viewer showed a half-empty database for hours, and a
        # stop deleted the only copy.
        build_path = _build_path(_db_path)
        _remove_db_files(build_path)   # leftovers of an earlier, unfinished build
        _warn_build_space(log, _db_path, archivo_tsv)
        log(t('db_build_target', name=os.path.basename(build_path)))

        # Must exist before the try/finally: the finally block reads them, and an
        # early failure (e.g. CREATE TABLE) would raise NameError, hiding the real cause
        stopped = False
        built   = False
        # OFF instead of MEMORY: the table is built from scratch in a new file
        # that's deleted if the step fails or is interrupted, so there's nothing
        # to roll back. Avoids writing the journal for every transaction.
        conn = _connect_retry(build_path, [
            "PRAGMA synchronous   = OFF",
            "PRAGMA journal_mode  = OFF",
            "PRAGMA cache_size    = -500000",   # ~500 MB cache
            "PRAGMA temp_store    = MEMORY",
            "PRAGMA locking_mode  = EXCLUSIVE",
            "PRAGMA mmap_size     = 30000000000",
        ], log)
        try:
            # db_source.txt and fts_source.txt aren't touched until the import
            # finishes: they describe the current database, which a build cut
            # short (crash, forced close, Stop) no longer damages.
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS bold_records")
            # The previous build's facts (record count...) stop being true now
            cur.execute("DROP TABLE IF EXISTS {0}".format(DB_META_TABLE))
            cur.execute("DROP TABLE IF EXISTS {0}".format(ROW_HASH_TABLE))
            cur.execute("CREATE TABLE bold_records ({})".format(", ".join(cols_sql)))
            cur.execute(_ROW_HASH_DDL)
            conn.commit()
            log(t('table_created'))

            log(t('importing_data'))
            chunk_n      = cfg.get("chunksize", CHUNKSIZE)
            cols_clean   = list(mapeo.values())
            n_cols       = len(cols_clean)
            # Every row of the .filt.tsv is a sequence and gets in: there's no
            # key to collide on. Each row is one sequence, not one specimen, so
            # a processid repeats once per marker (COI-5P + 16S, matK + rbcLa...)
            # and, rarely, within the same marker. Before, processid was the
            # PRIMARY KEY and INSERT OR IGNORE kept whichever sequence of the
            # specimen came first in the file, dropping ~400,000 of them.
            # The rowid is given explicitly (1, 2, 3... in file order) so the
            # fingerprint table can point at each row without asking SQLite.
            insert_q     = "INSERT INTO bold_records (rowid, {0}) VALUES (?, {1})".format(
                               ", ".join('"{0}"'.format(c) for c in cols_clean),
                               ",".join(["?"] * n_cols))
            hash_q       = "INSERT INTO {0} (rid, processid, marker, h) VALUES (?, ?, ?, ?)".format(
                               ROW_HASH_TABLE)
            key_idx      = _row_key_idx(cols_clean)
            hash_order   = _hash_order(cols_clean)

            _IO_BUF    = 8 * 1024 * 1024   # 8 MB read buffer
            _COMMIT_N  = 5                  # commit every N chunks
            processed  = chunk_count = 0
            # Rows with more or fewer columns than the header: stored anyway
            # (missing values as NULL, as before), but counted and reported,
            # since Step 1 always writes the full set
            irregular  = 0
            stop       = cfg.get("_stop")
            _last_ui   = 0.0

            with open(archivo_tsv, "r", encoding="utf-8", errors="replace",
                      newline="", buffering=_IO_BUF) as fin:
                reader = csv.reader(fin, delimiter="\t")
                next(reader)   # skip header
                batch  = []
                hbatch = []
                for row in reader:
                    if stop and stop.is_set():
                        stopped = True
                        break
                    # Missing columns -> None; empty string or the literal text
                    # "None" -> None (SQL NULL). BOLD exports the absence of
                    # data as the string "None", not as an empty field: without
                    # this conversion it would be stored as if it were a real
                    # value (13.9M records with col_species = 'None'), breaking
                    # the FASTA taxonomic fallback and the With/Without data filters.
                    # The TSV was generated by Step 1, so almost every row carries
                    # exactly n_cols: this avoids the per-column index check.
                    if len(row) != n_cols:
                        irregular += 1
                    vals = _tsv_row_values(row, n_cols)
                    processed += 1
                    batch.append((processed,) + vals)
                    hbatch.append((processed,) + _row_key(vals, key_idx) +
                                  (_row_hash(vals, hash_order),))
                    if len(batch) >= chunk_n:
                        cur.executemany(insert_q, batch)
                        cur.executemany(hash_q, hbatch)
                        batch.clear()
                        hbatch.clear()
                        chunk_count += 1
                        if chunk_count % _COMMIT_N == 0:
                            conn.commit()
                        # Refresh capped at ~4 Hz. Every cross-thread signal makes
                        # the UI thread do work (HTML parsing in the QTextEdit);
                        # emitting one per batch flooded it without the user
                        # being able to read anything at that speed.
                        _now = time.monotonic()
                        if _now - _last_ui >= 0.25:
                            _last_ui = _now
                            if total_lines > 0:
                                pct = min(processed / total_lines * 100, 100)
                                progress(min(pct, 99))
                                log(t('import_progress', processed=processed,
                                    total=total_lines, pct=pct))
                            else:
                                log(t('import_progress_nototal', processed=processed))
                if batch:
                    cur.executemany(insert_q, batch)
                    cur.executemany(hash_q, hbatch)
            conn.commit()
            if stopped:
                return False

            with open("../data/processed/column_mapping.csv", "w", encoding="utf-8") as f:
                f.write("Original,Limpio\n")
                for orig, clean in mapeo.items():
                    f.write("{},{}\n".format(orig, clean))

            cur.execute("SELECT COUNT(*) FROM bold_records")
            real_count = cur.fetchone()[0]
            hash_count = cur.execute("SELECT COUNT(*) FROM {0}".format(ROW_HASH_TABLE)).fetchone()[0]
            # Validation: every row read is in the database, and the count
            # matches what Step 1 wrote. Anything else is a bug or a damaged
            # .filt.tsv, and the build is discarded rather than put in service.
            if not (real_count == hash_count == processed and
                    (total_lines == 0 or total_lines == processed)):
                log(t('import_count_mismatch', read=processed, stored=real_count,
                      hashed=hash_count, expected=total_lines or processed))
                return False
            if irregular:
                log(t('import_irregular_rows', n=irregular, cols=n_cols))

            # Searching by BOLD ID used to rely on the PRIMARY KEY's index.
            # Built here, not in Step 3, so it's there whatever steps are run,
            # and named outside Step 3's idx_col_* set so Step 3 never drops it.
            if "col_processid" in cols_clean:
                log(t('creating_processid_index'))
                cur.execute('CREATE INDEX {0} ON bold_records ("col_processid")'.format(
                    PROCESSID_INDEX))
                with_id, n_specimens = cur.execute(
                    "SELECT COUNT(col_processid), COUNT(DISTINCT col_processid) "
                    "FROM bold_records").fetchone()
                log(t('sequences_per_specimen', seqs=real_count, specimens=n_specimens,
                      extra=with_id - n_specimens))

            _write_db_meta(conn, {
                "record_count":    real_count,
                "source_file":     os.path.basename(archivo_tsv),
                "built_at":        datetime.now().isoformat(timespec="seconds"),
                "creator_version": __version__,
                "schema_version":  db_contract.SCHEMA_VERSION,
            })
            with open("../data/processed/db_source.txt", "w") as _f:
                _f.write(os.path.basename(archivo_tsv))
            log(t('db_created', name=os.path.basename(_db_path)))
            log(t('records_inserted', n=real_count))
            built = True
            progress(100)
            return True
        finally:
            try:
                conn.execute("PRAGMA synchronous  = NORMAL")
                conn.execute("PRAGMA journal_mode = DELETE")
                conn.execute("PRAGMA locking_mode = NORMAL")
            except Exception:
                pass
            conn.close()
            if not built:
                _remove_db_files(build_path)
            if stopped:
                log(t('import_interrupted'))
    except Exception as e:
        log(_fmt_error(e, t('ctx_tsv_to_sqlite')))
        return False

# ---- Step 3: Index fields --------------------------------------------------------

def run_step3(log, progress, cfg):
    os.chdir(PROJECT_ROOT)
    db_path = _target_db(cfg)
    if not os.path.exists(db_path):
        log(t('db_not_found', path=db_path))
        return False
    try:
        conn = _connect_retry(db_path, [
            "PRAGMA synchronous  = OFF",
            "PRAGMA journal_mode = MEMORY",
            "PRAGMA cache_size   = -1000000",   # ~1 GB cache
            "PRAGMA temp_store   = MEMORY",
            "PRAGMA mmap_size    = 30000000000",
            "PRAGMA threads      = {}".format(min(4, os.cpu_count() or 1)),
        ], log)
        watch_done = None
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name='bold_records'")
            if cur.fetchone()[0] == 0:
                log(t('table_not_exists'))
                return False

            cur.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='bold_records'")
            existing_sql = {row[0]: (row[1] or "") for row in cur.fetchall()}

            # DBs created before the fix have idx_col_nuc_basecount with
            # COLLATE NOCASE, useless for numeric comparisons. It's recreated.
            if "NOCASE" in existing_sql.get("idx_col_nuc_basecount", "").upper():
                log(t('recreating_index'))
                cur.execute("DROP INDEX idx_col_nuc_basecount")
                existing_sql.pop("idx_col_nuc_basecount", None)

            # Only what exists in the table gets indexed: the field configuration
            # may have changed after building the database, and requesting an
            # index on a nonexistent column aborts the whole step.
            cur.execute("PRAGMA table_info(bold_records)")
            table_cols = {r[1] for r in cur.fetchall()}
            wanted     = [c for c in index_cols() if c in table_cols]
            ausentes   = [c for c in index_cols() if c not in table_cols]
            if ausentes:
                log(t('missing_index_fields', fields=", ".join(ausentes)))
            if not wanted:
                log(t('no_index_fields_exist'))
                return False

            # Indexes of fields no longer marked "Index" in the Fields panel:
            # they're dropped, otherwise unchecking a field never gave back
            # its disk space. Only this step's own indexes (idx_col_*) are
            # touched, never SQLite's or the FTS index's.
            configured = {"idx_{0}".format(c.lower()) for c in index_cols()}
            sobrantes  = sorted(n for n in existing_sql
                                if n.startswith("idx_col_") and n not in configured)
            for name in sobrantes:
                cur.execute('DROP INDEX IF EXISTS "{0}"'.format(name))
                existing_sql.pop(name, None)
            if sobrantes:
                conn.commit()
                log(t('indexes_dropped', n=len(sobrantes), names=", ".join(sobrantes)))

            existing = set(existing_sql)
            pendientes = [c for c in wanted if "idx_{0}".format(c.lower()) not in existing]

            if not pendientes:
                log(t('all_columns_indexed'))
                progress(100)
                return True

            omitidos = len(wanted) - len(pendientes)
            log(t('creating_indexes', n=len(pendientes), skipped=omitidos))
            stop   = cfg.get("_stop")
            nuevos = []
            watch_done = _interrupt_on_stop(conn, stop)
            for i, col in enumerate(pendientes):
                if stop and stop.is_set():
                    if nuevos:
                        conn.commit()
                    log(t('indexing_interrupted', n=len(nuevos)))
                    return False
                idx_name = "idx_{0}".format(col.lower())
                # col_nuc_basecount is INTEGER: a NOCASE index doesn't match the
                # BINARY collation of numeric comparisons and the planner ignores it
                collate  = "" if col == "col_nuc_basecount" else " COLLATE NOCASE"
                try:
                    cur.execute(
                        'CREATE INDEX IF NOT EXISTS {0} ON bold_records ("{1}"{2})'.format(idx_name, col, collate)
                    )
                    nuevos.append(col)
                    log(t('index_created_progress', i=i + 1, n=len(pendientes), name=idx_name))
                except sqlite3.Error as e:
                    if stop and stop.is_set():
                        continue   # interrupted by Stop: handled at the top of the loop
                    log(t('index_error', i=i + 1, n=len(pendientes), col=col, error=e))
                progress((i + 1) / len(pendientes) * 100)
            if stop and stop.is_set():
                # Stop arrived during the last index
                if nuevos:
                    conn.commit()
                log(t('indexing_interrupted', n=len(nuevos)))
                return False

            conn.commit()  # a single commit at the end — removes the overhead of N intermediate commits
            conn.execute("PRAGMA optimize")
            log(t('indexes_created', n=len(nuevos)))
            progress(100)
            return True
        finally:
            if watch_done is not None:
                watch_done.set()
            try:
                conn.execute("PRAGMA synchronous  = NORMAL")
                conn.execute("PRAGMA journal_mode = DELETE")
            except Exception:
                pass
            conn.close()
    except Exception as e:
        log(_fmt_error(e, t('ctx_index_fields')))
        return False

# ---- Step 4: Create FTS ----------------------------------------------------------

def run_step4(log, progress, cfg):
    os.chdir(PROJECT_ROOT)
    # Same source of the path as Steps 2 and 3: with a fixed DB_FILE, a
    # cfg["db_file"] would build the FTS index on a different database.
    db_path = _target_db(cfg)
    if not os.path.exists(db_path):
        log(t('db_not_found', path=db_path))
        return False
    conn = None
    try:
        # check_same_thread=False: the 'rebuild' runs in a helper thread so it
        # can emit heartbeats while it lasts. Only one thread touches the connection at a time.
        conn = _connect_retry(db_path, [
            "PRAGMA cache_size    = -1000000",   # ~1 GB page cache
            "PRAGMA synchronous   = OFF",
            "PRAGMA journal_mode  = MEMORY",
            "PRAGMA temp_store    = MEMORY",
            "PRAGMA locking_mode  = EXCLUSIVE",
            "PRAGMA mmap_size     = 30000000000",
            "PRAGMA threads       = {}".format(min(4, os.cpu_count() or 1)),
        ], log, check_same_thread=False)
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bold_records'")
        if not cur.fetchone():
            log(t('table_not_exists'))
            return False

        cur.execute("PRAGMA table_info(bold_records)")
        existing_cols = {r[1] for r in cur.fetchall()}
        sel_fts = [c for c in fts_cols() if c in existing_cols]
        if not sel_fts:
            log(t('no_fts_columns'))
            return False

        # Skip if the FTS index was already built from the same data as the current .db
        _db_src  = "../data/processed/db_source.txt"
        _fts_src = "../data/processed/fts_source.txt"
        if os.path.exists(_db_src) and os.path.exists(_fts_src):
            try:
                if open(_db_src).read().strip() == open(_fts_src).read().strip():
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bold_records_fts'")
                    # An index built with other columns (older ones lacked
                    # col_processid) is rebuilt, even from the same data
                    if cur.fetchone() and [d[0] for d in cur.execute(
                            "SELECT * FROM bold_records_fts LIMIT 0").description] == sel_fts:
                        log(t('fts_already_built'))
                        progress(100)
                        return True
            except Exception:
                pass

        cur.execute("SELECT COUNT(*) FROM bold_records")
        total = cur.fetchone()[0]
        if total == 0:
            log(t('empty_table'))
            return True

        log(t('creating_fts_table', n=len(sel_fts)))
        cur.execute("DROP TABLE IF EXISTS bold_records_fts")
        # tokenize='trigram' instead of 'unicode61': unicode61 only indexes
        # whole words, so MATCH 'Drosophila' wouldn't find "Hirtodrosophila" or
        # "Scaptodrosophila" (they contain the term as a substring, not as a
        # standalone token), whereas field search (LIKE '%Drosophila%') did
        # find them — hence the incomplete results. trigram indexes 3-character
        # n-grams, so MATCH reproduces the same substring behavior as LIKE
        # (verified: exact same count across the 21.3M real records).
        cur.execute("""CREATE VIRTUAL TABLE bold_records_fts USING fts5(
            {0},
            content='bold_records',
            content_rowid='rowid',
            tokenize='trigram'
        )""".format(", ".join(sel_fts)))
        conn.commit()

        # 'rebuild' does a single internal merge-sort over the whole content
        # table: it avoids creating hundreds of intermediate FTS segments and
        # the later merge with 'optimize', which was the biggest bottleneck.
        # It runs as a single statement, so Stop is honoured via conn.interrupt().
        log(t('rebuilding_fts_index', n=total))
        log(t('fts_may_take_minutes'))
        progress(-1)

        # 'rebuild' reports no progress. Without a heartbeat the GUI stays
        # identical for many minutes and the user assumes it froze. A Stop
        # press is picked up in the wait loop and aborts the statement with
        # conn.interrupt() (safe from this thread: check_same_thread=False).
        stop      = cfg.get("_stop")
        _rb_done  = threading.Event()
        _rb_error = []

        def _rebuild():
            try:
                cur.execute("INSERT INTO bold_records_fts(bold_records_fts) VALUES('rebuild')")
                conn.commit()
            except BaseException as exc:
                _rb_error.append(exc)
            finally:
                _rb_done.set()

        _t_rb = threading.Thread(target=_rebuild, daemon=True)
        _rb_t0 = time.monotonic()
        _t_rb.start()
        _rb_interrupted = False
        _rb_beat = _rb_t0
        while not _rb_done.wait(3):
            _rb_now = time.monotonic()
            if stop is not None and stop.is_set() and not _rb_interrupted:
                _rb_interrupted = True
                log(t('fts_stopping'))
                conn.interrupt()
                continue
            if not _rb_interrupted and _rb_now - _rb_beat >= 30:
                _rb_beat = _rb_now
                log(t('still_working', mins=(_rb_now - _rb_t0) / 60))
        _t_rb.join()
        if _rb_interrupted:
            try:
                conn.rollback()
                conn.execute("DROP TABLE IF EXISTS bold_records_fts")
                conn.commit()
            except Exception:
                pass
            log(t('fts_interrupted'))
            return False
        if _rb_error:
            raise _rb_error[0]
        log(t('rebuild_finished', mins=(time.monotonic() - _rb_t0) / 60))
        conn.execute("PRAGMA optimize")
        if os.path.exists(_db_src):
            try:
                with open(_fts_src, "w") as _f:
                    _f.write(open(_db_src).read().strip())
            except Exception:
                pass
        log(t('fts_created', n=len(sel_fts), total=total))
        progress(100)
        return True
    except sqlite3.Error as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        log(_fmt_error(e, t('ctx_create_fts')))
        return False
    finally:
        if conn:
            try:
                conn.execute("PRAGMA synchronous  = NORMAL")
                conn.execute("PRAGMA journal_mode = DELETE")
                conn.execute("PRAGMA locking_mode = NORMAL")
                conn.close()
            except Exception:
                pass


# ---- Update: apply a new BOLD package to the existing database ----------------
#
# BOLD publishes a full snapshot every week, never a list of changes, so the
# new package always has to be downloaded and filtered (Step 1). What can be
# avoided is rebuilding the indexes and the full-text index (Steps 3 and 4),
# which take most of a full rebuild's time: only the records that changed are
# written, and SQLite keeps every index in step as it goes.
#
# How the changes are found: Step 2 stores a fingerprint of every record in
# bold_row_hash (processid -> hash of its stored values). Here the new
# .filt.tsv is fingerprinted the same way into a staging database, and three
# queries give the records that are new, modified (same processid, another
# hash) and gone. Only those rows are then read again from the TSV and applied.
#
# Safety: everything is applied in a single transaction, in WAL mode. If the
# update fails or is stopped, SQLite discards it and the database stays
# exactly as it was; meanwhile the viewer keeps reading the previous version.
#
# When it pays off depends on how much changed and on the machine: changing a
# row costs several times more than including it in a bulk index build (x6
# measured on an NVMe SSD with the indexes fitting in RAM; much more on a
# mechanical disk with little RAM). Above UPDATE_MAX_CHANGE nothing is
# applied and a full rebuild is recommended instead.

UPDATE_MAX_CHANGE = 0.10
_STAGE_DB = "../data/processed/update_stage.db"


class _UpdateStopped(Exception):
    """Stop was pressed during the update."""


def _total_ram_gb():
    """Physical RAM in GB, or None if it can't be read. Shown on Home next to
    the update option: whether the indexes fit in memory is what decides how
    much faster updating is than rebuilding."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullTotalPhys / 1024 ** 3
            return None
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (AttributeError, ValueError, OSError):
        return None


def run_update(log, progress, cfg):
    os.chdir(PROJECT_ROOT)
    db_path = cfg.get("db_file", DB_FILE)
    if not os.path.exists(db_path):
        log(t('db_not_found', path=db_path))
        return False
    archivos = glob.glob("../data/processed/*.filt.tsv")
    if not archivos:
        log(t('no_filt_tsv_found'))
        return False
    archivo_tsv = max(archivos, key=os.path.getsize)
    stop = cfg.get("_stop")
    t0 = time.monotonic()

    def _check_stop():
        if stop is not None and stop.is_set():
            raise _UpdateStopped()

    conn = None
    attached = False
    watch_done = None
    wal = False        # main database switched to WAL for the apply phase
    applied = False
    try:
        # The database itself isn't touched until the changes are known to be
        # worth applying (see "Apply"): a refused update leaves it byte for byte.
        conn = _connect_retry(db_path, [
            "PRAGMA synchronous  = NORMAL",
            "PRAGMA cache_size   = -1000000",   # ~1 GB page cache
            "PRAGMA temp_store   = FILE",
            "PRAGMA mmap_size    = 30000000000",
        ], log, check_same_thread=False)
        cur = conn.cursor()

        # ---- Preconditions ---------------------------------------------------
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "bold_records" not in tables:
            log(t('table_not_exists'))
            return False
        if ROW_HASH_TABLE not in tables:
            log(t('update_needs_rebuild_once'))
            return False
        # Rows are written here in this version's layout: applied to a database
        # with another one, they'd mix two layouts in the same table.
        have = db_contract.schema_version(conn)
        if have != db_contract.SCHEMA_VERSION:
            log(t('update_schema_mismatch', have=have, want=db_contract.SCHEMA_VERSION))
            return False
        db_cols = [r[1] for r in cur.execute("PRAGMA table_info(bold_records)")]
        with open(archivo_tsv, "r", encoding="utf-8", errors="replace", newline="") as f:
            headers = next(csv.reader(f, delimiter="\t"), [])
        cols_clean = [_clean_col(h) for h in headers]
        if set(cols_clean) != set(db_cols) or "col_processid" not in cols_clean:
            log(t('update_fields_mismatch',
                  extra=", ".join(sorted(set(cols_clean) - set(db_cols))) or "—",
                  missing=", ".join(sorted(set(db_cols) - set(cols_clean))) or "—"))
            return False
        n_cols  = len(cols_clean)
        key_idx = _row_key_idx(cols_clean)
        order   = _hash_order(cols_clean)
        fts_cols = []
        if "bold_records_fts" in tables:
            fts_cols = [d[0] for d in cur.execute("SELECT * FROM bold_records_fts LIMIT 0").description]
        fts_idx = [cols_clean.index(c) for c in fts_cols]
        log(t('file_label', name=os.path.basename(archivo_tsv)))

        # ---- Pass 1: fingerprint the new package ---------------------------
        for suffix in ("", "-journal", "-wal", "-shm"):
            try: os.remove(_STAGE_DB + suffix)
            except OSError: pass
        cur.execute("ATTACH DATABASE ? AS st", (os.path.abspath(_STAGE_DB),))
        attached = True
        cur.execute("PRAGMA st.journal_mode = OFF")
        cur.execute("PRAGMA st.synchronous  = OFF")
        cur.execute("CREATE TABLE st.stage (line INTEGER PRIMARY KEY, processid TEXT, marker TEXT, h INTEGER)")
        log(t('update_pass1'))
        total_bytes = os.path.getsize(archivo_tsv) or 1
        line = 0
        batch = []
        _last_ui = 0.0
        with open(archivo_tsv, "r", encoding="utf-8", errors="replace",
                  newline="", buffering=8 * 1024 * 1024) as fin:
            reader = csv.reader(fin, delimiter="\t")
            next(reader)
            for row in reader:
                line += 1
                # Every row, as Step 2 stores every row: one without a
                # processid is still a sequence (it used to be skipped here)
                vals = _tsv_row_values(row, n_cols)
                batch.append((line,) + _row_key(vals, key_idx) + (_row_hash(vals, order),))
                if len(batch) >= 100_000:
                    cur.executemany("INSERT INTO st.stage VALUES (?, ?, ?, ?)", batch)
                    batch.clear()
                    _check_stop()
                    now = time.monotonic()
                    if now - _last_ui >= 0.5:
                        _last_ui = now
                        pct = min(fin.buffer.tell() / total_bytes * 100, 100)
                        progress(pct * 0.4)
                        log(t('update_pass1_progress', n=line, pct=pct))
            if batch:
                cur.executemany("INSERT INTO st.stage VALUES (?, ?, ?, ?)", batch)
        conn.commit()
        n_package = line
        progress(40)

        # ---- Diff --------------------------------------------------------------
        # A specimen can have several rows (one per marker, and rarely two of
        # the same marker), so rows are matched by content, not by processid:
        #  1. A stored row and a package row with the same processid, marker
        #     and fingerprint are the same sequence: untouched. The k-th copy
        #     pairs with the k-th, so identical rows are counted, not merged.
        #  2. What's left of a processid + marker on both sides is paired in
        #     order and counts as modified (rewritten in place).
        #  3. Left over in the package: new. Left over in the database: gone.
        log(t('update_diff'))
        watch_done = _interrupt_on_stop(conn, stop)
        cur.execute("CREATE TABLE st.newk AS SELECT line, processid, marker, h, ROW_NUMBER() OVER "
                    "(PARTITION BY processid, marker, h ORDER BY line) AS k FROM st.stage")
        cur.execute("DROP TABLE st.stage")
        cur.execute("CREATE TABLE st.oldk AS SELECT rid, processid, marker, h, ROW_NUMBER() OVER "
                    "(PARTITION BY processid, marker, h ORDER BY rid) AS k FROM main.{0}"
                    .format(ROW_HASH_TABLE))
        cur.execute("CREATE INDEX st.newk_i ON newk (processid, marker, h, k)")
        cur.execute("CREATE INDEX st.oldk_i ON oldk (processid, marker, h, k)")
        _check_stop()
        for side, other, id_col in (("new", "oldk", "line"), ("old", "newk", "rid")):
            cur.execute(
                "CREATE TABLE st.{0}_left AS SELECT {2}, processid, marker, ROW_NUMBER() OVER "
                "(PARTITION BY processid, marker ORDER BY {2}) AS j FROM st.{0}k x "
                "WHERE NOT EXISTS (SELECT 1 FROM st.{1} y WHERE y.processid = x.processid "
                "AND y.marker = x.marker AND y.h = x.h AND y.k = x.k)".format(side, other, id_col))
            cur.execute("CREATE INDEX st.{0}_left_i ON {0}_left (processid, marker, j)".format(side))
            _check_stop()
        cur.execute("DROP TABLE st.newk")
        cur.execute("DROP TABLE st.oldk")
        # rid NULL: new record. Otherwise the stored row it replaces.
        cur.execute("CREATE TABLE st.todo (line INTEGER PRIMARY KEY, rid INTEGER)")
        cur.execute("INSERT INTO st.todo SELECT n.line, o.rid FROM st.new_left n "
                    "LEFT JOIN st.old_left o ON o.processid = n.processid "
                    "AND o.marker = n.marker AND o.j = n.j")
        cur.execute("CREATE TABLE st.gone (rid INTEGER PRIMARY KEY)")
        cur.execute("INSERT INTO st.gone SELECT o.rid FROM st.old_left o WHERE NOT EXISTS "
                    "(SELECT 1 FROM st.new_left n WHERE n.processid = o.processid "
                    "AND n.marker = o.marker AND n.j = o.j)")
        conn.commit()
        _check_stop()
        n_old  = cur.execute("SELECT COUNT(*) FROM {0}".format(ROW_HASH_TABLE)).fetchone()[0]
        n_new  = cur.execute("SELECT COUNT(*) FROM st.todo WHERE rid IS NULL").fetchone()[0]
        n_chg  = cur.execute("SELECT COUNT(*) FROM st.todo WHERE rid IS NOT NULL").fetchone()[0]
        n_gone = cur.execute("SELECT COUNT(*) FROM st.gone").fetchone()[0]
        n_all  = n_new + n_chg + n_gone
        frac   = n_all / max(n_old, 1)
        log(t('update_stats', new=n_new, chg=n_chg, gone=n_gone, pct=frac * 100, total=n_old))
        progress(50)

        if frac > UPDATE_MAX_CHANGE:
            log(t('update_too_many', limit=int(UPDATE_MAX_CHANGE * 100)))
            return False

        # ---- Apply -----------------------------------------------------------
        _check_stop()
        if n_all:
            # WAL from here on: the viewer keeps reading the previous version
            # while the transaction is open, and an aborted update leaves no trace.
            conn.execute("PRAGMA main.journal_mode = WAL")
            wal = True
            log(t('update_applying', fts=t('update_with_fts') if fts_cols else ""))
            todo = dict(cur.execute("SELECT line, rid FROM st.todo"))
            gone = [r[0] for r in cur.execute("SELECT rid FROM st.gone")]

            quoted   = ['"{0}"'.format(c) for c in cols_clean]
            ins_q    = "INSERT INTO bold_records ({0}) VALUES ({1})".format(
                           ", ".join(quoted), ", ".join("?" * n_cols))
            upd_q    = "UPDATE bold_records SET {0} WHERE rowid = ?".format(
                           ", ".join(q + " = ?" for q in quoted))
            sel_q    = "SELECT rowid{0} FROM bold_records WHERE rowid = ?".format(
                           "".join(', "{0}"'.format(c) for c in fts_cols))
            fts_list = ", ".join('"{0}"'.format(c) for c in fts_cols)
            fts_del  = ("INSERT INTO bold_records_fts(bold_records_fts, rowid, {0}) VALUES ('delete', ?, {1})"
                        .format(fts_list, ", ".join("?" * len(fts_cols)))) if fts_cols else None
            fts_ins  = ("INSERT INTO bold_records_fts(rowid, {0}) VALUES (?, {1})"
                        .format(fts_list, ", ".join("?" * len(fts_cols)))) if fts_cols else None
            hash_ins = "INSERT INTO {0} (rid, processid, marker, h) VALUES (?, ?, ?, ?)".format(ROW_HASH_TABLE)
            hash_upd = "UPDATE {0} SET processid = ?, marker = ?, h = ? WHERE rid = ?".format(ROW_HASH_TABLE)

            done = 0
            _last_ui = 0.0

            def _tick():
                nonlocal _last_ui
                now = time.monotonic()
                if now - _last_ui >= 0.5:
                    _last_ui = now
                    pct = done / max(n_all, 1) * 100
                    progress(50 + pct * 0.49)
                    log(t('update_apply_progress', done=done, total=n_all, pct=pct))

            # Removed records
            for rid in gone:
                old = cur.execute(sel_q, (rid,)).fetchone()
                if old is not None:
                    if fts_cols:
                        cur.execute(fts_del, old)
                    cur.execute("DELETE FROM bold_records WHERE rowid = ?", (rid,))
                cur.execute("DELETE FROM {0} WHERE rid = ?".format(ROW_HASH_TABLE), (rid,))
                done += 1
                if done % 2000 == 0:
                    _check_stop()
                    _tick()

            # New and modified records: read again from the TSV, only those lines
            with open(archivo_tsv, "r", encoding="utf-8", errors="replace",
                      newline="", buffering=8 * 1024 * 1024) as fin:
                reader = csv.reader(fin, delimiter="\t")
                next(reader)
                line = 0
                for row in reader:
                    line += 1
                    if line not in todo:
                        continue
                    rid  = todo[line]
                    vals = _tsv_row_values(row, n_cols)
                    key  = _row_key(vals, key_idx)
                    h    = _row_hash(vals, order)
                    old  = cur.execute(sel_q, (rid,)).fetchone() if rid is not None else None
                    if old is not None:
                        if fts_cols:
                            cur.execute(fts_del, old)
                        cur.execute(upd_q, vals + (rid,))
                        cur.execute(hash_upd, key + (h, rid))
                        rowid = rid
                    else:
                        cur.execute(ins_q, vals)
                        rowid = cur.lastrowid
                        cur.execute(hash_ins, (rowid,) + key + (h,))
                    if fts_cols:
                        cur.execute(fts_ins, (rowid,) + tuple(vals[i] for i in fts_idx))
                    done += 1
                    if done % 2000 == 0:
                        _check_stop()
                        _tick()
        else:
            log(t('update_nothing'))

        # ---- Validation ----------------------------------------------------------
        # The database must end up with exactly one row per row of the package.
        # Otherwise the whole update is rolled back (nothing is committed yet).
        _check_stop()
        total     = n_old + n_new - n_gone
        n_records = cur.execute("SELECT COUNT(*) FROM bold_records").fetchone()[0]
        n_hashes  = cur.execute("SELECT COUNT(*) FROM {0}".format(ROW_HASH_TABLE)).fetchone()[0]
        if not (n_records == n_hashes == total == n_package):
            log(t('update_count_mismatch', package=n_package, stored=n_records,
                  hashed=n_hashes, expected=total))
            conn.rollback()
            return False

        # ---- Commit ------------------------------------------------------------
        _write_db_meta(conn, {      # also commits the whole transaction
            "record_count":    total,
            "source_file":     os.path.basename(archivo_tsv),
            "updated_at":      datetime.now().isoformat(timespec="seconds"),
            "last_update":     "+{0} ~{1} -{2}".format(n_new, n_chg, n_gone),
            "creator_version": __version__,
            "schema_version":  db_contract.SCHEMA_VERSION,
        })
        applied = True
        watch_done.set()
        watch_done = None
        for path, write in (("../data/processed/db_source.txt", True),
                            ("../data/processed/fts_source.txt", bool(fts_cols))):
            if write:
                with open(path, "w") as _f:
                    _f.write(os.path.basename(archivo_tsv))
        log(t('update_done', mins=(time.monotonic() - t0) / 60,
              new=n_new, chg=n_chg, gone=n_gone, total=total))
        progress(100)
        return True

    except _UpdateStopped:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
        log(t('update_interrupted'))
        return False
    except sqlite3.OperationalError as e:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
        if stop is not None and stop.is_set() and "interrupt" in str(e).lower():
            log(t('update_interrupted'))
        else:
            log(_fmt_error(e, t('ctx_update')))
            log(t('update_failed_rolled_back'))
        return False
    except Exception as e:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
        log(_fmt_error(e, t('ctx_update')))
        log(t('update_failed_rolled_back'))
        return False
    finally:
        if watch_done is not None:
            watch_done.set()
        if conn is not None:
            if attached:
                try: conn.execute("DETACH DATABASE st")
                except Exception: pass
            # Fold the WAL back into the database file and leave it in the same
            # journal mode as a freshly built one. If the viewer has it open
            # right now the switch fails harmlessly; the next run retries it.
            if wal:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("PRAGMA journal_mode = DELETE")
                except Exception:
                    pass
            if applied:
                try: conn.execute("PRAGMA optimize")
                except Exception: pass
            conn.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            try: os.remove(_STAGE_DB + suffix)
            except OSError: pass


# ==============================================================================
# GUI — PySide6
# ==============================================================================

# ---- Palette & stylesheet ---------------------------------------------------------
#
# Named by role, not by hue. Contrast ratios are against the panel background
# (#26262D) unless stated; body text should stay >= 4.5:1. Changes from the
# previous palette: "white" (which was this same blue) and "green" (which was
# cyan, and unused) are gone; "done" (#52547a, 2.1:1 — secondary text was
# practically unreadable) is split into muted/disabled/dimmed; the loose hex
# values that were scattered through the stylesheet now live here.

P = {
    # Surfaces
    "bg":          "#2a2d3a",
    "panel":       "#26262D",
    "sidebar":     "#3B3A44",
    "surface":     "#535366",
    "disabled_bg": "#45475a",
    # Text
    "text":        "#d5d8dd",   # 11:1
    "sub":         "#d6dcf8",   # 11:1, secondary text
    "muted":       "#a0a3bd",   # 6.1:1, technical labels, timestamps
    "disabled":    "#8a8da0",   # 4.6:1, disabled controls
    "dimmed":      "#7d8098",   # 3.9:1, finished log lines (de-emphasised on purpose)
    "on_btn":      "#ffffff",   # text on the blue buttons
    "on_light":    "#11111b",   # text on light fills (stop button hover)
    # Accents and buttons
    "accent":       "#88A6D7",  # 6.1:1
    "accent_hover": "#a9c1e6",
    "btn":          "#295EB3",  # primary / run (white text 6.3:1)
    "btn_hover":    "#477DD4",
    "card":         "#3D6DBB",  # small buttons (white text 5.1:1; was text on #477DD4, 2.9:1)
    "border":       "#477DD4",  # outlines, scrollbar handle, checkbox frames
    "progbar":       "#61D8FD",
    "progbar_light": "#8ae8ff",
    "blue_mid":      "#669AEE",
    # States
    "ok":          "#7fe0a0",
    "ok_hover":    "#a6ebbd",
    "running":     "#fab387",
    "yellow":      "#c09b07",
    "yellow_light": "#e0b520",
    "red":         "#f38ba8",
    "red_light":   "#f8afc0",
    # Requirement badges
    "req":         "#f38ba8",   # REQUIRED
    "rec":         "#e0b520",   # RECOMMENDED
    "opt":         "#8a8da0",   # OPTIONAL
}

# Monospace family for paths, the log and the timer. Resolved at startup
# (_resolve_mono_font) to the first one installed: "Courier New" was
# hardcoded, which is a poor fit on Windows and absent on many Linux systems.
_MONO_PREFERENCE = ["Cascadia Mono", "Consolas", "SF Mono", "Menlo",
                    "DejaVu Sans Mono", "Liberation Mono", "Courier New"]
_FONT_MONO = ["monospace"]   # mutable: index 0 is set once at startup


def _mono():
    return _FONT_MONO[0]


def _resolve_mono_font():
    from PySide6.QtGui import QFontDatabase
    installed = set(QFontDatabase.families())
    for fam in _MONO_PREFERENCE:
        if fam in installed:
            _FONT_MONO[0] = fam
            return
    _FONT_MONO[0] = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def _styled(widget, role=None, tone=None):
    """Applies a label role and/or tone defined in the app stylesheet.

    Replaces the dozens of inline setStyleSheet(f"color: ...; font-size: ...")
    calls that repeated the same few combinations: a role fixes size and
    weight (and a default colour), a tone overrides the colour for states
    (ok / warn / error / running). Changing a property after the widget is
    shown needs an unpolish/polish for Qt to re-evaluate the selectors.
    """
    if role is not None:
        widget.setProperty("role", role)
    if tone is not None:
        widget.setProperty("tone", tone)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    return widget


def _hsep_line():
    """Thin horizontal separator (QFrame#hsep in the stylesheet)."""
    f = QFrame()
    f.setObjectName("hsep")
    f.setFrameShape(QFrame.Shape.HLine)
    return f


# Step status markers (sidebar and Home summary)
STATUS_DONE = "✓"
STATUS_TODO = "○"


def _make_app_qss() -> str:
    p = dict(P)
    p.update({
        "fs7":  _fs(7),  "fs9":  _fs(9),  "fs10": _fs(10),
        "fs11": _fs(11), "fs12": _fs(12), "fs13": _fs(13),
        "fs14": _fs(14), "fs16": _fs(16),
        "px15": _px(15),
        "mono": _mono(),
    })
    # No font-family for the UI text: Qt then uses the platform's own UI font
    # (Segoe UI on Windows, the system font on macOS and Linux). Forcing
    # "Segoe UI" made every other platform fall back to an arbitrary font.
    return """
QMainWindow, QWidget {
    background-color: %(bg)s;
    color: %(text)s;
    font-size: %(fs13)s;
}
#sidebar { background-color: %(sidebar)s; }
/* Without this, labels in the sidebar ("Language", the project path) were
   painted with the window background, a darker box on the sidebar. */
#sidebar QLabel { background-color: transparent; }
QPushButton#navBtn {
    background-color: transparent;
    color: %(text)s;
    border: none;
    text-align: left center;
    padding: 7px 14px;
    font-size: %(fs12)s;
}
QPushButton#navBtn:hover { background-color: %(surface)s; }
QPushButton#navBtn[active="true"] {
    background-color: %(surface)s;
    color: %(accent)s;
    font-weight: bold;
    border-left: 5px solid %(progbar)s;
    padding-left: 11px;
}
QPushButton#navBtn[active="true"]:hover { background-color: %(disabled_bg)s; }
/* Home button: its own treatment (always-tinted background, bold, bottom
   border) so it doesn't read as item 0 of the step list below. */
QPushButton#homeBtn {
    background-color: rgba(136, 166, 215, 30);
    color: %(accent)s;
    border: none;
    border-bottom: 2px solid %(surface)s;
    text-align: left center;
    padding: 13px 16px;
    font-size: %(fs13)s;
    font-weight: bold;
}
QPushButton#homeBtn:hover { background-color: %(surface)s; }
QPushButton#homeBtn[active="true"] {
    background-color: %(surface)s;
    border-left: 5px solid %(progbar)s;
    padding-left: 11px;
}
QPushButton#homeBtn[active="true"]:hover { background-color: %(disabled_bg)s; }
/* This was inverted: the disabled button used to be painted bright blue and
   the enabled one dull gray — exactly the opposite of the expected signal. */
QPushButton#runBtn {
    background-color: %(btn)s;
    color: %(on_btn)s;
    border: none;
    padding: 7px 18px;
    font-weight: bold;
    font-size: %(fs12)s;
    border-radius: 6px;
}
QPushButton#runBtn:hover    { background-color: %(btn_hover)s; color: %(on_btn)s; }
QPushButton#runBtn:disabled { background-color: %(disabled_bg)s; color: %(muted)s; }
QPushButton#primaryBtn {
    background-color: %(btn)s;
    color: %(on_btn)s;
    border: none;
    padding: 10px 24px;
    font-weight: bold;
    font-size: %(fs13)s;
    border-radius: 8px;
}
QPushButton#primaryBtn:hover    { background-color: %(btn_hover)s; }
QPushButton#primaryBtn:disabled { background-color: %(disabled_bg)s; color: %(sub)s; }
/* Same visual hierarchy as the primary button but without weight: it's the
   alternative, not the default option. */
QPushButton#secondaryBtn {
    background-color: transparent;
    color: %(text)s;
    border: 1px solid %(surface)s;
    padding: 10px 24px;
    font-size: %(fs13)s;
    border-radius: 8px;
}
QPushButton#secondaryBtn:hover    { background-color: %(surface)s; border-color: %(border)s; }
QPushButton#secondaryBtn:disabled { color: %(disabled)s; border-color: %(disabled_bg)s; }
QPushButton#stopBtn {
    background-color: transparent;
    color: %(red)s;
    border: 2px solid %(red)s;
    padding: 7px 14px;
    font-size: %(fs12)s;
    border-radius: 6px;
}
QPushButton#stopBtn:hover    { background-color: %(red)s; color: %(on_light)s; border-color: %(red)s; }
QPushButton#stopBtn:disabled { color: %(disabled)s; border-color: %(disabled_bg)s; border-width: 1px; }
QPushButton#browseBtn {
    background-color: %(card)s;
    color: %(on_btn)s;
    border: none;
    padding: 3px 8px;
    border-radius: 6px;
}
QPushButton#browseBtn:hover { background-color: %(surface)s; color: %(on_btn)s; }
QPushButton#cardBtn {
    background-color: %(card)s;
    color: %(on_btn)s;
    border: none;
    padding: 4px 12px;
    font-size: %(fs12)s;
    border-radius: 6px;
}
QPushButton#cardBtn:hover    { background-color: %(surface)s; color: %(on_btn)s; }
QPushButton#cardBtn:disabled { background-color: %(disabled_bg)s; color: %(muted)s; }
/* "Open the viewer": green once there is a database to open, so it stands
   out from the blue buttons as the place to go next. */
QPushButton#viewerBtn {
    background-color: %(ok)s;
    color: %(on_light)s;
    border: none;
    padding: 4px 12px;
    font-size: %(fs12)s;
    font-weight: bold;
    border-radius: 6px;
}
QPushButton#viewerBtn:hover    { background-color: %(ok_hover)s; color: %(on_light)s; }
QPushButton#viewerBtn:disabled { background-color: %(disabled_bg)s; color: %(muted)s; font-weight: normal; }
/* Destructive action: distinguished from the rest without drawing attention. */
QPushButton#dangerBtn {
    background-color: transparent;
    color: %(sub)s;
    border: 1px solid %(surface)s;
    padding: 3px 11px;
    font-size: %(fs12)s;
    border-radius: 6px;
}
QPushButton#dangerBtn:hover    { color: %(red)s; border-color: %(red)s; }
QPushButton#dangerBtn:disabled { color: %(disabled)s; border-color: %(disabled_bg)s; }
QPushButton#clearBtn {
    background-color: transparent;
    color: %(sub)s;
    border: none;
    font-size: %(fs7)s;
    padding: 2px 8px;
    border-radius: 2px;
}
QPushButton#clearBtn:hover { background-color: %(surface)s; color: %(text)s; }
/* Language selector in the sidebar */
QPushButton#langBtn {
    font-size: %(fs10)s; font-weight: bold; border-radius: 4px; padding: 3px 0;
    background-color: %(sidebar)s; color: %(sub)s; border: none;
}
QPushButton#langBtn:hover { background-color: %(surface)s; }
QPushButton#langBtn[active="true"] { background-color: %(accent)s; color: %(bg)s; }
/* Without this rule, every tooltip fell back to the system default color
   (white or pale yellow on Windows) instead of the app's dark theme. With
   the explicit rule, every tooltip in the application always uses the same
   colors, with no exceptions. */
QToolTip {
    background-color: %(surface)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    padding: 4px 8px;
    font-size: %(fs11)s;
    font-weight: normal;
}
QMessageBox {
    background-color: %(bg)s;
}
QMessageBox QLabel {
    color: %(text)s;
    font-size: %(fs13)s;
    background-color: transparent;
}
QMessageBox QDialogButtonBox {
    background-color: %(bg)s;
}
QMessageBox QPushButton {
    background-color: %(surface)s;
    color: %(text)s;
    border: none;
    padding: 6px 22px;
    min-width: 80px;
    font-size: %(fs10)s;
    border-radius: 2px;
}
QMessageBox QPushButton:hover   { background-color: %(card)s; color: %(on_btn)s; }
QMessageBox QPushButton:default { background-color: %(accent)s; color: %(bg)s; }
QMessageBox QPushButton:default:hover { background-color: %(accent_hover)s; color: %(bg)s; }
QSplitter#mainSplit::handle { background-color: %(sidebar)s; }
QSplitter#mainSplit::handle:hover { background-color: %(border)s; }
#panel { background-color: %(panel)s; }
#panel QLabel { background-color: transparent; }
QFrame#hsep { background: %(surface)s; border: none; max-height: 1px; }
QTextEdit#logEdit {
    background-color: %(panel)s;
    color: %(text)s;
    border: none;
    font-family: "%(mono)s";
    font-size: %(fs11)s;
}
QProgressBar {
    background-color: %(surface)s;
    border: none;
    border-radius: 5px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 %(progbar_light)s, stop:1 %(progbar)s);
    border-radius: 5px;
}
QLineEdit {
    background-color: %(surface)s;
    color: %(text)s;
    border: none;
    padding: 3px 6px;
    selection-background-color: %(accent)s;
}
QScrollBar:vertical              { background: %(bg)s; width: 8px; border: none; }
QScrollBar::handle:vertical      { background: %(border)s; border-radius: 4px; min-height: 20px; }
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical    { height: 0px; }
/* Checkboxes in the Fields panel. Without their own style, Qt paints them
   with the system's light theme, and against the dark background a checked
   box can't be told apart from an empty one. */
QCheckBox { color: %(text)s; font-size: %(fs11)s; spacing: 7px; background: transparent; }
QCheckBox:disabled { color: %(disabled)s; }
QCheckBox::indicator {
    width: %(px15)spx; height: %(px15)spx;
    border: 1px solid %(border)s;
    border-radius: 3px;
    background: %(bg)s;
}
QCheckBox::indicator:hover            { border-color: %(progbar)s; }
QCheckBox::indicator:checked          { background: %(progbar)s; border-color: %(progbar)s; }
QCheckBox::indicator:disabled         { border-color: %(disabled_bg)s; }
QCheckBox::indicator:checked:disabled { background: %(muted)s; border-color: %(muted)s; }
QFrame#dropCard {
    background-color: transparent;
    border: 2px dashed %(border)s;
    border-radius: 6px;
}
QFrame#dropCard[loaded="true"]   { border: 1px solid %(accent)s; }
QFrame#updateBanner { background-color: %(sidebar)s; border: 1px solid %(rec)s; border-radius: 8px; }
QFrame#dropCard[dragging="true"] { border: 2px dashed %(btn)s; background-color: %(surface)s; }

/* ---- Label roles (see _styled) ------------------------------------------ */
QLabel[role="pageTitle"]  { color: %(accent)s; font-size: %(fs16)s; font-weight: bold; }
QLabel[role="stepTitle"]  { color: %(accent)s; font-size: %(fs14)s; font-weight: bold; }
QLabel[role="optionTitle"]{ color: %(accent)s; font-size: %(fs12)s; font-weight: bold; }
QLabel[role="cardTitle"]  { color: %(sub)s;    font-size: %(fs12)s; font-weight: bold; }
QLabel[role="body"]       { color: %(text)s;   font-size: %(fs11)s; }
QLabel[role="secondary"]  { color: %(sub)s;    font-size: %(fs11)s; }
QLabel[role="small"]      { color: %(sub)s;    font-size: %(fs10)s; }
QLabel[role="caption"]    { color: %(sub)s;    font-size: %(fs9)s; }
QLabel[role="colHeader"]  { color: %(sub)s;    font-size: %(fs10)s; font-weight: bold; }
QLabel[role="hint"]       { color: %(muted)s;  font-size: %(fs10)s; }
QLabel[role="techLabel"]  { color: %(muted)s;  font-size: %(fs10)s; font-family: "%(mono)s"; }
QLabel[role="pathsLabel"] { color: %(muted)s;  font-size: %(fs11)s; font-family: "%(mono)s"; }
QLabel[role="dropHint"]   { color: %(accent)s; font-size: %(fs12)s; padding: 8px 0; }
QLabel[role="dropFile"]   { color: %(ok)s;     font-size: %(fs12)s; }
QLabel[role="note"] {
    color: %(text)s; font-size: %(fs10)s; border-radius: 6px; padding: 8px 10px;
}
QLabel[role="badge"] {
    color: %(bg)s; font-size: %(fs9)s; font-weight: bold;
    border-radius: 4px; padding: 2px 9px;
}
/* Tones: override the role's colour for a state. After the roles so they win. */
QLabel[tone="neutral"] { color: %(sub)s; }
QLabel[tone="ok"]      { color: %(ok)s; }
QLabel[tone="warn"]    { color: %(yellow)s; }
QLabel[tone="rec"]     { color: %(rec)s; }
QLabel[tone="error"]   { color: %(red)s; }
QLabel[tone="running"] { color: %(running)s; }
/* Sized like runBtn/stopBtn so it sits in the same row as a peer. */
QLabel[tone="donePill"] {
    color: %(bg)s; font-size: %(fs12)s; font-weight: bold;
    border-radius: 6px;
}
/* Fills. Prefixed with #panel too: "#panel QLabel { background: transparent }"
   has an ID in it, so it outranks any selector without one. */
QLabel[role="note"],      #panel QLabel[role="note"]      { background-color: %(sidebar)s; }
QLabel[tone="donePill"],  #panel QLabel[tone="donePill"]  { background-color: %(ok)s; }
QLabel[role="badge"][level="req"], #panel QLabel[role="badge"][level="req"] { background-color: %(req)s; }
QLabel[role="badge"][level="rec"], #panel QLabel[role="badge"][level="rec"] { background-color: %(rec)s; }
QLabel[role="badge"][level="opt"], #panel QLabel[role="badge"][level="opt"] { background-color: %(opt)s; }
QLabel[role="badge"][level="ok"],  #panel QLabel[role="badge"][level="ok"]  { background-color: %(ok)s; }
""" % p

# ---- Steps definition --------------------------------------------------------

# Short names for each step, used to build both the numbered label
# ("1. Prepare data") and the variants ("Step 1 — Prepare data") that appear
# in log headers and warnings. LANG is fixed for the whole process, so t()
# always returns the same text here — the STEP_TECH/STEP_LEVEL/etc. keys (all
# built from STEPS[i][0]) stay consistent with each other for the duration of
# the session.
STEP_NAMES = [t('step1_name'), t('step2_name'), t('step3_name'), t('step4_name')]

STEPS = [
    ("1. " + STEP_NAMES[0], t('step1_desc'), run_step1, {}),
    ("2. " + STEP_NAMES[1], t('step2_desc'), run_step2, {}),
    ("3. " + STEP_NAMES[2], t('step3_desc'), run_step3, {}),
    ("4. " + STEP_NAMES[3], t('step4_desc'), run_step4, {}),
    # Not a fifth build step but the alternative to Steps 2-4 for a new weekly
    # package: no number, so it doesn't read as "the step after 4".
    ("↻ " + t('step_update_name'), t('step_update_desc'), run_update, {}),
]

# Technical label (for those who actually know the pipeline) and requirement
# level, which used to be embedded as '**REQUIRED**' in the description and
# showed up with the literal asterisks because the QLabel received plain text.
STEP_TECH = {
    STEPS[0][0]: t('step1_tech'),
    STEPS[1][0]: t('step2_tech'),
    STEPS[2][0]: t('step3_tech'),
    STEPS[3][0]: t('step4_tech'),
    STEPS[4][0]: t('step_update_tech'),
}

STEP_LEVEL = {
    STEPS[0][0]: (t('badge_required'),    "req"),
    STEPS[1][0]: (t('badge_required'),    "req"),
    STEPS[2][0]: (t('badge_recommended'), "rec"),
    STEPS[3][0]: (t('badge_optional'),    "opt"),
}

_SIDEBAR_GROUPS = [
    (t('sidebar_group_individual_steps'), [0, 1, 2, 3, 4], t('sidebar_group_hint')),
]

# The two ways to build it, offered as buttons on the Home screen. Previously
# only the 1-3 one existed, hidden as a "shortcut" in the sidebar, and there
# was no way to launch the whole process at once.
_PIPELINES = [
    {
        "title": t('pipeline1_title'),
        "label": t('pipeline1_label'),
        "label_done": t('pipeline1_label_done'),
        "steps": [0, 1, 2],
        "badge": t('badge_recommended'),
        "hint":  t('pipeline1_hint'),
        "tip":   t('pipeline1_tip'),
    },
    {
        "title": t('pipeline2_title'),
        "label": t('pipeline2_label'),
        "label_done": t('pipeline2_label_done'),
        "steps": [0, 1, 2, 3],
        "badge": "",
        "hint":  t('pipeline2_hint'),
        "tip":   t('pipeline2_tip'),
    },
    {
        # Step 1 with the new package, then the update instead of Steps 2-4.
        # The hint names this computer's RAM: whether the indexes fit in
        # memory is what decides how much faster updating is, and the choice
        # between updating and rebuilding is left to the user.
        "title": t('pipeline3_title'),
        "label": t('pipeline3_label'),
        "label_done": t('pipeline3_label'),
        "steps": [0, 4],
        "badge": "",
        "ram":   (t('ram_gb', n=_total_ram_gb()) if _total_ram_gb() else t('ram_unknown')),
        "hint":  "",      # set in PrepPanel.refresh: it includes the current database size
        "tip":   t('pipeline3_tip', pct=int(UPDATE_MAX_CHANGE * 100)),
        "needs_updatable_db": True,
    },
]

# Web viewer port (frontend/server.py -> app.run(port=5001))
VIEWER_PORT = 5001

# Portal from which Step 1's input .tar.gz is downloaded
BOLD_DOWNLOAD_URL = "https://bench.boldsystems.org/index.php/datapackages/Latest"

# Suffix of a .tar.gz still being copied into data/raw/ (see _CopyWorker).
# Never matches the "*.tar.gz" globs, so it's invisible to Step 1.
PART_SUFFIX = ".part"

# The Home panel goes last in the stack but first in the sidebar
PREP_IDX   = len(STEPS)
FIELDS_IDX = len(STEPS) + 1


# ---- data/ initialization ------------------------------------------------------
#
# data/ is entirely generated content (see .gitignore) and git never tracks
# empty folders, so a fresh clone from GitHub has no data/ tree at all — not
# even data/raw/, where Step 1 expects the downloaded .tar.gz to be dropped.
# Recreated here on every startup, so a first-time user
# always has somewhere to put the file instead of hitting a missing-folder error.

def _ensure_data_dirs():
    """Recreates the data/ folder tree (raw, processed, exports/*) if missing.

    Best-effort: a permission error or antivirus lock here must not prevent the
    window from opening at all, the way an unguarded os.makedirs would (unlike
    Step 1's own os.makedirs calls, this one runs before any per-step error
    handling exists to catch it).
    """
    for parts in (
        ("data", "raw"),
        ("data", "processed"),
        ("data", "exports", "csv_exports"),
        ("data", "exports", "fasta_exports"),
        ("data", "exports", "batch_exports"),
        ("data", "database"),
        ("data", "state", "logs"),
    ):
        try:
            os.makedirs(_proj_dir(*parts), exist_ok=True)
        except OSError:
            pass
    # Leftovers of a copy interrupted by a crash or a forced close: no copy
    # can be running yet at startup, so any .part found is an orphan.
    for part in glob.glob(_proj_dir("data", "raw", "*" + PART_SUFFIX)):
        try:
            os.remove(part)
        except OSError:
            pass


# ---- From the old layout ---------------------------------------------------------

def _remove_empty_tree(path):
    """Deletes a folder tree that holds no files; leaves it alone otherwise."""
    if not os.path.isdir(path):
        return
    for dirpath, _dirs, files in os.walk(path):
        if files:
            return
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def _migration_message():
    """What the move from the old layout (app/) did, for the log. None if nothing."""
    done, problems = _MIGRATION
    lines = [t('migr_' + key, detail=detail) for key, detail in done + problems]
    return "\n".join(lines) or None


# ---- Per-step input verification --------------------------------------------

def _proj_dir(*parts):
    """Absolute, normalized path inside the project (PROJECT_ROOT is dev/)."""
    return os.path.normpath(os.path.join(PROJECT_ROOT, "..", *parts))


def _os_open(path):
    """Opens a file or folder with the OS's default application/file manager."""
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=True)
    else:
        subprocess.run(["xdg-open", path], check=True)


def _open_folder(path):
    """Opens a folder in the file manager, creating it if needed."""
    try:
        os.makedirs(path, exist_ok=True)
        _os_open(path)
        return True
    except Exception:
        return False


def _fmt_size(n):
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return "{:,.1f} {}".format(n / div, unit).replace(",", " ")
    return "{} B".format(n)


def _intermediate_files():
    """Step 1 working files that are no longer needed once the DB is built.

    The .filt.tsv (about 20 GB) is only Step 2's input: neither the viewer nor
    Steps 3 and 4 ever open it. It's kept solely to allow reimporting without
    repeating the download and filtering, so deleting it is the user's call.
    A .tar.gz kept with Step 1's "Keep the .tar.gz" option is listed too.
    """
    out = []
    for path in sorted(glob.glob(_proj_dir("data", "processed", "*.tsv")) +
                       glob.glob(_proj_dir("data", "raw", "*.tar.gz"))):
        try:
            out.append((path, os.path.getsize(path)))
        except OSError:
            pass
    return out


def _check_input(step_label):
    db = layout.database_path()
    checks = {
        STEPS[0][0]: lambda: (
            len(glob.glob(_proj_dir("data", "raw", "*.tar.gz"))) > 0 or
            len(glob.glob(_proj_dir("data", "processed", "*.tsv"))) > 0   # .filt.tsv or extracted .tsv
        ),
        STEPS[1][0]: lambda: len(glob.glob(_proj_dir("data", "processed", "*.filt.tsv"))) > 0,
        STEPS[2][0]: lambda: os.path.exists(db),
        STEPS[3][0]: lambda: os.path.exists(db),
        # Same rule as the Home button: a database that stores fingerprints
        STEPS[4][0]: lambda: (os.path.exists(db) and _get_project_status().get("updatable", False) and
                              len(glob.glob(_proj_dir("data", "processed", "*.filt.tsv"))) > 0),
    }
    fn = checks.get(step_label)
    return fn() if fn else True


def _has_new_package():
    """Is there a .tar.gz in data/raw/ (the drop zone's folder)?"""
    return bool(glob.glob(_proj_dir("data", "raw", "*.tar.gz")))


def _prepared_data_is_new():
    """Is there prepared data (.filt.tsv) other than the one the database was
    built or last updated from? Then an update has something to compare."""
    filt = glob.glob(_proj_dir("data", "processed", "*.filt.tsv"))
    if not filt:
        return False
    try:
        with open(_proj_dir("data", "processed", "db_source.txt")) as f:
            src = f.read().strip()
    except OSError:
        src = ""
    return os.path.basename(max(filt, key=os.path.getsize)) != src


def _pipeline_blocker(pl, st, flags):
    """Why a Home option can't run right now (a string key), or None.

    Creating (Options 1 and 2) needs a package in the drop zone, unless some
    step of the option is still pending and its input is already on disk
    (e.g. the database exists but its indexes don't). Updating (Option 3)
    always needs a new package: it's what gets compared with the database.
    """
    if pl.get("needs_updatable_db"):
        if not st["step2"]:
            return 'update_no_db_tip'
        if not st.get("updatable"):
            return 'update_unavailable_tip'
        if not (_has_new_package() or _prepared_data_is_new()):
            return 'update_needs_package_tip'
        return None
    if _has_new_package():
        return None
    if any(not flags[i] and _check_input(STEPS[i][0]) for i in pl["steps"]):
        return None
    if st.get("outdated"):
        return 'pipeline_needs_package_tip'
    if not st["done1"] and not st["step2"]:
        return 'pipeline_needs_package_tip'
    return 'pipeline_done_needs_package_tip'


_STATUS_CACHE = {"t": 0.0, "v": None}

# Set by the worker thread while a step is running. Step 2 opens the DB with
# PRAGMA locking_mode = EXCLUSIVE: while it holds it, any other connection
# waits out the full timeout and fails ("database is locked"). If that
# connection happens on the UI thread, the window freezes for those seconds —
# which is exactly what used to happen when switching panels during Step 2.
_STATUS_BUSY = [False]


def _set_status_busy(busy):
    _STATUS_BUSY[0] = bool(busy)


def _invalidate_status():
    """Forces the status to be re-read. Called as soon as a step finishes."""
    _STATUS_CACHE["v"] = None


def _get_project_status(max_age=0.5):
    """Checks the project's current status. Fast: no COUNT(*) or heavy queries.

    Cached for half a second: a single UI refresh queries it from the four
    panels, the sidebar, and the Home screen.
    """
    now = time.monotonic()
    if _STATUS_CACHE["v"] is not None and now - _STATUS_CACHE["t"] < max_age:
        return _STATUS_CACHE["v"]
    # With a step running, the DB may be locked exclusively: the last known
    # value is returned instead of leaving the window blocked waiting for the timeout.
    if _STATUS_BUSY[0] and _STATUS_CACHE["v"] is not None:
        return _STATUS_CACHE["v"]
    root = PROJECT_ROOT
    db   = layout.database_path()

    version = None
    static_tsvs = glob.glob(os.path.join(_PKG_DIR, "*.tsv"))
    if static_tsvs:
        version = os.path.splitext(os.path.basename(static_tsvs[0]))[0]
    if not version:
        filt = glob.glob(os.path.join(root, "..", "data", "processed", "*.filt.tsv"))
        if filt:
            name = os.path.basename(max(filt, key=os.path.getmtime))
            version = name[:-len(".filt.tsv")]
    if not version:
        raw = [f for f in glob.glob(os.path.join(root, "..", "data", "processed", "*.tsv"))
               if not f.endswith(".filt.tsv")]
        if raw:
            version = os.path.splitext(os.path.basename(max(raw, key=os.path.getmtime)))[0]
    if not version:
        gz = glob.glob(os.path.join(root, "..", "data", "raw", "*.tar.gz"))
        if gz:
            name = os.path.basename(max(gz, key=os.path.getmtime))
            version = name[:-len(".tar.gz")] if name.endswith(".tar.gz") else os.path.splitext(name)[0]

    step1 = bool(glob.glob(os.path.join(root, "..", "data", "processed", "*.filt.tsv")))

    # Short timeout on purpose: this only paints status indicators. If the DB
    # is busy we'd rather fail in 0.3s (and keep the cached value) than block
    # the window; with timeout=3 we measured 4.6s of freeze per query.
    record_count = 0
    step2 = False
    outdated = False
    if os.path.exists(db):
        try:
            conn = sqlite3.connect(db, timeout=0.3)
            cur  = conn.cursor()
            cur.execute("SELECT 1 FROM bold_records LIMIT 1")
            if cur.fetchone():
                step2 = True
                # Usable by the viewer, but built in an older layout (before
                # schema 2 it left out every sequence after a specimen's first)
                outdated = db_contract.schema_version(conn) < db_contract.SCHEMA_VERSION
                rn = os.path.join(root, "..", "data", "processed", "records.number")
                if os.path.exists(rn):
                    try: record_count = int(open(rn).read().strip())
                    except Exception: pass
            conn.close()
        except Exception:
            pass

    step3 = False
    if step2:
        try:
            conn = sqlite3.connect(db, timeout=0.3)
            cur  = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='bold_records'")
            existing = {row[0] for row in cur.fetchall()}
            cur.execute("PRAGMA table_info(bold_records)")
            table_cols = {r[1] for r in cur.fetchall()}
            conn.close()
            # Same as Step 3: a configured field that isn't in the table can't
            # have an index, so requiring it would leave the step forever showing ○.
            expected = {"idx_{0}".format(c.lower())
                        for c in index_cols() if c in table_cols}
            step3    = bool(expected) and expected.issubset(existing)
        except Exception:
            pass

    step4 = False
    updatable = False
    if step2:
        try:
            conn = sqlite3.connect(db, timeout=0.3)
            cur  = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bold_records_fts'")
            # An index without col_processid (older versions) is shown as
            # pending: full-text search can't find BOLD IDs until it's rebuilt
            step4 = bool(cur.fetchone()) and "col_processid" in [
                d[0] for d in cur.execute("SELECT * FROM bold_records_fts LIMIT 0").description]
            # Built by a version that stores the record fingerprints (see run_update)
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (ROW_HASH_TABLE,))
            # ...and in the layout this version writes (see run_update)
            updatable = (bool(cur.fetchone()) and
                         db_contract.schema_version(conn) == db_contract.SCHEMA_VERSION)
            conn.close()
        except Exception:
            pass

    # The .filt.tsv can be deleted once imported (it takes up tens of GB), so
    # its absence with the DB already built doesn't mean Step 1 is missing:
    # marking it pending produced the absurd sequence "1 ○  2 ✅".
    st = {"version": version, "step1": step1, "step2": step2, "outdated": outdated,
          "step3": step3, "step4": step4, "record_count": record_count,
          "done1": step1 or step2, "updatable": updatable}
    _STATUS_CACHE.update(t=now, v=st)
    return st


# ---- Worker thread ----------------------------------------------------------------

class StepWorker(QThread):
    log_signal      = Signal(str)
    progress_signal = Signal(float)
    finished_signal = Signal(bool)

    def __init__(self, fn, cfg, parent=None):
        super().__init__(parent)
        self.fn        = fn
        self.cfg       = cfg
        self._stop_evt = threading.Event()

    def request_stop(self):
        self._stop_evt.set()

    def run(self):
        self.cfg["_stop"] = self._stop_evt
        _set_status_busy(True)
        try:
            ok = self.fn(
                log=self.log_signal.emit,
                progress=self.progress_signal.emit,
                cfg=self.cfg,
            )
        finally:
            # Must be released no matter what: otherwise the UI would keep
            # showing cached status forever.
            _set_status_busy(False)
        self.finished_signal.emit(bool(ok))


# ---- Source file copy ---------------------------------------------------------

class _CopyWorker(QThread):
    """Copies a large file to data/raw/ while reporting progress.

    shutil.copy on a 10 GB .tar.gz freezes the GUI for several minutes, so the
    copy runs in its own thread and in chunks.

    It's written as <name>.part and only renamed to its real name once
    complete: under its final name, the half-copied file already counted as
    Step 1's input (Run enabled mid-copy), and closing the app left a
    truncated .tar.gz that looked valid.
    """
    progress_signal = Signal(float)
    finished_signal = Signal(bool, str)

    _CHUNK = 8 * 1024 * 1024

    def __init__(self, src, dst, parent=None):
        super().__init__(parent)
        self._src = src
        self._dst = dst
        self._stop_evt = threading.Event()

    def request_stop(self):
        self._stop_evt.set()

    def run(self):
        part = self._dst + PART_SUFFIX
        try:
            total = os.path.getsize(self._src) or 1
            copied = 0
            with open(self._src, "rb") as fin, open(part, "wb") as fout:
                while True:
                    if self._stop_evt.is_set():
                        raise InterruptedError(t('copy_cancelled'))
                    chunk = fin.read(self._CHUNK)
                    if not chunk:
                        break
                    fout.write(chunk)
                    copied += len(chunk)
                    self.progress_signal.emit(min(copied / total * 100, 100))
            os.replace(part, self._dst)
            self.finished_signal.emit(True, self._dst)
        except Exception as e:
            try: os.remove(part)
            except OSError: pass
            self.finished_signal.emit(False, str(e))


# ---- DropFileCard widget -------------------------------------------------------

class DropFileCard(QFrame):
    """Drag-and-drop zone for Step 1's source file.

    If the dropped file is outside data/raw/, it gets copied there, which is
    where Step 1 looks for it. Previously the user had to make that copy by
    hand with nothing in the UI pointing it out.
    """
    file_changed = Signal(str)

    def __init__(self, label, dest_dir, extensions, hint, on_log=None, parent=None):
        super().__init__(parent)
        self.setObjectName("dropCard")
        self.setAcceptDrops(True)
        self._dest_dir   = dest_dir
        self._extensions = tuple(e.lower() for e in extensions)
        self._on_log     = on_log
        self._copier       = None
        self._path         = ""
        self._hint_default = hint

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        lbl = QLabel(label)
        _styled(lbl, "cardTitle")
        hdr.addWidget(lbl)
        hdr.addStretch()
        self._browse_btn = QPushButton(t('btn_browse'))
        self._browse_btn.setObjectName("cardBtn")
        self._browse_btn.setToolTip(t('tip_browse_disk'))
        self._browse_btn.clicked.connect(self._browse)
        hdr.addWidget(self._browse_btn)
        self._folder_btn = QPushButton(t('btn_open_folder'))
        self._folder_btn.setObjectName("cardBtn")
        self._folder_btn.setToolTip(t('tip_open_folder', dir=self._dest_dir))
        self._folder_btn.clicked.connect(lambda: _open_folder(self._dest_dir))
        hdr.addWidget(self._folder_btn)
        lay.addLayout(hdr)

        self._hint = QLabel(hint)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        _styled(self._hint, "dropHint")
        lay.addWidget(self._hint)

        self._file_lbl = QLabel()
        _styled(self._file_lbl, "dropFile")
        self._file_lbl.setWordWrap(True)
        self._file_lbl.hide()
        lay.addWidget(self._file_lbl)

        self._copy_bar = QProgressBar()
        self._copy_bar.setRange(0, 100)
        self._copy_bar.setTextVisible(False)
        self._copy_bar.setFixedHeight(_px(6))
        self._copy_bar.hide()
        lay.addWidget(self._copy_bar)

        self.scan_existing()

    # -- state ------------------------------------------------------------------

    def scan_existing(self):
        """Reflects whatever's already in data/raw/ without the user dropping anything.

        A copy in progress owns the card: rescanning mid-copy would replace the
        progress bar with the state of a file that isn't finished yet.
        """
        if self._copier and self._copier.isRunning():
            return
        found = []
        for ext in self._extensions:
            found += glob.glob(os.path.join(self._dest_dir, "*" + ext))
        if found:
            self._show_loaded(max(found, key=os.path.getmtime))
        else:
            self._show_empty()

    def _show_loaded(self, path):
        self._path = path
        try:
            size_gb = os.path.getsize(path) / (1024 ** 3)
            extra   = t('size_extra', gb=size_gb)
        except OSError:
            extra = ""
        self._file_lbl.setText(t('file_loaded_label', name=os.path.basename(path), extra=extra))
        self._file_lbl.show()
        self._hint.hide()
        self.setProperty("loaded", "true")
        self._repolish()

    def _show_empty(self):
        self._path = ""
        self._file_lbl.hide()
        self._hint.show()
        self.setProperty("loaded", "false")
        self._repolish()

    def _repolish(self):
        self.style().unpolish(self)
        self.style().polish(self)

    def _log(self, msg):
        if self._on_log:
            self._on_log(msg)

    def is_copying(self):
        return self._copier is not None and self._copier.isRunning()

    def cancel_copy(self):
        """Stops a copy in progress; the partial .part file is deleted."""
        if self.is_copying():
            self._copier.request_stop()

    # -- accepting a file ---------------------------------------------------

    def _accept(self, path):
        if not path or not os.path.isfile(path):
            return
        if not path.lower().endswith(self._extensions):
            QMessageBox.warning(
                self, t('invalid_file_title'),
                t('invalid_file_body', ext=" / ".join(self._extensions)),
            )
            return
        if os.path.normcase(os.path.dirname(os.path.abspath(path))) == \
           os.path.normcase(os.path.abspath(self._dest_dir)):
            self._show_loaded(path)          # already where it should be
            self.file_changed.emit(path)
            return
        self._start_copy(path)

    def _start_copy(self, src):
        if self._copier and self._copier.isRunning():
            return
        os.makedirs(self._dest_dir, exist_ok=True)
        dst = os.path.join(self._dest_dir, os.path.basename(src))
        if os.path.exists(dst):
            resp = QMessageBox.question(
                self, t('file_exists_title'),
                t('file_exists_body'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
        try:
            size_gb = os.path.getsize(src) / (1024 ** 3)
        except OSError:
            size_gb = 0
        self._log(t('copying_to_raw', name=os.path.basename(src), gb=size_gb))
        self._hint.setText(t('copying_hint'))
        self._hint.show()
        self._file_lbl.hide()
        self._copy_bar.setValue(0)
        self._copy_bar.show()
        self._browse_btn.setEnabled(False)
        self._copier = _CopyWorker(src, dst)
        self._copier.progress_signal.connect(lambda v: self._copy_bar.setValue(int(v)))
        self._copier.finished_signal.connect(lambda *_: _invalidate_status())
        self._copier.finished_signal.connect(self._on_copy_done)
        self._copier.start()

    def _on_copy_done(self, ok, info):
        self._copy_bar.hide()
        self._browse_btn.setEnabled(True)
        self._hint.setText(self._hint_default)
        if ok:
            self._log(t('file_copied', name=os.path.basename(info)))
            self._show_loaded(info)
            self.file_changed.emit(info)
        else:
            self._log(t('copy_failed', info=info))
            self._show_empty()
            self.file_changed.emit("")

    # -- events -----------------------------------------------------------------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("dragging", "true")
            self._repolish()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setProperty("dragging", "false")
        self._repolish()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.setProperty("dragging", "false")
        self._repolish()
        urls = event.mimeData().urls()
        if urls:
            self._accept(urls[0].toLocalFile())
            event.acceptProposedAction()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, t('select_downloaded_file'), "",
            t('bold_file_filter')
        )
        if path:
            self._accept(path)


# ---- StepPanel widget ----------------------------------------------------------

class StepPanel(QWidget):

    def __init__(self, label, desc, fn, cfg, app, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.fn        = fn
        self.cfg       = dict(cfg)
        self.app       = app
        self._worker   = None
        self._stopping = False
        # True from Run until set_done_ui. QThread.isRunning() isn't enough:
        # the worker emits finished_signal from inside run(), so when the
        # slot executes the thread may still report itself as running.
        self._active   = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(6)

        # ---- Title + requirement badge ---------------------------------------
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel(label)
        _styled(title, "stepTitle")
        title_row.addWidget(title)

        level, level_key = STEP_LEVEL.get(label, ("", "opt"))
        if level:
            badge = QLabel(level)
            badge.setProperty("level", level_key)
            _styled(badge, "badge")
            badge.setToolTip({
                t('badge_required'):    t('badge_tip_required'),
                t('badge_recommended'): t('badge_tip_recommended'),
                t('badge_optional'):    t('badge_tip_optional'),
            }.get(level, ""))
            title_row.addWidget(badge)
        title_row.addStretch()

        lay.addLayout(title_row)

        tech = STEP_TECH.get(label, "")
        if tech:
            tech_lbl = QLabel(tech)
            _styled(tech_lbl, "techLabel")
            lay.addWidget(tech_lbl)

        # The input/output paths are always the description's last line. It
        # used to be located by searching for "\nEntrada:", which never
        # matched the English text ("Input:"), so in English the paths were
        # shown run into the description instead of in their own block.
        desc_body, _nl, desc_paths = desc.strip().rpartition("\n")
        if not _nl:
            desc_body, desc_paths = desc_paths, ""
        desc_body, desc_paths = desc_body.strip(), desc_paths.strip()

        desc_lbl = QLabel(desc_body)
        _styled(desc_lbl, "secondary")
        desc_lbl.setWordWrap(True)
        lay.addWidget(desc_lbl)

        if desc_paths:
            lay.addWidget(_hsep_line())
            paths_lbl = QLabel(desc_paths)
            _styled(paths_lbl, "pathsLabel")
            paths_lbl.setWordWrap(True)
            lay.addWidget(paths_lbl)

        lay.addWidget(_hsep_line())
        lay.addSpacing(4)

        self._label = label

        # Step 1 is the only one that needs a file brought in by the user: it's
        # offered here instead of expecting them to copy it by hand into data/raw/.
        self._drop_card = None
        if fn is run_step1:
            self._drop_card = DropFileCard(
                t('bold_downloaded_file'),
                _proj_dir("data", "raw"),
                (".tar.gz",),
                t('drop_targz_hint'),
                on_log=app.log,
            )
            self._drop_card.file_changed.connect(lambda *_: self.app._refresh_all_panels_input())
            lay.addWidget(self._drop_card)

            # Remembered in data/state/ui_state.ini. self.cfg is the same dict the
            # Home pipelines pass to this step, so they honour it too.
            settings = getattr(app, "_settings", None)
            keep = False
            if settings is not None:
                keep = str(settings.value("step1/keep_targz", "false")).lower() == "true"
            self.cfg["keep_targz"] = keep
            keep_cb = QCheckBox(t('keep_targz_label'))
            keep_cb.setChecked(keep)
            keep_cb.setToolTip(t('keep_targz_tip'))

            def _on_keep(on):
                self.cfg["keep_targz"] = bool(on)
                if settings is not None:
                    settings.setValue("step1/keep_targz", "true" if on else "false")
                    settings.sync()
            keep_cb.toggled.connect(_on_keep)
            lay.addWidget(keep_cb)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        self._input_lbl = QLabel()
        _styled(self._input_lbl, "secondary")
        self._input_lbl.setWordWrap(True)
        input_row.addWidget(self._input_lbl, stretch=1)
        self._input_btn = QPushButton(t('btn_open_input_folder'))
        self._input_btn.setObjectName("cardBtn")
        self._input_btn.setToolTip(t('tip_open_input_folder'))
        self._input_btn.clicked.connect(self._open_input_folder)
        self._input_btn.hide()
        input_row.addWidget(self._input_btn, alignment=Qt.AlignmentFlag.AlignTop)
        lay.addLayout(input_row)

        self._build_config(lay, label)

        lay.addStretch()

        ctrl = QHBoxLayout()
        ctrl.setSpacing(14)
        self.run_btn = QPushButton(t('btn_run_step'))
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._start)
        ctrl.addWidget(self.run_btn)
        self.stop_btn = QPushButton(t('btn_stop'))
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setToolTip(t('tip_stop'))
        self.stop_btn.clicked.connect(self._stop)
        ctrl.addWidget(self.stop_btn)
        self.status_lbl = QLabel("")
        _styled(self.status_lbl, "caption")
        ctrl.addWidget(self.status_lbl)
        ctrl.addStretch()
        lay.addLayout(ctrl)

        self.prog_bar = QProgressBar()
        self.prog_bar.setRange(0, 100)
        self.prog_bar.setValue(0)
        self.prog_bar.setTextVisible(False)
        self.prog_bar.setFixedHeight(_px(10))
        lay.addWidget(self.prog_bar)

        # Note below the bar: without it, an indeterminate bar running for
        # hours (Step 4) looks like a hang.
        self._prog_note = QLabel("")
        _styled(self._prog_note, "caption")
        self._prog_note.setWordWrap(True)
        self._prog_note.hide()
        lay.addWidget(self._prog_note)

        self._refresh_input_status()

    def _open_input_folder(self):
        folders = {
            STEPS[0][0]: _proj_dir("data", "raw"),
            STEPS[1][0]: _proj_dir("data", "processed"),
            STEPS[2][0]: os.path.dirname(layout.database_path()),
            STEPS[3][0]: os.path.dirname(layout.database_path()),
            STEPS[4][0]: _proj_dir("data", "processed"),
        }
        _open_folder(folders.get(self._label, _proj_dir("data")))

    def _build_config(self, lay, label):
        fn = STEPS[[s[0] for s in STEPS].index(label)][2]
        if fn is not run_step2:
            return
        # Fine-tuning parameter: collapsed by default so as not to drop a
        # numeric field with no context onto the user in the main view.
        toggle = QPushButton(t('toggle_advanced_options_closed'))
        toggle.setObjectName("cardBtn")
        toggle.setToolTip(t('tip_rarely_needed'))
        lay.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)

        adv = QWidget()
        row = QHBoxLayout(adv)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(8)
        lbl = QLabel(t('records_per_batch_label'))
        _styled(lbl, "body")
        row.addWidget(lbl)
        entry = QLineEdit(str(CHUNKSIZE))
        entry.setFixedWidth(_px(80))
        _tip = t('chunk_size_tip', n=CHUNKSIZE)
        lbl.setToolTip(_tip)
        entry.setToolTip(_tip)
        entry.textChanged.connect(
            lambda v: self.cfg.update({"chunksize": int(v) if v.isdigit() else CHUNKSIZE})
        )
        row.addWidget(entry)
        hint = QLabel(t('default_chunk_hint', n=CHUNKSIZE))
        _styled(hint, "hint")
        row.addWidget(hint)
        row.addStretch()
        adv.hide()
        lay.addWidget(adv)

        def _toggle_adv():
            vis = not adv.isVisible()
            adv.setVisible(vis)
            toggle.setText(t('toggle_advanced_options_open') if vis else t('toggle_advanced_options_closed'))
        toggle.clicked.connect(_toggle_adv)

    def is_running(self):
        return self._active

    # What's missing and where, instead of a generic "Input not found".
    _MISSING_MSG = {
        STEPS[0][0]: t('missing_bold_file'),
        STEPS[1][0]: t('missing_prepared_data'),
        STEPS[2][0]: t('missing_database'),
        STEPS[3][0]: t('missing_database'),
        STEPS[4][0]: t('missing_update_input'),
    }

    def _missing_msg(self):
        """What's missing for this step. For the update it depends on why it
        can't run: no database yet, a database without fingerprints, or no
        prepared data for the new package."""
        if self.fn is run_update:
            try:
                st = _get_project_status()
            except Exception:
                st = {}
            if not st.get("step2"):
                return t('missing_update_no_db')
            if not st.get("updatable"):
                return t('missing_update_old_db')
        return self._MISSING_MSG.get(self._label, t('input_not_found_generic'))

    def _is_done(self):
        """Has this step already produced its output? Shown differently from 'missing input'."""
        try:
            st = _get_project_status()
        except Exception:
            return False
        return {
            STEPS[0][0]: st["done1"],
            STEPS[1][0]: st["step2"] and not st["outdated"],
            STEPS[2][0]: st["step3"],
            STEPS[3][0]: st["step4"],
            STEPS[4][0]: False,      # an update is never "done": each package brings new data
        }.get(self._label, False)

    def _refresh_input_status(self):
        if self._drop_card is not None:
            self._drop_card.scan_existing()
        ok = _check_input(self._label)
        if self._is_done():
            self._input_lbl.setText(t('step_done_already'))
            _styled(self._input_lbl, tone="ok")
            self._input_btn.hide()
        elif ok:
            self._input_lbl.setText(t('step_ready'))
            _styled(self._input_lbl, tone="ok")
            self._input_btn.hide()
        else:
            self._input_lbl.setText(self._missing_msg())
            _styled(self._input_lbl, tone="warn")
            self._input_btn.show()
        if not self.is_running():
            busy = self.app._is_any_running()
            self.run_btn.setEnabled(ok and not busy)
            # A grayed-out button with no explanation is the most common
            # source of confusion: it says why.
            if busy:
                self.run_btn.setToolTip(t('other_step_running_tip'))
            elif not ok and self._is_done():
                self.run_btn.setToolTip(t('step_done_files_deleted_tip'))
            elif not ok:
                self.run_btn.setToolTip(
                    t('missing_input_files_tip', msg=self._missing_msg().lstrip("● ")))
            else:
                self.run_btn.setToolTip(t('run_only_step_tip'))

    def _start(self):
        if self.is_running():
            return
        # Another step or a pipeline may be running (the button can be stale)
        if self.app._is_any_running():
            QMessageBox.warning(self, t('step_running_title'), t('pipeline_blocked_body'))
            return
        if not self.app._ensure_no_pending_build(self):
            return
        self._refresh_input_status()
        self._active = True
        self.app.refresh_sidebar_status()   # disables the Home pipeline buttons
        self._set_running_ui()
        # The log isn't cleared: clearing it here made the initial status
        # summary disappear, and that's the only guide to where the project stands.
        self.app.mark_task_start()
        self.app.log("\n▶ === {} ===".format(self._label))
        self.app.start_timer()
        self._worker = StepWorker(self.fn, self.cfg)
        self._worker.log_signal.connect(self.app.log)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.finished_signal.connect(self.set_done_ui)
        # After set_done_ui (slots run in connection order): the run is over
        self._worker.finished_signal.connect(lambda _ok: self.app._settle_db_build())
        self._worker.start()

    def _set_progbar_color(self, color):
        lighter = {
            P["progbar"]: P["progbar_light"],
            P["yellow"]:  P["yellow_light"],
            P["red"]:     P["red_light"],
        }.get(color, color)
        self.prog_bar.setStyleSheet(
            f"QProgressBar {{ background-color: {P['surface']}; border: none; border-radius: 5px; }}"
            f"QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {lighter}, stop:1 {color}); border-radius: 5px; }}"
        )

    def _on_progress(self, v):
        if self._stopping:
            return
        if v < 0:
            self.prog_bar.setRange(0, 0)
            self._prog_note.setText(t('no_progress_note'))
            self._prog_note.show()
        else:
            if self.prog_bar.maximum() == 0:
                self.prog_bar.setRange(0, 100)
                self._prog_note.hide()
            self.prog_bar.setValue(int(v))

    def _set_status(self, tone, text):
        """Shows the step's state next to the buttons. "donePill" is a filled
        badge; the rest are plain text. The label is kept as tall as the
        buttons, so the badge lines up with them instead of growing the row.
        Its side margins are set here, not as stylesheet padding: on a label
        already on screen, Qt left that padding out of the size hint and the
        text filled the badge edge to edge."""
        lbl = self.status_lbl
        pill = tone == "donePill"
        _styled(lbl, tone=tone)
        lbl.setText(text)
        side = _px(24) if pill else 0
        lbl.setContentsMargins(side, 0, side, 0)
        lbl.setFixedHeight(self.run_btn.height())
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter if pill
                         else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def set_stopping_ui(self):
        self._stopping = True
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.prog_bar.setRange(0, 100)
        self._set_progbar_color(P["yellow"])
        self._set_status("warn", t('stopping_status'))

    def _set_running_ui(self):
        self._stopping = False
        self.run_btn.setEnabled(False)
        self.run_btn.setToolTip(t('already_running_tip'))
        self.stop_btn.setEnabled(True)
        self._prog_note.hide()
        self.prog_bar.setRange(0, 100)
        self.prog_bar.setValue(0)
        self._set_progbar_color(P["progbar"])
        self._set_status("running", t('running_status'))

    def set_running_ui(self):
        self._set_running_ui()

    def set_done_ui(self, ok):
        self._active = False
        _invalidate_status()          # the step just changed the on-disk state
        self.app._flush_log_file()    # the on-disk log is now up to date
        was_stopping   = self._stopping
        self._stopping = False
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if not self.app._seq_running:
            self.app.stop_timer()
        self.prog_bar.setRange(0, 100)
        self._prog_note.hide()
        self._refresh_input_status()
        self.app.refresh_sidebar_status()
        if not self.app._seq_running:
            self.app._refresh_all_panels_input()
        if ok:
            self._set_status("donePill", t('completed_status'))
            self.prog_bar.setValue(100)
            if not self.app._seq_running:
                self.app._dim_completed_task()
                self.app.log_next_action()
        elif was_stopping:
            self._set_progbar_color(P["yellow"])
            self._set_status("warn", t('stopped_status'))
            self.prog_bar.setValue(0)
        else:
            self._set_progbar_color(P["red"])
            self._set_status("error", t('error_status'))

    def set_stopped_ui(self):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.app.stop_timer()
        self.prog_bar.setRange(0, 100)
        self.prog_bar.setValue(0)
        self._set_progbar_color(P["yellow"])
        self._set_status("warn", t('stopped_status'))

    def _stop(self):
        self.app.stop_current(self)


# ---- Update workers ---------------------------------------------------------

class _UpdateCheck(QThread):
    """Asks GitHub for the newest release, off the GUI thread."""
    done = Signal(object)

    def run(self):
        self.done.emit(updater.latest_release())


class _UpdateDownload(QThread):
    """Downloads and unpacks a release, ready for the next start."""
    progress = Signal(int, int)
    finished_with = Signal(object)        # None, or the error as text

    def __init__(self, release, parent=None):
        super().__init__(parent)
        self._release = release

    def run(self):
        try:
            updater.download_and_stage(self._release,
                                       progress=lambda d, tot: self.progress.emit(d, tot))
            self.finished_with.emit(None)
        except Exception as e:
            self.finished_with.emit(_cause_str(e))


# ---- Home panel -------------------------------------------------------------

class PrepPanel(QWidget):
    """Entry point: what's needed before starting, and the main button.

    Previously the application opened straight into Step 1 without explaining
    anywhere that a file has to be downloaded from BOLD, or how much space is needed.
    """

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.app = app

        # The content is taller than the panel on small windows or with high
        # DPI scaling: without scrolling, the status got cut off at the bottom.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # No horizontal bar: text must wrap to the column width, never force
        # horizontal scrolling to read it.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        inner = QWidget()
        inner.setObjectName("panel")
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        lay = QVBoxLayout(inner)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(8)

        title = QLabel(t('home_title'))
        _styled(title, "pageTitle")
        lay.addWidget(title)

        # A newer version on GitHub (App._check_updates fills it in)
        self.update_banner = QFrame()
        self.update_banner.setObjectName("updateBanner")
        ub = QHBoxLayout(self.update_banner)
        ub.setContentsMargins(12, 8, 8, 8)
        ub.setSpacing(8)
        self.upd_lbl = QLabel("")
        self.upd_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.upd_lbl.setWordWrap(True)
        _styled(self.upd_lbl, "body")
        ub.addWidget(self.upd_lbl, 1)
        self.upd_see_btn = QPushButton(t('upd_see'))
        self.upd_see_btn.setObjectName("cardBtn")
        self.upd_see_btn.clicked.connect(self.app.open_release_page)
        ub.addWidget(self.upd_see_btn)
        self.upd_btn = QPushButton(t('upd_install'))
        self.upd_btn.setObjectName("primaryBtn")
        self.upd_btn.clicked.connect(self.app.update_button_clicked)
        ub.addWidget(self.upd_btn)
        self.update_banner.hide()
        lay.addWidget(self.update_banner)

        intro = QLabel(t('home_intro'))
        intro.setWordWrap(True)
        _styled(intro, "body")
        lay.addWidget(intro)

        # The three requirements used to be spelled out one by one here. What
        # the user actually has to *do* is bring in the .tar.gz, so that gets
        # the same drop zone as Step 1 and the rest is condensed into one line:
        # otherwise the only way to load the file from Home was to go to Step 1.
        req_note = QLabel(t('home_req_note'))
        req_note.setTextFormat(Qt.TextFormat.RichText)
        req_note.setWordWrap(True)
        _styled(req_note, "secondary")
        lay.addWidget(req_note)

        self._drop_card = DropFileCard(
            t('bold_downloaded_file'),
            _proj_dir("data", "raw"),
            (".tar.gz",),
            t('drop_targz_hint'),
            on_log=app.log,
        )
        self._drop_card.file_changed.connect(lambda *_: self.app._refresh_all_panels_input())
        lay.addWidget(self._drop_card)

        lay.addWidget(_hsep_line())

        # ---- Summary status ---------------------------------------------------
        self._status_lbl = QLabel("")
        self._status_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._status_lbl.setWordWrap(True)
        _styled(self._status_lbl, "body")
        lay.addWidget(self._status_lbl)

        lay.addStretch()

        # ---- Main actions: the two ways to build it -----------------------------
        # One below the other, not in a row: with the log taking up the right
        # column, two side-by-side buttons forced a minimum width that cropped
        # the whole panel.
        opts = QVBoxLayout()
        opts.setSpacing(4)
        self.pipeline_btns = []
        for n, pl in enumerate(_PIPELINES):
            if n:
                opts.addSpacing(_px(26))

            # Header: "Option 1" + its badge, so they read as two alternatives
            # to choose between, not as two unrelated actions.
            hdr_row = QHBoxLayout()
            hdr_row.setSpacing(8)
            hdr = QLabel(pl["title"])
            _styled(hdr, "optionTitle")
            hdr_row.addWidget(hdr)
            if pl["badge"]:
                badge = QLabel(pl["badge"])
                badge.setProperty("level", "ok")
                _styled(badge, "badge")
                hdr_row.addWidget(badge)
            hdr_row.addStretch()
            opts.addLayout(hdr_row)
            opts.addSpacing(_px(2))

            btn = QPushButton("▶   " + pl["label"])
            btn.setObjectName("primaryBtn" if pl["badge"] else "secondaryBtn")
            btn.setMinimumHeight(_px(46))
            btn.setToolTip(pl["tip"])
            btn.clicked.connect(
                lambda checked=False, p=pl: self.app._run_sequence(p["steps"], p["label"])
            )
            opts.addWidget(btn)
            self.pipeline_btns.append(btn)

            hint = QLabel(pl["hint"])
            hint.setWordWrap(True)
            _styled(hint, "small")
            opts.addWidget(hint)
            if pl.get("needs_updatable_db"):
                self._update_hint = hint
        lay.addLayout(opts)

        # Compatibility with the rest of the class (bulk enable/disable)
        self.main_btn = self.pipeline_btns[0]

        self._main_hint = QLabel("")
        self._main_hint.setWordWrap(True)
        _styled(self._main_hint, "small")
        lay.addWidget(self._main_hint)

        # ---- Secondary actions -------------------------------------------------
        # In a 2x2 grid: in a single row they imposed a minimum width larger
        # than the column's, and the whole panel ended up with horizontal scroll.
        row2 = QGridLayout()
        row2.setSpacing(8)
        row2._n = 0

        def _add(btn):
            row2.addWidget(btn, row2._n // 2, row2._n % 2)
            row2._n += 1

        self.viewer_btn = QPushButton(t('btn_open_viewer'))
        self.viewer_btn.setObjectName("viewerBtn")
        self.viewer_btn.setToolTip(t('tip_open_viewer', port=VIEWER_PORT))
        self.viewer_btn.clicked.connect(self.app.open_viewer)
        _add(self.viewer_btn)

        manual_btn = QPushButton(t('btn_manual'))
        manual_btn.setObjectName("cardBtn")
        manual_btn.setToolTip(t('tip_manual'))
        manual_btn.clicked.connect(self.app.open_manual)
        _add(manual_btn)

        bold_btn = QPushButton(t('btn_download_bold'))
        bold_btn.setObjectName("cardBtn")
        bold_btn.setToolTip(t('tip_open_browser', url=BOLD_DOWNLOAD_URL))
        bold_btn.clicked.connect(
            lambda: webbrowser.open(BOLD_DOWNLOAD_URL)
        )
        _add(bold_btn)

        raw_btn = QPushButton(t('btn_open_raw_folder'))
        raw_btn.setObjectName("cardBtn")
        raw_btn.setToolTip(t('tip_raw_folder'))
        raw_btn.clicked.connect(lambda: _open_folder(_proj_dir("data", "raw")))
        _add(raw_btn)

        exports_btn = QPushButton(t('btn_open_exports'))
        exports_btn.setObjectName("cardBtn")
        exports_btn.setToolTip(t('tip_open_exports'))
        exports_btn.clicked.connect(lambda: _open_folder(layout.EXPORTS_DIR))
        _add(exports_btn)

        self.free_btn = QPushButton(t('btn_free_space'))
        self.free_btn.setObjectName("dangerBtn")
        self.free_btn.clicked.connect(self.app.free_disk_space)
        _add(self.free_btn)
        lay.addLayout(row2)

        # ---- Shortcuts --------------------------------------------------------
        lay.addSpacing(_px(10))
        lay.addWidget(_hsep_line())
        sc_title = QLabel(t('sc_section'))
        _styled(sc_title, "optionTitle")
        lay.addWidget(sc_title)
        sc_intro = QLabel(t('sc_intro'))
        sc_intro.setWordWrap(True)
        _styled(sc_intro, "small")
        lay.addWidget(sc_intro)
        sc_row = QHBoxLayout()
        sc_row.setSpacing(12)
        self.sc_desktop = QCheckBox(t('sc_desktop'))
        self.sc_menu = QCheckBox(t('sc_menu_' + (sys.platform if sys.platform in ("win32", "darwin") else "linux")))
        self.sc_desktop.setChecked(True)
        self.sc_menu.setChecked(True)
        sc_row.addWidget(self.sc_desktop)
        sc_row.addWidget(self.sc_menu)
        sc_row.addStretch()
        lay.addLayout(sc_row)
        sc_btns = QHBoxLayout()
        sc_btns.setSpacing(8)
        self.sc_create_btn = QPushButton(t('sc_create'))
        self.sc_create_btn.setObjectName("cardBtn")
        self.sc_create_btn.clicked.connect(self.app.create_shortcuts)
        sc_btns.addWidget(self.sc_create_btn)
        self.sc_remove_btn = QPushButton(t('sc_remove'))
        self.sc_remove_btn.setObjectName("dangerBtn")
        self.sc_remove_btn.clicked.connect(self.app.remove_shortcuts)
        sc_btns.addWidget(self.sc_remove_btn)
        sc_btns.addStretch()
        lay.addLayout(sc_btns)
        self.sc_status = QLabel("")
        self.sc_status.setWordWrap(True)
        _styled(self.sc_status, "small")
        lay.addWidget(self.sc_status)

        lay.addSpacing(_px(10))
        un_row = QHBoxLayout()
        un_row.addStretch()
        uninstall_btn = QPushButton(t('btn_uninstall'))
        uninstall_btn.setObjectName("dangerBtn")
        uninstall_btn.clicked.connect(self.app.uninstall)
        un_row.addWidget(uninstall_btn)
        lay.addLayout(un_row)

        self.refresh()
        self.refresh_shortcuts()

    def refresh_shortcuts(self):
        made = shortcuts.current()
        if made:
            self.sc_desktop.setChecked(made["desktop"])
            self.sc_menu.setChecked(made["menu"])
            where = [w for w, on in ((self.sc_desktop.text(), made["desktop"]),
                                     (self.sc_menu.text(), made["menu"])) if on]
            self.sc_status.setText(t('sc_made', where=t('sc_and').join(where)))
        else:
            self.sc_status.setText(t('sc_none'))
        self.sc_create_btn.setText(t('sc_recreate') if made else t('sc_create'))
        self.sc_remove_btn.setEnabled(bool(made))

    def refresh(self, st=None):
        st = st or _get_project_status()
        self._drop_card.scan_existing()
        def mark(done): return f'<span style="color:{P["ok"]}">{STATUS_DONE}</span>' if done \
                          else f'<span style="color:{P["yellow"]}">{STATUS_TODO}</span>'
        cnt = t('records_count_suffix', n=st["record_count"]) if st["record_count"] else ""
        ver = st["version"] or t('no_data_detected')
        self._status_lbl.setText(
            f'<b>{t("data_version_label")}</b> {ver}<br>'
            f'{mark(st["done1"])} {t("status_step1_label")} &nbsp;&nbsp;'
            f'{mark(st["step2"])} {t("status_step2_label", cnt=cnt)}<br>'
            f'{mark(st["step3"])} {t("status_step3_label")} &nbsp;&nbsp;'
            f'{mark(st["step4"])} {t("status_step4_label")}'
        )
        # Free disk space: only makes sense once the DB is already built. Before
        # that, the .filt.tsv is Step 2's input, and deleting it would destroy the work done.
        inter = _intermediate_files()
        total = sum(s for _, s in inter)
        if inter:
            self.free_btn.setText(t('free_space_btn_with_size', size=_fmt_size(total)))
        else:
            self.free_btn.setText(t('btn_free_space'))
        self.free_btn.setEnabled(bool(inter) and st["step2"])
        if not inter:
            self.free_btn.setToolTip(t('no_intermediate_files'))
        elif not st["step2"]:
            self.free_btn.setToolTip(t('available_after_db_built'))
        else:
            self.free_btn.setToolTip(t('free_space_tooltip', size=_fmt_size(total)))

        ready = st["step2"] and st["step3"]
        self.viewer_btn.setEnabled(st["step2"])
        self.viewer_btn.setToolTip(
            t('viewer_ready_tip', port=VIEWER_PORT)
            if st["step2"] else t('viewer_not_ready_tip')
        )
        # Each button reflects whether its pipeline is already satisfied: redo, not create.
        # RAM next to the database size: whether the indexes fit in memory is
        # what decides how much faster updating is than rebuilding.
        upd = next((pl for pl in _PIPELINES if pl.get("needs_updatable_db")), None)
        if upd is not None and getattr(self, "_update_hint", None) is not None:
            db_file = layout.database_path()
            try:
                db_part = (t('db_size_part', size=_fmt_size(os.path.getsize(db_file)))
                           if st["step2"] else "")
            except OSError:
                db_part = ""
            self._update_hint.setText(t('pipeline3_hint', ram=upd["ram"], db=db_part))
        # One flag per entry of STEPS; the update (index 4) is never "done"
        flags = [st["done1"], st["step2"] and not st["outdated"], st["step3"], st["step4"], False]
        # Disabled while any step runs, not just during a pipeline: launching
        # Steps 1-3 while Step 4 ran on its own put two steps on the same DB.
        busy = self.app._is_any_running()
        for btn, pl in zip(self.pipeline_btns, _PIPELINES):
            done = all(flags[i] for i in pl["steps"])
            btn.setText(("↻   " + pl["label_done"]) if done else ("▶   " + pl["label"]))
            blocker = _pipeline_blocker(pl, st, flags)
            btn.setEnabled(not busy and blocker is None)
            if busy:
                btn.setToolTip(t('other_step_running_tip'))
            elif blocker:
                btn.setToolTip(t(blocker))
            else:
                btn.setToolTip(pl["tip"])
        if ready:
            self._main_hint.setText(t('main_hint_ready'))
        else:
            self._main_hint.setText(t('main_hint_not_ready'))


# ---- Fields panel ----------------------------------------------------------------

_SRC_TSV = t('src_tsv')
_SRC_CAT = t('src_catalog')

_HEADER_CACHE_FILE = "tsv_header.txt"


def _peek_tar_header(gz_path):
    """Reads only the .tsv header inside a .tar.gz, without decompressing all of it.

    gzip is sequential: reading the .tsv's first line forces decoding the file
    from the start, but stops there — seconds, not the minutes it takes to
    extract the full ~20 GB. Returns None if it can't be read.
    """
    try:
        with tarfile.open(gz_path, mode="r:gz") as tar:
            for member in tar:
                if member.isfile() and member.name.lower().endswith(".tsv"):
                    f = tar.extractfile(member)
                    if f is None:
                        return None
                    return f.readline().decode("utf-8", errors="replace").rstrip("\r\n")
    except (OSError, tarfile.TarError):
        return None
    return None


def _cached_tar_header(gz_path):
    """Header of gz_path, cached on disk by the .tar.gz's name+size.

    The Fields panel calls this on every UI refresh: without a cache, each
    refresh would reopen the .tar.gz to read a couple KB of gzip.
    """
    cache_path = _proj_dir("data", "processed", _HEADER_CACHE_FILE)
    try:
        sig = "{}:{}".format(os.path.basename(gz_path), os.path.getsize(gz_path))
    except OSError:
        return None
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                if f.readline().rstrip("\n") == sig:
                    return f.readline().rstrip("\n")
        except OSError:
            pass
    header = _peek_tar_header(gz_path)
    if not header:
        return None
    try:
        os.makedirs(_proj_dir("data", "processed"), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(sig + "\n" + header + "\n")
    except OSError:
        pass
    return header


def _available_fields():
    """Fields that can be chosen, and where they come from.

    Always defers to the real header of the BOLD version present on disk:
    the already-decompressed .tsv if it exists, or otherwise the header read
    directly from the .tar.gz (without fully decompressing it). This way the
    check stays available even after Step 1 has already filtered and deleted
    the working files — previously it could only be compared during the brief
    window between decompressing and filtering.
    The in-code catalog is the last resort, for when nothing has been
    downloaded yet.
    The .filt.tsv doesn't work: it only contains the already-selected fields,
    so using it as the catalog would make everything discarded vanish from the list.
    """
    raw = [f for f in glob.glob(_proj_dir("data", "processed", "*.tsv"))
           if not f.endswith(".filt.tsv")]
    if raw:
        try:
            with open(max(raw, key=os.path.getsize), encoding="utf-8",
                      errors="replace") as f:
                headers = [h for h in f.readline().rstrip("\r\n").split("\t") if h]
            if len(headers) > 5:
                return headers, _SRC_TSV
        except OSError:
            pass

    gz_files = sorted(glob.glob(_proj_dir("data", "raw", "*.tar.gz")),
                       key=os.path.getmtime, reverse=True)
    if gz_files:
        header_line = _cached_tar_header(gz_files[0])
        if header_line:
            headers = [h for h in header_line.split("\t") if h]
            if len(headers) > 5:
                return headers, _SRC_TSV

    return list(BOLD_FIELDS), _SRC_CAT


class FieldsPanel(QWidget):
    """Editor for dev/fields_config.json.

    The file is the source of truth and can be hand-edited; this panel exists
    because the user of this tool is a biologist, not a programmer, and
    because it's where the fields the viewer can't work without get locked.
    """

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.app   = app
        self._rows = {}       # field name -> (include checkbox, index checkbox)
        self._src  = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 16)
        outer.setSpacing(8)

        title = QLabel(t('fields_panel_title'))
        _styled(title, "pageTitle")
        outer.addWidget(title)

        intro = QLabel(t('fields_panel_intro'))
        intro.setWordWrap(True)
        _styled(intro, "body")
        outer.addWidget(intro)

        warn = QLabel(t('fields_panel_warning'))
        warn.setTextFormat(Qt.TextFormat.RichText)
        warn.setWordWrap(True)
        _styled(warn, "note")
        outer.addWidget(warn)

        self._src_lbl = QLabel("")
        self._src_lbl.setWordWrap(True)
        _styled(self._src_lbl, "small")
        outer.addWidget(self._src_lbl)

        # ---- Header for the two checkbox columns -------------------------------
        hdr = QHBoxLayout()
        hdr.setContentsMargins(2, 0, _px(18), 0)
        h1 = QLabel(t('col_header_field'))
        h1.setToolTip(t('tip_include_field'))
        _styled(h1, "colHeader")
        hdr.addWidget(h1)
        hdr.addStretch()
        h2 = QLabel(t('col_header_index'))
        h2.setToolTip(t('tip_create_index'))
        _styled(h2, "colHeader")
        hdr.addWidget(h2)
        outer.addLayout(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._scroll = scroll
        outer.addWidget(scroll, stretch=1)

        self._count_lbl = QLabel("")
        self._count_lbl.setWordWrap(True)
        _styled(self._count_lbl, "body")
        outer.addWidget(self._count_lbl)

        # The main one takes the full width, as in PrepPanel. The two support
        # buttons go in the same 2x1 grid PrepPanel uses for its bottom
        # buttons (row2/_add): same "cardBtn" style and same symmetry — each
        # column sizes to its own content instead of forcing everything into
        # a single row, which is what cropped the longest text.
        self._save_btn = QPushButton(t('btn_save_selection'))
        self._save_btn.setObjectName("primaryBtn")
        self._save_btn.clicked.connect(self._save)
        outer.addWidget(self._save_btn)

        row2 = QGridLayout()
        row2.setSpacing(8)

        reset_btn = QPushButton(t('btn_reset_fields'))
        reset_btn.setObjectName("cardBtn")
        reset_btn.setToolTip(t('tip_reset_fields', n=len(DEFAULT_FIELDS)))
        reset_btn.clicked.connect(self._restore_defaults)
        row2.addWidget(reset_btn, 0, 0)

        self._check_btn = QPushButton(t('btn_check_tsv'))
        self._check_btn.setObjectName("cardBtn")
        self._check_btn.clicked.connect(self._check_now)
        row2.addWidget(self._check_btn, 0, 1)
        outer.addLayout(row2)

        self._report = None
        self._populate()
        self.refresh()

    # ---- Building the list ---------------------------------------------------

    def _populate(self):
        names, src = _available_fields()
        cfg = load_fields_cfg()
        # Verification only makes sense against the real header: against the
        # in-code catalog it would compare the selection with itself.
        self._report = verify_fields(names, cfg) if src == _SRC_TSV else None
        # A configured field that isn't in the catalog is shown anyway: hiding
        # it would drop it from the selection without the user noticing.
        known = {_norm_name(n) for n in names}
        names = list(names) + [c for c in cfg["fields"] if _norm_name(c) not in known]

        self._src   = src
        self._rows  = {}
        host = QWidget()
        host.setObjectName("panel")
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, _px(8), 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(0, 1)

        required = {_norm_name(c) for c in REQUIRED_FIELDS}
        # Verification markers: which fields aren't in the downloaded TSV, and
        # which ones BOLD has added relative to the catalog this was written against.
        rep   = self._report
        gone  = {_norm_name(m) for m in (rep["missing"] if rep else [])}
        added = {_norm_name(a) for a in (rep["added"]  if rep else [])}

        for r, name in enumerate(names):
            n   = _norm_name(name)
            inc = QCheckBox(name)
            idx = QCheckBox("")
            idx.setToolTip(t('tip_index_field', name=name))
            if n in required:
                inc.setChecked(True)
                inc.setEnabled(False)
                inc.setToolTip(t('tip_required_field'))

            tag = QLabel("")
            if n in gone:
                tag.setText(t('tag_not_in_tsv'))
                _styled(tag, "caption", tone="error")
                hint = rep["hints"].get(name)
                tag.setToolTip(
                    t('tip_field_missing')
                    + (t('tip_field_missing_hint', hint=hint) if hint else "")
                )
            elif n in added:
                tag.setText(t('tag_new_in_bold'))
                _styled(tag, "caption", tone="rec")
                tag.setToolTip(t('tip_new_field'))

            inc.toggled.connect(
                lambda on, i=idx: self._on_include_toggled(on, i))
            grid.addWidget(inc, r, 0)
            grid.addWidget(tag, r, 1)
            grid.addWidget(idx, r, 2, alignment=Qt.AlignmentFlag.AlignRight)
            self._rows[name] = (inc, idx)
        grid.setRowStretch(len(names), 1)
        self._scroll.setWidget(host)
        self._apply_cfg(cfg)

    def _on_include_toggled(self, on, idx_cb):
        # Indexing a field that isn't stored is meaningless
        idx_cb.setEnabled(on)
        if not on:
            idx_cb.setChecked(False)
        self._update_counts()

    def _apply_cfg(self, cfg):
        sel = {_norm_name(c) for c in cfg["fields"]}
        ind = {_norm_name(c) for c in cfg["indexed"]}
        for name, (inc, idx) in self._rows.items():
            n  = _norm_name(name)
            on = n in sel
            inc.blockSignals(True)
            inc.setChecked(on or not inc.isEnabled())
            inc.blockSignals(False)
            idx.setEnabled(inc.isChecked())
            idx.setChecked(n in ind and inc.isChecked())
        self._update_counts()

    # ---- State ------------------------------------------------------------------

    def _selection(self):
        fields  = [n for n, (inc, _) in self._rows.items() if inc.isChecked()]
        indexed = [n for n, (inc, idx) in self._rows.items()
                   if inc.isChecked() and idx.isChecked()]
        return fields, indexed

    def _is_dirty(self):
        """Does what's shown on screen differ from what's in the file?

        Compared against the file, not against a flag: "Restore defaults"
        leaves the checkboxes as they are without writing anything, and
        without this comparison the panel showed 23 fields while the file had 24.
        """
        fields, indexed = self._selection()
        saved = load_fields_cfg()
        return ([_norm_name(c) for c in fields]  != [_norm_name(c) for c in saved["fields"]] or
                sorted(_norm_name(c) for c in indexed) !=
                sorted(_norm_name(c) for c in saved["indexed"]))

    def _update_counts(self):
        fields, indexed = self._selection()
        txt = t('counts_text', n=len(fields), m=len(indexed))
        if self._is_dirty():
            txt += t('unsaved_changes_badge', c=P["rec"])
            self._save_btn.setText(t('btn_save_selection_dirty'))
        else:
            self._save_btn.setText(t('btn_save_selection'))
        self._count_lbl.setText(txt)

    def refresh(self, st=None):
        # The list is only rebuilt if its source changed (the downloaded TSV
        # appears or disappears): rebuilding 76 rows on every UI refresh would
        # throw away the user's half-finished checkbox work.
        if _available_fields()[1] != self._src:
            self._populate()
        src_txt = t('fields_source_text', src=self._src) + self._check_txt()
        stale = self._stale_marker()
        if stale:
            src_txt += t('stale_db_note', c=P["yellow"])
        self._src_lbl.setText(src_txt)
        self._check_btn.setEnabled(self._src == _SRC_TSV)
        self._check_btn.setToolTip(
            t('tip_check_tsv_available') if self._src == _SRC_TSV else
            t('tip_check_tsv_unavailable'))
        running = self.app._is_any_running()
        self._save_btn.setEnabled(not running)
        self._save_btn.setToolTip(
            t('tip_step_running') if running
            else t('tip_save_selection'))

    def _check_txt(self):
        """One-line summary of the verification against the real header."""
        rep = self._report
        if rep is None:
            return t('check_not_done', c=P["sub"])
        if rep["missing_req"]:
            return t('check_missing_required', c=P["red"], n=len(rep["missing_req"]))
        parts = []
        n_miss, n_add = len(rep["missing"]), len(rep["added"])
        if n_miss:
            parts.append(t('check_missing_fields', c=P["red"], n=n_miss,
                            s="s" if n_miss > 1 else "",
                            verb=("están" if n_miss > 1 else "está") if LANG == 'es'
                                 else ("are" if n_miss > 1 else "is")))
        if n_add:
            parts.append(t('check_new_fields', c=P["rec"], n=n_add,
                            s="s" if n_add > 1 else ""))
        if not parts:
            return t('check_all_ok', c=P["ok"], n=len(rep["present"]))
        return " · ".join(parts) + "."

    def _check_now(self):
        """Redoes the verification and dumps the detail into the log."""
        self._populate()
        rep = self._report
        if rep is None:
            self.app.log(t('log_no_bold_file'))
            return
        self.app.log(t('log_verify_header', n=rep["total"]))
        self.app.log(t('log_verify_present', n=len(rep["present"]),
                        total=len(rep["present"]) + len(rep["missing"])))
        for m in rep["missing"]:
            marca = "❌" if m in rep["missing_req"] else "⚠️"
            self.app.log(t('log_verify_missing_item', marca=marca, hint=_hint_txt(rep, m)))
        if rep["added"]:
            self.app.log(t('log_verify_new_fields', n=len(rep["added"]),
                            fields=", ".join(rep["added"])))
        if rep["missing_req"]:
            self.app.log(t('log_verify_will_stop'))
        self.refresh()

    @staticmethod
    def _stale_marker():
        """True if the on-disk .filt.tsv was made with a different selection."""
        marker = _proj_dir("data", "processed", "fields_source.txt")
        if not os.path.exists(marker):
            return False
        try:
            return open(marker).read().strip() != fields_fingerprint()
        except OSError:
            return False

    # ---- Actions ----------------------------------------------------------------

    def _restore_defaults(self):
        self._apply_cfg({"fields": list(DEFAULT_FIELDS),
                         "indexed": list(DEFAULT_INDEXED)})

    def confirm_leave(self):
        """Asks before leaving the panel with unsaved changes.

        Returns False only if the user decides to stay. Checking boxes and
        switching screens is the natural flow: without this prompt, the work
        was silently lost and the file kept the previous selection.
        """
        if not self._is_dirty():
            return True
        box = QMessageBox(self)
        box.setWindowTitle(t('unsaved_title'))
        box.setText(t('unsaved_text'))
        box.setInformativeText(t('unsaved_info'))
        save    = box.addButton(t('btn_save'), QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton(t('btn_discard'), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(t('btn_keep_editing'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save:
            self._save()
            return True
        if clicked is discard:
            self._apply_cfg(load_fields_cfg())
            return True
        return False

    def _save(self):
        fields, indexed = self._selection()
        if not fields:
            QMessageBox.warning(self, t('no_fields_title'), t('no_fields_body'))
            return
        try:
            save_fields_cfg(fields, indexed)
        except OSError as e:
            QMessageBox.critical(self, t('save_failed_title'), t('save_failed_body', error=e))
            return
        self.app.log(t('log_selection_saved', n=len(fields), m=len(indexed)))
        if self._stale_marker():
            self.app.log(t('log_selection_stale'))
        self._update_counts()   # turns off the "unsaved changes" notice
        self.refresh()
        self.app.refresh_sidebar_status()


# ---- Main window ------------------------------------------------------------------

class App(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"BOLD Database Creator v{__version__}")
        # Wider and less tall: with the log in a side column the window
        # becomes three columns (sidebar | step | output).
        self.resize(_px(1400), _px(820))
        self.setMinimumSize(_px(1060), _px(600))
        self._seq_running      = False
        self._seq_worker       = None
        self._seq_step_indices = []
        self._seq_step_pos     = 0
        self._timer_start      = 0.0
        self._last_was_live    = False
        self._seq_step_start   = 0.0
        self._task_start_pos   = 0
        self._elapsed_timer    = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_timer)
        self._log_file    = None
        self._log_flush_t = 0.0
        _ensure_data_dirs()
        self._init_msg = _migration_message()
        self._init_log_file()
        # Before _build(): resize events start arriving as soon as the
        # widgets are built, and the timer must already exist by then.
        self._init_settings()
        self._build()
        self._restore_geometry()
        QTimer.singleShot(0, self._show_initial_status)
        # After the window is up: the shortcut question and the update check
        QTimer.singleShot(900, self._after_startup)

    # ---- Window size and position across sessions -----------------------------

    def _init_settings(self):
        """UI state in data/state/ui_state.ini, alongside the rest of what's generated.

        An .ini in the project instead of the Windows registry: the tool lives
        in a folder that gets copied between machines, so the state travels
        with it and can be deleted by hand without touching the registry.
        """
        self._settings = None
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(500)   # saves on release, not on every pixel
        self._geo_timer.timeout.connect(self._save_geometry)
        try:
            path = layout.UI_STATE_FILE
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._settings = QSettings(path, QSettings.Format.IniFormat)
        except Exception:
            self._settings = None

    def _restore_geometry(self):
        if not self._settings:
            return
        try:
            geo = self._settings.value("window/geometry")
            if not geo or not self.restoreGeometry(geo):
                return
        except Exception:
            return
        # If the saved geometry falls outside the current screens (a monitor
        # that's no longer there, a different resolution), the window would
        # open invisible: it's recentered on the primary screen, shrinking it if it wouldn't fit.
        if QGuiApplication.screenAt(self.frameGeometry().center()) is None:
            screen = QGuiApplication.primaryScreen()
            if screen:
                avail = screen.availableGeometry()
                w = min(self.width(),  avail.width())
                h = min(self.height(), avail.height())
                self.resize(w, h)
                self.move(avail.center() - QPoint(w // 2, h // 2))

    def _save_geometry(self):
        if not self._settings:
            return
        try:
            self._settings.setValue("window/geometry", self.saveGeometry())
            self._settings.sync()
        except Exception:
            pass

    def _queue_geometry_save(self):
        if getattr(self, "_geo_timer", None) is not None and self._settings:
            self._geo_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._queue_geometry_save()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._queue_geometry_save()

    # ---- Language ---------------------------------------------------------------

    def _change_language(self, lang):
        """Saves the language preference and offers to restart to apply it.

        There's no live re-translation: hundreds of already-built widgets use
        the text fixed by t() at the moment they were created (see LANG and
        STRINGS_ES/EN at the top of the file). Restarting is far simpler and
        more reliable than rebuilding every widget.
        """
        if lang == LANG:
            return
        if self._settings:
            self._settings.setValue("language", lang)
            self._settings.sync()
        # Restarting would kill the step in progress just like terminate() did
        if self._is_any_running() or self._threads_alive():
            QMessageBox.information(self, t('restart_required_title'), t('restart_blocked_body'))
            return
        box = QMessageBox(self)
        box.setWindowTitle(t('restart_required_title'))
        box.setText(t('restart_required_body'))
        restart_btn = box.addButton(t('btn_restart_now'), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(t('btn_restart_later'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(restart_btn)
        box.exec()
        if box.clickedButton() is restart_btn:
            self._restart_app()

    def _restart_app(self):
        # close() goes through closeEvent (unsaved fields, geometry, log) and
        # returns False if the user decides to stay. A new process is started
        # instead of os.execl: on Windows, execl doesn't quote its arguments,
        # so a Python or project path with spaces broke the restart.
        if not self.close():
            return
        script = os.path.abspath(__file__)
        subprocess.Popen([sys.executable, script] + sys.argv[1:], cwd=SCRIPT_DIR)

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Sidebar --------------------------------------------------------
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        # Wide enough for the step names with their status marker in front
        sidebar.setFixedWidth(_px(262))
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(0)

        logo = QLabel("BOLD DB\nCreator")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            f"color: {P['accent']}; font-size: {_fs(16)}; font-weight: bold; padding: 20px 0 4px 0;"
        )
        sb.addWidget(logo)
        sb.addWidget(self._hsep())

        # Step buttons grouped
        sb.addWidget(self._hsep())
        self._btns = [None] * (len(STEPS) + 2)

        prep_btn = QPushButton(t('nav_home'))
        prep_btn.setObjectName("homeBtn")
        prep_btn.setIcon(QIcon(ICON_HOME))
        prep_btn.setIconSize(QSize(_px(20), _px(20)))
        prep_btn.setProperty("active", "false")
        prep_btn.setToolTip(t('tip_home_nav'))
        prep_btn.clicked.connect(lambda: self._show(PREP_IDX))
        sb.addWidget(prep_btn)
        self._btns[PREP_IDX] = prep_btn

        for grp_name, indices, grp_sub in _SIDEBAR_GROUPS:
            sb.addWidget(self._hsep())
            sb.addWidget(self._hsep())
            grp_lbl = QLabel(grp_name)
            grp_lbl.setStyleSheet(
                f"color: {P['accent']}; font-size: {_fs(10)}; font-weight: bold;"
                " padding: 8px 16px 2px 16px;"
            )
            sb.addWidget(grp_lbl)
            if grp_sub:
                sub_lbl = QLabel(grp_sub)
                sub_lbl.setWordWrap(True)
                sub_lbl.setStyleSheet(
                    f"color: {P['sub']}; font-size: {_fs(9)}; font-style: italic;"
                    " padding: 0 16px 8px 16px;"
                )
                sb.addWidget(sub_lbl)
            for i in indices:
                btn = QPushButton(STEPS[i][0])
                btn.setObjectName("navBtn")
                btn.setProperty("active", "false")
                level = STEP_LEVEL.get(STEPS[i][0], ("", ""))[0]
                btn.setToolTip("{}  ·  {}".format(
                    level, STEP_TECH.get(STEPS[i][0], "")))
                btn.clicked.connect(lambda checked=False, idx=i: self._show(idx))
                sb.addWidget(btn)
                self._btns[i] = btn

        # ---- Configuration -----------------------------------------------------
        # Below the steps, not between them: it's not something you have to do
        # to move forward, but something you check before starting if needed.
        sb.addWidget(self._hsep())
        sb.addWidget(self._hsep())
        cfg_lbl = QLabel(t('config_section_label'))
        cfg_lbl.setStyleSheet(
            f"color: {P['accent']}; font-size: {_fs(10)}; font-weight: bold;"
            " padding: 8px 16px 8px 16px;"
        )
        sb.addWidget(cfg_lbl)
        fields_btn = QPushButton(t('nav_fields_selection'))
        fields_btn.setObjectName("navBtn")
        fields_btn.setProperty("active", "false")
        fields_btn.setToolTip(t('tip_fields_nav'))
        fields_btn.clicked.connect(lambda: self._show(FIELDS_IDX))
        sb.addWidget(fields_btn)
        self._btns[FIELDS_IDX] = fields_btn

        # The SHORTCUTS section is gone: its only content was the step
        # sequences, which are now the two explicit buttons on the Home
        # screen, where the user sees them as soon as the app opens.
        # Footer
        sb.addStretch()
        sb.addWidget(self._hsep())

        lang_row = QHBoxLayout()
        lang_row.setContentsMargins(12, 6, 12, 6)
        lang_row.setSpacing(6)
        lang_lbl = QLabel(t('language_label'))
        _styled(lang_lbl, "caption")
        lang_row.addWidget(lang_lbl)
        lang_row.addStretch()
        for code in ('es', 'en'):
            lbtn = QPushButton(code.upper())
            lbtn.setFixedWidth(_px(34))
            active = code == LANG
            lbtn.setObjectName("langBtn")
            lbtn.setProperty("active", "true" if active else "false")
            lbtn.clicked.connect(lambda checked=False, c=code: self._change_language(c))
            lang_row.addWidget(lbtn)
        sb.addLayout(lang_row)
        sb.addWidget(self._hsep())

        root_txt = PROJECT_ROOT if len(PROJECT_ROOT) < 32 else "..." + PROJECT_ROOT[-28:]
        path_lbl = QLabel(root_txt)
        path_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        path_lbl.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(10)}; padding: 4px 12px 10px 12px;")
        path_lbl.setWordWrap(True)
        path_lbl.setToolTip(t('tip_project_folder', path=PROJECT_ROOT))
        sb.addWidget(path_lbl)

        root.addWidget(sidebar)

        # ---- Right area -----------------------------------------------------
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)
        root.addWidget(right)

        # Step panels in a QStackedWidget
        self._stack = QStackedWidget()
        self._stack.setObjectName("panel")
        self._panels = []
        for label, desc, fn, cfg in STEPS:
            panel = StepPanel(label, desc, fn, cfg, self)
            self._panels.append(panel)
            self._stack.addWidget(panel)
        self._prep = PrepPanel(self)
        self._stack.addWidget(self._prep)          # PREP_IDX index
        self._fields = FieldsPanel(self)
        self._stack.addWidget(self._fields)        # FIELDS_IDX index
        self._stack.setMinimumWidth(_px(430))

        # Log
        log_wrap = QWidget()
        log_lay = QVBoxLayout(log_wrap)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(0)
        hdr = QHBoxLayout()
        hdr.setContentsMargins(8, 4, 8, 2)
        lbl_s = QLabel(t('output_label'))
        lbl_s.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(13)};")
        hdr.addWidget(lbl_s)
        hdr.addStretch()
        self._timer_lbl = QLabel("")
        self._timer_lbl.setStyleSheet(
            f"color: {P['running']}; font-size: {_fs(11)};"
            f" font-family: '{_mono()}';"
        )
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._timer_lbl.setMinimumWidth(_px(90))
        hdr.addWidget(self._timer_lbl)
        hdr.addStretch()
        state_btn = QPushButton(t('btn_view_status'))
        state_btn.setObjectName("clearBtn")
        state_btn.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(13)};")
        state_btn.setToolTip(t('tip_view_status'))
        state_btn.clicked.connect(self._show_initial_status)
        hdr.addWidget(state_btn)
        clear_btn = QPushButton(t('btn_clear'))
        clear_btn.setObjectName("clearBtn")
        clear_btn.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(13)};")
        clear_btn.setToolTip(t('tip_clear_log'))
        clear_btn.clicked.connect(self.clear_log)
        hdr.addWidget(clear_btn)
        log_lay.addLayout(hdr)
        self._log = QTextEdit()
        self._log.setObjectName("logEdit")
        self._log.setReadOnly(True)
        # Multi-hour sessions: without a cap, the document grows without limit
        # and each insertion gets progressively slower. The full log is kept
        # in data/state/logs/ regardless.
        self._log.document().setMaximumBlockCount(4000)
        self._log.setToolTip(t('tip_log_widget'))
        log_lay.addWidget(self._log)

        # The log goes in a right-hand column, not a bottom strip: its lines
        # are long (paths, counters) and at the bottom they'd wrap into two or
        # three rows. With a QSplitter the user decides how much space to give each side.
        log_wrap.setMinimumWidth(_px(420))
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("mainSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(_px(4))
        split.addWidget(self._stack)
        split.addWidget(log_wrap)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 5)
        split.setSizes([_px(540), _px(600)])
        right_lay.addWidget(split)

        self._current = -1
        self._show(PREP_IDX)
        self.refresh_sidebar_status()

    @staticmethod
    def _hsep():
        return _hsep_line()

    def _show(self, idx):
        if (self._current == FIELDS_IDX and idx != FIELDS_IDX
                and not self._fields.confirm_leave()):
            return
        if self._current >= 0 and self._btns[self._current]:
            self._btns[self._current].setProperty("active", "false")
            self._btns[self._current].style().unpolish(self._btns[self._current])
            self._btns[self._current].style().polish(self._btns[self._current])
        self._stack.setCurrentIndex(idx)
        self._btns[idx].setProperty("active", "true")
        self._btns[idx].style().unpolish(self._btns[idx])
        self._btns[idx].style().polish(self._btns[idx])
        self._current = idx
        if idx < len(self._panels):
            self._panels[idx]._refresh_input_status()
        elif idx == FIELDS_IDX:
            self._fields.refresh()
        else:
            self._prep.refresh()

    # ---- Status shown in the sidebar ---------------------------------------

    def refresh_sidebar_status(self):
        """Marks ✅ / ○ next to each step.

        Previously that status only existed as text in the log at startup,
        and disappeared as soon as anything ran.
        """
        try:
            st = _get_project_status()
        except Exception:
            return
        flags = [st["done1"], st["step2"] and not st["outdated"], st["step3"], st["step4"]]
        for i, done in enumerate(flags):
            if self._btns[i]:
                # Two glyphs from the same text font: "✅" rendered as a colour
                # emoji next to a plain "○", at a different size and weight.
                self._btns[i].setText("{}  {}".format(STATUS_DONE if done else STATUS_TODO,
                                                      STEPS[i][0]))
        if getattr(self, "_prep", None):
            self._prep.refresh(st)
        # Only if it's visible: rebuilding its checkboxes while the user is
        # looking at another panel achieves nothing and could clobber a
        # half-finished selection.
        if getattr(self, "_fields", None) and self._current == FIELDS_IDX:
            self._fields.refresh(st)

    # ---- Shortcuts, updates, uninstall ------------------------------------------

    def _after_startup(self):
        self._maybe_ask_shortcuts()
        self._check_updates()

    def _maybe_ask_shortcuts(self):
        """Once, on a downloaded copy with no shortcuts yet."""
        if layout.is_repository_clone() or shortcuts.current() or not self._settings:
            return
        if self._settings.value("shortcuts/asked") == "true":
            return
        self._settings.setValue("shortcuts/asked", "true")
        self._settings.sync()
        box = QMessageBox(self)
        box.setWindowTitle(t('sc_ask_title'))
        box.setText(t('sc_ask_body'))
        yes = box.addButton(t('sc_ask_yes'), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(t('sc_ask_no'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        if box.clickedButton() is yes:
            self.create_shortcuts()

    def create_shortcuts(self):
        desktop, menu = self._prep.sc_desktop.isChecked(), self._prep.sc_menu.isChecked()
        if not (desktop or menu):
            QMessageBox.information(self, t('sc_section'), t('sc_pick_one'))
            return
        try:
            made = shortcuts.create(desktop, menu, LANG)
            self.log(t('sc_created_log', n=len(made)))
            for path in made:
                self.log("   " + path)
        except Exception as e:
            self.log(t('sc_error', error=_cause_str(e)))
        self._prep.refresh_shortcuts()

    def remove_shortcuts(self):
        removed = shortcuts.remove()
        self.log(t('sc_removed_log', n=len(removed)))
        self._prep.refresh_shortcuts()

    def _check_updates(self):
        staged = updater.staged_version()
        if staged and updater.self_update_supported():
            self._show_update_banner({"version": staged}, staged=True)
            return
        self._upd_check = _UpdateCheck(self)
        self._upd_check.done.connect(self._on_update_info)
        self._upd_check.start()

    def _on_update_info(self, release):
        if release and release.get("version") and updater.is_newer(release["version"], __version__):
            self._show_update_banner(release)

    def _show_update_banner(self, release, staged=False):
        prep = self._prep
        self._release = release
        self._update_staged = staged
        prep.upd_lbl.setText(t('upd_staged' if staged else 'upd_available',
                               new=release["version"], cur=__version__))
        prep.upd_see_btn.setVisible(not staged)
        prep.upd_btn.setEnabled(True)
        prep.upd_btn.setToolTip("")
        if staged:
            prep.upd_btn.setText(t('upd_restart'))
        else:
            prep.upd_btn.setText(t('upd_install'))
            if not updater.self_update_supported():
                prep.upd_btn.setEnabled(False)
                prep.upd_btn.setToolTip(t('upd_git_tip'))
            elif not release.get("asset_url"):
                prep.upd_btn.setEnabled(False)
                prep.upd_btn.setToolTip(t('upd_no_asset_tip'))
        prep.update_banner.show()

    def open_release_page(self):
        webbrowser.open((getattr(self, "_release", None) or {}).get("page") or updater.RELEASES_PAGE)

    def update_button_clicked(self):
        if getattr(self, "_update_staged", False):
            self._restart_for_update()
        else:
            self.install_update()

    def install_update(self):
        if self._is_any_running() or self._threads_alive():
            QMessageBox.information(self, t('upd_install'), t('upd_busy'))
            return
        btn = self._prep.upd_btn
        btn.setEnabled(False)
        btn.setText(t('upd_downloading', pct="…"))
        self._upd_dl = _UpdateDownload(self._release, self)
        self._upd_dl.progress.connect(
            lambda done, total: btn.setText(t('upd_downloading', pct=f"{100 * done // total} %" if total
                                              else _fmt_size(done))))
        self._upd_dl.finished_with.connect(self._on_update_downloaded)
        self._upd_dl.start()

    def _on_update_downloaded(self, error):
        btn = self._prep.upd_btn
        if error:
            self.log(t('upd_failed', error=error))
            btn.setEnabled(True)
            btn.setText(t('upd_install'))
            return
        version = self._release["version"]
        self.log(t('upd_downloaded_log', v=version))
        self._show_update_banner({"version": version}, staged=True)
        box = QMessageBox(self)
        box.setWindowTitle(t('upd_ready_title'))
        box.setText(t('upd_ready_body', v=version))
        now = box.addButton(t('upd_restart'), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(t('upd_restart_later'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(now)
        box.exec()
        if box.clickedButton() is now:
            self._restart_for_update()

    def _restart_for_update(self):
        """Closes with exit code 4: the launcher starts again, and start.py
        puts the downloaded version in place first."""
        app = QApplication.instance()
        # Otherwise closing the last window quits with code 0 first
        app.setQuitOnLastWindowClosed(False)
        if self.close():
            app.exit(4)
        else:
            app.setQuitOnLastWindowClosed(True)

    def uninstall(self):
        size = 0
        for dirpath, _dirs, files in os.walk(layout.DATA_DIR):
            for name in files:
                try:
                    size += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(t('uninstall_title'))
        box.setText(t('uninstall_body', data=layout.DATA_DIR, size=_fmt_size(size)))
        go = box.addButton(t('uninstall_go'), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(t('btn_cancel'), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not go:
            return
        removed = shortcuts.remove()
        self.log(t('sc_removed_log', n=len(removed)))
        _os_open(os.path.dirname(layout.ROOT))
        self.close()

    # ---- Home actions -----------------------------------------------------------

    def open_viewer(self):
        app_dir = layout.FRONTEND_DIR
        server  = os.path.join(app_dir, "server.py")
        if not os.path.exists(server):
            QMessageBox.warning(self, t('viewer_not_found_title'), t('viewer_not_found_body'))
            return
        if not os.path.exists(layout.database_path()):
            QMessageBox.information(
                self, t('no_db_yet_title'), t('no_db_yet_body'))
            return
        try:
            exe = sys.executable
            # pythonw.exe wouldn't open the server's console; python.exe is used instead
            if os.path.basename(exe).lower().startswith("pythonw"):
                cand = os.path.join(os.path.dirname(exe), "python.exe")
                if os.path.exists(cand):
                    exe = cand
            # The server opens the browser itself once it's listening, and if
            # a viewer is already running it just opens the browser and exits
            # (opening the browser here after a fixed 2 s raced the startup,
            # and every click started another server on the same port).
            subprocess.Popen([exe, "server.py", "--open-browser"], cwd=app_dir)
            self.log(t('log_viewer_started', port=VIEWER_PORT))
        except Exception as e:
            self.log(_fmt_error(e, t('ctx_open_viewer')))

    def free_disk_space(self):
        """Deletes Step 1's intermediates after an explicit warning.

        They're about 20 GB and losing them isn't reversible without
        re-downloading from BOLD, so the dialog states the size, what gets
        deleted, and what it would cost to recover it. The default option is No.
        """
        if self._is_any_running():
            QMessageBox.information(
                self, t('process_running_title'), t('process_running_body'))
            return

        inter = _intermediate_files()
        if not inter:
            QMessageBox.information(self, t('nothing_to_free_title'), t('nothing_to_free_body'))
            return

        st = _get_project_status(max_age=0)
        if not st["step2"]:
            QMessageBox.warning(
                self, t('db_not_built_title'), t('db_not_built_body'))
            return

        total = sum(s for _, s in inter)
        # Relative to data/: the list mixes data/processed/ and data/raw/
        listado = "\n".join(t('file_list_item', name=os.path.relpath(p, _proj_dir("data")),
                                                size=_fmt_size(s))
                            for p, s in inter)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(t('btn_free_space'))
        box.setText(t('free_space_confirm_text', size=_fmt_size(total)))
        box.setInformativeText(t('free_space_confirm_info', listado=listado))
        box.setStandardButtons(QMessageBox.StandardButton.Yes |
                               QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText(t('btn_yes_delete'))
        box.button(QMessageBox.StandardButton.No).setText(t('btn_cancel'))
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        borrado = 0
        for path, size in inter:
            try:
                os.remove(path)
                borrado += size
                self.log(t('log_deleted_file', name=os.path.basename(path), size=_fmt_size(size)))
            except OSError as e:
                self.log(t('could_not_delete_named', name=os.path.basename(path), error=e))
        self.log(t('log_space_freed', size=_fmt_size(borrado)))
        _invalidate_status()
        self.refresh_sidebar_status()
        self._refresh_all_panels_input()

    def open_manual(self):
        filename = "user_guide_en.html" if LANG == 'en' else "guia_de_uso.html"
        path = _proj_dir("guide", filename)
        if not os.path.exists(path) and LANG == 'en':
            # English manual missing (an old, not-yet-updated version, for
            # example): falls back to the Spanish version instead of leaving the button inert.
            path = _proj_dir("guide", "guia_de_uso.html")
        if os.path.exists(path):
            _os_open(path)
        else:
            QMessageBox.information(self, t('manual_not_found_title'), t('manual_not_found_body'))

    def mark_task_start(self):
        """Marks the point in the log from which it will be dimmed on completion."""
        self._task_start_pos = self._log.document().characterCount() - 1

    def log_next_action(self):
        """After completing a step, says what comes next."""
        try:
            st = _get_project_status()
        except Exception:
            return
        if not st["step1"]:
            nxt = t('next_step1')
        elif not st["step2"]:
            nxt = t('next_step2')
        elif not st["step3"]:
            nxt = t('next_step3')
        elif not st["step4"]:
            nxt = t('next_step4_optional')
        else:
            nxt = t('next_all_done')
        self.log("👉 " + nxt)

    def _set_shortcuts_state(self, enabled):
        """Enables or disables the two build buttons on the Home screen."""
        for btn in getattr(self._prep, "pipeline_btns", []):
            btn.setEnabled(enabled)

    def stop_current(self, panel):
        if self._seq_running and self._seq_worker and self._seq_worker.isRunning():
            self._seq_worker.request_stop()
            i = self._seq_step_indices[self._seq_step_pos]
            self._panels[i].set_stopping_ui()
            self.log(t('log_stopping_pipeline'))
        elif panel is not None and panel._worker and panel._worker.isRunning():
            panel._worker.request_stop()
            panel.set_stopping_ui()
            self.log(t('log_stopping_step'))

    def _run_sequence(self, step_indices, label):
        if self._seq_running:
            QMessageBox.warning(self, t('pipeline_running_title'), t('pipeline_running_body'))
            return
        # Any step, not only those in this sequence: two steps at once on the
        # same database collide (Step 2 drops the table Step 4 is indexing).
        if self._is_any_running():
            QMessageBox.warning(self, t('step_running_title'), t('pipeline_blocked_body'))
            return
        pasos = "\n".join(
            t('sequence_step_item', label=STEPS[i][0])
            for i in step_indices
        )
        resp = QMessageBox.question(
            self, t('confirm_title'),
            t('run_sequence_confirm', pasos=pasos),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        if not self._ensure_no_pending_build(self):
            return
        self._seq_running      = True
        self._seq_step_indices = list(step_indices)
        self._seq_step_pos     = 0
        self._set_shortcuts_state(False)
        self.mark_task_start()
        self.start_timer()
        self.log(t('log_pipeline_start', label=label))
        self._run_next_seq_step()

    def _run_next_seq_step(self):
        if self._seq_step_pos >= len(self._seq_step_indices):
            self._seq_running = False
            self._set_shortcuts_state(True)
            self.stop_timer()
            self._settle_db_build()
            self.log(t('log_process_finished'))
            self._refresh_all_panels_input()
            self.refresh_sidebar_status()
            self.log_next_action()
            return
        i     = self._seq_step_indices[self._seq_step_pos]
        panel = self._panels[i]
        # A step already done whose input was deleted ("Free disk space"
        # removes the .tar.gz and .filt.tsv) is skipped: running it failed
        # ("no .tar.gz found") and stopped the whole sequence, so Option 2
        # couldn't add full-text search to a database built with Option 1.
        _invalidate_status()
        if panel._is_done() and not _check_input(STEPS[i][0]):
            self.log(t('log_step_skipped_done', label=STEPS[i][0]))
            self._seq_step_pos += 1
            self._run_next_seq_step()
            return
        self._show(i)
        self._task_start_pos = self._log.document().characterCount() - 1
        self.log("\n[{}/{}] {}".format(
            self._seq_step_pos + 1, len(self._seq_step_indices), STEPS[i][0]
        ))
        panel.set_running_ui()
        self._seq_worker = StepWorker(panel.fn, panel.cfg)
        self._seq_worker.log_signal.connect(self.log)
        self._seq_worker.progress_signal.connect(
            lambda v, p=panel: p._on_progress(v)
        )
        self._seq_worker.finished_signal.connect(self._on_seq_step_done)
        self._seq_step_start = time.monotonic()
        self._seq_worker.start()

    def _on_seq_step_done(self, ok):
        step_elapsed = time.monotonic() - self._seq_step_start
        m, s = divmod(int(step_elapsed), 60)
        time_str = f"{m}m {s:02d}s" if m else f"{s}s"
        i     = self._seq_step_indices[self._seq_step_pos]
        panel = self._panels[i]
        panel.set_done_ui(ok)
        # Stop pressed just as the step was finishing successfully: the
        # sequence must not go on to the next step.
        stop_requested = self._seq_worker is not None and self._seq_worker._stop_evt.is_set()
        if ok and stop_requested:
            self._seq_running = False
            self._set_shortcuts_state(True)
            self.stop_timer()
            self._settle_db_build()
            self._refresh_all_panels_input()
            self.refresh_sidebar_status()
            return
        if not ok:
            self.log(t('log_step_failed', time=time_str))
            self._seq_running = False
            self._set_shortcuts_state(True)
            self.stop_timer()
            self._settle_db_build()
            self._refresh_all_panels_input()
            self.refresh_sidebar_status()
            return
        self.log(t('log_step_completed_in', time=time_str))
        self._dim_completed_task()
        self._seq_step_pos += 1
        self._run_next_seq_step()

    def start_timer(self):
        self._timer_start = time.monotonic()
        self._tick_timer()
        self._elapsed_timer.start()

    def stop_timer(self):
        self._elapsed_timer.stop()

    def _tick_timer(self):
        elapsed = int(time.monotonic() - self._timer_start)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        txt = f"⏱ {h:02d}:{m:02d}:{s:02d}" if h else f"⏱ {m:02d}:{s:02d}"
        self._timer_lbl.setText(txt)

    def log(self, message):
        live = message.startswith("\r")
        msg  = message[1:].rstrip("\n") if live else message.rstrip("\n")
        if not live:
            # Leading newlines (step headers use them to leave a gap) and empty
            # messages are spacers: written without a timestamp. Before, the
            # timestamp went in front of the newline, so it sat alone on the
            # blank line and the header below was left without one.
            lead = len(msg) - len(msg.lstrip("\n"))
            msg  = msg.lstrip("\n")
            if not msg.strip():
                lead = max(lead, 1)
            for _ in range(lead):
                self._log.append("")
                if self._log_file:
                    try: self._log_file.write("\n")
                    except Exception: pass
            self._last_was_live = False
            if not msg.strip():
                return
        # Request text (monochrome) presentation for emoji
        msg  = re.sub(r'([\U0001F000-\U0001FAFF☀-➿])️?', r'\1︎', msg)
        stripped = msg.lstrip()
        bold = False
        # By prefix only: matching "error" anywhere painted red warnings that
        # merely mention the word ("check the error message above").
        if stripped.startswith("❌"):
            color = P["red"]
            bold  = True
        elif stripped.startswith("⚠"):
            color = P["running"]
            bold  = True
        elif stripped.startswith(("✅", "🎉", "👉")):
            color = P["ok"]
            bold  = stripped.startswith(("🎉", "👉"))
        elif (stripped.startswith(("⏳", "🔍", "🔄", "▶")) or "===" in msg
              or re.match(r"^\s*\[\d+/\d+\]", msg)):
            color = P["progbar"]
            if "===" in msg or re.match(r"^\s*\[\d+/\d+\]", msg):
                bold = True
        elif stripped.startswith(("📋", "📁", "💽", "💾", "📝", "📊", "📂", "🚀", "📄", "📜", "🔎", "📘", "📌")):
            color = P["blue_mid"]
        elif msg.startswith("  "):
            color = P["sub"]
        else:
            color = P["text"]
        ts     = datetime.now().strftime("%H:%M:%S")
        safe   = (msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                     .replace("\n", "<br>"))
        weight = "bold" if bold else "normal"
        body   = (
            f'<span style="color:{color}; font-family:\'{_mono()}\',monospace;'
            f' font-weight:{weight};">{safe}</span>'
        )
        # Live (overwritten) progress lines carry a timestamp too: the time of
        # their latest update.
        html = (
            f'<span style="color:{P["muted"]}; font-family:\'{_mono()}\',monospace;">'
            f'[{ts}]</span> {body}'
        )
        if live and self._last_was_live:
            cur = self._log.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End)
            cur.movePosition(QTextCursor.MoveOperation.StartOfBlock,
                             QTextCursor.MoveMode.KeepAnchor)
            cur.insertHtml(html)
            self._log.setTextCursor(cur)
        else:
            self._log.append(html)
        self._last_was_live = live
        if self._log_file:
            try:
                self._log_file.write(f"[{ts}] {msg}\n")
                # flush() per message forced a disk write from the UI thread.
                # During Step 2 that disk is saturated by the import and each
                # flush could take seconds: capped to one per second (and
                # always on completion, see _flush_log_file).
                now = time.monotonic()
                if now - self._log_flush_t >= 1.0:
                    self._log_flush_t = now
                    self._log_file.flush()
            except Exception:
                pass

    def _flush_log_file(self):
        if self._log_file:
            try:
                self._log_file.flush()
                self._log_flush_t = time.monotonic()
            except Exception:
                pass

    def _dim_completed_task(self):
        doc = self._log.document()
        end = doc.characterCount() - 1
        if end <= self._task_start_pos:
            self._task_start_pos = end
            return
        # The last line is left untouched (the step's summary: records
        # inserted, indexes created...). Dimming it too made unreadable the
        # one sentence the user actually wants to read when the step finishes.
        cur_last = QTextCursor(doc)
        cur_last.setPosition(end)
        cur_last.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        dim_end = max(self._task_start_pos, cur_last.position() - 1)
        if dim_end > self._task_start_pos:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(P["dimmed"]))
            cur = QTextCursor(doc)
            cur.setPosition(self._task_start_pos)
            cur.setPosition(dim_end, QTextCursor.MoveMode.KeepAnchor)
            cur.mergeCharFormat(fmt)
        self._task_start_pos = end

    def clear_log(self):
        self._log.clear()
        self._last_was_live = False
        self._task_start_pos = 0

    def _refresh_all_panels_input(self):
        for panel in self._panels:
            if not panel.is_running():
                panel._refresh_input_status()
        if getattr(self, "_prep", None):
            # Full refresh, not just the drop card: the Home buttons depend on
            # whether a package has just been dropped or copied in.
            _invalidate_status()
            self._prep.refresh()

    def _is_any_running(self):
        if self._seq_running:
            return True
        return any(p.is_running() for p in self._panels)

    def _settle_db_build(self):
        """Puts a finished build in place of the current database, if there is
        one (see _promote_pending_build). Called only while nothing runs.
        False if a finished build is waiting and couldn't replace it."""
        pending = os.path.exists(_build_path(layout.database_path()))
        ok = _promote_pending_build(self.log)
        if pending:
            self._refresh_all_panels_input()
            self.refresh_sidebar_status()
        return ok

    def _ensure_no_pending_build(self, parent):
        """Before a run: a finished build that couldn't replace the database
        yet must do so first. Updating the current database in the meantime
        would be lost when it does, and Step 2 would delete it."""
        if self._settle_db_build():
            return True
        QMessageBox.warning(parent, t('db_build_pending_title'), t('db_build_pending_body'))
        return False

    def _init_log_file(self):
        logs_dir = layout.LOGS_DIR
        try:
            os.makedirs(logs_dir, exist_ok=True)
            ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            path = os.path.join(logs_dir, f"run_{ts}.log")
            self._log_file = open(path, "w", encoding="utf-8")
            self._log_file.write(
                t('log_session_header', ts=datetime.now().strftime('%Y-%m-%d %H:%M:%S')) + "\n\n"
            )
            # Keep only the 100 most recent log files
            existing = sorted(
                (f for f in os.listdir(logs_dir) if f.startswith("run_") and f.endswith(".log")),
                reverse=True
            )
            for old in existing[100:]:
                try:
                    os.remove(os.path.join(logs_dir, old))
                except Exception:
                    pass
        except Exception:
            self._log_file = None

    def _show_initial_status(self):
        if self._init_msg:
            self.log(self._init_msg)
        # A rebuild that finished but couldn't replace the database last
        # time, or one cut short by a crash: dealt with before reading the status
        self._settle_db_build()
        st  = _get_project_status()
        r   = PROJECT_ROOT
        self.log("─" * 46)

        def ok(cond): return "✅" if cond else "⚠️"

        gz_files  = glob.glob(os.path.join(r, "..", "data", "raw", "*.tar.gz"))
        filt_tsvs = glob.glob(os.path.join(r, "..", "data", "processed", "*.filt.tsv"))
        self.log(t('header_step1'))
        self.log(t('input_line', ok=ok(gz_files), msg=(
            t('n_targz_in_raw', n=len(gz_files)) if gz_files else t('no_targz_in_raw')
        )))
        self.log(t('output_line', ok=ok(st["step1"]), msg=(
            t('n_filt_tsv', n=len(filt_tsvs)) if filt_tsvs
            else t('no_filt_tsv_run_step')
        )))

        cnt = t('records_count_suffix', n=st['record_count']) if st["record_count"] else ""
        self.log(t('header_step2'))
        self.log(t('input_line', ok=ok(st["step1"]), msg=(
            t('filt_tsv_available') if st["step1"] else t('no_filt_tsv_run_step1')
        )))
        self.log(t('output_line', ok=ok(st["step2"]), msg=(
            t('dbfile_with_count', cnt=cnt) if st["step2"] else t('db_not_created_run_step')
        )))
        if st["outdated"]:
            self.log(t('db_outdated_rebuild'))

        self.log(t('header_step3'))
        self.log(t('input_line', ok=ok(st["step2"]), msg=(
            t('dbfile_present') if st["step2"] else t('db_not_found_run_step2')
        )))
        self.log(t('output_line', ok=ok(st["step3"]), msg=(
            t('all_indexes_created') if st["step3"] else t('indexes_pending')
        )))

        self.log(t('header_step4'))
        self.log(t('input_line', ok=ok(st["step2"]), msg=(
            t('dbfile_present') if st["step2"] else t('db_not_found_run_step2')
        )))
        self.log(t('output_line', ok=ok(st["step4"]), msg=(
            t('fts_table_present') if st["step4"] else t('fts_not_created')
        )))

        self.log("")
        all_ok = st["done1"] and st["step2"] and st["step3"] and st["step4"]
        if all_ok:
            self.log(t('all_complete', n=st["record_count"]))
            self.log(t('tap_open_viewer_home'))
        elif st["step2"]:
            cnt_str = t('records_count_paren', n=st["record_count"]) if st["record_count"] else ""
            self.log(t('db_exists_can_query', cnt=cnt_str))
            pending = []
            if not st["step3"]:
                pending.append(t('pending_step3'))
            if not st["step4"]:
                pending.append(t('pending_step4'))
            if pending:
                self.log(t('pending_steps_header'))
                for p in pending:
                    self.log("   • " + p)
        else:
            self.log(t('no_db_yet_log'))
            self.log(t('go_home_create_db'))
        self.log("─" * 46)
        if getattr(self, "_prep", None):
            self._prep.refresh(st)

    def _drop_cards(self):
        cards = [self._prep._drop_card] if getattr(self, "_prep", None) else []
        cards += [p._drop_card for p in self._panels if p._drop_card is not None]
        return cards

    def _threads_alive(self):
        """Is any worker thread still alive? Stricter than _is_any_running():
        the window must not be destroyed while a QThread is still unwinding."""
        workers = [self._seq_worker] + [p._worker for p in self._panels]
        if any(w is not None and w.isRunning() for w in workers):
            return True
        return any(c.is_copying() for c in self._drop_cards())

    def _close_when_idle(self):
        if self._is_any_running() or self._threads_alive():
            QTimer.singleShot(200, self._close_when_idle)
            return
        self.close()

    def closeEvent(self, event):
        # Unsaved fields are asked about first: asking after stopping the
        # process could cancel the close with the work already lost.
        if getattr(self, "_fields", None) and not self._fields.confirm_leave():
            event.ignore()
            return
        if self._is_any_running() or self._threads_alive():
            if getattr(self, "_closing", False):
                QMessageBox.information(self, t('closing_wait_title'), t('closing_wait_body'))
                event.ignore()
                return
            resp = QMessageBox.question(
                self, t('process_running_close_title'),
                t('process_running_close_body'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # Stop cooperatively and close once everything has wound down.
            # QThread.terminate() killed the thread wherever it was: with
            # Step 2 writing under journal_mode=OFF, that corrupted the DB,
            # and none of the steps' cleanup (finally blocks) ever ran.
            self._closing = True
            if self._seq_running:
                self.stop_current(None)
            else:
                for p in self._panels:
                    if p.is_running():
                        self.stop_current(p)
            for card in self._drop_cards():
                card.cancel_copy()
            self.log(t('log_closing_after_stop'))
            self._flush_log_file()
            event.ignore()
            QTimer.singleShot(200, self._close_when_idle)
            return
        # The timer may have a save pending if the app is closed right after
        # moving or resizing the window.
        self._save_geometry()
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
        event.accept()


# ---- Entry point ------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)
    _FONT_SCALE[0] = _compute_dpi_scale(app)
    _resolve_mono_font()
    app.setStyleSheet(_make_app_qss())
    window = App()
    window.show()
    sys.exit(app.exec())
