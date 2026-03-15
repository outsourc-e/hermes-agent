from typing import Any

from webapi.models.common import WebAPIModel


class SkillsListResponse(WebAPIModel):
    success: bool
    skills: list[dict[str, Any]]
    categories: list[str]
    count: int | None = None
    hint: str | None = None


class SkillDetailResponse(WebAPIModel):
    success: bool
    name: str
    content: str
    category: str | None = None
