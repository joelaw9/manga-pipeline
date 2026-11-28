"""A fake `claude` for junk review tests.

Dumps its arguments, environment, prompt and the child config it was given, runs the canned junk commands of its plan,
and prints canned stream-json lines: 1 assistant message per call, then the result. Its plan is plan.json beside it:
{"dump": folder, "scratch": the run's paths.scratch, "actions": ["list" | "decide:<verdict>" | "spans:<candidate id>"
| "cut:<ranges, space-separated>" (propose, then record with the digest it prints) | "crash" | "call:<the call's
context>" | "read:<path>[,<path>...]" (1 message of Read tool uses, 1 line per content block, as stream-json prints
them) | "show" (`junk show` of each listed id, each a Bash tool use in its own message)], "exit": code, "reply": result
dict or null}.

As Claude Code does, a junk call whose text no `Bash(...)` rule of its --allowedTools allows is not run: it is
reported in the result's permission_denials. A rule allows the call text it equals, and, when it ends in ` *`, every
call text starting with its text before the ` *` and a space.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Junk:
    """Runs junk commands under the child config, as the review's prompt writes them, when the rules allow them."""

    def __init__(self, child_config, argv):
        """Keep the child config and the Bash rules of argv's --allowedTools."""
        self.prefix = [sys.executable, "-m", "manga_pipeline.cli", "--config", str(child_config), "junk"]
        allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--add-dir")] if "--allowedTools" in argv else []
        self.rules = [a[len("Bash(") : -1] for a in allowed if a.startswith("Bash(") and a.endswith(")")]
        self.denials = []

    def _allowed(self, text):
        return any(text == r or (r.endswith(" *") and text.startswith(r[:-1])) for r in self.rules)

    def text(self, *args):
        """Return the junk command `args` as the review's prompt writes it (as review_run.join_command)."""
        return shlex.join(p.replace("\\", "/") for p in [*self.prefix, *args])

    def __call__(self, *args):
        """Run the junk command `args`; return its (exit code, stdout), or (None, "") when no rule allows it."""
        text = self.text(*args)
        if not self._allowed(text):
            self.denials.append({"tool_name": "Bash", "tool_input": {"command": text}})
            return None, ""
        done = subprocess.run([*self.prefix, *args], capture_output=True, text=True, check=False)
        return done.returncode, done.stdout


def cut(junk, ranges):
    """Propose a cut of `ranges` and record it with the digest it prints; return the log entries."""
    code, out = junk("cut", *ranges)
    log = [["cut", code, out]]
    digest = next((ln.split()[1] for ln in out.splitlines() if ln.startswith("digest: ")), None)
    if digest is not None:
        log.append(["record", *junk("cut", *ranges, "--record", digest)])
    return log


def tool_uses(message_id, uses, context):
    """Print 1 assistant message of tool uses [(id, name, input)]: 1 line per content block, sharing its id and usage.

    Each line twice, as `call:` prints its message: a reader must count a tool use once, however often it is seen.
    """
    for use_id, name, args in uses:
        block = {"type": "tool_use", "id": use_id, "name": name, "input": args}
        usage = {"cache_read_input_tokens": context}
        line = json.dumps({"type": "assistant", "message": {"id": message_id, "usage": usage, "content": [block]}})
        print(line, flush=True)
        print(line, flush=True)


class Run:
    """The plan's actions in turn: the ids its last list returned, the log of junk commands, the messages printed.

    A tool use's message carries the context of the last `call:` (0 before one).
    """

    def __init__(self, junk):
        """Start with no list, no log and no message printed."""
        self.junk, self.listed, self.log, self.calls, self.context = junk, [], [], 0, 0

    def act(self, action):
        """Run 1 action of the plan (crash aside): a printed message, or junk commands (a list prints its own)."""
        if action.startswith(("call:", "read:")) or action == "show":
            self._message(action)
        else:
            self._command(action)

    def _message(self, action):
        if action.startswith("call:"):  # stream-json: the call's message, twice (2 content blocks), as it happens
            self.context = int(action.split(":")[1])
            usage = {"cache_read_input_tokens": self.context}
            line = json.dumps({"type": "assistant", "message": {"id": f"m{self.calls}", "usage": usage}})
            print(line, flush=True)
            print(line, flush=True)
            self.calls += 1
        elif action.startswith("read:"):
            paths = action.split(":", 1)[1].split(",")
            reads = [(f"t{self.calls}-{n}", "Read", {"file_path": p}) for n, p in enumerate(paths)]
            tool_uses(f"m{self.calls}", reads, self.context)
            self.calls += 1
        else:  # show
            for cid in self.listed:
                self.log.append(["show", *self.junk("show", cid)])
                show = [(f"t{self.calls}", "Bash", {"command": self.junk.text("show", cid)})]
                tool_uses(f"m{self.calls}", show, self.context)
                self.calls += 1

    def _command(self, action):
        junk = self.junk
        if action == "list":
            tool_uses(f"m{self.calls}", [(f"t{self.calls}", "Bash", {"command": junk.text("list")})], self.context)
            self.calls += 1
            code, out = junk("list")
            self.listed = [line.split("\t")[0] for line in out.splitlines() if line]
            self.log.append(["list", code, self.listed])
        elif action.startswith("decide:"):
            self.log.extend(["decide", *junk("decide", cid, action.split(":")[1])] for cid in self.listed)
        elif action.startswith("spans:"):
            self.log.append(["spans", *junk("cut", "--spans", action.split(":")[1])])
        elif action.startswith("cut:"):
            self.log.extend(cut(junk, action.split(":", 1)[1].split()))


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
    junk = Junk(child, sys.argv[1:])
    run = Run(junk)
    for action in plan["actions"]:
        if action == "crash":
            sys.exit(3)
        run.act(action)
    log = run.log
    (dump / f"log-{runs}.json").write_text(json.dumps(log), encoding="utf-8")
    if plan["reply"] is not None:
        reply = {**plan["reply"]}
        reply["permission_denials"] = [*reply.get("permission_denials", []), *junk.denials]
        print(json.dumps({"type": "result", **reply}))
    sys.exit(plan["exit"])


if __name__ == "__main__":
    main()
