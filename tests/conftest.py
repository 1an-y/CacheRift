"""Wiring for this repo's own test suite."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

_PLUGIN = "cacherift.check.pytest_plugin"


def pytest_configure(config: pytest.Config) -> None:
    # An up-to-date install already loads the plugin through its pytest11 entry point, under the
    # name "cacherift". Registering the same module again is an error in pytest, so only import it
    # when nothing did: that keeps the suite runnable on a checkout whose install metadata is stale.
    plugins: Iterable[Any] = config.pluginmanager.get_plugins()
    if not any(getattr(plugin, "__name__", None) == _PLUGIN for plugin in plugins):
        config.pluginmanager.import_plugin(_PLUGIN)
