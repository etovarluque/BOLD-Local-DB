"""Haplotype networks (TCS and minimum spanning network): part of the
viewer's "Sequence analysis" tab.

The sequences (usually those of one species) are aligned as for the
distances and compared over the columns every one of them covers with an A,
C, G or T, so that a missing end or an N doesn't make a haplotype of its
own. Identical sequences there form a haplotype; the network joins the
haplotypes by the mutations that separate them.

- MSN (minimum spanning network; Bandelt, Forster & Röhl 1999, with
  epsilon 0): the union of all minimum spanning trees. Haplotypes are joined
  directly, the edge carrying the number of differences.
- TCS (statistical parsimony; Clement, Posada & Crandall 2000), as PopART
  builds it (Leigh & Bryant 2015): groups of haplotypes are joined by
  increasing distance, through unsampled intermediate haplotypes placed so
  that path lengths in the network match the observed differences as well
  as possible. Like PopART, it joins every group (no 95 % connection limit).

Independent numpy implementations of the published algorithms, checked
against PopART's. Also computes the usual diversity statistics and the
layout the page draws (stress majorization on the network's distances).
"""

import math
from collections import Counter

import numpy as np

import seq_analysis
import seq_distance

# Most sequences in a network, and most haplotypes (TCS grows with the
# fourth power of the haplotypes; MSN is cheap)
MAX_NETWORK_SEQUENCES = 2000
MAX_TCS_HAPLOTYPES = 300
MAX_MSN_HAPLOTYPES = 600
# Fewest columns compared, and share of the typical sequence the shared
# columns must keep (sequences that would shorten them more are left out)
MIN_CORE_SITES = 100
MIN_CORE_FRACTION = 0.8


class NetworkLimit(ValueError):
    """The input is too big (or too small) for a network."""

    def __init__(self, kind, n, limit):
        super().__init__(kind)
        self.kind, self.n, self.limit = kind, n, limit


# ── Haplotypes ────────────────────────────────────────────────────────────────

def core_columns(codes):
    """Rows kept and columns compared: those every kept row has as A, C, G
    or T. Rows that would leave the shared columns under MIN_CORE_FRACTION
    of the typical row are dropped, the most incomplete first."""
    valid = codes < 4
    keep = np.ones(codes.shape[0], dtype=bool)
    while True:
        rows = valid[keep]
        core = rows.all(axis=0)
        typical = float(np.median(rows.sum(axis=1)))
        if core.sum() >= MIN_CORE_FRACTION * typical or keep.sum() <= 2:
            return keep, core
        # The row that leaves out the most columns the others have
        common = rows.mean(axis=0) >= 0.5
        missing = (~valid & common).sum(axis=1).astype(float)
        missing[~keep] = -1
        keep[int(np.argmax(missing))] = False


def haplotypes(codes):
    """Haplotype of each row (numbered by first appearance) and the rows of
    each haplotype."""
    index, of_row, members = {}, [], []
    for i, row in enumerate(codes):
        key = row.tobytes()
        h = index.setdefault(key, len(index))
        if h == len(members):
            members.append([])
        members[h].append(i)
        of_row.append(h)
    return of_row, members


def hamming(codes):
    """Differences between every two rows (no missing data)."""
    n = codes.shape[0]
    same = np.zeros((n, n), dtype=np.int64)
    for b in range(4):
        x = (codes == b).astype(np.int32)
        same += x @ x.T
    return codes.shape[1] - same


def diversity(codes, of_row):
    """Haplotype diversity, nucleotide diversity, segregating sites and
    Tajima's D of the rows."""
    n, m = codes.shape
    counts = np.bincount(of_row)
    hd = (n / (n - 1)) * (1 - float(((counts / n) ** 2).sum())) if n > 1 else 0.0
    seg = int(sum(len(set(col)) > 1 for col in codes.T.tolist()))
    k = 0.0
    if n > 1:
        # Mean pairwise differences, from the haplotypes and their counts
        uniq = np.array([codes[of_row.index(h)] for h in range(len(counts))])
        d = hamming(uniq).astype(float)
        k = float((counts[:, None] * counts[None, :] * d).sum() / (n * (n - 1)))
    pi = k / m if m else 0.0
    tajima = None
    if n >= 4 and seg > 0:
        a1 = sum(1.0 / i for i in range(1, n))
        a2 = sum(1.0 / i ** 2 for i in range(1, n))
        b1 = (n + 1) / (3.0 * (n - 1))
        b2 = 2.0 * (n * n + n + 3) / (9.0 * n * (n - 1))
        c1 = b1 - 1 / a1
        c2 = b2 - (n + 2) / (a1 * n) + a2 / a1 ** 2
        e1, e2 = c1 / a1, c2 / (a1 ** 2 + a2)
        tajima = (k - seg / a1) / math.sqrt(e1 * seg + e2 * seg * (seg - 1))
    return {"hd": hd, "pi": pi, "k": k, "segregating": seg, "tajima_d": tajima}


