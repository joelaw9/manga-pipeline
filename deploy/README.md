# Deploy (LXC)

Units: `deploy/systemd/manga-pipeline-{intake,process,export,junk-review}.{service,timer}` (SPEC "Parts"). They run `/opt/manga-pipeline/.venv/bin/python` and read `/etc/manga-pipeline/pipeline.env` (mode 600, root: the 3 secret keys, never in a session or log).

## Install

```bash
for u in intake process export junk-review; do systemctl link /opt/manga-pipeline/deploy/systemd/manga-pipeline-$u.service /opt/manga-pipeline/deploy/systemd/manga-pipeline-$u.timer; done
systemctl daemon-reload
systemctl enable --now manga-pipeline-{intake,process,export,junk-review}.timer
```

A command by hand loads the env file in a subshell:

```bash
( set -a; . /etc/manga-pipeline/pipeline.env; set +a; .venv/bin/python -m manga_pipeline.cli status )
```

## Deploy or change a shared setting

1. `systemctl stop` the 4 timers (if enabled); disable both Windows scheduled tasks.
2. Wait until `systemctl list-units "manga-pipeline-*.service" --state=activating,active` lists nothing (a running oneshot is `activating`) and no Windows `process` runs.
3. Pull and install on all 3 machines; `systemctl daemon-reload` on the LXC; edit the configs.
4. `state version --yes` on Celestia only.
5. Each machine's next `process` checks the version (exit 4 before any work if it differs).
6. Start the timers and enable the tasks (those that were enabled before).
