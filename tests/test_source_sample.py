"""Source review's samples (SPEC "Source review", Samples), and the review run on a Windows share root."""

import io
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from manga_pipeline import config, intake, process, source_sample, validation

from tests.builders import settle
from tests.source_world import FILES, candidate, link, main, make_world, pages, zipped
from tests.timing import budget
from tests.world import credit_page


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    for handler in logging.getLogger("manga_pipeline").handlers[:]:
        logging.getLogger("manga_pipeline").removeHandler(handler)
        handler.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def offered(w, images_of, numbers=range(1, FILES + 1), names=None):
    """Add the candidate manga; set the pages its reader serves: images_of(n) for each chapter n.

    Returns (manga id, {number: chapter id}).
    """
    manga, chapters = candidate(w, numbers, names=names)
    for n, chapter_id in chapters.items():
        w.suwayomi.set_pages(chapter_id, images_of(n))
    return manga, chapters


def sample(w, capsys, manga, *more):
    return main(w, capsys, "source", "sample", "1", str(manga), *more)


def file_line(lines, file_id):
    return next(line for line in lines if line.startswith(f"file {file_id} "))


def scratch(w):
    return config.abs_path(w.cfg["paths"]["scratch"]) / source_sample.FOLDER


def downloads(w):
    return sorted(p for p in w.mangas.rglob("*") if p.is_file())


@budget(load="db", seconds=4)
def test_a_wider_copy_would_replace_its_files_and_the_sample_touches_no_download(world, capsys):
    manga, _ = offered(world, lambda n: pages(24, seed=str(n)))
    stale = scratch(world) / "1-7" / "3.cbz"  # left by a killed run
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"PK")
    before = downloads(world)
    ran = sample(world, capsys, manga)
    assert ran.code == 0, ran
    for file_id in (3, 4, 5):
        assert file_line(ran.lines, file_id).endswith(": better: would replace"), ran.lines
    assert "  candidate: pages 3, width 24, strip length 4500," in "\n".join(ran.lines)
    assert ran.lines[-1].startswith("junk counts only what the series' stored decisions remove: a lower bound")
    assert [w[0] for w in world.suwayomi.writes].count("chapter_pages") == 3, "pages come through the reader API"
    assert not [w for w in world.suwayomi.writes if w[0] in ("enqueue", "start_downloader", "set_in_library")]
    assert downloads(world) == before, "nothing is written under mangas/"
    assert list(scratch(world).iterdir()) == [], "a killed run's files are emptied and the sample's folder is gone"
    assert world.rows("SELECT count(*) FROM junk_scan") == [(0,)], "a sample is stored nowhere"
    assert world.rows("SELECT count(*) FROM junk_seen") == [(0,)]


@budget(load="db", seconds=4)
def test_a_recompressed_copy_is_a_duplicate(world, capsys):
    world.rows(
        "UPDATE published_file SET pages_in = 3, median_page_width = 16, strip_length = 4500, bpp = 50 RETURNING id"
    )
    manga, _ = offered(world, lambda n: pages(16, seed=str(n)))
    ran = sample(world, capsys, manga, "--chapters", "1")
    assert ran.code == 0, ran
    assert "file 5 #5 / chapter" in ran.lines[1]
    assert ": duplicate: would be dropped (bits per pixel " in ran.lines[1], ran.lines
    assert ran.lines[3] == "  published: pages 3, width 16, strip length 4500, bits per pixel 50.00, junk 0, rating 1"


@budget(load="db", seconds=4)
def test_an_uncensored_copy_is_better_by_its_rating(world, capsys):
    manga, _ = offered(world, lambda n: pages(16, seed=str(n)), names={5: "Chapter 5 [R18]"})
    ran = sample(world, capsys, manga)
    assert ran.code == 0, ran
    assert file_line(ran.lines, 5).endswith(": better: would replace"), "a higher edition rating wins"
    assert file_line(ran.lines, 4).endswith(
        ": duplicate: would be dropped (no better in width, bits per pixel or junk)"
    ), "the same rating and measures"


@budget(load="db", seconds=4)
def test_the_units_junk_would_offer_are_listed_and_the_junk_called_a_lower_bound(world, capsys):
    manga, _ = offered(world, lambda n: [*pages(16, n=2, seed=str(n)), credit_page()])
    ran = sample(world, capsys, manga, "--chapters", "1")
    assert ran.code == 0, ran
    assert "  for review: frame (width)" in ran.lines, "the wider page's frame, as the junk stage would offer it"
    assert "a lower bound" in ran.lines[-1]
    assert world.rows("SELECT count(*) FROM junk_candidate") == [(0,)], "nothing offered is stored"