# ── Networks ──────────────────────────────────────────────────────────────────

def _pairs_by_distance(d):
    """Pairs (j, i), j < i, grouped by distance, in PopART's order."""
    n = d.shape[0]
    levels = {}
    for i in range(n):
        for j in range(i):
            levels.setdefault(int(d[i, j]), []).append((j, i))
    return [levels[k] for k in sorted(levels)], sorted(levels)


def msn(d):
    """Minimum spanning network (epsilon 0): edges (a, b, differences)."""
    n = d.shape[0]
    comp = list(range(n))
    edges = []
    for pairs, dist in zip(*_pairs_by_distance(d)):
        new = [(u, v) for u, v in pairs if comp[u] != comp[v]]
        for u, v in new:
            edges.append((v, u, dist))
        for u, v in new:
            cu, cv = comp[u], comp[v]
            if cu != cv:
                lo, hi = min(cu, cv), max(cu, cv)
                comp = [lo if c == hi else c for c in comp]
        if len(set(comp)) == 1:
            break
    return n, edges


class _TCS:
    """TCS as PopART builds it: vertices 0..n-1 are the haplotypes, the rest
    unsampled intermediates."""

    BONUS, SHORTCUT, LONG = 20, 10, 5
    WORST = -(2 ** 31)

    def __init__(self, d):
        self.d = d
        self.n = d.shape[0]
        self.comp = list(range(self.n))
        self.P = np.full((self.n, self.n), np.inf)
        np.fill_diagonal(self.P, 0.0)
        self.edges = []

    def run(self):
        for pairs, M in zip(*_pairs_by_distance(self.d)):
            pending = pairs
            while pending:
                comp_a = comp_b = None
                others = []
                for u, v in pending:
                    cu, cv = self.comp[u], self.comp[v]
                    if cu == cv:
                        continue
                    if cu > cv:
                        cu, cv, u, v = cv, cu, v, u
                    if comp_a is None:
                        comp_a, comp_b = cu, cv
                    if (cu, cv) != (comp_a, comp_b):
                        others.append((u, v))
                    elif M == 1:
                        self._path(u, v, 1)
                    else:
                        a, b, length = self._intermediates(u, v, M)
                        existing = self.P[a, b]
                        # PopART stops on a shorter existing path; it never
                        # arises with complete data, and is skipped here
                        if existing > length:
                            self._path(a, b, length)
                if comp_a is not None:
                    self.comp = [comp_a if (c < 0 or c == comp_b) else c - 1 if c > comp_b else c
                                 for c in self.comp]
                pending = others
        return self._collapse()

    def _intermediates(self, u, v, dist):
        """The two vertices (in or between the groups of u and v) to join
        with a new path, and its length."""
        comp, P, d = self.comp, self.P, self.d
        cu, cv = comp[u], comp[v]
        A = [x for x in range(self.n) if comp[x] == cu]
        B = [x for x in range(self.n) if comp[x] == cv]
        dAB = d[np.ix_(A, B)]
        I = [i for i in range(len(comp)) if (comp[i] == cu or comp[i] < 0)
             and P[u, i] < dist]
        J = np.array([j for j in range(len(comp)) if comp[j] == cv or comp[j] < 0], dtype=np.int64)
        best, best_score, min_len = (u, v), self.WORST + 1, dist
        PJ_B = P[np.ix_(J, B)] if len(J) else None
        for i in I:
            path_ui = P[u, i]
            path_vj = P[v, J]
            ok = path_vj + path_ui < dist          # also drops unreachable ones
            if not ok.any():
                continue
            js = J[ok]
            dP = dist - path_vj[ok] - path_ui
            # Path length between every a in A and b in B through i ... j
            total = dP[:, None, None] + P[i, A][None, :, None] + PJ_B[ok][:, None, :]
            eq = total == dAB
            gt = total > dAB
            lt = total < dAB
            score = (self.BONUS * eq.sum(axis=(1, 2)) - self.LONG * gt.sum(axis=(1, 2))
                     - self.SHORTCUT * lt.sum(axis=(1, 2))).astype(np.int64)
            shortcut = (lt & (total < dist)).any(axis=(1, 2))
            for k in range(len(js)):
                s = self.WORST if shortcut[k] else int(score[k])
                if s > best_score or (s == best_score and dP[k] < min_len):
                    best_score, min_len, best = s, int(dP[k]), (i, int(js[k]))
        return best[0], best[1], int(min_len)

    def _path(self, a, b, length):
        """Join a and b by `length` mutations, through length - 1 new
        intermediates, and update the path lengths."""
        P = self.P
        # Paths between existing vertices may now go through the new one
        via = np.minimum(P[:, a][:, None] + length + P[b, :][None, :],
                         P[:, b][:, None] + length + P[a, :][None, :])
        P = np.minimum(P, via)
        k = np.arange(1, length)
        nv = P.shape[0]
        if length > 1:
            rows = np.minimum(k[:, None] + P[a][None, :], (length - k)[:, None] + P[b][None, :])
            kk = np.abs(k[:, None] - k[None, :]).astype(float)
            around = np.minimum(k[:, None] + P[a, b] + (length - k)[None, :],
                                (length - k)[:, None] + P[b, a] + k[None, :])
            inner = np.minimum(kk, around)
            P = np.block([[P, rows.T], [rows, inner]])
        self.P = P
        chain = [a] + list(range(nv, nv + length - 1)) + [b]
        self.comp.extend([-1] * (length - 1))
        for x, y in zip(chain, chain[1:]):
            self.edges.append((x, y, 1))

    def _collapse(self):
        """Intermediates of degree 2 become part of a longer edge."""
        nv = len(self.comp)
        adj = {x: {} for x in range(nv)}
        for x, y, w in self.edges:
            adj[x][y] = adj[x].get(y, 0) + w
            adj[y][x] = adj[y].get(x, 0) + w
        for x in range(self.n, nv):
            if len(adj[x]) == 2:
                (y, wy), (z, wz) = adj[x].items()
                if y != z and z not in adj[y]:
                    del adj[y][x], adj[z][x]
                    adj[y][z] = adj[z][y] = wy + wz
                    adj[x] = {}
        alive = [x for x in range(nv) if x < self.n or adj[x]]
        renum = {x: i for i, x in enumerate(alive)}
        edges = [(renum[x], renum[y], w) for x in alive for y, w in adj[x].items() if x < y]
        return len(alive), edges


