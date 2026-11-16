"""Junk units of a long-strip chapter (SPEC "Junk decisions": units, decisions). Ported from the bulk converter's
optimizer/strip_junk.py (edge blocks, match_blocks, the tail pass, the join rule, the kept check) and the strip steps
of its planner (plan_scan.strip_chapter, plan_review.tail_review / finish_strip), reshaped to 1 chapter:

- `scan(strip, ocr_text)` decodes once and returns a JSON-able scan holding everything `decide` reads: the gaps and
  spans, every edge block with its hard-join pieces and their remainders (signature, OCR text when given), the
  measurements of every content block of the tail region and every remainder (the tail pieces are cut from them),
  and the signatures of the middle blocks (the watermark rule).
- `decide(scan, decide_unit, known)` applies decisions with no decode: what is removed (edge blocks, pieces,
  remainders, tail pieces, join-rule blank rows), what is left undecided (edge blocks with their probes, tail
  pieces), and the keys of the decisions that acted as watermarks (the series remembers them: `known`).

Not ported (SPEC "Junk decisions", D5.1): chains, cut rows and card holes; the rest-of-card bundles (each tail piece
is its own unit). The watermark rule reads this chapter and the watermark keys the series' earlier chapters showed
(the old one read every chapter of the series at once).
Constants are the bulk converter's, frozen: the stored decisions were made with them.
"""

import itertools
from collections import Counter

import numpy as np

from manga_pipeline import images, strip

EDGE_PX = 6000  # edge region: the first and last rows of a strip whose blocks are junk units (old junk_edge_px)
MAX_SEAM = strip.SLICE_REPAIR["max_seam_score"]  # hard-join threshold (slice_repair.max_seam_score)
BLANK_STD = 3  # W1: a tail piece with luma std below this ...
BLANK_INK = 40  # ... and no pixel further than this from its median is blank (strip_tail.blank_std / blank_ink)
WEDGE_PX = 130  # W3: a piece (with gutters) shorter than this between 2 credit bands is dropped (strip_tail.wedge_px)
MIN_PAGE = strip.RESTITCH["min_page"]  # kept check: a strip at least this long is never cut below it
MARK_MIN_BANDS = 2  # tail bands sharing 1 decision that make it a mark


def signable(im):
    """The signature text of an image, or None when it is uniform or unsignable."""
    if images.is_uniform(im):
        return None
    sig = images.signature(im)
    return None if images.unsignable(sig) else images.sig_to_str(sig)


def measure_piece(im):
    """What the tail rules read of a gap-free run: luma std, ink (largest luma distance from its median), uniform,
    unsignable, signature text (None when uniform or unsignable)."""
    g = np.asarray(im.convert("L"), np.int16)
    uniform = images.is_uniform(im)
    sig = None if uniform else images.signature(im)
    unsignable = sig is not None and images.unsignable(sig)
    return {
        "std": float(g.std()),
        "ink": int(np.abs(g - np.median(g)).max()),
        "uniform": uniform,
        "unsignable": unsignable,
        "sig": None if sig is None or unsignable else images.sig_to_str(sig),
    }


# ---------------------------------------------------------------- the scan, which decodes


def scan(st, ocr_text=None, undecided=lambda _sig: True):
    """The scan of a Strip (strip.make_strip over the chapter's non-cover pages). ocr_text(image) -> text, called for
    every edge block and non-uniform hard-join piece whose signature `undecided(sig)` (no stored decision) says so;
    None: no OCR. Returns a JSON-able dict."""
    gaps = strip.find_gaps(st)
    spans = strip.spans(st)
    blocks_all = strip.content_blocks(st, gaps)
    cache = {}

    def render(a, b):
        return strip.render_rows(st.pages, st.width, a, b, cache)

    def unit(s, e, im):
        sig = images.signature(im)
        u = {"start": s, "end": e, "sig": images.sig_to_str(sig)}
        if ocr_text is not None and undecided(sig):
            u["text"] = ocr_text(im)
        return u

    edge, measured = [], {}
    for s, e in blocks_all:
        where = strip.edge_region(s, e, st.height, EDGE_PX)
        if where is None:
            continue
        im = render(s, e)
        if where == "tail":
            measured[f"{s}:{e}"] = measure_piece(im)
        if e - s < strip.MIN_EDGE_BLOCK_PX or images.is_uniform(im):
            continue
        joins = []
        for k, piece in strip.edge_pieces(im, where, MAX_SEAM):
            rest = im.crop((0, k, im.width, im.height) if where == "head" else (0, 0, im.width, k))
            (a, b), (x, y) = ((s, s + k), (s + k, e)) if where == "head" else ((s + k, e), (s, s + k))
            uniform = images.is_uniform(piece)
            join = (
                {"start": a, "end": b, "sig": images.sig_to_str(images.signature(piece))}
                if uniform
                else unit(a, b, piece)
            )
            r = None
            if where == "tail":
                measured[f"{x}:{y}"] = measure_piece(rest)
            if not images.is_uniform(rest):
                r = {"start": x, "end": y, "sig": images.sig_to_str(images.signature(rest))}
            joins.append({**join, "uniform": uniform, "rest": r})
        edge.append({**unit(s, e, im), "where": where, "joins": joins})
    tallest = EDGE_PX / (1 - images.ASPECT_TOLERANCE) ** 2  # a taller block can match no tail band
    middle = []
    for s, e in blocks_all:
        if e - s < strip.MIN_EDGE_BLOCK_PX or e - s > tallest or e <= EDGE_PX or s >= st.height - EDGE_PX:
            continue
        im = render(s, e)
        if not images.is_uniform(im):
            middle.append(images.sig_to_str(images.signature(im)))
    return {
        "layout": "strip",
        "width": st.width,
        "height": st.height,
        "gaps": [list(g) for g in gaps],
        "spans": [list(s) for s in spans],
        "blocks": edge,
        "measured": measured,
        "middle": middle,
    }


