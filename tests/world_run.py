"""Run the CLI in its own process against a dumped test world: python -m tests.world_run WORLD_JSON CLI_ARGS...

The clients' from_config return the world's fakes, rebuilt from the file (tests/world.py fakes_from), so a part run
as a real process reaches no network.
"""

import sys
from pathlib import Path

from manga_pipeline import cli
from manga_pipeline.clients import kavita, suwayomi

from tests.world import fakes_from

if __name__ == "__main__":
    fake_suwayomi, fake_kavita = fakes_from(Path(sys.argv[1]))
    suwayomi.from_config = lambda cfg: fake_suwayomi
    kavita.from_config = lambda cfg: fake_kavita
    sys.exit(cli.main(sys.argv[2:]))
