# Hermes Agent — Codebase Audit Report

**Date:** 2025-07-14  
**Auditor:** Aurora (Claude Code sub-agent)  
**Scope:** Full codebase audit — code quality, performance, security, architecture, feature gaps vs upstream  
**Branch:** `main` (5 commits ahead of `origin/main` / NousResearch upstream)

---

## Executive Summary

The codebase is generally well-structured and actively maintained. The fork adds a FastAPI WebAPI layer not present in upstream. No hardcoded secrets were found in application code. The most significant concerns are:

1. **🔴 CRITICAL: WebAPI has zero authentication** — all endpoints are unauthenticated
2. **🟠 HIGH: `time.sleep()` called inside async context** in `gateway/run.py` (two locations)
3. **🟠 HIGH: `lru_cache(maxsize=1)` on `SessionDB`/`MemoryStore`** — state bleeds across WebAPI restarts/multi-tenant use
4. **🟡 MEDIUM: God files** — `run_agent.py` (6920 lines), `gateway/run.py` (5155 lines), `cli.py` (7097 lines)
5. **🟡 MEDIUM: CORS only allows `localhost:3002`** — production deployments will silently break

---

## 1. Security

### 🔴 CRITICAL — WebAPI Has No Authentication

`webapi/` has **no authentication middleware whatsoever**. A search for `HTTPBearer`, `APIKeyHeader`, `OAuth2`, `authenticate`, `authorization` across all WebAPI files returns zero results.

Any process that can reach the host's loopback address (or any proxied port) gets full unrestricted access to:
- `/api/sessions/*` — create, read, delete sessions
- `/api/sessions/{id}/chat` — execute arbitrary agent queries (terminal, file write, web, etc.)
- `/api/memory/*` — read and overwrite all memory
- `/api/config/*` — read and modify agent configuration (model, API keys)
- `/api/skills/*` — install/modify agent skills

**Impact:** If this API is ever exposed beyond `127.0.0.1` (e.g., via a reverse proxy, Docker port binding, or ngrok tunnel), there is no barrier to full agent compromise.

**Recommendation:** Add a shared-secret header check (`X-API-Key`) as a minimum. FastAPI makes this trivial with a dependency.

---

### 🟡 MEDIUM — CORS Only Hardcoded to `localhost:3002`

```python
# webapi/app.py:22-24
allow_origins=[
    "http://localhost:3002",
    "http://127.0.0.1:3002",
],
```

CORS is hardcoded. Any production or alternate frontend URL (different port, HTTPS, deployed domain) will be blocked by browsers. There's no env var override (`HERMES_WEBAPI_ALLOWED_ORIGINS`).

**Recommendation:** Read allowed origins from env/config, e.g.:
```python
origins = os.getenv("HERMES_WEBAPI_CORS_ORIGINS", "http://localhost:3002").split(",")
```

---

### 🟢 No Hardcoded Secrets Found

No API keys, tokens, or secrets are hardcoded in application code. Auth is correctly loaded from `~/.hermes/.env` / `~/.hermes/auth.json`. The `skills_guard.py` pattern scanner for `hardcoded_secret` is a good proactive control.

---

### 🟢 Path Traversal — Mitigated in `file_tools.py`

`file_tools.py:175` uses `Path(path).expanduser().resolve()` and checks against the hermes home boundary. This is correct.

---

### 🟢 No Dangerous `eval()`/`exec()` in Application Code

`eval()`/`exec()` references found are only in `skills_guard.py` (as pattern strings for detecting dangerous code in user-provided skills) — correct usage.

---

## 2. Performance

### 🟠 HIGH — Blocking `time.sleep()` Inside Async Code (gateway/run.py)

Two locations in `gateway/run.py` call the synchronous `time.sleep()` directly (lines ~5014 and ~5025), inside what appears to be an async context (a PID replacement/shutdown flow):

```python
import time as _time
_time.sleep(0.5)   # blocking — freezes the event loop
```

This blocks the entire asyncio event loop for 0.5s each time, preventing message delivery and other gateway operations during gateway restart/replacement.

**Recommendation:** Replace with `await asyncio.sleep(0.5)` or move the blocking logic to `asyncio.to_thread()`.

---

### 🟡 MEDIUM — `lru_cache(maxsize=1)` on Singleton Dependencies

```python
# webapi/deps.py:28-35
@lru_cache(maxsize=1)
def get_session_db() -> SessionDB:
    return SessionDB()

@lru_cache(maxsize=1)
def get_memory_store() -> MemoryStore:
    store = MemoryStore()
    store.load_from_disk()
    return store
```