# ---------------------------------------------------------------- decide (no decode)


class Layout:
    """The rows of a scanned strip: height, width, spans and gaps (sorted)."""

    def __init__(self, sc):
        self.height, self.width = sc["height"], sc["width"]
        self.spans = sorted(tuple(s) for s in sc["spans"])
        self.gaps = sorted(tuple(g) for g in sc["gaps"])

    def runs(self):
        """[(start, end)] of the walkable runs between and around the spans (0-row ones included)."""
        out, pos = [], 0
        for s, e in self.spans:
            out.append((pos, s))
            pos = e
        return [*out, (pos, self.height)]

    def blank(self, a, b):
        """True when rows [a, b) hold no span row."""
        return not any(s < b and a < e for s, e in self.spans)


def uncovered(a, b, taken):
    """The parts of [a, b) outside every (start, end) in taken, in order."""
    out, pos = [], a
    for s, e in sorted(taken):
        if e <= pos or s >= b:
            continue
        if s > pos:
            out.append((pos, s))
        pos = max(pos, e)
    if pos < b:
        out.append((pos, b))
    return out


def _hits(a, b, taken):
    return any(s < b and a < e for s, e in taken)


def match_blocks(blocks, approve):
    """Stored decisions on the edge blocks: (removed [{"start", "end", "where", "decision", "why": "match"}], left: the
    edge blocks no junk decision removed, each with its non-uniform pieces as "probes"). approve(kind, sig text) -> the
    deciding junk row or None. A block matching whole is removed; else its hard-join pieces are tried from the chapter
    edge inward and the first match is removed, then its remainder (removed when it matches, else left as a block
    without probes, marked "rest": 1 cut per block); else the block is left."""
    removed, left = [], []

    def gone(u, row, where):
        removed.append({"start": u["start"], "end": u["end"], "where": where, "decision": row, "why": "match"})

    for b in blocks:
        row = approve("block", b["sig"])
        if row is not None:
            gone(b, row, b["where"])
            continue
        split = next(((j, h) for j in b["joins"] for h in [approve("probe", j["sig"])] if h is not None), None)
        if split is None:
            probes = [
                {k: v for k, v in j.items() if k not in ("uniform", "rest")} for j in b["joins"] if not j["uniform"]
            ]
            left.append({**{k: v for k, v in b.items() if k != "joins"}, "probes": probes})
            continue
        j, row = split
        gone(j, row, b["where"])
        r = j["rest"]
        if r is None:
            continue
        row = approve("block", r["sig"])
        if row is not None:
            gone(r, row, b["where"])
        else:
            left.append({**r, "where": b["where"], "probes": [], "rest": True})
    return removed, left


def watermarks(removed, middle, known=()):
    """The decision ids of this chapter's removed tail bands that are watermarks, not credits: removed as >= 2 tail
    bands, or whose key matches a middle block of the chapter (an unsignable one matches nothing) or one of `known`,
    the keys (parsed) the series' earlier chapters showed to be watermarks. removed: [{"where", "decision" (id),
    "key"}]."""
    bands = [r for r in removed if r["where"] == "tail"]
    marks = {d for d, n in Counter(r["decision"] for r in bands).items() if n >= MARK_MIN_BANDS}
    sigs = [s for s in map(images.sig_from_str, middle) if not images.unsignable(s)] + list(known)
    for r in bands:
        if r["decision"] not in marks and any(images.matches(images.sig_from_str(r["key"]), s) for s in sigs):
            marks.add(r["decision"])
    return marks


