from decimal import Decimal

from app.tools.parser import _metric_from_column, _normalize_unit_hint, _parse_number_with_unit, parse_ifind_response

def test_unit_hint_extracts_trailing_unit():
    assert _normalize_unit_hint("十大股东排名：第1名；单位：%") == "%"
    assert _normalize_unit_hint("单位：股") == "股"

def test_yi_suffix_respects_default_unit():
    assert _parse_number_with_unit("46.2725亿", "股") == (Decimal("46.2725"), "亿股")
    assert _parse_number_with_unit("6000.0607万", "股") == (Decimal("6000.0607"), "万股")
    assert _parse_number_with_unit("46.2725亿", "元") == (Decimal("46.2725"), "亿元")

def test_metric_keeps_shareholder_rank():
    assert _metric_from_column("股东持股比例（十大股东排名：第3名；单位：%）") == "股东持股比例（第3名）"

def test_annual_dividend_metric_is_not_treated_as_period():
    markdown = (
        "|证券代码|证券简称|年度累计单位分红（单位：元）|年度分红比例（单位：%）|年度累计分红总额（单位：元）|\n"
        "|---|---|---|---|---|\n"
        "|300750.SZ|宁德时代|1.411||61.8023亿|\n"
    )
    parsed = parse_ifind_response({"text": markdown}, entity_hint="宁德时代")
    metrics = {(fact["metric"], fact["value"], fact["unit"]) for fact in parsed["facts"]}
    assert ("年度累计单位分红", "1.411", "元") in metrics
    assert ("年度累计分红总额", "61.8023", "亿元") in metrics
