from __future__ import annotations

import time
from dataclasses import dataclass, field

class BudgetExceeded(RuntimeError):
    pass

@dataclass
class Budget:
    timeout_seconds: float
    max_tools: int | None = None
    max_models: int = 4
    convergence_seconds: float | None = None
    started: float = field(default_factory=time.monotonic)
    tools_used: int = 0
    models_used: int = 0

    @property
    def deadline(self) -> float:
        return self.started + self.timeout_seconds

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.05

    @property
    def models_remaining(self) -> int:
        return max(0, self.max_models - self.models_used)

    def can_start_tool(self) -> bool:
        return not self.expired()

    def take_tool(self) -> None:
        if self.expired():
            raise BudgetExceeded("TASK_TIMEOUT")
        self.tools_used += 1

    def take_model(self, attempts: int = 1) -> None:
        self.models_used += max(1, attempts)

    def model_timeout(self, cap: float) -> float:
        return max(0.0, min(cap, self.remaining()))

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def snapshot(self) -> dict:
        return {
            "tools_used": self.tools_used,
            "max_tools": self.max_tools,
            "models_used": self.models_used,
            "max_models": self.max_models,
            "elapsed_ms": self.elapsed_ms(),
            "remaining_ms": int(self.remaining() * 1000),
        }