import json
from app.tools.ifind_gateway import get_gateway
from app.tools.parser import parse_ifind_response

queries = [
    "宁德时代 2025年三季报 净利润 同比",
    "宁德时代 2025年三季报 营业收入 同比",
    "宁德时代 2025年三季报 净利润 营业收入 销售毛利率 同比",
    "宁德时代 2024年三季报 销售毛利率",
]
g = get_gateway()
for q in queries:
    raw = g._rpc_call("stock", "get_stock_financials", {"query": q})
    p = parse_ifind_response(raw, q, 1)
    print("QUERY", q)
    print("FACTS", json.dumps(p["facts"], ensure_ascii=False)[:3000])