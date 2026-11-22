"""The Discord queue (SPEC "Needs human"): reports are queued in their writer's transaction; export and watch send them.

A report is queued once per kind, item and text (the text holds the reason). A send claims each row before its
post, so 2 senders never post 1 row twice; a crash between a claim and its post loses that message (the log and
`status` still have it).
"""

import logging
from collections.abc import Sequence
from http import HTTPStatus
from typing import Literal

from manga_pipeline import db
from manga_pipeline.clients import discord as discord_client
from manga_pipeline.clients import http
from manga_pipeline.db import sql
from manga_pipeline.errors import Unreachable

log = logging.getLogger(__name__)

LIMIT = 2000  # Discord's message length, counted here in UTF-16 code units (never fewer than Discord counts)
DIGEST_OVER = 5  # more unsent reports than this go as 1 digest of the first DIGEST_OVER
CUT = "…"
# A refusal of the webhook itself, not of the message: the row waits for a later run
HOOK_REFUSED = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND})
UNSENT = sql.SQL("sent_at IS NULL AND failed IS NULL")
INCIDENTS = sql.SQL("kind = 'incident'")  # sent first, 1 message each, never in a digest
REPORTS = sql.SQL("kind <> 'incident'")

type Kind = Literal["chapter", "series", "title", "incident"]


def queue(conn: db.Connection, kind: Kind, item_id: int, text: str, fix: str) -> None:
    """Queue 1 report, inside the caller's transaction: what and why (text), and the command that clears it (fix).

    Nothing when the same kind, item and text is queued already (sent or not).
    """
    if not db.in_transaction(conn):
        msg = "notify.queue runs inside the caller's transaction"
        raise RuntimeError(msg)
    conn.execute(
        """INSERT INTO notification (kind, item_id, text, fix, queued_at) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (kind, item_id, md5(text)) DO NOTHING""",
        (kind, item_id, text, fix, db.now(conn)),
    )


def send(conn: db.Connection, discord: discord_client.Discord | None) -> None:
    """Post the unsent incidents, 1 message each, then the other reports: 1 message each, or 1 digest over DIGEST_OVER.

    Oldest first. Discord off (None): nothing. Never raises for Discord: unreachable or refusing the webhook ends the
    send with the row unsent (1 warning); a message Discord refuses is marked failed and the send goes on.
    """
    if discord is None:
        return
    while rows := _claim(conn, INCIDENTS, 1):  # none left (or another sender took the last): on to the reports
        if not _post(conn, discord, rows, message(rows[0]["text"], rows[0]["fix"])):
            return
    unsent = db.one_row(conn, sql.SQL("SELECT count(*) FROM notification WHERE {} AND {}").format(UNSENT, REPORTS))[0]
    if unsent > DIGEST_OVER:
        rows = _claim(conn, REPORTS, None)
        if rows:
            _post(conn, discord, rows, digest(rows))
        return
    for _ in range(unsent):
        rows = _claim(conn, REPORTS, 1)
        if not rows or not _post(conn, discord, rows, message(rows[0]["text"], rows[0]["fix"])):
            return


def message(text: str, fix: str, limit: int = LIMIT) -> str:
    """Return 1 report in at most limit UTF-16 units: its text, cut so that ` -> fix` (if any) always fits."""
    suffix = f" -> {fix}" if fix else ""
    return _cut(text, limit - _units(suffix)) + suffix


def digest(rows: Sequence[db.Row]) -> str:
    """Return 1 message for many reports: their count, the first DIGEST_OVER (each cut to its share), the rest.

    Fewer rows (another sender claimed some meanwhile) are listed whole, with no "the rest".
    """
    more = len(rows) > DIGEST_OVER
    head = f"{len(rows)} reports need a look" + (f"; the first {DIGEST_OVER}:" if more else ":")
    foot = "The rest: `status`."
    share = (LIMIT - _units(head) - _units(foot) - (DIGEST_OVER + 1)) // DIGEST_OVER  # each line ends in a newline
    lines = [f"- {message(r['text'], r['fix'], share - _units('- '))}" for r in rows[:DIGEST_OVER]]
    return "\n".join([head, *lines, foot] if more else [head, *lines])


def _claim(conn: db.Connection, which: sql.Composable, limit: int | None) -> list[db.Row]:
    """Mark the oldest `limit` unsent rows of `which` (all for None) sent and return them, oldest first."""
    with db.transaction(conn):
        rows = conn.execute(
            sql.SQL("""UPDATE notification SET sent_at = %s WHERE id IN
                (SELECT id FROM notification WHERE {} AND {} ORDER BY id LIMIT %s)
            RETURNING id, text, fix""").format(UNSENT, which),
            (db.now(conn), limit),
        ).fetchall()
    return sorted(rows, key=lambda r: r["id"])


def _post(conn: db.Connection, discord: discord_client.Discord, rows: Sequence[db.Row], text: str) -> bool:
    """Post text for the claimed rows; returns whether the send goes on.

    Unreachable or the webhook refused: the rows are unclaimed. The message refused: the rows are marked failed.
    """
    ids = [r["id"] for r in rows]
    try:
        discord.post(text)
    except Unreachable as e:
        _unclaim(conn, ids)
        log.warning("discord: not sent, tried again by the next send: %s", e)
        return False
    except http.HttpError as e:
        if e.status in HOOK_REFUSED or e.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            _unclaim(conn, ids)
            log.warning("discord: not sent, tried again by the next send: %s", e)
            return False
        with db.transaction(conn):
            conn.execute("UPDATE notification SET sent_at = NULL, failed = %s WHERE id = ANY(%s)", (str(e), ids))
        log.warning("discord: %d report(s) refused, never sent (status shows them): %s", len(ids), e)
        return True
    log.info("discord: sent %d report(s)", len(ids))
    return True


def _unclaim(conn: db.Connection, ids: list[int]) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE notification SET sent_at = NULL WHERE id = ANY(%s)", (ids,))


def _units(text: str) -> int:
    """Return text's length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _cut(text: str, limit: int) -> str:
    """Return text cut to at most limit UTF-16 code units, ending in CUT when cut."""
    if _units(text) <= limit:
        return text
    kept = text
    while kept and _units(kept) + _units(CUT) > limit:
        kept = kept[: -max(1, (_units(kept) + _units(CUT) - limit) // 2)]
    return kept + CUT
