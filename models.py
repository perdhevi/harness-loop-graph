"""Chapter H — a model per role.

config.json picks one provider and model for everything. `roles` can give any
role its own model (and provider, and settings such as num_ctx or think):

    "roles": {
      "planner": {"model": "qwen3:8b"},
      "reviser": {"model": "qwen3:8b"},
      "judge":   {"model": "mistral:7b"},
      "task":    {"model": "gemma4:e4b", "num_ctx": 16384},
      "compactor": {}
    }

A missing or empty role uses the default provider and model. Keys other than
`provider` override that provider's settings for this role only. Roles that
end up with identical settings share one adapter. model_adapter.py is untouched.
"""

from __future__ import annotations

import json

from model_adapter import ADAPTERS, ModelError

ROLES = ("planner", "task", "judge", "reviser", "compactor", "reviewer")   # reviewer: Chapter J


def role_settings(config: dict, role: str) -> tuple[str, dict]:
    """(provider, provider settings) for one role, after its overrides."""
    if role not in ROLES:
        raise ModelError(f"unknown role '{role}'; roles are: {', '.join(ROLES)}")
    override = dict((config.get("roles") or {}).get(role) or {})
    provider = override.pop("provider", config["provider"])
    if provider not in config.get("providers", {}):
        raise ModelError(f"role '{role}': unknown provider '{provider}'. Options: {list(config['providers'])}")
    return provider, {**config["providers"][provider], **override}


def make_role_models(config: dict, override=None) -> dict[str, object]:
    """One model object per role. `override` (tests, or a single model for everything) wins."""
    if override is not None:
        return {role: override for role in ROLES}
    cache: dict[str, object] = {}
    out = {}
    for role in ROLES:
        provider, settings = role_settings(config, role)
        key = json.dumps([provider, settings], sort_keys=True)
        if key not in cache:
            if provider not in ADAPTERS:
                raise ModelError(f"Unknown provider '{provider}'. Options: {list(ADAPTERS)}")
            cache[key] = ADAPTERS[provider](settings)
        out[role] = cache[key]
    return out


def describe_roles(config: dict) -> dict[str, str]:
    """{role: "provider/model"}, for run metadata and messages."""
    out = {}
    for role in ROLES:
        provider, settings = role_settings(config, role)
        out[role] = f"{provider}/{settings.get('model')}"
    return out


def ollama_models(config: dict) -> dict[str, dict]:
    """Every distinct Ollama model the roles use → its merged settings (for doctor and the num_ctx check)."""
    out: dict[str, dict] = {}
    for role in ROLES:
        provider, settings = role_settings(config, role)
        if provider == "ollama":
            out.setdefault(settings["model"], {**settings, "roles": []})["roles"].append(role)
    return out
