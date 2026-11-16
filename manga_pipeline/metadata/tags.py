"""A series' genres and tags from its stored source records (SPEC "Tag rules"). Pure: the same records and rule files
always give the same lists, with no source request.

Ported from the old metadata tool (`kavita_enrich.py` build_metadata, consolidate, derive_tags, top_up). One change
(decision D7.6): a top-up adds only consolidation targets and allowlist entries, never a name because other library
series use it, so a series' tags never depend on the order series are written.

Order: the sources' lists, the consolidation table, derived and promoted tags, the top-up.
"""

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from manga_pipeline import names

RULES = Path(__file__).with_name("tag_rules")
FILES = ("consolidation.csv", "derived_tags.yaml", "promote_tags.txt", "tag_allowlist.txt", "top_up_exclude.txt")
MIN_GENRES = 4  # a top-up brings a series up to these
MIN_TAGS = 6
ANILIST_MIN_TAG_RANK = 60
MANGABAKA_SPARSE_TAGS = 10  # fewer MangaBaka tags than this: MangaUpdates' categories join the rule terms
TOP_UP_WEIGHTS = ("core", "defining", "recurrent", "unweighted")
PAST_MIN_WEIGHTS = ("core", "defining", "recurrent")  # only these are added past the minimum
RULE_WEIGHTS = ("core", "defining", "recurrent", "incidental")
SMALL_WORDS = {"a", "an", "and", "as", "at", "for", "in", "of", "on", "or", "the", "to"}
PLURAL_MARKER = re.compile(r"/(?:ies|ves|e?s)\b", re.IGNORECASE)  # MangaUpdates' "Concubine/s", "Elf/ves"
SHORT_KEY_MAX = 4  # a key this short keeps its final "s"


@dataclass(frozen=True)
class Rules:
    table: dict  # {(kind, term key): (to kind, to name)}; to kind "remove" drops it
    derived: list  # derived and promoted tags: [{"tag", "kind", "rules": [{"all", "none", "min_weight"?}]}]
    allow_genres: dict  # {term key: name} a top-up may add
    allow_tags: dict
    always_tags: frozenset  # allowlisted tags: added past MIN_TAGS too, at a strong weight
    exclude: frozenset  # term keys a top-up never adds
    fingerprint: str  # sha256 of the rule files and of the code that applies them


def load(folder=RULES):
    """The rules of the rule files in folder. A file that is not valid raises ValueError naming it and the line or
    entry."""
    data = {name: (folder / name).read_bytes() for name in FILES}
    code = [Path(__file__).read_bytes(), Path(names.__file__).read_bytes()]  # this module and the name key it uses
    parts = [name.encode() + b"\0" + data[name] + b"\0" for name in FILES] + code
    fingerprint = hashlib.sha256(b"".join(parts)).hexdigest()
    text = {name: raw.decode("utf-8") for name, raw in data.items()}
    table = _consolidation(text["consolidation.csv"])
    try:
        entries = yaml.safe_load(text["derived_tags.yaml"]) or []
    except yaml.YAMLError as e:
        msg = f"derived_tags.yaml: {e}"
        raise ValueError(msg) from None
    derived = _derived(entries, table) + _promoted(_lines(text["promote_tags.txt"]), table)
    allowlisted = _lines(text["tag_allowlist.txt"])
    allow_genres, allow_tags = {}, {}
    for to_kind, to in table.values():
        if to_kind in ("genre", "tag"):
            (allow_genres if to_kind == "genre" else allow_tags).setdefault(term_key(to), to)
    for tag in allowlisted:
        allow_tags.setdefault(term_key(tag), tag)
    return Rules(
        table=table,
        derived=derived,
        allow_genres=allow_genres,
        allow_tags=allow_tags,
        always_tags=frozenset(term_key(t) for t in allowlisted),
        exclude=frozenset(term_key(t) for t in _lines(text["top_up_exclude.txt"])),
        fingerprint=fingerprint,
    )


