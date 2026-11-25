from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterator

from sqlalchemy import select

from app.agent.budget import Budget
from app.agent.persist import RunFenced, persist_turn
from app.agent.evidence_collector import public_evidence_payload
from app.agent.context import (
    build_compose_messages,
    build_plan_messages,
    build_repair_messages,
    degraded_answer,
    no_evidence_answer,
    unsupported_answer,
)
from app.agent.router import Plan, PlannedTool, detect_domain, extract_entities, route_question
from app.agent.verify import VerifyResult, repair_citations, verify_answer
from app.config import get_settings
from app.llm.client import LlmError, LlmResult, get_llm_client
from app.tools.ifind_gateway import ToolOutcome, get_gateway
from app.tools.registry import get_tool
from app.tools.rewrite import validate_args

logger = logging.getLogger(__name__)

@dataclass
class ChatRun:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    request_id: uuid.UUID
    question: str
    history: list[dict[str, str]]
    turn: int = 1
    run_id: uuid.UUID | None = None
    worker_id: str | None = None
    attempt: int | None = None
    timeout_seconds: float | None = None
    convergence_seconds: float | None = None

def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value

def _args_hash(args: dict[str, Any]) -> str:
    raw = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def _plan_payload(plan: Plan) -> dict[str, Any]:
    return {
        "intent": plan.intent,
        "tools": [{"name": item.name, "args": item.args} for item in plan.tools],
        "note": plan.note,
        "unsupported": plan.unsupported,
    }

def _verify_payload(result: VerifyResult) -> dict[str, Any]:
    return {
        "passed": result.passed,
        "issues": result.issues[:20],
        "unverified": result.unverified[:20],
        "cited_fact_ids": result.cited_fact_ids[:100],
        "number_count": result.number_count,
    }

def _public_evidence(evidence: dict[str, Any], fact_limit: int = 80) -> dict[str, Any]:
    return public_evidence_payload(evidence, fact_limit=fact_limit)