def tail_pieces(layout, removed, start):
    """The tail pieces from the credit start: the kept rows from `start` to the strip end split at every gap,
    [{"start", "end", "gap"}] in order."""
    out = []
    for s, e in uncovered(start, layout.height, [(r["start"], r["end"]) for r in removed]):
        pos = s
        for gs, ge in layout.gaps:
            x, y = max(gs, s), min(ge, e)
            if x >= y:
                continue
            if x > pos:
                out.append({"start": pos, "end": x, "gap": False})
            out.append({"start": x, "end": y, "gap": True})
            pos = y
        if pos < e:
            out.append({"start": pos, "end": e, "gap": False})
    return out


def is_blank(p):
    """W1: a gap, all blank rows, uniform, unsignable, or flat (luma std < BLANK_STD and ink <= BLANK_INK)."""
    return p["gap"] or p["blank"] or p["uniform"] or p["unsignable"] or (p["std"] < BLANK_STD and p["ink"] <= BLANK_INK)


def tail_classes(pieces, bands, others):
    """{index: "blank" | "wedge" | "review"}: W1 blank; W3 wedge (a non-blank piece whose height plus its adjacent gap
    pieces is under WEDGE_PX and whose nearest non-blank neighbours on both sides are credit bands); review (the rest:
    decided by piece decisions, else a unit). bands, others: [(start, end)] credit bands, every other removed
    range."""
    out = {i: "blank" for i, p in enumerate(pieces) if is_blank(p)}
    seq = sorted(
        [(s, e, "band") for s, e in bands]
        + [(s, e, "other") for s, e in others]
        + [(p["start"], p["end"], "blank" if i in out else "piece") for i, p in enumerate(pieces)]
    )

    def nearest(k, step):
        k += step
        while 0 <= k < len(seq) and seq[k][2] == "blank":
            k += step
        return seq[k][2] if 0 <= k < len(seq) else None

    for i, p in enumerate(pieces):
        if i in out:
            continue
        k = seq.index((p["start"], p["end"], "piece"))
        gutters = sum(
            q["end"] - q["start"] for q in pieces if q["gap"] and (q["end"] == p["start"] or q["start"] == p["end"])
        )
        short = p["end"] - p["start"] + gutters < WEDGE_PX
        out[i] = "wedge" if short and nearest(k, -1) == "band" and nearest(k, 1) == "band" else "review"
    return out


def _merge(rows):
    out = []
    for s, e in sorted(rows):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def soft(layout, r):
    """A W1 drop of a gap piece (its rows within 1 gap): the join rule counts its rows as blank rows."""
    return r["why"] == "blank" and any(s <= r["start"] and r["end"] <= e for s, e in layout.gaps)


def join_rule(layout, removed, gutter):
    """The blank rows a chapter's removals leave adjacent: at each join (removed ranges separated only by blank rows)
    the walkable runs touching it on both sides, the blank rows between its ranges and the rows of its soft ranges
    form 1 run C: at a strip end C goes whole; C >= gutter: C - gutter goes, gutter kept in its middle; C < gutter: C
    goes whole. Returns the ranges it removes ({"why": "join"}), sorted."""
    rs = sorted((r["start"], r["end"], r["where"], soft(layout, r)) for r in removed)
    merged = []  # [start, end, where, [rows a non-soft range removes]]
    for s, e, w, f in rs:
        hard = [] if f else [(s, e)]
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][3] = _merge(merged[-1][3] + hard)
        else:
            merged.append([s, e, w, hard])
    groups = []
    for m in merged:
        if groups and layout.blank(groups[-1][-1][1], m[0]):
            groups[-1].append(m)
        else:
            groups.append([m])
    runs, out = layout.runs(), []
    for g in groups:
        start, end, where = g[0][0], g[-1][1], g[0][2]
        before = next(((rs_, start) for rs_, re_ in runs if rs_ < start <= re_), None)
        after = next(((end, re_) for rs_, re_ in runs if rs_ <= end < re_), None)
        hard = [x for m in g for x in m[3]]
        c = _merge(
            ([before] if before else [])
            + [(x[1], y[0]) for x, y in itertools.pairwise(g) if y[0] > x[1]]
            + [x for m in g for x in uncovered(m[0], m[1], hard)]
            + ([after] if after else [])
        )
        total = sum(y - x for x, y in c)
        if not total:
            continue
        at_end = (
            start == 0 or end == layout.height or (before and before[0] == 0) or (after and after[1] == layout.height)
        )
        keep = None
        if not at_end and total >= gutter:
            k0 = (total - gutter) // 2
            keep = (k0, k0 + gutter)
        off = 0
        for x, y in c:
            parts = [(x, y)] if keep is None else uncovered(x, y, [(x + keep[0] - off, x + keep[1] - off)])
            out += [{"start": s, "end": t, "where": where, "decision": None, "why": "join"} for s, t in parts if t > s]
            off += y - x
    return sorted(out, key=lambda r: r["start"])


