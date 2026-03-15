from typing import Any

from webapi.models.common import WebAPIModel


class ChatRequest(WebAPIModel):
    message: str
    persist_user_message: str | None = None
    system_message: str | None = None
    model: str | None = None
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] | None = None
    skip_context_files: bool = False
    skip_memory: bool = False


class ChatResponse(WebAPIModel):
    session_id: str
    run_id: str
    model: str
    final_response: str | None = None
    completed: bool
    partial: bool
    interrupted: bool
    api_calls: int
    messages: list[dict[str, Any]]
    last_reasoning: str | None = None
    response_previewed: bool = False
