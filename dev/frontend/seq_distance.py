"""Alignment, genetic distances, barcode gap and NJ tree: phase 2 of the
viewer's "Sequence analysis" tab.

Sequences are aligned to a reference sequence picked from the input, as BOLD
does against its COI profile: each one is placed on the reference by the
k-mers they share and, only where that shows an indel, aligned with gaps
within a narrow band. Bases a sequence has beyond the reference, or inserted
relative to it, are left out, so every sequence ends up in the reference's
columns. That is sound for protein-coding barcodes, which rarely have indels;
for non-coding markers (ITS...) it's an approximation, and the page says so.

Distances are computed with matrix products over one-hot encoded sequences,
so even thousands of sequences take seconds. Pairs that share fewer than
MIN_OVERLAP comparable sites get no distance.

Uses numpy (installed by server.py like its other dependencies).
"""

import math
import re
from collections import Counter, defaultdict

import numpy as np

import seq_analysis
import seq_delimit

# ── Limits ────────────────────────────────────────────────────────────────────

# Most sequences read for a distance analysis, before the per-species
# subsample brings them under MAX_DISTANCE_SEQUENCES
MAX_INPUT_SEQUENCES = 100_000
# Most sequences in a distance analysis: the matrix grows with the square
MAX_DISTANCE_SEQUENCES = 5000
# Most sequences in a tree: neighbour joining grows with the cube, and a
# bigger tree can't be read on screen
MAX_TREE_SEQUENCES = 2000
# Non-coding markers align worse and more slowly (most need gaps)
MAX_NONCODING_SEQUENCES = 1000
# Shortest sequence aligned, in bp
MIN_ALIGN_LENGTH = 100
# Fewest comparable sites (both sequences with A, C, G or T) for a distance
MIN_OVERLAP = 100
# Most sequences in a species delimitation (ASAP and ABGD), per group
MAX_DELIMIT_SEQUENCES = 2000
# The full distance matrix goes into the ZIP up to this many sequences
# (beyond it the CSV would weigh hundreds of MB)
MAX_MATRIX_EXPORT = 2000


class AnalysisLimit(ValueError):
    """The input is too big (or too small) for the analysis requested."""

    def __init__(self, kind, n, limit):
        super().__init__(kind)
        self.kind, self.n, self.limit = kind, n, limit


# ── Alignment to a reference ──────────────────────────────────────────────────

KMER = 8
# Shared k-mers needed to place a sequence on the reference at all
MIN_VOTES = 3
# Extra band around the diagonals found, and widest band tried, in bp
BAND_MARGIN = 12
MAX_BAND = 90

# An ungapped placement is checked in windows of this many comparable sites;
# one under this identity means the sequence is shifted from there on (an
# indel), while the rest of it still matches
WINDOW = 40
WINDOW_MIN_IDENTITY = 0.5
# ...as long as some other window shows the placement is right
WINDOW_ANCHOR_IDENTITY = 0.65

MATCH, MISMATCH, GAP_OPEN, GAP_EXTEND = 2, -3, -10, -2
_NEG = -10 ** 9


def _pair_score(a, b):
    if a not in "ACGT" or b not in "ACGT":
        return 0
    return MATCH if a == b else MISMATCH


_SCORE = {(a, b): _pair_score(a, b)
          for a in "ACGTNRYKMSWBDHV" for b in "ACGTNRYKMSWBDHV"}


def choose_reference(seqs):
    """The sequence the others are aligned to.

    The longest clean sequence (only A, C, G, T) no longer than the 90th
    percentile of lengths: long enough to cover most of the others, without
    picking a rare full-length gene among barcodes.
    """
    clean = [s for s in seqs if not set(s) - set("ACGT")] or list(seqs)
    lengths = sorted(len(s) for s in clean)
    cap = lengths[int(0.9 * (len(lengths) - 1))]
    return max((s for s in clean if len(s) <= cap), key=len)


def _kmer_index(ref):
    index = defaultdict(list)
    for i in range(len(ref) - KMER + 1):
        kmer = ref[i:i + KMER]
        if "N" not in kmer:
            index[kmer].append(i)
    return index


def _diagonal_votes(seq, index):
    """How many k-mers support each diagonal (ref position - seq position)."""
    votes = Counter()
    for i in range(len(seq) - KMER + 1):
        hits = index.get(seq[i:i + KMER])
        if hits:
            for p in hits:
                votes[p - i] += 1
    return votes


def _ungapped(seq, d, m):
    """`seq` laid on the reference columns at diagonal `d`, with no gaps."""
    start_ref, start_seq = max(0, d), max(0, -d)
    length = max(0, min(len(seq) - start_seq, m - start_ref))
    return ("-" * start_ref + seq[start_seq:start_seq + length]
            + "-" * (m - start_ref - length))