@budget(load="db", seconds=4)
def test_a_rotated_page_and_a_file_with_no_match_are_left_out(world, capsys):
    def rotated(n):
        out = pages(24, seed=str(n))
        if n == 5:
            exif = Image.Exif()
            exif[0x0112] = 6
            data = io.BytesIO()
            Image.new("RGB", (24, 36), "gray").save(data, "JPEG", exif=exif)
            out[1] = data.getvalue()
        return out

    manga, _ = offered(world, rotated)
    world.suwayomi.add_chapter(manga, "Chapter 4", scanlator="Other")  # 2 groups list key 4, neither the file's
    ran = sample(world, capsys, manga)
    assert ran.code == 0, ran
    assert file_line(ran.lines, 3).endswith(": better: would replace")
    assert file_line(ran.lines, 4) == "file 4 #4: no match on the manga: left out"
    assert file_line(ran.lines, 5).endswith("has EXIF orientation 6 (not upright): left out"), ran.lines


@budget(load="db", seconds=4)
def test_the_sample_folder_is_gone_after_a_failure(world, capsys, monkeypatch):
    manga, _ = offered(world, lambda n: pages(24, seed=str(n)))

    def broken(*_args, **_kwargs):
        msg = "validation broke"
        raise RuntimeError(msg)

    monkeypatch.setattr(validation, "check", broken)
    assert sample(world, capsys, manga).code == 1
    assert list(scratch(world).iterdir()) == [], "the sample's folder is deleted after a failure"


@budget(load="db", seconds=5)
def test_a_copy_with_2_extra_credit_pages_gets_the_outcome_a_download_of_it_gets(world, capsys):
    padded = [*pages(16, seed="5"), credit_page("a"), credit_page("b")]
    manga, chapters = offered(world, lambda _n: padded)
    ran = sample(world, capsys, manga, "--chapters", "1")
    assert ran.code == 0, ran
    assert ": not the same chapter: a person decides (pages_in 5 against 3)" in ran.lines[1], ran.lines
    link(world, manga)
    world.suwayomi.manga_rows[manga]["inLibrary"] = True
    downloaded = world.suwayomi.path(chapters[5])  # the same chapter, downloaded
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(zipped(padded))
    settle(downloaded)
    world.suwayomi.chapter_rows[chapters[5]]["isDownloaded"] = True
    for part in (intake, intake, process):
        world.run(part)
    ((status, reason),) = world.rows("SELECT status, last_error FROM chapter WHERE kind = 'download'")
    assert status == "needs_human"
    assert "not the same chapter" in reason
    assert reason.endswith("pages_in 5 against 3"), "the same rule, the same measures"


# ---------------------------------------------------------------- a Windows share root (SPEC "Source review")


@pytest.fixture
def mapped_drive(world):
    """Map a free drive letter onto the world's share (subst: no admin rights), as a mapped drive gives one.

    The letter is unmapped after the test. A letter another test's worker took meanwhile makes subst fail: the next one
    is tried.
    """
    subst = Path(os.environ["SYSTEMROOT"]) / "System32" / "subst.exe"
    for letter in "ZYXWVUTSRQPONMLKJIH":
        drive = f"{letter}:"
        taken = Path(f"{drive}\\").exists()
        if not taken and subprocess.run([subst, drive, world.share], capture_output=True, check=False).returncode == 0:
            break
    else:
        pytest.fail("no free drive letter to map the share onto")
    yield drive
    subprocess.run([subst, drive, "/D"], capture_output=True, check=True)


def windows_roots(cfg, share, base):
    r"""Return cfg with every share root moved from the folder share to base, written with backslashes.

    base: a loopback administrative share (\\localhost\C$\...) or a mapped drive (Z:), as another Windows machine
    writes the root.
    """
    paths = dict(cfg["paths"])
    for root in config.SHARE_ROOTS:
        paths[root] = base + "\\" + str(Path(paths[root]).resolve().relative_to(share.resolve()))
    return {**cfg, "paths": paths}


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows share root")
@budget(load="db", seconds=6)
@pytest.mark.parametrize("form", ["unc", "drive"])
def test_measure_and_sample_read_the_library_under_a_windows_share_root(world, capsys, request, form):
    share = world.share.resolve()
    unc = f"\\\\localhost\\{share.drive[0]}$\\{str(share)[3:]}"
    base = unc if form == "unc" else request.getfixturevalue("mapped_drive")
    cfg = windows_roots(world.cfg, world.share, base)
    assert cfg["paths"]["library"].startswith(base + "\\")
    assert Path(cfg["paths"]["library"]).is_dir(), "the root reaches the share's library folder"
    ran = main(world, capsys, "source", "measure", "1", cfg=cfg)
    assert ran.code == 0, ran
    files = [line for line in ran.lines if line.startswith("file ")]
    assert len([line for line in files if "width 16, bits per pixel -," in line]) == FILES, "read under the root"
    manga, _ = offered(world, lambda n: pages(24, seed=str(n)))
    ran = main(world, capsys, "source", "sample", "1", str(manga), "--chapters", "1", cfg=cfg)
    assert ran.code == 0, ran
    assert file_line(ran.lines, 5).endswith(": better: would replace")
    assert "  published: pages 3, width 16," in ran.lines[3], "the library file read under the root"
