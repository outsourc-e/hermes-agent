#!/usr/bin/env python3
"""
Hermes Mobile API Server
========================
Lightweight HTTP/WebSocket server that wraps the AIAgent for the mobile app.
Runs inside Termux on Android, listening on localhost:18923.

Endpoints:
  GET  /api/health          — Health check (model, mode, capabilities)
  POST /api/chat            — Send a message (returns full response)
  WS   /ws/chat             — WebSocket for streaming responses
  GET  /api/local/discover  — Auto-discover local LLM servers
  POST /api/local/configure — Configure local LLM mode
  GET  /api/sessions/search — Search past conversations

Usage:
  python api_server.py [--port 18923] [--host 127.0.0.1]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add hermes-agent to path
HERMES_DIR = Path(__file__).parent
if str(HERMES_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hermes-mobile")

# ---------------------------------------------------------------------------
# Lazy imports — hermes-agent modules are heavy, import on first use
# ---------------------------------------------------------------------------

_agent = None
_agent_lock = None


def _save_session_turn(user_msg: str, assistant_msg: str):
    """Save a conversation turn to session history for future search."""
    try:
        sessions_dir = Path.home() / ".hermes" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        session_file = sessions_dir / f"{today}.jsonl"
        entry = json.dumps({
            "ts": datetime.now().isoformat(),
            "user": user_msg[:500],
            "assistant": assistant_msg[:500],
        })
        with open(session_file, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass  # Non-critical, don't break the chat


def get_or_create_agent(session_id: str = "mobile"):
    """Get or create a singleton AIAgent instance."""
    global _agent, _agent_lock
    if _agent_lock is None:
        import threading
        _agent_lock = threading.Lock()

    with _agent_lock:
        if _agent is None:
            try:
                from run_agent import AIAgent
                _agent = AIAgent(
                    model="xiaomi/mimo-v2-pro",
                    provider="nous",
                    platform="mobile",
                    session_id=session_id,
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=False,
                    max_iterations=30,
                )
                log.info("AIAgent initialized (model=xiaomi/mimo-v2-pro, provider=nous)")
            except Exception as e:
                log.error(f"Failed to init AIAgent: {e}")
                raise
    return _agent


# ---------------------------------------------------------------------------
# FastAPI Server
# ---------------------------------------------------------------------------

def create_app():
    """Create the FastAPI app."""
    try:
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from pydantic import BaseModel
    except ImportError:
        log.error("fastapi/uvicorn not installed. Run: pip install fastapi uvicorn websockets")
        sys.exit(1)

    app = FastAPI(
        title="Hermes Mobile API",
        version="1.0.0",
        docs_url=None,  # Disable docs in production
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Models ──────────────────────────────────────────────────────────

    class ChatRequest(BaseModel):
        message: str
        history: Optional[List[Dict[str, str]]] = None
        session_id: Optional[str] = None

    class ChatResponse(BaseModel):
        response: str
        session_id: str
        timestamp: str

    # ── Health ──────────────────────────────────────────────────────────

    @app.get("/api/health")
    async def health():
        agent = _agent
        model_info = {}
        if agent:
            model_info = {
                "model": getattr(agent, "model", "unknown"),
                "provider": getattr(agent, "provider", "unknown"),
            }
        return {
            "status": "ok",
            "service": "hermes-mobile",
            "version": "1.1.0",
            "timestamp": datetime.utcnow().isoformat(),
            "agent_initialized": agent is not None,
            **model_info,
            "has_api_key": bool(
                os.environ.get("NOUS_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or os.environ.get("LOCAL_LLM_URL")
            ),
            "mode": "local" if os.environ.get("LOCAL_LLM_URL") else "cloud",
        }

    # ── Local LLM Discovery ─────────────────────────────────────────────

    @app.get("/api/local/discover")
    async def discover_local_models():
        """Auto-discover local LLM servers (PocketPal, Ollama, LM Studio)."""
        import urllib.request

        servers = []
        endpoints = [
            {"name": "PocketPal", "url": "http://127.0.0.1:8080/v1"},
            {"name": "Ollama", "url": "http://127.0.0.1:11434/v1"},
            {"name": "LM Studio", "url": "http://127.0.0.1:1234/v1"},
            {"name": "jan", "url": "http://127.0.0.1:1337/v1"},
        ]
        for ep in endpoints:
            try:
                req = urllib.request.Request(
                    f"{ep['url']}/models",
                    headers={"Authorization": "Bearer not-needed"},
                )
                resp = urllib.request.urlopen(req, timeout=2)
                if resp.status == 200:
                    data = json.loads(resp.read())
                    models = [m.get("id", "?") for m in data.get("data", [])]
                    servers.append({
                        "name": ep["name"],
                        "url": ep["url"],
                        "available": True,
                        "models": models,
                    })
            except Exception:
                pass
        return {"servers": servers}

    # ── Local LLM Configuration ─────────────────────────────────────────

    @app.post("/api/local/configure")
    async def configure_local(body: dict):
        """Configure local LLM mode. Send empty url to disable."""
        url = body.get("url", "")
        model = body.get("model", "local")
        key = body.get("api_key", "not-needed")

        if not url:
            os.environ.pop("LOCAL_LLM_URL", None)
            os.environ.pop("LOCAL_LLM_MODEL", None)
            return {"status": "ok", "mode": "cloud"}

        os.environ["LOCAL_LLM_URL"] = url
        os.environ["LOCAL_LLM_MODEL"] = model
        os.environ["LOCAL_LLM_KEY"] = key

        # Persist to .env file
        env_path = Path.home() / ".hermes" / ".env"
        if env_path.exists():
            lines = env_path.read_text().splitlines()
            lines = [l for l in lines if not l.startswith("LOCAL_LLM_")]
        else:
            lines = []
            env_path.parent.mkdir(parents=True, exist_ok=True)

        lines.extend([
            f"LOCAL_LLM_URL={url}",
            f"LOCAL_LLM_MODEL={model}",
            f"LOCAL_LLM_KEY={key}",
        ])
        env_path.write_text("\n".join(lines) + "\n")

        return {"status": "ok", "mode": "local", "url": url, "model": model}

    # ── Session Search ──────────────────────────────────────────────────

    @app.get("/api/sessions/search")
    async def search_sessions(q: str = "", limit: int = 3):
        """Search past conversation sessions by keyword."""
        if not q:
            raise HTTPException(400, "Missing query parameter 'q'")

        sessions_dir = Path.home() / ".hermes" / "sessions"
        if not sessions_dir.exists():
            return {"results": [], "query": q}

        results = []
        keywords = [k.strip() for k in q.lower().split(" OR ")] if " OR " in q else [q.lower()]

        for f in sorted(sessions_dir.glob("*.jsonl"), reverse=True):
            if len(results) >= limit * 3:
                break
            try:
                for line in f.read_text().strip().split("\n"):
                    if not line:
                        continue
                    entry = json.loads(line)
                    text = (entry.get("user", "") + " " + entry.get("assistant", "")).lower()
                    if any(kw in text for kw in keywords):
                        results.append({
                            "date": f.stem,
                            "timestamp": entry.get("ts", "")[:19],
                            "user": entry.get("user", "")[:200],
                            "assistant": entry.get("assistant", "")[:200],
                        })
            except Exception:
                continue

        return {"results": results[:limit], "query": q, "total": len(results)}

    # ── Chat (HTTP POST) ───────────────────────────────────────────────

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        """Send a message and get a full response."""
        try:
            session_id = req.session_id or "mobile"
            agent = get_or_create_agent(session_id)

            # Build conversation history if provided
            history = None
            if req.history:
                history = [
                    {"role": m["role"], "content": m["content"]}
                    for m in req.history
                ]

            result = agent.run_conversation(
                user_message=req.message,
                conversation_history=history,
            )

            response_text = result.get("final_response", "")

            # Save to session history for future search
            _save_session_turn(req.message, response_text)

            return ChatResponse(
                response=response_text,
                session_id=session_id,
                timestamp=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            log.error(f"Chat error: {e}\n{traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Chat (WebSocket streaming) ─────────────────────────────────────

    @app.websocket("/ws/chat")
    async def websocket_chat(ws: WebSocket):
        """WebSocket endpoint for streaming chat responses."""
        await ws.accept()
        log.info("WebSocket connected")

        session_id = "mobile"

        try:
            while True:
                data = await ws.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    # Plain text message
                    msg = {"type": "chat", "message": data}

                msg_type = msg.get("type", "chat")

                if msg_type == "chat":
                    user_message = msg.get("message", "")
                    if not user_message.strip():
                        continue

                    session_id = msg.get("session_id", session_id)
                    history = msg.get("history")

                    # Send status
                    await ws.send_json({
                        "type": "status",
                        "content": "thinking",
                    })

                    try:
                        agent = get_or_create_agent(session_id)

                        # Set up streaming callback
                        def stream_delta(delta: str):
                            """Called for each token during streaming."""
                            asyncio.get_event_loop().call_soon_threadsafe(
                                lambda: asyncio.ensure_future(
                                    _safe_send(ws, {
                                        "type": "assistant",
                                        "content": delta,
                                        "streaming": True,
                                    })
                                )
                            )

                        # Set up tool callback
                        def tool_start(tool_name: str, args_preview: str):
                            asyncio.get_event_loop().call_soon_threadsafe(
                                lambda: asyncio.ensure_future(
                                    _safe_send(ws, {
                                        "type": "tool_call",
                                        "tool_name": tool_name,
                                        "content": args_preview,
                                        "status": "running",
                                    })
                                )
                            )

                        def tool_complete(tool_name: str, result_preview: str):
                            asyncio.get_event_loop().call_soon_threadsafe(
                                lambda: asyncio.ensure_future(
                                    _safe_send(ws, {
                                        "type": "tool_result",
                                        "tool_name": tool_name,
                                        "content": result_preview,
                                        "status": "completed",
                                    })
                                )
                            )

                        # Run agent in thread pool (it's synchronous)
                        conv_history = None
                        if history:
                            conv_history = [
                                {"role": m["role"], "content": m["content"]}
                                for m in history
                            ]

                        loop = asyncio.get_event_loop()
                        result = await loop.run_in_executor(
                            None,
                            lambda: agent.run_conversation(
                                user_message=user_message,
                                conversation_history=conv_history,
                            ),
                        )

                        # Send final response
                        final = result.get("final_response", "")

                        # Save to session history
                        _save_session_turn(user_message, final)

                        await ws.send_json({
                            "type": "assistant",
                            "content": final,
                            "streaming": False,
                        })

                        # Send done status
                        await ws.send_json({
                            "type": "status",
                            "content": "done",
                        })

                    except Exception as e:
                        log.error(f"Agent error: {e}\n{traceback.format_exc()}")
                        await ws.send_json({
                            "type": "error",
                            "content": str(e),
                        })

                elif msg_type == "ping":
                    await ws.send_json({"type": "pong"})

                elif msg_type == "reset":
                    _agent = None
                    session_id = msg.get("session_id", "mobile")
                    await ws.send_json({
                        "type": "status",
                        "content": "reset",
                    })

        except WebSocketDisconnect:
            log.info("WebSocket disconnected")
        except Exception as e:
            log.error(f"WebSocket error: {e}")

    return app


async def _safe_send(ws, data: dict):
    """Send JSON to WebSocket, ignoring errors if disconnected."""
    try:
        await ws.send_json(data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Hermes Mobile API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=18923, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload (dev)")
    args = parser.parse_args()

    log.info(f"Starting Hermes Mobile API on {args.host}:{args.port}")

    try:
        import uvicorn
    except ImportError:
        print("Installing uvicorn...")
        os.system(f"{sys.executable} -m pip install uvicorn fastapi websockets pydantic")
        import uvicorn

    app = create_app()

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        reload=args.reload,
        ws_max_size=16 * 1024 * 1024,  # 16MB max WS message
    )


if __name__ == "__main__":
    main()
