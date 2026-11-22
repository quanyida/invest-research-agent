import json, uuid
from app.tools.ifind_gateway import get_gateway
out = get_gateway().call("ifind.stock_quote", {"symbols": ["300750"], "metrics": ["最新价","涨跌幅","总市值","市盈率TTM","成交额"]}, uuid.uuid4())
print("STATUS", out.status, out.error_code, out.latency_ms, out.cached)
print("QUERY", out.query_text)
if out.evidence:
    print("SECTIONS", len(out.evidence["sections"]), "FACTS", len(out.evidence["facts"]))
    for fact in out.evidence["facts"][:8]:
        print(json.dumps(fact, ensure_ascii=True))
    print("RAW", out.evidence["raw_excerpt"][:300].encode("unicode_escape").decode("ascii"))