def compute(records, rules):
    """(genres, tags) of a series from its records ({(source, kind): payload or None}, as a check stores them)."""
    mb, mu = records.get(("mangabaka", "series")), records.get(("mangaupdates", "series"))
    md, al = records.get(("mangadex", "manga")), records.get(("anilist", "media"))
    genres, tags = _sourced(mb, mu, md, al)
    genres, tags = _consolidate(genres, tags, rules.table)
    mb_tags, other_terms = _rule_terms(mb, mu)
    derived = _derive(mb_tags + other_terms, rules, _term_weights(mb, other_terms, rules.table))
    genres = _unique(genres + [n for n, kind in derived if kind == "genre"])
    genre_keys = {term_key(g) for g in genres}
    tags = [t for t in _unique(tags + [n for n, kind in derived if kind == "tag"]) if term_key(t) not in genre_keys]
    add_g, add_t = _top_up(genres, tags, mb, rules, ())
    if mu and (len(genres) + len(add_g) < MIN_GENRES or len(tags) + len(add_t) < MIN_TAGS):
        add_g, add_t = _top_up(genres, tags, mb, rules, _mu_categories(mu))
    return genres + add_g, tags + add_t


# --- names


def display_name(name):
    """A source's name without MangaUpdates' plural marker ("Concubine/s" -> "Concubine")."""
    return PLURAL_MARKER.sub("", name or "").strip()


def term_key(term):
    """The key a tag or genre name matches by: plural markers and a final "s" dropped, case and punctuation ignored."""
    key = names.key(display_name(term))
    return key[:-1] if len(key) > SHORT_KEY_MAX and key.endswith("s") else key


