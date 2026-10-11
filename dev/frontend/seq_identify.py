"""Identification of query sequences against the local database: phase 3 of
the viewer's sequence analysis (like BOLD's Identification Engine).

A search index per marker is built once, from the database:
  - COI-5P: one representative sequence per BIN, plus the sequences with no
    BIN. A BIN's sequences differ by ~2 % at most, so its representative
    stands for all of them; once a query matches a BIN, its members are
    fetched and compared one by one.
  - Any other marker: all its sequences (they are far fewer and have no BINs).
The index keeps a fixed sample (1 in SAMPLE) of the 12-mers of each indexed
sequence, as posting lists (k-mer -> sequences that hold it). The same
sampling applied to a query finds the candidates sharing most k-mers with it,
which are then aligned to it (seq_distance's aligner) for an exact identity.

Index files (numpy arrays, memory-mapped when read) live in one folder per
marker, with meta.json recording the database version they were built from:
a database rebuilt or updated since makes the index stale (row ids change).
"""

import json
import os
import re
import shutil
import time

import numpy as np

import seq_analysis
import seq_distance

INDEX_VERSION = 1
K = 12
# One k-mer in SAMPLE is indexed (chosen by a hash, the same for queries)
SAMPLE = 8
# K-mers held by more sequences than this are left out of a query: they are
# conserved across whole orders and only add noise (and time)
MAX_POSTINGS = 50_000
# A BIN's representative: the sequence closest to this length with no N
BARCODE_LENGTH = 658
# Representatives longer than this (full genes, genomes) are cut to it
MAX_REP_LENGTH = 1600
# Sequences shorter than this aren't indexed
MIN_INDEX_LENGTH = 100

# Per query: candidates from the index, BINs expanded, members per BIN
CANDIDATES = 40
BINS_EXPANDED = 3
MEMBERS_PER_BIN = 100
# Most queries in one run
MAX_QUERIES = 2000
# A hit must share at least this many comparable sites with the query, and
# at least half of it
MIN_HIT_OVERLAP = 100
# Hits within this identity of the best take part in the consensus
CONSENSUS_MARGIN = 0.01

DEFAULT_THRESHOLDS = {"species": 0.98, "genus": 0.95, "family": 0.90}

_CODES = np.full(256, 4, dtype=np.uint8)
for _i, _b in enumerate("ACGT"):
    _CODES[ord(_b)] = _i
_MASK = (1 << (2 * K)) - 1
_HASH = np.uint32(2654435761)


class Cancelled(Exception):
    pass


# ── K-mers ────────────────────────────────────────────────────────────────────

def sampled_kmers(seqs):
    """(sequence index, k-mer code) of the sampled k-mers of `seqs`, each
    pair once. Vectorised over the whole batch."""
    if not seqs:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    joined = "N".join(seqs) + "N"
    codes = _CODES[np.frombuffer(joined.encode("ascii", "replace"), dtype=np.uint8)]
    n = codes.size - K + 1
    if n <= 0:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    kmer = np.zeros(n, dtype=np.uint32)
    for j in range(K):
        kmer = (kmer << np.uint32(2)) | (codes[j:j + n] & np.uint8(3)).astype(np.uint32)
    invalid = np.concatenate(([0], np.cumsum(codes >= 4)))
    valid = (invalid[K:K + n] - invalid[:n]) == 0
    keep = valid & (((kmer * _HASH) >> np.uint32(29)) == 0)
    pos = np.flatnonzero(keep)
    starts = np.cumsum([0] + [len(s) + 1 for s in seqs[:-1]])
    seq_of = np.searchsorted(starts, pos, side="right") - 1
    key = np.unique((seq_of.astype(np.int64) << 32) | kmer[pos].astype(np.int64))
    return key >> 32, key & 0xFFFFFFFF


# ── Index ─────────────────────────────────────────────────────────────────────

