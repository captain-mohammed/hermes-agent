"""Subagent orchestration + Bot Mode dashboard routes.

Read-only views over the same durable data the agent runtime already writes:

- ``async_delegations`` mirror rows in ``state.db`` (cross-process safe) for the
  Subagent Orchestration Panel, joined with the child subagent sessions
  (``parent_session_id``) whose transcripts are the reason→tool→reason record.
- ``cache/delegation/live/<id>`` batch live transcripts when present.
- Bot Mode roster via :mod:`tools.bot_mode_probe` and per-profile ``Bot Chat``
  sessions (the durable bot-to-bot conversation log) via :mod:`hermes_state`.
- Sending a dashboard message into a bot's Bot Chat reuses the exact turn
  command Bot Mode itself uses (``BOT_CHAT_TURN_ARGS``), so the dashboard acts
  as one more teammate messaging the roster.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import spawn_profile_action, scoped_to_thread

_log = logging.getLogger("hermes_cli.web_server")
router = APIRouter(prefix="/api/agents")

_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")
_cron_profile_home = late("_cron_profile_home", "hermes_cli.web_server_cron")
get_hermes_home = late("get_hermes_home", "hermes_cli.config")

_DELEGATION_STATES = {"running", "completed", "failed", "interrupted", "stalling", "stalled", "unknown"}
_TRANSCRIPT_TAIL_LINES = 400


class BotModeSend(BaseModel):
    profile: str = Field(..., min_length=1, description="Target bot profile name (default → 'hermes').")
    message: str = Field(..., min_length=1, max_length=32_000)


# ── helpers ──────────────────────────────────────────────────────────────────


def _delegation_db_path() -> Path:
    return Path(get_hermes_home()) / "state.db"


def _read_delegation_rows(limit: int) -> List[Dict[str, Any]]:
    """Durable async_delegations mirror rows, newest first. Empty when the
    table does not exist yet (no delegation ever ran on this install)."""
    db_path = _delegation_db_path()
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='async_delegations'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute(
                """SELECT delegation_id, origin_session, origin_ui_session_id, parent_session_id,
                          state, dispatched_at, completed_at, updated_at, event_json, result_json,
                          task_json, delivery_state, origin_session_id
                   FROM async_delegations ORDER BY dispatched_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _log.warning("GET /api/agents/delegations mirror read failed: %s", exc)
        return []


def _in_memory_delegations() -> List[Dict[str, Any]]:
    """Live in-process delegations when the gateway runs inside THIS process
    (dev mode). Always empty in the split-process deployment — harmless."""
    try:
        from tools.async_delegation import list_async_delegations

        return [d for d in (list_async_delegations() or []) if isinstance(d, dict)]
    except Exception:
        return []


def _live_manifest(delegation_id: str) -> Optional[Dict[str, Any]]:
    """Batch live-transcript manifest for a delegation id, when one exists."""
    try:
        from tools.delegation_live_log import _manifest_path

        path = _manifest_path(delegation_id)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _live_log_tail(delegation_id: str, task_index: int, lines: int = _TRANSCRIPT_TAIL_LINES) -> List[str]:
    try:
        from tools.delegation_live_log import live_transcript_root

        path = live_transcript_root() / delegation_id / f"task-{task_index}.log"
        if not path.is_file():
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in fh.readlines()[-lines:]]
    except Exception:
        return []


