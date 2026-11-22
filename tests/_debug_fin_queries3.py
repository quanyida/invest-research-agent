import json
from app.tools.ifind_gateway import get_gateway
from app.tools.parser import parse_ifind_response

queries = [
    "宁德时代 2025年三季报 销售毛利率 单季度-销售毛利率同比增长率",
    "宁德时代 2025年三季报 销售毛利率 销售毛利率(同比增长率)",
    "宁德时代 2025年三季报 销售毛利率 毛利率同比",
    "宁德时代 2025年三季报 销售毛利率 营业收入 同比",
]
g = get_gateway()
for q in queries:
    raw = g._rpc_call("stock", "get_stock_financials", {"query": q})
    p = parse_ifind_response(raw, q, 1)
    print("QUERY", q)
    print("SECTIONS", json.dumps(p["sections"], ensure_ascii=False)[:2500])