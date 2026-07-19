"""Faithful provider-list/status extraction from pinned Hermes CLI."""
from __future__ import annotations

import os
from typing import Any


def _get_available_providers() -> list[tuple[str, str, Any]]:
    try:
        from plugins.memory import discover_memory_providers, load_memory_provider

        raw = discover_memory_providers()
    except Exception:
        raw = []

    results = []
    for name, _description, _available in raw:
        try:
            provider = load_memory_provider(name)
            if not provider:
                continue
        except Exception:
            continue

        schema = provider.get_config_schema() if hasattr(provider, "get_config_schema") else []
        has_secrets = any(field.get("secret") for field in schema)
        has_non_secrets = any(not field.get("secret") for field in schema)
        if has_secrets and has_non_secrets:
            setup_hint = "API key / local"
        elif has_secrets:
            setup_hint = "requires API key"
        elif not schema:
            setup_hint = "no setup needed"
        else:
            setup_hint = "local"
        results.append((name, setup_hint, provider))
    return results


def cmd_status(args: Any) -> None:
    del args
    from hermes_cli.config import load_config

    config = load_config()
    mem_config = config.get("memory", {})
    provider_name = mem_config.get("provider", "")

    print("\nMemory status\n" + "─" * 40)
    print("  Built-in:  always active")
    print(f"  Provider:  {provider_name or '(none — built-in only)'}")

    providers = _get_available_providers()
    provider = None
    for provider_candidate_name, _, candidate in providers:
        if provider_candidate_name == provider_name:
            provider = candidate
            break

    if provider_name:
        if provider:
            print("\n  Plugin:    installed ✓")
            if provider.is_available():
                print("  Status:    available ✓")
            else:
                print("  Status:    not available ✗")
                schema = (
                    provider.get_config_schema()
                    if hasattr(provider, "get_config_schema")
                    else []
                )
                required_fields = [field for field in schema if field.get("env_var")]
                if required_fields:
                    print("  Missing:")
                    for field in required_fields:
                        env_var = field.get("env_var", "")
                        mark = "✓" if os.environ.get(env_var) else "✗"
                        print(f"    {mark} {env_var}")
        else:
            print("\n  Plugin:    NOT installed ✗")
            print(f"  Install the '{provider_name}' memory plugin to ~/.hermes/plugins/")

    if providers:
        print("\n  Installed plugins:")
        for installed_name, description, _ in providers:
            active = " ← active" if installed_name == provider_name else ""
            print(f"    • {installed_name}  ({description}){active}")
