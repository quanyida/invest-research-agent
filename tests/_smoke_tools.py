import json, uuid
from app.tools.ifind_gateway import get_gateway
for tool,args in [
 ("ifind.stock_financials", {"securities":["300750"],"period":"2025Q3","metrics":["营业收入","销售毛利率","净利润"],"compare":"同比"}),
 ("ifind.news_search", {"security":"300750","query":"海外建厂","days":30,"size":5}),
]:
 out=get_gateway().call(tool,args,uuid.uuid4())
 print("TOOL",tool,"STATUS",out.status,out.error_code,"FACTS",len(out.evidence["facts"]) if out.evidence else 0)
 if out.evidence:
  for f in out.evidence["facts"][:8]: print(json.dumps(f,ensure_ascii=True))
  print("RAW",out.evidence["raw_excerpt"][:300].encode("unicode_escape").decode("ascii"))