Using `lru_cache` here means:
- `get_memory_store()` loads memory from disk **once at startup only**. Any changes to disk memory files (e.g. from the CLI or gateway) are **never reflected** in the WebAPI process unless it's restarted.
- There's a `reload_memory_store()` workaround that calls `store.load_from_disk()` — but `get_session_db()` has no reload path at all.
- If the WebAPI is used in multi-worker mode (multiple Uvicorn workers), each worker gets its own cache — SQLite session state won't be consistent across workers.

**Recommendation:** Replace `lru_cache` with module-level singletons initialized at startup, or use FastAPI's `lifespan` context to manage state. Add a reload mechanism for `SessionDB`.

---

### 🟡 MEDIUM — Image Fallback Cache (`_anthropic_image_fallback_cache`) Grows Without Bound

In `run_agent.py`, `_anthropic_image_fallback_cache` is a plain `dict` instance on the `AIAgent` object with no eviction policy:

```python
self._anthropic_image_fallback_cache = {}
```

If an agent processes many image-bearing messages (especially in gateway's persistent sessions), this cache grows indefinitely for the session lifetime.

**Recommendation:** Use `functools.lru_cache` or a `cachetools.LRUCache` with a reasonable maxsize (e.g. 50).

---

### 🟡 MEDIUM — WebAPI Chat Runs Synchronously via `run_in_threadpool`

The `/chat` (non-streaming) route delegates to `run_in_threadpool`, which is correct for CPU-bound code. However, `agent.run_conversation()` contains many `time.sleep()` retry loops that block the thread pool. Under load (multiple concurrent requests), these can exhaust Uvicorn's default thread pool (40 threads).

**Recommendation:** Consider a dedicated executor or note the concurrent request limit in documentation.

---

## 3. Code Quality

### 🟠 HIGH — God Files

| File | Lines | Issue |
|---|---|---|
| `cli.py` | 7,097 | The CLI entry point mixes UI rendering, agent dispatch, config management, and command parsing |
| `run_agent.py` | 6,920 | `AIAgent` class contains the full agent loop, streaming, tool dispatch, compression, fallback, Honcho sync, trajectory saving — everything |
| `gateway/run.py` | 5,155 | Gateway runner + all platform-specific message formatting + auth + routing |
| `hermes_cli/main.py` | 4,047 | CLI command definitions |

`run_agent.py`'s `AIAgent.run_conversation()` is ~900 lines of a single method. This makes it extremely hard to test, trace errors, or modify individual behaviors (streaming, compression, retries).

**Recommendation:** Extract `run_conversation()` into a pipeline of composable steps: `_prepare_messages()`, `_call_api_with_retries()`, `_handle_tool_calls()`, `_maybe_compress()`. This is a significant refactor but would pay dividends.

---

### 🟡 MEDIUM — Duplicated Tool Dispatch Logic

`_execute_tool_calls_sequential()` and `_execute_tool_calls_concurrent()` in `run_agent.py` both contain near-identical:
- Per-tool interrupt checking
- JSON args parsing and validation
- Checkpoint logic (`write_file`, `patch`, `terminal`)
- Tool result truncation (100K chars)
- Budget pressure injection

The `_invoke_tool()` helper was added to address some of this but is only used in the concurrent path. The sequential path has its own inline copies of all the same logic.

**Recommendation:** Unify: have the sequential path call `_invoke_tool()` too, and lift all shared pre/post processing into shared helpers.

---

### 🟡 MEDIUM — Print Statements in Production Code

`run_agent.py` has dozens of bare `print()` calls (not using `logging`) that fire in non-quiet mode. In gateway/WebAPI deployments where `quiet_mode=True`, these are suppressed — but any path that accidentally creates an agent with `quiet_mode=False` will produce console noise in a server process.

**Recommendation:** Replace all `print()` in `run_agent.py` with `self._safe_print()` (already exists) or `logging.info()`. Gate with `if not self.quiet_mode:` at minimum.

---

### 🟡 MEDIUM — Retry Counter State Leaks Across Turns in CLI Mode

Several retry counters are reset at the **start** of `run_conversation()`:
```python
self._invalid_tool_retries = 0
self._invalid_json_retries = 0
self._empty_content_retries = 0
```

But they're **instance variables**, and in CLI mode the same `AIAgent` instance processes multiple conversational turns. If a turn ends mid-retry (e.g. interrupted), the counter is NOT reset for the next turn — causing the agent to exhaust its retry budget on the very first bad response in the subsequent turn.

**Recommendation:** Confirm these are reset correctly at all turn boundaries (they appear to be reset at `run_conversation()` entry — verify this covers all gateway vs CLI paths).

---

### 🟢 Dead Code — `run_agent.py:main()` Default Model Mismatch

```python
def main(
    ...
    model: str = "anthropic/claude-opus-4.6",  # line ~6773
```

The inline comment says "Defaults to anthropic/claude-sonnet-4.6" but the actual default is `claude-opus-4.6`. Minor doc inconsistency, but Opus is significantly more expensive.

---

## 4. Architecture

### 🟡 MEDIUM — `gateway/run.py` and `webapi/deps.py` Circular Import Risk

`webapi/deps.py` imports from `gateway/run.py`:
```python
from gateway.run import _resolve_model, _resolve_runtime_agent_kwargs
```

...with a `try/except ImportError` fallback. This means the WebAPI has a soft dependency on the gateway module. Any import error in `gateway/run.py` (e.g. a missing optional platform dependency like `discord.py`) silently falls back to the stub — which may produce a different model/config than the user expects.

**Recommendation:** Extract `_resolve_model()` and `_resolve_runtime_agent_kwargs()` to a shared `hermes_cli/config.py` or `agent/config.py` module, removing the gateway dependency from webapi.

---

### 🟡 MEDIUM — Race Condition Risk in Concurrent Chat Requests

The `lru_cache` on `get_session_db()` returns the **same `SessionDB` instance** across all concurrent requests. `SessionDB` uses SQLite, which has its own connection per instance. If `SessionDB` opens a single connection at construction time and reuses it, concurrent writes from multiple simultaneous chat requests could produce `OperationalError: database is locked` or data races.

**Recommendation:** Verify `SessionDB` uses WAL mode and either opens a connection per operation or uses a connection pool.

---

### 🟡 MEDIUM — Auth Lock Uses File Locking — May Fail on Some Filesystems

`hermes_cli/auth.py` uses `fcntl.flock()` for cross-process auth store locking (with `msvcrt` fallback on Windows). On NFS/CIFS mounts or some Docker volume mounts, `flock()` may not provide the expected mutual exclusion.

**Recommendation:** Document this limitation. For cloud deployments with shared storage, consider advisory locking via the SQLite DB or a more portable mechanism.

---

### 🟢 Auth Token Refresh Has Proper Locking

The `_auth_store_lock()` context manager with re-entrant depth tracking (`_auth_lock_holder.depth`) is well-implemented and prevents concurrent refresh races in the common case.

---

## 5. Feature Gaps vs Upstream

### Fork is 5 commits AHEAD of upstream (`origin/main` = NousResearch)

This fork **adds** the WebAPI; it does not lag behind upstream.

| Commit | Description |
|---|---|
| `10484330` | fix: webapi package discovery + entry point, auto port detection, codex auth auto-sync |
| `0c4d6343` | Merge upstream |
| `14ed6909` | fix: webapi deps — fallback for missing gateway._resolve_model import |
| `d09e2dad` | Merge upstream |
| `c941ae7f` | feat: Hermes Web API — FastAPI backend (sessions, SSE chat streaming, memory, skills, config) |

The fork's WebAPI feature is the primary addition. No features appear to have been removed from upstream.

**Note:** This fork is current as of the last merge. Upstream NousResearch continues active development — the fork should periodically pull from origin to stay current with bug fixes and new provider support.

---

## 6. Summary Table

| Severity | Area | Issue |
|---|---|---|
| 🔴 CRITICAL | Security | WebAPI has **no authentication** |
| 🟠 HIGH | Performance | `time.sleep()` inside async context (gateway/run.py) |
| 🟠 HIGH | Architecture | `lru_cache` singletons on SessionDB/MemoryStore |
| 🟡 MEDIUM | Security | CORS hardcoded to localhost:3002 |
| 🟡 MEDIUM | Performance | `_anthropic_image_fallback_cache` grows without bound |
| 🟡 MEDIUM | Performance | Thread pool exhaustion risk under concurrent WebAPI load |
| 🟡 MEDIUM | Code Quality | God files (run_agent.py 6920L, cli.py 7097L, gateway/run.py 5155L) |
| 🟡 MEDIUM | Code Quality | Duplicated tool dispatch logic (sequential vs concurrent paths) |
| 🟡 MEDIUM | Code Quality | Print statements in production agent loop |
| 🟡 MEDIUM | Architecture | Circular import: webapi → gateway |
| 🟡 MEDIUM | Architecture | Possible SQLite race with shared SessionDB under concurrent requests |
| 🟢 LOW | Code Quality | `main()` default model comment mismatch (Opus vs Sonnet) |
| 🟢 INFO | Security | No hardcoded secrets found |
| 🟢 INFO | Feature Gaps | Fork is ahead of upstream, no features removed |

---

*Report generated via static analysis and manual code review. No code was modified.*
