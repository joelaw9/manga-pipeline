"""The re-download of a bad download through intake (SPEC "Bad downloads"): validate's state machine and the
suwayomi_download wait, against FakeSuwayomi."""

import argparse
import json

import pytest

from manga_pipeline import cli, config, intake, workflow

from tests.timing import budget
from tests.world import World, cbz

BAD = b"PK\x03\x04 truncated download"
OTHER = "Other (EN)"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, sources={"ranking": [OTHER], "download_timeout_hours": 24})
    w.kavita_series("Solo Leveling")
    w.manga_id = w.manga("Solo Leveling")
    for n in [*range(1, 12), *range(13, 21)]:  # the original source lists chapters 1-20 (12 is the download)
        w.suwayomi.add_chapter(w.manga_id, f"Chapter {n}")
    w.other_source = w.suwayomi.add_source("Other")
    w.other = w.suwayomi.add_manga("Solo Leveling", w.other_source, in_library=False)
    w.other_chapters = {n: w.suwayomi.add_chapter(w.other, f"Ch. {n}") for n in range(1, 21)}
    return w


def bad_download(world):
    chapter_id, path = world.download(world.manga_id, "Chapter 12", data=BAD)
    world.run(intake)
    return chapter_id, path


def chapter(world):
    (row,) = world.rows("SELECT stage, status, waiting_on, download_path, suwayomi_chapter_id, source FROM chapter")
    return row


def state(world):
    ((text,),) = world.rows("SELECT redownload FROM chapter")
    return None if text is None else json.loads(text)


def writes(world, *kinds):
    return [w for w in world.suwayomi.writes if w[0] in kinds]


def retained(world, path):
    return config.abs_path(world.cfg["paths"]["retention"]) / path.parent.parent.name / path.parent.name / path.name


@budget(load="db")
def test_a_bad_download_is_removed_and_downloaded_again_from_the_same_chapter_first(world):
    chapter_id, path = bad_download(world)
    assert chapter(world)[:3] == ("validate", "waiting", "suwayomi_download")
    assert not path.exists() and retained(world, path).exists(), "the rejected copy goes the retention way"
    assert writes(world, "delete_downloaded", "enqueue", "start_downloader") == [
        ("delete_downloaded", [chapter_id]),
        ("enqueue", [chapter_id]),
        ("start_downloader",),
    ], "then Suwayomi's mark is cleared, and the same chapter queued and the downloader started"
    assert world.rows("SELECT outcome, content_digest FROM handled_chapter WHERE outcome = 'rejected'") == [
        ("rejected", None)
    ], "an unreadable archive has no digest"
    world.suwayomi.finish_downloads(cbz(seed="good"))
    world.run(intake)
    assert chapter(world) == ("junk", "ready", None, str(path), chapter_id, "Src (EN)"), "the replacement passes"
    assert state(world) is None, "the re-download state is cleared"
    assert world.rows("SELECT count(*) FROM page") == [(3,)], "the replacement's pages are measured and stored"


@budget(load="db")
def test_a_second_bad_copy_goes_to_the_next_source_and_the_chapter_keeps_its_identity(world):
    _, path = bad_download(world)
    ((identity,),) = world.rows("SELECT identity FROM chapter")
    world.suwayomi.finish_downloads(BAD)  # the same source serves a broken copy again
    world.run(intake)
    expected = state(world)["expected"]
    assert (expected["source"], expected["chapter_id"]) == (OTHER, world.other_chapters[12]), "Ch. 12 of Other"
    assert world.suwayomi.manga_rows[world.other]["inLibrary"] is False, "nothing is added to Suwayomi's library"
    assert not path.exists(), "the second bad copy is removed too"
    world.suwayomi.finish_downloads(cbz(seed="other"))
    world.run(intake)
    other_path = world.suwayomi.path(world.other_chapters[12])
    assert chapter(world) == ("junk", "ready", None, str(other_path), world.other_chapters[12], OTHER)
    assert world.rows("SELECT identity FROM chapter") == [(identity,)], "the identity never changes"
    assert world.rows("SELECT count(*) FROM handled_chapter WHERE outcome = 'rejected'") == [(2,)]


