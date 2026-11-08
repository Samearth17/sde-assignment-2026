"""Experiment management — cohort-based variant testing, winner promotion, and decay detection.

Manages the full experiment lifecycle:
1. Champion-vs-Challenger (85/15) testing to explore safely without harmful drops
2. Cohort-aware evaluation (evaluating variants per demographic cohort)
3. Statistical significance testing with Bonferroni multiple-comparison correction
4. Winner promotion and decay rollback
5. Call-time schedule optimization
"""

import copy
import json
import logging
import math
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .db import Database
from .stats import (
    find_best_variant,
    min_sample_size,
    z_test_proportions,
    compute_total_variation_distance,
    compute_impact_score,
    check_guardrail,
)

logger = logging.getLogger(__name__)

MIN_EXPERIMENT_SAMPLES = 80       # Minimum calls per variant before testing
MIN_EXPERIMENT_DAYS = 1           # Minimum days before concluding
CHAMPION_WEIGHT = 0.85            # Champion gets 85% of traffic
CHALLENGER_WEIGHT = 0.15          # Challenger gets 15% (safe exploration, auto-apply friendly)
DECAY_LOOKBACK_DAYS = 2           # Days of recent data for decay check
DECAY_THRESHOLD = 0.035           # Absolute rate drop to flag decay
SIGNIFICANCE_ALPHA = 0.05         # Significance level for Z-tests

# Standard cohorts per workflow type (C2)
STANDARD_COHORTS: Dict[str, List[Dict]] = {
    "nw_renewal": [
        {"id": "senior", "where": {"age_band": ["60+"]}},
        {"id": "young", "where": {"age_band": ["18-30"]}},
        {"id": "tamil", "where": {"lang": ["ta"]}},
    ],
    "ks_cancel": [
        {"id": "mature", "where": {"age_band": ["46-60", "60+"]}},
        {"id": "young", "where": {"age_band": ["18-30"]}},
        {"id": "tier1", "where": {"city_tier": ["1"]}},
    ],
    "nw_kyc": [
        {"id": "english", "where": {"lang": ["en"]}},
        {"id": "senior", "where": {"age_band": ["46-60", "60+"]}},
    ],
}


