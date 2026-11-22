from __future__ import annotations

import asyncio
from typing import Any, Callable

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from app.agent.budget import BudgetExceeded
from app.agent.evidence_collector import args_hash
from app.agent.run_context import RunContext
from app.tools.ifind_gateway import ToolOutcome
from app.tools.registry import ToolSpec
from app.tools.rewrite import validate_args

Executor = Callable[[dict[str, Any]], ToolOutcome]


def display_name(internal: str) -> str:
    return internal.replace(".", "_")


def pretty_name(name: str) -> str:
    if name.startswith("ifind_"):
        return "ifind." + name[len("ifind_") :]
    if name.startswith("web_"):
        return "web." + name[len("web_") :]
    return name


class AuditedTool(ToolBase):
    """One tenant-scoped tool exposed to AgentScope with shared auditing."""

    is_read_only = True
    is_concurrency_safe = False

    def __init__(self, spec: ToolSpec, ctx: RunContext, executor: Executor) -> None:
        self.spec = spec
        self.ctx = ctx
        self.executor = executor
        self.name = display_name(spec.name)
        if spec.provider == "ifind":
            suffix = " 直接查询 iFinD 结构化数据；参数必须符合 schema，不要编造参数。"
        else:
            suffix = " 返回公开网页搜索结果；正文只引用来源编号，媒体名、日期、链接由系统在来源区展示。"
        self.description = spec.description + suffix
        self.input_schema = spec.schema

    async def check_permissions(self, tool_input: dict, context: Any) -> PermissionDecision:
        if self.spec.name not in self.ctx.allowed_tools:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"tool {self.spec.name} is not available in this turn",
            )
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="tenant-scoped read-only tool",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        args = {key: value for key, value in kwargs.items() if not key.startswith("_")}
        if self.spec.provider == "ifind":
            try:
                validate_args(self.spec.name, args)
            except Exception as exc:
                record = self.ctx.collector.add_error(
                    self.spec.name,
                    args,
                    "BAD_REQUEST",
                    str(exc),
                )
                return ToolChunk(
                    content=[TextBlock(text=record.observation)],
                    state=ToolResultState.ERROR,
                )

        key = args_hash(args)
        existing = self.ctx.collector.lookup(self.spec.name, key)
        if existing is not None:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=existing.observation
                        + "\n(同一参数已调用过，复用上一条 observation；未重复扣预算。)"
                    )
                ]
            )

        try:
            self.ctx.budget.take_tool()
        except BudgetExceeded:
            record = self.ctx.collector.add_error(
                self.spec.name,
                args,
                "TASK_TIMEOUT",
                "本轮执行时间已用尽；只能基于已有 observation 成文，或写“未取到”。",
            )
            return ToolChunk(
                content=[TextBlock(text=record.observation)],
                state=ToolResultState.ERROR,
            )

        outcome = await asyncio.to_thread(self.executor, args)
        record = self.ctx.collector.add_outcome(self.spec.name, args, outcome)
        state = ToolResultState.SUCCESS if outcome.status == "ok" else ToolResultState.ERROR
        return ToolChunk(content=[TextBlock(text=record.observation)], state=state)


def create_tools(ctx: RunContext) -> list[AuditedTool]:
    from app.tools.registry import get_tool
    from app.tools.web_search import get_web_search

    tools: list[AuditedTool] = []
    web_client = None
    for name in ctx.allowed_tools:
        try:
            spec = get_tool(name)
        except KeyError:
            continue

        if spec.provider == "web":
            if web_client is None:
                web_client = get_web_search()
            executor: Executor = _web_executor(web_client, spec.name, ctx)
        else:
            executor = _ifind_executor(ctx, spec.name)
        tools.append(AuditedTool(spec, ctx, executor))
    return tools


def _ifind_executor(ctx: RunContext, tool_name: str) -> Executor:
    def run(args: dict[str, Any]) -> ToolOutcome:
        return ctx.gateway.call(
            tool_name,
            args,
            ctx.run.tenant_id,
            ctx.run.user_id,
        )

    return run


def _web_executor(client: Any, tool_name: str, ctx: RunContext) -> Executor:
    def run(args: dict[str, Any]) -> ToolOutcome:
        return client.call(
            tool_name,
            args,
            ctx.run.tenant_id,
            ctx.run.user_id,
        )

    return run