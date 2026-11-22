from __future__ import annotations

from typing import Any

from agentscope.middleware import MiddlewareBase

def _message_text(message: Any) -> str:
    try:
        return str(message.get_text_content() or "")
    except Exception:
        return ""

class ContextProbeMiddleware(MiddlewareBase):
    """Record what the model actually receives on the first call of a turn.

    This uses the public ``on_model_call`` hook, so it does not depend on
    AgentScope internals. The snapshot proves whether the rebuild summary and
    the recent questions really reached the prompt.
    """

    def __init__(
        self,
        summary: str = "",
        recent_questions: list[str] | None = None,
    ) -> None:
        self.summary = str(summary or "")
        self.recent_questions = [
            str(item)[:40]
            for item in (recent_questions or [])
            if str(item or "").strip()
        ]
        self.model_calls = 0
        self.first: dict[str, Any] | None = None

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ) -> Any:
        if self.first is None:
            messages = list(input_kwargs.get("messages") or [])
            joined = "\n".join(
                text for text in (_message_text(item) for item in messages) if text
            )
            self.first = {
                "message_count": len(messages),
                "prompt_chars": len(joined),
                "summary_chars": len(self.summary),
                "summary_present": bool(
                    self.summary and self.summary[:40] in joined
                ),
                "recent_questions_expected": len(self.recent_questions),
                "recent_questions_present": sum(
                    1 for item in self.recent_questions if item in joined
                ),
                "prompt_head": joined[:160],
            }
        self.model_calls += 1
        return await next_handler()

    def snapshot(self) -> dict[str, Any]:
        snapshot = dict(self.first or {})
        snapshot["model_calls"] = self.model_calls
        return snapshot