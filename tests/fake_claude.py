"""A fake `claude` for junk review tests.

Dumps its arguments, environment, prompt and the child config it was given, runs the canned junk commands of its plan,
and prints canned stream-json lines: 1 assistant message per call, then the result. Its plan is plan.json beside it:
{"dump": folder, "scratch": the run's paths.scratch, "actions": ["list" | "decide:<verdict>" | "crash" | "call:<the
call's context>"], "exit": code, "reply": result dict or null}.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def junk(child_config, *args):
    """Run the junk command `args` under the child config; return its (exit code, stdout)."""
    done = subprocess.run(
        [sys.executable, "-m", "manga_pipeline.cli", "--config", str(child_config), "junk", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout


def main():
    """Dump this run's arguments, environment, prompt and child config, run the plan's actions, reply and exit."""
    plan = json.loads((HERE / "plan.json").read_text(encoding="utf-8"))
    dump = Path(plan["dump"])
    dump.mkdir(parents=True, exist_ok=True)
    runs = len(list(dump.glob("argv-*.json")))
    (dump / f"argv-{runs}.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    (dump / f"env-{runs}.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    (dump / f"prompt-{runs}.txt").write_bytes(sys.stdin.buffer.read())  # the prompt arrives as UTF-8 bytes
    (child,) = Path(plan["scratch"], "junk-review").glob("*.yaml")
    (dump / f"config-{runs}.yaml").write_bytes(child.read_bytes())  # secret-free by design
    listed, log = [], []
    for action in plan["actions"]:
        if action == "crash":
            sys.exit(3)
        if action == "list":
            code, out = junk(child, "list")
            listed = [line.split("\t")[0] for line in out.splitlines() if line]
            log.append(["list", code, listed])
        elif action.startswith("decide:"):
            log.extend(["decide", *junk(child, "decide", cid, action.split(":")[1])] for cid in listed)
    (dump / f"log-{runs}.json").write_text(json.dumps(log), encoding="utf-8")
    calls = [int(a.split(":")[1]) for a in plan["actions"] if a.startswith("call:")]
    for n, call in enumerate(calls):  # stream-json: each call's message (twice: 2 content blocks)
        line = {"type": "assistant", "message": {"id": f"m{n}", "usage": {"cache_read_input_tokens": call}}}
        print(json.dumps(line))
        print(json.dumps(line))
    if plan["reply"] is not None:
        print(json.dumps({"type": "result", **plan["reply"]}))
    sys.exit(plan["exit"])


if __name__ == "__main__":
    main()