def _child_sessions_for(db, parent_session_id: str, dispatched_at: float) -> List[Dict[str, Any]]:
    """Subagent child sessions spawned at/after the delegation dispatch."""
    if not parent_session_id:
        return []
    try:
        rows = db._read_all(
            """SELECT id, title, source, model, started_at, ended_at, end_reason,
                      message_count, input_tokens, output_tokens
               FROM sessions
               WHERE parent_session_id = ? AND started_at >= ?
               ORDER BY started_at ASC""",
            (parent_session_id, float(dispatched_at) - 1.0),
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        _log.debug("child session lookup failed for %s: %s", parent_session_id, exc)
        return []


def _session_transcript(db, session_id: str, limit: int) -> List[Dict[str, Any]]:
    try:
        msgs = db.get_messages(session_id, limit=limit, latest=True) or []
    except Exception as exc:
        _log.debug("transcript read failed for %s: %s", session_id, exc)
        return []

    def _content(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for part in value:
                if isinstance(part, dict):
                    parts.append(str(part.get("text") or part.get("content") or ""))
                else:
                    parts.append(str(part))
            return "".join(parts)
        return str(value or "")

    events = []
    for m in msgs:
        role = str(m.get("role") or "")
        content = _content(m.get("content"))
        entry: Dict[str, Any] = {
            "role": role,
            "content": content[:4000],
            "ts": m.get("timestamp"),
        }
        if m.get("tool_name"):
            entry["tool_name"] = m.get("tool_name")
        if m.get("reasoning_content"):
            entry["reasoning"] = str(m.get("reasoning_content"))[:1500]
        events.append(entry)
    return events


def _profile_roster(root: Path) -> List[Dict[str, Any]]:
    try:
        from tools.bot_mode_probe import _handle, _is_bot_managed, _profile_role, _roster

        agents = []
        for name, profile_dir in _roster(root):
            agents.append({
                "profile": name,
                "handle": _handle(name),
                "managed": bool(_is_bot_managed(profile_dir)),
                "role": _profile_role(profile_dir) or "",
                "local": True,
            })
        return agents
    except Exception as exc:
        _log.debug("bot roster read failed: %s", exc)
        return []


def _remote_roster(root: Path) -> List[Dict[str, Any]]:
    try:
        from tools.bot_relay import read_remote_roster

        rows = read_remote_roster(root) or []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append({
                "profile": r.get("profile") or "",
                "handle": r.get("handle") or "",
                "connection_id": r.get("connection_id") or "",
                "connection_label": r.get("connection_label") or "",
                "local": False,
            })
        return out
    except Exception:
        return []


def _bot_chat_sessions(db) -> List[Dict[str, Any]]:
    """All 'Bot Chat' sessions (plus 'Bot Chat #N' continuations), newest first."""
    try:
        rows = db._read_all(
            """SELECT id, title, started_at, ended_at, end_reason, message_count,
                      input_tokens, output_tokens
               FROM sessions
               WHERE title = 'Bot Chat' OR title LIKE 'Bot Chat #%'
               ORDER BY started_at DESC LIMIT 25""",
            [],
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        _log.debug("Bot Chat session lookup failed: %s", exc)
        return []


def _resolve_profile_home(profile: Optional[str]) -> tuple:
    try:
        return _cron_profile_home(profile)
    except Exception:
        return (profile or "default", Path(get_hermes_home()))


# ── subagent orchestration ───────────────────────────────────────────────────


@router.get("/delegations")
async def list_delegations(
    limit: int = Query(50, ge=1, le=200),
    profile: Optional[str] = None,
):
    """Durable subagent delegations (newest first) enriched with live manifests
    and the child subagent session ids the panel can open transcripts for."""

    def _work() -> Dict[str, Any]:
        rows = _read_delegation_rows(limit)
        by_id: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item: Dict[str, Any] = {
                "delegation_id": row.get("delegation_id"),
                "origin_session": row.get("origin_session") or "",
                "parent_session_id": row.get("parent_session_id") or "",
                "state": row.get("state") or "unknown",
                "dispatched_at": row.get("dispatched_at"),
                "completed_at": row.get("completed_at"),
                "delivery_state": row.get("delivery_state") or "",
            }
            for key in ("task_json", "event_json", "result_json"):
                raw = row.get(key)
                if raw:
                    try:
                        item[key.removesuffix("_json")] = json.loads(raw)
                    except (TypeError, ValueError):
                        pass
            item.setdefault("task", {})
            item.setdefault("event", {})
            item.setdefault("result", {})
            manifest = _live_manifest(str(item["delegation_id"]))
            if manifest:
                item["live_manifest"] = manifest
            by_id[str(item["delegation_id"])] = item

        # Merge live in-process records (same-process gateway / dev mode).
        for live in _in_memory_delegations():
            did = str(live.get("delegation_id") or "")
            if not did:
                continue
            item = by_id.get(did)
            if item is None:
                item = {
                    "delegation_id": did,
                    "origin_session": str(live.get("session_key") or ""),
                    "parent_session_id": str(live.get("parent_session_id") or ""),
                    "state": str(live.get("status") or "unknown"),
                    "dispatched_at": live.get("dispatched_at"),
                    "completed_at": live.get("completed_at"),
                    "delivery_state": "",
                    "task": {}, "event": {}, "result": {},
                }
                by_id[did] = item
            for key in ("status", "goal", "model", "role", "seconds_since_progress", "in_tool", "children_activity"):
                if live.get(key) is not None:
                    item[key] = live[key]

        items = list(by_id.values())
        # Enrich with child subagent sessions (bounded work).
        if items:
            try:
                db = _open_session_db_for_profile(profile, read_only=True)
                try:
                    for item in items:
                        item["children"] = _child_sessions_for(
                            db, str(item.get("parent_session_id") or ""),
                            float(item.get("dispatched_at") or 0.0))
                finally:
                    db.close()
            except Exception as exc:
                _log.debug("delegation child enrichment failed: %s", exc)
                for item in items:
                    item["children"] = []
        else:
            for item in items:
                item["children"] = []

        items.sort(key=lambda x: float(x.get("dispatched_at") or 0.0), reverse=True)
        running = sum(1 for i in items if i.get("state") in ("running", "stalling"))
        return {"delegations": items, "total": len(items), "running": running}

    return await scoped_to_thread(profile, _work)


@router.get("/delegations/{delegation_id}/transcript")
async def delegation_transcript(
    delegation_id: str,
    session: Optional[str] = None,
    limit: int = Query(120, ge=1, le=500),
    profile: Optional[str] = None,
):
    """Reason→tool→reason transcript for one delegation: the live batch
    transcript (when present) and/or the child subagent session messages."""

    def _work() -> Dict[str, Any]:
        rows = [r for r in _read_delegation_rows(200) if str(r.get("delegation_id")) == delegation_id]
        row = rows[0] if rows else None
        manifest = _live_manifest(delegation_id)

        children: List[Dict[str, Any]] = []
        transcripts: Dict[str, List[Dict[str, Any]]] = {}
        if row or session:
            parent = str((row or {}).get("parent_session_id") or "")
            try:
                db = _open_session_db_for_profile(profile, read_only=True)
                try:
                    if session:
                        detail = db._read_all(
                            "SELECT id, title, source, model, started_at, ended_at FROM sessions WHERE id = ?",
                            (session,))
                        children = [dict(r) for r in detail]
                    else:
                        children = _child_sessions_for(db, parent, float((row or {}).get("dispatched_at") or 0.0))
                    for child in children[-6:]:
                        transcripts[str(child["id"])] = _session_transcript(db, str(child["id"]), limit)
                finally:
                    db.close()
            except Exception as exc:
                _log.debug("delegation transcript read failed: %s", exc)

        live_tasks = []
        if manifest:
            for task in manifest.get("tasks", []):
                idx = int(task.get("index") or 0)
                live_tasks.append({
                    "index": idx,
                    "goal": task.get("goal") or "",
                    "status": task.get("status") or "running",
                    "log_tail": _live_log_tail(delegation_id, idx),
                })

        if row is None and manifest is None and not children:
            raise HTTPException(status_code=404, detail="Delegation not found")

        return {
            "delegation_id": delegation_id,
            "state": (row or {}).get("state") or (manifest and "running") or "unknown",
            "dispatched_at": (row or {}).get("dispatched_at"),
            "completed_at": (row or {}).get("completed_at"),
            "task": json.loads((row or {}).get("task_json") or "{}") if row and (row or {}).get("task_json") else {},
            "children": children,
            "transcripts": transcripts,
            "live_tasks": live_tasks,
        }

    return await scoped_to_thread(profile, _work)


# ── bot mode ─────────────────────────────────────────────────────────────────


@router.get("/bot-mode")
async def bot_mode_roster(profile: Optional[str] = None):
    """Bot Mode status: local bot roster (profiles), remote relay roster, and
    whether this install is Bot-Mode-managed."""

    def _work() -> Dict[str, Any]:
        root = Path(get_hermes_home())
        local = _profile_roster(root)
        remote = _remote_roster(root)
        managed = any(a.get("managed") for a in local) or bool(remote)
        # Conversation counts per local agent come cheap from each profile db.
        for agent in local:
            count = 0
            latest: Optional[Dict[str, Any]] = None
            try:
                db = _open_session_db_for_profile(agent["profile"] if agent["profile"] != "default" else None,
                                                  read_only=True)
                try:
                    sessions = _bot_chat_sessions(db)
                    count = len(sessions)
                    latest = sessions[0] if sessions else None
                finally:
                    db.close()
            except Exception:
                pass
            agent["bot_chat_sessions"] = count
            agent["latest_activity"] = (latest or {}).get("ended_at") or (latest or {}).get("started_at")
        return {"managed": managed, "agents": local, "remote_agents": remote}

    return await scoped_to_thread(profile, _work)


@router.get("/bot-mode/conversations")
async def bot_mode_conversations(
    profile: str = Query("default"),
    session: Optional[str] = None,
    limit: int = Query(80, ge=1, le=400),
):
    """The bot-to-bot conversation record for one profile: its 'Bot Chat'
    sessions and the merged messages (peer DMs + the bot's replies)."""

    def _work() -> Dict[str, Any]:
        db = _open_session_db_for_profile(None if profile == "default" else profile, read_only=True)
        try:
            sessions = _bot_chat_sessions(db)
            target = session or (sessions[0]["id"] if sessions else None)
            messages = _session_transcript(db, target, limit) if target else []
            return {"profile": profile, "sessions": sessions, "session_id": target, "messages": messages}
        finally:
            db.close()

    return await scoped_to_thread(profile, _work)


@router.post("/bot-mode/send")
async def bot_mode_send(body: BotModeSend, profile: Optional[str] = None):
    """Message a bot from the dashboard: runs the same Bot Chat turn command
    Bot Mode uses (``hermes -p <profile> chat --in ~ -c 'Bot Chat' …``) with the
    message on a query file, so the reply lands in that bot's durable record."""
    _profile_home_name, _home = _resolve_profile_home(body.profile)
    content = f"Message from 🤖 dashboard (@dashboard): {body.message.strip()}"
    fd, query_path = tempfile.mkstemp(prefix="stash-dm-", suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(query_path)
        except OSError:
            pass
        raise

    argv = ["chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing", "-Q",
            "--query-file", query_path]
    try:
        return spawn_profile_action(
            body.profile, argv, "bot-mode-send",
            log_msg="Bot Mode dashboard send to %s" % body.profile,
            prefix="Bot Mode send",
        )
    finally:
        # The spawned CLI reads the file at startup; unlinking on Windows while
        # open elsewhere fails silently — schedule a best-effort delayed remove.
        def _cleanup() -> None:
            time.sleep(10)
            try:
                os.unlink(query_path)
            except OSError:
                pass

        import threading

        threading.Thread(target=_cleanup, name="bot-mode-send-cleanup", daemon=True).start()
