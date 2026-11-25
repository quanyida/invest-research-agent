from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.budget import Budget
from app.agent.capability import resolve_agentscope_tools
from app.agent.evidence_collector import EvidenceCollector
from app.agent.router import Plan
from app.config import Settings
from app.tools.ifind_gateway import IfindGateway

@dataclass
class RunContext:
    run: Any
    settings: Settings
    gateway: IfindGateway
    plan: Plan
    domain: str
    entities: list[str]
    turn: int
    allowed_tools: list[str]
    budget: Budget
    collector: EvidenceCollector
    prefetch_block: str = ""
    prefetch_info: dict[str, Any] | None = None

def build_run_context(run: Any, settings: Settings, gateway: IfindGateway) -> RunContext:
    plan = Plan(
        "agentscope_auto",
        note="AgentScope 根据问题和工具说明自主选择工具",
    )
    entities: list[str] = []
    domain = "auto"
    turn = max(1, int(getattr(run, "turn", 1) or 1))
    allowed = resolve_agentscope_tools()
    budget = Budget(
        timeout_seconds=float(getattr(run, "timeout_seconds", None) or settings.task_timeout_seconds),
        max_models=max(4, settings.max_steps * 2),
        convergence_seconds=getattr(run, "convergence_seconds", None),
    )
    return RunContext(
        run=run,
        settings=settings,
        gateway=gateway,
        plan=plan,
        domain=domain,
        entities=entities,
        turn=turn,
        allowed_tools=allowed,
        budget=budget,
        collector=EvidenceCollector(turn=turn),
    )

def plan_payload(ctx: RunContext) -> dict[str, Any]:
    plan = ctx.plan
    return {
        "mode": "agentscope_auto",
        "intent": plan.intent,
        "tools": [],
        "allowed": list(ctx.allowed_tools),
        "note": plan.note,
        "unsupported": plan.unsupported,
        "domain": ctx.domain,
        "entities": list(ctx.entities),
        "turn": ctx.turn,
        "engine": "agentscope",
    }
