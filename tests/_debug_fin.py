import json
from app.tools.ifind_gateway import get_gateway
from app.tools.rewrite import build_query
from app.tools.parser import parse_ifind_response

args = {"securities": ["宁德时代"], "period": "2025-09-30", "metrics": ["销售毛利率"], "compare": "同比"}
query = build_query("ifind.stock_financials", args)
g = get_gateway()
raw = g._rpc_call("stock", "get_stock_financials", {"query": query})
parsed = parse_ifind_response(raw, entity_hint=query, evidence_seq=1)
print("QUERY", query)
print("SECTIONS", json.dumps(parsed["sections"], ensure_ascii=False, indent=2)[:10000])
print("FACTS", json.dumps(parsed["facts"], ensure_ascii=False, indent=2)[:10000])