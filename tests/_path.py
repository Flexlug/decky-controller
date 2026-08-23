"""Test bootstrap: make ``py_modules`` importable and keep the daemon/backend loggers quiet.

``import _path`` first in every test. The root ``NullHandler`` turns the ``decky`` shim's
``logging.basicConfig`` into a no-op and suppresses ``lastResort`` output; ``assertLogs`` still works.
"""
import logging
import os
import sys

PY_MODULES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_modules")
if PY_MODULES not in sys.path:
    sys.path.insert(0, PY_MODULES)

logging.getLogger().addHandler(logging.NullHandler())
