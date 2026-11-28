"""The junk review smoke test (plan phase-10-8, D3; SPEC "Junk decisions", Review).

Runs 1 real junk review on fixture chapters, apart from the pipeline: a scratch database on the local test server
(the test role, which never exists on the pipeline's server) and a scratch share under 1 scratch folder. 1 strip series
holds 3 chapters at junk, made from synthetic images: story panels of noise, then the same 2 tail blocks, a scanlator
credit ("join our discord": a keyword reason, junk) and an official localization credit ("translated by" a publisher:
keyword `translat`, and in all 3 chapters; keep). `process` opens their candidates, then `junk review` runs with the
real Claude. It passes when the review's result is `ok`, it read at least 4 images and showed both candidates, the
credit is junk and the localization credit keep; a verdict miss runs it once more from scratch, a second miss fails.

The scratch config is config.yaml.example with the test login, every path under the scratch folder, and from this
machine's config only the Claude executable, the OCR device and workers and the image tools. Its Claude token
(junk.claude_token, from the file or MANGA_PIPELINE_JUNK_CLAUDE_TOKEN) reaches the commands as that variable, the only
MANGA_PIPELINE_ variable they are given, and is written nowhere. The scratch database
and folder are removed at the end, a failed run's too. Costs about 1 review of tokens a run. No lock: it touches
nothing of the pipeline's.

Usage, from the project root:
python -m scripts.review_smoke [--config PATH]
Exit 0 passed; 1 failed; 2 the machine's config has no Claude token.
"""

import argparse
import functools
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from manga_pipeline import config, db, fleet, state
from manga_pipeline.db import sql
from manga_pipeline.junk import candidates, store
from scripts import set_config

TOKEN = f"{config.ENV_PREFIX}_JUNK_CLAUDE_TOKEN"
DATABASE = "mp_test_smoke"  # on the local test server, as the test role (tests/test_config.yaml's login)
LOGIN = {"host": "localhost", "port": 5432, "user": "mp_test", "password": "mp_test"}
SERIES = "Smoke Strip"
CREDIT = ("JOIN OUR DISCORD", "for early chapters")  # junk
LOCALIZATION = ("Translated by Lezhin Entertainment", "Lettering by Kim Min")  # keep: an official credit
MIN_READS, MIN_SHOWN = 4, 2  # each candidate's 2 images; show on each
WIDTH, PAGE_H, PAGES = 800, 2000, 3
TOOLS = ("avifenc", "avifdec", "jpegtran", "oxipng", "vship")  # config.ToolsConfig
Run = Callable[[Path, list[str]], None]  # runs 1 pipeline command with a config file


def noise(seed: int, size: tuple[int, int]) -> Image.Image:
    """Return a panel of coloured noise: story art with no text."""
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8), "RGB")


def text_box(lines: tuple[str, ...]) -> Image.Image:
    """Return a framed box of large black text on white, as a credit block."""
    font = ImageFont.load_default(size=36)
    im = Image.new("RGB", (WIDTH - 100, 120 + 70 * len(lines)), "white")
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, im.width - 1, im.height - 1), outline="black", width=6)
    for i, line in enumerate(lines):
        draw.text((40, 60 + 70 * i), line, fill="black", font=font)
    return im


