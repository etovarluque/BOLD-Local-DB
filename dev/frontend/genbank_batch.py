#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GenBank Batch Search — ported from BarcodeSuite (_utilities/genbank_batch.py).

Batch-searches NCBI GenBank (nucleotide) with up to two crossed criteria: for
every term of Criterion 1 (optionally crossed with Criterion 2) it runs an
Entrez esearch to get the record count, then optionally efetches the matching
INSDSeq records to build a metadata table and/or a FASTA file.

Same logic as the BarcodeSuite worker, without Qt: server.py runs
GenbankBatchJob.run() in a background thread and polls the `status` dict it
updates. Differences from the desktop version, imposed by the browser:
  - Results are packed into one ZIP (the browser downloads it) instead of
    being written to a folder the user picks.
  - Metadata comes out as TSV, like the rest of this viewer's exports
    (all_results.tsv, per-term TSVs), instead of XLSX: no extra dependency.
  - A failed request is reported as such (log + "ERROR" in the counts CSV)
    instead of silently counting as 0 records.
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

# NCBI search fields offered in the UI. The value is the Entrez field tag,
# used verbatim in the query: `term[Organism]`.
FIELD_OPTIONS = [
    "Organism", "Country", "Gene", "Title",
    "All Fields", "Accession", "Author", "Journal",
]

# Columns offered for the customizable summary report: per term, how many
# distinct values were found for a column, and/or which ones. Keyed by the
# field names produced by GenbankBatchJob._iter_records().
REPORT_COLUMNS = [
    ("marker",   "Marker/Gene"),
    ("moltype",  "Moltype"),
    ("division", "Division"),
    ("country",  "Country"),
]

RETMAX_MIN, RETMAX_MAX, RETMAX_DEFAULT = 1, 10000, 500

META_HEADERS = [
    "Accession", "Term1", "Term2", "Organism", "Marker", "Definition",
    "Length", "Moltype", "Division", "Country", "Isolate", "Specimen_Voucher",
    "Collection_Date", "Lat_Lon", "Taxon_ID", "Create_Date", "Update_Date",
    "Taxonomy",
]


class GenbankCancelled(Exception):
    pass


def normalize_terms(raw_terms):
    """Strip/collapse whitespace, turn '_' into spaces, dedupe case-insensitively."""
    seen = {}
    for raw in raw_terms or []:
        if raw is None:
            continue
        term = re.sub(r"\s+", " ", str(raw).replace("_", " ")).strip()
        if not term:
            continue
        seen.setdefault(term.lower(), term)
    return list(seen.values())


def build_pairs(terms1, terms2):
    """A single term2 is a constant filter; several are crossed with every term1."""
    if not terms2:
        return [(t1, None) for t1 in terms1]
    if len(terms2) == 1:
        return [(t1, terms2[0]) for t1 in terms1]
    return [(t1, t2) for t1 in terms1 for t2 in terms2]


def _safe_name(s, max_len=80):
    s = re.sub(r"[^\w\s-]", "", str(s)).strip().replace(" ", "_")
    return s[:max_len] or "unknown"


def _unique_name(s, used):
    """_safe_name(), plus a numeric suffix if another term already took it
    ('A.b' and 'Ab' both become 'Ab'), compared case-insensitively because
    Windows/macOS file systems are."""
    base = _safe_name(s)
    name, n = base, 2
    while name.lower() in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name.lower())
    return name


