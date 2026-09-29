"""Intuition memory provider for Hermes Agent (directory plugin).

Hermes discovers this directory under ``$HERMES_HOME/plugins/intuition`` and
loads the provider through its own registry (``register_memory_provider``). The
implementation lives in the intuition package: an installed copy is used when the
host interpreter already has one, and otherwise the source tree recorded by
``intuition install hermes`` is put on ``sys.path``.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _enable_source_tree() -> None:
    """Make the real package importable, however this plugin was loaded.

    The host loads the directory under its own synthetic name, and the plugin
    directory can also shadow the package name when it sits on sys.path. A path
    entry covers the first case; extending this module's ``__path__`` covers the
    second, because then this shim *is* the ``intuition`` package.
    """
    marker = Path(__file__).with_name("_intuition_src.txt")
    if not marker.exists():
        return
    root = marker.read_text().strip()
    if not root:
        return
    if root not in sys.path:
        sys.path.insert(0, root)
    package = Path(root) / "intuition"
    paths = globals().get("__path__")
    if paths is not None and package.is_dir() and str(package) not in paths:
        paths.insert(0, str(package))


try:
    from intuition.adapters.hermes import IntuitionProvider
except ImportError:
    _enable_source_tree()
    from intuition.adapters.hermes import IntuitionProvider


def register(ctx) -> None:
    """Memory-provider entry point for the host's discovery."""
    ctx.register_memory_provider(IntuitionProvider())


__all__ = ["IntuitionProvider", "register"]
