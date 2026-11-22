import json
from app.tools.parser import extract_texts
for name in ("fin","news"):
 raw=json.load(open(f"tests/_raw_{name}.json",encoding="utf-8"))
 texts=extract_texts(raw)
 print("==",name,"texts",len(texts))
 for text in texts:
  p=json.loads(text)
  print("keys",p.keys(),"data type",type(p.get("data")))
  d=p.get("data")
  if isinstance(d,dict):
   print("dkeys",d.keys())
   for k,v in d.items(): print(" ",k,type(v),str(v)[:200].encode("unicode_escape").decode("ascii"))
  else:
   print("data",str(d)[:500].encode("unicode_escape").decode("ascii"))