# Changelog

All notable changes to this project are documented in this file.

This log starts at `1.1.0`. Changes before that are only in the git history
(`git log`), since versions weren't tracked release-by-release until now.

## [1.1.1]

### Fixed
- Full-text search and advanced field search both failed to match anything
  when the user typed accented characters (e.g. searching "México" or
  "Perú" returned zero results). `bold_db_creator.py` strips accents from
  every text field on import, so the table never has an accented value to
  compare against. Both search paths now strip accents from the user's
  input the same way (`_strip_accents`), matching what Batch Search already
  did.

## [1.1.0]

### Added
- Case-sensitive option for full-text search. The FTS index uses a trigram
  tokenizer that's inherently case-insensitive, so this is implemented as a
  cheap post-filter (`instr()`, case-sensitive in SQLite) applied to the
  already-narrow `MATCH` results.

### Fixed
- FASTA/CSV/batch export threads could hang indefinitely at 0% progress.
  They read the UI language via Flask's `request` object, but ran on plain
  `threading.Thread`s with no request context, so the very first status
  update raised `RuntimeError` and silently killed the thread.
- The FASTA/CSV export loading-dialog titles were hardcoded in Spanish and
  ignored the selected UI language.

## [1.0.0]

Initial tagged release.
