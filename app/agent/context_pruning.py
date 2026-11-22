from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from agentscope.message import TextBlock, ToolResultBlock
from agentscope.middleware import MiddlewareBase
from agentscope.state import AgentState

logger = logging.getLogger(__name__)

PRUNED_PREFIX = "[历史工具原文已裁剪]"


def _payload_chars(output: Any) -> int | None:
    if isinstance(output, str):
        return len(output)
    if isinstance(output, list) and all(isinstance(block, TextBlock) for block in output):
        return sum(len(block.text) for block in output)
    return None


def _already_pruned(output: Any) -> bool:
    if isinstance(output, str):
        return output.startswith(PRUNED_PREFIX)
    if isinstance(output, list) and output and isinstance(output[0], TextBlock):
        return output[0].text.startswith(PRUNED_PREFIX)
    return False


def prune_tool_payloads(
    state: AgentState,
    keep_recent_tool_results: int = 4,
) -> tuple[int, int]:
    """Replace old tool payloads with short references.

    Tool call/result blocks stay in place so their pairing remains valid. Only
    the payload bytes of old results are released.
    """
    keep_recent = max(0, keep_recent_tool_results)
    results = [
        block
        for message in state.context
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    if len(results) <= keep_recent:
        return 0, 0

    pruned = 0
    released_chars = 0
    for block in results[: len(results) - keep_recent]:
        chars = _payload_chars(block.output)
        if chars is None or chars == 0 or _already_pruned(block.output):
            continue
        block.output = (
            f"{PRUNED_PREFIX} tool={block.name} chars={chars}; "
            "如需细节请重新调用该工具。"
        )
        pruned += 1
        released_chars += chars
    return pruned, released_chars


class ContextPruningMiddleware(MiddlewareBase):
    """Prune tool payloads before summarization and retry with a smaller input."""

    def __init__(
        self,
        keep_recent_tool_results: int = 4,
        retry_keep_recent_tool_results: int = 1,
    ) -> None:
        self.keep_recent_tool_results = max(0, keep_recent_tool_results)
        self.retry_keep_recent_tool_results = max(0, retry_keep_recent_tool_results)
        self.pruned_tool_results = 0
        self.pruned_chars = 0
        self.compression_attempts = 0
        self.compression_failed = False
        self.fallback_mode: str | None = None
        self.last_error = ""

    def _prune(self, state: AgentState, keep_recent: int) -> None:
        pruned, released_chars = prune_tool_payloads(state, keep_recent)
        self.pruned_tool_results += pruned
        self.pruned_chars += released_chars

    async def on_compress_context(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        self._prune(agent.state, self.keep_recent_tool_results)

        if self.fallback_mode is not None:
            return

        for attempt in range(2):
            self.compression_attempts += 1
            try:
                await next_handler()
                return
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.compression_failed = True
                self.last_error = str(exc)[:500]
                logger.warning(
                    "context compression attempt %d failed: %s",
                    attempt + 1,
                    exc,
                )
                if attempt == 0:
                    self._prune(
                        agent.state,
                        self.retry_keep_recent_tool_results,
                    )
                    continue

        self.fallback_mode = "keep_context"
        logger.warning(
            "context compression failed twice; keeping the current context "
            "without truncating it"
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "pruned_tool_results": self.pruned_tool_results,
            "pruned_chars": self.pruned_chars,
            "compression_attempts": self.compression_attempts,
            "compression_failed": self.compression_failed,
            "fallback_mode": self.fallback_mode,
            "last_error": self.last_error,
        }