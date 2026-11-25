"""`junk backup` and `junk restore` (SPEC "Junk decisions", decision commands): the junk knowledge, ids kept."""

from contextlib import closing
from typing import LiteralString

import pytest

from manga_pipeline import cli, db
from manga_pipeline.db import sql
from manga_pipeline.junk import backup, redecide, review, store

from tests.builders import adopted, config_file, one, site_cfg
from tests.timing import budget

AT = "2026-11-25T00:00:00+00:00"


CUT: store.Decision = {
    "series_key": "S",
    "kind": "cut",
    "key": "c1d84387cf25a7a5|0.5",
    "verdict": "junk",
    "decider": "import",
    "decided_at": AT,
    "source": "held row 1",
    "chain": '{"below":[{"ref":[["S","c",0]],"segments":[{"h":0.5,"kind":"cut","tiles":"c1d84387cf25a7a5"}],'
    '"src":{"file":"a.cbz","sha256":"' + "ab" * 32 + '"}}]}',
    "credit": True,
}


def knowledge(conn):
    """Add decisions (1 re-decided, so history and acts), a sighting and a watermark."""
    with db.transaction(conn):
        store.upsert(
            conn,
            [
                {
                    "series_key": "S",
                    "kind": "block",
                    "key": f"{tile:016x}|0.7",
                    "verdict": "junk",
                    "decider": "claude",
                    "decided_at": AT,
                    "source": "candidate 1",
                    "images": "S-a-1",
                }
                for tile in (0x0F0F0F0F0F0F0F0F, 0xF0F0F0F0F0F0F0F0)
            ],
        )
        conn.execute(
            """INSERT INTO junk_seen (series_key, kind, key, identity, first_seen, origin)
            VALUES ('S', 'block', '0f0f0f0f0f0f0f0f|0.7', 'c1', %s, 'pipeline')""",
            (AT,),
        )
        conn.execute("INSERT INTO junk_watermark (series_key, key) VALUES ('S', 'w')")
    redecide.redecide(conn, [one(conn, "SELECT min(id) FROM junk_decision")], "keep", AT)
    with db.transaction(conn):  # a cut decision: its chain and credit travel too
        store.write(conn, [CUT], store.new_act(conn, "cut", AT))


def rows(conn):
    """Return every row of the 5 junk tables, every column, in a fixed order."""
    order = {
        "junk_act": ["id"],
        "junk_decision": ["id"],
        "junk_act_row": ["act", "decision", "change"],
        "junk_seen": ["series_key", "identity"],
        "junk_watermark": ["series_key", "key"],
    }
    out = {}
    for table, columns in order.items():
        query = sql.SQL("SELECT * FROM {} ORDER BY {}").format(
            sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
        )
        out[table] = [tuple(r) for r in conn.execute(query)]
    return out


@pytest.fixture
def source(tmp_path):
    cfg = adopted(tmp_path / "a")
    with closing(db.connect(cfg)) as conn:
        knowledge(conn)
    return cfg


@budget(load="db", seconds=10)
def test_backup_then_restore_into_an_empty_database_gives_equal_rows(source, tmp_path, capsys):
    folder = backup.backup(source, tmp_path / "backups")
    target = site_cfg(tmp_path / "b", built=False)
    assert cli.main(["--config", str(config_file(tmp_path / "b", target)), "junk", "restore", str(folder)]) == 0
    assert "junk_decision 4" in capsys.readouterr().out, "the restore names what it wrote"
    with closing(db.connect(source)) as a, closing(db.connect(target)) as b:
        assert rows(b) == rows(a), "every row of the 5 tables, every column, ids included"


@budget(load="db", seconds=10)
def test_a_restored_store_writes_new_ids_past_the_restored_ones(source, tmp_path):
    folder = backup.backup(source, tmp_path / "backups")
    target = adopted(tmp_path / "b")
    with closing(db.connect(target)) as conn:
        backup.restore(conn, folder)
        top = one(conn, "SELECT max(id) FROM junk_decision")
        keep = one(conn, "SELECT max(id) FROM junk_decision WHERE verdict = 'keep'")  # the re-decided row
        act = redecide.redecide(conn, [keep], "junk", AT)
        assert one(conn, "SELECT max(id) FROM junk_decision") == top + 1, "a new decision takes the next id"
        assert act == one(conn, "SELECT max(id) FROM junk_act"), "a new act takes the next id"
        assert act > 1, "past the restored act"


# 1 row in 1 junk table alone (junk_act_row names an act and a decision, so it never holds rows alone).
ONE_ROW: dict[str, LiteralString] = {
    "junk_act": "INSERT INTO junk_act (command, at) VALUES ('x', %(at)s)",
    "junk_decision": """INSERT INTO junk_decision (series_key, kind, key, verdict, decider, decided_at)
        VALUES ('T', 'block', 'k', 'junk', 'human', %(at)s)""",
    "junk_seen": """INSERT INTO junk_seen (series_key, kind, key, identity, first_seen, origin)
        VALUES ('T', 'block', 'k', 'c9', %(at)s, 'pipeline')""",
    "junk_watermark": "INSERT INTO junk_watermark (series_key, key) VALUES ('T', 'w')",
}


@budget(load="db", seconds=10)
@pytest.mark.parametrize("table", sorted(ONE_ROW))
def test_restore_refuses_a_database_holding_a_row_in_any_junk_table(source, tmp_path, table):
    folder = backup.backup(source, tmp_path / "backups")
    target = adopted(tmp_path / "b")
    with closing(db.connect(target)) as conn:
        conn.execute(ONE_ROW[table], {"at": AT})
        with pytest.raises(review.Refused, match=f"{table} holds rows"):
            backup.restore(conn, folder)
        assert one(conn, "SELECT count(*) FROM junk_watermark") == (table == "junk_watermark"), "nothing is written"


@budget(load="db", seconds=10)
@pytest.mark.parametrize("drop", ["columns.txt", "junk_seen.copy"])
def test_restore_refuses_a_folder_that_is_no_whole_backup(source, tmp_path, drop):
    folder = backup.backup(source, tmp_path / "backups")
    (folder / drop).unlink()
    target = adopted(tmp_path / "b")
    with closing(db.connect(target)) as conn, pytest.raises(review.Refused, match="is no junk backup"):
        backup.restore(conn, folder)


@budget(load="db", seconds=10)
def test_restore_refuses_a_table_lacking_a_column(source, tmp_path):
    folder = backup.backup(source, tmp_path / "backups")
    columns = folder / backup.COLUMNS_FILE
    text = columns.read_text(encoding="utf-8").replace(
        "junk_watermark: series_key", "junk_watermark: series_key, extra"
    )
    columns.write_bytes(text.encode("utf-8"))
    target = adopted(tmp_path / "b")
    with closing(db.connect(target)) as conn, pytest.raises(review.Refused, match=r"lacks the column\(s\) \['extra'\]"):
        backup.restore(conn, folder)


@budget(load="db", seconds=10)
def test_backup_refuses_an_existing_folder_and_no_database(source, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "now", lambda _conn: AT)
    assert backup.backup(source, tmp_path / "backups").name == "junk-20261125-000000", "named by the server's clock"
    with pytest.raises(review.Refused, match="exists already"):
        backup.backup(source, tmp_path / "backups")
    with pytest.raises(review.Refused, match="nothing to back up"):
        backup.backup(site_cfg(tmp_path / "c", built=False), tmp_path / "backups2")