@budget(load="db")
def test_a_download_in_error_is_dequeued_and_the_next_source_tried(world):
    chapter_id, _ = bad_download(world)
    world.suwayomi.fail_download(chapter_id)
    world.run(intake)
    assert ("dequeue", [chapter_id]) in world.suwayomi.writes
    assert state(world)["expected"]["source"] == OTHER
    assert state(world)["tried"] == ["Src (EN)"]


@budget(load="db")
def test_a_download_older_than_the_timeout_is_given_up(world):
    bad_download(world)
    with world_conn(world) as conn:
        old = "2000-01-01T00:00:00+00:00"
        conn.execute("UPDATE chapter SET redownload = json_set(redownload, '$.expected.enqueued_at', ?)", (old,))
    world.run(intake)
    assert state(world)["expected"]["source"] == OTHER, "still queued, but too old: the next source"


@budget(load="db")
def test_a_download_still_queued_keeps_waiting(world):
    bad_download(world)
    world.run(intake)
    assert chapter(world)[:3] == ("validate", "waiting", "suwayomi_download")
    assert state(world)["expected"]["source"] == "Src (EN)"


@budget(load="db")
def test_no_source_left_needs_a_human_naming_what_was_tried(world):
    world.suwayomi.chapter_rows.pop(world.other_chapters[12])  # Other lacks chapter 12
    chapter_id, _ = bad_download(world)
    world.suwayomi.fail_download(chapter_id)
    world.run(intake)
    ((status, error),) = world.rows("SELECT status, last_error FROM chapter")
    assert status == "needs_human"
    assert error == "no source left to re-download from (tried: Src (EN), Other (EN))"


@budget(load="db")
def test_every_remaining_source_down_fails_the_chapter_at_an_attempt(world):
    chapter_id, _ = bad_download(world)
    world.suwayomi.unavailable.add(world.other_source)
    world.suwayomi.fail_download(chapter_id)
    world.run(intake)
    ((status, attempts, error),) = world.rows("SELECT status, attempts, last_error FROM chapter")
    assert (status, attempts) == ("ready", 1) and "could not answer" in error
    assert state(world)["tried"] == ["Src (EN)"], "a source that could not answer is not counted as tried"


@budget(load="db")
@pytest.mark.parametrize("call", ["chapter", "delete_downloaded", "enqueue", "start_downloader"])
def test_suwayomi_unreachable_at_any_step_resumes_on_the_next_run(world, monkeypatch, call):
    chapter_id, _ = world.download(world.manga_id, "Chapter 12", data=BAD)
    original = getattr(world.suwayomi, call)

    def down(*args):
        monkeypatch.setattr(world.suwayomi, call, original)
        raise workflow.Unreachable("suwayomi")

    monkeypatch.setattr(world.suwayomi, call, down)
    world.run(intake)
    world.run(intake)
    assert chapter(world)[:3] == ("validate", "waiting", "suwayomi_download"), "the next run carries on"
    assert state(world)["expected"]["chapter_id"] == chapter_id, "the same chapter first, even after an interruption"
    assert world.rows("SELECT count(*) FROM handled_chapter WHERE outcome = 'rejected'") == [(1,)], (
        "the rejection is recorded once"
    )


@budget(load="db")
def test_a_replacement_of_an_unknown_format_becomes_the_chapters_download_and_needs_a_human(world):
    chapter_id, path = bad_download(world)
    world.suwayomi.finish_downloads(_cbz_with(b"\x00\x01 unknown"))
    world.run(intake)
    ((status, download, redownload),) = world.rows("SELECT status, download_path, redownload FROM chapter")
    assert (status, download, redownload) == ("needs_human", str(path), None)