def _unique(values):
    """Blanks and duplicates (by names.key) dropped, the first spelling kept."""
    seen, out = set(), []
    for value in values:
        key = names.key(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _canonical(name, kind, table):
    """The (kind, name) a source's name converts to, or None when the table removes it."""
    key = term_key(name)
    hit = table.get((kind, key)) or table.get(("tag" if kind == "genre" else "genre", key))
    if hit:
        return None if hit[0] == "remove" else hit
    return kind, display_name(name)


def _canonical_key(name, table):
    found = _canonical(name, "tag", table)
    return term_key(found[1]) if found else None


# --- rule files


def _consolidation(text):
    reader = csv.DictReader(io.StringIO(text))
    missing = {"from_kind", "from", "to_kind", "to"} - set(reader.fieldnames or ())
    if missing:
        msg = f"consolidation.csv: no column {', '.join(sorted(missing))}"
        raise ValueError(msg)
    table = {}
    for row in reader:
        kind, name = (row["from_kind"] or "").strip().lower(), (row["from"] or "").strip()
        to_kind, to = (row["to_kind"] or "").strip().lower(), (row["to"] or "").strip()
        if kind not in ("genre", "tag") or not name or to_kind not in ("genre", "tag", "remove"):
            msg = f"consolidation.csv line {reader.line_num}: not a genre or tag rule"
            raise ValueError(msg)
        if to_kind != "remove" and not to:
            msg = f"consolidation.csv line {reader.line_num}: no name to convert {name!r} to"
            raise ValueError(msg)
        table[(kind, term_key(name))] = (to_kind, to)
    return table


def _lines(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _derived(entries, table):
    def keys(terms, tag):
        if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
            msg = f"derived_tags.yaml: {tag}: not a list of names: {terms!r}"
            raise ValueError(msg)
        return {k for k in (_canonical_key(t, table) for t in terms) if k}

    if not isinstance(entries, list):
        msg = "derived_tags.yaml: not a list of entries"
        raise ValueError(msg)
    derived = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"tag", "kind", "rules"} or not entry.get("tag"):
            msg = f"derived_tags.yaml: not an entry of tag, kind and rules: {entry!r}"
            raise ValueError(msg)
        tag, kind, rules = entry["tag"], entry.get("kind") or "tag", entry.get("rules")
        if not isinstance(tag, str) or not isinstance(kind, str) or kind.lower() not in ("tag", "genre"):
            msg = f"derived_tags.yaml: {tag!r}: tag must be a name and kind tag or genre"
            raise ValueError(msg)
        kind = kind.lower()
        if not isinstance(rules, list) or not rules:
            msg = f"derived_tags.yaml: {tag}: needs a list of rules"
            raise ValueError(msg)
        parsed = []
        for rule in rules:
            groups = rule.get("all") if isinstance(rule, dict) else None
            if set(rule if isinstance(rule, dict) else ()) - {"all", "none"} or not isinstance(groups, list):
                msg = f"derived_tags.yaml: {tag}: a rule is all (a list of groups) and none: {rule!r}"
                raise ValueError(msg)
            if not groups or not all(isinstance(g, list) and g for g in groups):
                msg = f"derived_tags.yaml: {tag}: a rule needs at least 1 group, each with a term: {rule!r}"
                raise ValueError(msg)
            parsed.append(
                {"all": [keys(group, tag) for group in rule["all"]], "none": keys(rule.get("none") or [], tag)}
            )
        derived.append({"tag": tag, "kind": kind, "rules": parsed})
    return derived


def _promoted(lines, table):
    """Promoted tags as 1-term derived rules; "Tag: weight" also needs MangaBaka to weight it at least that."""
    promoted = []
    for line in lines:
        name, _, min_weight = (p.strip() for p in line.partition(":"))
        if min_weight and min_weight.lower() not in RULE_WEIGHTS:
            msg = f"promote_tags.txt: {line!r}: the weight is not one of {', '.join(RULE_WEIGHTS)}"
            raise ValueError(msg)
        found = _canonical(name, "tag", table)
        if found:
            rule = {"all": [{term_key(found[1])}], "none": set()}
            if min_weight:
                rule["min_weight"] = (term_key(found[1]), RULE_WEIGHTS.index(min_weight.lower()))
            promoted.append({"tag": found[1], "kind": found[0], "rules": [rule]})
    return promoted


# --- sources


def _mangabaka_genres(mb):
    """MangaBaka's genre slugs ("martial_arts", "sci-fi") as names ("Martial Arts", "Sci-Fi")."""

    def title(slug):
        words = slug.replace("_", " ").split()
        return " ".join(
            w if i and w in SMALL_WORDS else "-".join(p.capitalize() for p in w.split("-")) for i, w in enumerate(words)
        )

    return [title(g) for g in mb.get("genres") or []]


def _mu_genres(mu):
    return [g["genre"] for g in mu.get("genres") or []]


def _mu_categories(mu):
    return [c["category"] for c in mu.get("categories") or []]


def _sourced(mb, mu, md, al):
    """The sources' own lists: genres from MangaBaka (else MangaUpdates), AniList and MangaDex's genre tags; tags from
    AniList (rank at least 60, no spoiler) and MangaDex's theme tags, less any that is a genre."""
    md_tags = [
        (t["attributes"]["group"], t["attributes"]["name"].get("en"))
        for t in ((md or {}).get("attributes") or {}).get("tags") or []
    ]
    genres = _unique(
        (_mangabaka_genres(mb) if mb else _mu_genres(mu or {}))
        + ((al or {}).get("genres") or [])
        + [n for group, n in md_tags if group == "genre" and n]
    )
    genre_keys = {names.key(g) for g in genres}
    al_tags = [
        t["name"]
        for t in (al or {}).get("tags") or []
        if (t.get("rank") or 0) >= ANILIST_MIN_TAG_RANK and not t.get("isMediaSpoiler")
    ]
    tags = [
        t for t in _unique(al_tags + [n for g, n in md_tags if g == "theme" and n]) if names.key(t) not in genre_keys
    ]
    return genres, tags


def _consolidate(genres, tags, table):
    """Both lists through the table; a tag that ends up equal to a genre is dropped."""
    out = {"genre": [], "tag": []}
    for kind, values in (("genre", genres), ("tag", tags)):
        for value in values:
            found = _canonical(value, kind, table)
            if found:
                out[found[0]].append(found[1])
    new_genres = _unique(out["genre"])
    genre_keys = {term_key(g) for g in new_genres}
    return new_genres, [t for t in _unique(out["tag"]) if term_key(t) not in genre_keys]


def _rule_terms(mb, mu):
    """(MangaBaka's tags, the other terms) derived-tag rules match. MangaBaka's tags carry weights; the other terms
    carry none to judge: MangaBaka's genres, status, content rating and type, MangaUpdates' categories and genres when
    MangaBaka's tags are sparse, and MangaUpdates' terms and type alone without MangaBaka."""
    mu_terms = (_mu_categories(mu) + _mu_genres(mu)) if mu else []
    if not mb:
        return [], mu_terms + ([f"Type: {mu['type']}"] if mu and mu.get("type") else [])
    tags = [t["name"] for t in mb.get("tags_v2") or []]
    other = list(mb.get("genres") or [])
    other += [f"Status: {mb['status']}"] if mb.get("status") else []
    other += [f"Content Rating: {mb['content_rating']}"] if mb.get("content_rating") else []
    other += [f"Type: {mb['type']}"] if mb.get("type") else []
    if len(tags) < MANGABAKA_SPARSE_TAGS:
        other += mu_terms
    return tags, other


def _term_weights(mb, other_terms, table):
    """{term key: rank in RULE_WEIGHTS}: each MangaBaka tag's best weight; another term ranks 0 unless it is spelt
    exactly as a MangaBaka tag. MangaBaka carries MangaUpdates' categories and weights the low-vote ones incidental, so
    a raw category it repeats keeps MangaBaka's weight (as the old tool did)."""
    if not mb:
        return {}
    ranks = {}
    tag_names = set()
    for tag in mb.get("tags_v2") or []:
        tag_names.add(tag["name"])
        key = _canonical_key(tag["name"], table)
        weight = (tag.get("weight") or "").lower()
        if key:
            ranks[key] = min(ranks.get(key, 9), RULE_WEIGHTS.index(weight) if weight in RULE_WEIGHTS else 0)
    for term in other_terms:
        key = _canonical_key(term, table)
        if key and term not in tag_names:
            ranks[key] = 0
    return ranks


def _derive(terms, rules, weights):
    have = {k for k in (_canonical_key(t, rules.table) for t in terms) if k}

    def strong(rule):
        return "min_weight" not in rule or weights.get(rule["min_weight"][0], 0) <= rule["min_weight"][1]

    return [
        (d["tag"], d["kind"])
        for d in rules.derived
        if any(all(group & have for group in r["all"]) and not r["none"] & have and strong(r) for r in d["rules"])
    ]


def _top_up(genres, tags, mb, rules, mu_categories):
    """(genres, tags) to add toward MIN_GENRES / MIN_TAGS from MangaBaka's genres and weighted tags, then MangaUpdates
    categories MangaBaka lacks; only names in the allowlists, never excluded ones. Allowlisted tags, and tags the
    table makes genres, are added past the minimum too, at a strong weight only."""
    mb = mb or {}
    table = rules.table
    have_g, have_t = {term_key(g) for g in genres}, {term_key(t) for t in tags}
    cand_g, cand_t = [], []

    def candidate(name, kind, weight="unweighted", skip=()):
        found = _canonical(name, kind, table)
        if not found or term_key(found[1]) in skip:
            return
        if found[0] == "genre" or (kind == "tag" and term_key(found[1]) in rules.allow_genres):
            cand_g.append((found[1], weight if kind == "tag" and found[0] == "genre" else "unweighted"))
        else:
            cand_t.append((found[1], weight))

    for genre in _mangabaka_genres(mb):
        candidate(genre, "genre")

    def weight(tag):
        return (tag.get("weight") or "unweighted").lower()

    strong = [t for t in mb.get("tags_v2") or [] if weight(t) in TOP_UP_WEIGHTS]
    for tag in sorted(strong, key=lambda t: TOP_UP_WEIGHTS.index(weight(t))):
        candidate(tag["name"], "tag", weight(tag))
    mb_keys = {_canonical_key(t["name"], table) for t in mb.get("tags_v2") or []}
    for name in mu_categories:
        candidate(name, "tag", skip=mb_keys)
    add_g, add_t = [], []
    for name, w in cand_g:
        key = term_key(name)
        if len(have_g) >= MIN_GENRES and w not in PAST_MIN_WEIGHTS:
            continue
        if key in rules.allow_genres and key not in have_g and key not in rules.exclude:
            add_g.append(rules.allow_genres[key])
            have_g.add(key)
    for name, w in cand_t:
        key = term_key(name)
        if len(have_t) >= MIN_TAGS and (key not in rules.always_tags or w not in PAST_MIN_WEIGHTS):
            continue
        if key in rules.allow_tags and key not in have_t and key not in have_g and key not in rules.exclude:
            add_t.append(rules.allow_tags[key])
            have_t.add(key)
    return add_g, add_t
