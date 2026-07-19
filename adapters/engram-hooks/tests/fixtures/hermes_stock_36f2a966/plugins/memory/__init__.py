"""Faithful discovery/loading extraction from pinned ``plugins.memory``.

Only CLI-command discovery and unrelated bundled-provider helpers are omitted.
The constructor-swallowing behavior under test is preserved verbatim in shape.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MEMORY_PLUGINS_DIR = Path(__file__).parent
_USER_NAMESPACE = "_hermes_user_memory"


def _register_synthetic_package(name: str, search_locations: list[str]) -> None:
    if name in sys.modules:
        return
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    spec.submodule_search_locations = search_locations
    sys.modules[name] = importlib.util.module_from_spec(spec)


def _get_user_plugins_dir() -> Path | None:
    try:
        from hermes_constants import get_hermes_home

        directory = get_hermes_home() / "plugins"
        return directory if directory.is_dir() else None
    except Exception:
        return None


def _is_memory_provider_dir(path: Path) -> bool:
    init_file = path / "__init__.py"
    if not init_file.exists():
        return False
    try:
        source = init_file.read_text(errors="replace")[:8192]
        return "register_memory_provider" in source or "MemoryProvider" in source
    except Exception:
        return False


def _iter_provider_dirs() -> list[tuple[str, Path]]:
    seen: set[str] = set()
    directories: list[tuple[str, Path]] = []
    if _MEMORY_PLUGINS_DIR.is_dir():
        for child in sorted(_MEMORY_PLUGINS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            if not (child / "__init__.py").exists():
                continue
            seen.add(child.name)
            directories.append((child.name, child))
    user_dir = _get_user_plugins_dir()
    if user_dir:
        for child in sorted(user_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            if child.name in seen or not _is_memory_provider_dir(child):
                continue
            directories.append((child.name, child))
    return directories


def find_provider_dir(name: str) -> Path | None:
    bundled = _MEMORY_PLUGINS_DIR / name
    if bundled.is_dir() and (bundled / "__init__.py").exists():
        return bundled
    user_dir = _get_user_plugins_dir()
    if user_dir:
        user = user_dir / name
        if user.is_dir() and _is_memory_provider_dir(user):
            return user
    return None


def discover_memory_providers() -> list[tuple[str, str, bool]]:
    results: list[tuple[str, str, bool]] = []
    for name, child in _iter_provider_dirs():
        description = ""
        yaml_file = child / "plugin.yaml"
        if yaml_file.exists():
            try:
                import yaml

                with yaml_file.open(encoding="utf-8-sig") as stream:
                    metadata = yaml.safe_load(stream) or {}
                description = metadata.get("description", "")
            except Exception:
                pass
        available = True
        try:
            provider = _load_provider_from_dir(child)
            available = provider.is_available() if provider else False
        except Exception:
            available = False
        results.append((name, description, available))
    return results


def load_memory_provider(name: str) -> Any | None:
    provider_dir = find_provider_dir(name)
    if not provider_dir:
        return None
    try:
        provider = _load_provider_from_dir(provider_dir)
        if provider:
            return provider
        logger.warning("Memory provider '%s' loaded but no provider instance found", name)
        return None
    except Exception as exc:
        logger.warning("Failed to load memory provider '%s': %s", name, exc)
        return None


def _load_provider_from_dir(provider_dir: Path) -> Any | None:
    name = provider_dir.name
    is_bundled = (
        _MEMORY_PLUGINS_DIR in provider_dir.parents
        or provider_dir.parent == _MEMORY_PLUGINS_DIR
    )
    module_name = f"plugins.memory.{name}" if is_bundled else f"{_USER_NAMESPACE}.{name}"
    init_file = provider_dir / "__init__.py"
    if not init_file.exists():
        return None

    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", None):
        module = cached
    else:
        if not is_bundled:
            _register_synthetic_package(_USER_NAMESPACE, [])
        spec = importlib.util.spec_from_file_location(
            module_name,
            str(init_file),
            submodule_search_locations=[str(provider_dir)],
        )
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.debug("Failed to exec_module %s: %s", module_name, exc)
            sys.modules.pop(module_name, None)
            return None

    if hasattr(module, "register"):
        collector = _ProviderCollector()
        try:
            module.register(collector)
            if collector.provider:
                return collector.provider
        except Exception as exc:
            logger.debug("register() failed for %s: %s", name, exc)

    from agent.memory_provider import MemoryProvider

    for attr_name in dir(module):
        attribute = getattr(module, attr_name, None)
        if (
            isinstance(attribute, type)
            and issubclass(attribute, MemoryProvider)
            and attribute is not MemoryProvider
        ):
            try:
                return attribute()
            except Exception:
                pass
    return None


class _ProviderCollector:
    def __init__(self) -> None:
        self.provider: Any | None = None

    def register_memory_provider(self, provider: Any) -> None:
        self.provider = provider

    def register_tool(self, *args: Any, **kwargs: Any) -> None:
        pass

    def register_hook(self, *args: Any, **kwargs: Any) -> None:
        pass

    def register_cli_command(self, *args: Any, **kwargs: Any) -> None:
        pass
