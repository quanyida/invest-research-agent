from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.event import ReplyEndEvent
from agentscope.message import AssistantMsg, Msg, SystemMsg, UserMsg
from agentscope.model import OpenAIChatModel
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.types import ReplyFinishedReason
from openai import APITimeoutError

from app.agent.context_pruning import ContextPruningMiddleware
from app.agent.context_probe import ContextProbeMiddleware
from app.agent.history_context import (
    SUMMARY_SYSTEM_PROMPT,
    chunk_turns,
    load_history_turns,
    render_summary_input,
    split_history,
    turns_from_history,
    turns_to_messages,
    wrap_summary,
)
from app.agent.agent_state_store import (
    AgentStateStore,
    AgentStateTooLarge,
    compact_agent_state,
    detect_gap,
    ensure_turn_marks,
    mark_gap,
    record_saved_turn,
    reset_turn_marks,
    state_was_truncated,
)
from app.agent.answer_format import SECTION_LABELS, SECTION_PATTERNS, answer_is_complete
from app.agent.answer_guard import (
    digest_answer,
    guard_answer,
    revision_message,
    verify_payload,
)
from app.agent.context import build_agent_system_prompt, unsupported_answer
from app.agent.event_bridge import EventBridge
from app.agent.persist import persist_turn
from app.agent.prefetch import prefetch_web
from app.agent.run_context import build_run_context, plan_payload
from app.agent.verify import (
    VerifyResult,
    citation_only_issues,
    strip_citations,
    verify_answer,
    verify_raw_answer,
)
from app.config import ROOT, get_settings
from app.tools.agentscope_tools import create_tools
from app.tools.ifind_gateway import get_gateway

logger = logging.getLogger(__name__)

SKILLS_DIR = ROOT / "skills" / "ifind-finance-data"
AGENT_NAME = "investment_research_agent"
SYNTHESIS_MAX_TOKENS = 16384
SYNTHESIS_TIMEOUT_SECONDS = 240.0
SUMMARY_TIMEOUT_SECONDS = 45.0
SUMMARY_TOTAL_TIMEOUT_SECONDS = 90.0
SUMMARY_MAX_TOKENS = 3000
GAP_REBUILD_TURNS = 3
SYNTHESIS_SYSTEM_PROMPT = """你是一名严谨的卖方研究助理。本轮只做最终成文，不得调用任何工具。
你只能使用用户消息中 <tool_observation> 里的 iFinD 与公开网页原始数据，不得编造数据。
严格输出四段纯文本：结论：/依据：/关键数据：/待确认：；不要 Markdown、不要过程说明。
引用来源只能使用本轮 <tool_observation call="..."> 给出的编号，括号内只写编号本身（如 [E3-1]）。
不得引用历史轮次编号（历史编号已失效），不得在括号内附带媒体名、日期或链接，不得编造编号。
"""


