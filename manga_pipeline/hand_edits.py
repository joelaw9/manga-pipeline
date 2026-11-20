"""`series edit FILE [--series-id ID]` (SPEC "Retroactive updates", hand edits).

A person's corrections of a series' chapter titles and volumes, checked whole and stored in `naming_edit`. The command
rewrites nothing: the edits enter the series' naming inputs, so the next `export` compares it (rewrite.py) and open
chapters are named with them (metadata/naming.py).
"""

import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import TypedDict

from manga_pipeline import chapter_names, db, names, volumes, workflow
from manga_pipeline.db import sql
from manga_pipeline.metadata import naming
from manga_pipeline.replies import Json

FIELDS = ("title", "volume")


class EditChange(TypedDict, total=False):
    """A chapter's change as the edit file gives it, checked: the fields it sets (title None: untitled)."""

    title: str | None
    volume: int | None


class NamedChapter(TypedDict):
    """A chapter of the series as the edits would leave it (_chapters): its key and naming, and its file name.

    settled: a published file or a chapter comicinfo named; an open side story not settled can still be renumbered.
    adopted: an adopted file (origin library), which is not rewritten until it is converted.
    """

    key: naming.EditKey
    settled: bool
    adopted: bool
    kind: str
    season: int | None
    number: float
    volume: float | None
    name: str


def apply(ctx: workflow.Context, data: Json, series_id: int | None = None) -> list[str]:
    """Check data (the edit file's JSON) whole and store its edits in 1 transaction; returns the lines to print.

    Refused naming every problem found.
    """
    problems: list[str] = []
    edit = _edit_object(data)
    chapters = _chapters_of(edit, problems)
    series = _series(ctx, edit.get("series"), series_id, problems)
    wanted = _wanted(chapters, problems)
    if series is None:
        raise workflow.Refused("\n".join(problems))
    # read before the write lock (the downloads' ComicInfo.xml among them): the transaction reads only the stored edits
    try:
        found = chapter_names.inputs(ctx, series)
    except volumes.VolumeError as e:
        raise _unnamable(problems, e) from None
    files = {
        r["id"]: r
        for r in ctx.conn.execute(
            "SELECT id, library_path, origin FROM published_file WHERE series_id = %s", (series["id"],)
        )
    }
    # 1 transaction (the write lock): 2 edits of 1 series at once never merge against stale stored edits.
    with db.transaction(ctx.conn):
        stored = chapter_names.edits(ctx.conn, series["id"])
        merged = _merged(stored, wanted)
        try:
            after = _chapters(series, found, files, merged)
        except volumes.VolumeError as e:
            raise _unnamable(problems, e) from None
        problems += _unmatched(after, wanted)
        problems += _new_clashes(series, found, files, stored, after)
        if problems:
            raise workflow.Refused("\n".join(problems))
        lines = _store(ctx.conn, series["id"], stored, merged, wanted)
    return [f"series {series['id']} ({series['name']}): {line}" for line in lines]


def _unnamable(problems: list[str], e: volumes.VolumeError) -> workflow.Refused:
    """Return the refusal of a series whose chapters cannot be named, with the problems found before."""
    return workflow.Refused("\n".join([*problems, f"the series' chapters cannot be named: {e}"]))


def _edit_object(data: Json) -> Mapping[str, Json]:
    """Return the edit file's JSON object; Refused when the file is not a JSON object."""
    if not isinstance(data, dict):
        msg = "the edit file must be a JSON object with 'series' and 'chapters'"
        raise workflow.Refused(msg)
    return data


def _chapters_of(data: Mapping[str, Json], problems: list[str]) -> Mapping[str, Json]:
    """Return the edit file's 'chapters' object ({} when it is not a non-empty object).

    Its problems are added to problems.
    """
    problems.extend(f"unknown key {k!r} (series, chapters)" for k in data if k not in ("series", "chapters"))
    chapters = data.get("chapters")
    if not isinstance(chapters, dict) or not chapters:
        problems.append("'chapters' must be an object of chapter: {title, volume}")
        return {}
    return chapters


def _wanted(chapters: Mapping[str, Json], problems: list[str]) -> dict[naming.EditKey, tuple[str, EditChange | None]]:
    """Return {edit_key: (token, its checked change, None when the change is not valid)} of the edit file's chapters.

    Their problems added to problems (a token that is no chapter, or names one already given, or whose change is not an
    object, is left out).
    """
    wanted: dict[naming.EditKey, tuple[str, EditChange | None]] = {}
    for token, change in chapters.items():
        try:
            key = naming.parse_token(token)
        except ValueError as e:
            problems.append(str(e))
            continue
        if key in wanted:
            problems.append(f"chapter {token!r} names the same chapter as {wanted[key][0]!r}")
            continue
        if not isinstance(change, dict) or not change:
            problems.append(f"chapter {token!r}: give an object of title and/or volume")
            continue
        checked, found = _change(token, change)
        problems += found
        wanted[key] = (token, None if found else checked)
    return wanted


