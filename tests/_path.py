"""Make ``py_modules`` importable for the test-suite (``import _path`` first in every test)."""
import os
import sys

PY_MODULES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_modules")
if PY_MODULES not in sys.path:
    sys.path.insert(0, PY_MODULES)
