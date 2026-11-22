import json, uuid
from app.tools.ifind_gateway import get_gateway
out = get_gateway().call("ifind.stock_financials", {"securities": ["宁德时代"], "period": "2025-09-30", "metrics": ["销售毛利率"], "compare": "同比"}, uuid.uuid4())
print("STATUS", out.status, out.query_text)
ev = out.evidence or {}
print("as_of_label", ev.get("as_of_label"), "period_end", ev.get("period_end"), "data_as_of", ev.get("data_as_of"))
print("query", ev.get("query_text"))
for f in ev.get("facts") or []:
    print(json.dumps(f, ensure_ascii=False))