def exhausted(world):
    """A chapter needs_human at validate with no source left; returns its Suwayomi chapter id."""
    world.suwayomi.chapter_rows.pop(world.other_chapters[12])
    chapter_id, _ = bad_download(world)
    world.suwayomi.fail_download(chapter_id)
    world.run(intake)
    assert world.rows("SELECT status FROM chapter") == [("needs_human",)]
    return chapter_id


def command(handler, world, **args):
    handler(world.cfg, argparse.Namespace(**args))


@budget(load="db")
def test_chapter_use_queues_the_chosen_chapter_on_the_next_intake(world):
    exhausted(world)
    chosen = world.suwayomi.add_chapter(world.other, "Chapter 12 (fixed)")
    command(cli.chapter_use, world, chapter=1, suwayomi_chapter=chosen)
    assert world.rows("SELECT status, attempts FROM chapter") == [("ready", 0)]
    world.run(intake)
    assert writes(world, "enqueue")[-1] == ("enqueue", [chosen]) and state(world)["expected"]["chapter_id"] == chosen
    world.suwayomi.finish_downloads(cbz(seed="fixed"))
    world.run(intake)
    assert chapter(world)[:2] == ("junk", "ready"), "the chosen replacement is validated like any other"


@budget(load="db")
def test_chapter_use_on_a_chapter_whose_file_is_in_hand_rejects_that_file_first(world):
    _, path = world.download(world.manga_id, "Chapter 12", data=_cbz_with(b"\x00 unknown format"))
    world.run(intake)
    assert world.rows("SELECT status FROM chapter") == [("needs_human",)]
    command(cli.chapter_use, world, chapter=1, suwayomi_chapter=world.other_chapters[12])
    world.run(intake)
    assert not path.exists() and retained(world, path).exists(), "the file in hand goes the retention way"
    assert world.rows("SELECT outcome FROM handled_chapter WHERE outcome = 'rejected'") == [("rejected",)]
    assert state(world)["expected"]["chapter_id"] == world.other_chapters[12]


@budget(load="db")
@pytest.mark.parametrize("status", ["ready", "done"])
def test_chapter_use_and_drop_refuse_a_chapter_that_is_not_needs_human(world, status):
    world.download(world.manga_id, "Chapter 12")
    world.run(intake)
    with world_conn(world) as conn:
        conn.execute("UPDATE chapter SET status = ?", (status,))
    with pytest.raises(workflow.Refused, match="not needs_human"):
        command(cli.chapter_use, world, chapter=1, suwayomi_chapter=5)
    with pytest.raises(workflow.Refused, match="not needs_human"):
        command(cli.chapter_drop, world, chapter=1)
    with pytest.raises(workflow.Refused, match="no chapter 9"):
        command(cli.chapter_drop, world, chapter=9)


@budget(load="db")
def test_chapter_drop_removes_the_download_and_records_it_dropped(world):
    _, path = world.download(world.manga_id, "Chapter 12", data=_cbz_with(b"\x00 unknown format"))
    world.run(intake)
    command(cli.chapter_drop, world, chapter=1)
    assert not path.exists() and retained(world, path).exists()
    assert world.rows("SELECT status, last_error FROM chapter") == [("done", "dropped by a person")]
    assert world.rows("SELECT outcome FROM handled_chapter WHERE outcome = 'dropped'") == [("dropped",)]
    world.run(intake)
    assert world.rows("SELECT count(*) FROM chapter") == [(1,)], "a dropped chapter is not discovered again"


@budget(load="db")
def test_retry_starts_the_source_search_again_from_the_same_chapter(world):
    chapter_id = exhausted(world)
    command(cli.retry, world, chapter=1, series=None)
    world.run(intake)
    assert state(world)["expected"]["chapter_id"] == chapter_id, "the same chapter first again"
    assert state(world)["tried"] == []


