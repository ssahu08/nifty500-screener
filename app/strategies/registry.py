"""Strategy registry: discovers and loads all strategy modules."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from app.core.logging import get_logger
from app.strategies.base import Strategy

logger = get_logger(__name__)


def discover_strategies() -> list[type[Strategy]]:
    """Import all strategy modules and return their Strategy subclasses.

    Scans ``app/strategies/`` for modules with a ``strategy_class`` attribute.
    """
    discovered: list[type[Strategy]] = []
    package_dir = Path(__file__).parent

    for module_info in pkgutil.iter_modules([str(package_dir)]):
        module_name = f"app.strategies.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - a broken strategy must not crash
            logger.error("Failed to import strategy module %s: %s", module_name, exc)
            continue

        strategy_class = getattr(module, "strategy_class", None)
        if strategy_class is not None and issubclass(strategy_class, Strategy):
            discovered.append(strategy_class)

    return discovered


def get_registry() -> dict[str, Strategy]:
    """Return an instance of every registered strategy, keyed by name."""
    registry: dict[str, Strategy] = {}
    for cls in discover_strategies():
        try:
            instance = cls()
            registry[instance.metadata.name] = instance
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to instantiate strategy %s: %s", cls.__name__, exc)
    return registry
