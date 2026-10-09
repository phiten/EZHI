"""Import the integration's Home-Assistant-free modules under a synthetic package.

The real package's __init__.py pulls in homeassistant, which is not a test
dependency, so the older tests each load a single file by path with importlib.
That trick stops working as soon as a module has a relative import of its own
(`from .cloud import ...`), which the Bluetooth modules do.

Registering the component directory as the search path of a synthetic package
keeps those relative imports working while nothing above them is executed:

    from ezhi_component import ble_protocol

Nothing here imports Home Assistant, and nothing here touches a radio.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

PACKAGE = "ezhi_component"
COMPONENT_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "apsystems_ezhi_local"
)

if PACKAGE not in sys.modules:
    _spec = importlib.machinery.ModuleSpec(PACKAGE, None, is_package=True)
    _spec.submodule_search_locations = [str(COMPONENT_DIR)]
    sys.modules[PACKAGE] = importlib.util.module_from_spec(_spec)

# Where aiohttp is installed (it is, whenever Home Assistant is -- test_lc_ha.py
# and test_mqtt_connect.py need that), import the real one before any test
# module runs. Several older test modules put a bare stand-in into sys.modules
# "if aiohttp is not imported yet"; that is right on a bare Python, and wrong
# once something later in the same run needs the real package.
try:
    import aiohttp  # noqa: F401
except ImportError:
    pass
