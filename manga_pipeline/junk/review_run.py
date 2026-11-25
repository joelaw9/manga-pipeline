"""`junk review` (SPEC "Junk decisions", review): starts `claude -p` when some open candidate can be listed.

It exits at once when none can.

Claude's environment is built from nothing: the OS variables a program needs, the Claude token from the config as
CLAUDE_CODE_OAUTH_TOKEN, and this review's id as review.REVIEW_ID_VARIABLE; no MANGA_PIPELINE_ variable and no API
key. Claude's `junk` commands read a copy of this run's resolved config with every secret set to null and every path
absolute, written to <paths.scratch>/junk-review/<id>.yaml for the review's length. Claude may read the candidates
folder and run the 3 junk commands, nothing else. When Claude has ended, each candidate this review listed counts 1
review.
"""

import copy
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePath
from typing import Literal, TypedDict

import yaml

from manga_pipeline import config, db, workflow
from manga_pipeline.junk import candidates, groups, review

log = logging.getLogger(__name__)

TIMEOUT_S = 15 * 60  # the Claude process is killed after this
PROMPT = Path(__file__).with_name("review_prompt.md")
OS_VARIABLES = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE")
WINDOWS_VARIABLES = ("USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT")
PATH_KEYS: tuple[tuple[Literal["logging"], Literal["dir"]], ...] = (("logging", "dir"),)


class ReviewError(RuntimeError):
    """Claude's run failed (non-zero exit, an error result, or the time limit)."""


class ClaudeReply(TypedDict, total=False):
    """The fields junk review reads of the result object `claude -p --output-format json` prints."""

    permission_denials: list[object]
    is_error: bool
    subtype: str
    num_turns: int


def child_config(cfg: config.Config) -> config.Config:
    """Return the resolved config with every secret null and every configured path absolute."""
    out = copy.deepcopy(cfg)
    for section, name in config.secret_keys():
        out[section][name] = None
    for section, name in PATH_KEYS:
        out[section][name] = str(config.abs_path(out[section][name]))
    for name, value in out["paths"].items():
        if isinstance(value, str):
            out["paths"][name] = str(config.abs_path(value))
    return out


