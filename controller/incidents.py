"""Incident detection and response — caller-ID degradation, hallucination spikes, KPI drift.

Monitors rolling metrics, detects threshold breaches on populations (never single calls),
and orchestrates mitigation through LLM prompt fixes, caller-ID rotation, and rollback.
"""

import copy
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .api_client import SimulatorClient
from .db import Database
from .stats import detect_degradation, compute_total_variation_distance

logger = logging.getLogger(__name__)

# Detection thresholds
CALLER_ID_PICKUP_DROP_THRESHOLD = 0.07  # 7% absolute drop vs peer DIDs
CALLER_ID_MIN_DIALS = 25                # minimum dials to evaluate a DID
HALLUC_RATE_THRESHOLD = 0.025           # 2.5% hallucination rate triggers response
HALLUC_MIN_CONNECTED = 40               # minimum connected calls to evaluate
KPI_DRIFT_THRESHOLD = 0.03              # 3% absolute drop from baseline


class IncidentDetector:
    """Detects and responds to operational incidents."""

    def __init__(self, api: SimulatorClient, db: Database):
        self.api = api
        self.db = db

    # ── Caller-ID health monitoring (C5) ──────────────────────────────

    def check_caller_id_health(
        self, workflow_id: str, current_date: str, current_ts: str,
    ) -> List[Dict[str, Any]]:
        """Detect degraded caller IDs by comparing pickup rates across DIDs.

        Returns list of flagged caller IDs with evidence.
        """
        stats = self.db.get_caller_id_health(
            workflow_id, recent_days=2, current_date=current_date
        )

        if len(stats) < 2:
            return []

        total_dials = sum(s["total_dials"] for s in stats)
        total_connected = sum(s["total_connected"] for s in stats)

        if total_dials < CALLER_ID_MIN_DIALS * 2:
            return []

        flagged = []

        for s in stats:
            if s["total_dials"] < CALLER_ID_MIN_DIALS:
                continue

            rate = s["pickup_rate"]
            peer_dials = total_dials - s["total_dials"]
            peer_connected = total_connected - s["total_connected"]
            peer_rate = peer_connected / peer_dials if peer_dials > 0 else 0

            drop = peer_rate - rate
            if drop > CALLER_ID_PICKUP_DROP_THRESHOLD:
                open_incidents = self.db.get_open_incidents(workflow_id)
                already_flagged = any(
                    i["incident_type"] == "caller_id_degraded"
                    and s["caller_id"] in json.loads(i["details_json"]).get("caller_id", "")
                    for i in open_incidents
                )
                if already_flagged:
                    continue

                incident_id = f"did_{uuid.uuid4().hex[:8]}"
                incident = {
                    "incident_id": incident_id,
                    "workflow_id": workflow_id,
                    "incident_type": "caller_id_degraded",
                    "detected_at": current_ts,
                    "details": {
                        "caller_id": s["caller_id"],
                        "pickup_rate": round(rate, 4),
                        "peer_rate": round(peer_rate, 4),
                        "drop": round(drop, 4),
                        "dials": s["total_dials"],
                    },
                }
                self.db.create_incident(incident)
                self.db.audit(
                    ts=current_ts, workflow_id=workflow_id,
                    action="caller_id_flagged",
                    details=incident["details"],
                    evidence={"dials": s["total_dials"], "peer_dials": peer_dials},
                )
                flagged.append(incident)
                logger.warning(
                    f"{workflow_id}: Caller ID {s['caller_id']} degraded: "
                    f"{rate:.3f} vs peer {peer_rate:.3f} (drop {drop:.3f})"
                )

        return flagged

    def mitigate_caller_id(
        self, workflow_id: str, bad_caller_id: str, current_ts: str,
    ) -> Optional[Dict]:
        """Create a config that reduces traffic to the degraded caller ID.

        If auto-apply is enabled for caller_id, shifts by at most max_shift
        to ensure auto-apply succeeds immediately.
        """
        cur = self.api.get_config(workflow_id)
        config = cur["config"]

        default_alloc = config.get("allocations", {}).get("default", {})
        caller_ids = default_alloc.get("caller_id", {})

        if bad_caller_id not in caller_ids or len(caller_ids) <= 1:
            return None

        # Check workflow auto-apply limits
        wf_cache = self.db.get_workflow(workflow_id)
        gov = json.loads(wf_cache["governance_json"]) if wf_cache else {}
        auto = gov.get("auto_apply", {})
        auto_enabled = auto.get("enabled", False) and "caller_id" in auto.get("allowed_dims", [])
        max_shift = auto.get("max_shift", 0.5) if auto_enabled else 1.0

        new_config = copy.deepcopy(config)
        current_bad_weight = caller_ids.get(bad_caller_id, 0.0)

        # How much can we shift bad DID down?
        # Target is 0.02 (2% probe). Limit step to max_shift if auto-applying.
        desired_reduction = max(0.0, current_bad_weight - 0.02)
        actual_reduction = min(desired_reduction, max_shift * 0.95) if auto_enabled else desired_reduction
        new_bad_weight = max(0.02, current_bad_weight - actual_reduction)

        good_ids = [k for k in caller_ids if k != bad_caller_id]
        new_caller_ids = {}
        excess = current_bad_weight - new_bad_weight

        for k in good_ids:
            new_caller_ids[k] = round(caller_ids[k] + excess / len(good_ids), 4)
        new_caller_ids[bad_caller_id] = round(new_bad_weight, 4)

        # Normalize to sum to 1.0 exactly
        tot = sum(new_caller_ids.values())
        for k in new_caller_ids:
            new_caller_ids[k] = round(new_caller_ids[k] / tot, 4)

        new_config["allocations"]["default"]["caller_id"] = new_caller_ids

        # Also update any cohort allocations that specify caller_id
        for cohort_id, alloc in new_config.get("allocations", {}).items():
            if cohort_id == "default":
                continue
            if "caller_id" in alloc and bad_caller_id in alloc["caller_id"]:
                new_config["allocations"][cohort_id]["caller_id"] = copy.deepcopy(new_caller_ids)

        return new_config

    # ── Hallucination monitoring (C8) ─────────────────────────────────

    def check_hallucination_rate(
        self, workflow_id: str, current_date: str, current_ts: str,
    ) -> Optional[Dict[str, Any]]:
        """Detect elevated hallucination rates.

        Returns incident info if threshold breached, else None.
        """
        stats = self.db.get_hallucination_rate(
            workflow_id, recent_days=2, current_date=current_date
        )

        if stats["connected"] < HALLUC_MIN_CONNECTED:
            return None

        rate = stats["rate"]
        if rate < HALLUC_RATE_THRESHOLD:
            return None

        open_incidents = self.db.get_open_incidents(workflow_id)
        for inc in open_incidents:
            if inc["incident_type"] == "hallucination_spike":
                return None  # already tracking

        incident_id = f"halluc_{uuid.uuid4().hex[:8]}"
        incident = {
            "incident_id": incident_id,
            "workflow_id": workflow_id,
            "incident_type": "hallucination_spike",
            "detected_at": current_ts,
            "details": {
                "hallucination_rate": round(rate, 4),
                "connected_calls": stats["connected"],
                "halluc_flags": stats["halluc"],
                "threshold": HALLUC_RATE_THRESHOLD,
            },
        }
        self.db.create_incident(incident)
        self.db.audit(
            ts=current_ts, workflow_id=workflow_id,
            action="hallucination_spike",
            details=incident["details"],
        )
        logger.warning(
            f"{workflow_id}: Hallucination spike detected: "
            f"{rate:.3f} ({stats['halluc']}/{stats['connected']})"
        )
        return incident

    def handle_hallucination_incident(
        self, workflow_id: str, incident: Dict, current_ts: str,
        credits_remaining: float,
    ) -> Optional[Dict]:
        """Attempt to fix a hallucination incident:
        1. Ask LLM for prompt fix suggestions
        2. Validate candidates using synthetic validation runs
        3. Prioritize prompts that balance safety AND engagement:
           P3 (grounded, high engagement) > P1 (standard baseline) > others > P5 (strict, hurts engagement)
        4. Return proposed config with validation evidence
        """
        if credits_remaining < 6:
            logger.warning(f"{workflow_id}: Low credits ({credits_remaining}), skipping LLM fix")
            return None

        details = json.loads(incident["details_json"]) if isinstance(incident.get("details_json"), str) else incident.get("details", {})
        halluc_rate = details.get("hallucination_rate", 0)

        # Step 1: Get LLM suggestions (costs 3 credits)
        problem = (
            f"Hallucination rate is {halluc_rate:.1%} "
            f"(threshold: {HALLUC_RATE_THRESHOLD:.1%}). "
            f"Need a grounded prompt that eliminates hallucinations while maintaining customer engagement."
        )
        status, resp = self.api.suggest_fixes(workflow_id, problem)
        if status != 200:
            logger.error(f"{workflow_id}: LLM suggest-fixes failed: {status} {resp}")
            return None

        suggestions = resp.get("suggestions", [])
        if not suggestions:
            logger.warning(f"{workflow_id}: No prompt suggestions returned")
            return None

        # Sort suggestions to prioritize P3 > P1 > others > P5
        def prompt_preference(s: Dict) -> int:
            pv = s.get("prompt_version", "")
            if pv == "P3":
                return 0  # best: grounded + safe + higher engagement
            if pv == "P1":
                return 1  # solid: standard production prompt
            if pv == "P5":
                return 9  # last resort: strict compliance script that tanks engagement
            return 3

        suggestions.sort(key=prompt_preference)

        # Step 2: Get workflow's min_validation_pass_rate
        wf_cache = self.db.get_workflow(workflow_id)
        gov = json.loads(wf_cache["governance_json"]) if wf_cache else {}
        min_pass_rate = gov.get("min_validation_pass_rate", 0.97)

        # Step 3: Validate candidates
        best_candidate = None
        best_pass_rate = 0.0
        best_run_id = None

        for suggestion in suggestions:
            prompt_version = suggestion["prompt_version"]

            # Validate with n=200 (costs 2 credits)
            val_status, val_resp = self.api.run_validation(
                workflow_id, prompt_version, n=200
            )
            if val_status != 200:
                logger.error(f"{workflow_id}: Validation failed: {val_status}")
                continue

            run_id = val_resp.get("validation_run_id", "")
            pass_rate = val_resp.get("pass_rate", 0.0)

            self.db.record_validation_run(
                run_id=run_id,
                workflow_id=workflow_id,
                prompt_version=prompt_version,
                n=200,
                pass_rate=pass_rate,
                ts=current_ts,
            )

            logger.info(
                f"{workflow_id}: Validated {prompt_version}: "
                f"pass_rate={pass_rate:.3f} (min={min_pass_rate})"
            )

            if pass_rate >= min_pass_rate:
                best_candidate = prompt_version
                best_pass_rate = pass_rate
                best_run_id = run_id
                # If we found a top-tier candidate (P3 or P1), stop immediately to save credits
                if prompt_version in ("P3", "P1"):
                    break

        if not best_candidate:
            logger.warning(f"{workflow_id}: No prompt candidate passed validation")
            self.db.update_incident(
                incident["incident_id"],
                details={**details, "fix_status": "no_valid_candidate"},
                fix_attempt={"ts": current_ts, "result": "no_valid_candidate"},
            )
            return None

        # Step 4: Build proposed config
        cur = self.api.get_config(workflow_id)
        new_config = copy.deepcopy(cur["config"])
        new_config["prompt_version"] = best_candidate

        return {
            "config": new_config,
            "evidence": {"validation_run_id": best_run_id},
            "prompt_version": best_candidate,
            "pass_rate": best_pass_rate,
            "incident_id": incident["incident_id"],
        }

