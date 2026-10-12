"""Species delimitation from a distance matrix (ASAP and ABGD): part of the
distance analysis of the viewer's "Sequence analysis" tab.

Both methods split the sequences into groups (MOTUs, molecular operational
taxonomic units) using only their pairwise distances, without the names the
records carry; comparing the groups with the names and BINs is what tells
which species the marker separates, which ones it splits and which ones it
lumps together.

- ABGD (Automatic Barcode Gap Discovery; Puillandre, Lambert, Brouillet &
  Achaz 2012, Molecular Ecology 21: 1864-1877) looks, in the sorted pairwise
  distances, for the first jump (the barcode gap) beyond a prior maximum
  intraspecific distance P, and groups the sequences closer than it. It
  runs for ten values of P and then tries to split each group again
  (recursive partition).
- ASAP (Assemble Species by Automatic Partitioning; Puillandre, Brouillet &
  Achaz 2021, Molecular Ecology Resources 21: 609-620) builds every
  partition of hierarchical (single-linkage) clustering, scores each by how
  wide the gap at its threshold is and by how unlikely, under a coalescent
  model of a single species, the groups it keeps apart are to be one, and
  ranks them: the lowest ASAP score is the best partition.

Independent implementations, in numpy, of the published methods, following
the defaults of their reference programs (ABGD: P from 0.001 to 0.1 in ten
steps, relative gap width X = 1.5; ASAP: 0.1 slope window, p-values weighted
as much as gap widths, 0.001 threshold to split groups again), and checked
against them. Departures, all to keep the run in seconds: ASAP stops at
10,000 coalescent replicates per partition (its manual's default; the
program goes up to 100,000), so its smallest p-value is 1e-4, and partitions
tied on p-value share their rank instead of being ordered at random; ABGD
stops looking for a gap after scanning the distances some 200 million
times over, keeping the last window's answer (the program would go on
through every window size).

The coalescent replicates use a fixed random seed: the same data give the
same results.
"""

import math
import re
from collections import defaultdict

import numpy as np

# ── ABGD ──────────────────────────────────────────────────────────────────────

ABGD_P_MIN = 0.001
ABGD_P_MAX = 0.1
ABGD_STEPS = 10
# Relative gap width: a gap counts when the slope of the sorted distances
# rises this many times over the steepest one below the prior
ABGD_X = 1.5


def abgd_priors(p_min=ABGD_P_MIN, p_max=ABGD_P_MAX, steps=ABGD_STEPS):
    """Prior maximum intraspecific distances, evenly spaced on a log scale."""
    scale = math.log10(p_max / p_min) / (steps - 1)
    return [p_min * 10 ** (scale * i) for i in range(steps - 1)] + [p_max]


