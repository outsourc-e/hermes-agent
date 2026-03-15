from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from tools.memory_tool import MemoryStore
from webapi.deps import reload_memory_store
from webapi.models.memory import (
    MemoryDeleteRequest,
    MemoryMutationResponse,
    MemoryPatchRequest,
    MemoryPostRequest,
    MemoryReadResponse,
)


router = APIRouter(prefix="/api/memory", tags=["memory"])


def _read_target(store: MemoryStore, target: str) -> dict:
    if target == "memory":
        return {
            "target": target,
            "entries": store.memory_entries,
            "usage": store._success_response("memory")["usage"],
            "entry_count": len(store.memory_entries),
        }
    if target == "user":
        return {
            "target": target,
            "entries": store.user_entries,
            "usage": store._success_response("user")["usage"],
            "entry_count": len(store.user_entries),
        }
    raise HTTPException(status_code=400, detail=f"Invalid target '{target}'")


@router.get("", response_model=MemoryReadResponse)
async def get_memory(
    target: str = Query("all"),
    store: Annotated[MemoryStore, Depends(reload_memory_store)] = None,
) -> MemoryReadResponse:
    if target == "all":
        return MemoryReadResponse(
            targets=[
                _read_target(store, "memory"),
                _read_target(store, "user"),
            ]
        )
    return MemoryReadResponse(targets=[_read_target(store, target)])


@router.post("", response_model=MemoryMutationResponse)
async def add_memory(
    payload: MemoryPostRequest,
    store: Annotated[MemoryStore, Depends(reload_memory_store)],
) -> MemoryMutationResponse:
    result = store.add(payload.target, payload.content)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return MemoryMutationResponse.model_validate(result)


@router.patch("", response_model=MemoryMutationResponse)
async def patch_memory(
    payload: MemoryPatchRequest,
    store: Annotated[MemoryStore, Depends(reload_memory_store)],
) -> MemoryMutationResponse:
    result = store.replace(payload.target, payload.old_text, payload.content)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return MemoryMutationResponse.model_validate(result)


@router.delete("", response_model=MemoryMutationResponse)
async def delete_memory(
    payload: MemoryDeleteRequest,
    store: Annotated[MemoryStore, Depends(reload_memory_store)],
) -> MemoryMutationResponse:
    result = store.remove(payload.target, payload.old_text)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return MemoryMutationResponse.model_validate(result)
