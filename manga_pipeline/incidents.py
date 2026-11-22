"""Incidents (SPEC "Needs human"): a stalled part, downloads not taken, Kavita refusing the API key.

Each is queued as a report of kind `incident` (notify), whose text names the incident's start where it has one, so the
queue's unique key sends it once while it lasts. `watch` finds the first 2 every hour and sends what is queued; intake
queues the key incident and its metadata step removes it, once sent, when Kavita answers.
"""

import logging
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from manga_pipeline import config, db, notify
from manga_pipeline.clients import discord as discord_client

log = logging.getLogger(__name__)

STALL = timedelta(hours=24)  # a part with no successful run for longer is stalled
UNMATCHED = timedelta(hours=24)  # a download not taken for longer is reported
SUCCESS = ".last_success"  # meta: <owner>.last_success, written by every run that ended ok
STALL_FIX = "status (Runs), then the part's log on its machine"
DOWNLOADS_FIX = "status (Downloads not taken: each file's reason)"
KEY_REFUSED = "Kavita refuses the API key"
KEY_FIX = (
    f"set kavita.api_key ({config.ENV_PREFIX}_KAVITA_API_KEY in /etc/manga-pipeline/pipeline.env)"
    " to the Kavita user's API key"
)


def watch(cfg: config.Config, conn: db.Connection) -> None:
    """Queue the stalled parts and the folders of downloads not taken, then send what is queued (`watch`)."""
    found = queue_stalls(conn) + queue_downloads(conn)
    for text in found:
        log.warning("incident: %s", text)
    if not found:
        log.info("no open incident")
    notify.send(conn, discord_client.from_config(cfg))


def queue_stalls(conn: db.Connection) -> list[str]:
    """Queue 1 incident per owner whose last success is over STALL old; return their texts."""
    found: list[str] = []
    with db.transaction(conn):
        now = datetime.fromisoformat(db.now(conn))
        for key, value in conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall():
            if key.endswith(SUCCESS) and now - datetime.fromisoformat(value) > STALL:
                text = f"{key.removesuffix(SUCCESS)}: no successful run since {value}"
                notify.queue(conn, "incident", 0, text, STALL_FIX)
                found.append(text)
    return found


def queue_downloads(conn: db.Connection) -> list[str]:
    """Queue 1 incident per folder whose oldest download not taken is over UNMATCHED old; return their texts.

    The reason stays out of the text: some carry a count that changes, and the text keeps the incident once.
    """
    oldest: dict[str, str] = {}
    found: list[str] = []
    with db.transaction(conn):
        now = datetime.fromisoformat(db.now(conn))
        for path, first_seen in conn.execute("SELECT path, first_seen FROM unmatched_download").fetchall():
            folder = str(PurePosixPath(path).parent)
            oldest[folder] = min(first_seen, oldest.get(folder, first_seen))  # stored times compare as text
        for folder, first_seen in sorted(oldest.items()):
            if now - datetime.fromisoformat(first_seen) > UNMATCHED:
                text = f"downloads not taken in {folder} since {first_seen}"
                notify.queue(conn, "incident", 0, text, DOWNLOADS_FIX)
                found.append(text)
    return found


def queue_key_refused(conn: db.Connection) -> None:
    """Queue the key incident: 1 row while it is open (its text is fixed)."""
    with db.transaction(conn):
        notify.queue(conn, "incident", 0, KEY_REFUSED, KEY_FIX)


def clear_key_refused(conn: db.Connection) -> None:
    """End the key incident once sent (or refused by Discord): the next refusal is a new message.

    An unsent row stays, so every refusal is sent at least once.
    """
    with db.transaction(conn):
        conn.execute(
            """DELETE FROM notification WHERE kind = 'incident' AND item_id = 0 AND md5(text) = md5(%(t)s)
            AND text = %(t)s AND (sent_at IS NOT NULL OR failed IS NOT NULL)""",
            {"t": KEY_REFUSED},
        )
