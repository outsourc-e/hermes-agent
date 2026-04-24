import json
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query

from webapi.deps import get_runtime_model


def _get_custom_provider_entry(provider_name: Optional[str]) -> Optional[dict]:
    if not provider_name:
        return None
    try:
        from hermes_cli.config import get_compatible_custom_providers, load_config

        for entry in get_compatible_custom_providers(load_config()):
            name = str(entry.get("name") or "").strip().lower()
            if name == provider_name.strip().lower():
                return entry
    except Exception:
        return None
    return None


def _custom_provider_models(provider_name: str) -> list[tuple[str, str]]:
    entry = _get_custom_provider_entry(provider_name)
    if not entry:
        return []

    configured_models = entry.get("models")
    if isinstance(configured_models, list):
        resolved: list[tuple[str, str]] = []
        for model in configured_models:
            if isinstance(model, str) and model.strip():
                resolved.append((model.strip(), ""))
            elif isinstance(model, dict):
                model_id = str(model.get("id") or model.get("model") or "").strip()
                if model_id:
                    resolved.append((model_id, str(model.get("description") or "").strip()))
        if resolved:
            return resolved

    api_mode = str(entry.get("api_mode") or entry.get("transport") or "").strip().lower()
    base_url = str(entry.get("base_url") or "").strip().lower()
    if api_mode == "anthropic_messages" or "anthropic" in base_url or "18801" in base_url:
        from hermes_cli.models import curated_models_for_provider

        return curated_models_for_provider("anthropic")
    return []


def _openclaw_provider_ids() -> Optional[set[str]]:
    candidate_paths = [
        Path("/Users/aurora/.openclaw/openclaw.json"),
        Path.home() / ".openclaw" / "openclaw.json",
    ]
    for path in candidate_paths:
        try:
            if not path.exists():
                continue
            payload = json.loads(path.read_text())
            providers = payload.get("models", {}).get("providers", {})
            if isinstance(providers, dict) and providers:
                return {str(key).strip().lower() for key in providers.keys() if str(key).strip()}
        except Exception:
            continue
    return {
        "anthropic",
        "anthropic-oauth",
        "claude-bridge",
        "claude-cli",
        "google-antigravity",
        "lmstudio-pc1",
        "lmstudio-pc1-nemotron",
        "minimax",
        "ollama-pc1",
        "ollama-pc2",
        "openai",
        "openai-codex",
        "openrouter",
    }


def _available_providers_with_custom() -> list[dict[str, str]]:
    from hermes_cli.models import list_available_providers

    providers = list_available_providers()
    seen = {str(item.get("id") or "").strip().lower() for item in providers}
    try:
        from hermes_cli.config import get_compatible_custom_providers, load_config

        for entry in get_compatible_custom_providers(load_config()):
            name = str(entry.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            providers.append(
                {
                    "id": name,
                    "label": name,
                    "aliases": [],
                    "authenticated": True,
                }
            )
            seen.add(name.lower())
    except Exception:
        pass

    allowed = _openclaw_provider_ids()
    if allowed:
        providers = [item for item in providers if str(item.get("id") or "").strip().lower() in allowed]
    return providers

router = APIRouter()


@router.get("/v1/models")
async def list_models() -> dict:
    runtime_model = get_runtime_model()
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": "hermes-agent",
                "object": "model",
                "created": now,
                "owned_by": "hermes",
                "permission": [],
                "root": "hermes-agent",
                "parent": None,
                "runtime_model": runtime_model,
            },
            {
                "id": runtime_model,
                "object": "model",
                "created": now,
                "owned_by": "runtime",
                "permission": [],
                "root": runtime_model,
                "parent": "hermes-agent",
            },
        ],
    }


@router.get("/api/available-models")
async def available_models(provider: Optional[str] = Query(None)) -> dict:
    """Return available models for a provider.

    Uses the same resolution as `hermes setup`: live API query first,
    then static catalog fallback.  If provider is omitted, uses the
    currently configured provider.
    """
    from hermes_cli.models import curated_models_for_provider

    effective_provider = provider
    if not effective_provider:
        from webapi.deps import get_runtime_agent_kwargs
        runtime = get_runtime_agent_kwargs()
        effective_provider = runtime.get("provider", "anthropic")

    models = _custom_provider_models(effective_provider)
    if not models:
        models = curated_models_for_provider(effective_provider)
    providers = _available_providers_with_custom()

    return {
        "provider": effective_provider,
        "models": [{"id": m[0], "description": m[1]} for m in models],
        "providers": providers,
    }