def chapter_cbz(n: int) -> bytes:
    """Return chapter n's CBZ: 3 strip pages, 5 noise panels of its own, then the credit and the localization credit."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for p in range(1, PAGES + 1):
            im = Image.new("RGB", (WIDTH, PAGE_H), "white")
            im.paste(noise(100 * n + 2 * p, (WIDTH - 100, 700)), (50, 150))
            if p < PAGES:
                im.paste(noise(100 * n + 2 * p + 1, (WIDTH - 100, 700)), (50, 1100))
            else:
                im.paste(text_box(CREDIT), (50, 1000))
                im.paste(text_box(LOCALIZATION), (50, 1500))
            out = io.BytesIO()
            im.save(out, "PNG")
            z.writestr(f"{p:03}.png", out.getvalue())
    return data.getvalue()


def scratch_config(machine: config.Config, folder: Path) -> Path:
    """Write the scratch config (config.yaml.example, edited) in folder; return its path."""
    folders = {name: folder / name for name in machine["paths"] if name != "kavita_path_map"}  # every path key
    values: dict[str, object] = {f"database.{k}": v for k, v in {**LOGIN, "name": DATABASE}.items()}
    values |= {f"paths.{name}": path.as_posix() for name, path in folders.items()}
    values |= {
        "logging.dir": (folder / "logs").as_posix(),
        "junk.claude_path": machine["junk"]["claude_path"],
        "junk.ocr_device": machine["junk"]["ocr_device"],
        "junk.ocr_workers": machine["junk"]["ocr_workers"],
    }
    tools = machine["tools"]
    values |= {f"tools.{name}": str(config.abs_path(tools[name])) for name in TOOLS}
    text = (config.ROOT / "config.yaml.example").read_bytes().decode("utf-8")
    for n, (key, value) in enumerate(values.items(), 1):
        k, parts, v = set_config.parse(n, f"{key}={json.dumps(value)}")
        text, _ = set_config.edit(text, k, parts, v)
    for path in folders.values():
        path.mkdir()
    path = folder / "config.yaml"
    path.write_bytes(text.encode("utf-8"))
    return path


def setup(cfg: config.Config) -> None:
    """Build the scratch database, own the share, store the fleet version and add the series' 3 chapters at junk."""
    downloads = config.abs_path(cfg["paths"]["downloads"]) / "mangas" / "Smoke (EN)" / SERIES
    downloads.mkdir(parents=True)
    with closing(db.connect(cfg)) as conn:
        state.adopt(cfg, conn)
        with db.transaction(conn):
            fleet.store(conn, cfg)
            sid = db.one_row(
                conn,
                """INSERT INTO series (name, origin, status, rules_key, admitted_at)
                VALUES (%s, 'new', 'ok', %s, %s) RETURNING id""",
                (SERIES, SERIES, db.now(conn)),
            )["id"]
            conn.execute(
                "INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, 'strip', 'computed')", (sid,)
            )
            for n in (1, 2, 3):
                cbz = downloads / f"Chapter {n}.cbz"
                cbz.write_bytes(chapter_cbz(n))
                conn.execute(
                    """INSERT INTO chapter (series_id, kind, identity, number, stage, status, download_path,
                        content_digest, discovered_at, updated_at)
                    VALUES (%s, 'download', %s, %s, 'junk', 'ready', %s, %s, %s, %s)""",
                    (
                        sid,
                        json.dumps(["smoke", None, None, str(n), ""]),
                        n,
                        config.to_stored(cfg, cbz),
                        f"smoke-{n}",
                        db.now(conn),
                        db.now(conn),
                    ),
                )


