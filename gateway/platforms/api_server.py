"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        — OpenAI Chat Completions format (stateless)
- POST /v1/responses               — OpenAI Responses API format (stateful via previous_response_id)
- GET  /v1/responses/{response_id} — Retrieve a stored response
- DELETE /v1/responses/{response_id} — Delete a stored response
- GET  /v1/models                  — lists hermes-agent as an available model
- GET  /health                     — health check

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat,
AnythingLLM, NextChat, ChatBox, etc.) can connect to hermes-agent
through this adapter by pointing at http://localhost:8642/v1.

Requires:
- aiohttp (already available in the gateway)
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
)
from hermes_cli.config import load_config, save_config
from hermes_cli.models import curated_models_for_provider, list_available_providers
from hermes_state import SessionDB
from tools.memory_tool import MemoryStore
from tools.skills_tool import skill_view, skills_categories, skills_list

logger = logging.getLogger(__name__)

# Default settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100
MAX_REQUEST_BYTES = 1_000_000  # 1 MB default limit for POST bodies


def check_api_server_requirements() -> bool:
    """Check if API server dependencies are available."""
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    SQLite-backed LRU store for Responses API state.

    Each stored response includes the full internal conversation history
    (with tool calls and results) so it can be reconstructed on subsequent
    requests via previous_response_id.

    Persists across gateway restarts.  Falls back to in-memory SQLite
    if the on-disk path is unavailable.
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES, db_path: str = None):
        self._max_size = max_size
        if db_path is None:
            try:
                from hermes_cli.config import get_hermes_home
                db_path = str(get_hermes_home() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                name TEXT PRIMARY KEY,
                response_id TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def get(self, response_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored response by ID (updates access time for LRU)."""
        row = self._conn.execute(
            "SELECT data FROM responses WHERE response_id = ?", (response_id,)
        ).fetchone()
        if row is None:
            return None
        import time
        self._conn.execute(
            "UPDATE responses SET accessed_at = ? WHERE response_id = ?",
            (time.time(), response_id),
        )
        self._conn.commit()
        return json.loads(row[0])

    def put(self, response_id: str, data: Dict[str, Any]) -> None:
        """Store a response, evicting the oldest if at capacity."""
        import time
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (response_id, data, accessed_at) VALUES (?, ?, ?)",
            (response_id, json.dumps(data, default=str), time.time()),
        )
        # Evict oldest entries beyond max_size
        count = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        if count > self._max_size:
            self._conn.execute(
                "DELETE FROM responses WHERE response_id IN "
                "(SELECT response_id FROM responses ORDER BY accessed_at ASC LIMIT ?)",
                (count - self._max_size,),
            )
        self._conn.commit()

    def delete(self, response_id: str) -> bool:
        """Remove a response from the store. Returns True if found and deleted."""
        cursor = self._conn.execute(
            "DELETE FROM responses WHERE response_id = ?", (response_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_conversation(self, name: str) -> Optional[str]:
        """Get the latest response_id for a conversation name."""
        row = self._conn.execute(
            "SELECT response_id FROM conversations WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None

    def set_conversation(self, name: str, response_id: str) -> None:
        """Map a conversation name to its latest response_id."""
        self._conn.execute(
            "INSERT OR REPLACE INTO conversations (name, response_id) VALUES (?, ?)",
            (name, response_id),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """Add CORS headers for explicitly allowed origins; handle OPTIONS preflight."""
        adapter = request.app.get("api_server_adapter")
        origin = request.headers.get("Origin", "")
        cors_headers = None
        if adapter is not None:
            if not adapter._origin_allowed(origin):
                return web.Response(status=403)
            cors_headers = adapter._cors_headers_for_origin(origin)

        if request.method == "OPTIONS":
            if cors_headers is None:
                return web.Response(status=403)
            return web.Response(status=200, headers=cors_headers)

        response = await handler(request)
        if cors_headers is not None:
            response.headers.update(cors_headers)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


def _openai_error(message: str, err_type: str = "invalid_request_error", param: str = None, code: str = None) -> Dict[str, Any]:
    """OpenAI-style error envelope."""
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def body_limit_middleware(request, handler):
        """Reject overly large request bodies early based on Content-Length."""
        if request.method in ("POST", "PUT", "PATCH"):
            cl = request.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > MAX_REQUEST_BYTES:
                        return web.json_response(_openai_error("Request body too large.", code="body_too_large"), status=413)
                except ValueError:
                    return web.json_response(_openai_error("Invalid Content-Length header.", code="invalid_content_length"), status=400)
        return await handler(request)
else:
    body_limit_middleware = None  # type: ignore[assignment]

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def security_headers_middleware(request, handler):
        """Add security headers to all responses (including errors)."""
        response = await handler(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response
else:
    security_headers_middleware = None  # type: ignore[assignment]


class _IdempotencyCache:
    """In-memory idempotency cache with TTL and basic LRU semantics."""
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 300):
        from collections import OrderedDict
        self._store = OrderedDict()
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        import time as _t
        now = _t.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_set(self, key: str, fingerprint: str, compute_coro):
        self._purge()
        item = self._store.get(key)
        if item and item["fp"] == fingerprint:
            return item["resp"]
        resp = await compute_coro()
        import time as _t
        self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
        self._purge()
        return resp


_idem_cache = _IdempotencyCache()


def _make_request_fingerprint(body: Dict[str, Any], keys: List[str]) -> str:
    from hashlib import sha256
    subset = {k: body.get(k) for k in keys}
    return sha256(repr(subset).encode("utf-8")).hexdigest()


# Cron module — imported at module level to avoid Python descriptor binding
# issue when functions are imported inside a class body.
try:
    from cron.jobs import (
        list_jobs as _cron_list,
        get_job as _cron_get,
        create_job as _cron_create,
        update_job as _cron_update,
        remove_job as _cron_remove,
        pause_job as _cron_pause,
        resume_job as _cron_resume,
        trigger_job as _cron_trigger,
    )
    _CRON_MODULE_AVAILABLE = True
except ImportError:
    _CRON_MODULE_AVAILABLE = False


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI-compatible HTTP API server adapter.

    Runs an aiohttp web server that accepts OpenAI-format requests
    and routes them through hermes-agent's AIAgent.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.API_SERVER)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        self._port: int = int(extra.get("port", os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))))
        self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
        self._cors_origins: tuple[str, ...] = self._parse_cors_origins(
            extra.get("cors_origins", os.getenv("API_SERVER_CORS_ORIGINS", "")),
        )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._response_store = ResponseStore()
        self._session_db: Optional[SessionDB] = None
        self._memory_store: Optional[MemoryStore] = None

    @staticmethod
    def _parse_cors_origins(value: Any) -> tuple[str, ...]:
        """Normalize configured CORS origins into a stable tuple."""
        if not value:
            return ()

        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = [str(value)]

        return tuple(str(item).strip() for item in items if str(item).strip())

    def _cors_headers_for_origin(self, origin: str) -> Optional[Dict[str, str]]:
        """Return CORS headers for an allowed browser origin."""
        if not origin or not self._cors_origins:
            return None

        if "*" in self._cors_origins:
            headers = dict(_CORS_HEADERS)
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Max-Age"] = "600"
            return headers

        if origin not in self._cors_origins:
            return None

        headers = dict(_CORS_HEADERS)
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Max-Age"] = "600"
        return headers

    def _origin_allowed(self, origin: str) -> bool:
        """Allow non-browser clients and explicitly configured browser origins."""
        if not origin:
            return True

        if not self._cors_origins:
            return False

        return "*" in self._cors_origins or origin in self._cors_origins

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        Validate Bearer token from Authorization header.

        Returns None if auth is OK, or a 401 web.Response on failure.
        If no API key is configured, all requests are allowed.
        """
        if not self._api_key:
            return None  # No key configured — allow all (local-only use)

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token == self._api_key:
                return None  # Auth OK

        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status=401,
        )

    def _get_session_db(self) -> SessionDB:
        """Create the session DB lazily."""
        if self._session_db is None:
            self._session_db = SessionDB()
        return self._session_db

    def _get_memory_store(self) -> MemoryStore:
        """Create the memory store lazily."""
        if self._memory_store is None:
            self._memory_store = MemoryStore()
            self._memory_store.load_from_disk()
        return self._memory_store

    @staticmethod
    def _normalize_session_record(session: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Parse serialized session fields into API-friendly JSON."""
        if session is None:
            return None
        normalized = dict(session)
        model_config = normalized.get("model_config")
        if model_config:
            try:
                normalized["model_config"] = json.loads(model_config)
            except (TypeError, json.JSONDecodeError):
                pass
        return normalized

    @staticmethod
    def _current_model_settings(config: Dict[str, Any]) -> Dict[str, Any]:
        """Extract model/provider/base_url/api_mode from config.yaml."""
        model_cfg = config.get("model")
        if isinstance(model_cfg, dict):
            return {
                "model": str(model_cfg.get("default") or model_cfg.get("model") or "").strip(),
                "provider": str(model_cfg.get("provider") or "").strip(),
                "api_mode": str(model_cfg.get("api_mode") or "").strip(),
                "base_url": str(model_cfg.get("base_url") or "").strip(),
            }
        if isinstance(model_cfg, str):
            return {
                "model": model_cfg.strip(),
                "provider": "",
                "api_mode": "",
                "base_url": "",
            }
        return {"model": "", "provider": "", "api_mode": "", "base_url": ""}

    @staticmethod
    def _parse_int(value: Any, default: int, minimum: int = 0) -> int:
        """Parse an integer query parameter with bounds."""
        if value in (None, ""):
            return default
        parsed = int(value)
        if parsed < minimum:
            raise ValueError(f"Value must be >= {minimum}")
        return parsed

    # ------------------------------------------------------------------
    # Agent creation helper
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_content(
        text: str, attachments: Optional[List[Dict[str, Any]]] = None
    ) -> tuple:
        """Build multimodal content from text + image attachments.

        Returns (user_content, persist_text) where user_content is either
        a plain string or a list of content parts for multimodal input.
        """
        if not attachments:
            return text, text

        image_parts: List[Dict[str, Any]] = []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            mime = ""
            for key in ("contentType", "mimeType", "mediaType"):
                val = att.get(key)
                if isinstance(val, str) and val.strip():
                    mime = val.strip()
                    break
            if not mime.startswith("image/"):
                continue
            content = ""
            for key in ("content", "base64", "data"):
                val = att.get(key)
                if isinstance(val, str) and val.strip():
                    content = val.strip()
                    break
            if not content:
                # Try dataUrl format: data:image/png;base64,...
                data_url = att.get("dataUrl", "")
                if isinstance(data_url, str) and data_url.startswith("data:"):
                    content = data_url.split(",", 1)[-1] if "," in data_url else ""
            if not content:
                continue
            image_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{content}"},
            })

        if not image_parts:
            return text, text

        content_parts: List[Dict[str, Any]] = []
        if text.strip():
            content_parts.append({"type": "text", "text": text})
        content_parts.extend(image_parts)
        return content_parts, text

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.  Toolsets are resolved
        from config.yaml platform_toolsets.api_server (same as all other
        gateway platforms), falling back to the hermes-api-server default.
        """
        from run_agent import AIAgent
        from gateway.run import _resolve_runtime_agent_kwargs, _resolve_gateway_model, _load_gateway_config
        from hermes_cli.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        model = _resolve_gateway_model()

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))

        max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="api_server",
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
        )
        return agent

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "hermes-agent"})

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models — return hermes-agent as an available model."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": "hermes-agent",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "hermes",
                    "permission": [],
                    "root": "hermes-agent",
                    "parent": None,
                }
            ],
        })

    async def _handle_list_sessions(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions — list sessions."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            limit = self._parse_int(request.query.get("limit"), 50)
            offset = self._parse_int(request.query.get("offset"), 0)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        source = (request.query.get("source") or "").strip() or None
        db = self._get_session_db()
        items = [
            self._normalize_session_record(item)
            for item in db.list_sessions_rich(source=source, limit=limit, offset=offset)
        ]
        total = db.session_count(source=source)
        return web.json_response({"items": items, "total": total})

    async def _handle_create_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions — create a new session."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)

        title = body.get("title")
        source = str(body.get("source") or "api_server").strip() or "api_server"
        model = body.get("model")
        system_prompt = body.get("system_prompt")
        session_id = f"sess_{uuid.uuid4().hex}"
        db = self._get_session_db()

        try:
            db.create_session(
                session_id=session_id,
                source=source,
                model=model,
                system_prompt=system_prompt,
            )
            if title is not None:
                db.set_session_title(session_id, str(title))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        session = self._normalize_session_record(db.get_session(session_id))
        return web.json_response({"session": session})

    async def _handle_search_sessions(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/search — search messages across sessions."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        query = (request.query.get("q") or "").strip()
        if not query:
            return web.json_response({"error": "Missing query parameter: q"}, status=400)
        try:
            limit = self._parse_int(request.query.get("limit"), 20)
            offset = self._parse_int(request.query.get("offset"), 0)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        results = self._get_session_db().search_messages(query=query, limit=limit, offset=offset)
        return web.json_response({"query": query, "count": len(results), "results": results})

    async def _handle_get_session(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id} — fetch one session."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session = self._normalize_session_record(self._get_session_db().get_session(session_id))
        if session is None:
            return web.json_response({"error": "Session not found"}, status=404)
        return web.json_response({"session": session})

    async def _handle_get_session_messages(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}/messages — fetch session messages."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        db = self._get_session_db()
        if db.get_session(session_id) is None:
            db.ensure_session(session_id, source="web")
        items = db.get_messages(session_id)
        return web.json_response({"items": items, "total": len(items)})

    async def _handle_update_session(self, request: "web.Request") -> "web.Response":
        """PATCH /api/sessions/{session_id} — update a session."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        db = self._get_session_db()
        if db.get_session(session_id) is None:
            return web.json_response({"error": "Session not found"}, status=404)
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)

        try:
            if "title" in body:
                db.set_session_title(session_id, body.get("title"))
            if "system_prompt" in body:
                db.update_system_prompt(session_id, body.get("system_prompt"))
            if "end_reason" in body:
                db.end_session(session_id, str(body.get("end_reason") or "updated"))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        session = self._normalize_session_record(db.get_session(session_id))
        return web.json_response({"session": session})

    async def _handle_delete_session(self, request: "web.Request") -> "web.Response":
        """DELETE /api/sessions/{session_id} — delete a session."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        deleted = self._get_session_db().delete_session(session_id)
        if not deleted:
            return web.json_response({"error": "Session not found"}, status=404)
        return web.json_response({"ok": True})

    async def _handle_fork_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/fork — clone a session and its messages."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        db = self._get_session_db()
        original = db.get_session(session_id)
        if original is None:
            return web.json_response({"error": "Session not found"}, status=404)

        forked_id = f"sess_{uuid.uuid4().hex}"
        try:
            db.create_session(
                session_id=forked_id,
                source=original.get("source") or "api_server",
                model=original.get("model"),
                system_prompt=original.get("system_prompt"),
                user_id=original.get("user_id"),
                parent_session_id=session_id,
            )
            messages = db.get_messages(session_id)
            for message in messages:
                db.append_message(
                    session_id=forked_id,
                    role=message.get("role"),
                    content=message.get("content"),
                    tool_name=message.get("tool_name"),
                    tool_calls=message.get("tool_calls"),
                    tool_call_id=message.get("tool_call_id"),
                    token_count=message.get("token_count"),
                    finish_reason=message.get("finish_reason"),
                    reasoning=message.get("reasoning"),
                )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        session = self._normalize_session_record(db.get_session(forked_id))
        return web.json_response({"session": session, "forked_from": session_id})

    async def _handle_session_chat(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat — run a session-aware chat turn."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        session_id = request.match_info["session_id"]
        db = self._get_session_db()
        session = self._normalize_session_record(db.get_session(session_id))
        if session is None:
            db.ensure_session(session_id, source="web")
            session = self._normalize_session_record(db.get_session(session_id)) or {}

        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)

        message = body.get("message")
        if not isinstance(message, str):
            return web.json_response({"error": "Missing or invalid 'message' field"}, status=400)

        raw_attachments_sync = body.get("attachments")
        if raw_attachments_sync:
            logger.debug("[chat] Received %d attachment(s): %s",
                         len(raw_attachments_sync),
                         [(a.get("name"), a.get("contentType"), len(a.get("content", "") or a.get("base64", "") or "")) for a in raw_attachments_sync if isinstance(a, dict)])
        user_content, persist_text = self._build_user_content(message, raw_attachments_sync)
        if isinstance(user_content, list):
            logger.debug("[chat] Built multimodal content with %d parts", len(user_content))

        model = body.get("model") or session.get("model") or "hermes-agent"
        system_message = body.get("system_message")
        history = db.get_messages_as_conversation(session_id)
        loop = asyncio.get_event_loop()

        def _run():
            agent = self._create_agent(
                ephemeral_system_prompt=system_message,
                session_id=session_id,
            )
            agent._session_db = db  # Enable session persistence
            result = agent.run_conversation(
                user_content,
                conversation_history=history,
                persist_user_message=persist_text,
            )
            usage = {
                "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
            }
            return result, usage

        try:
            result, usage = await loop.run_in_executor(None, _run)
        except Exception as e:
            logger.error("Error running session chat for %s: %s", session_id, e, exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

        return web.json_response({
            "session_id": session_id,
            "run_id": f"run_{uuid.uuid4().hex}",
            "model": model,
            "final_response": result.get("final_response"),
            "completed": result.get("completed", False),
            "partial": result.get("partial", False),
            "interrupted": result.get("interrupted", False),
            "api_calls": result.get("api_calls", 0),
            "messages": result.get("messages", []),
            "last_reasoning": result.get("last_reasoning"),
            "response_previewed": result.get("response_previewed", False),
            "usage": usage,
        })

    async def _handle_session_chat_stream(self, request: "web.Request") -> "web.StreamResponse":
        """POST /api/sessions/{session_id}/chat/stream — stream a session chat turn over SSE."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        session_id = request.match_info["session_id"]
        db = self._get_session_db()
        session = self._normalize_session_record(db.get_session(session_id))
        if session is None:
            db.ensure_session(session_id, source="web")
            session = self._normalize_session_record(db.get_session(session_id)) or {}

        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)

        message = body.get("message")
        if not isinstance(message, str):
            return web.json_response({"error": "Missing or invalid 'message' field"}, status=400)

        # Build multimodal content if image attachments are present
        raw_attachments = body.get("attachments")
        if raw_attachments:
            logger.debug("[chat/stream] Received %d attachment(s): %s",
                         len(raw_attachments),
                         [(a.get("name"), a.get("contentType"), len(a.get("content", "") or a.get("base64", "") or "")) for a in raw_attachments if isinstance(a, dict)])
        user_content, persist_text = self._build_user_content(message, raw_attachments)
        if isinstance(user_content, list):
            logger.debug("[chat/stream] Built multimodal content with %d parts", len(user_content))

        system_message = body.get("system_message")
        history = db.get_messages_as_conversation(session_id)
        assistant_message_id = f"msg_asst_{uuid.uuid4().hex}"

        # Note: user message persistence is handled by AIAgent._flush_messages_to_session_db
        # Don't double-persist here or messages will appear twice

        import queue as _q
        stream_q: _q.Queue = _q.Queue()

        def _encode_sse(event_name: str, payload: Dict[str, Any]) -> bytes:
            return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

        def _queue_event(event_name: str, payload: Dict[str, Any]) -> None:
            stream_q.put(_encode_sse(event_name, payload))

        def _tool_map(messages: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
            mapping: Dict[str, Dict[str, Any]] = {}
            for item in messages:
                if item.get("role") != "assistant":
                    continue
                for index, tool_call in enumerate(item.get("tool_calls") or []):
                    tool_id = tool_call.get("id")
                    if not tool_id:
                        continue
                    fn = tool_call.get("function") or {}
                    raw_args = fn.get("arguments")
                    try:
                        parsed_args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
                    except json.JSONDecodeError:
                        parsed_args = raw_args
                    mapping[tool_id] = {
                        "tool_name": fn.get("name") or item.get("tool_name") or f"tool_{index + 1}",
                        "args": parsed_args,
                    }
            return mapping

        def _result_preview(content: Any, limit: int = 4000) -> str:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            return text[:limit] + ("..." if len(text) > limit else "")

        run_id = f"run_{uuid.uuid4().hex}"

        def _on_delta(delta):
            if delta:
                _queue_event(
                    "assistant.delta",
                    {"session_id": session_id, "run_id": run_id, "message_id": assistant_message_id, "delta": delta},
                )

        def _on_tool_progress(name, preview, args):
            if name == "_thinking":
                _queue_event(
                    "tool.progress",
                    {"session_id": session_id, "run_id": run_id, "message_id": assistant_message_id, "delta": preview},
                )
                return
            payload = {
                "session_id": session_id,
                "run_id": run_id,
                "tool_name": name,
                "preview": preview,
                "args": args,
            }
            _queue_event("tool.started", payload)

        agent_ref = [None]
        loop = asyncio.get_event_loop()

        async def _run_agent_task():
            def _run():
                agent = self._create_agent(
                    ephemeral_system_prompt=system_message,
                    session_id=session_id,
                    stream_delta_callback=_on_delta,
                    tool_progress_callback=_on_tool_progress,
                )
                agent._session_db = db  # Enable session persistence
                agent_ref[0] = agent
                return agent.run_conversation(
                    user_content,
                    conversation_history=history,
                    persist_user_message=persist_text,
                )

            return await loop.run_in_executor(None, _run)

        agent_task = asyncio.ensure_future(_run_agent_task())

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)

        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        try:
            user_message_id = f"msg_user_{uuid.uuid4().hex}"
            await response.write(_encode_sse("session.created", {
                "session_id": session_id,
                "run_id": run_id,
                "title": session.get("title") or "New Chat",
            }))
            await response.write(_encode_sse("run.started", {
                "session_id": session_id,
                "run_id": run_id,
                "user_message": {
                    "id": user_message_id,
                    "role": "user",
                    "content": message,
                },
            }))
            await response.write(_encode_sse("message.started", {
                "session_id": session_id,
                "run_id": run_id,
                "message": {"id": assistant_message_id, "role": "assistant"},
            }))

            while True:
                try:
                    frame = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        while True:
                            try:
                                frame = stream_q.get_nowait()
                                if frame is None:
                                    break
                                await response.write(frame)
                            except _q.Empty:
                                break
                        break
                    continue

                if frame is None:
                    break

                await response.write(frame)

            result = await agent_task
            tools = _tool_map(result.get("messages") or [])
            for item in result.get("messages") or []:
                if item.get("role") != "tool":
                    continue
                tool_id = item.get("tool_call_id")
                tool_meta = tools.get(tool_id, {})
                await response.write(_encode_sse("tool.completed", {
                    "session_id": session_id,
                    "run_id": run_id,
                    "tool_call_id": tool_id,
                    "tool_name": tool_meta.get("tool_name") or item.get("tool_name") or "unknown",
                    "args": tool_meta.get("args"),
                    "result_preview": _result_preview(item.get("content")),
                }))

            await response.write(_encode_sse("assistant.completed", {
                "session_id": session_id,
                "run_id": run_id,
                "message_id": assistant_message_id,
                "content": result.get("final_response") or "",
                "completed": result.get("completed", False),
                "partial": result.get("partial", False),
                "interrupted": result.get("interrupted", False),
            }))
            await response.write(_encode_sse("run.completed", {
                "session_id": session_id,
                "run_id": run_id,
                "message_id": assistant_message_id,
                "completed": result.get("completed", False),
                "partial": result.get("partial", False),
                "interrupted": result.get("interrupted", False),
                "api_calls": result.get("api_calls"),
            }))
            await response.write(_encode_sse("done", {"session_id": session_id, "run_id": run_id, "state": "final"}))
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            agent = agent_ref[0]
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("Session SSE client disconnected; interrupted session %s", session_id)

        return response

    async def _handle_get_memory(self, request: "web.Request") -> "web.Response":
        """GET /api/memory — read current memory state."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        target = (request.query.get("target") or "all").strip().lower()
        if target not in {"all", "memory", "user"}:
            return web.json_response({"error": "target must be one of: all, memory, user"}, status=400)

        store = self._get_memory_store()
        store.load_from_disk()
        targets = []
        if target in {"all", "memory"}:
            targets.append({
                "target": "memory",
                "entries": store.memory_entries,
                "entry_count": len(store.memory_entries),
            })
        if target in {"all", "user"}:
            targets.append({
                "target": "user",
                "entries": store.user_entries,
                "entry_count": len(store.user_entries),
            })
        return web.json_response({"targets": targets})

    async def _handle_add_memory(self, request: "web.Request") -> "web.Response":
        """POST /api/memory — add a memory entry."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)

        target = str(body.get("target") or "").strip().lower()
        content = str(body.get("content") or "")
        if target not in {"memory", "user"}:
            return web.json_response({"error": "target must be 'memory' or 'user'"}, status=400)
        result = self._get_memory_store().add(target, content)
        status = 200 if result.get("success") else 400
        return web.json_response(result, status=status)

    async def _handle_replace_memory(self, request: "web.Request") -> "web.Response":
        """PATCH /api/memory — replace a memory entry."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)

        target = str(body.get("target") or "").strip().lower()
        old_text = str(body.get("old_text") or "")
        content = str(body.get("content") or "")
        if target not in {"memory", "user"}:
            return web.json_response({"error": "target must be 'memory' or 'user'"}, status=400)
        result = self._get_memory_store().replace(target, old_text, content)
        status = 200 if result.get("success") else 400
        return web.json_response(result, status=status)

    async def _handle_delete_memory(self, request: "web.Request") -> "web.Response":
        """DELETE /api/memory — delete a memory entry."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)

        target = str(body.get("target") or "").strip().lower()
        old_text = str(body.get("old_text") or "")
        if target not in {"memory", "user"}:
            return web.json_response({"error": "target must be 'memory' or 'user'"}, status=400)
        result = self._get_memory_store().remove(target, old_text)
        status = 200 if result.get("success") else 400
        return web.json_response(result, status=status)

    async def _handle_list_skills(self, request: "web.Request") -> "web.Response":
        """GET /api/skills — list skills."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        category = (request.query.get("category") or "").strip() or None
        return web.json_response(json.loads(skills_list(category=category)))

    async def _handle_skill_categories(self, request: "web.Request") -> "web.Response":
        """GET /api/skills/categories — list skill categories."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        return web.json_response(json.loads(skills_categories()))

    async def _handle_view_skill(self, request: "web.Request") -> "web.Response":
        """GET /api/skills/{name} — fetch skill details."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        name = request.match_info["name"]
        file_path = (request.query.get("file_path") or "").strip() or None
        return web.json_response(json.loads(skill_view(name, file_path=file_path)))

    async def _handle_get_config(self, request: "web.Request") -> "web.Response":
        """GET /api/config — fetch the current config."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        config = load_config()
        current = self._current_model_settings(config)
        return web.json_response({
            "model": current["model"],
            "provider": current["provider"],
            "api_mode": current["api_mode"],
            "base_url": current["base_url"],
            "config": config,
        })

    async def _handle_update_config(self, request: "web.Request") -> "web.Response":
        """PATCH /api/config — update model/provider/base_url settings."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)

        config = load_config()
        model_cfg = config.get("model")
        if isinstance(model_cfg, dict):
            updated_model_cfg = dict(model_cfg)
        elif isinstance(model_cfg, str) and model_cfg.strip():
            updated_model_cfg = {"default": model_cfg.strip()}
        else:
            updated_model_cfg = {}

        if "model" in body:
            updated_model_cfg["default"] = str(body.get("model") or "").strip()
        if "provider" in body:
            updated_model_cfg["provider"] = str(body.get("provider") or "").strip()
        if "base_url" in body:
            updated_model_cfg["base_url"] = str(body.get("base_url") or "").strip()

        config["model"] = updated_model_cfg
        try:
            save_config(config)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        current = self._current_model_settings(config)
        return web.json_response({
            "ok": True,
            "model": current["model"],
            "provider": current["provider"],
            "base_url": current["base_url"],
        })

    async def _handle_available_models(self, request: "web.Request") -> "web.Response":
        """GET /api/available-models — list provider models and available providers."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        config = load_config()
        current = self._current_model_settings(config)
        provider = (request.query.get("provider") or current["provider"] or "openrouter").strip()
        models = [
            {"id": model_id, "description": description}
            for model_id, description in curated_models_for_provider(provider)
        ]

        # Ensure the currently configured model is always in the list
        current_model = (current.get("model") or "").strip()
        if current_model:
            model_ids = {m["id"] for m in models}
            if current_model not in model_ids:
                models.insert(0, {"id": current_model, "description": "current"})

        # Discover local Ollama models if reachable
        try:
            import aiohttp as _aiohttp
            for ollama_base in ["http://127.0.0.1:11434", "http://localhost:11434"]:
                try:
                    async with _aiohttp.ClientSession() as _sess:
                        async with _sess.get(f"{ollama_base}/api/tags", timeout=_aiohttp.ClientTimeout(total=2)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                model_ids = {m["id"] for m in models}
                                for om in (data.get("models") or []):
                                    name = om.get("name") or om.get("model") or ""
                                    if name and name not in model_ids:
                                        models.append({"id": name, "description": "local"})
                                        model_ids.add(name)
                                break
                except Exception:
                    continue
        except ImportError:
            pass

        providers = list_available_providers()
        return web.json_response({"provider": provider, "models": models, "providers": providers})

    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions — OpenAI Chat Completions format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(_openai_error("Invalid JSON in request body"), status=400)

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return web.json_response(
                {"error": {"message": "Missing or invalid 'messages' field", "type": "invalid_request_error"}},
                status=400,
            )

        # Fast-path for capability probes (max_tokens=1 or model='test')
        # Return a minimal valid response so frontends detect the endpoint
        max_tokens = body.get("max_tokens")
        probe_model = body.get("model", "")
        if max_tokens == 1 or probe_model == "test":
            return web.json_response({
                "id": f"chatcmpl-probe-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": probe_model or "hermes-agent",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
            })

        stream = body.get("stream", False)

        # Extract system message (becomes ephemeral system prompt layered ON TOP of core)
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                # Accumulate system messages
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in ("user", "assistant"):
                conversation_messages.append({"role": role, "content": content})

        # Extract the last user message as the primary input
        user_message = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not user_message:
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        session_id = str(uuid.uuid4())
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", "hermes-agent")
        created = int(time.time())

        if stream:
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # Filter out None — the agent fires stream_delta_callback(None)
                # to signal the CLI display to close its response box before
                # tool execution, but the SSE writer uses None as end-of-stream
                # sentinel.  Forwarding it would prematurely close the HTTP
                # response, causing Open WebUI (and similar frontends) to miss
                # the final answer after tool calls.  The SSE loop detects
                # completion via agent_task.done() instead.
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(name, preview, args):
                """Inject tool progress into the SSE stream for Open WebUI."""
                if name.startswith("_"):
                    return  # Skip internal events (_thinking)
                from agent.display import get_tool_emoji
                emoji = get_tool_emoji(name)
                label = preview or name
                _stream_q.put(f"\n`{emoji} {label}`\n")

            # Start agent in background.  agent_ref is a mutable container
            # so the SSE writer can interrupt the agent on client disconnect.
            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                agent_ref=agent_ref,
            ))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref,
            )

        # Non-streaming: run the agent (with optional Idempotency-Key)
        async def _compute_completion():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(body, keys=["model", "messages", "tools", "tool_choice", "stream"])
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_completion)
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_completion()
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_data = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_response,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        return web.json_response(response_data)

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None,
    ) -> "web.StreamResponse":
        """Write real streaming SSE from agent's stream_delta_callback queue.

        If the client disconnects mid-stream (network drop, browser tab close),
        the agent is interrupted via ``agent.interrupt()`` so it stops making
        LLM API calls, and the asyncio task wrapper is cancelled.
        """
        import queue as _q

        sse_headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        # CORS middleware can't inject headers into StreamResponse after
        # prepare() flushes them, so resolve CORS headers up front.
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        try:
            # Role chunk
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())

            # Stream content chunks as they arrive from the agent
            loop = asyncio.get_event_loop()
            while True:
                try:
                    delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain any remaining items
                        while True:
                            try:
                                delta = stream_q.get_nowait()
                                if delta is None:
                                    break
                                content_chunk = {
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": model,
                                    "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                                }
                                await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())
                            except _q.Empty:
                                break
                        break
                    continue

                if delta is None:  # End of stream sentinel
                    break

                content_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                }
                await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())

            # Get usage from completed agent
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
            except Exception:
                pass

            # Finish chunk
            finish_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
            await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # Client disconnected mid-stream.  Interrupt the agent so it
            # stops making LLM API calls at the next loop iteration, then
            # cancel the asyncio task wrapper.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", completion_id)

        return response

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — OpenAI Responses API format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        raw_input = body.get("input")
        if raw_input is None:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = body.get("store", True)

        # conversation and previous_response_id are mutually exclusive
        if conversation and previous_response_id:
            return web.json_response(_openai_error("Cannot use both 'conversation' and 'previous_response_id'"), status=400)

        # Resolve conversation name to latest response_id
        if conversation:
            previous_response_id = self._response_store.get_conversation(conversation)
            # No error if conversation doesn't exist yet — it's a new conversation

        # Normalize input to message list
        input_messages: List[Dict[str, str]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for item in raw_input:
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    # Handle content that may be a list of content parts
                    if isinstance(content, list):
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "input_text":
                                text_parts.append(part.get("text", ""))
                            elif isinstance(part, dict) and part.get("type") == "output_text":
                                text_parts.append(part.get("text", ""))
                            elif isinstance(part, str):
                                text_parts.append(part)
                        content = "\n".join(text_parts)
                    input_messages.append({"role": role, "content": content})
        else:
            return web.json_response(_openai_error("'input' must be a string or array"), status=400)

        # Reconstruct conversation history from previous_response_id
        conversation_history: List[Dict[str, str]] = []
        if previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored is None:
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            conversation_history = list(stored.get("conversation_history", []))
            # If no instructions provided, carry forward from previous
            if instructions is None:
                instructions = stored.get("instructions")

        # Append new input messages to history (all but the last become history)
        for msg in input_messages[:-1]:
            conversation_history.append(msg)

        # Last input message is the user_message
        user_message = input_messages[-1].get("content", "") if input_messages else ""
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        # Truncation support
        if body.get("truncation") == "auto" and len(conversation_history) > 100:
            conversation_history = conversation_history[-100:]

        # Run the agent (with Idempotency-Key support)
        session_id = str(uuid.uuid4())

        async def _compute_response():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(
                body,
                keys=["input", "instructions", "previous_response_id", "conversation", "model", "tools"],
            )
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_response)
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_response()
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())

        # Build the full conversation history for storage
        # (includes tool calls from the agent run)
        full_history = list(conversation_history)
        full_history.append({"role": "user", "content": user_message})
        # Add agent's internal messages if available
        agent_messages = result.get("messages", [])
        if agent_messages:
            full_history.extend(agent_messages)
        else:
            full_history.append({"role": "assistant", "content": final_response})

        # Build output items (includes tool calls + final message)
        output_items = self._extract_output_items(result)

        response_data = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "created_at": created_at,
            "model": body.get("model", "hermes-agent"),
            "output": output_items,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        # Store the complete response object for future chaining / GET retrieval
        if store:
            self._response_store.put(response_id, {
                "response": response_data,
                "conversation_history": full_history,
                "instructions": instructions,
            })
            # Update conversation mapping so the next request with the same
            # conversation name automatically chains to this response
            if conversation:
                self._response_store.set_conversation(conversation, response_id)

        return web.json_response(response_data)

    # ------------------------------------------------------------------
    # GET / DELETE response endpoints
    # ------------------------------------------------------------------

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} — retrieve a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} — delete a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        deleted = self._response_store.delete(response_id)
        if not deleted:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    # ------------------------------------------------------------------
    # Cron jobs API
    # ------------------------------------------------------------------

    # Check cron module availability once (not per-request)
    # NOTE: imports are done at module level to avoid Python descriptor
    # binding issue — functions imported inside a class body become
    # unbound methods, so self._fn(arg) passes self as first positional.
    _CRON_AVAILABLE = _CRON_MODULE_AVAILABLE

    _JOB_ID_RE = __import__("re").compile(r"[a-f0-9]{12}")
    # Allowed fields for update — prevents clients injecting arbitrary keys
    _UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    _MAX_NAME_LENGTH = 200
    _MAX_PROMPT_LENGTH = 5000

    def _check_jobs_available(self) -> Optional["web.Response"]:
        """Return error response if cron module isn't available."""
        if not self._CRON_AVAILABLE:
            return web.json_response(
                {"error": "Cron module not available"}, status=501,
            )
        return None

    def _check_job_id(self, request: "web.Request") -> tuple:
        """Validate and extract job_id. Returns (job_id, error_response)."""
        job_id = request.match_info["job_id"]
        if not self._JOB_ID_RE.fullmatch(job_id):
            return job_id, web.json_response(
                {"error": "Invalid job ID format"}, status=400,
            )
        return job_id, None

    async def _handle_list_jobs(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs — list all cron jobs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            include_disabled = request.query.get("include_disabled", "").lower() in ("true", "1")
            jobs = _cron_list(include_disabled=include_disabled)
            return web.json_response({"jobs": jobs})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs — create a new cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            schedule = (body.get("schedule") or "").strip()
            prompt = body.get("prompt", "")
            deliver = body.get("deliver", "local")
            skills = body.get("skills")
            repeat = body.get("repeat")

            if not name:
                return web.json_response({"error": "Name is required"}, status=400)
            if len(name) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if not schedule:
                return web.json_response({"error": "Schedule is required"}, status=400)
            if len(prompt) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
                return web.json_response({"error": "Repeat must be a positive integer"}, status=400)

            kwargs = {
                "prompt": prompt,
                "schedule": schedule,
                "name": name,
                "deliver": deliver,
            }
            if skills:
                kwargs["skills"] = skills
            if repeat is not None:
                kwargs["repeat"] = repeat

            job = _cron_create(**kwargs)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_job(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs/{job_id} — get a single cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update_job(self, request: "web.Request") -> "web.Response":
        """PATCH /api/jobs/{job_id} — update a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            body = await request.json()
            # Whitelist allowed fields to prevent arbitrary key injection
            sanitized = {k: v for k, v in body.items() if k in self._UPDATE_ALLOWED_FIELDS}
            if not sanitized:
                return web.json_response({"error": "No valid fields to update"}, status=400)
            # Validate lengths if present
            if "name" in sanitized and len(sanitized["name"]) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if "prompt" in sanitized and len(sanitized["prompt"]) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            job = _cron_update(job_id, sanitized)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_job(self, request: "web.Request") -> "web.Response":
        """DELETE /api/jobs/{job_id} — delete a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            success = _cron_remove(job_id)
            if not success:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pause_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/pause — pause a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_pause(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_resume_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/resume — resume a paused cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_resume(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_run_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/run — trigger immediate execution."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_trigger(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ------------------------------------------------------------------
    # Output extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_output_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Build the full output item array from the agent's messages.

        Walks *result["messages"]* and emits:
        - ``function_call`` items for each tool_call on assistant messages
        - ``function_call_output`` items for each tool-role message
        - a final ``message`` item with the assistant's text reply
        """
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])

        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", ""),
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        # Final assistant message
        final = result.get("final_response", "")
        if not final:
            final = result.get("error", "(No response generated)")

        items.append({
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": final,
                }
            ],
        })
        return items

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        agent_ref: Optional[list] = None,
    ) -> tuple:
        """
        Create an agent and run a conversation in a thread executor.

        Returns ``(result_dict, usage_dict)`` where *usage_dict* contains
        ``input_tokens``, ``output_tokens`` and ``total_tokens``.

        If *agent_ref* is a one-element list, the AIAgent instance is stored
        at ``agent_ref[0]`` before ``run_conversation`` begins.  This allows
        callers (e.g. the SSE writer) to call ``agent.interrupt()`` from
        another thread to stop in-progress LLM calls.
        """
        loop = asyncio.get_event_loop()

        def _run():
            agent = self._create_agent(
                ephemeral_system_prompt=ephemeral_system_prompt,
                session_id=session_id,
                stream_delta_callback=stream_delta_callback,
                tool_progress_callback=tool_progress_callback,
            )
            if agent_ref is not None:
                agent_ref[0] = agent
            result = agent.run_conversation(
                user_message=user_message,
                conversation_history=conversation_history,
            )
            usage = {
                "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
            }
            return result, usage

        return await loop.run_in_executor(None, _run)

    # ------------------------------------------------------------------
    # BasePlatformAdapter interface
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the aiohttp web server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        try:
            mws = [mw for mw in (cors_middleware, body_limit_middleware, security_headers_middleware) if mw is not None]
            self._app = web.Application(middlewares=mws)
            self._app["api_server_adapter"] = self
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get("/v1/health", self._handle_health)
            self._app.router.add_get("/v1/models", self._handle_models)
            self._app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
            self._app.router.add_post("/v1/responses", self._handle_responses)
            self._app.router.add_get("/v1/responses/{response_id}", self._handle_get_response)
            self._app.router.add_delete("/v1/responses/{response_id}", self._handle_delete_response)
            # Cron jobs management API
            self._app.router.add_get("/api/jobs", self._handle_list_jobs)
            self._app.router.add_post("/api/jobs", self._handle_create_job)
            self._app.router.add_get("/api/jobs/{job_id}", self._handle_get_job)
            self._app.router.add_patch("/api/jobs/{job_id}", self._handle_update_job)
            self._app.router.add_delete("/api/jobs/{job_id}", self._handle_delete_job)
            self._app.router.add_post("/api/jobs/{job_id}/pause", self._handle_pause_job)
            self._app.router.add_post("/api/jobs/{job_id}/resume", self._handle_resume_job)
            self._app.router.add_post("/api/jobs/{job_id}/run", self._handle_run_job)
            self._app.router.add_get("/api/sessions", self._handle_list_sessions)
            self._app.router.add_post("/api/sessions", self._handle_create_session)
            self._app.router.add_get("/api/sessions/search", self._handle_search_sessions)
            self._app.router.add_get("/api/sessions/{session_id}", self._handle_get_session)
            self._app.router.add_get("/api/sessions/{session_id}/messages", self._handle_get_session_messages)
            self._app.router.add_patch("/api/sessions/{session_id}", self._handle_update_session)
            self._app.router.add_delete("/api/sessions/{session_id}", self._handle_delete_session)
            self._app.router.add_post("/api/sessions/{session_id}/fork", self._handle_fork_session)
            self._app.router.add_post("/api/sessions/{session_id}/chat", self._handle_session_chat)
            self._app.router.add_post("/api/sessions/{session_id}/chat/stream", self._handle_session_chat_stream)
            self._app.router.add_get("/api/memory", self._handle_get_memory)
            self._app.router.add_post("/api/memory", self._handle_add_memory)
            self._app.router.add_patch("/api/memory", self._handle_replace_memory)
            self._app.router.add_delete("/api/memory", self._handle_delete_memory)
            self._app.router.add_get("/api/skills", self._handle_list_skills)
            self._app.router.add_get("/api/skills/categories", self._handle_skill_categories)
            self._app.router.add_get("/api/skills/{name}", self._handle_view_skill)
            self._app.router.add_get("/api/config", self._handle_get_config)
            self._app.router.add_patch("/api/config", self._handle_update_config)
            self._app.router.add_get("/api/available-models", self._handle_available_models)

            # Port conflict detection — fail fast if port is already in use
            import socket as _socket
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                    _s.settimeout(1)
                    _s.connect(('127.0.0.1', self._port))
                logger.error('[%s] Port %d already in use. Set a different port in config.yaml: platforms.api_server.port', self.name, self._port)
                return False
            except (ConnectionRefusedError, OSError):
                pass  # port is free

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            self._mark_connected()
            logger.info(
                "[%s] API server listening on http://%s:%d",
                self.name, self._host, self._port,
            )
            return True

        except Exception as e:
            logger.error("[%s] Failed to start API server: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        """Stop the aiohttp web server."""
        self._mark_disconnected()
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        if self._session_db is not None:
            self._session_db.close()
            self._session_db = None
        self._memory_store = None
        logger.info("[%s] API server stopped", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Not used — HTTP request/response cycle handles delivery directly.
        """
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the API server."""
        return {
            "name": "API Server",
            "type": "api",
            "host": self._host,
            "port": self._port,
        }
