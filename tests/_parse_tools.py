import json
from app.tools.parser import parse_ifind_response
for name in ("fin","news","ifind"):
 raw=json.load(open(f"tests/_raw_{name}.json",encoding="utf-8"))
 p=parse_ifind_response(raw,entity_hint="300750",evidence_seq=1)
 print("==",name,"sections",len(p["sections"]),"facts",len(p["facts"]),"quality",p["parse_quality"],"period",p["period_end"])
 for f in p["facts"][:8]: print(json.dumps(f,ensure_ascii=True))