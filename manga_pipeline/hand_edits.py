"""`series edit FILE [--series-id ID]` (SPEC "Retroactive updates", hand edits): a person's corrections of a series'
chapter titles and volumes, checked whole and stored in `naming_edit`. The command rewrites nothing: the edits enter
the series' naming inputs, so the next `export` compares it (rewrite.py) and open chapters are named with them
(metadata/naming.py)."""

import json
from pathlib import PurePosixPath

from manga_pipeline import chapter_names, db, names, volumes, workflow
from manga_pipeline.metadata import naming

FIELDS = ("title", "volume")


def apply(ctx, data, series_id=None):
    """Check data (the edit file's JSON) whole and store its edits in 1 transaction; returns the lines to print.
    Refused naming every problem found."""
    problems = []
    if not isinstance(data, dict):
        msg = "the edit file must be a JSON object with 'series' and 'chapters'"
        raise workflow.Refused(msg)
    problems += [f"unknown key {k!r} (series, chapters)" for k in data if k not in ("series", "chapters")]
    chapters = data.get("chapters")
    if not isinstance(chapters, dict) or not chapters:
        problems.append("'chapters' must be an object of chapter: {title, volume}")
        chapters = {}
    series = _series(ctx, data.get("series"), series_id, problems)
    wanted = {}  # edit_key: (token, {field: value})
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
        before = len(problems)
        problems += [f"chapter {token!r}: unknown field {f!r} (title, volume)" for f in change if f not in FIELDS]
        title, volume = change.get("title"), change.get("volume")
        if "title" in change and title is not None and not (isinstance(title, str) and title.strip()):
            problems.append(f"chapter {token!r}: title must be a non-empty text or null, not {title!r}")
        if (
            "volume" in change
            and volume is not None
            and (isinstance(volume, bool) or not isinstance(volume, int) or volume < 0)
        ):
            problems.append(f"chapter {token!r}: volume must be a whole number from 0 or null, not {volume!r}")
        ok = len(problems) == before
        wanted[key] = (token, {f: v.strip() if isinstance(v, str) else v for f, v in change.items() if f in FIELDS}, ok)
    if series is None:
        raise workflow.Refused("\n".join(problems))
    lines = []
    # 1 transaction (the write lock): 2 edits of 1 series at once never merge against stale stored edits.
    with db.transaction(ctx.conn):
        stored = chapter_names.edits(ctx.conn, series["id"])
        merged = dict(stored)
        for key, (_, change, ok) in wanted.items():
            old = stored.get(key, naming.Edit(None, None))
            if ok:
                merged[key] = naming.Edit(change.get("title", old.title), change.get("volume", old.volume))
        merged = {k: e for k, e in merged.items() if (e.title, e.volume) != (None, None)}
        try:
            after = _chapters(ctx, series, merged)
        except volumes.VolumeError as e:
            raise workflow.Refused("\n".join([*problems, f"the series' chapters cannot be named: {e}"])) from None
        try:
            existing = _clashes(_chapters(ctx, series, stored))
        except volumes.VolumeError:  # unnamable as it stands (a volume that is not whole): these edits may fix it
            existing = {}
        for key, (token, _, _) in wanted.items():
            found = [c for c in after if c["key"] == key]
            if len(found) != 1:
                problems.append(f"chapter {token!r}: {len(found) or 'no'} chapters of the series match")
        # only what these edits make, wherever it lands: a clash the series already has is not theirs
        problems += [m for k, m in _clashes(after).items() if k not in existing]
        if problems:
            raise workflow.Refused("\n".join(problems))
        now = workflow.now(ctx.conn)
        for key, (token, _, _) in wanted.items():
            kind, season, number = key
            e = merged.get(key)
            if stored.get(key) == e:
                lines.append(f"{token}: unchanged")
                continue
            where = (series["id"], season, kind == names.SIDE, names.fmt_num(number))
            if e is None:
                ctx.conn.execute(
                    """DELETE FROM naming_edit WHERE series_id = %s AND season IS NOT DISTINCT FROM %s AND side = %s
                    AND number = %s""",
                    where,
                )
            else:
                ctx.conn.execute(
                    """INSERT INTO naming_edit (series_id, season, side, number, title, volume, edited_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (series_id, season, side, number) DO UPDATE SET title = excluded.title,
                        volume = excluded.volume, edited_at = excluded.edited_at""",
                    (*where, e.title, e.volume, now),
                )
            lines.append(f"{token}: {describe(e)}")
    return [f"series {series['id']} ({series['name']}): {line}" for line in lines]


def load(path):
    """The edit file's JSON; Refused when it cannot be read or is not JSON."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        msg = f"{path}: {e}"
        raise workflow.Refused(msg) from None


def _series(ctx, name, series_id, problems):
    if not isinstance(name, str) or not name.strip():
        problems.append("'series' must be the series' Kavita name")
        return None
    rows = ctx.conn.execute(
        f"SELECT s.* FROM series s WHERE s.name = %(name)s AND {workflow.SCOPE} ORDER BY s.id",
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


def _chapters(ctx, series, edits):
    """The series' chapters as the edits would leave them: [{key, kind, season, number, volume, title, name}] (name:
    the file name it holds or would get)."""
    rows, overrides, mangadex, platform = chapter_names.inputs(ctx, series)
    named = naming.resolve(series["naming_basis"], rows, overrides, mangadex, platform, edits=edits)
    files = {
        r["id"]: r
        for r in ctx.conn.execute(
            "SELECT id, library_path, origin FROM published_file WHERE series_id = %s", (series["id"],)
        )
    }
    published = ctx.conn.execute(  # a chapter published and waiting to be confirmed: its file is its row
        """SELECT c.id FROM chapter c JOIN published_file p ON p.library_path = c.library_path
        WHERE c.series_id = %s""",
        (series["id"],),
    ).fetchall()
    published = {r[0] for r in published}
    out = []
    for r in rows:
        if r.key in published:
            continue
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
        out.append({"key": naming.edit_key(kind, r.season, number), "kind": kind, "season": r.season,
                    "number": number, "volume": volume, "name": name})  # fmt: skip
    return out


def _clashes(chapters):
    """{(what, the 2 chapters' keys): problem} of the series' chapters (2 chapters of 1 file name or 1 (volume,
    number), or main volumes decreasing along a season's chapter numbers), each pair once."""
    problems = {}
    for c in chapters:
        token = naming.edit_token(*c["key"])
        for o in chapters:
            if o is c:
                continue
            pair = frozenset((c["key"], o["key"]))
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
                and None not in (c["volume"], o["volume"])
                and (o["number"] - c["number"]) * (o["volume"] - c["volume"]) < 0
            ):
                volume, other = names.fmt_num(c["volume"]), names.fmt_num(o["volume"])
                problems[("order", pair)] = (
                    f"chapter {token!r}: volume {volume} is out of order with chapter "
                    f"{names.fmt_num(o['number'])} in volume {other}"
                )
    return problems


def describe(e):
    """An edit as printed: its fields, or "edit removed" for None."""
    if e is None:
        return "edit removed"
    parts = [f"title {e.title!r}" if e.title is not None else "", f"volume {e.volume}" if e.volume is not None else ""]
    return ", ".join(p for p in parts if p)
