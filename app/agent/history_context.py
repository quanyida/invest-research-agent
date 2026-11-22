from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import case, select

from app.agent.verify import strip_citations
from app.db import tenant_scope
from app.models import Message

SUMMARY_SYSTEM_PROMPT = """把下面的多轮投研对话压缩成要点，供后续追问使用。
每轮输出一行，格式：第N轮：讨论标的/用户口径/关键数字与结论（不超过80字）。
只输出压缩结果本身，不要分析、不要解释、不要输出思考过程。
不得补充、推断或编造数据；不要保留引用编号、链接、媒体名。
最后用一行“跨轮要点：”汇总用户长期关注点。"""

SUMMARY_MAX_CHARS = 24000
SUMMARY_CHUNK_CHARS = 2600
SUMMARY_MAX_CHUNKS = 6
SUMMARY_QUESTION_CHARS = 300
SUMMARY_ANSWER_CHARS = 800
MAX_HISTORY_TURNS = 60
MAX_TRACKED_TURNS = 200

def _clean(text: Any, limit: int) -> str:
    value = strip_citations(str(text or ""))
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        value = value[:limit] + "…"
    return value


def group_history(
    rows: list[dict[str, Any]],
    max_turns: int = MAX_HISTORY_TURNS,
) -> list[dict[str, Any]]:
    """Pair user questions with the following assistant answer."""
    by_turn: dict[int, dict[str, Any]] = {}
    pending = ""
    fallback = 0
    for row in rows:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "")
        if role == "user":
            pending = content
            continue
        if role != "assistant":
            continue
        if str(row.get("status") or "") not in {"done", "degraded"} or not content.strip():
            pending = ""
            continue
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        try:
            turn = int(meta.get("turn"))
        except (TypeError, ValueError):
            fallback += 1
            turn = fallback
        fallback = max(fallback, turn)
        by_turn[turn] = {"turn": turn, "question": pending, "answer": content}
        pending = ""
    turns = [by_turn[key] for key in sorted(by_turn)]
    return turns[-max(1, int(max_turns)):]

def load_history_turns(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    exclude_request_id: uuid.UUID,
    max_turns: int = MAX_HISTORY_TURNS,
) -> list[dict[str, Any]]:
    """Load completed prior turns, excluding the current request."""
    with tenant_scope(tenant_id, user_id) as db:
        rows = db.execute(
            select(
                Message.role,
                Message.content,
                Message.meta,
                Message.status,
                Message.created_at,
            )
            .where(
                Message.conversation_id == conversation_id,
                Message.request_id != exclude_request_id,
            )
            .order_by(
                Message.created_at,
                case((Message.role == "user", 0), else_=1),
            )
        ).mappings().all()
    return group_history([dict(row) for row in rows], max_turns=max_turns)

def turns_from_history(history: list[dict[str, str]] | None) -> list[dict[str, Any]]:
    """Fallback when the database history is unavailable."""
    turns: list[dict[str, Any]] = []
    pending = ""
    for item in history or []:
        role = str(item.get("role") or "")
        content = str(item.get("content") or "")
        if role == "user":
            pending = content
        elif role == "assistant" and content.strip():
            turns.append(
                {
                    "turn": len(turns) + 1,
                    "question": pending,
                    "answer": content,
                }
            )
            pending = ""
    return turns


def split_history(
    turns: list[dict[str, Any]],
    recent_turns: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    size = max(1, int(recent_turns))
    if len(turns) <= size:
        return [], list(turns)
    return list(turns[:-size]), list(turns[-size:])

def render_summary_input(
    turns: list[dict[str, Any]],
    max_chars: int = SUMMARY_MAX_CHARS,
) -> tuple[str, list[int]]:
    """Render the early turns, keeping the newest ones when the budget is tight."""
    blocks: list[str] = []
    included: list[int] = []
    used = 0
    for item in reversed(turns):
        block = (
            f"第{item['turn']}轮\n"
            f"用户：{_clean(item.get('question'), SUMMARY_QUESTION_CHARS)}\n"
            f"助手：{_clean(item.get('answer'), SUMMARY_ANSWER_CHARS)}"
        )
        if blocks and used + len(block) > max_chars:
            break
        blocks.append(block)
        included.append(int(item["turn"]))
        used += len(block) + 2
    blocks.reverse()
    included.reverse()
    return "\n\n".join(blocks), included

def _block_chars(item: dict[str, Any]) -> int:
    return (
        len(f"第{item['turn']}轮\n用户：") 
        + len(_clean(item.get("question"), SUMMARY_QUESTION_CHARS))
        + len("\n助手：")
        + len(_clean(item.get("answer"), SUMMARY_ANSWER_CHARS))
    )


def chunk_turns(
    turns: list[dict[str, Any]],
    max_chars: int = SUMMARY_CHUNK_CHARS,
    max_chunks: int = SUMMARY_MAX_CHUNKS,
) -> list[list[dict[str, Any]]]:
    """Pack turns into model-sized chunks, keeping only the newest chunks.

    Reasoning models spend their whole completion budget on hidden reasoning
    when the prompt is long, so summaries are produced chunk by chunk. When the
    history exceeds ``max_chunks``, the oldest chunks are dropped; callers can
    derive the dropped turns from the returned coverage.
    """
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for item in turns:
        size = _block_chars(item) + 2
        if current and used + size > max_chars:
            chunks.append(current)
            current = []
            used = 0
        current.append(item)
        used += size
    if current:
        chunks.append(current)
    if len(chunks) > max(1, int(max_chunks)):
        return chunks[len(chunks) - max(1, int(max_chunks)) :]
    return chunks


def turns_to_messages(turns: list[dict[str, Any]]) -> list[Any]:
    from agentscope.message import AssistantMsg, UserMsg

    messages: list[Any] = []
    for item in turns:
        question = str(item.get("question") or "").strip()
        answer = strip_citations(str(item.get("answer") or "")).strip()
        if question:
            messages.append(UserMsg(name="user", content=question))
        if answer:
            messages.append(AssistantMsg(name="assistant", content=answer))
    return messages

def wrap_summary(text: str, turns: list[dict[str, Any]]) -> str:
    body = str(text or "").strip()
    if not body:
        return ""
    first = int(turns[0]["turn"])
    last = int(turns[-1]["turn"])
    label = f"第{first}轮" if first == last else f"第{first}-{last}轮"
    return f"【早期对话摘要（{label}）】\n{body}"