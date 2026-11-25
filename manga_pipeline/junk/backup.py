"""`junk backup` and `junk restore` (SPEC "Junk decisions", decision commands): the junk knowledge, ids kept.

A backup is a folder: each table as a PostgreSQL COPY binary file, and columns.txt naming each table's columns
(`table: col, col, …`, 1 line each). Restore writes them back into a database whose junk tables are all empty and have
those columns, in TABLES order (acts before the decisions and log rows that name them), then moves each id sequence
past the largest id restored. No JSON: the server reads and checks the rows.
"""

from datetime import UTC, datetime
from pathlib import Path

from manga_pipeline import config, db
from manga_pipeline.db import sql
from manga_pipeline.junk.review import Refused

# The junk knowledge, in restore order: a table's rows name rows of the tables before it.
TABLES = ("junk_act", "junk_decision", "junk_act_row", "junk_seen", "junk_watermark")
COLUMNS_FILE = "columns.txt"


def _columns(conn: db.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s
        ORDER BY ordinal_position""",
        (table,),
    )
    return [r[0] for r in rows]


def backup(cfg: config.Config, parent: Path) -> Path:
    """Write the junk knowledge into a new folder junk-<UTC time> in parent, from 1 snapshot; return the folder.

    Refused when the folder exists already or no database is built.
    """
    if not db.built(cfg):
        msg = f"no database {cfg['database']['name']} built yet: nothing to back up"
        raise Refused(msg)
    folder = parent / f"junk-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    if folder.exists():
        msg = f"{folder} exists already: a backup never overwrites one"
        raise Refused(msg)
    folder.mkdir(parents=True)
    with db.read_only(cfg) as conn, conn.transaction():  # repeatable read: 1 snapshot for every table
        lines: list[str] = []
        for table in TABLES:
            columns = _columns(conn, table)
            lines.append(f"{table}: {', '.join(columns)}")
            query = sql.SQL("COPY {} ({}) TO STDOUT (FORMAT binary)").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
            )
            with (folder / f"{table}.copy").open("wb") as out, conn.cursor().copy(query) as copy:
                for data in copy:
                    out.write(data)
    (folder / COLUMNS_FILE).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return folder


def _read_columns(folder: Path) -> dict[str, list[str]]:
    """Return columns.txt's table -> columns; Refused for a folder that is no junk backup."""
    path = folder / COLUMNS_FILE
    if not path.is_file():
        msg = f"{folder} is no junk backup: it holds no {COLUMNS_FILE}"
        raise Refused(msg)
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        table, _, names = line.partition(": ")
        out[table] = names.split(", ")
    if sorted(out) != sorted(TABLES) or any(not (folder / f"{t}.copy").is_file() for t in TABLES):
        msg = f"{folder} is no junk backup of {', '.join(TABLES)}"
        raise Refused(msg)
    return out


def restore(conn: db.Connection, folder: Path) -> dict[str, int]:
    """Write a backup's rows back, ids kept, in 1 transaction; return the rows written per table.

    Refused unless every junk table is empty and has every column the backup names.
    """
    wanted = _read_columns(folder)
    with db.transaction(conn):
        for table in TABLES:
            missing = sorted(set(wanted[table]) - set(_columns(conn, table)))
            if missing:
                msg = (
                    f"{table} lacks the column(s) {missing} the backup holds: restore it with the program it came from"
                )
                raise Refused(msg)
            if conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table))).fetchone():
                msg = f"{table} holds rows: a backup is restored only into a database whose junk knowledge is empty"
                raise Refused(msg)
        counts: dict[str, int] = {}
        for table in TABLES:
            query = sql.SQL("COPY {} ({}) FROM STDIN (FORMAT binary)").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, wanted[table]))
            )
            with (folder / f"{table}.copy").open("rb") as src, conn.cursor().copy(query) as copy:
                while data := src.read(1 << 20):
                    copy.write(data)
            counts[table] = db.one_row(conn, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)), ())[0]
        for table in ("junk_act", "junk_decision"):  # the tables with an id sequence
            conn.execute(
                sql.SQL("SELECT setval(pg_get_serial_sequence(%s, 'id'), max(id)) FROM {} HAVING count(*) > 0").format(
                    sql.Identifier(table)
                ),
                (table,),
            )
    return counts