def tcs(d):
    """TCS network: (vertex count, edges (a, b, differences)); vertices
    0..n-1 are the haplotypes, the rest unsampled intermediates."""
    return _TCS(d).run()


# ── Layout ────────────────────────────────────────────────────────────────────

def layout(nv, edges, radius, unit=1.6, iterations=300):
    """Coordinates for the vertices: stress majorization on the network's
    path lengths, an edge being as long as its two nodes plus `unit` per
    square root of its mutations."""
    if nv == 1:
        return np.zeros((1, 2))
    L = np.full((nv, nv), np.inf)
    np.fill_diagonal(L, 0.0)
    for a, b, w in edges:
        length = radius[a] + radius[b] + unit * math.sqrt(w)
        L[a, b] = L[b, a] = min(L[a, b], length)
    for k in range(nv):
        L = np.minimum(L, L[:, k:k + 1] + L[k:k + 1, :])
    L[np.isinf(L)] = np.nanmax(L[np.isfinite(L)]) * 1.5
    # Start from classical scaling, then majorize (weights 1/d²)
    n = nv
    J = np.eye(n) - 1.0 / n
    B = -0.5 * J @ (L ** 2) @ J
    vals, vecs = np.linalg.eigh(B)
    X = vecs[:, -2:] * np.sqrt(np.maximum(vals[-2:], 1e-9))
    rng = np.random.default_rng(1)
    X = X + rng.normal(scale=1e-3, size=X.shape)
    with np.errstate(divide="ignore"):
        W = np.where(L > 0, 1.0 / L ** 2, 0.0)
    np.fill_diagonal(W, 0.0)
    Lw = -W
    np.fill_diagonal(Lw, W.sum(axis=1))
    Lw_pinv = np.linalg.pinv(Lw)
    for _ in range(iterations):
        diff = X[:, None, :] - X[None, :, :]
        dist = np.sqrt((diff ** 2).sum(axis=2))
        with np.errstate(divide="ignore", invalid="ignore"):
            Bz = np.where(dist > 0, -W * L / dist, 0.0)
        np.fill_diagonal(Bz, 0.0)
        np.fill_diagonal(Bz, -Bz.sum(axis=1))
        X_new = Lw_pinv @ (Bz @ X)
        if np.abs(X_new - X).max() < 1e-4:
            X = X_new
            break
        X = X_new
    X = X - X.mean(axis=0)
    # Longest extent across the page, which is wider than tall
    _, _, Vt = np.linalg.svd(X, full_matrices=False)
    return X @ Vt.T