def judge(cfg: config.Config) -> tuple[list[str], bool, bool]:
    """Return the review's lines, whether it passed, and whether it failed on a verdict alone (a rerun may pass)."""
    with closing(db.connect(cfg)) as conn:
        waiting = conn.execute("SELECT stage, status, waiting_on FROM chapter ORDER BY id").fetchall()
        result = conn.execute("SELECT value FROM meta WHERE key = 'junk-review.last_result'").fetchone()
        rows = conn.execute("SELECT * FROM junk_candidate ORDER BY id").fetchall()
        decisions = store.by_series(conn)
        verdicts: dict[str, list[str]] = {"credit": [], "localization": [], "other": []}
        for row in rows:
            text = candidates.read_info(candidates.folder(cfg, row["folder"]))["text"].lower()
            which = "credit" if "discord" in text else "localization" if "translat" in text else "other"
            d = candidates.covering(decisions, row)
            verdicts[which].append(d["verdict"] if d is not None else row["verdict"] or "undecided")
    line = result["value"] if result is not None else "no review ran"
    counts = {name: int(n) for name, _, n in (p.partition(" ") for p in line.split(", ")) if name in ("reads", "shown")}
    lines = [
        f"chapters: {', '.join('/'.join(str(v) for v in c) for c in waiting)}",
        f"review: {line}",
        f"credit: {', '.join(verdicts['credit']) or 'no candidate'}",
        f"localization: {', '.join(verdicts['localization']) or 'no candidate'}",
        *([f"other candidates: {', '.join(verdicts['other'])}"] if verdicts["other"] else []),
    ]
    acted = (
        line.startswith("ok")
        and counts.get("reads", 0) >= MIN_READS
        and counts.get("shown", 0) >= MIN_SHOWN
        and bool(verdicts["credit"])
        and bool(verdicts["localization"])
        and not verdicts["other"]  # the fixture opens exactly the 2
    )
    right = set(verdicts["credit"]) == {"junk"} and set(verdicts["localization"]) == {"keep"}
    return lines, acted and right, acted and not right


def command(cfg_path: Path, args: list[str], token: str) -> None:
    """Run `python -m manga_pipeline.cli --config CFG ARGS`; given the token alone of the MANGA_PIPELINE_ variables.

    Its exit is not checked: a failed review (exit 1) is judged from what it recorded, as any other.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(f"{config.ENV_PREFIX}_")} | {TOKEN: token}
    subprocess.run(
        [sys.executable, "-m", "manga_pipeline.cli", "--config", str(cfg_path), *args],
        env=env,
        cwd=config.ROOT,
        check=False,
    )


@contextmanager
def scratch(machine: config.Config) -> Generator[Path]:
    """Yield a new scratch folder's config file; drop the scratch database and remove the folder at the end."""
    folder = Path(tempfile.mkdtemp(prefix="review-smoke-"))
    try:
        cfg_path = scratch_config(machine, folder)
        _drop(cfg_path)  # a killed run's
        try:
            yield cfg_path
        finally:
            _drop(cfg_path)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _drop(cfg_path: Path) -> None:
    cfg = config.load(cfg_path, env={})
    with closing(db.open_connection(cfg, db.MAINTENANCE_DATABASE)) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DATABASE)))


def attempt(machine: config.Config, run: Run) -> tuple[list[str], bool, bool]:
    """Run the smoke test once in a new scratch folder and database; return judge's answer."""
    with scratch(machine) as cfg_path:
        cfg = config.load(cfg_path, env={})
        setup(cfg)
        run(cfg_path, ["process"])
        run(cfg_path, ["junk", "review"])
        return judge(cfg)


def smoke(machine: config.Config, run: Run) -> tuple[list[str], bool]:
    """Run the smoke test, once more on a verdict miss; return its lines and whether it passed."""
    lines, passed, verdict_miss = attempt(machine, run)
    if verdict_miss:
        again, passed, _ = attempt(machine, run)
        lines = [*lines, "a verdict missed: run again", *again]
    return lines, passed


def main(argv: list[str] | None = None) -> int:
    """Run it with this machine's config; print its lines and whether it passed."""
    parser = argparse.ArgumentParser(description="Run 1 real junk review on fixture chapters, apart from the pipeline.")
    parser.add_argument("--config", help="this machine's config file (default: config.yaml)")
    args = parser.parse_args(argv)
    machine = config.load(args.config)
    token = machine["junk"]["claude_token"]
    if token is None or not token.strip():
        sys.stderr.write(f"error: junk.claude_token is not set (nor {TOKEN}): Claude cannot log in\n")
        return 2
    lines, passed = smoke(machine, functools.partial(command, token=token))
    sys.stdout.write("".join(f"{line}\n" for line in [*lines, "passed" if passed else "failed"]))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
