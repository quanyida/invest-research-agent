from __future__ import annotations

import re
from typing import Any

from agentscope.middleware import MiddlewareBase

from app.agent.budget import Budget

TIME_MARKER = "【本轮剩余时间】"
CONVERGENCE_SECONDS = 60.0
CAUTION_SECONDS = 120.0
_REMAINING_RE = re.compile(r"当前剩余约\s*(\d+)\s*秒")


def render_time_budget(
    total_seconds: float,
    remaining_seconds: float,
    convergence_seconds: float = CONVERGENCE_SECONDS,
) -> str:
    total = max(0, int(round(float(total_seconds))))
    remaining = max(0, int(round(float(remaining_seconds))))
    lines = [
        "",
        TIME_MARKER,
        f"- 本轮总时长 {total} 秒；当前剩余约 {remaining} 秒。",
    ]
    if remaining_seconds <= convergence_seconds:
        lines.append(
            f"- 【必须收敛】剩余时间不足 {int(convergence_seconds)} 秒，"
            "立即用已有数据成文，不要再调用工具。"
        )
    elif remaining_seconds <= CAUTION_SECONDS:
        lines.append("- 剩余时间已不多，停止补充取数，优先用已有数据完成答案。")
    else:
        lines.append(
            f"- 剩余 {int(convergence_seconds)} 秒时必须停止取数并立即成文。"
        )
    return "\n".join(lines)


class TimeBudgetMiddleware(MiddlewareBase):
    """Inject a fresh time budget into every model call.

    ``on_system_prompt`` runs each time AgentScope prepares model input, so the
    remaining seconds reflect tool calls already completed in this turn.
    ``on_model_call`` reads the final payload and records whether the dynamic
    marker actually reached the model.
    """

    def __init__(
        self,
        budget: Budget,
        convergence_seconds: float = CONVERGENCE_SECONDS,
    ) -> None:
        self.budget = budget
        self.convergence_seconds = float(convergence_seconds)
        self.prompt_updates = 0
        self.calls: list[dict[str, Any]] = []

    async def on_system_prompt(self, agent: Any, current_prompt: str) -> str:
        self.prompt_updates += 1
        block = render_time_budget(
            self.budget.timeout_seconds,
            self.budget.remaining(),
            self.convergence_seconds,
        )
        return f"{current_prompt}{block}"

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ) -> Any:
        messages = list(input_kwargs.get("messages") or [])
        joined = "\n".join(
            text
            for text in (
                str(getattr(message, "get_text_content", lambda: "")() or "")
                for message in messages
            )
            if text
        )
        match = _REMAINING_RE.search(joined)
        self.calls.append(
            {
                "call": len(self.calls) + 1,
                "remaining_seconds": int(match.group(1)) if match else None,
                "marker_present": TIME_MARKER in joined,
                "urgent": "【必须收敛】" in joined,
            }
        )
        return await next_handler()

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_seconds": int(round(float(self.budget.timeout_seconds))),
            "convergence_seconds": int(round(self.convergence_seconds)),
            "prompt_updates": self.prompt_updates,
            "model_calls": len(self.calls),
            "urgent_calls": sum(1 for call in self.calls if call["urgent"]),
            "calls": self.calls[-50:],
        }