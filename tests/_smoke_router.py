from app.agent.router import route_question


cases = [
    ("宁德时代 2025Q3 毛利率多少，同比变化？", "financials", "ifind.stock_financials", "销售毛利率"),
    ("比亚迪和宁德时代 2025Q3 净利率对比", "compare", "ifind.stock_financials", "销售净利率"),
    ("宁德时代最近有什么新闻", "news", "ifind.news_search", None),
    ("上月 CPI 多少", "macro", "ifind.macro", None),
]

for question, intent, tool, metric in cases:
    plan = route_question(question)
    assert plan.intent == intent, (question, plan.intent, intent, [item.name for item in plan.tools])
    assert plan.tools and plan.tools[0].name == tool, (question, plan.intent, [item.name for item in plan.tools])
    if metric:
        assert metric in plan.metrics, (question, plan.metrics, metric)
    print("OK", intent, tool, question)

print("router smoke passed")