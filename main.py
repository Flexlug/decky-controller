"""Decky Loader entry point: wires ``decky`` to the backend service in ``py_modules/controller_backend``."""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Optional

import decky  # type: ignore[import-not-found]

PLUGIN_NAME = "Decky Controller"
PY_MODULES_DIR = os.path.join(decky.DECKY_PLUGIN_DIR, "py_modules")
if PY_MODULES_DIR not in sys.path:
    sys.path.insert(0, PY_MODULES_DIR)

from controller_backend.service import Service  # noqa: E402

JsonDict = dict[str, Any]


def _plugin_version() -> str:
    declared = getattr(decky, "DECKY_PLUGIN_VERSION", None)
    if isinstance(declared, str) and declared:
        return declared
    try:
        with open(os.path.join(decky.DECKY_PLUGIN_DIR, "package.json"), encoding="utf-8") as package_json:
            version = json.load(package_json).get("version")
        return version if isinstance(version, str) and version else "0.0.0"
    except (OSError, ValueError) as exc:
        decky.logger.warning("cannot read package.json version: %s", exc)
        return "0.0.0"


def _route_package_logs_to_decky() -> None:
    """The backend and deckhw log through their own loggers; send them wherever Decky's logger goes."""
    handlers = list(decky.logger.handlers)
    for name in ("controller_backend", "deckhw"):
        logger = logging.getLogger(name)
        logger.setLevel(decky.logger.getEffectiveLevel())
        if handlers:
            logger.propagate = False
            for handler in handlers:
                if handler not in logger.handlers:
                    logger.addHandler(handler)


_SERVICE: Optional[Service] = None


def _service() -> Service:
    global _SERVICE
    if _SERVICE is None:
        _route_package_logs_to_decky()
        _SERVICE = Service(
            emit=decky.emit, logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR, settings_dir=decky.DECKY_PLUGIN_SETTINGS_DIR,
            runtime_dir=decky.DECKY_PLUGIN_RUNTIME_DIR, log_dir=decky.DECKY_PLUGIN_LOG_DIR,
            plugin_version=_plugin_version(), decky_version=getattr(decky, "DECKY_VERSION", None),
        )
    return _SERVICE


def _error(error: BaseException, **extra: Any) -> JsonDict:
    """Uniform error answer for callables; expected (validation / missing file) errors log without a traceback."""
    if isinstance(error, (ValueError, FileNotFoundError, TimeoutError)):
        decky.logger.error("callable failed: %s: %s", type(error).__name__, error)
    else:
        decky.logger.exception("callable failed: %s", error)
    return {"ok": False, "error": str(error) or type(error).__name__, **extra}


class Plugin:
    """Decky may call these on an instance or with the class itself as ``self`` (older loaders), so no state
    lives on ``self``; every callable answers ``{"ok": false, "error": …}`` instead of raising."""

    async def get_status(self) -> JsonDict:
        try:
            return await _service().build_status()
        except Exception as error:
            return _error(error)

    async def start(self, profile: Optional[str] = None) -> JsonDict:
        try:
            if profile is not None and not isinstance(profile, str):
                raise ValueError(f"profile must be a string, got {type(profile).__name__}")
            return await _service().start(profile)
        except Exception as error:
            return _error(error)

    async def stop(self) -> JsonDict:
        try:
            return await _service().stop("user")
        except Exception as error:
            return _error(error)

    async def get_settings(self) -> JsonDict:
        try:
            return {"ok": True, **_service().settings.load()}
        except Exception as error:
            return _error(error)

    async def set_settings(self, settings: Optional[JsonDict] = None) -> JsonDict:
        try:
            merged, warnings = _service().settings.update(settings if settings is not None else {})
            for warning in warnings:
                decky.logger.warning("set_settings: %s (ignored)", warning)
            result: JsonDict = {"ok": True, **merged}
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as error:
            return _error(error)

    async def get_diagnostics(self) -> JsonDict:
        try:
            return await _service().diagnostics()
        except Exception as error:
            return _error(error)

    async def _main(self) -> None:
        service = _service()
        decky.logger.info("%s %s backend starting (python %s, plugin dir %s)",
                          PLUGIN_NAME, service.plugin_version, sys.version.split()[0], service.plugin_dir)
        try:
            await service.startup()
        except Exception:
            decky.logger.exception("backend startup failed")

    async def _unload(self) -> None:
        decky.logger.info("%s: unloading — stopping daemon and rolling back", PLUGIN_NAME)
        try:
            await _service().shutdown("unload")
        except Exception:
            decky.logger.exception("unload failed")

    async def _uninstall(self) -> None:
        decky.logger.info("%s: uninstalling — stopping daemon and rolling back", PLUGIN_NAME)
        try:
            await _service().shutdown("uninstall")
        except Exception:
            decky.logger.exception("uninstall cleanup failed")

    async def _migration(self) -> None:
        decky.logger.info("%s: no migration needed", PLUGIN_NAME)   # settings always lived in the settings dir
