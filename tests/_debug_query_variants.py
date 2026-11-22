import json
from app.tools.ifind_gateway import get_gateway
from app.tools.parser import parse_ifind_response

queries = [
    "宁德时代 2025年三季报 2024年三季报 销售毛利率",
    "宁德时代 2025-09-30 2024-09-30 销售毛利率",
    "宁德时代 2025年三季报 销售毛利率 2024年三季报 销售毛利率",
    "宁德时代 2025年三季报 销售毛利率 同比变化",
]
g = get_gateway()
for q in queries:
    print("=" * 20)
    print("QUERY", q)
    try:
        raw = g._rpc_call("stock", "get_stock_financials", {"query": q})
        p = parse_ifind_response(raw, q, 1)
        print("SECTIONS", json.dumps(p["sections"], ensure_ascii=False)[:4000])
        print("FACTS", json.dumps(p["facts"], ensure_ascii=False)[:2000])
    except Exception as exc:
        print("ERROR", type(exc).__name__, str(exc)[:500])