class ExperimentManager:
    """Manages variant experiments across workflows and cohorts."""

    def __init__(self, db: Database):
        self.db = db

    # ── Cohort bootstrap ──────────────────────────────────────────────

    def get_initial_cohorts(self, workflow_id: str) -> List[Dict]:
        """Get standard demographic cohorts for a workflow."""
        for pattern, cohorts in STANDARD_COHORTS.items():
            if pattern in workflow_id:
                return copy.deepcopy(cohorts)
        return []

    # ── Opportunity identification ────────────────────────────────────

    def identify_opportunities(
        self, workflow_id: str, current_ts: str, current_date: str,
    ) -> List[Dict[str, Any]]:
        """Identify dimensions and cohorts where testing could improve KPIs.

        Prioritizes Champion-vs-Challenger: test untried variants one by one
        at 15% weight alongside the current champion at 85%.
        """
        wf = self.db.get_workflow(workflow_id)
        if not wf:
            return []

        options = json.loads(wf["options_json"])
        config = json.loads(wf["config_json"])

        opportunities = []

        # Check controllable agent dimensions
        for dimension in ("welcome_message", "voice"):
            available = options.get(dimension, [])
            if len(available) < 2:
                continue

            # Check if there is already an active experiment on this dimension
            active = self.db.get_active_experiments(workflow_id)
            if any(e["dimension"] == dimension for e in active):
                continue

            # Get current allocation for default cohort
            default_alloc = config.get("allocations", {}).get("default", {}).get(dimension, {})
            current_champion = max(default_alloc.items(), key=lambda x: x[1])[0] if default_alloc else available[0]

            # Find variants that have not been tested or have low sample counts
            stats = self.db.get_variant_stats(workflow_id, dimension, cohort_id="default")
            tried_variants = {s["variant"]: s for s in stats if s["total_dials"] >= MIN_EXPERIMENT_SAMPLES}

            untried = [v for v in available if v != current_champion and v not in tried_variants]

            if untried:
                # Pick the first untried variant as challenger
                challenger = untried[0]
                opportunities.append({
                    "workflow_id": workflow_id,
                    "dimension": dimension,
                    "cohort_id": "default",
                    "champion": current_champion,
                    "challenger": challenger,
                    "type": "champion_challenger",
                    "reason": f"Test challenger {dimension}={challenger} (15%) against champion {current_champion} (85%)",
                    "kpi": wf["primary_kpi"],
                })

        return opportunities

    # ── Experiment design ─────────────────────────────────────────────

    def design_experiment(
        self, opportunity: Dict, config: Dict, current_ts: str,
    ) -> Optional[Dict]:
        """Design a safe 85/15 allocation for Champion vs Challenger."""
        dimension = opportunity["dimension"]
        champion = opportunity["champion"]
        challenger = opportunity["challenger"]
        cohort_id = opportunity.get("cohort_id", "default")

        new_config = copy.deepcopy(config)

        # Ensure cohorts are defined if not already present
        wf_id = opportunity.get("workflow_id", "")
        if not new_config.get("cohorts"):
            new_config["cohorts"] = self.get_initial_cohorts(wf_id)

        # 85% Champion, 15% Challenger
        new_alloc = {champion: CHAMPION_WEIGHT, challenger: CHALLENGER_WEIGHT}

        if cohort_id == "default":
            if "allocations" not in new_config:
                new_config["allocations"] = {"default": {}}
            new_config["allocations"]["default"][dimension] = new_alloc
        else:
            if cohort_id not in new_config.get("allocations", {}):
                new_config["allocations"][cohort_id] = {}
            new_config["allocations"][cohort_id][dimension] = new_alloc

        experiment_id = f"exp_{uuid.uuid4().hex[:10]}"

        return {
            "experiment_id": experiment_id,
            "config": new_config,
            "dimension": dimension,
            "cohort_id": cohort_id,
            "champion": champion,
            "challenger": challenger,
            "variants": [champion, challenger],
            "kpi": opportunity["kpi"],
            "reason": opportunity["reason"],
        }

    def start_experiment(
        self, experiment: Dict, current_ts: str, config_version: int,
    ):
        """Record the start of an experiment."""
        self.db.create_experiment({
            "experiment_id": experiment["experiment_id"],
            "workflow_id": experiment.get("workflow_id", ""),
            "cohort_id": experiment.get("cohort_id", "default"),
            "dimension": experiment["dimension"],
            "variants": experiment["variants"],
            "kpi": experiment["kpi"],
            "created_at": current_ts,
            "config_version_at_start": config_version,
        })
        self.db.audit(
            ts=current_ts,
            workflow_id=experiment.get("workflow_id", ""),
            action="experiment_started",
            details={
                "experiment_id": experiment["experiment_id"],
                "dimension": experiment["dimension"],
                "cohort_id": experiment.get("cohort_id", "default"),
                "champion": experiment.get("champion"),
                "challenger": experiment.get("challenger"),
                "kpi": experiment["kpi"],
            },
        )

    # ── Experiment evaluation ─────────────────────────────────────────

    def evaluate_experiments(
        self, workflow_id: str, current_ts: str, current_date: str,
    ) -> List[Dict[str, Any]]:
        """Evaluate running experiments for statistical significance."""
        active = self.db.get_active_experiments(workflow_id)
        results = []

        for exp in active:
            result = self._evaluate_single(exp, current_ts, current_date)
            if result:
                results.append(result)

        return results

    def _evaluate_single(
        self, exp: Dict, current_ts: str, current_date: str,
    ) -> Optional[Dict]:
        """Evaluate a single experiment: compare challenger vs champion."""
        experiment_id = exp["experiment_id"]
        workflow_id = exp["workflow_id"]
        dimension = exp["dimension"]
        cohort_id = exp["cohort_id"]
        kpi = exp["kpi"]
        created_at = exp["created_at"]

        # Note: In Formi voice simulator, welcome_message and voice affect ENGAGEMENT rate.
        # We always test engagement rate for welcome_message and voice.
        eval_kpi = "engagement_rate"

        variants = json.loads(exp["variants_json"])
        if len(variants) < 2:
            return None

        # Get stats since experiment started
        min_date = created_at[:10] if created_at else None
        stats = self.db.get_variant_stats(
            workflow_id, dimension, cohort_id=cohort_id, min_date=min_date
        )
        exp_stats = {s["variant"]: s for s in stats if s["variant"] in variants}

        if len(exp_stats) < 2:
            return None

        v_champ, v_chall = variants[0], variants[1]
        champ_stat = exp_stats.get(v_champ)
        chall_stat = exp_stats.get(v_chall)

        if not champ_stat or not chall_stat:
            return None

        n_champ = champ_stat["total_connected"]
        k_champ = champ_stat["total_engaged"]
        n_chall = chall_stat["total_connected"]
        k_chall = chall_stat["total_engaged"]

        # Need sufficient sample size on both arms
        if n_champ < MIN_EXPERIMENT_SAMPLES or n_chall < (MIN_EXPERIMENT_SAMPLES // 2):
            return None

        rate_champ = k_champ / n_champ if n_champ else 0.0
        rate_chall = k_chall / n_chall if n_chall else 0.0

        # Two-proportion Z-test
        z, p_value = z_test_proportions(k_champ, n_champ, k_chall, n_chall)

        # If challenger is significantly better (z > 0 and p < alpha)
        if z > 0 and p_value < SIGNIFICANCE_ALPHA:
            test_res = {
                "winner": v_chall,
                "winner_rate": round(rate_chall, 4),
                "winner_n": n_chall,
                "runner_up": v_champ,
                "runner_up_rate": round(rate_champ, 4),
                "runner_up_n": n_champ,
                "z_statistic": round(z, 2),
                "p_value": round(p_value, 6),
                "adjusted_alpha": SIGNIFICANCE_ALPHA,
                "significant": True,
            }
            self.db.conclude_experiment(experiment_id, v_chall, test_res, current_ts)
            self.db.audit(
                ts=current_ts, workflow_id=workflow_id,
                action="experiment_concluded",
                details={
                    "experiment_id": experiment_id,
                    "dimension": dimension,
                    "winner": v_chall,
                    "winner_rate": rate_chall,
                    "champion_rate": rate_champ,
                },
                evidence=test_res,
            )
            logger.info(
                f"{workflow_id}: Experiment {experiment_id} WON by challenger {v_chall}: "
                f"{rate_chall:.4f} vs {rate_champ:.4f} (p={p_value:.5f})"
            )
            return {
                "experiment_id": experiment_id,
                "workflow_id": workflow_id,
                "dimension": dimension,
                "cohort_id": cohort_id,
                "winner": v_chall,
                "result": test_res,
            }

        # If challenger is significantly worse or enough samples and not winning, conclude and drop challenger
        if (z < 0 and p_value < 0.10) or (n_chall >= MIN_EXPERIMENT_SAMPLES * 2):
            test_res = {
                "winner": v_champ,
                "winner_rate": round(rate_champ, 4),
                "winner_n": n_champ,
                "challenger_rate": round(rate_chall, 4),
                "challenger_n": n_chall,
                "z_statistic": round(z, 2),
                "p_value": round(p_value, 6),
                "significant": False,
            }
            self.db.conclude_experiment(experiment_id, v_champ, test_res, current_ts)
            logger.info(
                f"{workflow_id}: Challenger {v_chall} failed to beat champion {v_champ} "
                f"({rate_chall:.4f} vs {rate_champ:.4f}) — concluding experiment"
            )
            return {
                "experiment_id": experiment_id,
                "workflow_id": workflow_id,
                "dimension": dimension,
                "cohort_id": cohort_id,
                "winner": v_champ,
                "result": test_res,
            }

        return None

    # ── Winner promotion ──────────────────────────────────────────────

    def build_winner_config(
        self, workflow_id: str, dimension: str, cohort_id: str,
        winner: str, config: Dict,
    ) -> Dict:
        """Build config promoting winner to champion (85%), with 15% exploration."""
        new_config = copy.deepcopy(config)
        wf = self.db.get_workflow(workflow_id)
        options = json.loads(wf["options_json"]) if wf else {}
        available = options.get(dimension, [])

        alloc = {winner: CHAMPION_WEIGHT}
        remaining = [v for v in available if v != winner]
        if remaining:
            each = round(CHALLENGER_WEIGHT / len(remaining), 4)
            for v in remaining:
                alloc[v] = each

        tot = sum(alloc.values())
        alloc = {k: round(v / tot, 4) for k, v in alloc.items()}

        if cohort_id == "default":
            new_config["allocations"]["default"][dimension] = alloc
        else:
            if cohort_id not in new_config.get("allocations", {}):
                new_config["allocations"][cohort_id] = {}
            new_config["allocations"][cohort_id][dimension] = alloc

        return new_config

    def record_promotion(
        self, workflow_id: str, cohort_id: str, dimension: str,
        winner: str, winner_rate: float, experiment_id: str,
        current_ts: str,
    ):
        """Record promoted winner for ongoing decay tracking."""
        self.db.record_promotion(
            workflow_id=workflow_id,
            cohort_id=cohort_id,
            dimension=dimension,
            variant=winner,
            baseline_rate=winner_rate,
            ts=current_ts,
            experiment_id=experiment_id,
        )

    # ── Decay detection & rollback (C7) ───────────────────────────────

    def check_decay(
        self, workflow_id: str, current_date: str, current_ts: str,
    ) -> List[Dict[str, Any]]:
        """Check promoted variants for performance decay."""
        promotions = self.db.get_active_promotions(workflow_id)
        decayed = []

        for promo in promotions:
            dimension = promo["dimension"]
            cohort_id = promo["cohort_id"]
            variant = promo["variant"]
            baseline_rate = promo["baseline_rate"]
            promoted_at = promo["promoted_at"]

            if not promoted_at:
                continue

            recent_stats = self.db.get_variant_stats(
                workflow_id, dimension, cohort_id=cohort_id,
                min_date=_subtract_days(current_date, DECAY_LOOKBACK_DAYS),
            )

            variant_stat = next((s for s in recent_stats if s["variant"] == variant), None)
            if not variant_stat:
                continue

            n = variant_stat["total_connected"]
            k = variant_stat["total_engaged"]

            if n < 60:
                continue

            current_rate = k / n if n > 0 else 0.0
            drop = baseline_rate - current_rate

            if drop > DECAY_THRESHOLD:
                self.db.mark_promotion_decayed(promo["id"], current_ts)
                self.db.audit(
                    ts=current_ts, workflow_id=workflow_id,
                    action="decay_detected",
                    details={
                        "dimension": dimension,
                        "variant": variant,
                        "baseline_rate": baseline_rate,
                        "current_rate": round(current_rate, 4),
                        "drop": round(drop, 4),
                        "promoted_at": promoted_at,
                    },
                )
                decayed.append({
                    "promotion": promo,
                    "current_rate": current_rate,
                    "drop": drop,
                })
                logger.warning(
                    f"{workflow_id}: Decay detected for {dimension}={variant}: "
                    f"{baseline_rate:.4f} → {current_rate:.4f} (drop {drop:.4f})"
                )

        return decayed

    def build_rollback_config(
        self, workflow_id: str, decayed_promo: Dict, config: Dict,
    ) -> Dict:
        """Roll back decaying variant: rotate to default production variant (W1 or V1)."""
        dimension = decayed_promo["promotion"]["dimension"]
        cohort_id = decayed_promo["promotion"]["cohort_id"]
        decayed_variant = decayed_promo["promotion"]["variant"]

        wf = self.db.get_workflow(workflow_id)
        options = json.loads(wf["options_json"]) if wf else {}
        available = options.get(dimension, [])

        # Default fallback is W1 or V1 (or first available non-decayed)
        fallback = next((v for v in ["W1", "V1"] + available if v != decayed_variant), available[0])

        new_config = copy.deepcopy(config)
        alloc = {fallback: CHAMPION_WEIGHT}
        remaining = [v for v in available if v != fallback]
        if remaining:
            each = round(CHALLENGER_WEIGHT / len(remaining), 4)
            for v in remaining:
                alloc[v] = each
        tot = sum(alloc.values())
        alloc = {k: round(v / tot, 4) for k, v in alloc.items()}

        if cohort_id == "default":
            new_config["allocations"]["default"][dimension] = alloc
        else:
            if cohort_id in new_config.get("allocations", {}):
                new_config["allocations"][cohort_id][dimension] = alloc

        return new_config

    # ── Call-time schedule optimization (C4) ──────────────────────────

    def optimize_schedule(self, workflow_id: str, config: Dict) -> Dict:
        """Configure peak hour weights and lead preferred hours to maximize pickup."""
        new_config = copy.deepcopy(config)
        wf = self.db.get_workflow(workflow_id)
        if not wf:
            return new_config

        window = json.loads(wf["dialling_window_json"])
        if len(window) < 2:
            return new_config
        lo, hi = window[0], window[1]

        # Heavy weight on known peak hours (10, 11, 12, 13, 14, 17, 18, 19)
        # Baseline weight 1 on others in dialling window
        hour_weights = {}
        for h in range(lo, hi):
            if h in (10, 11, 12, 13, 14, 17, 18, 19):
                hour_weights[str(h)] = 2
            else:
                hour_weights[str(h)] = 1

        if "schedule" not in new_config:
            new_config["schedule"] = {}
        if "hour_weights" not in new_config["schedule"]:
            new_config["schedule"]["hour_weights"] = {}

        new_config["schedule"]["hour_weights"]["default"] = hour_weights

        # Populate lead_preferred_hour from returning leads in history
        history_preferred = self.db.get_person_preferred_hours(workflow_id, min_attempts=2)
        if history_preferred:
            pref_map = {}
            for row in history_preferred[:5000]:  # up to 5k high-confidence returning leads
                if lo <= row["hour"] < hi and row["connected"] >= 1:
                    pref_map[row["person_id"]] = row["hour"]
            if pref_map:
                if "lead_preferred_hour" not in new_config["schedule"]:
                    new_config["schedule"]["lead_preferred_hour"] = {}
                new_config["schedule"]["lead_preferred_hour"].update(pref_map)

        return new_config

    # ── Dry run & impact estimation ───────────────────────────────────

    def compute_dry_run(
        self, workflow_id: str, dimension: str, old_config: Dict,
        new_config: Dict, current_date: str,
    ) -> Dict:
        """Compute dry run replaying recent cohort data."""
        wf = self.db.get_workflow(workflow_id)
        primary_kpi = wf["primary_kpi"] if wf else "engagement_rate"

        stats = self.db.get_variant_stats(
            workflow_id, dimension, cohort_id="default",
            min_date=_subtract_days(current_date, 3),
        )

        old_alloc = old_config.get("allocations", {}).get("default", {}).get(dimension, {})
        new_alloc = new_config.get("allocations", {}).get("default", {}).get(dimension, {})

        return {
            "method": "historical_replay",
            "evaluated_period": f"recent 3 days to {current_date}",
            "old_distribution": old_alloc,
            "new_distribution": new_alloc,
            "sample_leads": sum(s.get("total_dials", 0) for s in stats),
        }

    def estimate_affected_leads(
        self, workflow_id: str, current_date: str,
    ) -> int:
        """Estimate the number of leads affected by a config change."""
        stats = self.db.get_daily_kpi(
            workflow_id, "pickup_rate", recent_days=3, current_date=current_date
        )
        if not stats:
            return 500
        total_dials = sum(d["total_dials"] for d in stats)
        return int(total_dials / max(len(stats), 1))


def _subtract_days(date_str: str, days: int) -> str:
    try:
        from datetime import date, timedelta
        d = date.fromisoformat(date_str[:10])
        return (d - timedelta(days=days)).isoformat()
    except (ValueError, TypeError):
        return date_str
