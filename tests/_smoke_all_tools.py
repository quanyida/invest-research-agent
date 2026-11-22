import json
import uuid

from app.tools.ifind_gateway import get_gateway

CASES = [
    ("ifind.stock_quote", {"symbols": ["300750"], "metrics": ["最新价", "涨跌幅", "总市值", "市盈率TTM"]}),
    ("ifind.stock_summary", {"query": "宁德时代 最新估值水平与主营业务"}),
    ("ifind.stock_financials", {"securities": ["300750"], "period": "2025Q3", "metrics": ["营业收入", "净利润", "销售毛利率", "ROE"], "compare": "同比"}),
    ("ifind.stock_performance", {"security": "300750", "start": "2026-10-01", "end": "2026-11-17", "metrics": ["收盘价", "涨跌幅", "换手率"]}),
    ("ifind.stock_holders", {"security": "300750", "metrics": ["总股本", "流通股本", "前十大股东"]}),
    ("ifind.stock_events", {"security": "300750", "period": "2026", "metrics": ["分红", "增发", "回购", "解禁", "业绩预告"]}),
    ("ifind.stock_risk", {"security": "300750", "window": "过去1年", "benchmark": "沪深300"}),
    ("ifind.notice_search", {"security": "300750", "query": "海外建厂", "days": 30, "size": 5}),
    ("ifind.news_search", {"security": "300750", "query": "海外建厂", "days": 30, "size": 5}),
    ("ifind.macro", {"query": "中国 CPI 同比", "period": "2026年10月"}),
    ("ifind.index", {"target": "沪深300", "kind": "index", "query": "最近30日涨跌幅"}),
    ("ifind.screen", {"query": "汽车零部件行业市值大于1000亿的股票"}),
]


def main() -> None:
    gateway = get_gateway()
    tenant_id = uuid.uuid4()
    for tool, args in CASES:
        try:
            out = gateway.call(tool, args, tenant_id)
            facts = out.evidence.get("facts", []) if out.evidence else []
            print(f"TOOL {tool} STATUS {out.status} ERR {out.error_code} FACTS {len(facts)} CACHED {out.cached} LATENCY {out.latency_ms}")
            print(f"  QUERY {out.query_text}")
            for fact in facts[:3]:
                print("  FACT " + json.dumps(fact, ensure_ascii=False)[:500])
        except Exception as exc:
            print(f"TOOL {tool} EXCEPTION {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()