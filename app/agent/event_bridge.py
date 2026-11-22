from __future__ import annotations

import time
from typing import Any

from agentscope.event import (
    ModelCallEndEvent,
    ModelCallStartEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import ToolResultState

from app.agent.budget import Budget
from app.agent.evidence_collector import EvidenceCollector, ToolRecord, public_evidence_payload
from app.tools.agentscope_tools import pretty_name

def _public_evidence(record: ToolRecord) -> dict[str, Any]:
    return public_evidence_payload(record.evidence or {})

def _state_label(state: Any) -> str:
    try:
        return ToolResultState(state).name.lower()
    except Exception:
        return str(getattr(state, "name", state) or "").lower()

class EventBridge:
    """Translate AgentScope events into the existing SSE vocabulary."""

    def __init__(self, collector: EvidenceCollector, budget: Budget | None = None) -> None:
        self.collector = collector
        self.budget = budget
        self.tool_names: dict[str, str] = {}
        self.model_starts: dict[str, float] = {}
        self.llm_audits: list[dict[str, Any]] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def record_events(self, record: ToolRecord) -> list[tuple[str, dict[str, Any]]]:
        """Publish a record created outside AgentScope's tool-call lifecycle."""
        group = getattr(record, "split_group", "") or ""
        records = (
            [
                item
                for item in self.collector.records
                if getattr(item, "split_group", "") == group
            ]
            if group
            else [record]
        )
        for item in records:
            item.emitted = True
        out: list[tuple[str, dict[str, Any]]] = [
            (
                "tool_start",
                {
                    "tool": pretty_name(record.tool),
                    "tool_call_id": f"prefetch-{record.seq}",
                },
            )
        ]
        for item in records:
            if item.evidence is not None:
                out.append(("evidence", {"evidence": _public_evidence(item)}))
        out.append(
            (
                "tool_end",
                {
                    "tool": pretty_name(record.tool),
                    "status": record.status,
                    "latency_ms": record.latency_ms,
                    "cached": record.cached,
                    "error_code": record.error_code,
                },
            )
        )
        return out

    def handle(self, event: Any) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        if isinstance(event, ToolCallStartEvent):
            self.tool_names[event.tool_call_id] = event.tool_call_name
            out.append(
                (
                    "tool_start",
                    {
                        "tool": pretty_name(event.tool_call_name),
                        "tool_call_id": event.tool_call_id,
                    },
                )
            )
            return out

        if isinstance(event, ToolResultEndEvent):
            display = self.tool_names.pop(event.tool_call_id, "")
            records = self.collector.drain()
            if records:
                for record in records:
                    if record.evidence is not None:
                        out.append(("evidence", {"evidence": _public_evidence(record)}))
                for record in records:
                    if not getattr(record, "is_primary", True):
                        continue
                    out.append(
                        (
                            "tool_end",
                            {
                                "tool": pretty_name(record.tool),
                                "status": record.status,
                                "latency_ms": record.latency_ms,
                                "cached": record.cached,
                                "error_code": record.error_code,
                            },
                        )
                    )
            else:
                out.append(
                    (
                        "tool_end",
                        {
                            "tool": pretty_name(display) if display else "tool",
                            "status": _state_label(event.state),
                            "latency_ms": 0,
                            "cached": False,
                            "error_code": None,
                        },
                    )
                )
            return out

        if isinstance(event, ModelCallStartEvent):
            self.model_starts[getattr(event, "reply_id", "")] = time.monotonic()
            return out

        if isinstance(event, ModelCallEndEvent):
            started = self.model_starts.pop(getattr(event, "reply_id", ""), None)
            latency = int((time.monotonic() - started) * 1000) if started else 0
            prompt = int(event.input_tokens or 0)
            completion = int(event.output_tokens or 0)
            if self.budget is not None:
                self.budget.take_model()
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.llm_audits.append(
                {
                    "kind": "llm",
                    "purpose": "agentscope",
                    "model": str(getattr(event, "model_name", "") or ""),
                    "temperature": 0,
                    "max_tokens": 0,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "reasoning_tokens": 0,
                    "finish_reason": str(getattr(event, "finished_reason", "") or ""),
                    "latency_ms": latency,
                    "status": "ok",
                    "attempts": 1,
                }
            )
            return out

        return out

    def usage(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "llm_calls": len(self.llm_audits),
        }
