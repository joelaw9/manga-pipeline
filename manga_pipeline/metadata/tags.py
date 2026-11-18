"""A series' genres and tags from its stored source records (SPEC "Tag rules").

Pure: the same records and rule files always give the same lists, with no source request.

Ported from the old metadata tool (`kavita_enrich.py` build_metadata, consolidate, derive_tags, top_up). One change
(decision D7.6): a top-up adds only consolidation targets and allowlist entries, never a name because other library
series use it, so a series' tags never depend on the order series are written.

Order: the sources' lists, the consolidation table, derived and promoted tags, the top-up.
"""

import csv
import hashlib
import io
import re
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

import yaml

from manga_pipeline import names
from manga_pipeline.config import Yaml
from manga_pipeline.metadata.records import SeriesRecords
from manga_pipeline.replies.sources import (
    AniListMedia,
    MangaBakaSeries,
    MangaBakaTag,
    MangaDexManga,
    MangaUpdatesSeries,
)

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
type Table = dict[tuple[str, str], tuple[str, str]]  # {(kind, term key): (to kind, to name)}; to kind "remove" drops it


class DerivedRule(TypedDict):
    """A rule of a derived tag: every group shares a term with the series', none of none does, min_weight met."""

    all: list[set[str]]
    none: set[str]
    min_weight: NotRequired[tuple[str, int]]  # (term key, rank in RULE_WEIGHTS) MangaBaka must weight it at least


class Derived(TypedDict):
    """A derived or promoted tag: its name, kind (tag or genre) and rules (any one gives it)."""

    tag: str
    kind: str
    rules: list[DerivedRule]


@dataclass(frozen=True)
class Rules:
    """The loaded tag rules: consolidation table, derived and promoted tags, allowlists, exclusions, fingerprint."""

    table: Table
    derived: list[Derived]  # derived and promoted tags
    allow_genres: dict[str, str]  # {term key: name} a top-up may add
    allow_tags: dict[str, str]
    always_tags: frozenset[str]  # allowlisted tags: added past MIN_TAGS too, at a strong weight
    exclude: frozenset[str]  # term keys a top-up never adds
    fingerprint: str  # sha256 of the rule files and of the code that applies them


def load(folder: Path = RULES) -> Rules:
    """Load the rules of the rule files in folder.

    A file that is not valid raises ValueError naming it and the line or entry.
    """
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
    allow_genres: dict[str, str] = {}
    allow_tags: dict[str, str] = {}
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


def compute(records: SeriesRecords, rules: Rules) -> tuple[list[str], list[str]]:
    """(genres, tags) of a series from its records, as a check stores them."""
    mb, mu = records["mangabaka"], records["mangaupdates"]
    md, al = records["mangadex"], records["anilist"]
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


def display_name(name: str) -> str:
    """Return a source's name without MangaUpdates' plural marker ("Concubine/s" -> "Concubine")."""
    return PLURAL_MARKER.sub("", name or "").strip()


def term_key(term: str) -> str:
    """Return the key a tag or genre name matches by.

    Plural markers and a final "s" dropped, case and punctuation ignored.
    """
    key = names.key(display_name(term))
    return key[:-1] if len(key) > SHORT_KEY_MAX and key.endswith("s") else key


def _unique(values: Iterable[str]) -> list[str]:
    """Blanks and duplicates (by names.key) dropped, the first spelling kept."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = names.key(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _canonical(name: str, kind: str, table: Table) -> tuple[str, str] | None:
    """Return the (kind, name) a source's name converts to, or None when the table removes it."""
    key = term_key(name)
    hit = table.get((kind, key)) or table.get(("tag" if kind == "genre" else "genre", key))
    if hit:
        return None if hit[0] == "remove" else hit
    return kind, display_name(name)


def _canonical_key(name: str, table: Table) -> str | None:
    found = _canonical(name, "tag", table)
    return term_key(found[1]) if found else None


# --- rule files


