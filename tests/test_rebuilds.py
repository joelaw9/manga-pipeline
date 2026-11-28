"""Rebuilds (SPEC "Retroactive updates"; plan phase-10-9d): a pipeline file rebuilt from its original in retention."""

import shutil
import zipfile
from contextlib import closing

import pytest

from manga_pipeline import archive, cli, comicinfo, config, conversions, db, export, intake, process, status, workflow
from manga_pipeline.db import sql
from manga_pipeline.junk import redecide

from tests.builders import config_file
from tests.test_better_copies import SERIES, library, pages, through
from tests.test_conversions import SERIES as ADOPTED
from tests.test_conversions import adopt, set_cap
from tests.test_rewrites import scan
from tests.timing import budget
from tests.world import World, cbz, decide_junk


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def manga(world):
    found = next((i for i, m in world.suwayomi.manga_rows.items() if m["title"] == SERIES), None)
    return found or world.manga(SERIES)


def published(world, name="Chapter 3", data=None):
    """Publish and confirm `name` (4 pages unless data) in the bulk series; return its file's row."""
    library(world)
    world.download(manga(world), name, data=data or pages(16, 4, name))
    through(world)
    scan(world)
    world.run(export)  # confirms it
    ((row,),) = world.rows("SELECT to_jsonb(p) FROM published_file p WHERE origin = 'pipeline' AND state = 'confirmed'")
    return row


def file_row(world, file_id):
    ((row,),) = world.rows("SELECT to_jsonb(p) FROM published_file p WHERE id = %s", file_id)
    return row


def rebuild(world, *file_ids):
    with closing(db.connect(world.cfg)) as conn:
        return conversions.rebuild(world.cfg, conn, list(file_ids))


def run_rebuild(world):
    for part in (intake, process, export):
        world.run(part)


def fields(world, row):
    with zipfile.ZipFile(config.to_local(world.cfg, row["library_path"])) as z:
        return comicinfo.read_all(z.read(archive.COMICINFO))


KEPT = ("identity", "source", "source_name", "kind", "library_path")


@budget(load="db", seconds=30)
def test_a_rebuild_replaces_the_file_from_its_original_keeping_its_name_source_and_identity(world):
    before = published(world)
    xml = fields(world, before)
    assert rebuild(world, before["id"]) == (1, []), "queued"
    ((path,),) = world.rows("SELECT download_path FROM chapter WHERE status <> 'done'")
    assert path == f"staging/adopted/{before['id']}/Chapter 3.cbz", "the link is named as the download was"
    run_rebuild(world)
    after = file_row(world, before["id"])
    assert {k: after[k] for k in KEPT} == {k: before[k] for k in KEPT}, "the row keeps the file's own fields"
    assert after["original"] != before["original"], "the rebuild's download is the original now"
    assert after["original"].startswith(f"retention/adopted/{before['id']}/"), "kept where a link is put aside"
    assert archive.pages_digest(config.to_local(world.cfg, after["original"])) == archive.pages_digest(
        config.to_local(world.cfg, before["original"])
    ), "the same pages: 1 inode under 2 names"
    assert after["previous"] is not None, "the replaced file is kept"
    now = fields(world, after)
    assert (now.get("Notes"), now.get("Web")) == (xml.get("Notes"), xml.get("Web")), "its source and URL kept"
    assert world.rows("SELECT count(*) FROM chapter WHERE status <> 'done'") == [(0,)], "nothing left open"


@budget(load="db", seconds=40)
def test_a_second_rebuild_takes_the_url_and_suwayomi_chapter_of_the_first(world):
    before = published(world)
    ((url, suwayomi_id),) = world.rows("SELECT source_url, suwayomi_chapter_id FROM chapter WHERE id = 1")
    rebuild(world, before["id"])
    run_rebuild(world)
    scan(world)
    world.run(export)  # Kavita re-read the rebuilt file: it is no longer busy
    assert rebuild(world, before["id"]) == (1, []), "the newest download chapter is the first rebuild"
    assert world.rows("SELECT source_url, suwayomi_chapter_id FROM chapter WHERE status <> 'done'") == [
        (url, suwayomi_id)
    ]
    run_rebuild(world)
    after = file_row(world, before["id"])
    assert {k: after[k] for k in KEPT} == {k: before[k] for k in KEPT}, "a second rebuild keeps them too"
    assert "(2)" in after["original"], "the original under its next free name, parsed by its source name"