class GenbankBatchJob:
    _NCBI_BASE    = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    _TOOL         = "BOLD-Local-DB"
    _MAX_RETRY    = 5
    _FETCH_CHUNK  = 200    # records per efetch call
    _LOG_KEEP     = 5000   # log lines kept in memory for the browser

    def __init__(self, cfg, workdir, zip_path, status, tr):
        """
        cfg      — validated request (see server.start_genbank_batch).
        workdir  — scratch folder for the output files; removed at the end.
        zip_path — where the final ZIP goes.
        status   — dict shared with the status route; updated in place.
                   Setting status['cancel'] = True stops the job.
        tr       — server's t(): message templates in the active language.
        """
        self.cfg      = cfg
        self.workdir  = workdir
        self.zip_path = zip_path
        self.status   = status
        self.tr       = tr
        self._rl_lock = threading.Lock()
        self._rl_next = 0.0
        self._failed_chunks = 0   # efetch pages lost in the current query
        # NCBI allows ~10 req/s with an API key, ~3 req/s without — stay under both.
        self._rate = 9.0 if cfg.get("api_key") else 2.5

    # ── Status helpers ───────────────────────────────────────────────────

    def _stopped(self):
        return bool(self.status.get("cancel"))

    def _log(self, line):
        log = self.status.setdefault("log", [])
        log.append(line)
        if len(log) > self._LOG_KEEP:
            drop = len(log) - self._LOG_KEEP
            del log[:drop]
            self.status["log_offset"] = self.status.get("log_offset", 0) + drop

    def _set_progress(self, units, total_units):
        self.status["progress"] = round(min(99.0, units / total_units * 100), 1) if total_units else 0

    # ── HTTP helpers (rate-limited, retrying) ────────────────────────────

    def _rate_acquire(self):
        interval = 1.0 / self._rate
        with self._rl_lock:
            wait = self._rl_next - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._rl_next = time.monotonic() + interval

    def _interruptible_sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stopped():
                return
            time.sleep(0.5)

    def _http_get(self, url, params, timeout=90):
        """Returns the response body, or None if every attempt failed."""
        params = dict(params, tool=self._TOOL)
        if self.cfg.get("api_key"):
            params["api_key"] = self.cfg["api_key"]
        full_url = url + "?" + urllib.parse.urlencode(params)
        for attempt in range(self._MAX_RETRY):
            if self._stopped():
                return None
            self._rate_acquire()
            if self._stopped():
                return None
            try:
                with urllib.request.urlopen(full_url, timeout=timeout) as resp:
                    if resp.status == 200:
                        return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self._interruptible_sleep(min(30 * (attempt + 1), 120))
                elif 500 <= e.code < 600:
                    self._interruptible_sleep(min(10 * (attempt + 1), 60))
                else:
                    return None
            except Exception:
                self._interruptible_sleep(2)
        return None

    # ── Entrez calls ─────────────────────────────────────────────────────

    def _esearch(self, query):
        """(count, webenv, query_key), or None if the request failed."""
        xml_text = self._http_get(
            f"{self._NCBI_BASE}esearch.fcgi",
            {"db": "nucleotide", "term": query, "usehistory": "y", "retmax": 0},
        )
        if not xml_text:
            return None
        try:
            root = ET.fromstring(xml_text)
            count = int(root.findtext("Count", "0") or "0")
        except Exception:
            return None
        return count, root.findtext("WebEnv", ""), root.findtext("QueryKey", "")

    @staticmethod
    def _feature_qualifiers(feat):
        """Return {qualifier_name: value} for a single INSDFeature."""
        quals = {}
        for qual in feat.findall("./INSDFeature_quals/INSDQualifier"):
            name = qual.findtext("INSDQualifier_name")
            if name and name not in quals:
                quals[name] = (qual.findtext("INSDQualifier_value") or "").strip()
        return quals

    @classmethod
    def _source_qualifiers(cls, seq_el):
        """Qualifiers of the 'source' feature (present on every INSDSeq record):
        country, isolate, specimen_voucher, collection_date, lat_lon, db_xref, etc."""
        for feat in seq_el.findall("./INSDSeq_feature-table/INSDFeature"):
            if feat.findtext("INSDFeature_key") == "source":
                return cls._feature_qualifiers(feat)
        return {}

    @classmethod
    def _record_marker(cls, seq_el):
        """Best-effort marker/gene name: first non-source feature's /gene, else /product.

        GenBank has no dedicated "marker" field like BOLD's col_marker_code — this
        is free text from whichever feature (gene/CDS/rRNA/tRNA/...) the depositor
        annotated, so naming is inconsistent across records (COI/COX1/CO1/cox1...).
        """
        for feat in seq_el.findall("./INSDSeq_feature-table/INSDFeature"):
            if feat.findtext("INSDFeature_key") == "source":
                continue
            quals = cls._feature_qualifiers(feat)
            marker = quals.get("gene") or quals.get("product")
            if marker:
                return marker
        return ""

    def _iter_records(self, webenv, key, n_records, on_chunk=None):
        """Page through the esearch history set, yielding one dict per record.

        Calls `on_chunk(fetched, n_records)` after each page, so progress also
        moves within a single large fetch. Pages that fail are counted in
        self._failed_chunks.
        """
        fetched = 0
        while fetched < n_records:
            if self._stopped():
                return
            n = min(self._FETCH_CHUNK, n_records - fetched)
            xml_text = self._http_get(
                f"{self._NCBI_BASE}efetch.fcgi",
                {
                    "db": "nucleotide", "WebEnv": webenv, "query_key": key,
                    "retstart": fetched, "retmax": n,
                    "rettype": "gbc", "retmode": "xml",
                },
                timeout=120,
            )
            fetched += n
            if on_chunk:
                on_chunk(fetched, n_records)
            if not xml_text:
                if not self._stopped():
                    self._failed_chunks += 1
                continue
            try:
                root = ET.fromstring(xml_text)
            except Exception:
                self._failed_chunks += 1
                continue
            for seq_el in root.findall("INSDSeq"):
                src = self._source_qualifiers(seq_el)
                taxon_id = ""
                dbxref = src.get("db_xref", "")
                if dbxref.startswith("taxon:"):
                    taxon_id = dbxref.split(":", 1)[1]

                yield {
                    "accession": (seq_el.findtext("INSDSeq_primary-accession")
                                  or seq_el.findtext("INSDSeq_locus") or ""),
                    "organism":  (seq_el.findtext("INSDSeq_organism") or "").strip(),
                    "sequence":  (seq_el.findtext("INSDSeq_sequence") or "").strip(),
                    "marker":    self._record_marker(seq_el),
                    "definition": (seq_el.findtext("INSDSeq_definition") or "").strip(),
                    "length":    seq_el.findtext("INSDSeq_length") or "",
                    "moltype":   seq_el.findtext("INSDSeq_moltype") or "",
                    "division":  (seq_el.findtext("INSDSeq_division") or "").strip(),
                    "create_date": seq_el.findtext("INSDSeq_create-date") or "",
                    "update_date": seq_el.findtext("INSDSeq_update-date") or "",
                    "country":   src.get("country", ""),
                    "isolate":   src.get("isolate", ""),
                    "specimen_voucher": src.get("specimen_voucher", ""),
                    "collection_date":  src.get("collection_date", ""),
                    "lat_lon":   src.get("lat_lon", ""),
                    "taxon_id":  taxon_id,
                    "taxonomy":  (seq_el.findtext("INSDSeq_taxonomy") or "").strip(),
                }

    def _fetch_records(self, webenv, key, n_records, t1, t2, meta_w, fasta_f,
                       want_metadata, want_fasta, tally, used_names, on_chunk):
        """Fetch records for one (t1, t2) query, writing them into the compiled
        outputs (`meta_w`/`fasta_f`) *and* into a dedicated
        `individual_results/<term>/` folder with that term's own files. If
        `tally` is given ({column: Counter()}), also tallies distinct values
        per column for the summary report.

        Returns the number of records written.
        """
        ind_meta_f = ind_meta_w = ind_fasta_f = None
        if want_metadata or want_fasta:
            safe = _unique_name(t1 if not t2 else f"{t1}_{t2}", used_names)
            ind_dir = os.path.join(self.workdir, "individual_results", safe)
            os.makedirs(ind_dir, exist_ok=True)
            if want_metadata:
                ind_meta_f = open(os.path.join(ind_dir, f"metadata_{safe}.tsv"),
                                  "w", newline="", encoding="utf-8-sig")
                ind_meta_w = csv.writer(ind_meta_f, delimiter="\t")
                ind_meta_w.writerow(META_HEADERS)
            if want_fasta:
                ind_fasta_f = open(os.path.join(ind_dir, f"sequences_{safe}.fasta"),
                                   "w", encoding="utf-8")

        n_written = 0
        try:
            for rec in self._iter_records(webenv, key, n_records, on_chunk=on_chunk):
                n_written += 1
                if tally:
                    for col, counter in tally.items():
                        val = rec.get(col)
                        if val:
                            counter[val] += 1

                if want_metadata:
                    values = [
                        rec["accession"], t1, t2 or "", rec["organism"], rec["marker"],
                        rec["definition"], rec["length"], rec["moltype"], rec["division"],
                        rec["country"], rec["isolate"], rec["specimen_voucher"],
                        rec["collection_date"], rec["lat_lon"], rec["taxon_id"],
                        rec["create_date"], rec["update_date"], rec["taxonomy"],
                    ]
                    meta_w.writerow(values)
                    ind_meta_w.writerow(values)

                if want_fasta and rec["sequence"]:
                    header_parts = [
                        rec["accession"], rec["organism"].replace(" ", "_"),
                        rec["marker"].replace(" ", "_"), t1.replace(" ", "_"),
                    ]
                    if t2:
                        header_parts.append(t2.replace(" ", "_"))
                    entry = f">{'|'.join(p for p in header_parts if p)}\n{rec['sequence'].upper()}\n"
                    fasta_f.write(entry)
                    ind_fasta_f.write(entry)
        finally:
            if ind_meta_f:
                ind_meta_f.close()
            if ind_fasta_f:
                ind_fasta_f.close()
        return n_written

    # ── Customizable summary report (per-term distinct-value metrics) ─────

    _REPORT_LABELS = dict(REPORT_COLUMNS)

    def _metric_headers(self, specs):
        headers = []
        for spec in specs:
            label = self._REPORT_LABELS.get(spec["column"], spec["column"])
            if spec["count"]:
                headers.append(f"# {label}")
            if spec["list"]:
                headers.append(label)
        return headers

    @staticmethod
    def _build_metric_cells(specs, tally, max_list):
        cells = []
        for spec in specs:
            counter = tally.get(spec["column"])
            items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])) if counter else []
            n_distinct = len(items)
            if spec["count"]:
                cells.append(n_distinct)
            if spec["list"]:
                shown = items[:max_list] if max_list > 0 else items
                if spec.get("with_counts"):
                    text = "; ".join(f"{v} ({n})" for v, n in shown)
                else:
                    text = "; ".join(v for v, n in shown)
                rest = n_distinct - len(shown)
                if rest > 0:
                    text = f"{text}; … +{rest} more" if text else f"… +{rest} more"
                cells.append(text)
        return cells

    # ── Entry point ──────────────────────────────────────────────────────

    def run(self):
        """Runs the whole batch and packs the ZIP. Returns a summary dict:
        {'queries_done', 'queries_total', 'records', 'failed'}; raises
        GenbankCancelled if stopped before any query finished."""
        cfg = self.cfg
        os.makedirs(self.workdir, exist_ok=True)

        field1_tag = cfg["field1_tag"]
        terms1 = normalize_terms(cfg["field1_terms"])
        terms2 = normalize_terms(cfg.get("field2_terms")) if cfg.get("field2_enabled") else []
        field2_tag = cfg.get("field2_tag") if terms2 else None

        pairs = build_pairs(terms1, terms2)
        total = len(pairs)
        want_metadata = bool(cfg.get("want_metadata"))
        want_fasta    = bool(cfg.get("want_fasta"))
        retmax        = int(cfg.get("retmax", RETMAX_DEFAULT))
        metric_specs  = cfg.get("report_metrics") or []
        max_list      = int(cfg.get("report_max_list") or 0)
        want_report   = bool(metric_specs)

        counts_f = open(os.path.join(self.workdir, "genbank_counts.csv"),
                        "w", newline="", encoding="utf-8-sig")
        counts_w = csv.writer(counts_f)
        counts_w.writerow(
            ["term1", "field1", "term2", "field2", "query", "record_count"]
            + self._metric_headers(metric_specs)
        )

        meta_f = meta_w = fasta_f = None
        if want_metadata:
            meta_f = open(os.path.join(self.workdir, "genbank_metadata.tsv"),
                          "w", newline="", encoding="utf-8-sig")
            meta_w = csv.writer(meta_f, delimiter="\t")
            meta_w.writerow(META_HEADERS)
        if want_fasta:
            fasta_f = open(os.path.join(self.workdir, "genbank_sequences.fasta"),
                           "w", encoding="utf-8")

        res = 1000  # sub-steps per query, so a large fetch also moves the bar
        total_units = total * res
        used_names = set()
        done = 0
        records = 0
        failed = 0
        start_time = time.time()
        try:
            for i, (t1, t2) in enumerate(pairs, 1):
                if self._stopped():
                    break
                query = f"{t1}[{field1_tag}]"
                if t2:
                    query += f" AND {t2}[{field2_tag}]"

                elapsed = time.time() - start_time
                rate = (i - 1) / elapsed if elapsed > 0 and i > 1 else 0
                eta = (total - i + 1) / rate if rate > 0 else 0
                eta_txt = f"  —  ETA {int(eta // 60)}m {int(eta % 60)}s" if rate > 0 else ""
                self.status["message"] = f"({i:,}/{total:,})  {query}{eta_txt}"
                base_units = (i - 1) * res
                self._set_progress(base_units, total_units)

                result = self._esearch(query)
                if self._stopped():
                    break
                if result is None:
                    failed += 1
                    counts_w.writerow([t1, field1_tag, t2 or "", field2_tag or "", query, "ERROR"]
                                      + [""] * len(self._metric_headers(metric_specs)))
                    counts_f.flush()
                    self._log(self.tr("genbank_log_failed", query=query))
                    done = i
                    continue
                count, webenv, key = result

                tally = {spec["column"]: Counter() for spec in metric_specs}
                self._failed_chunks = 0
                n_written = 0
                if count and (want_metadata or want_fasta or want_report) and webenv and key:
                    n_fetch = min(count, retmax)

                    def _on_chunk(fetched, n_records, base=base_units):
                        frac = fetched / n_records if n_records else 1.0
                        self._set_progress(base + int(frac * res), total_units)

                    n_written = self._fetch_records(
                        webenv, key, n_fetch, t1, t2, meta_w, fasta_f,
                        want_metadata, want_fasta, tally if want_report else None,
                        used_names, _on_chunk,
                    )
                    records += n_written
                if self._stopped():
                    # The current query didn't finish: leave it out of the
                    # counts CSV rather than write a partial tally.
                    break

                self._set_progress(i * res, total_units)
                counts_w.writerow(
                    [t1, field1_tag, t2 or "", field2_tag or "", query, count]
                    + self._build_metric_cells(metric_specs, tally, max_list)
                )
                counts_f.flush()
                line = self.tr("genbank_log_count", query=query, n=f"{count:,}")
                if count > retmax and (want_metadata or want_fasta or want_report):
                    line += self.tr("genbank_log_capped", n=f"{retmax:,}")
                if self._failed_chunks:
                    line += self.tr("genbank_log_chunks_failed", n=self._failed_chunks)
                self._log(line)
                done = i
        finally:
            counts_f.close()
            if meta_f:
                meta_f.close()
            if fasta_f:
                fasta_f.close()

        if done == 0 and self._stopped():
            raise GenbankCancelled()

        self.status["message"] = self.tr("genbank_packing")
        self._pack_zip()
        return {"queries_done": done, "queries_total": total,
                "records": records, "failed": failed}

    def _pack_zip(self):
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for dirpath, _, filenames in os.walk(self.workdir):
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    zf.write(full, os.path.relpath(full, self.workdir).replace(os.sep, "/"))

    def cleanup(self):
        shutil.rmtree(self.workdir, ignore_errors=True)
