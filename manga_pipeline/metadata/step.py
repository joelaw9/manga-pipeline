"""`intake`'s metadata step (SPEC "Metadata checks"): bring the series rows in step with the library, link library
manga that have no downloads, then run the due checks, each series alone. The step never stops intake's chapter work:
a service that cannot be reached skips the step, and a check that fails is logged and stays due. Only a failing share or
database ends the run.
"""

import logging

from manga_pipeline import db, identify, workflow
from manga_pipeline.clients import http
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.metadata import check, covers, library, sources

log = logging.getLogger(__name__)
# What a service's answer can raise: the check simply did not complete.
CHECK_FAILURES = (workflow.Unreachable, http.HttpError, http.Timeout, sources.SourceError, suwayomi_client.GraphQLError)


def run(ctx, clients):
    kavita_series = identify.KavitaSeries(ctx)
    try:
        library.sync(ctx, kavita_series.rows())
        identify.sweep(ctx, ctx.suwayomi.library_manga(), kavita_series)
    except CHECK_FAILURES as e:
        log.warning("%s: metadata step skipped: %s", ctx.part, e)
        return
    covers.clean(ctx)
    for series_id in check.due(ctx):
        try:
            check.run(ctx, clients, series_id)
        except CHECK_FAILURES as e:
            log.warning("series %s: metadata check not completed: %s", series_id, e)
        except Exception as e:
            if isinstance(e, db.Error) or (
                isinstance(e, OSError) and (workflow.dropped_share(e) or e.errno in workflow.SHARE_ERRNOS)
            ):
                raise  # the database or the share: the run's problem
            # A reply of an unexpected shape, a file this machine cannot write: this series' problem, not the run's
            log.exception("series %s: metadata check failed", series_id)
