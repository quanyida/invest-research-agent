import json
from app.tools.ifind_gateway import get_gateway
from app.tools.registry import get_tool
from app.tools.parser import parse_ifind_response

args = {"securities": ["宁德时代"], "period": "2025-09-30", "metrics": ["销售毛利率"], "compare": "同比"}
g = get_gateway()
spec = get_tool("ifind.stock_financials")
query, raw, calls = g._call_upstream(spec, args)
print("QUERY", query)
print("RAW", json.dumps(raw, ensure_ascii=False)[:5000])
parsed = parse_ifind_response(raw, entity_hint=query, evidence_seq=1)
print("SECTIONS", json.dumps(parsed["sections"], ensure_ascii=False)[:5000])
print("FACTS", json.dumps(parsed["facts"], ensure_ascii=False)[:5000])