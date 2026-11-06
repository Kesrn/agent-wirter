"""Known schema drift comparison behavior."""

from scripts.check_schema_parity import evaluate_drift, format_resolved_notice


def _entry(object_name: str, description: str = "known legacy mismatch") -> dict[str, str]:
    return {
        "type": "nullability",
        "table": "projects",
        "object": object_name,
        "description": description,
    }


def test_known_baseline_drift_does_not_fail():
    entry = _entry("created_at")
    new, disappeared, status = evaluate_drift([entry], [entry])

    assert new == []
    assert disappeared == []
    assert status == 0


def test_new_drift_fails():
    known = _entry("created_at")
    added = _entry("updated_at", "untracked new mismatch")
    new, disappeared, status = evaluate_drift([known, added], [known])

    assert new == [added]
    assert disappeared == []
    assert status == 1


def test_disappeared_baseline_drift_only_reports_resolution():
    resolved = _entry("created_at")
    new, disappeared, status = evaluate_drift([], [resolved])

    assert new == []
    assert disappeared == [resolved]
    assert status == 0
    assert format_resolved_notice(disappeared).startswith("Known drift no longer present (1):")
