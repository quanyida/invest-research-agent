from __future__ import annotations

import json
import os
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8000"
REPORT_DIR = Path(__file__).resolve().parents[1] / "reports"

QUESTIONS = [
    ("F01", "单指标", "宁德时代 2025Q3 毛利率多少"),
    ("F02", "单指标", "宁德时代 2025Q3 营业收入多少"),
    ("F03", "单指标", "宁德时代 2025Q3 净利润多少"),
    ("F04", "单指标", "宁德时代 2025Q3 ROE 多少"),
    ("F05", "单指标", "比亚迪 2025Q3 毛利率多少"),
    ("F06", "单指标", "贵州茅台 2025Q3 净利率多少"),
    ("Y01", "同比环比", "宁德时代 2025Q3 毛利率同比变化"),
    ("Y02", "同比环比", "宁德时代 2025Q3 营业收入同比"),
    ("Y03", "同比环比", "比亚迪 2025Q3 净利润同比"),
    ("Y04", "同比环比", "贵州茅台 2025Q3 营业收入同比"),
    ("C01", "多标的对比", "比亚迪和宁德时代 2025Q3 净利率对比"),
    ("C02", "多标的对比", "宁德时代和比亚迪 2025Q3 毛利率对比"),
    ("C03", "多标的对比", "贵州茅台和五粮液 2025Q3 净利率对比"),
    ("C04", "多标的对比", "宁德时代和比亚迪 2025Q3 营业收入对比"),
    ("Q01", "行情估值", "宁德时代现在多少钱"),
    ("Q02", "行情估值", "宁德时代最新市值和PE"),
    ("Q03", "行情估值", "比亚迪最新价和涨跌幅"),
    ("Q04", "行情估值", "贵州茅台最新市盈率"),
    ("H01", "股东股本", "宁德时代前十大股东"),
    ("H02", "股东股本", "宁德时代总股本和流通股本"),
    ("H03", "股东股本", "比亚迪前十大股东"),
    ("E01", "事件", "宁德时代今年分红方案"),
    ("E02", "事件", "宁德时代最近解禁"),
    ("E03", "事件", "比亚迪今年回购"),
    ("M01", "宏观", "上月 CPI 多少"),
    ("M02", "宏观", "上月 PPI 多少"),
    ("M03", "宏观", "最近一期 GDP 同比"),
    ("U01", "不可答", "帮我看看这份 PDF 的公告原文"),
    ("U02", "不可答", "宁德时代公告原文第 3 页写了什么"),
    ("U03", "不可答", "宁德时代为什么跌"),
]


def parse_stream(response: httpx.Response) -> dict:
    result = {
        "plan": None,
        "tools": [],
        "verify": None,
        "usage": None,
        "done": None,
        "errors": [],
        "answer": "",
        "events": [],
    }
    event = None
    chunks = []
    for line in response.iter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
            result["events"].append(event)
        elif line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if event == "plan":
                result["plan"] = data
            elif event == "tool_end":
                result["tools"].append(data)
            elif event == "verify":
                result["verify"] = data
            elif event == "usage":
                result["usage"] = data
            elif event == "done":
                result["done"] = data
            elif event == "error":
                result["errors"].append(data)
            elif event == "delta":
                chunks.append(str(data.get("text") or ""))
    result["answer"] = "".join(chunks)
    return result


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    only = {part.strip() for part in os.getenv("EVAL_IDS", "").split(",") if part.strip()}
    questions = [item for item in QUESTIONS if not only or item[0] in only]
    if only:
        stamp += "_subset"
    jsonl_path = REPORT_DIR / f"eval_30_{stamp}.jsonl"
    summary_path = REPORT_DIR / f"eval_30_{stamp}.md"

    timeout = httpx.Timeout(120.0, connect=5.0)
    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        health = client.get("/healthz")
        print("HEALTH", health.status_code)
        if health.status_code != 200:
            print("服务未启动，请先运行 uvicorn")
            return 2
        email = "eval-" + uuid.uuid4().hex[:8] + "@example.com"
        reg = client.post("/api/v1/auth/register", json={"email": email, "password": "test-pass-123", "tenant_name": "eval 30"})
        print("REGISTER", reg.status_code)
        if reg.status_code != 200:
            print(reg.text[:500])
            return 2

        results = []
        for index, (qid, category, question) in enumerate(questions, start=1):
            started = time.monotonic()
            error = None
            payload = None
            try:
                with client.stream(
                    "POST",
                    "/api/v1/chat",
                    json={"conversation_id": None, "client_request_id": str(uuid.uuid4()), "message": question},
                ) as response:
                    if response.status_code != 200:
                        error = f"HTTP {response.status_code}"
                        response.read()
                    else:
                        payload = parse_stream(response)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            elapsed = round(time.monotonic() - started, 2)
            row = {
                "id": qid,
                "category": category,
                "question": question,
                "elapsed_s": elapsed,
                "error": error,
                "plan": (payload or {}).get("plan"),
                "tools": (payload or {}).get("tools"),
                "verify": (payload or {}).get("verify"),
                "done": (payload or {}).get("done"),
                "errors": (payload or {}).get("errors"),
                "answer": (payload or {}).get("answer", ""),
            }
            results.append(row)
            with jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            status = (row["done"] or {}).get("status", "ERROR")
            verify = (row["verify"] or {}).get("passed")
            tools = ",".join(str(item.get("tool")) for item in (row["tools"] or [])) or "-"
            preview = row["answer"].replace("\n", " ")[:90]
            print(f"[{index:02d}/{len(questions)}] {qid} {category} status={status} verify={verify} {elapsed:.1f}s tools={tools} {preview}")
            time.sleep(1.0)

    total = len(results)
    done = sum(1 for item in results if ((item["done"] or {}).get("status") == "done"))
    degraded = sum(1 for item in results if ((item["done"] or {}).get("status") == "degraded"))
    errors = sum(1 for item in results if item["error"])
    verify_pass = sum(1 for item in results if (item["verify"] or {}).get("passed") is True)
    times = sorted(item["elapsed_s"] for item in results)
    p95 = times[min(len(times) - 1, int(len(times) * 0.95))] if times else 0
    lines = [
        "# 30 题评测报告",
        "",
        f"- 时间：{stamp}",
        f"- 总数：{total}",
        f"- done：{done}",
        f"- degraded：{degraded}",
        f"- 请求异常：{errors}",
        f"- verify 通过：{verify_pass}",
        f"- 平均耗时：{round(statistics.mean(times), 2)}s",
        f"- P95 耗时：{p95}s",
        "",
        "| ID | 类别 | 状态 | verify | 耗时 | 工具 | 问题 | 回答摘要 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        status = (item["done"] or {}).get("status", "ERROR")
        verify = (item["verify"] or {}).get("passed")
        tools = ", ".join(str(tool.get("tool")) for tool in (item["tools"] or [])) or "-"
        answer = item["answer"].replace("\n", " ").replace("|", "/")[:160]
        lines.append(f"| {item['id']} | {item['category']} | {status} | {verify} | {item['elapsed_s']}s | {tools} | {item['question']} | {answer} |")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("SUMMARY", json.dumps({"total": total, "done": done, "degraded": degraded, "errors": errors, "verify_pass": verify_pass, "avg_s": round(statistics.mean(times), 2), "p95_s": p95}, ensure_ascii=False))
    print("JSONL", jsonl_path)
    print("REPORT", summary_path)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