def _min_window(nval):
    return 1000 if nval > 10000 else max(nval // 10, 1)


def _first_peak(values, steps, cummean, window, prior, x):
    """First jump in the slope of the sorted distances `values` past the
    prior, with a window of `window` values. Returns (distance, rank) or
    None."""
    n = len(values)
    span = window - 1
    slope = (values[span:] - values[:n - span]) / span           # n - window + 1 values
    last = n - window                                            # last slope index

    # Estimate of theta: mean distance up to the prior
    i = int(np.searchsorted(values, prior, side="right"))
    i = min(max(i, 1), n)
    theta = cummean[i - 1]

    # Steepest slope inside [0, prior]
    i0 = 1
    if last > 1:
        beyond = np.flatnonzero(values[1 + window:last + window] > prior)
        i0 = 1 + int(beyond[0]) if beyond.size else last
    slope_max = float(slope[:max(i0, 1)].max())

    down = np.flatnonzero(slope[1:] < slope[:-1])                # slope[j+1] < slope[j]
    up = np.flatnonzero(slope[1:] > slope[:-1])
    reach = window // 10

    i = i0
    while i < last:
        # Climb to a local maximum
        k = np.searchsorted(down, i)
        i = int(down[k]) if k < down.size and down[k] < last else last
        # Look up to `reach` values to its right for a higher one; each
        # higher value found moves the summit and the horizon
        top = i
        while True:
            stop = min(top + reach + 1, last)
            if i >= stop:
                break
            seg = slope[i:stop]
            higher = np.flatnonzero(seg > slope[top])
            if higher.size:
                top = i + int(higher[0])
                i = top + 1
            else:
                i = stop
                break
        # Centre of the jump: narrow the window down to 2 values, moving
        # right while the jump starts further on. That ends on the largest
        # step in the window; only when steps tie is the walk done one by
        # one, so the tie breaks as in ABGD
        c = None
        if window > 2 and top + window < n:
            seg = steps[top:top + window - 1]
            c = int(np.argmax(seg))
            if np.count_nonzero(seg >= seg[c] - 1e-12) > 1:
                c = None
        if c is None:
            v = values[top:top + window + 1].tolist()
            c = 0
            for wt in range(window - 1, 1, -1):
                if v[c + wt - 1] - v[c] < v[c + wt] - v[c + 1] and top + c < n - wt - 1:
                    c += 1
        ct = top + c
        mean_dist = (values[ct] + values[ct + 1]) / 2.0
        if mean_dist > 2.581 * 2 * theta and slope[top] > x * slope_max:
            return mean_dist, ct + 0.5
        if slope[top] > slope_max and cummean[ct] <= theta:
            slope_max = float(slope[top])
        i = top + 1 if top > ct else ct + 1
        k = np.searchsorted(up, i)
        i = int(up[k]) if k < up.size and up[k] < last else max(i, last)
    return None


def _find_gap(values, prior, x=ABGD_X):
    """The barcode gap of sorted distances, tried with growing windows until
    three in a row agree (within 10 %). Returns its distance or None."""
    n = len(values)
    if n < 3:
        return None
    cummean = np.cumsum(values, dtype=np.float64) / np.arange(1, n + 1)
    steps = np.diff(values)
    w_min, w_max = _min_window(n), n - 1
    step = w_min // 10 if w_min > 10 else 1
    stable, stable_dist, found = 0, -1.0, None
    w = max(w_min, 2)
    tries = 0
    while w <= w_max and stable < 3:
        found = _first_peak(values, steps, cummean, w, prior, x)
        dist = found[0] if found else -1.0
        if found and abs(dist - stable_dist) < 0.1 * stable_dist:
            stable += 1
        else:
            stable, stable_dist = 1, dist
        w += step
        tries += 1
        # Until three windows agree, every window would be tried, each one a
        # pass over all the distances: past a budget the search stops with
        # the last window's answer, as when it runs out of windows (no gap,
        # if the last one found none)
        if tries * n > ABGD_WINDOW_BUDGET and tries >= 60:
            break
    return found[0] if found else None


# Distances × windows scanned looking for a gap before concluding there is none
ABGD_WINDOW_BUDGET = 200_000_000


def _components(D, members, limit):
    """Groups of `members` joined by distances under `limit` (single
    linkage), as lists of indices, in order of their first member."""
    sub = D[np.ix_(members, members)] < limit
    label = np.full(len(members), -1)
    groups = []
    for start in range(len(members)):
        if label[start] >= 0:
            continue
        label[start] = len(groups)
        stack, group = [start], [start]
        while stack:
            row = stack.pop()
            new = np.flatnonzero(sub[row] & (label < 0))
            label[new] = len(groups)
            stack.extend(new.tolist())
            group.extend(new.tolist())
        groups.append(sorted(members[g] for g in group))
    return groups


def _upper(D, members):
    sub = D[np.ix_(members, members)]
    return np.sort(sub[np.triu_indices(len(members), 1)].astype(np.float64))


def abgd(D, priors=None, x=ABGD_X):
    """ABGD over a complete distance matrix. One entry per prior until one
    gives a single group: {prior, gap, initial, recursive}, the last two
    as lists of groups (lists of indices)."""
    n = D.shape[0]
    everyone = list(range(n))
    values = _upper(D, everyone)
    results = []
    for prior in priors or abgd_priors():
        gap = _find_gap(values, prior, x)
        if gap is None:
            results.append({"prior": prior, "gap": None, "initial": [everyone],
                            "recursive": [everyone]})
            break
        initial = _components(D, everyone, gap)
        groups = [g for g in initial]
        changed = True
        while changed:
            changed = False
            nxt = []
            for g in groups:
                if len(g) >= 3:
                    sub_gap = _find_gap(_upper(D, g), prior, x)
                    if sub_gap is not None:
                        parts = _components(D, g, sub_gap)
                        if len(parts) > 1:
                            nxt.extend(parts)
                            changed = True
                            continue
                nxt.append(g)
            groups = nxt
        results.append({"prior": prior, "gap": gap, "initial": initial, "recursive": groups})
        if len(groups) == 1:
            break
    return results


# ── ASAP ──────────────────────────────────────────────────────────────────────

# Distances averaged around a threshold to measure the gap: ±10 %
ASAP_SLOPE_SPAN = 0.1
# Weight of the p-value rank in the ASAP score (the gap width gets the rest)
ASAP_P_WEIGHT = 0.5
# A group formed with a p-value at or under this is split again
ASAP_SPLIT_P = 0.001
# Coalescent replicates: first batch, cap, and successes after which a
# p-value is known well enough
ASAP_FIRST_REPLICATES = 500
ASAP_MAX_REPLICATES = 10_000
ASAP_MIN_SUCCESS = 50
_EPS = 0.0001
# Partitions reported, best first
ASAP_REPORTED = 10
# Sequences simulated at once (replicates × leaves), to bound memory
_SIM_CELLS = 1_000_000


def _intra_replicates(n, k, mu, reps, rng):
    """Sum of pairwise differences within the two sides of a coalescent tree
    of `n` leaves whose root splits them into `k` and `n - k`, under
    `reps` replicates, with `mu` mutations per unit of branch length.

    The top-down coalescent cuts the n - 1 gaps between adjacent leaves in
    a uniformly random order (the root's gap first); undoing the cuts in
    reverse order joins segments, and each join gives the two branches
    under it, with their lengths. A branch over s leaves of a side of m
    leaves carries mutations that differ between s × (m - s) pairs.
    """
    out = np.zeros(reps)
    if n <= 2:
        return out
    m = n - 1
    steps = m - 1                                  # joins below the root
    root = k - 1
    others = np.array([g for g in range(m) if g != root], dtype=np.int64)
    # Inter-coalescence times with i lineages: exponential, mean 2 / (i(i-1))
    i = np.arange(2, n + 1, dtype=np.float64)
    scale = 2.0 / (i * (i - 1.0))
    chunk = max(1, _SIM_CELLS // n)
    for start in range(0, reps, chunk):
        r = min(reps, start + chunk) - start
        base = np.arange(r, dtype=np.int64) * n
        # Gap cut at each rank 2..m
        order = others[np.argsort(rng.random((r, steps)), axis=1)]
        tau = rng.exponential(size=(r, n - 1)) * scale
        cut = np.zeros((r, n))                     # cut[:, j]: time of the cut of rank j + 1
        np.cumsum(tau, axis=1, out=cut[:, 1:])
        # Segments by their endpoints (flat, one row of n per replicate):
        # left[x] is the first leaf of the segment ending at leaf x,
        # right[x] the last leaf of the one starting at x, until[x] the
        # lower end of the branch over the segment starting at x (the
        # present, for single leaves)
        left = np.tile(np.arange(n, dtype=np.int64), r)
        right = left.copy()
        until = np.repeat(cut[:, -1], n)
        first = np.empty((r, steps), dtype=np.int64)
        last = np.empty((r, steps), dtype=np.int64)
        len1 = np.empty((r, steps))
        len2 = np.empty((r, steps))
        for j in range(steps, 0, -1):              # ranks m..2, last cut first
            g = base + order[:, j - 1]
            t = cut[:, j]
            a = left.take(g)
            b = right.take(g + 1)
            len1[:, j - 1] = until.take(base + a) - t
            len2[:, j - 1] = until.take(g + 1) - t
            first[:, j - 1] = a
            last[:, j - 1] = b
            right[base + a] = b
            left[base + b] = a
            until[base + a] = t
        gap = order
        s1 = gap - first + 1
        s2 = last - gap
        side = np.where(last < k, k, n - k)
        w1 = s1 * (side - s1)
        w2 = s2 * (side - s2)
        np.maximum(len1, 0.0, out=len1)
        np.maximum(len2, 0.0, out=len2)
        total = (rng.poisson(mu * len1) * w1).sum(axis=1) + (rng.poisson(mu * len2) * w2).sum(axis=1)
        out[start:start + r] = total
    return out


def _slope(sorted_d, d, span=ASAP_SLOPE_SPAN):
    """How sharply the sorted distances rise around d (ASAP's gap width)."""
    nbv = len(sorted_d)
    i = int(np.searchsorted(sorted_d, d, side="left"))
    hi = int(np.searchsorted(sorted_d, d * (1.0 + span), side="right"))
    shift_h = hi - i
    d_h = float(sorted_d[min(hi, nbv - 1)])
    d_max = (sorted_d[i - 1] if i > 0 else sorted_d[i]) * (1.0 - span)
    p0 = int(np.searchsorted(sorted_d, d_max, side="left"))
    q = max(p0 - 1, 0) if p0 <= i else i
    shift_l = i - q
    d_l = float(sorted_d[q])
    if shift_h + shift_l == 0:
        return 0.0
    return (d_h - d_l) / ((1.0 + d_h + d_l) * (shift_h + shift_l))


def _mst_edges(D):
    """Minimum spanning tree (Prim): the merges of single-linkage clustering."""
    n = D.shape[0]
    D = D.astype(np.float64)
    in_tree = np.zeros(n, dtype=bool)
    in_tree[0] = True
    best = D[0].copy()
    parent = np.zeros(n, dtype=np.int64)
    best[0] = np.inf
    edges = []
    for _ in range(n - 1):
        cand = np.where(in_tree, np.inf, best)
        j = int(np.argmin(cand))
        edges.append((float(best[j]), int(parent[j]), j))
        in_tree[j] = True
        closer = (D[j] < best) & ~in_tree
        best[closer] = D[j][closer]
        parent[closer] = j
        best[j] = np.inf
    edges.sort()
    return edges


def asap(D, length, seed=1, max_replicates=ASAP_MAX_REPLICATES):
    """ASAP over a complete distance matrix (fractions) of sequences
    `length` sites long. Returns the partitions, best first, as dicts
    {rank, score, p, slope, threshold, distance, groups, groups_simple}, and
    how many were evaluated."""
    rng = np.random.default_rng(seed)
    n = D.shape[0]
    D64 = D.astype(np.float64)
    sorted_d = np.sort(D[np.triu_indices(n, 1)].astype(np.float64))

    # Nodes of the hierarchy: leaves 0..n-1, then one per group formed
    size = [1] * n
    children = [[] for _ in range(n)]
    level_of = [-1] * n
    parent = [None] * n
    pval = [-1.0] * n
    sum_all = [0.0] * n

    comp_node = list(range(n))       # union-find root -> its group's node
    uf = list(range(n))
    group_members = {i: [i] for i in range(n)}     # node -> sequences, current groups

    def find(x):
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    within_sum, within_n = 0.0, 0     # over all current groups
    n_groups = n
    partitions = []
    edges = _mst_edges(D)
    e = 0
    prev_d = 0.0
    while e < len(edges):
        d = edges[e][0]
        batch = []
        while e < len(edges) and edges[e][0] == d:
            batch.append(edges[e])
            e += 1
        level = len(partitions)
        groups_before = n_groups

        # Join this level's pairs; the groups each new one is made of
        roots_before = {}
        for _, a, b in batch:
            ra, rb = find(a), find(b)
            roots_before.setdefault(ra, comp_node[ra])
            roots_before.setdefault(rb, comp_node[rb])
            if ra != rb:
                uf[rb] = ra
                n_groups -= 1
        old_by_new = defaultdict(list)
        for root, node in roots_before.items():
            old_by_new[find(root)].append(node)

        S_tot, nb_tot = within_sum, within_n
        altered = []
        for new_root, olds in old_by_new.items():
            if len(olds) < 2:
                continue
            kids = olds
            kid_members = [group_members.pop(c) for c in kids]
            cross = 0.0
            for x in range(len(kids)):
                for y in range(x + 1, len(kids)):
                    cross += float(D64[np.ix_(kid_members[x], kid_members[y])].sum())
            node = len(size)
            sizes = [size[c] for c in kids]
            size.append(sum(sizes))
            children.append(kids)
            level_of.append(level)
            parent.append(None)
            pval.append(-1.0)
            intra = sum(sum_all[c] for c in kids)
            sum_all.append(intra + cross)
            for c in kids:
                parent[c] = node
            comp_node[new_root] = node
            group_members[node] = [x for m in kid_members for x in m]
            nb_inter = sum(sizes[x] * sizes[y] for x in range(len(sizes)) for y in range(x + 1, len(sizes)))
            nb_intra = sum(s * (s - 1) // 2 for s in sizes)
            altered.append({"node": node, "n": size[node], "k": max(sizes),
                            "sum_intra": intra, "nb_intra": nb_intra,
                            "sum_inter": cross, "nb_inter": nb_inter})
            within_sum += cross
            within_n += nb_inter
        if not altered:
            continue

        pi_intra_obs = S_tot / nb_tot if nb_tot else 0.0
        if level == 0:
            p = 1.0
            for a in altered:
                pval[a["node"]] = 1.0 if a["nb_intra"] == 0 else 0.0
        else:
            p = _test_level(altered, S_tot, nb_tot, pi_intra_obs, length, pval, rng, max_replicates)

        partitions.append({
            "level": level, "distance": d, "threshold": (d + prev_d) / 2.0,
            "groups_simple": groups_before, "p": p,
            "slope": _slope(sorted_d, d),
        })
        prev_d = d

    if not partitions:
        return [], 0

    # Ranks: p-values ascending (ties share the best rank), slopes descending
    ps = np.array([x["p"] for x in partitions])
    slopes = np.array([x["slope"] for x in partitions])
    rank_p = np.searchsorted(np.sort(ps), ps, side="left") + 1
    rank_s = np.argsort(np.argsort(-slopes, kind="stable"), kind="stable") + 1
    for x, rp, rs in zip(partitions, rank_p, rank_s):
        x["score"] = float(rs * (1 - ASAP_P_WEIGHT) + rp * ASAP_P_WEIGHT)
    order = sorted(range(len(partitions)),
                   key=lambda i: (partitions[i]["score"], partitions[i]["p"], -partitions[i]["slope"]))

    best = []
    for rank, i in enumerate(order[:ASAP_REPORTED], 1):
        x = partitions[i]
        groups = []
        for node in _groups_before(x["level"], size, level_of, parent):
            for g in _split_again(node, children, pval):
                groups.append(sorted(_leaves(g, children)))
        groups.sort(key=lambda g: g[0])
        best.append({**x, "rank": rank, "groups": groups})
    return best, len(partitions)


def _leaves(node, children):
    out, stack = [], [node]
    while stack:
        x = stack.pop()
        if children[x]:
            stack.extend(children[x])
        else:
            out.append(x)
    return out


def _groups_before(level, size, level_of, parent):
    """The groups just before the merges of `level`: nodes formed earlier
    whose parent (if any) formed at that level or later."""
    return [x for x in range(len(size))
            if level_of[x] < level and (parent[x] is None or level_of[parent[x]] >= level)]


def _split_again(node, children, pval):
    """ASAP's recursion: a group formed with a significant p-value is split
    into the groups it was formed from, and so on down."""
    out, stack = [], [node]
    while stack:
        x = stack.pop()
        if children[x] and 0 <= pval[x] <= ASAP_SPLIT_P:
            stack.extend(children[x])
        else:
            out.append(x)
    return out


def _test_level(altered, S_tot, nb_tot, pi_intra_obs, length, pval, rng, max_replicates):
    """p-value of a partition (and of each group it merges): how often a
    single coalescent species, as diverse between the groups merged as the
    data, shows within them as little diversity as observed."""
    sims = []
    for a in altered:
        n_, k = a["n"], a["k"]
        pi_inter = a["sum_inter"] / a["nb_inter"] if a["nb_inter"] else 0.0
        theta = length * pi_inter * n_ / (2.0 * (n_ - 1.0))
        sims.append({
            **a, "mu": theta / 2.0,
            "obs": (a["sum_intra"] / a["nb_intra"] if a["nb_intra"] else 0.0) * length,
            "n_theo": k * (k - 1) // 2 + (n_ - k) * (n_ - k - 1) // 2,
            "succ": 0, "active": True,
        })
    S_alt = sum(a["sum_intra"] for a in altered)
    nb_alt = sum(a["nb_intra"] for a in altered)
    part_open, part_succ, part_reps = True, 0, 0
    done, batch = 0, ASAP_FIRST_REPLICATES
    target = pi_intra_obs * length
    while done < max_replicates:
        b = min(batch, max_replicates - done)
        active = [s for s in sims if s["active"]]
        if not active:
            break
        tot_S = np.zeros(b)
        for s in active:
            S = _intra_replicates(s["n"], s["k"], s["mu"], b, rng)
            if s["nb_intra"] > 0:
                theo = S / s["n_theo"] if s["n_theo"] else np.zeros(b)
                s["succ"] += int(np.count_nonzero(theo - _EPS <= s["obs"]))
            tot_S += S
        if part_open:
            nb = sum(s["n_theo"] for s in sims) + nb_tot - nb_alt
            if nb > 0:
                pi = (tot_S + (S_tot - S_alt) * length) / nb
                part_succ += int(np.count_nonzero(pi - _EPS <= target))
            part_reps += b
        done += b
        if part_succ >= ASAP_MIN_SUCCESS:
            for s in sims:
                if s["active"] and s["nb_intra"] > 0 and s["succ"] >= ASAP_MIN_SUCCESS:
                    s["active"] = False
                    s["p"] = (s["succ"] + 1) / (done + 1.0)
                    part_open = False
        if not part_open:
            # Groups made of single sequences have nothing to test
            for s in sims:
                if s["active"] and s["nb_intra"] == 0:
                    s["active"] = False
        batch *= 2
    for s in sims:
        if s["nb_intra"] == 0:
            pval[s["node"]] = 1.0
        else:
            pval[s["node"]] = s.get("p", (s["succ"] + 1) / (done + 1.0))
    return (part_succ + 1) / (part_reps + 1.0)


# ── Groups against names and BINs ─────────────────────────────────────────────

# Names that don't identify a species (as in the identification check)
_PROVISIONAL = re.compile(r"\b(sp|spp|cf|aff|nr|gr|complex)\b\.?|\d", re.IGNORECASE)


# Species statuses, problems first
_STATUS_ORDER = ["split_merged", "merged", "split", "match"]


def concordance(motu, species, bins):
    """Each named species against the MOTUs of a partition: 'match' (one
    MOTU, all its own), 'split' (several MOTUs), 'merged' (a MOTU shared
    with other species) or 'split_merged'. Also how many MOTUs coincide
    exactly with a BIN."""
    names = ["" if not sp or _PROVISIONAL.search(sp) else sp for sp in species]
    motus_of = defaultdict(set)
    species_of = defaultdict(set)
    count = defaultdict(int)
    bins_of = defaultdict(set)
    for m, sp, b in zip(motu, names, bins):
        if sp:
            motus_of[sp].add(m)
            species_of[m].add(sp)
            count[sp] += 1
            if b:
                bins_of[sp].add(b)
    rows = []
    for sp, ms in motus_of.items():
        split = len(ms) > 1
        merged = any(len(species_of[m]) > 1 for m in ms)
        status = "split_merged" if split and merged else "split" if split else "merged" if merged else "match"
        others = sorted({o for m in ms for o in species_of[m]} - {sp})
        rows.append({"species": sp, "n": count[sp], "motus": sorted(ms), "bins": sorted(bins_of[sp]),
                     "with": others, "status": status})
    rows.sort(key=lambda r: (_STATUS_ORDER.index(r["status"]), r["species"]))

    # MOTU = BIN: the MOTU's records with a BIN all share it, and that
    # BIN's records are all in the MOTU
    bin_motus = defaultdict(set)
    motu_bins = defaultdict(set)
    for m, b in zip(motu, bins):
        if b:
            bin_motus[b].add(m)
            motu_bins[m].add(b)
    same_bin = sum(1 for m, bs in motu_bins.items()
                   if len(bs) == 1 and bin_motus[next(iter(bs))] == {m})
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in _STATUS_ORDER}
    summary.update(species=len(rows), motus=len(set(motu)), motus_with_bin=len(motu_bins),
                   motus_equal_bin=same_bin, bins=len(bin_motus))
    return summary, rows


def labels_of(groups, n):
    """MOTU number (1, 2, ...) of each index, from a list of groups, numbered
    by size (largest first) and then by first member."""
    order = sorted(range(len(groups)), key=lambda i: (-len(groups[i]), groups[i][0]))
    label = [0] * n
    for number, i in enumerate(order, 1):
        for x in groups[i]:
            label[x] = number
    return label
