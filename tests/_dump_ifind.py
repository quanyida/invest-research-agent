import json
from app.tools.ifind_gateway import get_gateway
g=get_gateway()
raw=g._rpc_call("stock","stock_highfreq_quotes",{"symbols":"300750","indicators":"最新价,涨跌幅,总市值,市盈率TTM,成交额","data_mode":"real_time"})
open("tests/_raw_ifind.json","w",encoding="utf-8").write(json.dumps(raw,ensure_ascii=True,indent=2))
print(type(raw), list(raw.keys()) if isinstance(raw,dict) else len(raw))