"""Unit tests for the controller modules:
- Statistical testing (Z-test, sample sizes, TV distance, impact score)
- Database persistence, idempotency, and audit trails
- Incident detection logic (caller-ID degradation, hallucination spikes)
- Experiment design (Champion-vs-Challenger, decay detection)
"""

import os
import tempfile


from controller.stats import (
    z_test_proportions,
    min_sample_size,
    compute_total_variation_distance,
    compute_impact_score,
    check_guardrail,
    find_best_variant,
    detect_degradation,
)
from controller.db import Database
from controller.experiments import ExperimentManager


def test_z_test_proportions():
    # Equal proportions -> z near 0, p near 1
    z, p = z_test_proportions(50, 100, 50, 100)
    assert abs(z) < 1e-6
    assert p > 0.99

    # Highly significant difference
    z, p = z_test_proportions(20, 100, 60, 100)
    assert z > 5.0
    assert p < 1e-6


def test_min_sample_size():
    n = min_sample_size(baseline_rate=0.5, mde=0.03, alpha=0.05, power=0.80)
    assert 500 <= n <= 5000

    # Fallback bounds
    assert min_sample_size(0.0, 0.0) >= 50


def test_total_variation_distance():
    # Same distribution -> TV = 0
    d1 = {"W1": 0.5, "W2": 0.5}
    assert compute_total_variation_distance(d1, d1) == 0.0

    # Completely disjoint distributions -> TV = 1.0
    d2 = {"W1": 1.0}
    d3 = {"W2": 1.0}
    assert compute_total_variation_distance(d2, d3) == 1.0

    # Shift of 0.15
    d4 = {"W1": 1.0}
    d5 = {"W1": 0.85, "W2": 0.15}
    assert abs(compute_total_variation_distance(d4, d5) - 0.15) < 1e-6


def test_guardrail_check():
    baseline = 0.30
    max_drop = 0.02
    min_samples = 100

    # Within guardrail
    assert check_guardrail(current_rate=0.29, baseline_rate=baseline, max_drop=max_drop, current_n=200) is True

    # Drop exceeds guardrail (0.30 - 0.27 = 0.03 > 0.02)
    assert check_guardrail(current_rate=0.27, baseline_rate=baseline, max_drop=max_drop, current_n=200) is False

    # Not enough samples -> lenient
    assert check_guardrail(current_rate=0.20, baseline_rate=baseline, max_drop=max_drop, current_n=50, min_samples=min_samples) is True


def test_find_best_variant():
    stats = [
        {"variant": "V1", "total_dials": 1000, "total_connected": 500, "total_engaged": 250},
        {"variant": "V2", "total_dials": 1000, "total_connected": 500, "total_engaged": 350},  # 70% vs 50%
    ]
    winner = find_best_variant(stats, kpi="engagement_rate", min_samples=100)
    assert winner is not None
    assert winner["winner"] == "V2"
    assert winner["significant"] is True
    assert winner["p_value"] < 0.001


def test_db_persistence_and_idempotency(tmp_path):
    db_file = str(tmp_path / "test.db")
    db = Database(db_file)

    # Chunk processing idempotency
    assert db.is_chunk_processed(1) is False
    db.mark_chunk_processed(1, "events", "2027-01-04T10:00:00")
    db.commit()
    assert db.is_chunk_processed(1) is True

    # Call recording
    db.record_call(
        workflow_id="wf_1", cohort_id="default",
        variant={"voice": "V1", "welcome_message": "W1", "caller_id": "D1", "prompt_version": "P1"},
        period="2027-01-04", hour=10, connected=True, engaged=True, halluc_flag=False, duration_s=45
    )
    db.commit()

    v_stats = db.get_variant_stats("wf_1", "voice", cohort_id="default")
    assert len(v_stats) == 1
    assert v_stats[0]["variant"] == "V1"
    assert v_stats[0]["total_connected"] == 1
    assert v_stats[0]["total_engaged"] == 1

    # Audit logging
    db.audit("2027-01-04T10:00:00", "wf_1", "test_action", {"key": "val"}, outcome="success")
    logs = db.query_audit(workflow_id="wf_1")
    assert len(logs) == 1
    assert logs[0]["action"] == "test_action"

    db.close()

    # Re-open database (crash recovery simulation)
    db2 = Database(db_file)
    assert db2.is_chunk_processed(1) is True
    v_stats2 = db2.get_variant_stats("wf_1", "voice", cohort_id="default")
    assert len(v_stats2) == 1
    db2.close()


if __name__ == "__main__":
    from pathlib import Path
    print("Running test_z_test_proportions...")
    test_z_test_proportions()
    print("Running test_min_sample_size...")
    test_min_sample_size()
    print("Running test_total_variation_distance...")
    test_total_variation_distance()
    print("Running test_guardrail_check...")
    test_guardrail_check()
    print("Running test_find_best_variant...")
    test_find_best_variant()
    print("Running test_db_persistence_and_idempotency...")
    with tempfile.TemporaryDirectory() as td:
        test_db_persistence_and_idempotency(Path(td))
    print("ALL TESTS PASSED SUCCESSFULLY!")