@budget(load="db", seconds=30)
def test_a_decision_changed_after_publish_reaches_the_file_by_its_rebuild(world):
    library(world)
    world.download(manga(world), "Chapter 3", data=cbz(pages=4, seed="3", credit=True))
    for _ in range(3):  # the credit page waits for a decision; a person keeps it
        through(world)
        decide_junk(world.cfg, "keep")
    through(world)
    scan(world)
    world.run(export)
    ((file_id, pages_in),) = world.rows("SELECT id, pages_in FROM published_file WHERE origin = 'pipeline'")
    assert pages_in == 5, "set up: published with its credit page"
    with closing(db.connect(world.cfg)) as conn:
        ((keep,),) = conn.execute("SELECT id FROM junk_decision WHERE verdict = 'keep' AND decider = 'human'")
        redecide.redecide(conn, [keep], "junk", workflow.now(conn))
    rebuild(world, file_id)
    run_rebuild(world)
    after = file_row(world, file_id)
    with zipfile.ZipFile(config.to_local(world.cfg, after["library_path"])) as z:
        assert len(archive.archive_pages(z.namelist())) == 4, "the newly decided junk page is gone"


@budget(load="db", seconds=30)
def test_each_file_a_rebuild_cannot_take_is_listed_and_the_rest_queued(world):
    row = published(world)
    bulk = world.rows("SELECT min(id) FROM published_file WHERE origin = 'bulk'")[0][0]
    queued, listed = rebuild(world, 999999, row["id"], row["id"], bulk)
    assert queued == 1
    assert listed == [
        "file 999999: no such published file",
        f"file {row['id']}: named twice",
        f"file {bulk}: origin bulk: only a pipeline file is rebuilt",
    ]
    assert rebuild(world, row["id"]) == (0, [f"file {row['id']}: queued"]), "its rebuild is open already"


@budget(load="db", seconds=30)
@pytest.mark.parametrize(
    ("change", "why"),
    [
        ("UPDATE published_file SET state = 'unconfirmed' WHERE id = %s", "not confirmed yet"),
        ("UPDATE published_file SET joined_parts = 2 WHERE id = %s", "a joined file: no single original"),
        ("UPDATE published_file SET original = NULL WHERE id = %s", "no original recorded"),
        ("UPDATE published_file SET pending_error = 'stuck' WHERE id = %s", "busy"),
        (
            "UPDATE series SET status = 'needs_human' WHERE id = (SELECT series_id FROM published_file WHERE id = %s)",
            "its series is needs_human",
        ),
        ('UPDATE published_file SET identity = \'["", null, "", "7", ""]\' WHERE id = %s', "would be parsed as"),
    ],
    ids=["unconfirmed", "joined", "no-original", "busy", "series", "identity"],
)
def test_a_file_a_rebuild_cannot_take_now_is_listed_with_why(world, change, why):
    row = published(world)
    world.rows(change + " RETURNING 1", row["id"])
    queued, listed = rebuild(world, row["id"])
    assert queued == 0
    assert len(listed) == 1
    assert why in listed[0]


@budget(load="db", seconds=30)
def test_an_original_gone_from_retention_is_listed(world):
    row = published(world)
    config.to_local(world.cfg, row["original"]).unlink()
    assert rebuild(world, row["id"]) == (
        0,
        [f"file {row['id']}: its original {row['original']} is no longer in retention"],
    )


@budget(load="db", seconds=30)
def test_an_original_kept_under_a_taken_name_is_parsed_by_the_files_source_name(world):
    row = published(world)
    kept = config.to_local(world.cfg, row["original"])
    taken = kept.with_name(f"{kept.stem} (2){kept.suffix}")
    shutil.move(kept, taken)
    world.rows(
        "UPDATE published_file SET original = %s WHERE id = %s RETURNING 1",
        config.to_stored(world.cfg, taken),
        row["id"],
    )
    assert rebuild(world, row["id"]) == (1, []), "not refused: discover parses the link, named by the source name"


@budget(load="db", seconds=30)
def test_a_rebuild_whose_original_is_gone_at_discover_is_held_and_never_links_the_published_file(world):
    row = published(world)
    rebuild(world, row["id"])
    world.rows("UPDATE published_file SET original = NULL WHERE id = %s RETURNING 1", row["id"])
    world.run(intake)
    ((status_, error),) = world.rows("SELECT status, last_error FROM chapter WHERE status <> 'done'")
    assert status_ == "needs_human"
    assert "never links the published file" in error


