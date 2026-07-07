"""Root conftest — ensure scripts/runpod/ (regular package) is the
``runpod`` module used by tests, not the root-level runpod/ namespace
package.

pytest with --import-mode=importlib resolves the test module's package
chain from the file's location, so runpod/tests/test_runpod_lifecycle.py
causes ``runpod`` to be imported as a namespace package from the root
``runpod/`` directory.  We force-import the real package from
``scripts/runpod/`` and inject it into sys.modules before collection.
"""
import importlib.util
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
_scripts = str(_root / "scripts")
if _scripts in sys.path:
    sys.path.remove(_scripts)
sys.path.insert(0, _scripts)

# Force the regular package scripts/runpod/ to be the ``runpod`` module.
for key in list(sys.modules):
    if key == "runpod" or key.startswith("runpod."):
        del sys.modules[key]

spec = importlib.util.spec_from_file_location(
    "runpod",
    str(_root / "scripts" / "runpod" / "__init__.py"),
    submodule_search_locations=[str(_root / "scripts" / "runpod")],
)
mod = importlib.util.module_from_spec(spec)
sys.modules["runpod"] = mod
spec.loader.exec_module(mod)
