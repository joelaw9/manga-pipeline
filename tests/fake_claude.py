"""A fake `claude` for junk review tests: dumps its arguments, environment, prompt and the child config it was given,
runs the canned junk commands of its plan, and prints a canned JSON result. Its plan is plan.json beside it: {"dump":
folder, "scratch": the run's paths.scratch, "actions": ["list" | "decide:<verdict>" | "crash"], "exit": code, "reply":
result dict or null}.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def junk(child_config, *args):
    done = subprocess.run(
        [sys.executable, "-m", "manga_pipeline.cli", "--config", str(child_config), "junk", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout


def main():
    plan = json.loads((HERE / "plan.json").read_text(encoding="utf-8"))
    dump = Path(plan["dump"])
    dump.mkdir(parents=True, exist_ok=True)
    runs = len(list(dump.glob("argv-*.json")))
    (dump / f"argv-{runs}.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    (dump / f"env-{runs}.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    (dump / f"prompt-{runs}.txt").write_text(sys.stdin.read(), encoding="utf-8")
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
    if plan["reply"] is not None:
        print(json.dumps(plan["reply"]))
    sys.exit(plan["exit"])


if __name__ == "__main__":
    main()
