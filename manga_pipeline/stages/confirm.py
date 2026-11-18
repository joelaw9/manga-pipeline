"""Stage confirm (SPEC "Chapter stages", "Publishing"): confirm a published file once Kavita lists it.

Once Kavita lists the published file in its series with the size on disk, the download is removed the retention way
and the published file is confirmed. Until then the chapter waits on kavita_scan; a series Kavita no longer holds is
needs_human.
"""

from manga_pipeline import config, db, kavita_link, retention, workflow


def run(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    """Confirm the chapter's published file once Kavita lists it, removing the download; else wait on kavita_scan.

    A series Kavita no longer holds is marked needs_human, and the chapter waits.
    """
    series = _series(ctx, chapter)
    if series["kavita_series_id"] is None:
        return workflow.Waiting("kavita_scan")  # a new series Kavita does not list yet (SPEC "Publishing")
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

    def record(conn: db.Connection) -> None:
        conn.execute(
            "UPDATE published_file SET state = 'confirmed' WHERE library_path = %s", (chapter["library_path"],)
        )

    return workflow.Done(record)


def wait_over(ctx: workflow.Context, chapter: db.Row) -> bool:
    """Return whether the kavita_scan wait ends: the file is indexed, or Kavita no longer holds the series.

    In the second case confirm says so.
    """
    series = _series(ctx, chapter)
    if series["kavita_series_id"] is None:
        return False
    return ctx.kavita.series(series["kavita_series_id"]) is None or _indexed(ctx, series, chapter)


def _series(ctx: workflow.Context, chapter: db.Row) -> db.Row:
    """Return the chapter's series, its Kavita id looked for first when it is new.

    New: a new series Kavita did not list before.
    """
    series = db.one_row(ctx.conn, "SELECT * FROM series WHERE id = %s", (chapter["series_id"],))
    if series["kavita_series_id"] is not None:
        return series
    if kavita_link.attach(ctx, series["id"], kavita_link.listing(ctx)) is None:
        return series
    return db.one_row(ctx.conn, "SELECT * FROM series WHERE id = %s", (chapter["series_id"],))


def _indexed(ctx: workflow.Context, series: db.Row, chapter: db.Row) -> bool:
    """Kavita lists the published file in the series with the size it has on disk (a scan can keep a stale size)."""
    target = config.to_local(ctx.cfg, chapter["library_path"])
    size = target.stat().st_size
    return any(f["path"] == target and f["bytes"] == size for f in ctx.kavita.series_files(series["kavita_series_id"]))
