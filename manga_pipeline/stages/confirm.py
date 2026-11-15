"""Stage confirm (SPEC "Chapter stages", "Publishing"): once Kavita lists the published file in its series with the
size on disk, the download is removed the retention way and the published file is confirmed. Until then the chapter
waits on kavita_scan; a series Kavita no longer holds is needs_human.
"""

from manga_pipeline import config, db, retention, workflow


def run(ctx, chapter):
    series = _series(ctx, chapter)
    if ctx.kavita.series(series["kavita_series_id"]) is None:
        with db.transaction(ctx.conn):
            workflow.needs_human(
                ctx.conn,
                "series",
                series["id"],
                f"Kavita no longer holds series {series['kavita_series_id']} -> series link MANGA_ID KAVITA_SERIES_ID",
            )
        return workflow.Waiting("kavita_scan", {"why": "series gone from Kavita"})
    if not _indexed(ctx, series, chapter):
        return workflow.Waiting("kavita_scan")
    workflow.holds_claim(ctx, chapter)
    retention.put(ctx.cfg, config.to_local(ctx.cfg, chapter["download_path"]))

    def record(conn):
        conn.execute(
            "UPDATE published_file SET state = 'confirmed' WHERE library_path = %s", (chapter["library_path"],)
        )

    return workflow.Done(record)


def wait_over(ctx, chapter):
    """The kavita_scan wait ends when the file is indexed, or Kavita no longer holds the series (confirm says so)."""
    series = _series(ctx, chapter)
    return ctx.kavita.series(series["kavita_series_id"]) is None or _indexed(ctx, series, chapter)


def _series(ctx, chapter):
    return ctx.conn.execute("SELECT * FROM series WHERE id = %s", (chapter["series_id"],)).fetchone()


def _indexed(ctx, series, chapter):
    """Kavita lists the published file in the series with the size it has on disk (a scan can keep a stale size)."""
    target = config.to_local(ctx.cfg, chapter["library_path"])
    size = target.stat().st_size
    return any(f["path"] == target and f["bytes"] == size for f in ctx.kavita.series_files(series["kavita_series_id"]))
