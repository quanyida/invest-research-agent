from app.agent.evidence_collector import EvidenceCollector
from app.tools.ifind_gateway import ToolOutcome


def _outcome(value: str) -> ToolOutcome:
    return ToolOutcome(
        status="ok",
        evidence={
            "as_of_label": "test",
            "facts": [
                {
                    "fact_id": "local-1",
                    "entity": "SMTC",
                    "period": "2026-09-30",
                    "metric": "测试指标",
                    "value": value,
                    "unit": "%",
                }
            ],
        },
        error_code=None,
        latency_ms=1,
        cached=False,
        query_text="test",
        upstream_calls=[],
    )


def test_fact_ids_are_unique_across_tools_in_one_turn():
    collector = EvidenceCollector(turn=1)
    collector.add_outcome("ifind.global_profile", {"query": "a"}, _outcome("1"))
    collector.add_outcome("ifind.global_financials", {"query": "b"}, _outcome("2"))

    ids = [
        fact["fact_id"]
        for evidence in collector.public_evidences()
        for fact in evidence["facts"]
    ]
    assert ids == ["E1-1", "E1-2"]
