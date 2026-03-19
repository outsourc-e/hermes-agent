from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.config import load_config, save_config
from webapi.deps import get_config, get_runtime_agent_kwargs, get_runtime_model
from webapi.models.config import ConfigResponse


router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigPatch(BaseModel):
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None


@router.get("", response_model=ConfigResponse)
async def get_web_config() -> ConfigResponse:
    runtime = get_runtime_agent_kwargs()
    return ConfigResponse(
        model=get_runtime_model(),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        base_url=runtime.get("base_url"),
        config=get_config(),
    )


@router.patch("")
async def patch_web_config(patch: ConfigPatch) -> dict[str, Any]:
    """Patch ~/.hermes/config.yaml with the provided fields.
    Only model, provider, and base_url are exposed — everything else
    (toolsets, memory, display, etc.) is left untouched.
    """
    try:
        config = load_config()

        if patch.model is not None:
            config["model"] = patch.model
        if patch.provider is not None:
            config["provider"] = patch.provider
        if patch.base_url is not None:
            if patch.base_url.strip():
                config["base_url"] = patch.base_url.strip()
            else:
                config.pop("base_url", None)  # empty string = remove it

        save_config(config)
        return {"ok": True, "model": config.get("model"), "provider": config.get("provider"), "base_url": config.get("base_url")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