def _looks_shifted(row, ref):
    """Whether an ungapped placement loses the reference somewhere: one
    window matches well while another is down to chance. (The identity of the
    whole sequence says nothing here: in rRNA genes, full of indels, an
    ungapped placement is lost after the first one and the overall identity
    drops to that of unrelated sequences.)"""
    same = [x == y for x, y in zip(row, ref) if x in "ACGT" and y in "ACGT"]
    if len(same) < 2 * WINDOW:
        return False
    windows = [sum(same[i:i + WINDOW]) / WINDOW
               for i in range(0, len(same) - WINDOW + 1, WINDOW // 2)]
    return min(windows) < WINDOW_MIN_IDENTITY and max(windows) >= WINDOW_ANCHOR_IDENTITY


def _banded(seq, ref, lo, hi):
    """Gapped alignment of `seq` to `ref` within diagonals lo..hi.

    Affine gaps (Gotoh); free end gaps on both sequences, since barcodes are
    fragments of different extent. Returns (row over the reference columns,
    bases inserted relative to the reference).
    """
    n, m = len(seq), len(ref)
    width = hi - lo + 1
    # Per row i, cell k is column j = i + lo + k. H: best score; E: ends in a
    # gap in seq (consumes ref); F: ends in a gap in ref (consumes seq).
    H = [[_NEG] * width for _ in range(n + 1)]
    E = [[_NEG] * width for _ in range(n + 1)]
    F = [[_NEG] * width for _ in range(n + 1)]
    # Traceback: H from 0 start / 1 diagonal / 2 E / 3 F; E and F: 0 opened, 1 extended
    TH = [bytearray(width) for _ in range(n + 1)]
    TE = [bytearray(width) for _ in range(n + 1)]
    TF = [bytearray(width) for _ in range(n + 1)]

    for k in range(width):
        if 0 <= lo + k <= m:
            H[0][k] = 0

    for i in range(1, n + 1):
        si = seq[i - 1]
        Hp, Fp = H[i - 1], F[i - 1]
        Hi, Ei, Fi, THi, TEi, TFi = H[i], E[i], F[i], TH[i], TE[i], TF[i]
        for k in range(width):
            j = i + lo + k
            if j < 0 or j > m:
                continue
            if j == 0:
                Hi[k] = 0          # the sequence's start hangs before the reference
                continue
            best, how = _NEG, 0
            if Hp[k] > _NEG:
                best, how = Hp[k] + _SCORE[(si, ref[j - 1])], 1
            if k > 0 and Hi[k - 1] > _NEG:
                opened, extended = Hi[k - 1] + GAP_OPEN, Ei[k - 1] + GAP_EXTEND
                if opened >= extended:
                    Ei[k] = opened
                else:
                    Ei[k], TEi[k] = extended, 1
                if Ei[k] > best:
                    best, how = Ei[k], 2
            if k + 1 < width and Hp[k + 1] > _NEG:
                opened, extended = Hp[k + 1] + GAP_OPEN, Fp[k + 1] + GAP_EXTEND
                if opened >= extended:
                    Fi[k] = opened
                else:
                    Fi[k], TFi[k] = extended, 1
                if Fi[k] > best:
                    best, how = Fi[k], 3
            Hi[k], THi[k] = best, how

    # Best end: last row (seq used up) or last column (ref used up)
    end, end_score = None, _NEG
    for k in range(width):
        j = n + lo + k
        if 0 <= j <= m and H[n][k] > end_score:
            end, end_score = (n, k), H[n][k]
    for i in range(n + 1):
        k = m - i - lo
        if 0 <= k < width and H[i][k] > end_score:
            end, end_score = (i, k), H[i][k]

    row = ["-"] * m
    inserted = 0
    i, k = end
    state = "H"
    while i > 0 and i + lo + k > 0:
        j = i + lo + k
        if state == "H":
            how = TH[i][k]
            if how == 0:
                break
            if how == 1:
                row[j - 1] = seq[i - 1]
                i -= 1
            else:
                state = "E" if how == 2 else "F"
        elif state == "E":           # ref base j against a gap
            extended = TE[i][k]
            k -= 1
            state = "E" if extended else "H"
        else:                        # seq base i inserted
            extended = TF[i][k]
            inserted += 1
            i -= 1
            k += 1
            state = "F" if extended else "H"
    return "".join(row), inserted


def align_to_reference(seqs, ref):
    """Rows of `seqs` over the columns of `ref`.

    Returns one dict per sequence: row (None when it couldn't be placed),
    reversed (placed on its reverse complement), gapped (needed the gapped
    alignment), inserted (bases left out as insertions).
    """
    index = _kmer_index(ref)
    m = len(ref)
    out = []
    for seq in seqs:
        best = None
        for reverse, s in ((False, seq), (True, seq_analysis.reverse_complement(seq))):
            votes = _diagonal_votes(s, index)
            if votes:
                d, c = votes.most_common(1)[0]
                if best is None or c > best[2]:
                    best = (reverse, s, c, d, votes)
        if best is None or best[2] < MIN_VOTES:
            out.append({"row": None, "reversed": False, "gapped": False, "inserted": 0})
            continue
        reverse, s, c1, d1, votes = best
        row = _ungapped(s, d1, m)
        if not _looks_shifted(row, ref):
            out.append({"row": row, "reversed": reverse, "gapped": False, "inserted": 0})
            continue
        # An indel moves the sequence from one diagonal to another: the band
        # spans the supported diagonals near the best one
        near = [d for d, c in votes.items()
                if d != d1 and abs(d - d1) <= MAX_BAND and c >= MIN_VOTES]
        lo = min(near + [d1]) - BAND_MARGIN
        hi = max(near + [d1]) + BAND_MARGIN
        row, inserted = _banded(s, ref, lo, hi)
        out.append({"row": row, "reversed": reverse, "gapped": True, "inserted": inserted})
    return out


# A sequence joins a group when it aligns to the group's reference with at
# least this identity over at least MIN_GROUP_SITES comparable sites; below
# that it isn't the same gene, or is too far for a meaningful alignment
MIN_GROUP_IDENTITY = 0.6
MIN_GROUP_SITES = 50
# Most alignment groups, and fewest sequences a group needs
MAX_GROUPS = 6
MIN_GROUP_SIZE = 3


def _identity(row, ref):
    """Identity of an aligned row to the reference, over comparable sites,
    and how many sites that is."""
    same = total = 0
    for x, y in zip(row, ref):
        if x in "ACGT" and y in "ACGT":
            total += 1
            same += x == y
    return (same / total if total else 0.0), total


def align_groups(seqs):
    """Sequences split into groups that align to a common reference.

    One marker name can hold unrelated genes (BOLD's "16S" covers bacterial
    16S rRNA and the mitochondrial 16S of animals), or taxa too far apart to
    align. Sequences that don't align to the first reference get a reference
    of their own, up to MAX_GROUPS groups. Distances are only computed within
    a group.

    Returns (groups, unplaced): each group {ref, ref_index, members:
    [(index, aligned)]}, largest first; unplaced, the indices in no group.
    """
    remaining = list(range(len(seqs)))
    groups, unplaced = [], []
    attempts = 0
    while remaining and len(groups) < MAX_GROUPS and attempts < 3 * MAX_GROUPS:
        attempts += 1
        pool = [seqs[i] for i in remaining]
        ref = choose_reference(pool)
        ref_index = remaining[pool.index(ref)]
        aligned = align_to_reference(pool, ref)
        members, rest = [], []
        for i, a in zip(remaining, aligned):
            ok = False
            if a["row"] is not None:
                identity, sites = _identity(a["row"], ref)
                ok = identity >= MIN_GROUP_IDENTITY and sites >= MIN_GROUP_SITES
            if ok:
                members.append((i, a))
            else:
                rest.append(i)
        if len(members) < MIN_GROUP_SIZE:
            # The reference found too few relatives: they stay out
            unplaced.extend(i for i, _ in members)
        else:
            groups.append({"ref": ref, "ref_index": ref_index, "members": members})
        remaining = rest
    unplaced.extend(remaining)
    groups.sort(key=lambda g: -len(g["members"]))
    return groups, unplaced


# ── Distances ─────────────────────────────────────────────────────────────────

_CODES = np.full(256, 4, dtype=np.uint8)
for _i, _b in enumerate("ACGT"):
    _CODES[ord(_b)] = _i


def encode(rows):
    """Aligned rows (same length) as an n × m array: 0-3 for ACGT, 4 otherwise."""
    m = len(rows[0])
    raw = np.frombuffer("".join(rows).encode("ascii"), dtype=np.uint8).reshape(len(rows), m)
    return _CODES[raw]


def distance_matrix(codes, model="k2p", block=512):
    """Pairwise distances (fractions), NaN where two sequences share fewer
    than MIN_OVERLAP comparable sites or the model saturates.

    model: "p" (proportion of differing sites), "jc" (Jukes-Cantor 1969:
    equal base frequencies and one substitution rate) or "k2p" (Kimura
    2-parameter: transitions and transversions at different rates).

    Gaps, Ns and ambiguities are left out pair by pair (pairwise deletion).
    Counts come from matrix products of one-hot encodings: identical sites,
    sites in the same purine/pyrimidine class (identical + transitions), and
    comparable sites.
    """
    n = codes.shape[0]
    onehot = [(codes == b).astype(np.float32) for b in range(4)]
    purine = onehot[0] + onehot[2]
    pyrimidine = onehot[1] + onehot[3]
    valid = purine + pyrimidine
    D = np.empty((n, n), dtype=np.float32)
    for start in range(0, n, block):
        sl = slice(start, min(start + block, n))
        ident = sum(onehot[b][sl] @ onehot[b].T for b in range(4))
        sites = valid[sl] @ valid.T
        with np.errstate(divide="ignore", invalid="ignore"):
            if model == "p":
                d = (sites - ident) / sites
            elif model == "jc":
                p = (sites - ident) / sites
                a = 1 - 4 * p / 3
                d = -0.75 * np.log(a)
                d[(a <= 0) | (d > 1)] = np.nan
            else:
                same_class = purine[sl] @ purine.T + pyrimidine[sl] @ pyrimidine.T
                P = (same_class - ident) / sites       # transitions
                Q = (sites - same_class) / sites       # transversions
                a, b = 1 - 2 * P - Q, 1 - 2 * Q
                d = -0.5 * np.log(a) - 0.25 * np.log(b)
                # Saturated: past one substitution per site the two sequences
                # are too far apart (or too badly aligned) for a distance
                d[(a <= 0) | (b <= 0) | (d > 1)] = np.nan
        d[sites < MIN_OVERLAP] = np.nan
        D[sl] = d
    np.fill_diagonal(D, 0.0)
    # Rounding makes identical sequences exactly 0
    D[np.abs(D) < 1e-7] = 0.0
    return D


# ── Barcode gap and distance summary ──────────────────────────────────────────

def _codes_of(labels):
    """Integer code per label, -1 for no label."""
    table = {}
    return np.array([table.setdefault(x, len(table)) if x else -1 for x in labels],
                    dtype=np.int64)


def _stats(values):
    if values.size == 0:
        return None
    return {"min": float(values.min()), "mean": float(values.mean()),
            "max": float(values.max())}


def barcode_gap(D, species, ids):
    """Per species: the largest distance within it, and the nearest sequence
    of another species (its nearest neighbour). Without a gap (the neighbour
    no farther than the farthest conspecific), the barcode can't tell the
    species apart."""
    members = defaultdict(list)
    for i, sp in enumerate(species):
        if sp:
            members[sp].append(i)
    labelled = np.array([bool(sp) for sp in species])
    result = []
    for sp, idx in members.items():
        rows = D[idx]
        intra = None
        if len(idx) >= 2:
            sub = rows[:, idx]
            values = sub[np.triu_indices(len(idx), 1)]
            values = values[~np.isnan(values)]
            if values.size:
                intra = {"max": float(values.max()), "mean": float(values.mean())}
        others = labelled.copy()
        others[idx] = False
        nn = None
        if others.any():
            sub = rows[:, others]
            if not np.isnan(sub).all():
                flat = np.nanargmin(sub)
                r, c = divmod(int(flat), sub.shape[1])
                j = np.flatnonzero(others)[c]
                nn = {"distance": float(sub[r, c]), "species": species[j], "id": ids[j]}
        result.append({
            "species": sp, "n": len(idx),
            "max_intra": intra["max"] if intra else None,
            "mean_intra": intra["mean"] if intra else None,
            "nn": nn["distance"] if nn else None,
            "nn_species": nn["species"] if nn else None,
            "nn_id": nn["id"] if nn else None,
        })
    result.sort(key=lambda x: x["species"])
    return result


# Identification check: a record this close to another species...
SUSPECT_NEAR = 0.01
# ...and this far from every record of its own species
SUSPECT_FAR = 0.03
# Names that don't identify a species
_PROVISIONAL = re.compile(r"\b(sp|spp|cf|aff|nr|gr|complex)\b\.?|\d", re.IGNORECASE)


def identification_check(D, species, genera, ids, bins):
    """Records that look like misidentifications or mixed-up samples.

    For each record with a species, its nearest record of another species.
    When that one is within SUSPECT_NEAR, the record is a suspect if nothing
    of its own species backs it (none within SUSPECT_FAR) and either the
    neighbour is of another genus or its own species is far away. A record
    with close conspecifics is not suspect: when a misidentified sequence
    sits next to it, that one is.
    The rest are species that share sequences, unless one of the two records
    is a suspect: the same epithet in two genera ('renamed': the species was
    moved to another genus and the records use both names), or a pair of
    congeneric species the marker doesn't tell apart ('congeneric':
    hybridisation, recent species, synonyms).
    Provisional names ("Genus sp.", "cf.", "aff.", interim names with
    numbers) are no species identification: they are left out.
    """
    species = ["" if _PROVISIONAL.search(sp) else sp for sp in species]
    codes = _codes_of(species)
    n = len(species)
    near = []
    for i in range(n):
        if codes[i] < 0:
            continue
        row = D[i]
        het = (codes >= 0) & (codes != codes[i]) & ~np.isnan(row)
        if not het.any():
            continue
        j = int(np.flatnonzero(het)[np.argmin(row[het])])
        if row[j] > SUSPECT_NEAR:
            continue
        con = (codes == codes[i]) & ~np.isnan(row)
        con[i] = False
        own = float(row[con].min()) if con.any() else None
        near.append((i, j, float(row[j]), own))

    def genus(k):
        return genera[k] or (species[k].split()[0] if species[k] else "")

    def epithet(k):
        words = species[k].split()
        return words[1] if len(words) > 1 and words[1].isalpha() and words[1].islower() else ""

    def renamed(i, j):
        return bool(epithet(i)) and epithet(i) == epithet(j) and genus(i) != genus(j)

    suspects, flagged = [], set()
    for i, j, dist, own in near:
        other_genus = bool(genus(i) and genus(j) and genus(i) != genus(j))
        unsupported = own is None or own > SUSPECT_FAR
        if renamed(i, j):
            continue
        if unsupported and (other_genus or own is not None):
            flagged.add(i)
            suspects.append({
                "id": ids[i], "species": species[i], "bin": bins[i],
                "own": own, "own_n": int((codes == codes[i]).sum()) - 1,
                "nn": dist, "nn_species": species[j], "nn_id": ids[j], "nn_bin": bins[j],
                "reason": "other_genus" if other_genus else "far_from_own",
            })
    pairs = {}
    for i, j, dist, own in near:
        if i in flagged or j in flagged:
            continue
        kind = "renamed" if renamed(i, j) else "congeneric" if genus(i) == genus(j) else None
        if kind is None:
            # A record of another genus next to one backed by its own
            # species: the suspect is the other one, if anything
            continue
        key = tuple(sorted((species[i], species[j])))
        p = pairs.setdefault(key, {"species": list(key), "kind": kind, "min": dist,
                                   "records": set(), "shared_bin": False})
        p["min"] = min(p["min"], dist)
        p["records"].update((ids[i], ids[j]))
        p["shared_bin"] = p["shared_bin"] or (bool(bins[i]) and bins[i] == bins[j])
    shared = [{**p, "records": sorted(p["records"])} for p in pairs.values()]
    suspects.sort(key=lambda s: (s["reason"] != "other_genus", -(s["own"] or 0), s["species"]))
    shared.sort(key=lambda p: (p["kind"] != "renamed", p["min"], p["species"]))
    return {"near": SUSPECT_NEAR, "far": SUSPECT_FAR, "suspects": suspects, "shared": shared}


# Fine histogram of distances, merged into wider bars at the end
_FINE_BIN = 0.001        # 0.1 %
_FINE_BINS = 1000        # up to 100 %


def distance_summary(D, species, genera, families, block=512):
    """BOLD's distance summary (within species, within genus between species,
    within family between genera) and the distribution of distances within
    and between species."""
    n = D.shape[0]
    sp, ge, fa = _codes_of(species), _codes_of(genera), _codes_of(families)
    levels = {
        "species": {"count": 0, "sum": 0.0, "min": math.inf, "max": -math.inf},
        "genus": {"count": 0, "sum": 0.0, "min": math.inf, "max": -math.inf},
        "family": {"count": 0, "sum": 0.0, "min": math.inf, "max": -math.inf},
    }
    hist = {"intra": np.zeros(_FINE_BINS + 1, np.int64),
            "congeneric": np.zeros(_FINE_BINS + 1, np.int64),
            "inter": np.zeros(_FINE_BINS + 1, np.int64)}

    def add(level, values):
        if values.size:
            acc = levels[level]
            acc["count"] += int(values.size)
            acc["sum"] += float(values.sum(dtype=np.float64))
            acc["min"] = min(acc["min"], float(values.min()))
            acc["max"] = max(acc["max"], float(values.max()))

    def bins(values):
        return np.bincount(np.minimum((values / _FINE_BIN).astype(np.int64), _FINE_BINS),
                           minlength=_FINE_BINS + 1)

    cols = np.arange(n)
    for start in range(0, n, block):
        stop = min(start + block, n)
        rows = np.arange(start, stop)
        upper = cols[None, :] > rows[:, None]           # each pair once
        d = D[start:stop]
        ok = upper & ~np.isnan(d)
        s_r, s_c = sp[start:stop, None], sp[None, :]
        g_r, g_c = ge[start:stop, None], ge[None, :]
        f_r, f_c = fa[start:stop, None], fa[None, :]
        same_sp = ok & (s_r >= 0) & (s_r == s_c)
        diff_sp = ok & (s_r >= 0) & (s_c >= 0) & (s_r != s_c)
        same_ge = diff_sp & (g_r >= 0) & (g_r == g_c)
        same_fa = ok & (g_r >= 0) & (g_c >= 0) & (g_r != g_c) & (f_r >= 0) & (f_r == f_c)
        add("species", d[same_sp])
        add("genus", d[same_ge])
        add("family", d[same_fa])
        hist["intra"] += bins(d[same_sp])
        hist["congeneric"] += bins(d[same_ge])
        hist["inter"] += bins(d[diff_sp])

    def taxa_with(groups_of):
        """Taxa with at least two members (two sequences, species or genera)."""
        return sum(1 for members in groups_of.values() if len(members) >= 2)

    by_species, species_by_genus, genera_by_family = defaultdict(set), defaultdict(set), defaultdict(set)
    for i in range(n):
        if species[i]:
            by_species[species[i]].add(i)
            if genera[i]:
                species_by_genus[genera[i]].add(species[i])
        if genera[i] and families[i]:
            genera_by_family[families[i]].add(genera[i])
    taxa = {"species": taxa_with(by_species), "genus": taxa_with(species_by_genus),
            "family": taxa_with(genera_by_family)}

    summary = []
    for level, acc in levels.items():
        summary.append({
            "level": level, "taxa": taxa[level], "comparisons": acc["count"],
            "min": acc["min"] if acc["count"] else None,
            "mean": acc["sum"] / acc["count"] if acc["count"] else None,
            "max": acc["max"] if acc["count"] else None,
        })

    # Between species: congeneric pairs when there are any (the comparison
    # that matters for identification), otherwise any two species
    other = "congeneric" if hist["congeneric"].sum() else "inter"
    return summary, _merge_histogram(hist["intra"], hist[other]), other


def _merge_histogram(a, b):
    """Two fine histograms as at most ~50 shared bars of a round width."""
    used = np.flatnonzero((a + b) > 0)
    if used.size == 0:
        return {"width": None, "bins": []}
    top = int(used.max()) + 1
    for width in (1, 2, 5, 10, 20, 50, 100):      # in fine bins (0.1 %)
        if top / width <= 50:
            break
    nbars = -(-top // width)
    bars = []
    for i in range(nbars):
        lo, hi = i * width, (i + 1) * width
        bars.append({"from": lo * _FINE_BIN, "to": hi * _FINE_BIN,
                     "intra": int(a[lo:hi].sum()), "inter": int(b[lo:hi].sum())})
    return {"width": width * _FINE_BIN, "bins": bars}


# ── Neighbour-joining tree ────────────────────────────────────────────────────

def drop_incomplete(D):
    """Indices to keep so that no distance is missing: NJ needs them all.
    Drops, one at a time, the sequence missing the most distances."""
    missing = np.isnan(D)
    counts = missing.sum(axis=1)
    keep = np.ones(D.shape[0], dtype=bool)
    while counts.max(initial=0) > 0:
        worst = int(np.argmax(counts))
        keep[worst] = False
        counts -= missing[:, worst]
        counts[worst] = 0
    return np.flatnonzero(keep)


def neighbor_joining(D):
    """Unrooted NJ tree as edges (node, node, length). Tips are 0..n-1."""
    n = D.shape[0]
    if n == 2:
        return [(0, 1, float(D[0, 1]))]
    D = D.astype(np.float64)
    ids = list(range(n))
    next_id = n
    edges = []
    active = n
    while active > 2:
        Dv = D[:active, :active]
        r = Dv.sum(axis=1)
        Q = Dv * (active - 2)
        Q -= r[:, None]
        Q -= r[None, :]
        np.fill_diagonal(Q, np.inf)
        i, j = divmod(int(np.argmin(Q)), active)
        if i > j:
            i, j = j, i
        dij = Dv[i, j]
        li = 0.5 * dij + (r[i] - r[j]) / (2 * (active - 2))
        li = min(max(li, 0.0), max(dij, 0.0))
        lj = max(dij - li, 0.0)
        u = next_id
        next_id += 1
        edges.append((u, ids[i], li))
        edges.append((u, ids[j], lj))
        dk = 0.5 * (Dv[i] + Dv[j] - dij)
        Dv[i, :] = dk
        Dv[:, i] = dk
        Dv[i, i] = 0.0
        last = active - 1
        if j != last:
            Dv[j, :] = Dv[last, :]
            Dv[:, j] = Dv[:, last]
            Dv[j, j] = 0.0
            ids[j] = ids[last]
        ids[i] = u
        ids.pop()
        active -= 1
    edges.append((ids[0], ids[1], max(float(D[0, 1]), 0.0)))
    return edges


def midpoint_root(edges, n_tips):
    """The tree rooted at the midpoint of its longest tip-to-tip path, as
    {node: [(child, length), ...]} plus the root node."""
    adj = defaultdict(list)
    for a, b, length in edges:
        adj[a].append((b, length))
        adj[b].append((a, length))

    def farthest(start):
        dist, prev, stack = {start: 0.0}, {start: None}, [start]
        while stack:
            node = stack.pop()
            for nxt, length in adj[node]:
                if nxt not in dist:
                    dist[nxt] = dist[node] + length
                    prev[nxt] = node
                    stack.append(nxt)
        tip = max((x for x in dist if x < n_tips), key=lambda x: dist[x])
        return tip, dist, prev

    a, _, _ = farthest(0)
    b, dist, prev = farthest(a)
    half = dist[b] / 2
    # Walk back from b until passing the midpoint
    node = b
    while prev[node] is not None and dist[prev[node]] > half:
        node = prev[node]
    parent = prev[node]
    root = max(adj) + 1
    if parent is None:
        root = node
    else:
        # Split the edge parent–node at the midpoint
        to_node = dist[node] - half
        to_parent = half - dist[parent]
        length = next(l for x, l in adj[node] if x == parent)
        adj[node] = [(x, l) for x, l in adj[node] if x != parent] + [(root, to_node)]
        adj[parent] = [(x, l) for x, l in adj[parent] if x != node] + [(root, to_parent)]
        adj[root] = [(node, to_node), (parent, to_parent)]
        assert abs(to_node + to_parent - length) < 1e-6

    children, stack, seen = defaultdict(list), [root], {root}
    while stack:
        node = stack.pop()
        for nxt, length in adj[node]:
            if nxt not in seen:
                seen.add(nxt)
                children[node].append((nxt, max(length, 0.0)))
                stack.append(nxt)
    return children, root


def to_newick(children, root, label):
    """Newick text, smaller clades first (a ladderized tree reads better)."""
    sizes = {}

    def size(node):
        stack, order = [node], []
        while stack:
            x = stack.pop()
            order.append(x)
            stack.extend(c for c, _ in children.get(x, []))
        for x in reversed(order):
            sizes[x] = sum(sizes[c] for c, _ in children.get(x, [])) or 1
        return sizes[node]

    size(root)
    out = []

    def write(node, length):
        kids = children.get(node)
        if kids:
            out.append("(")
            for i, (child, l) in enumerate(sorted(kids, key=lambda c: sizes[c[0]])):
                if i:
                    out.append(",")
                write(child, l)
            out.append(")")
        else:
            out.append(label(node))
        if length is not None:
            out.append(f":{length:.6f}")

    import sys
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 10000))
    try:
        write(root, None)
    finally:
        sys.setrecursionlimit(limit)
    out.append(";")
    return "".join(out)


# ── The whole analysis ────────────────────────────────────────────────────────

def _subsample(records, per_species):
    """At most `per_species` records per species (or per BIN when there is no
    species), the longest ones. Records with neither are all kept."""
    groups = defaultdict(list)
    keep = []
    for rec in records:
        key = rec.get("species") or rec.get("bin")
        if key:
            groups[key].append(rec)
        else:
            keep.append(rec)
    for recs in groups.values():
        recs.sort(key=lambda r: -len(r["clean"]))
        keep.extend(recs[:per_species])
    return keep


def _tip_label(rec):
    """Readable, Newick-safe label: processid|species|BIN."""
    parts = [rec.get("processid") or rec.get("id") or "",
             rec.get("species") or rec.get("taxon") or "", rec.get("bin") or ""]
    text = "|".join(p for p in parts if p)
    for ch in " ()[]:;,'\t":
        text = text.replace(ch, "_")
    return text or "seq"


def usable_records(records, forced_code=None):
    """The records fit for alignment (cleaned beforehand), oriented as
    in phase 1 (sequences submitted reversed are read on the reverse
    strand), and how many were left out and why."""
    rows, _ = seq_analysis.composition(records, forced_code)
    flags = {id(rec): row["flags"] for rec, row in
             zip([r for r in records if len(r["clean"]) >= seq_analysis.MIN_LENGTH], rows)}
    excluded = Counter()
    usable = []
    for rec in records:
        f = flags.get(id(rec))
        if f is None or len(rec["clean"]) < MIN_ALIGN_LENGTH:
            excluded["short"] += 1
        elif "stops" in f or "invalid" in f:
            # Probable pseudogenes or contamination: they distort distances
            excluded["stops"] += 1
        else:
            usable.append(rec)
    return usable, excluded


def run_distance(records, marker, forced_code=None, model="k2p",
                 per_species=None, want_tree=True, want_delimit=False):
    """Alignment, distances, barcode gap and (optionally) NJ tree and
    species delimitation.

    Returns (result for the page, {file name: text} for the ZIP).
    Raises AnalysisLimit when the input is too big or too small.
    """
    for rec in records:
        rec["marker"] = marker
        rec["clean"] = seq_analysis.clean_sequence(rec.get("seq"))
    # Subsample first: the input may hold tens of thousands of sequences
    before_subsample = len(records)
    if per_species:
        records = _subsample(records, per_species)
    subsampled = per_species if per_species and len(records) < before_subsample else None
    coding = seq_analysis.marker_kind(marker) not in ("noncoding", None) or bool(forced_code)
    limit = MAX_DISTANCE_SEQUENCES if coding else MAX_NONCODING_SEQUENCES
    if len(records) > limit:
        raise AnalysisLimit("too_many", len(records), limit)

    usable, excluded = usable_records(records, forced_code)

    if len(usable) < 3:
        raise AnalysisLimit("too_few", len(usable), 3)

    seqs = [rec["clean"] for rec in usable]
    groups, unplaced = align_groups(seqs)
    for g in groups:
        rec = usable[g["ref_index"]]
        g["ref_id"] = rec.get("processid") or rec.get("id") or ""
    excluded["unplaced"] = len(unplaced)
    placed = sum(len(g["members"]) for g in groups)
    if placed < 3:
        raise AnalysisLimit("too_few", placed, 3)

    # Records in group order; each group a block of the matrix. Pairs from
    # different groups have no distance: their sequences can't be aligned.
    recs = [usable[i] for g in groups for i, _ in g["members"]]
    aligned = [a for g in groups for _, a in g["members"]]
    n = len(recs)
    D = np.full((n, n), np.nan, dtype=np.float32)
    blocks = []
    start = 0
    for g in groups:
        m = len(g["members"])
        D[start:start + m, start:start + m] = distance_matrix(
            encode([a["row"] for _, a in g["members"]]), model)
        blocks.append((start, start + m))
        start += m
    usable = recs

    ids = [rec.get("processid") or rec.get("id") or f"seq{i + 1}" for i, rec in enumerate(usable)]
    species = [rec.get("species") or "" for rec in usable]
    genera = [rec.get("genus") or (sp.split()[0] if sp else "") for rec, sp in zip(usable, species)]
    families = [rec.get("family") or "" for rec in usable]

    gap = barcode_gap(D, species, ids)
    check = identification_check(D, species, genera, ids, [rec.get("bin") or "" for rec in usable])
    summary, histogram, other = distance_summary(D, species, genera, families)
    # Missing distances within a group (between groups they are all missing)
    no_overlap = int(sum(np.isnan(D[a:b, a:b]).sum() for a, b in blocks) // 2)

    labels = [_tip_label(rec) for rec in usable]
    several = len(groups) > 1
    files = {
        "barcode_gap.csv": _csv(
            ["species", "sequences", "max_intra", "mean_intra", "nearest_neighbour",
             "nn_species", "nn_sequence", "gap"],
            [[g["species"], g["n"], _f(g["max_intra"]), _f(g["mean_intra"]), _f(g["nn"]),
              g["nn_species"] or "", g["nn_id"] or "",
              _f(g["nn"] - g["max_intra"]) if g["nn"] is not None and g["max_intra"] is not None else ""]
             for g in gap]),
        "distance_summary.csv": _csv(
            ["level", "taxa", "comparisons", "min", "mean", "max"],
            [[s["level"], s["taxa"], s["comparisons"], _f(s["min"]), _f(s["mean"]), _f(s["max"])]
             for s in summary]),
        "identification_check.csv": _csv(
            ["type", "processid", "species", "bin", "nearest_own_species", "nearest_other_species",
             "other_species", "other_processid", "other_bin", "reason"],
            [["suspect", s["id"], s["species"], s["bin"], _f(s["own"]), _f(s["nn"]),
              s["nn_species"], s["nn_id"], s["nn_bin"], s["reason"]] for s in check["suspects"]]
            + [["shared", "; ".join(p["records"]), p["species"][0], "", "", _f(p["min"]),
                p["species"][1], "", "yes" if p["shared_bin"] else "no", p["kind"]]
               for p in check["shared"]]),
    }
    # One alignment per group: each has its own reference's columns
    for k, (a, b) in enumerate(blocks):
        name = f"alignment_group{k + 1}.fasta" if several else "alignment.fasta"
        files[name] = "".join(f">{labels[i]}\n{aligned[i]['row']}\n" for i in range(a, b))
    if n <= MAX_MATRIX_EXPORT:
        files["distances.csv"] = _csv(
            [""] + labels,
            [[lab] + [_f(None if math.isnan(x) else float(x)) for x in D[i]]
             for i, lab in enumerate(labels)])

    # One tree per group: sequences that can't be aligned have no distance,
    # and NJ needs every one
    trees, tree_notes = [], []
    if want_tree:
        for k, (a, b) in enumerate(blocks):
            m = b - a
            if m > MAX_TREE_SEQUENCES:
                tree_notes.append((k, "too_many", m, MAX_TREE_SEQUENCES))
                continue
            keep = a + drop_incomplete(D[a:b, a:b])
            if len(keep) < 3:
                tree_notes.append((k, "too_few", len(keep), 3))
                continue
            sub = D[np.ix_(keep, keep)]
            edges = neighbor_joining(sub)
            children, root = midpoint_root(edges, len(keep))
            trees.append({
                "group": k,
                # Tips by their position in `tips`, for the page
                "newick": to_newick(children, root, str),
                "tips": [{"i": int(i), "id": ids[i], "species": species[i], "genus": genera[i],
                          "bin": usable[i].get("bin") or "",
                          "country": usable[i].get("country") or ""}
                         for i in keep],
                "dropped": m - len(keep),
            })
            name = f"tree_group{k + 1}.nwk" if several else "tree.nwk"
            files[name] = to_newick(children, root, lambda t, keep=keep: labels[keep[t]]) + "\n"

    delimit, delimit_notes = [], []
    if want_delimit:
        bins = [rec.get("bin") or "" for rec in usable]
        delimit = _delimit(D, blocks, groups, ids, species, genera, bins, several, files, delimit_notes)

    def taxa_of(a, b):
        """The most common classes (or phyla, kingdoms) of a group."""
        names = Counter(usable[i].get("class") or usable[i].get("phylum")
                        or usable[i].get("kingdom") or "" for i in range(a, b))
        return [[name, count] for name, count in names.most_common(4) if name]

    result = {
        "marker": marker,
        "model": model,
        "coding": coding,
        "received": before_subsample,
        "analysed": n,
        "subsampled": subsampled,
        "excluded": dict(excluded),
        "groups": [{"n": b - a,
                    "reference": {"id": g["ref_id"], "length": len(g["ref"])},
                    "taxa": taxa_of(a, b)}
                   for g, (a, b) in zip(groups, blocks)],
        "gapped": sum(1 for a in aligned if a["gapped"]),
        "reversed": sum(1 for a in aligned if a["reversed"]),
        "inserted": sum(a["inserted"] for a in aligned),
        "no_overlap_pairs": no_overlap,
        "summary": summary,
        "histogram": histogram,
        "histogram_other": other,
        "gap": gap,
        "check": check,
        "trees": trees,
        "tree_notes": tree_notes,
        "matrix_exported": n <= MAX_MATRIX_EXPORT,
        "delimit": delimit,
        "delimit_notes": delimit_notes,
        "delimit_requested": bool(want_delimit),
    }
    return result, files


def _delimit(D, blocks, groups, ids, species, genera, bins, several, files, notes):
    """ASAP and ABGD within each group of sequences that align together.
    Adds the tables to `files` and returns, per group, the partitions the
    page offers: ASAP's best ones and ABGD's for each prior, each as the
    MOTU number of every sequence."""
    out = []
    asap_rows, abgd_rows, motu_cols, conc_rows = [], [], [], []
    for k, (a, b) in enumerate(blocks):
        m = b - a
        if m > MAX_DELIMIT_SEQUENCES:
            notes.append((k, "too_many", m, MAX_DELIMIT_SEQUENCES))
            continue
        keep = a + drop_incomplete(D[a:b, a:b])
        if len(keep) < 3:
            notes.append((k, "too_few", len(keep), 3))
            continue
        # As ASAP and ABGD read a matrix: doubles with six decimals
        sub = np.round(D[np.ix_(keep, keep)].astype(np.float64), 6)
        length = len(groups[k]["ref"])
        best, evaluated = seq_delimit.asap(sub, length)
        priors = seq_delimit.abgd(sub)
        nk = len(keep)
        asap = [{
            "rank": x["rank"], "score": x["score"], "p": x["p"], "slope": x["slope"],
            "threshold": x["threshold"], "subsets": len(x["groups"]),
            "subsets_simple": x["groups_simple"],
            "motu": seq_delimit.labels_of(x["groups"], nk),
        } for x in best]
        abgd = [{
            "prior": x["prior"], "gap": x["gap"],
            "initial": len(x["initial"]), "recursive": len(x["recursive"]),
            "motu_initial": seq_delimit.labels_of(x["initial"], nk),
            "motu_recursive": seq_delimit.labels_of(x["recursive"], nk),
        } for x in priors]
        sp = [species[i] for i in keep]
        bn = [bins[i] for i in keep]
        out.append({"group": k, "members": [int(i) for i in keep], "dropped": m - len(keep),
                    "ids": [ids[i] for i in keep], "species": sp, "bins": bn,
                    "length": length, "asap": asap, "asap_evaluated": evaluated, "abgd": abgd,
                    "guide": seq_delimit.guide(sub, sp, [genera[i] for i in keep], bn, asap, abgd)})

        gname = str(k + 1) if several else ""
        for x in asap:
            asap_rows.append(([gname] if several else []) + [
                x["rank"], f"{x['score']:g}", f"{x['p']:.3e}", f"{x['slope']:.6e}",
                _f(x["threshold"]), x["subsets"], x["subsets_simple"]])
        for x in abgd:
            abgd_rows.append(([gname] if several else []) + [
                f"{x['prior']:.6f}", _f(x["gap"]), x["initial"], x["recursive"]])
        cols = [("asap_rank1", asap[0]["motu"])] if asap else []
        cols += [(f"asap_rank{x['rank']}", x["motu"]) for x in asap[1:3]]
        cols += [(f"abgd_P{x['prior']:.4f}", x["motu_recursive"]) for x in abgd]
        motu_cols.append((k, keep, cols))
        if asap:
            _, rows = seq_delimit.concordance(asap[0]["motu"], sp, bn)
            for r in rows:
                conc_rows.append(([gname] if several else []) + [
                    r["species"], r["n"], len(r["motus"]), "; ".join(f"MOTU {x}" for x in r["motus"]),
                    "; ".join(r["bins"]), "; ".join(r["with"]), r["status"]])

    if not out:
        return out
    g = ["group"] if several else []
    files["delimitation_asap.csv"] = _csv(
        g + ["rank", "asap_score", "p_value", "gap_width", "threshold_distance",
             "motus", "motus_without_recursion"], asap_rows)
    files["delimitation_abgd.csv"] = _csv(
        g + ["prior_p", "barcode_gap_distance", "motus_initial", "motus_recursive"], abgd_rows)
    files["delimitation_concordance_asap.csv"] = _csv(
        g + ["species", "sequences", "motus", "motu_list", "bins", "shares_motu_with", "status"],
        conc_rows)
    # One table per group: the partitions of different groups have
    # different columns
    for k, keep, cols in motu_cols:
        name = f"delimitation_motus_group{k + 1}.csv" if several else "delimitation_motus.csv"
        files[name] = _csv(
            ["processid", "species", "bin"] + [c for c, _ in cols],
            [[ids[i], species[i], bins[i]] + [v[j] for _, v in cols]
             for j, i in enumerate(keep)])
    return out


def _f(x):
    return "" if x is None else f"{x:.5f}"


def _csv(header, rows):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()
