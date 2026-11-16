"""Run the CLI in its own process against a dumped test world: python -m tests.world_run WORLD_JSON CLI_ARGS...

The clients' from_config return the world's fakes, rebuilt from the file (tests/world.py fakes_from), the metadata
sources are empty fakes, and the image tools and scorer are fakes (tests/fakes/convert.py), so a part run as a real
process reaches no network and no GPU.
"""

import sys
from pathlib import Path

from manga_pipeline import cli
from manga_pipeline.clients import kavita, suwayomi
from manga_pipeline.metadata import sources as metadata_sources

from tests.fakes import convert as fake_convert
from tests.fakes.sources import fake_sources
from tests.world import fakes_from

if __name__ == "__main__":
    fake_suwayomi, fake_kavita = fakes_from(Path(sys.argv[1]))
    suwayomi.from_config = lambda _cfg: fake_suwayomi
    kavita.from_config = lambda _cfg: fake_kavita
    fakes = fake_sources()
    metadata_sources.new_sources = lambda: fakes
    fake_convert.install(setattr)
    sys.exit(cli.main(sys.argv[2:]))
