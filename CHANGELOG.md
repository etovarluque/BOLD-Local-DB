# Changelog

All notable changes to this project are documented in this file.

This log starts at `1.1.0`. Changes before that are only in the git history
(`git log`), since versions weren't tracked release-by-release until now.

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
