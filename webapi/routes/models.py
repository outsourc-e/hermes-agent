import time
from typing import Optional

from fastapi import APIRouter, Query

from webapi.deps import get_runtime_model

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
    from hermes_cli.models import (
        curated_models_for_provider,
        list_available_providers,
    )

    effective_provider = provider
    if not effective_provider:
        from webapi.deps import get_runtime_agent_kwargs
        runtime = get_runtime_agent_kwargs()
        effective_provider = runtime.get("provider", "anthropic")

    models = curated_models_for_provider(effective_provider)
    providers = list_available_providers()

    return {
        "provider": effective_provider,
        "models": [{"id": m[0], "description": m[1]} for m in models],
        "providers": providers,
    }
