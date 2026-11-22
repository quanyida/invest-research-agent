import json
from app.tools.ifind_gateway import get_gateway
g=get_gateway()
for name,tool,args in [
 ("fin","get_stock_financials",{"query":"宁德时代 2025年三季报 营业收入 销售毛利率 净利润 同比"}),
 ("news","search_news",{"query":"宁德时代 海外建厂","time_start":"2026-10-18","time_end":"2026-11-17","size":3}),
]:
 raw=g._rpc_call("stock" if name=="fin" else "news",tool,args)
 open(f"tests/_raw_{name}.json","w",encoding="utf-8").write(json.dumps(raw,ensure_ascii=True,indent=2))
 print(name, len(json.dumps(raw,ensure_ascii=True)))