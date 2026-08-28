#!/usr/bin/env python3
"""
BOLD Database Creator - unified GUI
Steps: 1-Filter TSV  2-TSV->SQLite  3-Index  4-FTS
"""

__version__ = "1.1.2"

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
    import importlib.metadata as _meta
    pip_pkgs = ["PySide6"]
    missing = []
    for pkg in pip_pkgs:
        try:
            _meta.version(pkg)
        except _meta.PackageNotFoundError:
            missing.append(pkg)
    if not missing:
        return

    _TITLE = "BOLD DB Creator — Instalando"
    _MSG   = (
        "Instalando dependencias, por favor espere…\n\n"
        "Paquetes: " + ", ".join(missing) + "\n\n"
        "La aplicación iniciará automáticamente al terminar.\n"
        "No cierre esta ventana."
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

# ---- Configuration constants ----------------------------------------------------
DB_FILE    = "../app/bold_db.db"
TABLE_NAME = "bold_records"
CHUNKSIZE  = 50_000

# Field whose value decides whether a row makes it into the database: without a
# nucleotide sequence the record is useless for anything the viewer does.
FILTER_FIELD = "nuc"


COL_DEFS = {
    "processid":                    "TEXT PRIMARY KEY COLLATE NOCASE",
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

FIELDS_CONFIG_FILE = os.path.join(SCRIPT_DIR, "fields_config.json")

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
# processid is deliberately omitted: it's PRIMARY KEY, so SQLite already
# maintains its own index (sqlite_autoindex_bold_records_1) with the same
# collation.
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
# The language is resolved ONCE at startup (read from app/ui_state.ini, the
# same QSettings that stores the window geometry) and doesn't change live:
# the language selector in the sidebar only saves the preference and asks for
# a restart. This avoids having to re-translate hundreds of already-built
# widgets on the fly, in exchange for a restart to see the change.

def _load_language():
    """Reads 'language' from app/ui_state.ini by hand, as plain text instead of via QSettings.

    Avoids building a separate QSettings instance here: the App creates its own
    (self._settings) for the window geometry, and two QSettings instances over the
    same file compete for their own in-memory cache — one of them can end up
    rewriting the entire file without the key the other just read, clobbering
    'language' or 'window/geometry'. With a plain-text read here and QSettings
    reserved solely for self._settings, each half of the state has a single
    owner writing it.
    """
    try:
        path = os.path.normpath(os.path.join(PROJECT_ROOT, "..", "app", "ui_state.ini"))
        with open(path, "r", encoding="utf-8") as f:
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
    'err_detail_saved': 'Detalle técnico guardado en app/logs/error.log',
    'unfiltered_tsv': 'TSV sin filtrar',
    'deleted_processed': '🗑 {label} eliminado (ya procesado): {name}',
    'could_not_delete_named': '⚠️ No se pudo eliminar {name}: {error}',
    'db_busy_retrying': '⏳ Base de datos ocupada (probablemente el antivirus escaneándola) — reintentando en {delay}s ({attempt}/{attempts})...',
    'deleting_obsolete_targz': '🗑 Eliminando .tar.gz obsoleto: {name}',
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
    'file_label': '📁 Archivo: {name}',
    'ctx_generate_tsv': 'Generar TSV',
    'no_filt_tsv_found': '❌ No se encontró archivo *.filt.tsv en ../data/processed/',
    'db_already_built': '✅ BD ya construida desde {name} — omitiendo importación',
    'total_records_expected': '📋 Total registros esperados: {n:,}',
    'records_number_not_found': '⚠️ records.number no encontrado — ejecuta el Paso 1 primero',
    'columns_detected': '📋 Columnas detectadas: {n}',
    'table_created': '✅ Tabla creada con tipos y restricciones',
    'importing_data': '⏳ Importando datos (puede tardar)...',
    'import_progress': '\r  {processed:,} / {total:,}  ({rate:,.0f} r/s)',
    'db_created': '✅ BD creada: {name}',
    'records_inserted': '✅ Registros insertados: {n:,}',
    'import_interrupted': '⚠️ Importación interrumpida — BD parcial eliminada',
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
    'rebuilding_fts_index': '⏳ Reconstruyendo índice FTS5 ({n:,} registros) — operación única, no interrumpible...',
    'fts_may_take_minutes': '   Puede tardar varios minutos. La barra no avanza porque SQLite no informa del progreso: es normal, no está bloqueado.',
    'still_working': '\r   ⏳ Sigue trabajando — {mins:.0f} min transcurridos (no cierre la ventana)',
    'rebuild_finished': '   Reconstrucción terminada en {mins:.0f} min',
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
                  'Entrada: data/processed/*.filt.tsv   Salida: app/bold_db.db',
    'step3_desc': 'Crea índices sobre los campos de búsqueda. Sin ellos cada consulta recorre la tabla '
                  'entera y el visor va muy lento.\n'
                  'Entrada: app/bold_db.db   Salida: índices en bold_records',
    'step4_desc': 'Añade búsqueda de texto completo (escribir cualquier palabra sin elegir campo, '
                  'incluso coincidencias parciales dentro de otra palabra). '
                  'Tarda varios minutos y añade en torno a un 25% al tamaño de la base de datos: '
                  'actívalo solo si vas a usar esa función.\n'
                  'Entrada: app/bold_db.db   Salida: tabla bold_records_fts',
    'step1_tech': 'Descomprimir .tar.gz → filtrar TSV',
    'step2_tech': 'TSV → SQLite (bold_records)',
    'step3_tech': 'CREATE INDEX sobre 20 columnas',
    'step4_tech': 'Tabla virtual FTS5 (bold_records_fts)',
    'badge_required': 'OBLIGATORIO',
    'badge_recommended': 'RECOMENDADO',
    'badge_optional': 'OPCIONAL',
    'step1_cost': '≈ 30-60 min · necesita ~60 GB libres',
    'step2_cost': '≈ 1-2 h · la BD ocupa ~25 GB',
    'step3_cost': '≈ 20-40 min · añade ~8 GB',
    'step4_cost': '≈ 5-15 min · añade ~25% al tamaño de la BD',
    'sidebar_group_individual_steps': 'PASOS INDIVIDUALES',
    'sidebar_group_hint': 'Para ejecutar uno a uno. No hace falta si ya usaste el botón de Inicio.',
    'pipeline1_title': 'Opción 1',
    'pipeline1_label': 'Crear la base de datos',
    'pipeline1_label_done': 'Rehacer la base de datos',
    'pipeline1_hint': 'Pasos 1 a 3. Es lo que necesita la mayoría: podrás buscar por '
                      'cualquier campo (especie, país, BIN…) y el visor irá rápido.',
    'pipeline1_tip': 'Ejecuta seguidos los pasos 1, 2 y 3.\nDuración aproximada: 2-4 horas.',
    'pipeline2_title': 'Opción 2',
    'pipeline2_label': 'Crear la base de datos completa',
    'pipeline2_label_done': 'Rehacer la base de datos completa',
    'pipeline2_hint': 'Pasos 1 a 4. Añade la búsqueda por texto libre. Suma unos '
                      'minutos más y la base de datos crece en torno a un 25%.',
    'pipeline2_tip': 'Ejecuta seguidos los pasos 1, 2, 3 y 4.\n'
                     'Duración aproximada: 2-4 horas. Elige esta opción solo si vas a '
                     'buscar escribiendo palabras sueltas sin elegir campo.',
    'badge_tip_required': 'Sin este paso la base de datos no se puede crear.',
    'badge_tip_recommended': 'Se puede omitir, pero el visor irá notablemente más lento.',
    'badge_tip_optional': 'Solo si vas a usar la búsqueda por texto libre.',
    'cost_tooltip': 'Estimación sobre la descarga completa de BOLD. Varía mucho según el equipo.',
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
    'run_only_step_tip': 'Ejecuta solo este paso.  {cost}',
    'stopping_status': 'Deteniendo...',
    'running_status': 'Ejecutando...',
    'no_progress_note': 'Sin progreso medible — es normal en esta operación. Puede tardar varios minutos.',
    'already_running_tip': 'Este paso ya se está ejecutando.',
    'completed_status': '✓  Completado',
    'stopped_status': 'Detenido',
    'error_status': 'Error — mira el detalle en el registro de abajo',
    'home_title': 'Inicio',
    'home_intro': 'Esta herramienta convierte la descarga pública de BOLD en una base de datos '
                  'local que puedes consultar con el visor. Se hace una sola vez por cada versión '
                  'de los datos.',
    'home_req_note': 'Necesitas el <b>.tar.gz</b> del portal de BOLD (sin descomprimir), '
                     'unos <b>150 GB libres</b> en este disco y entre <b>0,5 y 2 horas</b> '
                     'para los pasos 1-3. Puedes dejarlo trabajando y volver luego.',
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
    'main_hint_ready': 'Ya puedes consultar los datos: pulsa «Abrir el visor». Solo hace falta '
                       'rehacer la base de datos si descargas una versión nueva de BOLD.',
    'main_hint_not_ready': 'Los pasos ya completados se omiten automáticamente, así que es seguro '
                          'pulsar cualquiera de los dos aunque hayas empezado antes.',
    'src_tsv': 'TSV descargado',
    'src_catalog': 'catálogo de BOLD',
    'fields_panel_title': 'Selección de Campos del archivo TSV',
    'fields_panel_intro': 'El archivo .tsv de BOLD contiene 76 columnas, y la base de datos se genera con las '
                          'seleccionadas aquí. Menos campos = base de datos más pequeña y pasos más '
                          'rápidos; más campos = más información en el visor y en las exportaciones.',
    'fields_panel_warning': '⚠️  La selección se aplica al <b>preparar los datos (Paso 1)</b>. Si la '
                            'cambias con la base de datos ya hecha, hay que repetir los pasos desde el '
                            '1, y eso necesita el <b>.tar.gz</b> de BOLD otra vez (se borra al procesarlo).',
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
    'output_label': 'Salida',
    'btn_view_status': 'Ver estado',
    'tip_view_status': 'Vuelve a mostrar en qué punto está el proyecto.',
    'btn_clear': 'Limpiar',
    'tip_clear_log': 'Vacía este registro. No afecta a los archivos ni a los pasos.',
    'tip_log_widget': 'Registro de lo que va ocurriendo. Se guarda también en app/logs/',
    'viewer_not_found_title': 'Visor no encontrado',
    'viewer_not_found_body': 'No se encontró app/server.py.',
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
    'free_space_confirm_info': 'Se eliminarán de data/processed/:\n\n{listado}\n\n'
                               '⚠️  ESTA ACCIÓN NO SE PUEDE DESHACER.\n\n'
                               'Qué NO se ve afectado:\n'
                               '    • La base de datos (app/bold_db.db) queda intacta.\n'
                               '    • El visor sigue funcionando exactamente igual.\n'
                               '    • Los pasos 3 y 4 se pueden ejecutar sin problema.\n\n'
                               'Qué pierdes:\n'
                               '    • Para volver a construir la base de datos tendrías que descargar '
                               'otra vez el archivo de BOLD (~10 GB) y repetir el Paso 1 '
                               '(30-60 minutos).\n\n'
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
    'step_running_body': 'Uno de los pasos ya esta corriendo.',
    'sequence_step_item': '  • {label}   ({cost})',
    'confirm_title': 'Confirmar',
    'run_sequence_confirm': 'Se ejecutarán estos pasos, uno detrás de otro:\n\n{pasos}\n\n'
                            'En total puede tardar varias horas. Puedes dejar la ventana abierta y '
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
    'pending_step4': 'Paso 4 — Búsqueda por texto libre  (opcional: tarda varios minutos)',
    'pending_steps_header': '⚠️ Pasos pendientes:',
    'no_db_yet_log': '⚠️ Aún no hay base de datos.',
    'go_home_create_db': '👉 Ve a «Inicio» y pulsa «Crear la base de datos».',
    'process_running_close_title': 'Hay un proceso en curso',
    'process_running_close_body': 'Todavía hay un paso ejecutándose.\n\n'
                                  'Si cierras ahora se interrumpirá de golpe y el trabajo de ese paso se '
                                  'perderá; puede quedar una base de datos a medias que habrá que rehacer.\n\n'
                                  'Es preferible pulsar «Detener» y esperar a que termine de cerrar.\n\n'
                                  '¿Cerrar de todos modos?',
    'app_init_skipped': '⚠️ dev/frontend no encontrada — inicialización de app/ omitida',
    'app_init_done': '✅ app/ inicializada desde dev/frontend',
    'app_init_error': '❌ Error al inicializar app/: {error}',
    'log_session_header': '# BOLD DB Creator — sesión {ts}',
    'language_label': 'Idioma',
    'restart_required_title': 'Reiniciar para aplicar',
    'restart_required_body': 'El cambio de idioma se aplica al reiniciar la aplicación.\n\n'
                             '¿Reiniciar ahora?',
    'btn_restart_now': 'Reiniciar ahora',
    'btn_restart_later': 'Más tarde',
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
    'err_detail_saved': 'Technical detail saved in app/logs/error.log',
    'unfiltered_tsv': 'Unfiltered TSV',
    'deleted_processed': '🗑 {label} deleted (already processed): {name}',
    'could_not_delete_named': '⚠️ Could not delete {name}: {error}',
    'db_busy_retrying': '⏳ Database busy (likely the antivirus scanning it) — retrying in {delay}s ({attempt}/{attempts})...',
    'deleting_obsolete_targz': '🗑 Deleting obsolete .tar.gz: {name}',
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
    'file_label': '📁 File: {name}',
    'ctx_generate_tsv': 'Generate TSV',
    'no_filt_tsv_found': '❌ No *.filt.tsv file found in ../data/processed/',
    'db_already_built': '✅ DB already built from {name} — skipping import',
    'total_records_expected': '📋 Total records expected: {n:,}',
    'records_number_not_found': '⚠️ records.number not found — run Step 1 first',
    'columns_detected': '📋 Columns detected: {n}',
    'table_created': '✅ Table created with types and constraints',
    'importing_data': '⏳ Importing data (this may take a while)...',
    'import_progress': '\r  {processed:,} / {total:,}  ({rate:,.0f} r/s)',
    'db_created': '✅ DB created: {name}',
    'records_inserted': '✅ Records inserted: {n:,}',
    'import_interrupted': '⚠️ Import interrupted — partial DB deleted',
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
    'rebuilding_fts_index': '⏳ Rebuilding FTS5 index ({n:,} records) — one-off operation, cannot be interrupted...',
    'fts_may_take_minutes': "   This may take several minutes. The bar doesn't move because SQLite doesn't report progress: that's normal, it isn't stuck.",
    'still_working': '\r   ⏳ Still working — {mins:.0f} min elapsed (do not close the window)',
    'rebuild_finished': '   Rebuild finished in {mins:.0f} min',
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
                  'Input: data/processed/*.filt.tsv   Output: app/bold_db.db',
    'step3_desc': 'Creates indexes on the search fields. Without them every query scans the '
                  'whole table and the viewer is very slow.\n'
                  'Input: app/bold_db.db   Output: indexes on bold_records',
    'step4_desc': 'Adds full-text search (type any word without choosing a field, '
                  'including partial matches inside another word). '
                  'It takes several minutes and adds about 25% to the database size: '
                  'enable it only if you are going to use that feature.\n'
                  'Input: app/bold_db.db   Output: bold_records_fts table',
    'step1_tech': 'Extract .tar.gz → filter TSV',
    'step2_tech': 'TSV → SQLite (bold_records)',
    'step3_tech': 'CREATE INDEX on 20 columns',
    'step4_tech': 'FTS5 virtual table (bold_records_fts)',
    'badge_required': 'REQUIRED',
    'badge_recommended': 'RECOMMENDED',
    'badge_optional': 'OPTIONAL',
    'step1_cost': '≈ 30-60 min · needs ~60 GB free',
    'step2_cost': '≈ 1-2 h · the DB takes up ~25 GB',
    'step3_cost': '≈ 20-40 min · adds ~8 GB',
    'step4_cost': '≈ 5-15 min · adds ~25% to the DB size',
    'sidebar_group_individual_steps': 'INDIVIDUAL STEPS',
    'sidebar_group_hint': 'To run one at a time. Not needed if you already used the Start button.',
    'pipeline1_title': 'Option 1',
    'pipeline1_label': 'Create the database',
    'pipeline1_label_done': 'Redo the database',
    'pipeline1_hint': 'Steps 1 to 3. This is what most people need: you will be able to search by '
                      'any field (species, country, BIN…) and the viewer will be fast.',
    'pipeline1_tip': 'Runs steps 1, 2 and 3 in sequence.\nApproximate duration: 2-4 hours.',
    'pipeline2_title': 'Option 2',
    'pipeline2_label': 'Create the full database',
    'pipeline2_label_done': 'Redo the full database',
    'pipeline2_hint': 'Steps 1 to 4. Adds full-text search. Takes a few extra '
                      'minutes and the database grows by about 25%.',
    'pipeline2_tip': 'Runs steps 1, 2, 3 and 4 in sequence.\n'
                     'Approximate duration: 2-4 hours. Choose this option only if you are going to '
                     'search by typing loose words without choosing a field.',
    'badge_tip_required': "Without this step the database can't be created.",
    'badge_tip_recommended': 'It can be skipped, but the viewer will be noticeably slower.',
    'badge_tip_optional': 'Only if you are going to use full-text search.',
    'cost_tooltip': 'Estimate based on the full BOLD download. Varies a lot depending on the machine.',
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
    'run_only_step_tip': 'Runs only this step.  {cost}',
    'stopping_status': 'Stopping...',
    'running_status': 'Running...',
    'no_progress_note': "No measurable progress — that's normal for this operation. It may take several minutes.",
    'already_running_tip': 'This step is already running.',
    'completed_status': '✓  Completed',
    'stopped_status': 'Stopped',
    'error_status': 'Error — see the detail in the log below',
    'home_title': 'Start',
    'home_intro': "This tool converts BOLD's public download into a local database "
                  "you can query with the viewer. It's done once per data version.",
    'home_req_note': 'You need the <b>.tar.gz</b> from the BOLD portal (do not extract it), '
                     'about <b>150 GB free</b> on this disk and between <b>0.5 and 2 hours</b> '
                     'for steps 1-3. You can leave it working and come back later.',
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
    'main_hint_ready': 'You can already browse the data: click «Open the viewer». You only need to '
                       'redo the database if you download a new version of BOLD.',
    'main_hint_not_ready': "Steps already completed are skipped automatically, so it's safe "
                          'to click either one even if you started before.',
    'src_tsv': 'downloaded TSV',
    'src_catalog': 'BOLD catalog',
    'fields_panel_title': 'TSV File Field Selection',
    'fields_panel_intro': "BOLD's .tsv file has 76 columns, and the database is generated with the "
                          "ones selected here. Fewer fields = smaller database and faster steps; "
                          "more fields = more information in the viewer and exports.",
    'fields_panel_warning': '⚠️  The selection applies when <b>preparing the data (Step 1)</b>. If you '
                            'change it after the database is already built, the steps need to be repeated from '
                            'Step 1, and that needs the BOLD <b>.tar.gz</b> again (it is deleted once processed).',
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
    'output_label': 'Output',
    'btn_view_status': 'View status',
    'tip_view_status': 'Shows again where the project stands.',
    'btn_clear': 'Clear',
    'tip_clear_log': "Clears this log. Doesn't affect the files or the steps.",
    'tip_log_widget': 'Log of what is happening. Also saved to app/logs/',
    'viewer_not_found_title': 'Viewer not found',
    'viewer_not_found_body': 'app/server.py was not found.',
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
    'free_space_confirm_info': 'These will be removed from data/processed/:\n\n{listado}\n\n'
                               '⚠️  THIS ACTION CANNOT BE UNDONE.\n\n'
                               'What is NOT affected:\n'
                               '    • The database (app/bold_db.db) stays intact.\n'
                               '    • The viewer keeps working exactly the same.\n'
                               '    • Steps 3 and 4 can still be run without issue.\n\n'
                               'What you lose:\n'
                               '    • To rebuild the database you would need to download '
                               'the BOLD file again (~10 GB) and repeat Step 1 '
                               '(30-60 minutes).\n\n'
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
    'step_running_body': 'One of the steps is already running.',
    'sequence_step_item': '  • {label}   ({cost})',
    'confirm_title': 'Confirm',
    'run_sequence_confirm': 'These steps will run one after another:\n\n{pasos}\n\n'
                            'In total this may take several hours. You can leave the window open and '
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
    'pending_step4': 'Step 4 — Full-text search  (optional: takes several minutes)',
    'pending_steps_header': '⚠️ Pending steps:',
    'no_db_yet_log': '⚠️ There is no database yet.',
    'go_home_create_db': '👉 Go to «Start» and click «Create the database».',
    'process_running_close_title': 'A process is running',
    'process_running_close_body': "A step is still running.\n\n"
                                  "If you close now it will be interrupted abruptly and that step's work will "
                                  'be lost; the database may be left half-built and need to be redone.\n\n'
                                  'It is better to click «Stop» and wait for it to finish closing.\n\n'
                                  'Close anyway?',
    'app_init_skipped': '⚠️ dev/frontend not found — app/ initialization skipped',
    'app_init_done': '✅ app/ initialized from dev/frontend',
    'app_init_error': '❌ Error initializing app/: {error}',
    'log_session_header': '# BOLD DB Creator — session {ts}',
    'language_label': 'Language',
    'restart_required_title': 'Restart to apply',
    'restart_required_body': 'The language change applies when the app restarts.\n\n'
                             'Restart now?',
    'btn_restart_now': 'Restart now',
    'btn_restart_later': 'Later',
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
    """Reads dev/fields_config.json. Returns the factory selection if it doesn't exist.

    Never raises: a corrupt or half-written file shouldn't prevent the
    application from starting, so it falls back to the defaults.
    """
    fields  = list(DEFAULT_FIELDS)
    indexed = list(DEFAULT_INDEXED)
    try:
        with open(FIELDS_CONFIG_FILE, encoding="utf-8") as f:
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
    tmp = FIELDS_CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FIELDS_CONFIG_FILE)


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
# outside this set; col_processid already has its own PRIMARY KEY index.
_FTS_EXCLUDE = {"col_country_iso"}


def fts_cols(cfg=None):
    return [c for c in index_cols(cfg) if c not in _FTS_EXCLUDE]

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
        _logs_dir = os.path.normpath(os.path.join(PROJECT_ROOT, "..", "app", "logs"))
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

def _cleanup_raw_files(log, gz_path, tsv_path):
    """Deletes the .tar.gz and the unfiltered TSV once filtering is complete."""
    for path, label in [(gz_path, ".tar.gz"), (tsv_path, t('unfiltered_tsv'))]:
        if path and os.path.exists(path):
            try:
                os.remove(path)
                log(t('deleted_processed', label=label, name=os.path.basename(path)))
            except OSError as e:
                log(t('could_not_delete_named', name=os.path.basename(path), error=str(e)))


_FIELDS_MARKER = "../data/processed/fields_source.txt"


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
    log("✅ Campos verificados contra la cabecera del TSV: {} de {} configurados, "
        "sobre {} disponibles".format(len(cols), len(cfg["fields"]), rep["total"]))
    return cols, filt_idx


def run_step1(log, progress, cfg):
    os.chdir(PROJECT_ROOT)
    try:
        os.makedirs("../data/processed", exist_ok=True)

        # ---- Version detection and cleanup of obsolete files -----------------
        gz_files   = sorted(glob.glob("../data/raw/*.tar.gz"), key=os.path.getmtime, reverse=True)
        gz_path    = gz_files[0] if gz_files else None
        gz_version = None
        if gz_path:
            for old in gz_files[1:]:
                log(t('deleting_obsolete_targz', name=os.path.basename(old)))
                try: os.remove(old)
                except OSError as e: log(t('could_not_delete', error=str(e)))
            name = os.path.basename(gz_path)
            gz_version = name[:-len(".tar.gz")] if name.endswith(".tar.gz") else os.path.splitext(name)[0]
            os.makedirs("../app/static", exist_ok=True)
            for f in glob.glob("../app/static/*.tsv"):
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
            os.makedirs("../app/static", exist_ok=True)
            for f in glob.glob("../app/static/*.tsv"):
                try: os.remove(f)
                except OSError: pass
            open("../app/static/" + tsv_marker, "w").close()
            log(t('filtered_tsv_exists', name=os.path.basename(filt_best)))
            _cleanup_raw_files(log, gz_path, max(raw_tsv, key=os.path.getsize) if raw_tsv else None)
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
            try:
                for member in _tar:
                    if extract_abort:
                        break
                    if not member.isfile():
                        _tar.extract(member, "../data/processed")
                        continue
                    f_in = _tar.extractfile(member)
                    if f_in is None:
                        continue
                    safe = os.path.normpath(member.name).lstrip(os.sep)
                    out_path = os.path.join("../data/processed", safe)
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

        os.makedirs("../app/static", exist_ok=True)
        for f in glob.glob("../app/static/*.tsv"):
            os.remove(f)
        open("../app/static/" + tsv_filename, "w").close()

        # ---- Sub-step B: Filter .tsv -> .filt.tsv ------------------------------
        archivo_filtrado = archivo_tsv.replace(".tsv", ".filt.tsv")
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
                    if val and val != "None":
                        row = tuple(parts[c] if c < len(parts) else "" for c in col_idx)
                        # str.isascii() is O(1) in CPython (a flag in the object's
                        # header), so the vast majority of rows — already pure
                        # ASCII — pay nothing for this check.
                        if not line.isascii():
                            row = tuple(normalize_field(v) for v in row)
                        if nuc_pos is not None and row[nuc_pos]:
                            row = row[:nuc_pos] + (normalize_nuc(row[nuc_pos]),) + row[nuc_pos + 1:]
                        batch.append(row)
                        written += 1
                        if len(batch) >= _BATCH:
                            writer.writerows(batch)
                            batch.clear()
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
        log(t('file_label', name=os.path.basename(archivo_filtrado)))
        _cleanup_raw_files(log, gz_path, archivo_tsv)
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
                    _has_data = bool(cur_chk.fetchone())
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

        # Must exist before the try/finally: the finally block reads it, and an
        # early failure (e.g. CREATE TABLE) would raise NameError, hiding the real cause
        stopped = False
        # OFF instead of MEMORY: the table is rebuilt from scratch and the DB
        # is deleted if the step fails or is interrupted, so there's nothing
        # to roll back. Avoids writing the journal for every transaction.
        conn = _connect_retry(cfg.get("db_file", DB_FILE), [
            "PRAGMA synchronous   = OFF",
            "PRAGMA journal_mode  = OFF",
            "PRAGMA cache_size    = -500000",   # ~500 MB cache
            "PRAGMA temp_store    = MEMORY",
            "PRAGMA locking_mode  = EXCLUSIVE",
            "PRAGMA mmap_size     = 30000000000",
        ], log)
        try:
            # Invalidate FTS before rebuilding the table — fts_source will stop matching
            try: os.remove("../data/processed/fts_source.txt")
            except OSError: pass

            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS bold_records")
            cur.execute("CREATE TABLE bold_records ({})".format(", ".join(cols_sql)))
            conn.commit()
            log(t('table_created'))

            log(t('importing_data'))
            chunk_n      = cfg.get("chunksize", CHUNKSIZE)
            cols_clean   = list(mapeo.values())
            n_cols       = len(cols_clean)
            placeholders = ",".join(["?"] * n_cols)
            insert_q     = "INSERT OR IGNORE INTO bold_records VALUES ({})".format(placeholders)

            _IO_BUF    = 8 * 1024 * 1024   # 8 MB read buffer
            _COMMIT_N  = 5                  # commit every N chunks
            processed  = chunk_count = 0
            stop       = cfg.get("_stop")
            _t0        = time.monotonic()
            _last_ui   = 0.0

            with open(archivo_tsv, "r", encoding="utf-8", errors="replace",
                      newline="", buffering=_IO_BUF) as fin:
                reader = csv.reader(fin, delimiter="\t")
                next(reader)   # skip header
                batch = []
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
                    if len(row) == n_cols:
                        batch.append(tuple(None if v in _NULLISH else v for v in row))
                    else:
                        batch.append(tuple(
                            None if (row[i] if i < len(row) else "") in _NULLISH
                            else row[i]
                            for i in range(n_cols)
                        ))
                    processed += 1
                    if len(batch) >= chunk_n:
                        cur.executemany(insert_q, batch)
                        batch.clear()
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
                                progress(min(processed / total_lines * 100, 99))
                            elapsed = max(_now - _t0, 0.001)
                            log(t('import_progress', processed=processed, total=total_lines, rate=processed / elapsed))
                if batch:
                    cur.executemany(insert_q, batch)
            conn.commit()
            if stopped:
                return False

            with open("../data/processed/column_mapping.csv", "w", encoding="utf-8") as f:
                f.write("Original,Limpio\n")
                for orig, clean in mapeo.items():
                    f.write("{},{}\n".format(orig, clean))

            cur.execute("SELECT COUNT(*) FROM bold_records")
            real_count = cur.fetchone()[0]
            with open("../data/processed/db_source.txt", "w") as _f:
                _f.write(os.path.basename(archivo_tsv))
            log(t('db_created', name=os.path.basename(cfg.get("db_file", DB_FILE))))
            log(t('records_inserted', n=real_count))
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
            if stopped:
                db_path = cfg.get("db_file", DB_FILE)
                try: os.remove(db_path)
                except OSError: pass
                try: os.remove("../data/processed/db_source.txt")
                except OSError: pass
                log(t('import_interrupted'))
    except Exception as e:
        log(_fmt_error(e, t('ctx_tsv_to_sqlite')))
        return False

# ---- Step 3: Index fields --------------------------------------------------------

def run_step3(log, progress, cfg):
    os.chdir(PROJECT_ROOT)
    db_path = cfg.get("db_file", DB_FILE)
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
                    log(t('index_error', i=i + 1, n=len(pendientes), col=col, error=e))
                progress((i + 1) / len(pendientes) * 100)

            conn.commit()  # a single commit at the end — removes the overhead of N intermediate commits
            conn.execute("PRAGMA optimize")
            log(t('indexes_created', n=len(nuevos)))
            progress(100)
            return True
        finally:
            try:
                conn.execute("PRAGMA synchronous  = NORMAL")
                conn.execute("PRAGMA journal_mode = DELETE")
            except Exception:
                pass
            conn.close()
    except Exception as e:
        log(_fmt_error(e, t('ctx_index_fields')))
        return False

# ---- Step 6: Create FTS ----------------------------------------------------------

def run_step6(log, progress, cfg):
    os.chdir(PROJECT_ROOT)
    if not os.path.exists(DB_FILE):
        log(t('db_not_found', path=DB_FILE))
        return False
    conn = None
    try:
        # check_same_thread=False: the 'rebuild' runs in a helper thread so it
        # can emit heartbeats while it lasts. Only one thread touches the connection at a time.
        conn = _connect_retry(DB_FILE, [
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
                    if cur.fetchone():
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
        # It can't be interrupted partway through.
        log(t('rebuilding_fts_index', n=total))
        log(t('fts_may_take_minutes'))
        progress(-1)

        # 'rebuild' reports no progress and can't be interrupted. Without a
        # heartbeat, the GUI stays identical for several minutes and the user
        # assumes it froze and kills the application.
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
        while not _rb_done.wait(30):
            mins = (time.monotonic() - _rb_t0) / 60
            log(t('still_working', mins=mins))
        _t_rb.join()
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


# ==============================================================================
# GUI — PySide6
# ==============================================================================

# ---- Palette & stylesheet ---------------------------------------------------------

P = {
    "bg":      "#2a2d3a",
    "panel":   "#26262D",
    "sidebar": "#3B3A44",
    "accent":  "#88A6D7",
    "green":   "#61D8FD",
    "red":     "#f38ba8",
    "white":   "#88A6D7",
    "gray":    "#45475a",
    "yellow":  "#c09b07",
    "blue":    "#295EB3",
    "text":    "#d5d8dd",
    "sub":     "#d6dcf8",
    "surface": "#535366",
    "overlay": "#477DD4",
    "running": "#fab387",
    "progbar": "#61D8FD",
    "blue_mid": "#669AEE",
    "done":     "#52547a",
    # Readable green for confirmations: #52547a against the log background gave
    # ~2:1 contrast, making unreadable exactly the thing that confirms success.
    "ok":       "#7fe0a0",
    "req":      "#f38ba8",   # REQUIRED badge
    "rec":      "#e0b520",   # RECOMMENDED badge
    "opt":      "#8a8da0",   # OPTIONAL badge
}

def _make_app_qss() -> str:
    p = dict(P)
    p.update({
        "fs7":  _fs(7),  "fs9":  _fs(9),  "fs10": _fs(10),
        "fs11": _fs(11), "fs12": _fs(12), "fs13": _fs(13),
        "px15": _px(15),
    })
    return """
QMainWindow, QWidget {
    background-color: %(bg)s;
    color: %(text)s;
    font-family: "Segoe UI";
    font-size: %(fs13)s;
}
#sidebar { background-color: %(sidebar)s; }
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
QPushButton#navBtn[active="true"]:hover { background-color: %(gray)s; }
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
QPushButton#homeBtn[active="true"]:hover { background-color: %(gray)s; }
/* This was inverted: the disabled button used to be painted bright blue and
   the enabled one dull gray — exactly the opposite of the expected signal. */
QPushButton#runBtn {
    background-color: %(blue)s;
    color: #ffffff;
    border: none;
    padding: 7px 18px;
    font-weight: bold;
    font-size: %(fs12)s;
    border-radius: 6px;
}
QPushButton#runBtn:hover    { background-color: %(overlay)s; color: #ffffff; }
QPushButton#runBtn:disabled { background-color: %(gray)s; color: %(done)s; }
QPushButton#primaryBtn {
    background-color: %(blue)s;
    color: #ffffff;
    border: none;
    padding: 10px 24px;
    font-weight: bold;
    font-size: %(fs13)s;
    border-radius: 8px;
}
QPushButton#primaryBtn:hover    { background-color: %(overlay)s; }
QPushButton#primaryBtn:disabled { background-color: %(gray)s; color: %(sub)s; }
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
QPushButton#secondaryBtn:hover    { background-color: %(surface)s; border-color: %(overlay)s; }
QPushButton#secondaryBtn:disabled { color: %(done)s; border-color: %(gray)s; }
QPushButton#cardBtn:disabled    { background-color: %(gray)s; color: %(done)s; }
QPushButton#stopBtn {
    background-color: transparent;
    color: %(red)s;
    border: 2px solid %(red)s;
    padding: 7px 14px;
    font-size: %(fs12)s;
    border-radius: 6px;
}
QPushButton#stopBtn:hover    { background-color: %(red)s; color: #11111b; border-color: %(red)s; }
QPushButton#stopBtn:disabled { color: %(overlay)s; border-color: %(overlay)s; border-width: 1px; }
QPushButton#browseBtn {
    background-color: %(overlay)s;
    color: %(text)s;
    border: none;
    padding: 3px 8px;
    border-radius: 6px;
}
QPushButton#browseBtn:hover { background-color: %(surface)s; color: %(accent)s; }
QPushButton#cardBtn {
    background-color: %(overlay)s;
    color: %(text)s;
    border: none;
    padding: 4px 12px;
    font-size: %(fs12)s;
    border-radius: 6px;
}
QPushButton#cardBtn:hover { background-color: %(surface)s; color: %(accent)s; }
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
QPushButton#dangerBtn:disabled { color: %(done)s; border-color: %(gray)s; }
QPushButton#clearBtn {
    background-color: transparent;
    color: %(sub)s;
    border: none;
    font-size: %(fs7)s;
    padding: 2px 8px;
    border-radius: 2px;
}
QPushButton#clearBtn:hover { background-color: %(surface)s; color: %(text)s; }
/* Without this rule, every tooltip fell back to the system default color
   (white or pale yellow on Windows) instead of the app's dark theme — and a
   widget with its own setStyleSheet (like the headers in the Fields panel)
   could also inherit a tone slightly different from a "default" tooltip.
   With the explicit rule, every tooltip in the application always uses the
   same colors, with no exceptions. */
QToolTip {
    background-color: %(surface)s;
    color: %(text)s;
    border: 1px solid %(overlay)s;
    padding: 4px 8px;
    font-family: "Segoe UI";
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
QMessageBox QPushButton:hover   { background-color: %(overlay)s; color: %(accent)s; }
QMessageBox QPushButton:default { background-color: %(accent)s; color: %(bg)s; }
QMessageBox QPushButton:default:hover { background-color: #d4b8f8; color: %(bg)s; }
QSplitter#mainSplit::handle { background-color: %(sidebar)s; }
QSplitter#mainSplit::handle:hover { background-color: %(overlay)s; }
#panel { background-color: %(panel)s; }
#panel QLabel { background-color: transparent; }
QTextEdit#logEdit {
    background-color: #26262D;
    color: %(text)s;
    border: none;
    font-family: "Courier New", "Consolas", monospace;
    font-size: %(fs11)s;
}
QProgressBar {
    background-color: %(surface)s;
    border: none;
    border-radius: 5px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #8ae8ff, stop:1 %(progbar)s);
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
QScrollBar::handle:vertical      { background: %(overlay)s; border-radius: 4px; min-height: 20px; }
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical    { height: 0px; }
/* Checkboxes in the Fields panel. Without their own style, Qt paints them
   with the system's light theme, and against the dark background a checked
   box can't be told apart from an empty one. */
QCheckBox { color: %(text)s; font-size: %(fs11)s; spacing: 7px; background: transparent; }
QCheckBox:disabled { color: %(done)s; }
QCheckBox::indicator {
    width: %(px15)spx; height: %(px15)spx;
    border: 1px solid %(overlay)s;
    border-radius: 3px;
    background: %(bg)s;
}
QCheckBox::indicator:hover            { border-color: %(progbar)s; }
QCheckBox::indicator:checked          { background: %(progbar)s; border-color: %(progbar)s; }
QCheckBox::indicator:disabled         { border-color: %(gray)s; }
QCheckBox::indicator:checked:disabled { background: %(done)s; border-color: %(done)s; }
QFrame#dropCard {
    background-color: transparent;
    border: 2px dashed %(overlay)s;
    border-radius: 6px;
}
QFrame#dropCard[loaded="true"]   { border: 1px solid %(accent)s; }
QFrame#dropCard[dragging="true"] { border: 2px dashed %(blue)s; background-color: %(surface)s; }
""" % p

# ---- Steps definition --------------------------------------------------------

# Short names for each step, used to build both the numbered label
# ("1. Prepare data") and the variants ("Step 1 — Prepare data") that appear
# in log headers and warnings. LANG is fixed for the whole process, so t()
# always returns the same text here — the STEP_TECH/STEP_LEVEL/STEP_COST/etc.
# keys (all built from STEPS[i][0]) stay consistent with each other for the
# duration of the session.
STEP_NAMES = [t('step1_name'), t('step2_name'), t('step3_name'), t('step4_name')]

STEPS = [
    ("1. " + STEP_NAMES[0], t('step1_desc'), run_step1, {}),
    ("2. " + STEP_NAMES[1], t('step2_desc'), run_step2, {}),
    ("3. " + STEP_NAMES[2], t('step3_desc'), run_step3, {}),
    ("4. " + STEP_NAMES[3], t('step4_desc'), run_step6, {}),
]

# Technical label (for those who actually know the pipeline) and requirement
# level, which used to be embedded as '**REQUIRED**' in the description and
# showed up with the literal asterisks because the QLabel received plain text.
STEP_TECH = {
    STEPS[0][0]: t('step1_tech'),
    STEPS[1][0]: t('step2_tech'),
    STEPS[2][0]: t('step3_tech'),
    STEPS[3][0]: t('step4_tech'),
}

STEP_LEVEL = {
    STEPS[0][0]: (t('badge_required'),    "req"),
    STEPS[1][0]: (t('badge_required'),    "req"),
    STEPS[2][0]: (t('badge_recommended'), "rec"),
    STEPS[3][0]: (t('badge_optional'),    "opt"),
}

# Approximate duration and space, shown in the panel and in tooltips.
STEP_COST = {
    STEPS[0][0]: t('step1_cost'),
    STEPS[1][0]: t('step2_cost'),
    STEPS[2][0]: t('step3_cost'),
    STEPS[3][0]: t('step4_cost'),
}

_SIDEBAR_GROUPS = [
    (t('sidebar_group_individual_steps'), [0, 1, 2, 3], t('sidebar_group_hint')),
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
]

# Web viewer port (app/server.py -> app.run(port=5001))
VIEWER_PORT = 5001

# Portal from which Step 1's input .tar.gz is downloaded
BOLD_DOWNLOAD_URL = "https://bench.boldsystems.org/index.php/datapackages/Latest"

# The Home panel goes last in the stack but first in the sidebar
PREP_IDX   = len(STEPS)
FIELDS_IDX = len(STEPS) + 1


# ---- data/ initialization ------------------------------------------------------
#
# data/ is entirely generated content (see .gitignore) and git never tracks
# empty folders, so a fresh clone from GitHub has no data/ tree at all — not
# even data/raw/, where Step 1 expects the downloaded .tar.gz to be dropped.
# Recreated here on every startup, the same way app/ is, so a first-time user
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
    ):
        try:
            os.makedirs(_proj_dir(*parts), exist_ok=True)
        except OSError:
            pass


# ---- app/ initialization -------------------------------------------------------

def _ensure_app_initialized():
    """Copies dev/frontend -> app/ if the folder hasn't been initialized. Returns a message or None."""
    src = os.path.join(PROJECT_ROOT, "frontend")
    dst = os.path.normpath(os.path.join(PROJECT_ROOT, "..", "app"))
    if (os.path.exists(os.path.join(dst, "server.py")) and
            os.path.exists(os.path.join(dst, "templates", "index.html"))):
        return None
    if not os.path.exists(src):
        return t('app_init_skipped')
    try:
        os.makedirs(dst, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return t('app_init_done')
    except Exception as e:
        return t('app_init_error', error=e)


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
    """
    out = []
    for path in sorted(glob.glob(_proj_dir("data", "processed", "*.tsv"))):
        try:
            out.append((path, os.path.getsize(path)))
        except OSError:
            pass
    return out


def _check_input(step_label):
    db = _proj_dir("app", "bold_db.db")
    checks = {
        STEPS[0][0]: lambda: (
            len(glob.glob(_proj_dir("data", "raw", "*.tar.gz"))) > 0 or
            len(glob.glob(_proj_dir("data", "processed", "*.filt.tsv"))) > 0
        ),
        STEPS[1][0]: lambda: len(glob.glob(_proj_dir("data", "processed", "*.filt.tsv"))) > 0,
        STEPS[2][0]: lambda: os.path.exists(db),
        STEPS[3][0]: lambda: os.path.exists(db),
    }
    fn = checks.get(step_label)
    return fn() if fn else True


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
    db   = os.path.join(root, "..", "app", "bold_db.db")

    version = None
    static_tsvs = glob.glob(os.path.join(root, "..", "app", "static", "*.tsv"))
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
    if os.path.exists(db):
        try:
            conn = sqlite3.connect(db, timeout=0.3)
            cur  = conn.cursor()
            cur.execute("SELECT 1 FROM bold_records LIMIT 1")
            if cur.fetchone():
                step2 = True
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
    if step2:
        try:
            conn = sqlite3.connect(db, timeout=0.3)
            cur  = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bold_records_fts'")
            step4 = bool(cur.fetchone())
            conn.close()
        except Exception:
            pass

    # The .filt.tsv can be deleted once imported (it takes up tens of GB), so
    # its absence with the DB already built doesn't mean Step 1 is missing:
    # marking it pending produced the absurd sequence "1 ○  2 ✅".
    st = {"version": version, "step1": step1, "step2": step2,
          "step3": step3, "step4": step4, "record_count": record_count,
          "done1": step1 or step2}
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
    """
    progress_signal = Signal(float)
    finished_signal = Signal(bool, str)

    _CHUNK = 8 * 1024 * 1024

    def __init__(self, src, dst, parent=None):
        super().__init__(parent)
        self._src = src
        self._dst = dst

    def run(self):
        try:
            total = os.path.getsize(self._src) or 1
            copied = 0
            with open(self._src, "rb") as fin, open(self._dst, "wb") as fout:
                while True:
                    chunk = fin.read(self._CHUNK)
                    if not chunk:
                        break
                    fout.write(chunk)
                    copied += len(chunk)
                    self.progress_signal.emit(min(copied / total * 100, 100))
            self.finished_signal.emit(True, self._dst)
        except Exception as e:
            try: os.remove(self._dst)
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
        lbl.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(12)}; font-weight: bold;")
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
        self._hint.setStyleSheet(f"color: {P['overlay']}; font-size: {_fs(12)}; padding: 8px 0;")
        lay.addWidget(self._hint)

        self._file_lbl = QLabel()
        self._file_lbl.setStyleSheet(f"color: {P['ok']}; font-size: {_fs(12)};")
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

    # -- accepting a file ---------------------------------------------------

    def _accept(self, path):
        if not path or not os.path.isfile(path):
            return
        if not path.lower().endswith(self._extensions):
            QMessageBox.warning(
                self, t('invalid_file_title'),
                t('invalid_file_body', ext=" o ".join(self._extensions)),
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

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(6)

        # ---- Title + requirement badge ---------------------------------------
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel(label)
        title.setStyleSheet(f"color: {P['accent']}; font-size: {_fs(14)}; font-weight: bold;")
        title_row.addWidget(title)

        level, level_key = STEP_LEVEL.get(label, ("", "opt"))
        if level:
            badge = QLabel(level)
            badge.setStyleSheet(
                f"color: {P['bg']}; background-color: {P[level_key]};"
                f" font-size: {_fs(9)}; font-weight: bold;"
                " border-radius: 4px; padding: 2px 9px;"
            )
            badge.setToolTip({
                t('badge_required'):    t('badge_tip_required'),
                t('badge_recommended'): t('badge_tip_recommended'),
                t('badge_optional'):    t('badge_tip_optional'),
            }.get(level, ""))
            title_row.addWidget(badge)
        title_row.addStretch()

        cost = STEP_COST.get(label, "")
        if cost:
            cost_lbl = QLabel(cost)
            cost_lbl.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(10)};")
            cost_lbl.setToolTip(t('cost_tooltip'))
            title_row.addWidget(cost_lbl)
        lay.addLayout(title_row)

        tech = STEP_TECH.get(label, "")
        if tech:
            tech_lbl = QLabel(tech)
            tech_lbl.setStyleSheet(
                f"color: {P['done']}; font-size: {_fs(10)};"
                " font-family: 'Courier New', monospace;"
            )
            lay.addWidget(tech_lbl)

        _cut = desc.find("\nEntrada:")
        desc_body  = desc[:_cut].strip()  if _cut >= 0 else desc.strip()
        desc_paths = desc[_cut + 1:].strip() if _cut >= 0 else ""

        desc_lbl = QLabel(desc_body)
        desc_lbl.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(11)};")
        desc_lbl.setWordWrap(True)
        lay.addWidget(desc_lbl)

        if desc_paths:
            io_sep = QFrame()
            io_sep.setFrameShape(QFrame.Shape.HLine)
            io_sep.setStyleSheet(f"background: {P['surface']}; border: none; max-height: 1px;")
            lay.addWidget(io_sep)
            paths_lbl = QLabel(desc_paths)
            paths_lbl.setStyleSheet(
                f"color: {P['done']}; font-size: {_fs(11)};"
                " font-family: 'Courier New', monospace;"
            )
            paths_lbl.setWordWrap(True)
            lay.addWidget(paths_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {P['surface']}; border: none; max-height: 1px;")
        lay.addWidget(sep)
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

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        self._input_lbl = QLabel()
        self._input_lbl.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(11)};")
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
        self.status_lbl.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(9)};")
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
        self._prog_note.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(9)};")
        self._prog_note.setWordWrap(True)
        self._prog_note.hide()
        lay.addWidget(self._prog_note)

        self._refresh_input_status()

    def _open_input_folder(self):
        folders = {
            STEPS[0][0]: _proj_dir("data", "raw"),
            STEPS[1][0]: _proj_dir("data", "processed"),
            STEPS[2][0]: _proj_dir("app"),
            STEPS[3][0]: _proj_dir("app"),
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
        lbl.setStyleSheet(f"color: {P['text']}; font-size: {_fs(11)};")
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
        hint.setStyleSheet(f"color: {P['done']}; font-size: {_fs(10)};")
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
        return self._worker is not None and self._worker.isRunning()

    # What's missing and where, instead of a generic "Input not found".
    _MISSING_MSG = {
        STEPS[0][0]: t('missing_bold_file'),
        STEPS[1][0]: t('missing_prepared_data'),
        STEPS[2][0]: t('missing_database'),
        STEPS[3][0]: t('missing_database'),
    }

    def _is_done(self):
        """Has this step already produced its output? Shown differently from 'missing input'."""
        try:
            st = _get_project_status()
        except Exception:
            return False
        return {
            STEPS[0][0]: st["done1"],
            STEPS[1][0]: st["step2"],
            STEPS[2][0]: st["step3"],
            STEPS[3][0]: st["step4"],
        }.get(self._label, False)

    def _refresh_input_status(self):
        if self._drop_card is not None:
            self._drop_card.scan_existing()
        ok = _check_input(self._label)
        if self._is_done():
            self._input_lbl.setText(t('step_done_already'))
            self._input_lbl.setStyleSheet(f"color: {P['ok']}; font-size: {_fs(11)};")
            self._input_btn.hide()
        elif ok:
            self._input_lbl.setText(t('step_ready'))
            self._input_lbl.setStyleSheet(f"color: {P['ok']}; font-size: {_fs(11)};")
            self._input_btn.hide()
        else:
            self._input_lbl.setText(self._MISSING_MSG.get(
                self._label, t('input_not_found_generic')))
            self._input_lbl.setStyleSheet(f"color: {P['yellow']}; font-size: {_fs(11)};")
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
                    t('missing_input_files_tip', msg=self._MISSING_MSG.get(self._label, "").lstrip("● ")))
            else:
                self.run_btn.setToolTip(
                    t('run_only_step_tip', cost=STEP_COST.get(self._label, "")))

    def _start(self):
        if self.is_running():
            return
        self._refresh_input_status()
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
        self._worker.start()

    def _set_progbar_color(self, color):
        lighter = {
            P["progbar"]: "#8ae8ff",
            P["yellow"]:  "#e0b520",
            P["red"]:     "#f8afc0",
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

    def set_stopping_ui(self):
        self._stopping = True
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.prog_bar.setRange(0, 100)
        self._set_progbar_color(P["yellow"])
        self.status_lbl.setStyleSheet(f"color: {P['yellow']}; font-size: {_fs(9)};")
        self.status_lbl.setText(t('stopping_status'))

    def _set_running_ui(self):
        self._stopping = False
        self.run_btn.setEnabled(False)
        self.run_btn.setToolTip(t('already_running_tip'))
        self.stop_btn.setEnabled(True)
        self._prog_note.hide()
        self.prog_bar.setRange(0, 100)
        self.prog_bar.setValue(0)
        self._set_progbar_color(P["progbar"])
        self.status_lbl.setStyleSheet(f"color: {P['running']}; font-size: {_fs(9)};")
        self.status_lbl.setText(t('running_status'))

    def set_running_ui(self):
        self._set_running_ui()

    def set_done_ui(self, ok):
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
            self.status_lbl.setStyleSheet(
                f"color: {P['bg']}; background-color: {P['ok']};"
                f" font-size: {_fs(10)}; font-weight: bold;"
                " border-radius: 4px; padding: 2px 10px;"
            )
            self.status_lbl.setText(t('completed_status'))
            self.prog_bar.setValue(100)
            if not self.app._seq_running:
                self.app._dim_completed_task()
                self.app.log_next_action()
        elif was_stopping:
            self._set_progbar_color(P["yellow"])
            self.status_lbl.setStyleSheet(f"color: {P['yellow']}; font-size: {_fs(9)};")
            self.status_lbl.setText(t('stopped_status'))
            self.prog_bar.setValue(0)
        else:
            self._set_progbar_color(P["red"])
            self.status_lbl.setStyleSheet(f"color: {P['red']}; font-size: {_fs(9)};")
            self.status_lbl.setText(t('error_status'))

    def set_stopped_ui(self):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.app.stop_timer()
        self.prog_bar.setRange(0, 100)
        self.prog_bar.setValue(0)
        self._set_progbar_color(P["yellow"])
        self.status_lbl.setStyleSheet(f"color: {P['yellow']}; font-size: {_fs(9)};")
        self.status_lbl.setText(t('stopped_status'))

    def _stop(self):
        self.app.stop_current(self)


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
        title.setStyleSheet(f"color: {P['accent']}; font-size: {_fs(16)}; font-weight: bold;")
        lay.addWidget(title)

        intro = QLabel(t('home_intro'))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {P['text']}; font-size: {_fs(11)};")
        lay.addWidget(intro)

        # The three requirements used to be spelled out one by one here. What
        # the user actually has to *do* is bring in the .tar.gz, so that gets
        # the same drop zone as Step 1 and the rest is condensed into one line:
        # otherwise the only way to load the file from Home was to go to Step 1.
        req_note = QLabel(t('home_req_note'))
        req_note.setTextFormat(Qt.TextFormat.RichText)
        req_note.setWordWrap(True)
        req_note.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(11)};")
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

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {P['surface']}; border: none; max-height: 1px;")
        lay.addWidget(sep)

        # ---- Summary status ---------------------------------------------------
        self._status_lbl = QLabel("")
        self._status_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(f"color: {P['text']}; font-size: {_fs(11)};")
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
            hdr.setStyleSheet(
                f"color: {P['accent']}; font-size: {_fs(12)}; font-weight: bold;"
            )
            hdr_row.addWidget(hdr)
            if pl["badge"]:
                badge = QLabel(pl["badge"])
                badge.setStyleSheet(
                    f"color: {P['bg']}; background-color: {P['ok']};"
                    f" font-size: {_fs(9)}; font-weight: bold;"
                    " border-radius: 4px; padding: 2px 9px;"
                )
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
            hint.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(10)};")
            opts.addWidget(hint)
        lay.addLayout(opts)

        # Compatibility with the rest of the class (bulk enable/disable)
        self.main_btn = self.pipeline_btns[0]

        self._main_hint = QLabel("")
        self._main_hint.setWordWrap(True)
        self._main_hint.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(10)};")
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
        self.viewer_btn.setObjectName("cardBtn")
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

        self.free_btn = QPushButton(t('btn_free_space'))
        self.free_btn.setObjectName("dangerBtn")
        self.free_btn.clicked.connect(self.app.free_disk_space)
        _add(self.free_btn)
        lay.addLayout(row2)

        self.refresh()

    def refresh(self, st=None):
        st = st or _get_project_status()
        self._drop_card.scan_existing()
        def mark(done): return f'<span style="color:{P["ok"]}">✅</span>' if done \
                          else f'<span style="color:{P["yellow"]}">○</span>'
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
        flags = [st["done1"], st["step2"], st["step3"], st["step4"]]
        for btn, pl in zip(self.pipeline_btns, _PIPELINES):
            done = all(flags[i] for i in pl["steps"])
            btn.setText(("↻   " + pl["label_done"]) if done else ("▶   " + pl["label"]))
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
        title.setStyleSheet(f"color: {P['accent']}; font-size: {_fs(16)}; font-weight: bold;")
        outer.addWidget(title)

        intro = QLabel(t('fields_panel_intro'))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {P['text']}; font-size: {_fs(11)};")
        outer.addWidget(intro)

        warn = QLabel(t('fields_panel_warning'))
        warn.setTextFormat(Qt.TextFormat.RichText)
        warn.setWordWrap(True)
        warn.setStyleSheet(
            f"color: {P['text']}; font-size: {_fs(10)}; background-color: {P['sidebar']};"
            " border-radius: 6px; padding: 8px 10px;"
        )
        outer.addWidget(warn)

        self._src_lbl = QLabel("")
        self._src_lbl.setWordWrap(True)
        self._src_lbl.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(10)};")
        outer.addWidget(self._src_lbl)

        # ---- Header for the two checkbox columns -------------------------------
        hdr = QHBoxLayout()
        hdr.setContentsMargins(2, 0, _px(18), 0)
        h1 = QLabel(t('col_header_field'))
        h1.setToolTip(t('tip_include_field'))
        h1.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(10)}; font-weight: bold;")
        hdr.addWidget(h1)
        hdr.addStretch()
        h2 = QLabel(t('col_header_index'))
        h2.setToolTip(t('tip_create_index'))
        h2.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(10)}; font-weight: bold;")
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
        self._count_lbl.setStyleSheet(f"color: {P['text']}; font-size: {_fs(11)};")
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
                tag.setStyleSheet(f"color: {P['red']}; font-size: {_fs(9)};")
                hint = rep["hints"].get(name)
                tag.setToolTip(
                    t('tip_field_missing')
                    + (t('tip_field_missing_hint', hint=hint) if hint else "")
                )
            elif n in added:
                tag.setText(t('tag_new_in_bold'))
                tag.setStyleSheet(f"color: {P['rec']}; font-size: {_fs(9)};")
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
        self._init_msg = _ensure_app_initialized()
        self._init_log_file()
        # Before _build(): resize events start arriving as soon as the
        # widgets are built, and the timer must already exist by then.
        self._init_settings()
        self._build()
        self._restore_geometry()
        QTimer.singleShot(0, self._show_initial_status)

    # ---- Window size and position across sessions -----------------------------

    def _init_settings(self):
        """UI state in app/ui_state.ini, alongside the rest of what's generated.

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
            path = _proj_dir("app", "ui_state.ini")
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
        self._save_geometry()
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
        python = sys.executable
        os.execl(python, python, *sys.argv)

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
            f"color: {P['white']}; font-size: {_fs(16)}; font-weight: bold; padding: 20px 0 4px 0;"
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
                f"color: {P['white']}; font-size: {_fs(10)}; font-weight: bold;"
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
                btn.setToolTip("{}  ·  {}  ·  {}".format(
                    level, STEP_COST.get(STEPS[i][0], ""), STEP_TECH.get(STEPS[i][0], "")))
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
            f"color: {P['white']}; font-size: {_fs(10)}; font-weight: bold;"
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
        lang_lbl.setStyleSheet(f"color: {P['sub']}; font-size: {_fs(9)};")
        lang_row.addWidget(lang_lbl)
        lang_row.addStretch()
        for code in ('es', 'en'):
            lbtn = QPushButton(code.upper())
            lbtn.setFixedWidth(_px(34))
            active = code == LANG
            lbtn.setStyleSheet(
                f"font-size: {_fs(10)}; font-weight: bold; border-radius: 4px; padding: 3px 0;"
                + (f"background-color: {P['accent']}; color: {P['bg']};" if active
                   else f"background-color: {P['sidebar']}; color: {P['sub']};")
            )
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
            " font-family: 'Courier New', 'Consolas', monospace;"
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
        # in app/logs/ regardless.
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
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"background: {P['surface']}; border: none; max-height: 1px;")
        return f

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
        flags = [st["done1"], st["step2"], st["step3"], st["step4"]]
        for i, done in enumerate(flags):
            if self._btns[i]:
                self._btns[i].setText("{}  {}".format("✅" if done else "○", STEPS[i][0]))
        if getattr(self, "_prep", None):
            self._prep.refresh(st)
        # Only if it's visible: rebuilding its checkboxes while the user is
        # looking at another panel achieves nothing and could clobber a
        # half-finished selection.
        if getattr(self, "_fields", None) and self._current == FIELDS_IDX:
            self._fields.refresh(st)

    # ---- Home actions -----------------------------------------------------------

    def open_viewer(self):
        app_dir = _proj_dir("app")
        server  = os.path.join(app_dir, "server.py")
        if not os.path.exists(server):
            QMessageBox.warning(self, t('viewer_not_found_title'), t('viewer_not_found_body'))
            return
        if not os.path.exists(os.path.join(app_dir, "bold_db.db")):
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
            subprocess.Popen([exe, "server.py"], cwd=app_dir)
            self.log(t('log_viewer_started', port=VIEWER_PORT))
            QTimer.singleShot(
                2000, lambda: webbrowser.open("http://127.0.0.1:{}".format(VIEWER_PORT)))
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
        listado = "\n".join(t('file_list_item', name=os.path.basename(p), size=_fmt_size(s))
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

    def _set_run_buttons_state(self, enabled):
        for p in self._panels:
            p.run_btn.setEnabled(enabled)
        # Saving fields while a step is running would leave Step 1 filtering
        # with one selection while writing the marker for another.
        if getattr(self, "_fields", None):
            self._fields._save_btn.setEnabled(enabled)

    def stop_current(self, panel):
        if self._seq_running and self._seq_worker and self._seq_worker.isRunning():
            self._seq_worker.request_stop()
            i = self._seq_step_indices[self._seq_step_pos]
            self._panels[i].set_stopping_ui()
            self.log(t('log_stopping_pipeline'))
        elif panel._worker and panel._worker.isRunning():
            panel._worker.request_stop()
            panel.set_stopping_ui()
            self.log(t('log_stopping_step'))

    def _run_sequence(self, step_indices, label):
        if self._seq_running:
            QMessageBox.warning(self, t('pipeline_running_title'), t('pipeline_running_body'))
            return
        if any(self._panels[i].is_running() for i in step_indices):
            QMessageBox.warning(self, t('step_running_title'), t('step_running_body'))
            return
        pasos = "\n".join(
            t('sequence_step_item', label=STEPS[i][0], cost=STEP_COST.get(STEPS[i][0], ""))
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
            self.log(t('log_process_finished'))
            self._refresh_all_panels_input()
            self.refresh_sidebar_status()
            self.log_next_action()
            return
        i     = self._seq_step_indices[self._seq_step_pos]
        panel = self._panels[i]
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
        if not ok:
            self.log(t('log_step_failed', time=time_str))
            self._seq_running = False
            self._set_shortcuts_state(True)
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
        # Request text (monochrome) presentation for emoji
        msg  = re.sub(r'([\U0001F000-\U0001FAFF☀-➿])️?', r'\1︎', msg)
        stripped = msg.lstrip()
        bold = False
        if stripped.startswith("❌") or "ERROR" in msg.upper():
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
        elif stripped.startswith(("📋", "📁", "💽", "📝", "📊", "📂", "🚀", "📄", "📜", "🔎", "📘", "📌")):
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
            f'<span style="color:{color}; font-family:\'Courier New\',monospace;'
            f' font-weight:{weight};">{safe}</span>'
        )
        if live:
            html = body
        else:
            html = (
                f'<span style="color:{P["done"]}; font-family:\'Courier New\',monospace;">'
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
            fmt.setForeground(QColor(P["done"]))
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
            self._prep._drop_card.scan_existing()

    def _is_any_running(self):
        if self._seq_running:
            return True
        return any(p.is_running() for p in self._panels)

    def _init_log_file(self):
        logs_dir = os.path.normpath(os.path.join(PROJECT_ROOT, "..", "app", "logs"))
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
        st  = _get_project_status()
        r   = PROJECT_ROOT
        if self._init_msg:
            self.log(self._init_msg)
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

    def closeEvent(self, event):
        if self._is_any_running():
            resp = QMessageBox.question(
                self, t('process_running_close_title'),
                t('process_running_close_body'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self._seq_running and self._seq_worker and self._seq_worker.isRunning():
                self._seq_worker.terminate()
                self._seq_worker.wait(3000)
            for p in self._panels:
                if p.is_running() and p._worker:
                    p._worker.terminate()
                    p._worker.wait(3000)
        if getattr(self, "_fields", None) and not self._fields.confirm_leave():
            event.ignore()
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
    app.setStyleSheet(_make_app_qss())
    window = App()
    window.show()
    sys.exit(app.exec())
