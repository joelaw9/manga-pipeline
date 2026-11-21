# Deploy (LXC)

Units: `deploy/systemd/manga-pipeline-{intake,process,export,junk-review}.{service,timer}` (SPEC "Parts"). They run `/opt/manga-pipeline/.venv/bin/python` and read `/etc/manga-pipeline/pipeline.env` (mode 600, root: the 3 secret keys, never in a session or log; never `ANTHROPIC_API_KEY`). Single-quote a value that holds `$`, a space or `#`, so systemd and the shell read it the same.

## Install

Link the units (1 command per line in the LXC shell):

```bash
systemctl link /opt/manga-pipeline/deploy/systemd/manga-pipeline-{intake,process,export,junk-review}.{service,timer}
```

```bash
systemctl daemon-reload
```

Start the timers (only at the plan's start step):

```bash
systemctl enable --now manga-pipeline-{intake,process,export,junk-review}.timer
```

A command by hand runs from the checkout (the program is not packaged) with the env file loaded in a subshell:

```bash
( cd /opt/manga-pipeline && set -a && . /etc/manga-pipeline/pipeline.env && set +a && .venv/bin/python -m manga_pipeline.cli status )
```

## Deploy or change a shared setting

1. `systemctl stop` the 4 timers (if enabled); disable both Windows scheduled tasks.
2. Wait until `systemctl list-units "manga-pipeline-*.service" --state=activating,active` lists nothing (a running oneshot is `activating`) and no Windows `process` runs.
3. Pull and install on all 3 machines; `systemctl daemon-reload` on the LXC; edit the configs.
4. `state version --yes` on Celestia only.
5. Each machine's next `process` checks the version (exit 4 before any work if it differs).
6. Start the timers and enable the tasks (those that were enabled before).
