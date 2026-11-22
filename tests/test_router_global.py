from app.agent.router import extract_global_entities, route_question


def test_smtc_deep_analysis_routes_to_global_analysis():
    plan = route_question("深度分析下美股SMTC的业务情况和AI时代下的预期")

    assert plan.intent == "global_analysis"
    assert plan.securities == ["SMTC"]
    assert [tool.name for tool in plan.tools] == [
        "ifind.global_profile",
        "ifind.global_financials",
    ]


def test_generic_us_ticker_is_recognized_with_market_context():
    plan = route_question("分析一下美股CRWD的业务和最新财务")

    assert plan.intent == "global_analysis"
    assert plan.securities == ["CRWD"]


def test_ai_is_not_treated_as_a_ticker():
    assert "AI" not in extract_global_entities("美股AI时代下的投资机会")
