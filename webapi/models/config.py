from typing import Any

from webapi.models.common import WebAPIModel


class ConfigResponse(WebAPIModel):
    model: str
    provider: str | None = None
    api_mode: str | None = None
    base_url: str | None = None
    config: dict[str, Any]
