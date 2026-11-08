"""Governance client — manages the full proposal lifecycle.

Handles approval submissions, polling, applying approved changes,
auto-apply within policy, supervisor edit reconciliation, approver
fatigue limits, and timing against approver hours.
"""

import copy
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .api_client import SimulatorClient
from .db import Database
from .stats import compute_total_variation_distance, compute_impact_score

logger = logging.getLogger(__name__)


class GovernanceClient:
    """Manages the governance lifecycle for config changes."""

    def __init__(self, api: SimulatorClient, db: Database):
        self.api = api
        self.db = db

    # ── Approver hours check ──────────────────────────────────────────

    def is_within_approver_hours(self, workflow_id: str, current_ts: str) -> bool:
        """Check if current time is within approver hours with safety margin."""
        if not current_ts or len(current_ts) < 13:
            return True
        hour = int(current_ts[11:13])

        wf_cache = self.db.get_workflow(workflow_id)
        if not wf_cache:
            return True

        gov = json.loads(wf_cache["governance_json"])
        approver_hours = gov.get("approver_hours", [10, 19])
        start_hour, end_hour = approver_hours[0], approver_hours[1]

        # Safety window: submit at or after start_hour, and at least 2h before end_hour
        # so latency (up to 2h) finishes before the approver leaves
        return start_hour <= hour <= (end_hour - 2)

    # ── Proposal submission ───────────────────────────────────────────

    def submit_proposal(
        self,
        workflow_id: str,
        proposed_config: Dict,
        change_summary: str,
        diff: Dict,
        affected_leads: int,
        dry_run: Dict,
        impact_score: float,
        rationale: str,
        change_type: str = "experiment",
        evidence: Optional[Dict] = None,
        current_ts: str = "",
        force: bool = False,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Submit a proposal through the approval path.

        Respects approver hours and fatigue limits.
        Returns (success, approval_id, error_message).
        """
        # 1. Check approver working hours (unless forced for critical mitigation)
        if not force and not self.is_within_approver_hours(workflow_id, current_ts):
            logger.info(
                f"{workflow_id}: Outside approver hours ({current_ts[11:16]}), "
                f"queueing proposal for next working window"
            )
            return False, None, "outside_approver_hours"

        # 2. Get current config version
        cur = self.api.get_config(workflow_id)
        base_version = cur["version"]

        # If proposed config equals current config, skip (no_change)
        if proposed_config == cur["config"]:
            logger.debug(f"{workflow_id}: Proposed config equals current config, skipping")
            return False, None, "no_change"

        # 3. Check approver fatigue limit
        wf_cache = self.db.get_workflow(workflow_id)
        ttl_hours = 24
        if wf_cache:
            gov = json.loads(wf_cache["governance_json"])
            max_per_24h = gov.get("max_requests_per_24h", 3)
            ttl_hours = gov.get("proposal_ttl_hours", 24)

            if current_ts:
                since = _subtract_hours(current_ts, 24)
                recent_count = self.db.count_recent_approvals(workflow_id, since)
                if recent_count >= max_per_24h:
                    logger.warning(
                        f"{workflow_id}: Approver fatigue limit ({max_per_24h}/24h) reached, "
                        f"skipping proposal"
                    )
                    return False, None, "approver_fatigue_limit"

        proposal_id = f"prop_{uuid.uuid4().hex[:12]}"

        body = {
            "proposal_id": proposal_id,
            "workflow_id": workflow_id,
            "base_version": base_version,
            "proposed_config": proposed_config,
            "change_summary": change_summary,
            "diff": diff,
            "affected_leads": max(int(affected_leads), 0),
            "dry_run": dry_run,
            "impact_score": min(max(float(impact_score), 0.0), 1.0),
            "rationale": rationale,
            "expires_in_hours": ttl_hours,
        }
        if evidence:
            body["evidence"] = evidence

        status_code, resp = self.api.submit_approval(body)

        if status_code == 200:
            approval_id = resp.get("approval_id", "")
            self.db.record_approval_submitted({
                "proposal_id": proposal_id,
                "approval_id": approval_id,
                "workflow_id": workflow_id,
                "base_version": base_version,
                "proposed_config": proposed_config,
                "change_summary": change_summary,
                "change_type": change_type,
                "submitted_at": current_ts,
                "expires_at": resp.get("expires_at"),
            })
            self.db.audit(
                ts=current_ts, workflow_id=workflow_id,
                action="proposal_submitted",
                details={
                    "proposal_id": proposal_id,
                    "approval_id": approval_id,
                    "change_summary": change_summary,
                    "change_type": change_type,
                    "diff": diff,
                    "affected_leads": affected_leads,
                    "impact_score": impact_score,
                },
                evidence=evidence,
            )
            logger.info(f"{workflow_id}: Proposal {proposal_id} submitted → {approval_id}")
            return True, approval_id, None

        elif status_code == 409:
            error = resp.get("error", "")
            logger.warning(f"{workflow_id}: Proposal 409: {error}")
            return False, None, str(error)
        else:
            logger.error(f"{workflow_id}: Proposal failed: {status_code} {resp}")
            return False, None, str(resp)

    # ── Auto-apply ────────────────────────────────────────────────────

    def try_auto_apply(
        self,
        workflow_id: str,
        proposed_config: Dict,
        rationale: str,
        current_ts: str = "",
        evidence: Optional[Dict] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Try to auto-apply a config change within policy.

        Returns (success, error_message).
        """
        wf_cache = self.db.get_workflow(workflow_id)
        if not wf_cache:
            return False, "workflow_not_cached"

        gov = json.loads(wf_cache["governance_json"])
        auto = gov.get("auto_apply", {})
        if not auto.get("enabled", False):
            return False, "auto_apply_disabled"

        cur = self.api.get_config(workflow_id)
        base_version = cur["version"]

        if proposed_config == cur["config"]:
            return False, "no_change"

        proposal_id = f"auto_{uuid.uuid4().hex[:12]}"

        success, resp = self.api.auto_apply(
            workflow_id=workflow_id,
            proposal_id=proposal_id,
            base_version=base_version,
            proposed_config=proposed_config,
            rationale=rationale,
            evidence=evidence,
        )

        if success:
            new_version = resp.get("version", base_version + 1)
            self.db.update_config_version(
                workflow_id, new_version, proposed_config, current_ts
            )
            self.db.audit(
                ts=current_ts, workflow_id=workflow_id,
                action="auto_applied",
                details={
                    "proposal_id": proposal_id,
                    "rationale": rationale,
                    "changed_dims": resp.get("changed_dims", []),
                    "old_version": base_version,
                    "new_version": new_version,
                },
                evidence=evidence,
                outcome="applied",
            )
            logger.info(f"{workflow_id}: Auto-applied {proposal_id} → v{new_version}")
            return True, None
        else:
            error = resp.get("error", str(resp))
            return False, error

    # ── Apply approved changes ────────────────────────────────────────

    def poll_and_apply_approvals(self, current_ts: str):
        """Check all pending approvals and apply approved ones."""
        pending = self.db.get_pending_approvals()
        for appr in pending:
            approval_id = appr.get("approval_id")
            if not approval_id:
                continue

            try:
                status_resp = self.api.get_approval(approval_id)
            except Exception as e:
                logger.error(f"Failed to check approval {approval_id}: {e}")
                continue

            status = status_resp.get("status", "pending")

            if status == "approved":
                self.db.update_approval_status(
                    approval_id=approval_id,
                    status="approved",
                    decided_at=status_resp.get("decided_at"),
                    apply_by=status_resp.get("apply_by"),
                )
                self.db.audit(
                    ts=current_ts, workflow_id=appr["workflow_id"],
                    action="proposal_approved",
                    details={"approval_id": approval_id,
                             "proposal_id": appr["proposal_id"]},
                )
                self._apply_approved(appr, approval_id, current_ts)

            elif status in ("rejected", "expired", "lapsed"):
                reasons = status_resp.get("reasons", [])
                self.db.update_approval_status(
                    approval_id=approval_id,
                    status=status,
                    decided_at=status_resp.get("decided_at"),
                    reasons=reasons,
                )
                self.db.audit(
                    ts=current_ts, workflow_id=appr["workflow_id"],
                    action=f"proposal_{status}",
                    details={
                        "approval_id": approval_id,
                        "proposal_id": appr["proposal_id"],
                        "reasons": reasons,
                    },
                )
                logger.info(
                    f"{appr['workflow_id']}: Approval {approval_id} {status}: {reasons}"
                )

        approved = self.db.get_approved_unapplied()
        for appr in approved:
            self._apply_approved(appr, appr["approval_id"], current_ts)

    def _apply_approved(self, appr: Dict, approval_id: str, current_ts: str):
        """Apply an approved change."""
        workflow_id = appr["workflow_id"]

        success, resp = self.api.apply_approval(workflow_id, approval_id)

        if success:
            new_version = resp.get("version", 0)
            config = json.loads(appr["proposed_config_json"])
            self.db.update_approval_status(
                approval_id=approval_id, status="applied",
                applied_at=current_ts,
            )
            self.db.update_config_version(workflow_id, new_version, config, current_ts)
            self.db.audit(
                ts=current_ts, workflow_id=workflow_id,
                action="config_applied",
                details={
                    "approval_id": approval_id,
                    "proposal_id": appr["proposal_id"],
                    "new_version": new_version,
                    "change_type": appr.get("change_type", "unknown"),
                    "change_summary": appr.get("change_summary", ""),
                },
                outcome="applied",
            )
            logger.info(f"{workflow_id}: Applied approval {approval_id} → v{new_version}")
        else:
            error = resp.get("error", str(resp))
            if error == "stale_base_version":
                self.db.update_approval_status(
                    approval_id=approval_id, status="stale",
                    reasons=["stale_base_version"],
                )
                self.db.audit(
                    ts=current_ts, workflow_id=workflow_id,
                    action="apply_stale",
                    details={
                        "approval_id": approval_id,
                        "reason": "config changed since proposal (e.g. supervisor edit)",
                    },
                )
                logger.warning(f"{workflow_id}: Apply failed stale_base_version for {approval_id}")
            elif "lapsed" in str(error) or "410" in str(resp.get("status_code", "")):
                self.db.update_approval_status(
                    approval_id=approval_id, status="lapsed",
                )
                logger.warning(f"{workflow_id}: Approval {approval_id} lapsed")
            else:
                logger.error(f"{workflow_id}: Apply failed: {error}")

    # ── Supervisor edit handling ──────────────────────────────────────

    def handle_supervisor_edit(self, workflow_id: str, new_version: int,
                               changed_dims: List[str], current_ts: str):
        """Handle a supervisor config edit — reconcile our state."""
        config_data = self.api.get_config(workflow_id)
        config = config_data["config"]
        version = config_data["version"]

        self.db.update_config_version(workflow_id, version, config, current_ts)

        for dim in changed_dims:
            if dim in ("welcome_message", "voice", "caller_id", "prompt_version"):
                self.db.supersede_experiments(workflow_id, dim, current_ts)

        self.db.audit(
            ts=current_ts, workflow_id=workflow_id,
            action="supervisor_edit",
            details={
                "new_version": version,
                "changed_dims": changed_dims,
            },
            outcome="state_reconciled",
        )
        logger.info(
            f"{workflow_id}: Supervisor edit → v{version}, "
            f"changed: {changed_dims}"
        )


def _subtract_hours(ts: str, hours: int) -> str:
    """Subtract hours from an ISO timestamp string."""
    try:
        dt = datetime.fromisoformat(ts)
        result = dt - timedelta(hours=hours)
        return result.isoformat()
    except (ValueError, TypeError):
        return ts
