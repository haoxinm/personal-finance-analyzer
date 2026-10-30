from __future__ import annotations

from pathlib import Path

import pytest

from .support import seed_home


@pytest.fixture(autouse=True)
def _isolated_home(home: Path) -> None:
    """Every backup test runs with `SPEND_ANALYZER_HOME` redirected, so a call that forgets an
    explicit `home=` can never read or replace a real data directory."""
    del home


@pytest.fixture
def source(home: Path) -> tuple[Path, dict[str, str]]:
    """The configured data directory ("machine A"), fully seeded."""
    return home, seed_home(home)
