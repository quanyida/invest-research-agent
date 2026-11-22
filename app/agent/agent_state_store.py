from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text

from agentscope.state import AgentState

from app.agent.verify import strip_citations
from app.config import Settings
from app.db import tenant_scope

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

class AgentStateTooLarge(RuntimeError):
    def __init__(self, size_bytes: int) -> None:
        super().__init__(f"agent state too large: {size_bytes} bytes")
        self.size_bytes = size_bytes

TRUNCATED_MARKER = "Some earlier messages were truncated"

def compact_agent_state(state: AgentState) -> int:
    """Keep only conversational text in the persisted AgentState.

    Tool observations are valid for the turn that fetched them and are already
    stored in the evidence table. Keeping raw tool payloads in the saved state
    makes the next turn hit the context compression threshold, which can drop
    the previous answer and break follow-up questions such as "上面的...".
    """
    released = 0
    kept_messages: list[Any] = []
    for msg in state.context:
        kept_blocks = []
        for block in msg.content:
            if block.type in {"tool_call", "tool_result", "thinking", "hint"}:
                released += 1
                continue
            if block.type == "text" and msg.role == "assistant":
                cleaned = strip_citations(getattr(block, "text", ""))
                if cleaned != getattr(block, "text", ""):
                    block.text = cleaned
                if not cleaned.strip():
                    released += 1
                    continue
            kept_blocks.append(block)
        if not kept_blocks:
            continue
        msg.content = kept_blocks
        kept_messages.append(msg)
    state.context = kept_messages
    summary = state.summary
    if isinstance(summary, str):
        state.summary = strip_citations(summary)
    elif isinstance(summary, list):
        for block in summary:
            if getattr(block, "type", "") == "text":
                block.text = strip_citations(getattr(block, "text", ""))
    return released

def state_was_truncated(state: AgentState) -> bool:
    """Whether the saved state carries AgentScope's fallback truncation marker."""
    summary = state.summary
    if isinstance(summary, str):
        text = summary
    else:
        text = " ".join(
            block.text
            for block in summary
            if getattr(block, "type", "") == "text"
        )
    return TRUNCATED_MARKER in text

class AgentStateStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.max_bytes = max(16, int(settings.agent_state_max_kb)) * 1024

    def load(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> tuple[AgentState | None, int]:
        try:
            with tenant_scope(tenant_id, user_id) as db:
                row = db.execute(
                    text(
                        "select agent_state, agent_state_version "
                        "from conversations where id = :id"
                    ),
                    {"id": str(conversation_id)},
                ).mappings().first()
        except Exception:
            logger.exception("agent state load failed for conversation %s", conversation_id)
            return None, 0
        if not row:
            return None, 0
        version = int(row.get("agent_state_version") or 0)
        payload = row.get("agent_state") or {}
        if not isinstance(payload, dict) or not payload:
            return None, version
        if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
            logger.warning("agent state schema mismatch for conversation %s", conversation_id)
            return None, version
        raw_state = payload.get("state")
        if not isinstance(raw_state, dict) or not raw_state:
            return None, version
        try:
            state = AgentState.model_validate(raw_state)
        except Exception:
            logger.exception("agent state validate failed for conversation %s", conversation_id)
            return None, version
        state.session_id = str(conversation_id)
        return state, version

    def serialize(self, state: AgentState) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "state": state.model_dump(mode="json"),
        }
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if size > self.max_bytes:
            raise AgentStateTooLarge(size)
        return payload

    def save(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        payload: dict[str, Any],
        expected_version: int,
    ) -> bool:
        try:
            with tenant_scope(tenant_id, user_id) as db:
                result = db.execute(
                    text(
                        "update conversations "
                        "set agent_state = cast(:state as jsonb), "
                        "agent_state_version = agent_state_version + 1, "
                        "agent_state_updated_at = now() "
                        "where id = :id and agent_state_version = :version"
                    ),
                    {
                        "state": json.dumps(payload, ensure_ascii=False),
                        "id": str(conversation_id),
                        "version": int(expected_version),
                    },
                )
                return int(result.rowcount or 0) == 1
        except Exception:
            logger.exception("agent state save failed for conversation %s", conversation_id)
            return False
TURNS_KEY = "agent_state_saved_turns"
BASE_TURN_KEY = "agent_state_base_turn"
TRACKING_KEY = "agent_state_turn_tracking"
GAP_KEY = "agent_state_gap_turns"

def ensure_turn_marks(state: AgentState, turn: int) -> None:
    """Initialize turn tracking for states saved before this feature existed.

    Legacy states have no record of which turns were written, so we assume all
    prior turns are covered and only track gaps from this turn onward.
    """
    context = state.middle_context
    if context.get(TRACKING_KEY):
        return
    context[TRACKING_KEY] = True
    context[BASE_TURN_KEY] = max(0, int(turn) - 1)
    context[TURNS_KEY] = []

def reset_turn_marks(state: AgentState, turn: int) -> None:
    """Mark every turn before ``turn`` as covered by a rebuild."""
    context = state.middle_context
    context[TRACKING_KEY] = True
    context[BASE_TURN_KEY] = max(0, int(turn) - 1)
    context[TURNS_KEY] = []
    context[GAP_KEY] = []

def record_saved_turn(state: AgentState, turn: int) -> None:
    context = state.middle_context
    if not context.get(TRACKING_KEY):
        ensure_turn_marks(state, turn)
    saved: set[int] = set()
    for value in context.get(TURNS_KEY) or []:
        try:
            saved.add(int(value))
        except (TypeError, ValueError):
            continue
    saved.add(int(turn))
    context[TURNS_KEY] = sorted(saved)[-200:]
    if BASE_TURN_KEY not in context:
        context[BASE_TURN_KEY] = max(0, int(turn) - 1)

def detect_gap(state: AgentState, turn: int) -> list[int]:
    """Return the turns between the coverage base and this turn that were never saved."""
    context = state.middle_context
    if not context.get(TRACKING_KEY):
        return []
    try:
        base = int(context.get(BASE_TURN_KEY) or 0)
    except (TypeError, ValueError):
        base = 0
    saved: set[int] = set()
    for value in context.get(TURNS_KEY) or []:
        try:
            saved.add(int(value))
        except (TypeError, ValueError):
            continue
    return [item for item in range(base + 1, max(base + 1, int(turn))) if item not in saved]

def mark_gap(state: AgentState, gap_turns: list[int]) -> None:
    state.middle_context[GAP_KEY] = [int(item) for item in gap_turns]