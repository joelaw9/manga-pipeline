"""Stage publish (SPEC "Chapter stages", "Publishing"). Stub: renames the staged file into
<paths.library>/<series name>/<download file name> and records it as an unconfirmed published_file; asks Kavita
nothing. Phase 7 replaces it."""

from pathlib import Path

from manga_pipeline import config, workflow


def run(ctx, chapter):
    name = ctx.conn.execute("SELECT name FROM series WHERE id = ?", (chapter["series_id"],)).fetchone()[0]
    staged = Path(chapter["staged_path"])
    target = config.abs_path(ctx.cfg["paths"]["library"]) / name / Path(chapter["download_path"]).name
    if staged.exists():
        if target.exists():
            raise FileExistsError(f"{target} is already in the library; a library file is never overwritten")
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.rename(target)
    elif not target.exists():
        raise FileNotFoundError(f"neither the staged {staged} nor the published {target} exists")
    size = target.stat().st_size

    def record(conn):
        published_id = conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, state, size, origin)
            VALUES (?, ?, ?, 'unconfirmed', ?, 'pipeline') RETURNING id""",
            (str(target), chapter["series_id"], chapter["identity"], size),
        ).fetchone()[0]
        conn.execute(
            "UPDATE chapter SET library_path = ?, published_file_id = ? WHERE id = ?",
            (str(target), published_id, chapter["id"]),
        )

    return workflow.Done(record)
