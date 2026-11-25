# Deploy (LXC and Windows)

Units: `deploy/systemd/manga-pipeline-{intake,process,export,junk-review,watch}.{service,timer}` (SPEC "Parts"; `watch`: SPEC "Needs human"). They run `/opt/manga-pipeline/.venv/bin/python` and read `/etc/manga-pipeline/pipeline.env` (mode 600, root: the 3 secret keys, never in a session or log; never `ANTHROPIC_API_KEY`). Single-quote a value that holds `$`, a space or `#`, so systemd and the shell read it the same.

## Install

Link the units (1 command per line in the LXC shell):

```bash
systemctl link /opt/manga-pipeline/deploy/systemd/manga-pipeline-{intake,process,export,junk-review,watch}.{service,timer}
```

```bash
systemctl daemon-reload
```

Start the timers (only at the plan's start step):

```bash
systemctl enable --now manga-pipeline-{intake,process,export,junk-review,watch}.timer
```

Every unit starts its command with `--scheduled`: while the automation is paused (`state pause`) the run ends at once, doing nothing (SPEC "Parts"). A command run by hand never passes it, so it works during a pause.

On Celestia and Luna, the scheduled task `manga-pipeline process` runs every 10 minutes: the machine's Python with the arguments `-m manga_pipeline.cli --scheduled process`, the checkout as working directory, whether logged on or not, below-normal priority, `IgnoreNew`, a 4-hour limit.

A command by hand runs from the checkout (the program is not packaged) with the env file loaded in a subshell:

```bash
( cd /opt/manga-pipeline && set -a && . /etc/manga-pipeline/pipeline.env && set +a && .venv/bin/python -m manga_pipeline.cli status )
```

## Deploy or change a shared setting

1. `state pause` on any machine (by hand, as above, `state pause` in place of `status`): it pauses every timer's and task's run and returns once none is still running, printing how many it waits for each minute. Manual commands keep working. Timers and tasks stay enabled.
2. Pull and install on all 3 machines; `systemctl daemon-reload` on the LXC; edit the configs right after the pull, with `python -m scripts.set_config KEY=VALUE ...` from the checkout (`python -m scripts.set_config export.rewrites_open=0`; `.venv/bin/python` on the LXC): a scheduled run started between the 2 fails its config check (exit 2) and does nothing. A run started mid-pull may fail on import (exit 1), touching nothing. On Windows, a `pip install` that fails on a file in use is run again.
3. `state version --yes` on Celestia only.
4. `state resume`. Timers and tasks stopped on purpose before the deploy (junk review, say) stay as they were.
5. Each machine's next `process` checks the version (exit 4 before any work if it differs).
6. Run `watch` by hand: it must exit 0 (a config or schema every command refuses is reported by nothing else); `status` shows no pause.

## Back up and restore the junk knowledge

`junk backup DIR` (no lock, any time) writes the decisions with their history, the acts, the sightings and the
watermarks into a new `junk-<time>` folder in DIR. A new database regains them with `junk restore DIR/junk-<time>`,
run first, before any part (a part's baseline fills the decisions, and restore needs every junk table empty), by the
program that wrote the backup or a later one (each table must have the backup's columns; a later program's steps that rewrite junk rows do not reach the restored rows):

```bash
( cd /opt/manga-pipeline && set -a && . /etc/manga-pipeline/pipeline.env && set +a && .venv/bin/python -m manga_pipeline.cli junk restore /backups/junk-20261125-120000 )
```

## Front-load adopted files' conversions (backfill only)

The drop-in `deploy/systemd/manga-pipeline-intake.service.d/queue-first.conf` runs `scripts/queue_first.py --scheduled` before each `intake` run (nothing while the automation is paused; `state pause` does not wait for a run already past its pause check, which may still queue conversions for a few seconds): it queues the conversions of the "Reading" collection's series, then of the series with an open download (SPEC "State"). Its failure is in the journal and never stops `intake` (`ExecStartPre=-`). `systemctl link` takes unit files only, so the drop-in is linked by hand.

Install, at the backfill's start:

```bash
mkdir -p /etc/systemd/system/manga-pipeline-intake.service.d
```

```bash
ln -s /opt/manga-pipeline/deploy/systemd/manga-pipeline-intake.service.d/queue-first.conf /etc/systemd/system/manga-pipeline-intake.service.d/queue-first.conf
```

```bash
systemctl daemon-reload
```

Remove, once every file of those series is queued, or when the backfill ends:

```bash
rm /etc/systemd/system/manga-pipeline-intake.service.d/queue-first.conf
```

```bash
systemctl daemon-reload
```

Run it once by hand (as a command by hand above):

```bash
( cd /opt/manga-pipeline && set -a && . /etc/manga-pipeline/pipeline.env && set +a && .venv/bin/python -m scripts.queue_first )
```
