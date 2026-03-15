import json
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from hermes_state import SessionDB
from webapi.deps import WEB_SOURCE, ensure_session_title, get_session_db, new_session_id
from webapi.models.sessions import (
    ForkSessionResponse,
    MessageListResponse,
    MessageRecord,
    SearchSessionsResponse,
    SessionCreateRequest,
    SessionDetailResponse,
    SessionListResponse,
    SessionPatchRequest,
    SessionRecord,
)


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _coerce_session(row: dict) -> SessionRecord:
    return SessionRecord.model_validate(row)


def _coerce_message(row: dict) -> MessageRecord:
    return MessageRecord.model_validate(row)


@router.get("/search", response_model=SearchSessionsResponse)
async def search_sessions(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session_db: Annotated[SessionDB, Depends(get_session_db)] = None,
) -> SearchSessionsResponse:
    results = session_db.search_messages(
        q,
        source_filter=["web", "webapi", "cli", "telegram", "discord", "whatsapp", "slack"],
        limit=limit,
        offset=offset,
    )
    return SearchSessionsResponse(query=q, count=len(results), results=results)


@router.post("", response_model=SessionDetailResponse, status_code=201)
async def create_session(
    payload: SessionCreateRequest,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> SessionDetailResponse:
    session_id = payload.id or new_session_id()
    title = ensure_session_title(session_db, payload.title)
    session_db.create_session(
        session_id=session_id,
        source=payload.source or WEB_SOURCE,
        model=payload.model,
        model_config=payload.session_model_config,
        system_prompt=payload.system_prompt,
        user_id=payload.user_id,
        parent_session_id=payload.parent_session_id,
    )
    if title:
        session_db.set_session_title(session_id, title)
    session = session_db.get_session(session_id)
    return SessionDetailResponse(session=_coerce_session(session))


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None),
    session_db: Annotated[SessionDB, Depends(get_session_db)] = None,
) -> SessionListResponse:
    sessions = session_db.list_sessions_rich(source=source, limit=limit, offset=offset)
    return SessionListResponse(items=[_coerce_session(item) for item in sessions], total=session_db.session_count(source=source))


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> SessionDetailResponse:
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return SessionDetailResponse(session=_coerce_session(session))


@router.get("/{session_id}/messages", response_model=MessageListResponse)
async def get_session_messages(
    session_id: str,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> MessageListResponse:
    if not session_db.get_session(session_id):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    messages = session_db.get_messages(session_id)
    return MessageListResponse(items=[_coerce_message(item) for item in messages], total=len(messages))


@router.patch("/{session_id}", response_model=SessionDetailResponse)
async def patch_session(
    session_id: str,
    payload: SessionPatchRequest,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> SessionDetailResponse:
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    if payload.title is not None:
        try:
            session_db.set_session_title(session_id, payload.title)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.system_prompt is not None:
        session_db.update_system_prompt(session_id, payload.system_prompt)
    if payload.end_reason is not None:
        session_db.end_session(session_id, payload.end_reason)

    updated = session_db.get_session(session_id)
    return SessionDetailResponse(session=_coerce_session(updated))


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> dict[str, bool | str]:
    try:
        deleted = session_db.delete_session(session_id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Session '{session_id}' cannot be deleted because it has dependent forked sessions"
            ),
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"ok": True, "session_id": session_id}


@router.post("/{session_id}/fork", response_model=ForkSessionResponse)
async def fork_session(
    session_id: str,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> ForkSessionResponse:
    original = session_db.export_session(session_id)
    if not original:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    fork_id = new_session_id()
    fork_title = session_db.get_next_title_in_lineage(original.get("title") or "New Chat")
    model_config = original.get("model_config")
    if isinstance(model_config, str) and model_config:
        try:
            model_config = json.loads(model_config)
        except json.JSONDecodeError:
            model_config = None

    session_db.create_session(
        session_id=fork_id,
        source=original.get("source") or WEB_SOURCE,
        model=original.get("model"),
        model_config=model_config,
        system_prompt=original.get("system_prompt"),
        user_id=original.get("user_id"),
        parent_session_id=session_id,
    )
    session_db.set_session_title(fork_id, fork_title)

    for message in original.get("messages", []):
        session_db.append_message(
            session_id=fork_id,
            role=message.get("role"),
            content=message.get("content"),
            tool_name=message.get("tool_name"),
            tool_calls=message.get("tool_calls"),
            tool_call_id=message.get("tool_call_id"),
            finish_reason=message.get("finish_reason"),
        )

    forked = session_db.get_session(fork_id)
    return ForkSessionResponse(session=_coerce_session(forked), forked_from=session_id)
