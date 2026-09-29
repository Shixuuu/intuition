"""Hermes directory-plugin shim ($HERMES_HOME/plugins/intuition/).

`intuition install hermes` copies this folder and writes _intuition_pkg.txt
holding the absolute path of the installed `intuition` package. This file
extends the package __path__ so `intuition.adapters.*` resolves to the real
package even though the plugin dir itself shadows the package name.
"""

import os as _os

_PKG = _os.path.join(_os.path.dirname(__file__), "_intuition_pkg.txt")
if _os.path.exists(_PKG):
    _real = open(_PKG).read().strip()
    if _real and _real not in __path__:
        __path__.insert(0, _real)

from intuition.adapters.hermes import (  # noqa: F401,E402
    IntuitionProvider,
    register,
)