def _consolidation(text: str) -> Table:
    reader = csv.DictReader(io.StringIO(text))
    missing = {"from_kind", "from", "to_kind", "to"} - set(reader.fieldnames or ())
    if missing:
        msg = f"consolidation.csv: no column {', '.join(sorted(missing))}"
        raise ValueError(msg)
    table: Table = {}
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


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _derived(entries: Yaml, table: Table) -> list[Derived]:
    if not isinstance(entries, list):
        msg = "derived_tags.yaml: not a list of entries"
        raise ValueError(msg)
    return [_derived_entry(entry, table) for entry in entries]


def _derived_entry(entry: Yaml, table: Table) -> Derived:
    """1 derived_tags.yaml entry as {tag, kind, rules}; ValueError naming what is wrong with it."""
    if not isinstance(entry, dict) or set(entry) - {"tag", "kind", "rules"} or not entry.get("tag"):
        msg = f"derived_tags.yaml: not an entry of tag, kind and rules: {entry!r}"
        raise ValueError(msg)
    tag, kind, rules = entry["tag"], entry.get("kind") or "tag", entry.get("rules")
    if not isinstance(tag, str) or not isinstance(kind, str) or kind.lower() not in ("tag", "genre"):
        msg = f"derived_tags.yaml: {tag!r}: tag must be a name and kind tag or genre"
        raise ValueError(msg)
    if not isinstance(rules, list) or not rules:
        msg = f"derived_tags.yaml: {tag}: needs a list of rules"
        raise ValueError(msg)
    return {"tag": tag, "kind": kind.lower(), "rules": [_derived_rule(rule, tag, table) for rule in rules]}


def _derived_rule(rule: Yaml, tag: str, table: Table) -> DerivedRule:
    """1 rule of the entry for tag as {all: [term key sets], none: term key set}; ValueError naming what is wrong."""
    groups = rule.get("all") if isinstance(rule, dict) else None
    if not isinstance(rule, dict) or set(rule) - {"all", "none"} or not isinstance(groups, list):
        msg = f"derived_tags.yaml: {tag}: a rule is all (a list of groups) and none: {rule!r}"
        raise ValueError(msg)
    if not groups or not all(isinstance(g, list) and g for g in groups):
        msg = f"derived_tags.yaml: {tag}: a rule needs at least 1 group, each with a term: {rule!r}"
        raise ValueError(msg)
    return {
        "all": [_rule_keys(group, tag, table) for group in groups],
        "none": _rule_keys(rule.get("none") or [], tag, table),
    }


def _rule_keys(terms: Yaml, tag: str, table: Table) -> set[str]:
    """Return a rule's list of terms as their canonical keys; ValueError when it is not a list of names."""
    words = [t for t in terms if isinstance(t, str)] if isinstance(terms, list) else []
    if not isinstance(terms, list) or len(words) != len(terms):
        msg = f"derived_tags.yaml: {tag}: not a list of names: {terms!r}"
        raise ValueError(msg)
    return {k for k in (_canonical_key(t, table) for t in words) if k}


def _promoted(lines: Iterable[str], table: Table) -> list[Derived]:
    """Promoted tags as 1-term derived rules; "Tag: weight" also needs MangaBaka to weight it at least that."""
    promoted: list[Derived] = []
    for line in lines:
        name, _, min_weight = (p.strip() for p in line.partition(":"))
        if min_weight and min_weight.lower() not in RULE_WEIGHTS:
            msg = f"promote_tags.txt: {line!r}: the weight is not one of {', '.join(RULE_WEIGHTS)}"
            raise ValueError(msg)
        found = _canonical(name, "tag", table)
        if found:
            rule: DerivedRule = {"all": [{term_key(found[1])}], "none": set()}
            if min_weight:
                rule["min_weight"] = (term_key(found[1]), RULE_WEIGHTS.index(min_weight.lower()))
            promoted.append({"tag": found[1], "kind": found[0], "rules": [rule]})
    return promoted


# --- sources


