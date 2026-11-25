"""The automation's pause (SPEC "Parts", "Run lock"): 1 row of meta, read by every scheduled run before any work.

The row's key and value keep their meaning in every version: a deploy is paused by the old program and the new alike.
"""

import socket

from manga_pipeline import db, lock
from manga_pipeline.db import sql

KEY = "automation_paused"  # meta key; its value is "<server time> <host>", the pause's start and where it was set


def paused(conn: db.Connection) -> str | None:
    """Return the pause ("<server time> <host>") when the automation is paused, else None."""
    row = conn.execute("SELECT value FROM meta WHERE key = %s", (KEY,)).fetchone()
    return None if row is None else row["value"]


def set_pause(conn: db.Connection) -> str:
    """Pause the automation from this host; a pause already set is kept as it is. Return the pause."""
    with db.transaction(conn):  # the read too: a state resume elsewhere cannot clear it between
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            (KEY, f"{db.now(conn)} {socket.gethostname()}"),
        )
        return db.one_row(conn, "SELECT value FROM meta WHERE key = %s", (KEY,))["value"]


def clear(conn: db.Connection) -> bool:
    """End the pause; return whether one was set."""
    with db.transaction(conn):
        return conn.execute("DELETE FROM meta WHERE key = %s", (KEY,)).rowcount == 1


def waiting(conn: db.Connection) -> int:
    """Return how many other sessions hold lock.AUTOMATION: the scheduled runs still running."""
    return db.one_row(conn, sql.SQL("SELECT {}").format(lock.holders_sql(sql.Literal(lock.AUTOMATION))))[0]
