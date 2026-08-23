"""A stand-in for Decky Loader's ``decky`` module so ``main.py`` can be imported in tests."""
import logging
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install(root, plugin_dir=REPO_ROOT):
    """Put a fake ``decky`` into ``sys.modules`` (dirs under ``root``) and return it; emits are collected in
    ``decky.emitted`` as ``(event, payload)``. Call before ``import main``; call again to re-point the dirs."""
    stub = sys.modules.get("decky")
    if stub is None or not getattr(stub, "_is_stub", False):
        stub = types.ModuleType("decky")
        stub._is_stub = True
        sys.modules["decky"] = stub
    stub.logger = logging.getLogger("decky-controller-test")
    stub.emitted = []

    async def emit(event, *args):
        stub.emitted.append((event, args[0] if args else None))

    stub.emit = emit
    stub.DECKY_PLUGIN_DIR = plugin_dir
    stub.DECKY_PLUGIN_SETTINGS_DIR = os.path.join(root, "settings")
    stub.DECKY_PLUGIN_RUNTIME_DIR = os.path.join(root, "runtime")
    stub.DECKY_PLUGIN_LOG_DIR = os.path.join(root, "logs")
    stub.DECKY_PLUGIN_VERSION = None
    stub.DECKY_VERSION = "test"
    return stub