def _change(token: str, change: Mapping[str, Json]) -> tuple[EditChange, list[str]]:
    """Return 1 chapter's change, checked (its title stripped), and its problems.

    A problem: an unknown field, a title that is not a non-empty text or null, a volume that is not a whole number
    from 0 or null.
    """
    problems = [f"chapter {token!r}: unknown field {f!r} (title, volume)" for f in change if f not in FIELDS]
    checked: EditChange = {}
    if "title" in change:
        title = change["title"]
        if title is None or (isinstance(title, str) and title.strip()):
            checked["title"] = title.strip() if isinstance(title, str) else None
        else:
            problems.append(f"chapter {token!r}: title must be a non-empty text or null, not {title!r}")
    if "volume" in change:
        volume = change["volume"]
        if volume is None or (not isinstance(volume, bool) and isinstance(volume, int) and volume >= 0):
            checked["volume"] = volume
        else:
            problems.append(f"chapter {token!r}: volume must be a whole number from 0 or null, not {volume!r}")
    return checked, problems


def _merged(
    stored: Mapping[naming.EditKey, naming.Edit], wanted: dict[naming.EditKey, tuple[str, EditChange | None]]
) -> dict[naming.EditKey, naming.Edit]:
    """Return the series' edits once the valid wanted changes are applied over the stored ones ({edit_key: Edit}).

    An edit left with no field is gone.
    """
    merged = dict(stored)
    for key, (_, change) in wanted.items():
        old = stored.get(key, naming.Edit(None, None))
        if change is not None:
            merged[key] = naming.Edit(change.get("title", old.title), change.get("volume", old.volume))
    return {k: e for k, e in merged.items() if (e.title, e.volume) != (None, None)}


def _unmatched(after: list[NamedChapter], wanted: dict[naming.EditKey, tuple[str, EditChange | None]]) -> list[str]:
    """Return a problem for each wanted chapter that does not match exactly 1 of the series' chapters.

    Or that sets a field of an open side story not named yet (its side number can still change; a null is accepted), or
    of an adopted file (it is not rewritten until it is converted; a null is accepted). after: _chapters.
    """
    problems: list[str] = []
    for key, (token, change) in wanted.items():
        found = [c for c in after if c["key"] == key]
        sets = change is not None and any(v is not None for v in change.values())
        if len(found) != 1:
            problems.append(f"chapter {token!r}: {len(found) or 'no'} chapters of the series match")
        elif found[0]["adopted"] and sets:
            problems.append(f"chapter {token!r}: {found[0]['name']} is adopted and not converted yet: edit it then")
        elif found[0]["kind"] == names.SIDE and not found[0]["settled"] and sets:
            problems.append(
                f"chapter {token!r}: side {names.fmt_num(key[2])} is open and not named yet: edit it once it is named"
            )
    return problems


def _new_clashes(
    series: db.Row,
    found: chapter_names.Inputs,
    files: Mapping[int, db.Row],
    stored: Mapping[naming.EditKey, naming.Edit],
    after: list[NamedChapter],
) -> list[str]:
    """Return the clashes of the series' chapters as the edits leave them (after) that the stored edits do not have.

    Only what these edits make, wherever it lands. found, files: as _chapters takes them.
    """
    try:
        existing = _clashes(_chapters(series, found, files, stored))
    except volumes.VolumeError:  # unnamable as it stands (a volume that is not whole): these edits may fix it
        existing: dict[tuple[str, frozenset[naming.EditKey]], str] = {}
    return [m for k, m in _clashes(after).items() if k not in existing]


def _store(
    conn: db.Connection,
    series_id: int,
    stored: Mapping[naming.EditKey, naming.Edit],
    merged: Mapping[naming.EditKey, naming.Edit],
    wanted: dict[naming.EditKey, tuple[str, EditChange | None]],
) -> list[str]:
    """Store each wanted chapter's edit as merged leaves it (removed when it has no field left); a line per chapter."""
    lines: list[str] = []
    now = workflow.now(conn)
    for key, (token, _) in wanted.items():
        kind, season, number = key
        e = merged.get(key)
        if stored.get(key) == e:
            lines.append(f"{token}: unchanged")
            continue
        where = (series_id, season, kind == names.SIDE, names.fmt_num(number))
        if e is None:
            conn.execute(
                """DELETE FROM naming_edit WHERE series_id = %s AND season IS NOT DISTINCT FROM %s AND side = %s
                AND number = %s""",
                where,
            )
        else:
            conn.execute(
                """INSERT INTO naming_edit (series_id, season, side, number, title, volume, edited_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (series_id, season, side, number) DO UPDATE SET title = excluded.title,
                    volume = excluded.volume, edited_at = excluded.edited_at""",
                (*where, e.title, e.volume, now),
            )
        lines.append(f"{token}: {describe(e)}")
    return lines