def safe_name(marker):
    return re.sub(r"[^\w.-]+", "_", marker) or "_"


def index_dir(root, marker):
    return os.path.join(root, safe_name(marker.upper()))


def read_meta(root, marker):
    try:
        with open(os.path.join(index_dir(root, marker), "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        return meta if meta.get("version") == INDEX_VERSION else None
    except (OSError, ValueError):
        return None


def _rep_score(seq):
    """Higher is a better BIN representative: no Ns, then close to barcode length."""
    core = seq.strip("-N")
    return (not ("N" in core or "-" in core), -abs(len(core) - BARCODE_LENGTH))


def build_index(open_conn, marker, n_marker, table_rows, root, db_sig,
                progress=None, cancelled=None):
    """Builds the index of `marker` under `root`.

    open_conn: a callable returning a read-only connection (context manager).
    n_marker / table_rows: records of the marker and of the whole table, for
    the progress and to choose between an indexed query and a table scan.
    progress(phase, done, total) and cancelled() are optional callbacks.
    """
    progress = progress or (lambda *a: None)
    cancelled = cancelled or (lambda: False)
    by_bin = {}            # bin -> (score, rowid, seq)
    loose = []             # (rowid, seq) without BIN
    target = marker.upper()
    use_bins = target == "COI-5P"

    with open_conn() as conn:
        # Most of the table is COI-5P: reading it in order beats the index
        if n_marker > 0.3 * table_rows:
            cursor = conn.execute("SELECT rowid, col_marker_code, col_bin_uri, col_nuc FROM bold_records")
            total = table_rows
            filtered = True
        else:
            cursor = conn.execute(
                "SELECT rowid, col_marker_code, col_bin_uri, col_nuc FROM bold_records "
                "WHERE col_marker_code = ? COLLATE NOCASE", (marker,))
            total = n_marker
            filtered = False
        done = 0
        while True:
            rows = cursor.fetchmany(20000)
            if not rows:
                break
            for rowid, mk, bin_uri, nuc in rows:
                if filtered and (mk or "").upper() != target:
                    continue
                if not nuc or len(nuc) < MIN_INDEX_LENGTH:
                    continue
                if use_bins and bin_uri:
                    score = _rep_score(nuc)
                    best = by_bin.get(bin_uri)
                    if best is None or score > best[0]:
                        by_bin[bin_uri] = (score, rowid, nuc)
                else:
                    loose.append((rowid, nuc))
            done += len(rows)
            progress("read", done, total)
            if cancelled():
                raise Cancelled()

    entries_rowid, entries_bin, seqs = [], [], []
    for bin_uri, (_, rowid, nuc) in by_bin.items():
        entries_rowid.append(rowid)
        entries_bin.append(bin_uri)
        seqs.append(nuc)
    by_bin.clear()
    for rowid, nuc in loose:
        entries_rowid.append(rowid)
        entries_bin.append("")
        seqs.append(nuc)
    loose.clear()

    n = len(seqs)
    keys = []
    batch = 20000
    for start in range(0, n, batch):
        chunk = [seq_analysis.clean_sequence(s)[:MAX_REP_LENGTH] for s in seqs[start:start + batch]]
        seq_idx, kmers = sampled_kmers(chunk)
        keys.append((kmers << 32) | (seq_idx + start))
        progress("kmers", min(start + batch, n), n)
        if cancelled():
            raise Cancelled()
    seqs.clear()
    progress("sort", 0, 1)
    key = np.concatenate(keys) if keys else np.empty(0, np.int64)
    keys.clear()
    key.sort()
    kmer = (key >> 32).astype(np.int64)
    postings = (key & 0xFFFFFFFF).astype(np.int32)
    del key
    counts = np.bincount(kmer, minlength=1 << (2 * K))
    del kmer
    offsets = np.zeros(counts.size + 1, dtype=np.int64)
    np.cumsum(counts, out=offsets[1:])

    final = index_dir(root, marker)
    tmp = final + ".building"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    np.save(os.path.join(tmp, "offsets.npy"), offsets)
    np.save(os.path.join(tmp, "postings.npy"), postings)
    np.save(os.path.join(tmp, "rowids.npy"), np.array(entries_rowid, dtype=np.int64))
    np.save(os.path.join(tmp, "bins.npy"), np.array(entries_bin, dtype="S16"))
    meta = {
        "version": INDEX_VERSION, "marker": marker, "db": list(db_sig),
        "entries": n, "bins": sum(1 for b in entries_bin if b),
        "postings": int(postings.size), "k": K, "sample": SAMPLE,
        "built": time.strftime("%Y-%m-%d %H:%M"),
    }
    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    _CACHE.pop(final, None)
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    progress("done", 1, 1)
    return meta


_CACHE = {}


class Index:
    """A built index, its big arrays memory-mapped."""

    def __init__(self, folder, meta):
        self.meta = meta
        load = lambda name, **kw: np.load(os.path.join(folder, name), **kw)
        self.offsets = load("offsets.npy", mmap_mode="r")
        self.postings = load("postings.npy", mmap_mode="r")
        self.rowids = load("rowids.npy")
        self.bins = load("bins.npy")
        self.n = self.rowids.size

    def candidates(self, seq, top=CANDIDATES):
        """[(entry, shared k-mers)] best first, over both strands."""
        best = None
        for s in (seq, seq_analysis.reverse_complement(seq)):
            _, kmers = sampled_kmers([s])
            chunks = []
            for k in kmers:
                a, b = self.offsets[k], self.offsets[k + 1]
                if 0 < b - a <= MAX_POSTINGS:
                    chunks.append(self.postings[a:b])
            if not chunks:
                continue
            counts = np.bincount(np.concatenate(chunks), minlength=self.n)
            m = int(counts.max())
            if best is None or m > best[0]:
                best = (m, counts)
        if best is None or best[0] < 2:
            return []
        counts = best[1]
        take = min(top, int((counts >= 2).sum()))
        idx = np.argpartition(-counts, take - 1)[:take]
        idx = idx[np.argsort(-counts[idx])]
        return [(int(i), int(counts[i])) for i in idx if counts[i] >= 2]


def load_index(root, marker):
    folder = index_dir(root, marker)
    meta = read_meta(root, marker)
    if meta is None:
        return None
    cached = _CACHE.get(folder)
    if cached and cached.meta == meta:
        return cached
    index = Index(folder, meta)
    _CACHE[folder] = index
    return index


# ── Identification ────────────────────────────────────────────────────────────

_HIT_COLUMNS = ("rowid", "col_processid", "col_species", "col_genus", "col_family",
                "col_order", "col_class", "col_phylum", "col_bin_uri", "col_country_ocean",
                "col_nuc")
_HIT_KEYS = ("rowid", "processid", "species", "genus", "family", "order", "class",
             "phylum", "bin", "country", "seq")


def _fetch_rows(conn, sql, params):
    return [dict(zip(_HIT_KEYS, r)) for r in conn.execute(sql, params)]


def _score(query, hits):
    """Identity of each hit to the query, over the sites both have."""
    if not hits:
        return
    aligned = seq_distance.align_to_reference(
        [seq_analysis.clean_sequence(h["seq"])[:MAX_REP_LENGTH * 2] for h in hits], query)
    for h, a in zip(hits, aligned):
        if a["row"] is None:
            h["identity"], h["overlap"] = 0.0, 0
        else:
            h["identity"], h["overlap"] = seq_distance._identity(a["row"], query)


def _consensus(hits, thresholds):
    """(level, taxon, note) from the hits, best first."""
    if not hits:
        return "none", "", ""
    best = hits[0]["identity"]
    near = [h for h in hits if h["identity"] >= best - CONSENSUS_MARGIN]

    def agree(rank):
        values = {h[rank] for h in near if h.get(rank)}
        return values.pop() if len(values) == 1 else None, values

    levels = [("species", "species"), ("genus", "genus"), ("family", "family")]
    note = ""
    for level, rank in levels:
        if best < thresholds[level]:
            continue
        taxon, values = agree(rank)
        if taxon:
            return level, taxon, note
        if len(values) > 1 and not note:
            note = " / ".join(sorted(values)[:4]) + (" …" if len(values) > 4 else "")
    # Under the family threshold, or no agreement: the order (or class) of
    # the near hits, as a hint
    for rank in ("order", "class", "phylum"):
        taxon, _ = agree(rank)
        if taxon:
            return "none", taxon, note
    return "none", "", note


def identify_one(index, conn, query, marker, thresholds, max_hits):
    """Hits and assignment for one cleaned query sequence."""
    cands = index.candidates(query)
    if not cands:
        return [], ("none", "", "")
    rowids = [int(index.rowids[e]) for e, _ in cands]
    marks = ",".join("?" * len(rowids))
    hits = _fetch_rows(conn, f"SELECT {', '.join(_HIT_COLUMNS)} FROM bold_records "
                             f"WHERE rowid IN ({marks})", rowids)
    _score(query, hits)

    # The best BINs, then all (up to MEMBERS_PER_BIN) of their sequences:
    # the representative is only one of them
    seen = {h["rowid"] for h in hits}
    bins = []
    for h in sorted(hits, key=lambda h: -h["identity"]):
        if h["bin"] and h["bin"] not in bins:
            bins.append(h["bin"])
        if len(bins) == BINS_EXPANDED:
            break
    members = []
    for b in bins:
        rows = _fetch_rows(conn, f"SELECT {', '.join(_HIT_COLUMNS)} FROM bold_records "
                                 "WHERE col_bin_uri = ? COLLATE NOCASE AND col_marker_code = ? COLLATE NOCASE "
                                 "LIMIT ?", (b, marker, MEMBERS_PER_BIN))
        members.extend(r for r in rows if r["rowid"] not in seen)
        seen.update(r["rowid"] for r in rows)
    _score(query, members)
    hits.extend(members)

    # A hit must cover at least half the query: a short, conserved stretch
    # shared with a distant taxon says nothing about the identity
    min_overlap = min(len(query), max(MIN_HIT_OVERLAP, len(query) // 2))
    hits = [h for h in hits if h["overlap"] >= min_overlap]
    hits.sort(key=lambda h: (-h["identity"], -h["overlap"]))
    for h in hits:
        h.pop("seq", None)
    return hits[:max_hits], _consensus(hits, thresholds)


def identify(index, open_conn, queries, marker, thresholds, max_hits,
             progress=None, cancelled=None):
    """Identification of `queries` ([{id, seq}]). Returns one result per query."""
    progress = progress or (lambda *a: None)
    cancelled = cancelled or (lambda: False)
    results = []
    with open_conn() as conn:
        for i, q in enumerate(queries):
            if cancelled():
                raise Cancelled()
            seq = seq_analysis.clean_sequence(q["seq"])
            res = {"id": q["id"], "length": len(seq)}
            if len(seq) < seq_analysis.MIN_LENGTH:
                res.update(level="none", taxon="", note="", short=True, hits=[], best=None)
            else:
                hits, (level, taxon, note) = identify_one(index, conn, seq, marker, thresholds, max_hits)
                best = hits[0] if hits else None
                res.update(level=level, taxon=taxon, note=note, hits=hits,
                           best=round(best["identity"], 4) if best else None,
                           best_overlap=best["overlap"] if best else None,
                           best_bin=best["bin"] if best else "",
                           best_id=best["processid"] if best else "")
                for h in hits:
                    h["identity"] = round(h["identity"], 4)
                    h.pop("rowid", None)
            results.append(res)
            progress("identify", i + 1, len(queries))
    return results