@budget(load="db")
def test_an_interrupted_chapter_use_keeps_the_choice(world, monkeypatch):
    exhausted(world)
    chosen = world.suwayomi.add_chapter(world.other, "Chapter 12 (fixed)")
    command(cli.chapter_use, world, chapter=1, suwayomi_chapter=chosen)
    original = world.suwayomi.enqueue

    def down(ids):
        monkeypatch.setattr(world.suwayomi, "enqueue", original)
        raise workflow.Unreachable("suwayomi")

    monkeypatch.setattr(world.suwayomi, "enqueue", down)
    world.run(intake)
    world.run(intake)
    assert writes(world, "enqueue")[-1] == ("enqueue", [chosen]), "the chosen chapter is queued on the next run"
    assert state(world)["expected"]["chapter_id"] == chosen and state(world)["chosen"] is None


@budget(load="db")
def test_a_download_finished_after_the_timeout_is_still_taken(world):
    chapter_id, path = bad_download(world)
    world.suwayomi.finish_downloads(cbz(seed="late"))
    with world_conn(world) as conn:
        old = "2000-01-01T00:00:00+00:00"
        conn.execute("UPDATE chapter SET redownload = json_set(redownload, '$.expected.enqueued_at', ?)", (old,))
    world.run(intake)
    assert chapter(world)[:2] == ("junk", "ready"), "a finished download is judged, however late intake looks at it"


@budget(load="db")
def test_a_download_neither_queued_nor_downloaded_is_queued_again(world):
    chapter_id, _ = bad_download(world)
    world.suwayomi.queue.clear()  # Suwayomi dropped it
    world.run(intake)
    assert writes(world, "enqueue")[-1] == ("enqueue", [chapter_id]) and chapter(world)[1] == "waiting"
    assert state(world)["tried"] == [], "no source is given up for it"


@budget(load="db")
def test_a_download_flagged_without_its_file_fails_its_source(world):
    chapter_id, path = bad_download(world)
    world.suwayomi.finish_downloads(cbz(seed="good"))
    path.unlink()
    world.run(intake)
    assert state(world)["tried"] == ["Src (EN)"] and state(world)["expected"]["source"] == OTHER


@budget(load="db")
def test_a_chosen_download_numbered_otherwise_needs_a_human(world):
    exhausted(world)
    other_number = world.suwayomi.add_chapter(world.other, "Chapter 13")
    command(cli.chapter_use, world, chapter=1, suwayomi_chapter=other_number)
    world.run(intake)
    world.suwayomi.finish_downloads(cbz(seed="13"))
    world.run(intake)
    ((status, error, download),) = world.rows("SELECT status, last_error, download_path FROM chapter")
    assert status == "needs_human" and "numbered" in error, "never published under the chapter's number"
    assert download == str(world.suwayomi.path(other_number)), "it is the chapter's download now: drop removes it"


@budget(load="db")
def test_a_chosen_download_numbered_12_10_is_never_taken_for_chapter_12_1(world):
    chapter_id, _ = world.download(world.manga_id, "Chapter 12.1", data=BAD)
    world.run(intake)
    world.suwayomi.fail_download(chapter_id)
    world.run(intake)
    assert world.rows("SELECT status FROM chapter") == [("needs_human",)], "the other source lists no chapter 12.1"
    chosen = world.suwayomi.add_chapter(world.other, "Chapter 12.10")
    command(cli.chapter_use, world, chapter=1, suwayomi_chapter=chosen)
    world.run(intake)
    world.suwayomi.finish_downloads(cbz(seed="12.10"))
    world.run(intake)
    ((status, error, identity),) = world.rows("SELECT status, last_error, identity FROM chapter")
    assert status == "needs_human" and "'12.10'" in error and "'12.1')" in error, (
        "a number with 2 readings is numbered otherwise than chapter 12.1: never published as it"
    )
    assert json.loads(identity)[3] == "12.1", "the chapter keeps its own identity"