def load(path: str | Path) -> Json:
    """Return the edit file's JSON; Refused when it cannot be read or is not JSON."""
    try:
        with Path(path).open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        msg = f"{path}: {e}"
        raise workflow.Refused(msg) from None


def _series(ctx: workflow.Context, name: Json, series_id: int | None, problems: list[str]) -> db.Row | None:
    if not isinstance(name, str) or not name.strip():
        problems.append("'series' must be the series' Kavita name")
        return None
    rows = ctx.conn.execute(
        sql.SQL("SELECT s.* FROM series s WHERE s.name = %(name)s AND {scope} ORDER BY s.id").format(
            scope=workflow.SCOPE
        ),
        {**workflow.scope_params(ctx.cfg), "name": name},
    ).fetchall()
    if series_id is not None:
        rows = [r for r in rows if r["id"] == series_id]
    if len(rows) != 1:
        ids = ", ".join(str(r["id"]) for r in rows)
        problems.append(
            f"series {name!r}" + (f" (--series-id {series_id})" if series_id is not None else "")
            + (f": {len(rows)} series match ({ids}); pick 1 with --series-id" if rows else ": no in-scope series")
        )  # fmt: skip
        return None
    return rows[0]


def _chapters(
    series: db.Row,
    found: chapter_names.Inputs,
    files: Mapping[int, db.Row],
    edits: Mapping[naming.EditKey, naming.Edit],
) -> list[NamedChapter]:
    """Return the series' chapters as the edits would leave them: [{key, settled, kind, season, number, volume, name}].

    found: chapter_names.inputs of the series; files: {id: its published_file row (id, library_path, origin)}.
    name: the file name it holds or would get. VolumeError when the chapters cannot be named.
    """
    rows, overrides, mangadex, platform = found
    named = naming.resolve(series["naming_basis"], rows, overrides, mangadex, platform, edits=edits)
    out: list[NamedChapter] = []
    for r in rows:
        if r.settled:
            e = naming.apply_edit(r, edits)
            kind, volume, number, title = r.kind, e.volume, r.number, e.title
        elif r.key in named:
            n = named[r.key]
            kind, volume, number, title = n.kind, n.volume, n.number, n.title
        else:
            continue
        file = files.get(r.key[1]) if isinstance(r.key, tuple) else None
        name = (
            PurePosixPath(file["library_path"]).name
            if file is not None and file["origin"] != "pipeline"
            else names.file_name(number, title, volume)
        )
        adopted = file is not None and file["origin"] == "library"
        out.append({"key": naming.edit_key(kind, r.season, number), "settled": r.settled, "adopted": adopted,
                    "kind": kind, "season": r.season, "number": number, "volume": volume, "name": name})  # fmt: skip
    return out


def _clashes(chapters: list[NamedChapter]) -> dict[tuple[str, frozenset[naming.EditKey]], str]:
    """Return {(what, the 2 chapters' keys): problem} of the series' chapters, each pair once.

    A problem: 2 chapters of 1 file name or 1 (volume, number), or main volumes decreasing along a season's chapter
    numbers.
    """
    problems: dict[tuple[str, frozenset[naming.EditKey]], str] = {}
    for c in chapters:
        token = naming.edit_token(*c["key"])
        for o in chapters:
            if o is c:
                continue
            pair = frozenset((c["key"], o["key"]))
            volume, other = c["volume"], o["volume"]
            if o["name"] == c["name"]:
                problems[("name", pair)] = f"chapter {token!r}: file name {c['name']!r} is another chapter's"
            elif c["volume"] is not None and c["volume"] == o["volume"] and c["number"] == o["number"]:
                problems[("volume", pair)] = (
                    f"chapter {token!r}: volume {names.fmt_num(c['volume'])} already has a chapter "
                    f"{names.fmt_num(c['number'])}"
                )
            elif (
                c["kind"] == o["kind"] == names.MAIN
                and c["season"] == o["season"]
                and volume is not None
                and other is not None
                and (o["number"] - c["number"]) * (other - volume) < 0
            ):
                problems[("order", pair)] = (
                    f"chapter {token!r}: volume {names.fmt_num(volume)} is out of order with chapter "
                    f"{names.fmt_num(o['number'])} in volume {names.fmt_num(other)}"
                )
    return problems


def describe(e: naming.Edit | None) -> str:
    """Return an edit as printed: its fields, or "edit removed" for None."""
    if e is None:
        return "edit removed"
    parts = [f"title {e.title!r}" if e.title is not None else "", f"volume {e.volume}" if e.volume is not None else ""]
    return ", ".join(p for p in parts if p)
