import json
from app.tools.parser import parse_ifind_response
raw=json.load(open("tests/_raw_ifind.json",encoding="utf-8"))
p=parse_ifind_response(raw,entity_hint="300750",evidence_seq=1)
print("sections",len(p["sections"]),"facts",len(p["facts"]),"quality",p["parse_quality"])
for f in p["facts"]:
    print(json.dumps(f,ensure_ascii=True))