class AgentLoop:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.gateway = get_gateway()
        self.llm = get_llm_client()

    @staticmethod
    def _chunks(text: str, size: int = 40) -> Iterator[str]:
        for index in range(0, len(text), size):
            yield text[index : index + size]

    @staticmethod
    def _clean_text(text: str) -> str:
        value = text.strip()
        if value.startswith("```"):
            lines = value.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            value = "\n".join(lines).strip()
        return value

    def stream(self, run: ChatRun) -> Iterator[tuple[str, dict[str, Any]]]:
        budget = Budget(
            timeout_seconds=float(run.timeout_seconds or self.settings.task_timeout_seconds),
            max_models=self.settings.model_max_attempts,
            convergence_seconds=run.convergence_seconds,
        )
        trace: list[dict[str, Any]] = []
        state: dict[str, Any] = {
            "tool_runs": [],
            "evidences": [],
            "errors": [],
            "replans": 0,
            "plan": {},
            "verify": {},
        }
        audits: list[dict[str, Any]] = []

        plan_started = time.monotonic()
        plan = route_question(run.question, run.history)
        trace.append({"state": "PLAN", "ms": int((time.monotonic() - plan_started) * 1000), "detail": plan.intent})
        if plan.needs_llm:
            domain = detect_domain(run.question, plan.securities or extract_entities(run.question))
            yield ("plan", {"intent": "routing", "tools": [], "replans": 0, "note": "规则未命中，交给模型选工具"})
            plan = yield from self._llm_plan(run.question, budget, audits, trace, domain)
        state["plan"] = _plan_payload(plan)
        yield (
            "plan",
            {
                "intent": plan.intent,
                "tools": [{"name": item.name, "args": item.args} for item in plan.tools],
                "replans": state["replans"],
                "note": plan.note,
            },
        )

        if plan.unsupported:
            answer = unsupported_answer(plan.note)
            verify_result = VerifyResult(passed=True)
            state["verify"] = _verify_payload(verify_result)
            yield ("verify", {"passed": True, "unverified": [], "repaired": False})
            yield from self._finish(run, answer, "done", verify_result, plan, state, audits, budget, trace)
            return

        yield from self._execute_plan(plan, run, budget, state, trace, audits)
        if not state["evidences"] and state["replans"] < 1 and budget.can_start_tool() and not budget.expired():
            fallback = self._fallback_plan(plan, run.question)
            if fallback.tools:
                state["replans"] = 1
                yield (
                    "plan",
                    {
                        "intent": fallback.intent,
                        "tools": [{"name": item.name, "args": item.args} for item in fallback.tools],
                        "replans": state["replans"],
                        "note": "首轮未取到可用证据，做一次回退取数",
                    },
                )
                yield from self._execute_plan(fallback, run, budget, state, trace, audits)

        evidences = state["evidences"]
        if not evidences:
            answer = no_evidence_answer(state["errors"])
            verify_result = VerifyResult(passed=False, issues=["没有可用证据"])
            state["verify"] = _verify_payload(verify_result)
            yield ("verify", {"passed": False, "unverified": [{"fact_id": "", "reason": "没有可用证据"}], "repaired": False})
            yield from self._finish(run, answer, "degraded", verify_result, plan, state, audits, budget, trace)
            return

        compose_started = time.monotonic()
        box = yield from self._run_llm(
            build_compose_messages(run.question, evidences, run.history),
            purpose="compose",
            temperature=0.0,
            max_tokens=2048,
            cap=65.0,
            budget=budget,
            audits=audits,
            trace=trace,
        )
        trace.append({"state": "COMPOSE", "ms": int((time.monotonic() - compose_started) * 1000), "detail": "buffered"})
        result: LlmResult | None = box.get("result")
        if result is None:
            error = box.get("error")
            reason = f"模型调用失败：{getattr(error, 'code', 'UNKNOWN')}"
            answer = degraded_answer(evidences, reason)
            verify_result = verify_answer(answer, evidences)
            state["verify"] = _verify_payload(verify_result)
            yield (
                "verify",
                {
                    "passed": verify_result.passed,
                    "unverified": verify_result.unverified[:12],
                    "repaired": False,
                    "fallback": "evidence_excerpt",
                },
            )
            yield from self._finish(run, answer, "degraded", verify_result, plan, state, audits, budget, trace)
            return

        draft = self._clean_text(result.content)
        draft, citation_repairs = repair_citations(draft, evidences)
        verify_result = verify_answer(draft, evidences)
        repaired = False
        if not verify_result.passed and budget.models_remaining > 0 and budget.remaining() > 5:
            repair_started = time.monotonic()
            repair_box = yield from self._run_llm(
                build_repair_messages(run.question, evidences, draft, verify_result.issues, run.history),
                purpose="repair",
                temperature=0.0,
                max_tokens=1536,
                cap=20.0,
                budget=budget,
                audits=audits,
                trace=trace,
            )
            trace.append({"state": "REPAIR", "ms": int((time.monotonic() - repair_started) * 1000), "detail": "one pass"})
            repair_result: LlmResult | None = repair_box.get("result")
            if repair_result is not None:
                repaired_text = self._clean_text(repair_result.content)
                repaired_text, repair_count = repair_citations(repaired_text, evidences)
                citation_repairs += repair_count
                repaired_verify = verify_answer(repaired_text, evidences)
                repaired = True
                draft = repaired_text
                verify_result = repaired_verify

        if not verify_result.passed:
            reason = "事实校验未通过：" + "；".join(verify_result.issues[:3])
            answer = degraded_answer(evidences, reason)
            status = "degraded"
        else:
            answer = draft
            status = "done"

        state["verify"] = _verify_payload(verify_result)
        state["verify"]["citation_repairs"] = citation_repairs
        yield (
            "verify",
            {
                "passed": verify_result.passed,
                "unverified": verify_result.unverified[:12],
                "repaired": repaired,
                "citation_repairs": citation_repairs,
                "issues": verify_result.issues[:12],
            },
        )
        yield from self._finish(run, answer, status, verify_result, plan, state, audits, budget, trace)
    def _llm_plan(
        self,
        question: str,
        budget: Budget,
        audits: list[dict[str, Any]],
        trace: list[dict[str, Any]],
        domain: str | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        box = yield from self._run_llm(
            build_plan_messages(question, domain),
            purpose="plan",
            temperature=0.0,
            max_tokens=512,
            cap=20.0,
            budget=budget,
            audits=audits,
            trace=trace,
        )
        result: LlmResult | None = box.get("result")
        if result is None:
            error = box.get("error")
            note = f"模型规划失败：{getattr(error, 'code', 'UNKNOWN')}"
            return Plan("unknown", needs_llm=False, note=note, raw_question=question)
        raw = self._clean_text(result.content)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return Plan("llm", needs_llm=False, note="模型规划结果不是合法 JSON", raw_question=question)
        tools: list[PlannedTool] = []
        for item in (payload.get("tools") or [])[:3]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            args = item.get("args")
            if not name or not isinstance(args, dict):
                continue
            try:
                spec = get_tool(name)
                properties = set((spec.schema.get("properties") or {}).keys())
                if any(key not in properties for key in args):
                    raise ValueError("unknown argument")
                for required in spec.schema.get("required") or []:
                    if required not in args:
                        raise ValueError(f"missing {required}")
                validate_args(name, args)
            except (KeyError, ValueError, TypeError):
                continue
            tools.append(PlannedTool(name, args))
        if not tools:
            return Plan("llm", needs_llm=False, note="模型未选出可用工具", raw_question=question)
        return Plan("llm", tools=tools, note=str(payload.get("reason") or "模型规划"), raw_question=question)

    def _fallback_plan(self, plan: Plan, question: str) -> Plan:
        securities = plan.securities or extract_entities(question)
        if detect_domain(question, securities) == "global":
            entity = securities[0] if securities else question
            tried = {item.name for item in plan.tools}
            if "ifind.global_profile" not in tried:
                return Plan(
                    "fallback",
                    tools=[PlannedTool("ifind.global_profile", {"query": f"{entity} 所属行业 上市日期 首发价格"})],
                    securities=securities[:1],
                    note="港美股结构化工具未取到数据，回退到基本资料。",
                    raw_question=question,
                )
            return Plan(
                "fallback",
                tools=[PlannedTool("ifind.global_financials", {"query": f"{entity} 最新报告期 营业收入 净利润"})],
                securities=securities[:1],
                note="港美股基本资料未取到数据，回退到核心财务指标。",
                raw_question=question,
            )
        if securities:
            query = f"{securities[0]} 最新财务数据、估值与经营概况"
            return Plan(
                "fallback",
                tools=[PlannedTool("ifind.stock_summary", {"query": query})],
                securities=securities[:1],
                note="结构化工具未取到数据，回退到股票摘要。",
                raw_question=question,
            )
        if plan.intent in {"macro", "index"}:
            return Plan(
                "fallback",
                tools=[PlannedTool("ifind.stock_summary", {"query": question})],
                note="首轮未取到结构化数据，回退到摘要检索。",
                raw_question=question,
            )
        return Plan("fallback", note="没有可用的回退工具。", raw_question=question)
    def _execute_plan(
        self,
        plan: Plan,
        run: ChatRun,
        budget: Budget,
        state: dict[str, Any],
        trace: list[dict[str, Any]],
        audits: list[dict[str, Any]],
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        candidates: list[dict[str, Any]] = []
        for item in plan.tools:
            if not budget.can_start_tool():
                break
            try:
                spec = get_tool(item.name)
            except KeyError:
                state["errors"].append(f"未知工具 {item.name}")
                continue
            budget.take_tool()
            candidates.append(
                {
                    "item": item,
                    "spec": spec,
                    "step": len(state["tool_runs"]) + len(candidates) + 1,
                }
            )

        for offset in range(0, len(candidates), 2):
            batch = candidates[offset : offset + 2]
            if not batch or budget.expired():
                break
            for candidate in batch:
                yield (
                    "tool_start",
                    {
                        "step": candidate["step"],
                        "tool": candidate["item"].name,
                        "args": candidate["item"].args,
                    },
                )
            started = time.monotonic()
            executor = ThreadPoolExecutor(max_workers=min(2, len(batch)))
            futures = {
                executor.submit(
                    self.gateway.call,
                    candidate["item"].name,
                    candidate["item"].args,
                    run.tenant_id,
                    run.user_id,
                ): candidate
                for candidate in batch
            }
            pending = set(futures)
            while pending:
                timeout = min(10.0, max(0.2, budget.remaining()))
                done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                if pending:
                    if budget.expired():
                        for future in pending:
                            future.cancel()
                        break
                    yield ("ping", {"stage": "tool", "remaining_ms": int(budget.remaining() * 1000)})
            executor.shutdown(wait=False, cancel_futures=True)

            for candidate in batch:
                future = next((key for key in futures if futures[key] is candidate), None)
                outcome: ToolOutcome | None = None
                if future is not None:
                    try:
                        outcome = future.result(timeout=0)
                    except Exception as exc:
                        state["errors"].append(f"{candidate['item'].name}: {type(exc).__name__}")
                        outcome = ToolOutcome(
                            "upstream_error",
                            None,
                            f"UPSTREAM_ERROR:{type(exc).__name__}",
                            int((time.monotonic() - started) * 1000),
                            False,
                            "",
                            [],
                        )
                if outcome is None:
                    continue
                evidence = None
                if outcome.status == "ok" and outcome.evidence:
                    seq = len(state["evidences"]) + 1
                    evidence = copy.deepcopy(outcome.evidence)
                    evidence["id"] = f"E{seq}"
                    evidence["seq"] = seq
                    evidence["channel"] = candidate["spec"].channel
                    evidence["source_kind"] = candidate["spec"].source_kind
                    evidence["source_kind_label"] = candidate["spec"].source_kind_label
                    facts = evidence.get("facts") or []
                    fact_id_map: dict[str, str] = {}
                    for index, fact in enumerate(facts, start=1):
                        old_fact_id = str(fact.get("fact_id") or "")
                        new_fact_id = f"E{seq}-{index}"
                        if old_fact_id:
                            fact_id_map[old_fact_id] = new_fact_id
                        fact["fact_id"] = new_fact_id
                        fact["evidence_id"] = f"E{seq}"
                    for fact in facts:
                        if fact.get("derived_from"):
                            fact["derived_from"] = [
                                fact_id_map.get(str(item), str(item))
                                for item in fact["derived_from"]
                            ]
                    evidence["facts"] = facts
                    state["evidences"].append(evidence)
                else:
                    reason = outcome.error_code or outcome.status
                    state["errors"].append(f"{candidate['item'].name}: {reason}")

                upstream_tool = (
                    str(evidence.get("upstream_tool"))
                    if evidence
                    else str(candidate["spec"].upstream)
                )
                audit = {
                    "kind": "tool",
                    "step": candidate["step"],
                    "tool": candidate["item"].name,
                    "server": candidate["spec"].server,
                    "upstream_tool": upstream_tool,
                    "args": candidate["item"].args,
                    "args_hash": (
                        str(evidence.get("args_hash"))
                        if evidence
                        else _args_hash(candidate["item"].args)
                    ),
                    "query_text": outcome.query_text,
                    "status": outcome.status,
                    "cached": outcome.cached,
                    "latency_ms": outcome.latency_ms,
                    "error_code": outcome.error_code,
                    "upstreams": [item.get("tool") for item in outcome.upstream_calls],
                }
                audits.append(audit)
                state["tool_runs"].append(audit)
                yield (
                    "tool_end",
                    {
                        "step": candidate["step"],
                        "tool": candidate["item"].name,
                        "status": outcome.status,
                        "cached": outcome.cached,
                        "latency_ms": outcome.latency_ms,
                        "error_code": outcome.error_code,
                    },
                )
                if evidence is not None:
                    yield (
                        "evidence",
                        {
                            "evidence": _public_evidence(evidence),
                            "citations": [fact.get("fact_id") for fact in evidence.get("facts") or []],
                        },
                    )

    def _run_llm(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        temperature: float,
        max_tokens: int,
        cap: float,
        budget: Budget,
        audits: list[dict[str, Any]],
        trace: list[dict[str, Any]],
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        if budget.models_remaining <= 0 or budget.expired():
            return {"result": None, "error": LlmError("BUDGET", "模型预算已耗尽", attempts=0)}
        timeout = budget.model_timeout(cap)
        if timeout <= 1.0:
            return {"result": None, "error": LlmError("TIMEOUT", "剩余时间不足", attempts=0)}

        started = time.monotonic()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            self.llm.complete,
            messages,
            purpose=purpose,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_attempts=budget.models_remaining,
        )
        while True:
            done, _ = wait({future}, timeout=min(10.0, max(0.2, budget.remaining())))
            if done:
                break
            if budget.expired() or future.cancel():
                executor.shutdown(wait=False, cancel_futures=True)
                elapsed = int((time.monotonic() - started) * 1000)
                error = LlmError("TIMEOUT", "模型调用超出总 deadline", attempts=0)
                audits.append(
                    {
                        "kind": "llm",
                        "purpose": purpose,
                        "model": self.settings.llm_model,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "reasoning_tokens": 0,
                        "finish_reason": None,
                        "latency_ms": elapsed,
                        "status": "timeout",
                        "attempts": 0,
                    }
                )
                return {"result": None, "error": error}
            yield ("ping", {"stage": purpose, "remaining_ms": int(budget.remaining() * 1000)})
        executor.shutdown(wait=False, cancel_futures=True)
        try:
            result = future.result(timeout=0)
            budget.take_model(result.attempts)
            audits.append(
                {
                    "kind": "llm",
                    "purpose": purpose,
                    "model": result.model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "reasoning_tokens": result.reasoning_tokens,
                    "finish_reason": result.finish_reason,
                    "latency_ms": result.latency_ms,
                    "status": "ok",
                    "attempts": result.attempts,
                }
            )
            return {"result": result, "error": None}
        except LlmError as exc:
            budget.take_model(max(1, exc.attempts))
            audits.append(
                {
                    "kind": "llm",
                    "purpose": purpose,
                    "model": self.settings.llm_model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "finish_reason": None,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "status": "error",
                    "error_code": exc.code,
                    "attempts": max(1, exc.attempts),
                }
            )
            return {"result": None, "error": exc}
        except Exception as exc:
            budget.take_model(1)
            audits.append(
                {
                    "kind": "llm",
                    "purpose": purpose,
                    "model": self.settings.llm_model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "finish_reason": None,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "status": "error",
                    "error_code": type(exc).__name__,
                    "attempts": 1,
                }
            )
            return {
                "result": None,
                "error": LlmError(type(exc).__name__, str(exc), attempts=1),
            }
    def _finish(
        self,
        run: ChatRun,
        answer: str,
        status: str,
        verify_result: VerifyResult,
        plan: Plan,
        state: dict[str, Any],
        audits: list[dict[str, Any]],
        budget: Budget,
        trace: list[dict[str, Any]],
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        tool_audits = [item for item in audits if item.get("kind") == "tool"]
        llm_audits = [item for item in audits if item.get("kind") == "llm"]
        token_prompt = sum(int(item.get("prompt_tokens") or 0) for item in llm_audits)
        token_completion = sum(int(item.get("completion_tokens") or 0) for item in llm_audits)
        usage = {
            **budget.snapshot(),
            "prompt_tokens": token_prompt,
            "completion_tokens": token_completion,
            "total_tokens": token_prompt + token_completion,
        }
        meta = {
            "request_id": str(run.request_id),
            "plan": _plan_payload(plan),
            "verify": _verify_payload(verify_result),
            "trace": trace,
            "errors": state["errors"][:20],
            "budget": usage,
            "evidence_count": len(state["evidences"]),
            "tool_runs": len(tool_audits),
            "llm_runs": len(llm_audits),
        }

        try:
            _, run_finalized = persist_turn(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                conversation_id=run.conversation_id,
                message_id=run.message_id,
                request_id=run.request_id,
                question=run.question,
                answer=answer,
                status=status,
                meta=meta,
                evidences=state["evidences"],
                tool_audits=tool_audits,
                llm_audits=llm_audits,
                usage=usage,
                elapsed_ms=budget.elapsed_ms(),
                run_id=run.run_id,
                worker_id=run.worker_id,
                attempt=run.attempt,
            )
        except RunFenced:
            raise
        except Exception as exc:
            logger.exception("chat persistence failed")
            yield (
                "error",
                {
                    "code": "PERSIST_FAILED",
                    "message": f"回答已生成但保存失败：{type(exc).__name__}",
                },
            )
            return

        for chunk in self._chunks(answer):
            yield ("delta", {"text": chunk})
        yield ("usage", usage)
        yield (
            "done",
            {
                "status": status,
                "message_id": str(run.message_id),
                "conversation_id": str(run.conversation_id),
                "request_id": str(run.request_id),
                "elapsed_ms": budget.elapsed_ms(),
                "run_finalized": run_finalized,
            },
        )
