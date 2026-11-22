import json, uuid
from app.tools.ifind_gateway import get_gateway

args = {"securities": ["宁德时代"], "period": "2025-09-30", "metrics": ["销售毛利率"], "compare": "同比"}
out = get_gateway().call("ifind.stock_financials", args, uuid.uuid4())
print("STATUS", out.status, "ERR", out.error_code, "CACHED", out.cached, "QUERY", out.query_text)
print("FACTS", json.dumps((out.evidence or {}).get("facts", []), ensure_ascii=False, indent=2)[:12000])
print("SECTIONS", json.dumps((out.evidence or {}).get("sections", []), ensure_ascii=False, indent=2)[:12000])