# ── The whole analysis ────────────────────────────────────────────────────────

def run_network(records, marker, forced_code=None, method="tcs"):
    """Haplotypes, network and layout of the records (one marker), for the
    page, which also builds the downloads from it.

    Raises NetworkLimit when the input is too big or too small.
    """
    for rec in records:
        rec["marker"] = marker
        rec["clean"] = seq_analysis.clean_sequence(rec.get("seq"))
    if len(records) > MAX_NETWORK_SEQUENCES:
        raise NetworkLimit("too_many", len(records), MAX_NETWORK_SEQUENCES)
    usable, excluded = seq_distance.usable_records(records, forced_code)
    if len(usable) < 2:
        raise NetworkLimit("too_few", len(usable), 2)

    groups, unplaced = seq_distance.align_groups([rec["clean"] for rec in usable])
    excluded["unplaced"] = len(unplaced)
    if not groups:
        raise NetworkLimit("too_few", 0, 2)
    # Sequences that can't be aligned with the main group are left out
    groups.sort(key=lambda g: -len(g["members"]))
    excluded["other_group"] = sum(len(g["members"]) for g in groups[1:])
    members = groups[0]["members"]
    recs = [usable[i] for i, _ in members]
    codes = seq_distance.encode([a["row"] for _, a in members])

    keep, core = core_columns(codes)
    excluded["incomplete"] = int((~keep).sum())
    recs = [r for r, k in zip(recs, keep) if k]
    codes = codes[keep][:, core]
    if len(recs) < 2:
        raise NetworkLimit("too_few", len(recs), 2)
    if codes.shape[1] < MIN_CORE_SITES:
        raise NetworkLimit("short_core", codes.shape[1], MIN_CORE_SITES)

    of_row, hap_rows = haplotypes(codes)
    nh = len(hap_rows)
    limit = MAX_TCS_HAPLOTYPES if method == "tcs" else MAX_MSN_HAPLOTYPES
    if nh > limit:
        raise NetworkLimit("too_many_haplotypes", nh, limit)
    hap_codes = codes[[rows[0] for rows in hap_rows]]
    d = hamming(hap_codes)
    nv, edges = tcs(d) if method == "tcs" else msn(d)

    counts = [len(rows) for rows in hap_rows]
    radius = [math.sqrt(c) * 0.6 + 0.4 if v < nh else 0.25
              for v, c in zip(range(nv), counts + [0] * (nv - nh))]
    xy = layout(nv, edges, radius)

    # Haplotypes named by frequency (H1 the commonest), as tables usually do
    order = sorted(range(nh), key=lambda h: (-counts[h], h))
    name = {h: f"H{k + 1}" for k, h in enumerate(order)}
    ids = [r.get("processid") or r.get("id") or f"seq{i + 1}" for i, r in enumerate(recs)]
    letters = "ACGT"
    hap_seqs = ["".join(letters[c] for c in row) for row in hap_codes]

    result = {
        "method": method,
        "marker": marker,
        "received": len(records),
        "analysed": len(recs),
        "excluded": {k: v for k, v in excluded.items() if v},
        "sites": int(codes.shape[1]),
        "alignment_columns": int(len(core)),
        "haplotypes": nh,
        "intermediates": nv - nh,
        "stats": diversity(codes, of_row),
        "vertices": [{
            "x": float(xy[v, 0]), "y": float(xy[v, 1]), "r": float(radius[v]),
            "hap": name.get(v), "n": counts[v] if v < nh else 0,
            "seqs": hap_rows[v] if v < nh else [],
        } for v in range(nv)],
        "edges": [{"a": a, "b": b, "w": int(w)} for a, b, w in edges],
        "seqs": [{"id": ids[i], "species": r.get("species") or "", "bin": r.get("bin") or "",
                  "country": r.get("country") or "", "hap": name[of_row[i]]}
                 for i, r in enumerate(recs)],
        "hap_seqs": {name[h]: hap_seqs[h] for h in range(nh)},
    }
    return result