def child_env(token: str | None, review_id: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return Claude's environment: the OS variables present, the project root as PYTHONPATH, the token, the review id.

    Nothing else.
    """
    if token is None:
        msg = "junk.claude_token is not set: Claude cannot log in"
        raise ReviewError(msg)
    environ = os.environ if environ is None else environ
    names = OS_VARIABLES + (WINDOWS_VARIABLES if os.name == "nt" else ())
    env = {n: environ[n] for n in names if n in environ}
    env["PYTHONPATH"] = str(config.ROOT)  # the junk commands run as python -m from the candidates folder
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env[review.REVIEW_ID_VARIABLE] = review_id
    return env


def join_command(parts: Iterable[str]) -> str:
    """Return a command line as Claude's Bash tool reads it.

    A POSIX shell on every platform (Git Bash on Windows, which drops backslashes), so paths go in forward slashes
    (Claude's Bash rules compare this text).
    """
    return shlex.join(p.replace("\\", "/") for p in parts)


def rule_path(path: str | PurePath) -> str:
    """Return an absolute path as a Claude Code permission rule writes it.

    `//` and its POSIX form, a Windows drive as its lower-case letter (F: -> //f/...). A single leading `/` would
    anchor to Claude's working directory.
    """
    p = path if isinstance(path, PurePath) else Path(path)
    if p.drive:
        return "//" + p.drive[0].lower() + p.as_posix()[len(p.drive) :]
    return "/" + p.as_posix()


def command(cfg: config.Config, prefix: str) -> list[str]:
    """Return the claude -p command line; ReviewError when junk.claude_path is no executable."""
    claude = shutil.which(cfg["junk"]["claude_path"])
    if claude is None:
        msg = f"junk.claude_path {cfg['junk']['claude_path']!r} is no executable"
        raise ReviewError(msg)
    candidates = str(config.abs_path(cfg["paths"]["candidates"]))
    return [
        claude,
        "-p",
        "--output-format",
        "json",
        "--permission-prompts",
        "none",
        "--tools",
        "Read,Bash",
        "--allowedTools",
        f"Read({rule_path(candidates)}/**)",
        f"Bash({prefix} list)",  # exactly: `list --all` is a person's
        f"Bash({prefix} show *)",
        f"Bash({prefix} decide *)",
        "--add-dir",
        candidates,
    ]


def _meta(conn: db.Connection, values: Iterable[tuple[str, str]]) -> None:
    with db.transaction(conn):
        db.executemany(
            conn,
            "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(f"junk-review.{k}", v) for k, v in values],
        )


def run(cfg: config.Config, conn: db.Connection) -> None:
    """Run 1 review. Raises ReviewError when Claude's run failed; the reviews are counted either way."""
    _meta(conn, [("last_start", workflow.now(conn))])
    with db.transaction(conn):
        conn.execute("UPDATE junk_candidate SET review_open = 0 WHERE review_open = 1")  # left by a killed review
    try:
        with workflow.share_errors():
            gone = candidates.cleanup(cfg, conn)
    except workflow.Unreachable as e:  # as a part: recorded, exit 0, the next timer run tries again
        log.warning("junk review: unreachable (%s), review ended", e)
        _meta(conn, [("last_end", workflow.now(conn)), ("last_result", f"unreachable: {e.service}")])
        return
    if gone:
        log.info("junk review: %d closed candidate(s) cleaned up", gone)
    folder = config.abs_path(cfg["paths"]["scratch"]) / "junk-review"
    shutil.rmtree(folder, ignore_errors=True)
    groups.remove_all(cfg)
    if not review.listable(conn, cfg):
        log.info("junk review: no candidate to review")
        end = workflow.now(conn)
        _meta(conn, [("last_end", end), ("last_result", "ok: nothing to review"), ("last_success", end)])
        return
    review_id = uuid.uuid4().hex
    result = "error: not run"
    try:
        folder.mkdir(parents=True)
        child = folder / f"{review_id}.yaml"
        child.write_bytes(yaml.safe_dump(child_config(cfg), sort_keys=False).encode("utf-8"))
        prefix = join_command([sys.executable, "-m", "manga_pipeline.cli", "--config", str(child), "junk"])
        try:
            result = _claude(cfg, prefix, review_id)
        except ReviewError as e:  # Claude could not start: recorded as the result
            result = f"error: {e}"
    finally:
        with db.transaction(conn):
            counted = conn.execute(
                """UPDATE junk_candidate SET reviews = reviews + 1, review_open = 0
                WHERE review_id = %s AND review_open = 1""",
                (review_id,),
            ).rowcount
        shutil.rmtree(folder, ignore_errors=True)
        groups.remove_all(cfg)
        end = workflow.now(conn)
        ok = result.startswith("ok")
        _meta(conn, [("last_end", end), ("last_result", result)] + ([("last_success", end)] if ok else []))
        log.info("junk review %s: %s; %d candidate(s) reviewed", review_id, result, counted)
    if not ok:
        raise ReviewError(result)


def _claude(cfg: config.Config, prefix: str, review_id: str) -> str:
    """Run Claude; returns "ok: ..." or "error: ..."."""
    prompt = PROMPT.read_text(encoding="utf-8").replace("{prefix}", prefix)
    env = child_env(cfg["junk"]["claude_token"], review_id)
    cwd = config.abs_path(cfg["paths"]["candidates"])
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(
            command(cfg, prefix),
            input=prompt.encode("utf-8"),
            capture_output=True,
            env=env,
            cwd=cwd,
            timeout=TIMEOUT_S,
            check=False,  # the exit is judged below
        )
    except subprocess.TimeoutExpired:
        return f"error: claude was killed after {TIMEOUT_S} s"
    reply = _reply(done.stdout)
    if isinstance(reply, dict) and (denials := reply.get("permission_denials")):
        log.warning("junk review %s: claude was refused %s", review_id, denials)
    if done.returncode != 0:
        return f"error: claude exited {done.returncode}"
    if not isinstance(reply, dict):
        return "error: claude printed no JSON result"
    if reply.get("is_error"):
        return f"error: claude reported an error ({reply.get('subtype')})"
    return f"ok: {reply.get('num_turns')} turn(s)"


def _reply(stdout: bytes) -> ClaudeReply | list[object] | str | float | bool | None:
    """Return what Claude printed, its stdout as JSON: any JSON value, an object read as its result; None for no JSON.

    Unchecked: an object is not checked to be a result object.
    """
    try:
        return json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