@budget(load="db")
def test_a_retried_chapter_numbered_otherwise_is_held_again(world):
    test_a_chosen_download_numbered_otherwise_needs_a_human(world)
    command(cli.retry, world, chapter=1, series=None)
    world.run(intake)
    ((stage, status),) = world.rows("SELECT stage, status FROM chapter")
    assert (stage, status) == ("validate", "needs_human"), "the numbering is checked on every look, never published"


@budget(load="db")
def test_a_refused_choice_stays_recorded_until_retry(world):
    exhausted(world)
    command(cli.chapter_use, world, chapter=1, suwayomi_chapter=999999)
    world.run(intake)
    assert world.rows("SELECT status FROM chapter") == [("needs_human",)] and state(world)["chosen"] == 999999
    command(cli.retry, world, chapter=1, series=None)
    assert state(world)["chosen"] is None and state(world)["tried"] == [], "retry forgets it and searches again"


@budget(load="db")
def test_chapter_drop_is_refused_while_re_download_work_is_outstanding(world):
    exhausted(world)
    with world_conn(world) as conn:
        conn.execute(
            "UPDATE chapter SET redownload = json_set(redownload, '$.remove', json_array(json_object('path', 'x.cbz')))"
        )
    with pytest.raises(workflow.Refused, match="outstanding"):
        command(cli.chapter_drop, world, chapter=1)


@budget(load="db")
def test_chapter_use_with_the_file_already_gone_rejects_nothing(world):
    _, path = world.download(world.manga_id, "Chapter 12", data=_cbz_with(b"\x00 unknown format"))
    world.run(intake)
    path.unlink()
    command(cli.chapter_use, world, chapter=1, suwayomi_chapter=world.other_chapters[12])
    world.run(intake)
    assert world.rows("SELECT count(*) FROM handled_chapter WHERE outcome = 'rejected'") == [(0,)]
    assert state(world)["expected"]["chapter_id"] == world.other_chapters[12]


@budget(load="db")
def test_a_suwayomi_chapter_another_open_chapter_holds_is_never_picked(world):
    _, held = world.download(world.other, "Ch. 12")  # the other source's Ch. 12 is already a chapter in hand
    world.suwayomi.manga_rows[world.other]["inLibrary"] = True
    world.run(intake)
    world.suwayomi.manga_rows[world.other]["inLibrary"] = False
    chapter_id, _ = world.download(world.manga_id, "Chapter 12", data=BAD)
    world.run(intake)
    world.suwayomi.fail_download(chapter_id)
    world.run(intake)
    ((status, error),) = world.rows("SELECT status, last_error FROM chapter WHERE suwayomi_chapter_id = ?", chapter_id)
    assert status == "needs_human" and "Other (EN)" in error, "Other's Ch. 12 belongs to the other chapter"
    assert held.exists(), "the other chapter's download is untouched"


@budget(load="db")
@pytest.mark.parametrize("call", ["dequeue", "sources", "search_source", "fetch_manga_and_chapters"])
def test_suwayomi_unreachable_on_the_way_to_the_next_source_resumes(world, monkeypatch, call):
    chapter_id, _ = bad_download(world)
    world.suwayomi.fail_download(chapter_id)
    original = getattr(world.suwayomi, call)

    def down(*args):
        monkeypatch.setattr(world.suwayomi, call, original)
        raise workflow.Unreachable("suwayomi")

    monkeypatch.setattr(world.suwayomi, call, down)
    world.run(intake)
    world.run(intake)
    assert state(world)["expected"]["chapter_id"] == world.other_chapters[12], "the next run carries on to Other"
    assert ("dequeue", [chapter_id]) in world.suwayomi.writes


