from fastapi import APIRouter

from webapi.deps import get_config, get_runtime_agent_kwargs, get_runtime_model
from webapi.models.config import ConfigResponse


router = APIRouter(prefix="/api/config", tags=["config"])


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
