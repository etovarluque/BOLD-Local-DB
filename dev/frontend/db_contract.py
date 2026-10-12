"""The database contract between bold_db_creator.py (writes) and server.py (reads).

Everything both programs need to agree on lives here: the file name, the
table names, the application version and the layout version of the database.
Before, each program kept its own copy of these values, and a change made in
one of them only showed up as SQL errors in the other.

It lives in dev/frontend/ because the Creator copies that folder to app/,
where the viewer runs; the Creator imports it from dev/frontend/ directly.
Standard library only: the Creator imports it before its own dependencies are
checked.
"""

import sqlite3

APP_VERSION = "2.2.0"

# Inside app/
DB_FILENAME = "bold_db.db"
# A full rebuild (Steps 2-4) writes a new database next to the current one,
# under this suffix, and only replaces the current one once it's complete:
# meanwhile the viewer keeps serving the previous version, and a failed or
# stopped rebuild leaves it untouched.
BUILD_SUFFIX = ".building"

MAIN_TABLE     = "bold_records"
FTS_TABLE      = "bold_records_fts"
META_TABLE     = "bold_meta"
ROW_HASH_TABLE = "bold_row_hash"

# Layout version of the database: tables, how columns are named and typed,
# the FTS index's tokenizer... Raise it with any change the viewer or the
# update would misread, and set MIN_VIEWER_SCHEMA to the oldest layout the
# viewer still reads. Written to META_TABLE as "schema_version".
#   1 - bold_records (col_* columns, processid PRIMARY KEY) + bold_records_fts
#       (trigram, external content) + bold_meta + bold_row_hash (processid, h).
#       Kept only the first sequence of each specimen.
#   2 - One row per sequence: processid is no longer a key (a specimen has a
#       row per marker), with its own index idx_processid. bold_row_hash is
#       (rid, processid, marker, h), rid being the bold_records rowid.
# The viewer still reads layout 1 (same columns, fewer rows); the update
# doesn't, and the Creator asks for a rebuild.
SCHEMA_VERSION    = 2
MIN_VIEWER_SCHEMA = 1

# Databases built before the version was recorded (Creator <= 1.2.0) already
# have the version-1 layout, so they keep working without a rebuild.
LEGACY_SCHEMA_VERSION = 1


def read_meta(conn):
    """META_TABLE as a dict. Empty if the table doesn't exist (older builds)."""
    try:
        rows = conn.execute(
            "SELECT key, value FROM {0}".format(META_TABLE)).fetchall()
    except sqlite3.Error:
        return {}
    return {k: v for k, v in rows}


def schema_version(conn):
    """Layout version of an open database."""
    value = read_meta(conn).get("schema_version")
    try:
        return int(value)
    except (TypeError, ValueError):
        return LEGACY_SCHEMA_VERSION


def schema_status(version):
    """'ok', 'too_old' (rebuild the database) or 'too_new' (update the program)."""
    if version < MIN_VIEWER_SCHEMA:
        return "too_old"
    if version > SCHEMA_VERSION:
        return "too_new"
    return "ok"
