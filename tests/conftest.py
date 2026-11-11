from __future__ import annotations

import pytest

from watch_with_me import models


@pytest.fixture(autouse=True)
def _hermetic_host_config(monkeypatch):
    """Never read the real ~/.hermes/config.yaml during tests.

    The watcher and the dashboard API merge the LIVE host config on every
    poll; tests that exercise that merge patch ``models.load_hermes_config``
    themselves, everything else gets an empty host config so behaviour stays
    hermetic regardless of the machine the suite runs on.
    """
    monkeypatch.setattr(models, "load_hermes_config", lambda: {})