@budget(load="db")
def test_a_replacement_repeating_another_published_chapter_is_mislabelled(world):
    from manga_pipeline import images, measures

    bad_download(world)
    copy = _textured_cbz("a copy of chapter 30")  # the mislabel check compares page pHashes: pages need content
    probe = world.mangas / "probe.cbz"
    probe.write_bytes(copy)
    phashes = measures.summary(images.measure_pages(probe))["page_phashes"]
    probe.unlink()
    with world_conn(world) as conn:
        for n in (30, 31, 32):
            conn.execute(
                """INSERT INTO published_file (library_path, series_id, identity, state, page_phashes, strip_length,
                    origin) VALUES (?, 1, ?, 'confirmed', ?, 4500, 'pipeline')""",
                (f"/lib/{n}.cbz", json.dumps(["", None, "", str(n), ""]), json.dumps(phashes)),
            )
    world.suwayomi.finish_downloads(copy)
    world.run(intake)
    ((status, error),) = world.rows("SELECT status, last_error FROM chapter")
    assert status == "needs_human" and "mislabelled" in error


@budget(load="db")
def test_status_lists_re_downloads_notes_and_the_commands_that_settle_a_chapter(world):
    from contextlib import closing

    from manga_pipeline import db, status

    bad_download(world)
    world.download(world.manga_id, "Chapter 23", data=_cbz_with(b"\x00 unknown format"))
    page = cbz(seed="dup")
    world.download(world.manga_id, "Chapter 24", data=_cbz_with_pages(page, page))
    world.run(intake)
    with closing(db.connect(world.cfg)) as conn:
        text = "\n".join(status.report(conn))
    assert "waiting on Src (EN) chapter" in text and "tried: none" in text, text
    assert "-> retry 2, chapter use 2 SUWAYOMI_CHAPTER_ID, chapter drop 2" in text
    assert "chapter 3 (Solo Leveling" in text and "duplicate pages: 001.png, 002.png" in text


def _cbz_with_pages(*cbzs):
    """A CBZ whose pages are the first page of each of cbzs."""
    import io
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for i, data in enumerate(cbzs, 1):
            with zipfile.ZipFile(io.BytesIO(data)) as src:
                first = src.namelist()[0]
                z.writestr(f"{i:03}{first[first.rfind('.') :]}", src.read(first))
    return out.getvalue()


@budget(load="db")
def test_the_path_rebuilt_for_a_download_is_where_suwayomi_wrote_it(world):
    source = world.suwayomi.add_source("Manga Ball")
    manga = world.suwayomi.add_manga("A Martial Master Wakes Up as a Concubine", source, in_library=False)
    chapter_id = world.suwayomi.add_chapter(manga, "Chapter 141", scanlator="Articuno")
    found = _target(world, chapter_id)
    # Observed on the real Suwayomi 2026-11-13: a non-library download landed exactly here.
    real = "Manga Ball (EN)/A Martial Master Wakes Up as a Concubine/Articuno_Chapter 141.cbz"
    assert found["path"] == str(world.mangas / real) and found["source"] == "Manga Ball (EN)"


def _target(world, chapter_id):
    from contextlib import closing

    from manga_pipeline import db, redownload

    with closing(db.connect(world.cfg)) as conn:
        return redownload.target(world.context(conn, "intake"), chapter_id)


def _textured_cbz(seed, pages=3):
    """A CBZ of noise pages (16 x 24): unlike the world's 1-colour pages, they have pHashes."""
    import io
    import zipfile
    import zlib

    import numpy as np
    from PIL import Image

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n in range(1, pages + 1):
            rng = np.random.default_rng(zlib.crc32(f"{seed} {n}".encode()))
            im = io.BytesIO()
            Image.fromarray(rng.integers(0, 256, (24, 16, 3), dtype=np.uint8)).save(im, "PNG")
            z.writestr(f"{n:03}.png", im.getvalue())
    return out.getvalue()


def _cbz_with(page):
    import io
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("001.png", page)
    return out.getvalue()


class world_conn:
    """A connection with an open transaction, for a test's direct changes."""

    def __init__(self, world):
        from manga_pipeline import db

        self.conn = db.connect(world.cfg)
        self.tx = db.transaction(self.conn)

    def __enter__(self):
        self.tx.__enter__()
        return self.conn

    def __exit__(self, *exc):
        self.tx.__exit__(*exc)
        self.conn.close()