def settle(layout, removed, gutter_px):
    """The removed set after the join rule: the removed ranges but the soft ones, plus join_rule's ranges, sorted."""
    return sorted(
        [r for r in removed if not soft(layout, r)] + join_rule(layout, removed, gutter_px), key=lambda r: r["start"]
    )


def gutter(width):
    """1 gutter in px at strip width (gap_min_rows is given at 1000 px width)."""
    return max(1, round(strip.RESTITCH["gap_min_rows"] * width / 1000))


def kept_problem(layout, removed):
    """Why cutting `removed` out is refused (only blank rows kept, or under MIN_PAGE from a strip at least that long),
    or None."""
    kept = uncovered(0, layout.height, [(r["start"], r["end"]) for r in removed])
    length = sum(e - s for s, e in kept)
    blank = all(layout.blank(s, e) for s, e in kept)
    if (length < MIN_PAGE <= layout.height) or blank:
        return (
            f"junk removal would leave {length} px of the {layout.height} px strip"
            f"{' (only blank rows)' if blank else ''} (minimum {MIN_PAGE})"
        )
    return None


def decide(sc, decide_unit, known=()):
    """Apply decisions to a strip scan. decide_unit(kind, sig text) -> the deciding row (dict-like with "id",
    "verdict", "key") or None; known: the series' watermark keys (parsed). Returns {"removed": final ranges (after
    the join rule), "undecided": [{"kind", "start", "end", "where", "sig", "text"?, "probes"?}], "kept_problem":
    text or None, "watermarks": the keys of the decisions that removed a watermark here}."""
    layout = Layout(sc)

    def approve(kind, sig):
        row = decide_unit(kind, sig)
        return row if row is not None and row["verdict"] == "junk" else None

    found, left = match_blocks(sc["blocks"], approve)
    removed = [{**r, "decision": r["decision"]["id"], "key": r["decision"]["key"]} for r in found]
    marks = watermarks(removed, sc["middle"], known)
    bands = [r for r in removed if r["where"] == "tail" and r["decision"] not in marks]
    drops, review = [], []
    pieces = []
    if bands:
        start = min(r["start"] for r in bands)
        cut = tail_pieces(layout, removed, start)
        for p in cut:
            p["blank"] = p["gap"] or layout.blank(p["start"], p["end"])
            if not p["blank"]:
                m = sc["measured"].get(f"{p['start']}:{p['end']}")
                if m is None:
                    msg = f"tail piece {p['start']}-{p['end']} was not measured by the scan"
                    raise ValueError(msg)
                p.update(m)
            pieces.append(p)
        classes = tail_classes(
            pieces, [(r["start"], r["end"]) for r in bands], [(r["start"], r["end"]) for r in removed if r not in bands]
        )
        for i, why in sorted(classes.items()):
            p = pieces[i]
            rng = {"start": p["start"], "end": p["end"], "where": "tail", "decision": None}
            if why != "review":
                drops.append({**rng, "why": why})
                continue
            row = decide_unit("piece", p["sig"])
            if row is None:
                review.append({"kind": "piece", "start": p["start"], "end": p["end"], "where": "tail", "sig": p["sig"]})
            elif row["verdict"] == "junk":
                drops.append({**rng, "decision": row["id"], "why": "piece"})
    everything = sorted(removed + drops, key=lambda r: r["start"])
    owned = [(r["start"], r["end"]) for r in everything] + [(p["start"], p["end"]) for p in pieces]
    undecided = list(review)
    for b in left:
        if _hits(b["start"], b["end"], owned):
            continue
        if decide_unit("block", b["sig"]) is None:
            # a probe decided keep stays: it is no longer offered
            probes = [p for p in b["probes"] if decide_unit("probe", p["sig"]) is None]
            undecided.append({**b, "kind": "block", "probes": probes})
    final = settle(layout, everything, gutter(layout.width))
    return {
        "removed": [{"start": r["start"], "end": r["end"], "why": r["why"]} for r in final],
        "undecided": sorted(undecided, key=lambda u: u["start"]),
        "kept_problem": kept_problem(layout, final) if final else None,
        "watermarks": sorted({r["key"] for r in removed if r["decision"] in marks}),
    }