@budget(load="db", seconds=30)
def test_the_newest_download_chapter_of_another_source_is_not_taken(world):
    row = published(world)
    world.rows(  # a given-up copy from another source, of the same download name
        """INSERT INTO chapter (series_id, kind, identity, number, download_path, source, source_url, stage, status,
            content_digest, discovered_at, updated_at)
        SELECT series_id, kind, identity, number, download_path, 'Elsewhere', 'https://elsewhere/3', stage, status,
            content_digest, discovered_at, updated_at
        FROM chapter WHERE id = 1 RETURNING id"""
    )
    ((url,),) = world.rows("SELECT source_url FROM chapter WHERE id = 1")
    rebuild(world, row["id"])
    assert world.rows("SELECT source_url FROM chapter WHERE status <> 'done'") == [(url,)]


@budget(load="db", seconds=30)
def test_a_rebuild_counts_and_claims_with_conversions_and_holds_no_new_download(world):
    row = published(world)
    rebuild(world, row["id"])
    world.download(manga(world), "Chapter 4", data=pages(16, 4, "4"))
    world.run(intake)  # links the rebuild; discovers and validates chapter 4
    assert world.rows("SELECT stage FROM chapter WHERE status = 'ready' ORDER BY id") == [("junk",), ("junk",)]
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, "process")
        first = workflow.claim(ctx, ["junk"])
        assert first is not None
        assert first["published_file_id"] is None, "the new download first: the rebuild is claimed last"
    rows = world.rows(
        db.sql.SQL("SELECT c.published_file_id IS NOT NULL FROM chapter c WHERE {} ORDER BY c.id").format(
            workflow.REPLACES_ADOPTED.format(c=sql.Identifier("c"))
        )
    )
    assert rows == [(True,)], "the rebuild counts with the conversions"
    assert "1 rebuild(s) open (0 waiting at junk, 0 needs_human)" in "\n".join(_report(world))


def _report(world):
    with closing(db.read_only(world.cfg)) as conn:
        return status.report(conn)


@budget(load="db", seconds=10)
def test_a_chapter_with_no_download_is_no_conversion_nor_rebuild(world):
    with closing(db.connect(world.cfg)) as conn:
        query = sql.SQL(
            "SELECT NOT {adopted} AS not_adopted, NOT {rebuild} AS not_rebuild"
            " FROM (VALUES (NULL::text, NULL::bigint)) AS c(download_path, published_file_id)"
        ).format(
            adopted=workflow.REPLACES_ADOPTED.format(c=sql.Identifier("c")),
            rebuild=workflow.REBUILD.format(c=sql.Identifier("c")),
        )
        row = db.one_row(conn, query)
        assert (row["not_adopted"], row["not_rebuild"]) == (True, True), (
            "a rewrite or join chapter (no download) stays in OPEN_REWRITES: false, never null"
        )


@budget(load="db", seconds=40)
def test_a_converted_adopted_file_is_rebuilt_from_its_adopted_original(tmp_path):
    world = World(tmp_path)
    data = cbz(seed="a1", pages=4)
    adopt(world, {"Ch.1.cbz": data})
    set_cap(world, 1)
    for part in (intake, process, export):
        world.run(part)
    ((file_id, original),) = world.rows("SELECT id, original FROM published_file")
    assert original is not None, "set up: the conversion recorded the adopted bytes as its original"
    world.kavita.run_scans()
    scan(world)
    world.run(export)  # Kavita re-read the converted file: it is no longer busy
    assert rebuild(world, file_id) == (1, [])
    for part in (intake, process, export):
        world.run(part)
    after = file_row(world, file_id)
    assert after["library_path"] == f"library/{ADOPTED}/Ch.1.cbz"
    assert after["original"] != original, "converted again from the adopted bytes"
    assert archive.pages_digest(config.to_local(world.cfg, after["original"])) == archive.pages_digest(
        config.to_local(world.cfg, original)
    )


@budget(load="db", seconds=20)
def test_rewrite_rebuild_lists_and_exits_1_and_needs_the_share(world, tmp_path, capsys):
    row = published(world)
    path = str(config_file(tmp_path, world.cfg))
    assert cli.main(["--config", path, "rewrite", "rebuild", str(row["id"]), "999999"]) == 1
    out = capsys.readouterr().out
    assert "rebuilds queued 1, listed 1" in out
    assert "file 999999: no such published file" in out
    shutil.rmtree(config.abs_path(world.cfg["paths"]["retention"]))
    assert cli.main(["--config", path, "rewrite", "rebuild", str(row["id"])]) == 4, "the share folders are checked"