def _mangabaka_genres(mb: MangaBakaSeries) -> list[str]:
    """MangaBaka's genre slugs ("martial_arts", "sci-fi") as names ("Martial Arts", "Sci-Fi")."""

    def title(slug: str) -> str:
        words = slug.replace("_", " ").split()
        return " ".join(
            w if i and w in SMALL_WORDS else "-".join(p.capitalize() for p in w.split("-")) for i, w in enumerate(words)
        )

    return [title(g) for g in mb.get("genres") or []]


def _mu_genres(mu: MangaUpdatesSeries) -> list[str]:
    return [g["genre"] for g in mu.get("genres") or []]


def _mu_categories(mu: MangaUpdatesSeries) -> list[str]:
    return [c["category"] for c in mu.get("categories") or []]


def _sourced(
    mb: MangaBakaSeries | None,
    mu: MangaUpdatesSeries | None,
    md: MangaDexManga | None,
    al: AniListMedia | None,
) -> tuple[list[str], list[str]]:
    """Return the sources' own genre and tag lists.

    Genres from MangaBaka (else MangaUpdates), AniList and MangaDex's genre tags; tags from AniList (rank at least 60,
    no spoiler) and MangaDex's theme tags, less any that is a genre.
    """
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


def _consolidate(genres: list[str], tags: list[str], table: Table) -> tuple[list[str], list[str]]:
    """Both lists through the table; a tag that ends up equal to a genre is dropped."""
    out: dict[str, list[str]] = {"genre": [], "tag": []}
    for kind, values in (("genre", genres), ("tag", tags)):
        for value in values:
            found = _canonical(value, kind, table)
            if found:
                out[found[0]].append(found[1])
    new_genres = _unique(out["genre"])
    genre_keys = {term_key(g) for g in new_genres}
    return new_genres, [t for t in _unique(out["tag"]) if term_key(t) not in genre_keys]


def _rule_terms(mb: MangaBakaSeries | None, mu: MangaUpdatesSeries | None) -> tuple[list[str], list[str]]:
    """Return (MangaBaka's tags, the other terms) derived-tag rules match.

    MangaBaka's tags carry weights; the other terms carry none to judge: MangaBaka's genres, status, content rating and
    type, MangaUpdates' categories and genres when MangaBaka's tags are sparse, and MangaUpdates' terms and type alone
    without MangaBaka.
    """
    mu_terms = (_mu_categories(mu) + _mu_genres(mu)) if mu else []
    if not mb:
        return [], mu_terms + ([f"Type: {mu_type}"] if mu and (mu_type := mu.get("type")) else [])
    tags = [t["name"] for t in mb.get("tags_v2") or []]
    other = list(mb.get("genres") or [])
    other += [f"Status: {status}"] if (status := mb.get("status")) else []
    other += [f"Content Rating: {rating}"] if (rating := mb.get("content_rating")) else []
    other += [f"Type: {mb_type}"] if (mb_type := mb.get("type")) else []
    if len(tags) < MANGABAKA_SPARSE_TAGS:
        other += mu_terms
    return tags, other


def _term_weights(mb: MangaBakaSeries | None, other_terms: Iterable[str], table: Table) -> dict[str, int]:
    """Return {term key: rank in RULE_WEIGHTS}: each MangaBaka tag's best weight.

    Another term ranks 0 unless it is spelt exactly as a MangaBaka tag. MangaBaka carries MangaUpdates' categories and
    weights the low-vote ones incidental, so a raw category it repeats keeps MangaBaka's weight (as the old tool did).
    """
    if not mb:
        return {}
    ranks: dict[str, int] = {}
    tag_names: set[str] = set()
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


def _derive(terms: Iterable[str], rules: Rules, weights: Mapping[str, int]) -> list[tuple[str, str]]:
    have = {k for k in (_canonical_key(t, rules.table) for t in terms) if k}

    def strong(rule: DerivedRule) -> bool:
        return "min_weight" not in rule or weights.get(rule["min_weight"][0], 0) <= rule["min_weight"][1]

    return [
        (d["tag"], d["kind"])
        for d in rules.derived
        if any(all(group & have for group in r["all"]) and not r["none"] & have and strong(r) for r in d["rules"])
    ]


