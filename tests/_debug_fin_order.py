import json, uuid
from app.tools.ifind_gateway import get_gateway
from app.tools.registry import get_tool
from app.tools.parser import parse_ifind_response

args = {"securities": ["宁德时代"], "period": "2025-09-30", "metrics": ["销售毛利率"], "compare": "同比"}
g = get_gateway()
spec = get_tool("ifind.stock_financials")

def direct(label):
    q, raw, calls = g._call_upstream(spec, args)
    p = parse_ifind_response(raw, q, 1)
    print(label, "DIRECT", q, json.dumps(p["sections"], ensure_ascii=False)[:500])

def via_call(label):
    out = g.call("ifind.stock_financials", args, uuid.uuid4())
    print(label, "CALL", out.query_text, "CACHED", out.cached, json.dumps((out.evidence or {}).get("sections", []), ensure_ascii=False)[:500])

direct("A")
via_call("B")
direct("C")
via_call("D")