import time

from fastapi import APIRouter

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