def _top_up(
    genres: list[str], tags: list[str], mb: MangaBakaSeries | None, rules: Rules, mu_categories: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Return (genres, tags) to add toward MIN_GENRES / MIN_TAGS.

    They come from MangaBaka's genres and weighted tags, then MangaUpdates categories MangaBaka lacks; only names in the
    allowlists, never excluded ones. Allowlisted tags, and tags the table makes genres, are added past the minimum too,
    at a strong weight only.
    """
    cand_g, cand_t = _top_up_candidates(mb, rules, mu_categories)
    have_g = {term_key(g) for g in genres}
    genre_keys = _top_up_genres(cand_g, have_g, rules)
    tag_keys = _top_up_tags(cand_t, {term_key(t) for t in tags}, have_g | set(genre_keys), rules)
    return [rules.allow_genres[k] for k in genre_keys], [rules.allow_tags[k] for k in tag_keys]


def _top_up_candidates(
    mb: MangaBakaSeries | None, rules: Rules, mu_categories: Sequence[str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ([(genre, weight)], [(tag, weight)]) a top-up may take, in order.

    The order: MangaBaka's genres, its strong tags (strongest first), then MangaUpdates categories MangaBaka has no tag
    for; each as the table names it, removed ones left out. No MangaBaka record: its categories alone.
    """
    table = rules.table
    cand_g: list[tuple[str, str]] = []
    cand_t: list[tuple[str, str]] = []
    mb_genres = _mangabaka_genres(mb) if mb else []
    mb_tags = (mb.get("tags_v2") or []) if mb else []

    def candidate(name: str, kind: str, weight: str = "unweighted", skip: Container[str | None] = ()) -> None:
        found = _canonical(name, kind, table)
        if not found or term_key(found[1]) in skip:
            return
        if found[0] == "genre" or (kind == "tag" and term_key(found[1]) in rules.allow_genres):
            cand_g.append((found[1], weight if kind == "tag" and found[0] == "genre" else "unweighted"))
        else:
            cand_t.append((found[1], weight))

    for genre in mb_genres:
        candidate(genre, "genre")

    def weight(tag: MangaBakaTag) -> str:
        return (tag.get("weight") or "unweighted").lower()

    strong = [t for t in mb_tags if weight(t) in TOP_UP_WEIGHTS]
    for tag in sorted(strong, key=lambda t: TOP_UP_WEIGHTS.index(weight(t))):
        candidate(tag["name"], "tag", weight(tag))
    mb_keys = {_canonical_key(t["name"], table) for t in mb_tags}
    for name in mu_categories:
        candidate(name, "tag", skip=mb_keys)
    return cand_g, cand_t


def _top_up_genres(candidates: Iterable[tuple[str, str]], have: set[str], rules: Rules) -> list[str]:
    """Return the keys of the allowlisted, not excluded genres the top-up adds, in order.

    Up to MIN_GENRES held, past it at a strong weight only.
    """
    have, added = set(have), []
    for name, w in candidates:
        key = term_key(name)
        if len(have) >= MIN_GENRES and w not in PAST_MIN_WEIGHTS:
            continue
        if key in rules.allow_genres and key not in have and key not in rules.exclude:
            added.append(key)
            have.add(key)
    return added


def _top_up_tags(
    candidates: Iterable[tuple[str, str]], have: set[str], genre_keys: Container[str], rules: Rules
) -> list[str]:
    """Return the keys of the allowlisted, not excluded tags the top-up adds, in order, none a genre.

    genre_keys are the genres held and added. Up to MIN_TAGS held, past it an always-added tag at a strong weight only.
    """
    have, added = set(have), []
    for name, w in candidates:
        key = term_key(name)
        if len(have) >= MIN_TAGS and (key not in rules.always_tags or w not in PAST_MIN_WEIGHTS):
            continue
        if key in rules.allow_tags and key not in have and key not in genre_keys and key not in rules.exclude:
            added.append(key)
            have.add(key)
    return added