async def _await_write(task: "asyncio.Task[Any]") -> Any:
    """Let an in-flight database write finish before propagating cancellation."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _reason_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, ReplyFinishedReason):
        return value.value
    raw = getattr(value, "value", value)
    return str(raw).lower()


def _synthesis_context(question: str) -> list[Any]:
    value = (question or "").strip()
    return [UserMsg(name="user", content=value)] if value else []

def _synthesis_prompt(
    evidences: list[dict[str, Any]],
    *,
    question: str = "",
    draft: str = "",
) -> str:
    blocks: list[str] = []
    for evidence in evidences:
        raw = str(evidence.get("raw_text") or evidence.get("raw_excerpt") or "").strip()
        if not raw:
            continue
        eid = str(evidence.get("id") or f"E{evidence.get('seq', 1)}")
        tool = str(evidence.get("tool") or "ifind")
        blocks.append(f'<tool_observation tool="{tool}" call="{eid}">\n{raw}\n</tool_observation>')
    body = "\n\n".join(blocks)
    if len(body) > 60000:
        body = body[:60000] + "\n[原始证据过长，已截断]"
    question = (question or "").strip()
    draft = (draft or "").strip()
    question_block = f"用户问题：{question}\n\n" if question else ""
    draft_block = f"<draft>\n{draft[:6000]}\n</draft>\n\n" if draft else ""
    return (
        f"{question_block}"
        "本轮工具调用已结束。请只基于以下 iFinD 与公开网页原始数据，把上一版草稿补全为完整最终答案，不要再调用工具。\n"
        "用户问题中的标的是唯一分析对象，不得写其他公司。\n"
        "严格四段：结论：/依据：/关键数据：/待确认：；不要 Markdown、不要过程说明。\n\n"
        f"{draft_block}{body}\n\n"
        "请给出完整分析，不要只写半句；无法从原文确认的内容放入“待确认”。"
    )


def _timeout_fallback(
    evidences: list[dict[str, Any]],
    *,
    model_timeout: bool,
) -> tuple[str, str, str]:
    if model_timeout:
        return digest_answer(evidences, "模型调用超时未完成"), "model_timeout", "MODEL_TIMEOUT"
    reason = "本次分析超时未完成"
    return digest_answer(evidences, reason), "timeout", "TIMEOUT"



class AgentScopeRunner:
    """Single AgentScope runtime for one chat turn."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.gateway = get_gateway()

    def _model(
        self,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        stream: bool = True,
    ) -> OpenAIChatModel:
        return OpenAIChatModel(
            credential=OpenAICredential(
                api_key=self.settings.llm_api_key,
                base_url=self.settings.llm_base_url,
            ),
            model=self.settings.llm_model,
            context_size=self.settings.llm_context_size,
            parameters=OpenAIChatModel.Parameters(
                temperature=self.settings.llm_temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.settings.llm_max_tokens,
            ),
            stream=stream,
            max_retries=max(0, min(1, self.settings.model_max_attempts - 1)),
            client_kwargs={
                "timeout": timeout
                if timeout is not None
                else self.settings.llm_timeout_seconds
            },
        )

    def _toolkit(self, ctx: Any) -> Toolkit:
        skill_paths = [str(SKILLS_DIR)] if SKILLS_DIR.exists() else []
        return Toolkit(tools=create_tools(ctx), skills_or_loaders=skill_paths)

    def _context_config(self) -> ContextConfig:
        return ContextConfig(
            tool_result_limit=max(6000, self.settings.tool_result_limit),
            trigger_ratio=0.7,
            compression_fallback_to_truncation=False,
        )

    @staticmethod
    def _react_config(_ctx: Any) -> ReActConfig:
        return ReActConfig(
            interruption_message="本次分析已中断，正在整理已获取的数据。",
            interruption_raise_cancelled_error=True,
        )

    @staticmethod
    def _chunks(text: str, size: int = 40) -> list[str]:
        return [text[index : index + size] for index in range(0, len(text), size)]

    @staticmethod
    def _history_messages(history: list[dict[str, str]] | None) -> list[Any]:
        messages: list[Any] = []
        for item in history or []:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            role = str(item.get("role") or "")
            if role == "user":
                messages.append(UserMsg(name="user", content=content))
            elif role == "assistant":
                content = strip_citations(content).strip()
                if content:
                    messages.append(AssistantMsg(name="assistant", content=content))
        return messages

    @staticmethod
    def _response_text(response: Any) -> str:
        """Collect text blocks without relying on ``isinstance``.

        Some OpenAI-compatible gateways return content blocks from a different
        module instance; matching on the ``text`` attribute keeps this working.
        """
        parts: list[str] = []
        for block in getattr(response, "content", None) or []:
            value = getattr(block, "text", None)
            if not isinstance(value, str) or not value:
                continue
            kind = getattr(block, "type", None)
            if kind is not None and kind != "text":
                continue
            parts.append(value)
        return "".join(parts).strip()

    async def _summarize_history(
        self,
        turns: list[dict[str, Any]],
        ctx: Any,
    ) -> tuple[str, dict[str, Any] | None]:
        """Compress early turns chunk by chunk.

        The configured model is a reasoning model: a single long prompt can
        consume the whole completion budget on hidden reasoning and return no
        text at all. Chunking keeps every request small and bounded.
        """
        chunks = chunk_turns(turns)
        if not chunks:
            return "", None
        deadline = time.monotonic() + SUMMARY_TOTAL_TIMEOUT_SECONDS
        started = time.monotonic()
        parts: list[str] = []
        covered: list[int] = []
        failed: list[int] = []
        prompt_tokens = 0
        completion_tokens = 0
        calls = 0
        finish_reason = ""
        for chunk in chunks:
            body, included = render_summary_input(chunk)
            if not body:
                continue
            remaining = min(deadline - time.monotonic(), ctx.budget.remaining())
            if remaining <= 1.0:
                failed.extend(included)
                continue
            model = self._model(
                max_tokens=SUMMARY_MAX_TOKENS,
                temperature=0.1,
                timeout=min(SUMMARY_TIMEOUT_SECONDS, remaining),
                stream=False,
            )
            ctx.budget.take_model()
            calls += 1
            try:
                response = await model(
                    [
                        SystemMsg(name="system", content=SUMMARY_SYSTEM_PROMPT),
                        UserMsg(name="user", content=body),
                    ]
                )
            except Exception:
                logger.exception(
                    "history summary chunk failed for turns %s", included
                )
                failed.extend(included)
                continue
            text = self._response_text(response)
            usage = getattr(response, "usage", None)
            prompt_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            completion_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            finish_reason = str(getattr(response, "finished_reason", "") or "")
            if not text:
                logger.warning(
                    "history summary chunk returned no text for turns %s",
                    included,
                )
                failed.extend(included)
                continue
            parts.append(text)
            covered.extend(included)
        audit = {
            "kind": "llm",
            "purpose": "history_summary",
            "model": self.settings.llm_model,
            "temperature": 0.1,
            "max_tokens": SUMMARY_MAX_TOKENS,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": 0,
            "finish_reason": finish_reason,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "status": "ok" if parts else "empty",
            "attempts": calls,
            "calls": calls,
            "chunks": len(chunks),
            "covered_turns": sorted(set(covered)),
            "failed_turns": sorted(set(failed)),
        }
        if not parts:
            return "", audit
        ordered = [{"turn": item} for item in sorted(set(covered))]
        return wrap_summary("\n".join(parts), ordered), audit

    async def _rebuild_state(
        self,
        run: Any,
        ctx: Any,
        gap_turns: list[int],
    ) -> tuple[str, list[Any], dict[str, Any]]:
        info: dict[str, Any] = {
            "summary_turns": [],
            "summary_chars": 0,
            "summary_calls": 0,
            "summary_failed_turns": [],
            "summary_skipped": False,
            "recent_turns": [],
            "history_source": "db",
            "audit": None,
            "gap_turns": list(gap_turns),
            "rebuild_reason": "",
        }
        try:
            turns = await asyncio.to_thread(
                load_history_turns,
                run.tenant_id,
                run.user_id,
                run.conversation_id,
                run.request_id,
            )
        except Exception:
            logger.exception(
                "history load failed for conversation %s", run.conversation_id
            )
            turns = []
            info["history_source"] = "unavailable"
        if not turns and getattr(run, "history", None):
            turns = turns_from_history(run.history)
            info["history_source"] = "run_history"
        early, recent = split_history(
            turns, max(1, int(self.settings.history_turns))
        )
        info["recent_turns"] = [int(item["turn"]) for item in recent]
        recent_messages = (
            turns_to_messages(recent)
            if recent
            else self._history_messages(run.history)
        )
        if not early:
            return "", recent_messages, info
        if not self.settings.agent_state_save:
            info["summary_skipped"] = True
            info["summary_skip_reason"] = "state_disabled"
            return "", recent_messages, info
        if ctx.budget.expired():
            info["summary_skipped"] = True
            info["summary_skip_reason"] = "budget"
            return "", recent_messages, info
        try:
            summary, audit = await self._summarize_history(early, ctx)
        except Exception as exc:
            logger.exception(
                "history summary failed for conversation %s", run.conversation_id
            )
            info["summary_skipped"] = True
            info["summary_skip_reason"] = type(exc).__name__
            return "", recent_messages, info
        info["audit"] = audit
        info["summary_calls"] = int((audit or {}).get("calls") or 0)
        info["summary_failed_turns"] = list(
            (audit or {}).get("failed_turns") or []
        )
        if not summary:
            info["summary_skipped"] = True
            info["summary_skip_reason"] = "empty"
            return "", recent_messages, info
        covered = set((audit or {}).get("covered_turns") or [])
        dropped = [
            int(item["turn"]) for item in early if int(item["turn"]) not in covered
        ]
        info["summary_turns"] = sorted(covered)
        info["summary_dropped_turns"] = dropped
        info["summary_chars"] = len(summary)
        return summary, recent_messages, info

    async def _persist(
        self,
        run: Any,
        ctx: Any,
        *,
        answer: str,
        status: str,
        meta: dict[str, Any],
        evidences: list[dict[str, Any]],
        tool_audits: list[dict[str, Any]],
        llm_audits: list[dict[str, Any]],
        usage: dict[str, Any],
        agent: Agent | None,
        state_version: int,
        state_store: AgentStateStore,
    ) -> tuple[bool | None, bool]:
        state_payload: dict[str, Any] | None = None
        state_skipped = False
        skip_reason: str | None = None
        if not self.settings.agent_state_save:
            state_skipped = True
            skip_reason = "state_disabled"
        elif agent is None:
            state_skipped = True
            skip_reason = "agent_unavailable"
        else:
            record_saved_turn(agent.state, ctx.turn)
            released = compact_agent_state(agent.state)
            meta["agent_state_released_blocks"] = released
            try:
                state_payload = state_store.serialize(agent.state)
            except AgentStateTooLarge:
                try:
                    await agent.compress_context()
                    state_payload = state_store.serialize(agent.state)
                except Exception:
                    logger.exception("agent state compression failed")
                    state_skipped = True
                    skip_reason = "compression_error"
            except Exception:
                logger.exception("agent state serialization failed")
                state_skipped = True
                skip_reason = "serialization_error"

        meta["agent_state_skipped"] = state_skipped
        meta["agent_state_skip_reason"] = skip_reason
        persist_task = asyncio.create_task(
            asyncio.to_thread(
                persist_turn,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                conversation_id=run.conversation_id,
                message_id=run.message_id,
                request_id=run.request_id,
                question=run.question,
                answer=answer,
                status=status,
                meta=meta,
                evidences=evidences,
                tool_audits=tool_audits,
                llm_audits=llm_audits,
                usage=usage,
                agent_state=state_payload,
                agent_state_version=state_version if state_payload is not None else None,
                elapsed_ms=int(ctx.budget.elapsed_ms()),
                run_id=run.run_id,
                worker_id=getattr(run, "worker_id", None),
                attempt=getattr(run, "attempt", None),
            )
        )
        # The turn is already produced; a cancellation (timeout, disconnect)
        # must not throw away the collected tool results before they are stored.
        saved, run_finalized = await _await_write(persist_task)
        return saved, state_skipped, run_finalized

    async def stream(self, run: Any) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        started = time.monotonic()
        ctx = build_run_context(run, self.settings, self.gateway)
        state_store = AgentStateStore(self.settings)
        state, state_version = await asyncio.to_thread(
            state_store.load,
            run.tenant_id,
            run.user_id,
            run.conversation_id,
        )
        state_rebuilt = state is None
        rebuild_reason = "state_missing" if state_rebuilt else ""
        loaded_released = 0
        gap_turns: list[int] = []
        if state is not None and state_was_truncated(state):
            logger.warning(
                "agent state for conversation %s contains fallback truncation; "
                "rebuilding from history",
                run.conversation_id,
            )
            state = None
            state_rebuilt = True
            rebuild_reason = "state_truncated"
        if state is not None:
            loaded_released = compact_agent_state(state)
            ensure_turn_marks(state, ctx.turn)
            gap_turns = detect_gap(state, ctx.turn)
            if len(gap_turns) >= GAP_REBUILD_TURNS:
                logger.warning(
                    "agent state for conversation %s is missing turns %s; rebuilding",
                    run.conversation_id,
                    gap_turns,
                )
                state = None
                state_rebuilt = True
                rebuild_reason = "state_gap"
        rebuild_info: dict[str, Any] = {}
        recent_messages: list[Any] = []
        if state is None:
            state = AgentState(session_id=str(run.conversation_id))
            summary, recent_messages, rebuild_info = await self._rebuild_state(
                run, ctx, gap_turns
            )
            if summary:
                state.summary = summary
            reset_turn_marks(state, ctx.turn)
            mark_gap(state, gap_turns)
        else:
            mark_gap(state, gap_turns)

        meta: dict[str, Any] = {
            "engine": "agentscope",
            "conversation_id": str(run.conversation_id),
            "message_id": str(run.message_id),
            "request_id": str(run.request_id),
            "timeout_seconds": self.settings.task_timeout_seconds,
            "turn": ctx.turn,
            "agent_state_version": state_version,
            "agent_state_rebuilt": state_rebuilt,
            "agent_state_rebuild_reason": rebuild_reason,
            "agent_state_gap_turns": gap_turns,
            "agent_state_summary_turns": rebuild_info.get("summary_turns", []),
            "agent_state_summary_chars": rebuild_info.get("summary_chars", 0),
            "agent_state_summary_skipped": rebuild_info.get("summary_skipped", False),
            "agent_state_summary_calls": rebuild_info.get("summary_calls", 0),
            "agent_state_summary_dropped_turns": rebuild_info.get(
                "summary_dropped_turns", []
            ),
            "agent_state_context_turns": rebuild_info.get("recent_turns", []),
            "agent_state_loaded_released_blocks": loaded_released,
            "stream_mode": self.settings.answer_stream_mode,
            "context": {
                "size": self.settings.llm_context_size,
                "trigger_ratio": 0.7,
                "trigger_tokens": int(self.settings.llm_context_size * 0.7),
            },
        }
        yield ("meta", dict(meta))
        yield ("plan", plan_payload(ctx))

        trace: list[dict[str, Any]] = []
        agent: Agent | None = None
        bridge = EventBridge(ctx.collector, ctx.budget)
        final_answer = ""
        final_verify = VerifyResult(passed=False, issues=["not_started"])
        status = "degraded"
        degraded = True
        revised = False
        answer_source = "agent"
        context_middleware = ContextPruningMiddleware()
        probe = ContextProbeMiddleware(
            summary=state.summary if isinstance(state.summary, str) else "",
            recent_questions=[
                str(item.get_text_content())
                for item in recent_messages
                if getattr(item, "role", "") == "user"
            ],
        )

        if ctx.plan.unsupported:
            final_answer = unsupported_answer(ctx.plan.note)
            final_verify = VerifyResult(passed=True)
            status = "done"
            degraded = False
            answer_source = "unsupported"
            trace.append({"state": "UNSUPPORTED", "ms": int((time.monotonic() - started) * 1000)})
        else:
            prefetch_record = await prefetch_web(ctx)
            if prefetch_record is not None:
                for event_name, data in bridge.record_events(prefetch_record):
                    yield event_name, data
            meta["web_prefetch"] = ctx.prefetch_info
            model = self._model()
            agent = Agent(
                name=AGENT_NAME,
                system_prompt=build_agent_system_prompt(ctx),
                model=model,
                toolkit=self._toolkit(ctx),
                state=state,
                context_config=self._context_config(),
                react_config=self._react_config(ctx),
                middlewares=[context_middleware, probe],
            )
            if state_rebuilt and recent_messages:
                await agent.observe(recent_messages)

            async def drive(
                target: Agent, content: str
            ) -> AsyncIterator[tuple[str, Any]]:
                final_msg: Msg | None = None
                finished_reason: str | None = None
                async for item in target.reply_stream(
                    UserMsg(name="user", content=content),
                    yield_final_msg=True,
                ):
                    if isinstance(item, Msg):
                        final_msg = item
                        yield ("_final_msg", item)
                        continue
                    if isinstance(item, ReplyEndEvent):
                        finished_reason = _reason_value(item.finished_reason)
                    for event_name, data in bridge.handle(item):
                        yield event_name, data
                if finished_reason is None and final_msg is not None:
                    finished_reason = _reason_value(final_msg.finished_reason)
                if finished_reason is None:
                    raise RuntimeError("AGENT_PAUSED_UNSUPPORTED")
                if finished_reason == ReplyFinishedReason.INTERRUPTED.value:
                    raise TimeoutError("AGENT_FINISHED_INTERRUPTED")
                if finished_reason not in {
                    ReplyFinishedReason.COMPLETED.value,
                    ReplyFinishedReason.EXCEED_MAX_ITERS.value,
                }:
                    raise RuntimeError(f"AGENT_FINISHED_{finished_reason.upper()}")

            try:
                async with asyncio.timeout(self.settings.task_timeout_seconds):
                    final_msg = None
                    async for event_name, data in drive(agent, run.question):
                        if event_name == "_final_msg":
                            final_msg = data
                        else:
                            yield event_name, data

                    raw_answer = final_msg.get_text_content() if final_msg is not None else ""
                    evidences = ctx.collector.public_evidences()
                    guard = guard_answer(
                        raw_answer or "",
                        evidences,
                        "; ".join(ctx.collector.error_messages()),
                        verify_numbers=not self.settings.ifind_raw_passthrough,
                    )
                    final_answer = guard.answer
                    final_verify = guard.verify
                    degraded = guard.degraded
                    revised = guard.repaired

                    incomplete = not answer_is_complete(final_answer)
                    if incomplete:
                        logger.warning(
                            "incomplete agent answer len=%d sections=%s head=%r tail=%r",
                            len(final_answer),
                            [
                                label
                                for label, pattern in zip(SECTION_LABELS, SECTION_PATTERNS)
                                if pattern.search(final_answer)
                            ],
                            final_answer[:400],
                            final_answer[-400:],
                        )
                    citation_only = citation_only_issues(guard.verify)
                    if evidences and (
                        incomplete
                        or (guard.degraded and not citation_only)
                        or (not guard.verify.passed and not citation_only)
                    ):
                        if incomplete or guard.degraded:
                            revision_agent = Agent(
                                name=AGENT_NAME,
                                system_prompt=SYNTHESIS_SYSTEM_PROMPT,
                                model=self._model(
                                    max_tokens=max(
                                        self.settings.llm_max_tokens,
                                        SYNTHESIS_MAX_TOKENS,
                                    ),
                                    temperature=0.1,
                                    timeout=SYNTHESIS_TIMEOUT_SECONDS,
                                    stream=False,
                                ),
                                toolkit=Toolkit(tools=[]),
                                state=AgentState(
                                    session_id=f"{run.conversation_id}:synthesis",
                                    context=_synthesis_context(run.question),
                                ),
                                context_config=self._context_config(),
                                middlewares=[context_middleware],
                                react_config=ReActConfig(
                                    max_iters=1,
                                    interruption_message="本次修订已中断，正在整理已获取的数据。",
                                    interruption_raise_cancelled_error=True,
                                ),
                            )
                            revision_text = _synthesis_prompt(
                                evidences,
                                question=run.question,
                                draft=guard.answer,
                            )
                        else:
                            revision_agent = Agent(
                                name=AGENT_NAME,
                                system_prompt=build_agent_system_prompt(ctx),
                                model=model,
                                toolkit=Toolkit(tools=[]),
                                state=agent.state,
                                context_config=self._context_config(),
                                middlewares=[context_middleware],
                                react_config=ReActConfig(
                                    max_iters=1,
                                    interruption_message="本次修订已中断，正在整理已获取的数据。",
                                    interruption_raise_cancelled_error=True,
                                ),
                            )
                            revision_text = revision_message(
                                guard.answer, guard.verify.issues
                            )
                        revision_msg = None
                        async for event_name, data in drive(revision_agent, revision_text):
                            if event_name == "_final_msg":
                                text = data.get_text_content() or ""
                                if text and "maximum reasoning-acting iterations" not in text:
                                    revision_msg = data
                            else:
                                yield event_name, data
                        if revision_msg is not None:
                            revision_guard = guard_answer(
                                revision_msg.get_text_content() or "",
                                evidences,
                                "revision",
                                verify_numbers=not self.settings.ifind_raw_passthrough,
                            )
                            revision_ok = (
                                revision_guard.verify.passed
                                or citation_only_issues(revision_guard.verify)
                            )
                            if not (
                                revision_ok
                                and answer_is_complete(revision_guard.answer)
                            ):
                                logger.warning(
                                    "revision output rejected: %r",
                                    (revision_msg.get_text_content() or "")[:2000],
                                )
                                logger.warning(
                                    "revision raw content: %r",
                                    str(getattr(revision_msg, "content", ""))[:2000],
                                )
                            if revision_ok and answer_is_complete(revision_guard.answer):
                                final_answer = revision_guard.answer
                                final_verify = revision_guard.verify
                                degraded = revision_guard.degraded or not revision_guard.verify.passed
                                revised = True
                            else:
                                final_answer = digest_answer(
                                    evidences,
                                    "Agent 修订轮未产出完整答案",
                                )
                                final_verify = revision_guard.verify
                                degraded = True
                                revised = True
                                answer_source = "digest"
                        else:
                            final_answer = digest_answer(evidences, "Agent 修订轮未产出文本")
                            degraded = True
                            revised = True
                            answer_source = "digest"

                    if not revised and guard.repaired:
                        revised = True
                    status = "done" if final_verify.passed and not degraded else "degraded"
                    if degraded and answer_source == "agent":
                        answer_source = "degraded"
                    trace.append(
                        {
                            "state": "AGENT",
                            "ms": int((time.monotonic() - started) * 1000),
                            "repaired": revised,
                            "degraded": degraded,
                        }
                    )
            except (TimeoutError, APITimeoutError) as exc:
                model_timeout = isinstance(exc, APITimeoutError)
                timeout_seconds = (
                    self.settings.llm_timeout_seconds
                    if model_timeout
                    else self.settings.task_timeout_seconds
                )
                logger.warning(
                    "agent %s timed out after %.1fs; returning evidence digest",
                    "model call" if model_timeout else "turn",
                    timeout_seconds,
                )
                evidences = ctx.collector.public_evidences()
                final_answer, answer_source, timeout_state = _timeout_fallback(
                    evidences,
                    model_timeout=model_timeout,
                )
                if self.settings.ifind_raw_passthrough:
                    _, final_verify = verify_raw_answer(final_answer, evidences)
                else:
                    final_verify = verify_answer(final_answer, evidences)
                status = "degraded"
                degraded = True
                revised = False
                agent = None
                meta["timeout"] = True
                meta["degraded_reason"] = answer_source
                if model_timeout:
                    meta["model_timeout"] = True
                trace.append(
                    {
                        "state": timeout_state,
                        "ms": int((time.monotonic() - started) * 1000),
                        "timeout_seconds": int(timeout_seconds),
                    }
                )

        evidences = ctx.collector.public_evidences()
        tool_audits = ctx.collector.audits()
        llm_audits = list(bridge.llm_audits)
        summary_audit = rebuild_info.get("audit")
        if isinstance(summary_audit, dict):
            llm_audits.insert(0, summary_audit)
        usage = {**ctx.budget.snapshot(), **bridge.usage()}
        usage["state_rebuilt"] = state_rebuilt
        usage["summary_calls"] = int((summary_audit or {}).get("calls") or 0)
        usage["summary_prompt_tokens"] = int(
            (summary_audit or {}).get("prompt_tokens") or 0
        )
        usage["summary_completion_tokens"] = int(
            (summary_audit or {}).get("completion_tokens") or 0
        )
        if degraded:
            meta.setdefault("degraded_reason", answer_source)

        meta.update(
            {
                "plan": plan_payload(ctx),
                "verify": verify_payload(final_verify, final_answer, evidences),
                "trace": trace,
                "errors": ctx.collector.error_messages()[:20],
                "budget": usage,
                "evidence_count": len(evidences),
                "tool_runs": len(tool_audits),
                "llm_runs": len(llm_audits),
                "repaired": revised,
                "degraded": degraded,
                "answer_source": answer_source,
                "context_compression": context_middleware.snapshot(),
                "agent_state_prompt_probe": probe.snapshot(),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        )

        async with asyncio.timeout(self.settings.task_timeout_seconds):
            saved, state_skipped, run_finalized = await self._persist(
                run,
                ctx,
                answer=final_answer,
                status=status,
                meta=meta,
                evidences=evidences,
                tool_audits=tool_audits,
                llm_audits=llm_audits,
                usage=usage,
                agent=agent,
                state_version=state_version,
                state_store=state_store,
            )

        usage["agent_state_saved"] = saved
        usage["agent_state_skipped"] = state_skipped
        usage["agent_state_conflict"] = bool(saved is False)
        verdict = verify_payload(final_verify, final_answer, evidences)
        verdict["repaired"] = revised
        verdict["degraded"] = degraded
        yield ("verify", verdict)
        for chunk in self._chunks(final_answer):
            yield ("delta", {"text": chunk})
        yield ("usage", usage)
        yield (
            "done",
            {
                "status": status,
                "message_id": str(run.message_id),
                "conversation_id": str(run.conversation_id),
                "request_id": str(run.request_id),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "agent_state_saved": saved,
                "run_finalized": run_finalized,
            },
        )
