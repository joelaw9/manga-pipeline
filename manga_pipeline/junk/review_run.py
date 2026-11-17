"""`junk review` (SPEC "Junk decisions", review): starts `claude -p` when some open candidate can be listed, and exits
at once when none can.

Claude's environment is built from nothing: the OS variables a program needs, the Claude token from the config as
CLAUDE_CODE_OAUTH_TOKEN, and this review's id as review.REVIEW_ID_VARIABLE; no MANGA_PIPELINE_ variable and no API
key. Claude's `junk` commands read a copy of this run's resolved config with every secret set to null and every path
absolute, written to <paths.scratch>/junk-review/<id>.yaml for the review's length. Claude may read the candidates
folder and run the 3 junk commands, nothing else. When Claude has ended, each candidate this review listed counts 1
review.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePath

import yaml

from manga_pipeline import config, db, workflow
from manga_pipeline.junk import candidates, review

log = logging.getLogger(__name__)

TIMEOUT_S = 15 * 60  # the Claude process is killed after this
PROMPT = Path(__file__).with_name("review_prompt.md")
OS_VARIABLES = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE")
WINDOWS_VARIABLES = ("USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT")
PATH_KEYS = (("logging", "dir"),)


class ReviewError(RuntimeError):
    """Claude's run failed (non-zero exit, an error result, or the time limit)."""


def child_config(cfg):
    """The resolved config with every secret null and every configured path absolute."""
    out = json.loads(json.dumps(cfg))
    for section, name in config.secret_keys():
        out[section][name] = None
    for section, name in PATH_KEYS:
        out[section][name] = str(config.abs_path(out[section][name]))
    for name, value in out["paths"].items():
        if isinstance(value, str):
            out["paths"][name] = str(config.abs_path(value))
    return out


def child_env(token, review_id, environ=None):
    """Claude's environment: the OS variables present, the project root as PYTHONPATH, the token, the review id;
    nothing else.
    """
    environ = os.environ if environ is None else environ
    names = OS_VARIABLES + (WINDOWS_VARIABLES if os.name == "nt" else ())
    env = {n: environ[n] for n in names if n in environ}
    env["PYTHONPATH"] = str(config.ROOT)  # the junk commands run as python -m from the candidates folder
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env[review.REVIEW_ID_VARIABLE] = review_id
    return env


def join_command(parts):
    """A command line as Claude's Bash tool reads it: a POSIX shell on every platform (Git Bash on Windows, which drops
    backslashes), so paths go in forward slashes (Claude's Bash rules compare this text).
    """
    return shlex.join(p.replace("\\", "/") for p in parts)


def rule_path(path):
    """An absolute path as a Claude Code permission rule writes it: `//` and its POSIX form, a Windows drive as its
    lower-case letter (F: -> //f/...). A single leading `/` would anchor to Claude's working directory.
    """
    p = path if isinstance(path, PurePath) else Path(path)
    if p.drive:
        return "//" + p.drive[0].lower() + p.as_posix()[len(p.drive) :]
    return "/" + p.as_posix()


def command(cfg, prefix):
    """The claude -p command line."""
    candidates = str(config.abs_path(cfg["paths"]["candidates"]))
    return [
        shutil.which(cfg["junk"]["claude_path"]),
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


def _meta(conn, values):
    with db.transaction(conn):
        db.executemany(
            conn,
            "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(f"junk-review.{k}", v) for k, v in values],
        )


def run(cfg, conn):
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
        result = _claude(cfg, prefix, review_id)
    finally:
        with db.transaction(conn):
            counted = conn.execute(
                """UPDATE junk_candidate SET reviews = reviews + 1, review_open = 0
                WHERE review_id = %s AND review_open = 1""",
                (review_id,),
            ).rowcount
        shutil.rmtree(folder, ignore_errors=True)
        end = workflow.now(conn)
        ok = result.startswith("ok")
        _meta(conn, [("last_end", end), ("last_result", result)] + ([("last_success", end)] if ok else []))
        log.info("junk review %s: %s; %d candidate(s) reviewed", review_id, result, counted)
    if not ok:
        raise ReviewError(result)


def _claude(cfg, prefix, review_id):
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
    try:
        reply = json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        reply = None
    if isinstance(reply, dict) and reply.get("permission_denials"):
        log.warning("junk review %s: claude was refused %s", review_id, reply["permission_denials"])
    if done.returncode != 0:
        return f"error: claude exited {done.returncode}"
    if not isinstance(reply, dict):
        return "error: claude printed no JSON result"
    if reply.get("is_error"):
        return f"error: claude reported an error ({reply.get('subtype')})"
    return f"ok: {reply.get('num_turns')} turn(s)"
