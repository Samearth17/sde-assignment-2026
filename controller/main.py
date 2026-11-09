"""Main controller loop — the orchestrator.

Ties together event ingestion, experiment management, incident detection,
governance, and decay monitoring into a single continuously-running loop.

Architecture:
  advance time → ingest events → handle config changes → detect incidents →
  evaluate experiments → propose changes → apply approved → repeat

Crash safety: all state is in SQLite. On restart, we resume from the last
committed chunk and reconcile pending approvals with the simulator.
"""

import argparse
import copy
import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .api_client import SimulatorClient
from .db import Database
from .ingestion import EventIngester
from .experiments import ExperimentManager
from .governance import GovernanceClient
from .incidents import IncidentDetector
from .stats import compute_total_variation_distance, compute_impact_score

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("controller")

TICK_MINUTES = 60  # sim-minutes per tick


class Controller:
    """The main controller — drives the optimization loop."""

    def __init__(self, sim_url: str, db_path: str = "controller_state.db"):
        self.api = SimulatorClient(sim_url)
        self.db = Database(db_path)
        self.ingester = EventIngester(self.db)
        self.experiments = ExperimentManager(self.db)
        self.governance = GovernanceClient(self.api, self.db)
        self.incidents = IncidentDetector(self.api, self.db)

        self._running = True
        self._last_daily_actions: Dict[str, str] = {}
        self._queued_proposals: List[Dict] = []
        self._bootstrapped = False

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self._running = False

    def _once_per_day(self, key: str, current_date: str) -> bool:
        if self._last_daily_actions.get(key) == current_date:
            return False
        self._last_daily_actions[key] = current_date
        return True

    # ── Main loop ─────────────────────────────────────────────────────

    def run(self):
        logger.info("═══ Controller starting ═══")
        self._initialize()
        self._ingest_history()
        self._main_loop()
        logger.info("═══ Controller finished ═══")
        self.api.close()
        self.db.close()

    def _initialize(self):
        logger.info("Initializing: connecting to simulator and loading workflows...")
        state = None
        for attempt in range(15):
            try:
                state = self.api.get_state()
                break
            except Exception as e:
                if attempt == 0:
                    logger.info("Waiting for simulator at %s to be ready...", self.api.base_url)
                time.sleep(1.0)

        if not state:
            logger.error(
                "Could not connect to simulator at %s after 15s.\n"
                "  -> Did you start the simulator server in another terminal window?\n"
                "  -> Run this first: python3 -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/dev --force\n"
                "  -> Once it says 'ready in ... — http://127.0.0.1:8700/docs', run ./run.sh again.",
                self.api.base_url,
            )
            sys.exit(1)

        logger.info(f"Connected to simulator: time={state['ts']}, credits={state['credits_remaining']}")

        # If the simulator is at Day 0 (start of fresh scenario run), but the DB has records from a past run:
        if state["ts"].startswith("2027-01-04T00:00:00") and self.db.get_last_processed_chunk("events") > 0:
            logger.info("New scenario detected at Day 0 — clearing stale state from previous run...")
            self.db.clear_all()
            self._bootstrapped = False
            self._last_daily_actions.clear()

        workflows = self.api.list_workflows()
        for wf in workflows:
            wf_id = wf["workflow_id"]
            config_data = self.api.get_config(wf_id)
            self.db.upsert_workflow(wf, config_data["config"], config_data["version"], state["ts"])

        self._reconcile_approvals(state["ts"])

    def _reconcile_approvals(self, current_ts: str):
        pending = self.db.get_pending_approvals()
        for appr in pending:
            approval_id = appr.get("approval_id")
            if not approval_id:
                continue
            try:
                resp = self.api.get_approval(approval_id)
                status = resp.get("status", "pending")
                if status != "pending":
                    self.db.update_approval_status(
                        approval_id=approval_id, status=status,
                        decided_at=resp.get("decided_at"),
                        reasons=resp.get("reasons", []),
                        apply_by=resp.get("apply_by"),
                    )
                    if status == "approved":
                        self.governance._apply_approved(appr, approval_id, current_ts)
                    logger.info(f"  Reconciled {approval_id}: {status}")
            except Exception as e:
                logger.warning(f"  Failed to reconcile {approval_id}: {e}")

    def _ingest_history(self):
        last_hist_chunk = self.db.get_last_processed_chunk("history")
        chunks = self.api.list_chunks(kind="history", after=last_hist_chunk)
        if not chunks:
            logger.info("History: already processed")
            return

        total_events = 0
        logger.info(f"Ingesting {len(chunks)} history chunks...")
        for chunk_meta in chunks:
            chunk_id = chunk_meta["chunk"]
            if self.db.is_chunk_processed(chunk_id):
                continue
            events = self.api.get_chunk(chunk_id)
            self.ingester.process_chunk(chunk_id, events, "history", chunk_meta.get("until_ts", ""))
            total_events += len(events)
        logger.info(f"History: ingested {total_events} events")

    def _main_loop(self):
        while self._running:
            try:
                result = self.api.advance(minutes=TICK_MINUTES)
            except Exception as e:
                logger.error(f"Advance failed: {e}")
                time.sleep(0.5)
                continue

            current_ts = result.get("ts", "")
            finished = result.get("finished", False)
            new_chunks = result.get("new_chunks", [])

            if finished and not current_ts:
                break

            current_date = current_ts[:10]
            current_hour = int(current_ts[11:13]) if len(current_ts) >= 13 else 0

            # 1. Ingest events
            all_config_changes = []
            all_approval_decisions = []
            for chunk_meta in new_chunks:
                chunk_id = chunk_meta["chunk"]
                if self.db.is_chunk_processed(chunk_id):
                    continue
                events = self.api.get_chunk(chunk_id)
                res = self.ingester.process_chunk(chunk_id, events, "events", current_ts)
                all_config_changes.extend(res.config_changes)
                all_approval_decisions.extend(res.approval_decisions)

            # 2. Reconcile supervisor edits
            for cc in all_config_changes:
                if cc.get("actor") == "supervisor":
                    self.governance.handle_supervisor_edit(
                        cc["workflow_id"], cc.get("version", 0),
                        cc.get("changed_dims", []), current_ts
                    )

            # 3. Handle approval decision events
            for ad in all_approval_decisions:
                approval_id = ad.get("approval_id", "")
                status = ad.get("status", "")
                if approval_id and status in ("approved", "rejected", "expired"):
                    self.db.update_approval_status(
                        approval_id=approval_id, status=status,
                        decided_at=current_ts, reasons=ad.get("reasons", []),
                    )

            # 4. Apply any approved proposals immediately
            self.governance.poll_and_apply_approvals(current_ts)

            # 5. Bootstrap Day 0 (prompt upgrade & schedule optimization)
            state = self.api.get_state()
            credits = state.get("credits_remaining", 0)

            if not self._bootstrapped and current_hour >= 10:
                self._bootstrap_workflows(current_date, current_ts, credits)
                self._bootstrapped = True

            # 6. Flush queued proposals during working hours (10:00 - 16:00)
            if 10 <= current_hour <= 16:
                self._flush_proposal_queue(current_ts)

            # 7. Periodic actions by sim time
            # Incident detection: check every 2 hours
            if current_hour % 2 == 0:
                self._run_incident_checks(current_date, current_ts, credits)

            # Experiment evaluation at 13:00 (inside working hours)
            if current_hour == 13 and self._once_per_day("eval_experiments", current_date):
                self._evaluate_and_promote(current_date, current_ts)

            # Experiment deployment at 14:00
            if current_hour == 14 and self._once_per_day("start_experiments", current_date):
                self._start_new_experiments(current_date, current_ts)

            # Decay check at 15:00
            if current_hour == 15 and self._once_per_day("check_decay", current_date):
                self._run_decay_checks(current_date, current_ts, credits)

            if finished:
                logger.info(f"Scenario finished at {current_ts}")
                break

        try:
            state = self.api.get_state()
            self.governance.poll_and_apply_approvals(state.get("ts", ""))
        except Exception:
            pass

    # ── Bootstrap Day 0 ───────────────────────────────────────────────

    def _bootstrap_workflows(self, current_date: str, current_ts: str, credits: float):
        """On D0: optimize schedule, upgrade prompt to P3 if available, and add cohorts."""
        workflows = self.db.get_all_workflows()
        for wf in workflows:
            wf_id = wf["workflow_id"]
            cur = self.api.get_config(wf_id)
            cfg = copy.deepcopy(cur["config"])
            options = json.loads(wf["options_json"])

            # 1. Schedule optimization (C4)
            sched_cfg = copy.deepcopy(cur["config"])
            sched_cfg = self.experiments.optimize_schedule(wf_id, sched_cfg)

            # Try auto-apply for schedule ONLY (do not include cohorts here)
            gov = json.loads(wf["governance_json"])
            auto = gov.get("auto_apply", {})
            if auto.get("enabled") and "schedule" in auto.get("allowed_dims", []):
                self.governance.try_auto_apply(
                    wf_id, sched_cfg, rationale="Optimize peak-hour calling schedule and lead preferred hours",
                    current_ts=current_ts
                )

            # 3. Proactive P3 prompt validation & upgrade (C8)
            prompts = options.get("prompt_version", [])
            current_prompt = cur["config"].get("prompt_version", "P1")
            if "P3" in prompts and current_prompt != "P3" and credits >= 10:
                # Validate P3 (costs 2 credits)
                val_status, val_resp = self.api.run_validation(wf_id, "P3", n=200)
                if val_status == 200:
                    pass_rate = val_resp.get("pass_rate", 0.0)
                    min_pass = gov.get("min_validation_pass_rate", 0.97)
                    if pass_rate >= min_pass:
                        p3_cfg = copy.deepcopy(cur["config"])
                        p3_cfg["prompt_version"] = "P3"
                        # Submit upgrade proposal
                        affected = self.experiments.estimate_affected_leads(wf_id, current_date)
                        self.governance.submit_proposal(
                            workflow_id=wf_id,
                            proposed_config=p3_cfg,
                            change_summary=f"Upgrade prompt from {current_prompt} to grounded prompt P3",
                            diff={"prompt_version": {"old": current_prompt, "new": "P3"}},
                            affected_leads=affected,
                            dry_run={"method": "validation_run", "pass_rate": pass_rate, "prompt": "P3"},
                            impact_score=0.7,
                            rationale="P3 is grounded, safer, and delivers higher customer engagement.",
                            change_type="optimization",
                            evidence={"validation_run_id": val_resp.get("validation_run_id")},
                            current_ts=current_ts,
                        )

    # ── Queue management ──────────────────────────────────────────────

    def _flush_proposal_queue(self, current_ts: str):
        if not self._queued_proposals:
            return
        to_retry = []
        for item in self._queued_proposals:
            wf_id = item["workflow_id"]
            if self.governance.is_within_approver_hours(wf_id, current_ts):
                ok, _, _ = self.governance.submit_proposal(
                    workflow_id=wf_id,
                    proposed_config=item["proposed_config"],
                    change_summary=item["change_summary"],
                    diff=item["diff"],
                    affected_leads=item["affected_leads"],
                    dry_run=item["dry_run"],
                    impact_score=item["impact_score"],
                    rationale=item["rationale"],
                    change_type=item.get("change_type", "experiment"),
                    evidence=item.get("evidence"),
                    current_ts=current_ts,
                )
                if not ok:
                    to_retry.append(item)
            else:
                to_retry.append(item)
        self._queued_proposals = to_retry

    # ── Incident detection ────────────────────────────────────────────

    def _run_incident_checks(self, current_date: str, current_ts: str, credits: float):
        workflows = self.db.get_all_workflows()
        for wf in workflows:
            wf_id = wf["workflow_id"]

            # 1. Caller ID Health
            flagged = self.incidents.check_caller_id_health(wf_id, current_date, current_ts)
            for incident in flagged:
                bad_did = incident.get("details", {}).get("caller_id", "")
                if bad_did:
                    self._handle_caller_id_incident(wf_id, bad_did, incident, current_ts, current_date)

            # 2. Hallucination Spike
            if credits > 10:
                halluc = self.incidents.check_hallucination_rate(wf_id, current_date, current_ts)
                if halluc:
                    self._handle_hallucination_incident(wf_id, halluc, current_ts, credits, current_date)

    def _handle_caller_id_incident(self, workflow_id: str, bad_did: str,
                                    incident: Dict, current_ts: str, current_date: str):
        new_config = self.incidents.mitigate_caller_id(workflow_id, bad_did, current_ts)
        if not new_config:
            return

        # Try auto-apply first
        success, _ = self.governance.try_auto_apply(
            workflow_id=workflow_id, proposed_config=new_config,
            rationale=f"Rotate traffic away from degraded caller ID {bad_did}",
            current_ts=current_ts,
        )
        if success:
            self.db.update_incident(incident["incident_id"], status="resolved", resolved_at=current_ts)
            return

        # If outside approver hours, queue it
        cur = self.api.get_config(workflow_id)
        affected = self.experiments.estimate_affected_leads(workflow_id, current_date)
        old_alloc = cur["config"].get("allocations", {}).get("default", {}).get("caller_id", {})
        new_alloc = new_config.get("allocations", {}).get("default", {}).get("caller_id", {})

        prop_data = {
            "workflow_id": workflow_id,
            "proposed_config": new_config,
            "change_summary": f"Rotate away from degraded caller ID {bad_did}",
            "diff": {"allocations.default.caller_id": new_alloc},
            "affected_leads": affected,
            "dry_run": {"bad_did": bad_did, "old_alloc": old_alloc, "new_alloc": new_alloc},
            "impact_score": 0.6,
            "rationale": f"Caller ID {bad_did} pickup rate dropped significantly. Rotating traffic.",
            "change_type": "incident",
        }

        if self.governance.is_within_approver_hours(workflow_id, current_ts):
            self.governance.submit_proposal(
                workflow_id=workflow_id, proposed_config=new_config,
                change_summary=prop_data["change_summary"], diff=prop_data["diff"],
                affected_leads=affected, dry_run=prop_data["dry_run"],
                impact_score=0.6, rationale=prop_data["rationale"],
                change_type="incident", current_ts=current_ts,
            )
        else:
            self._queued_proposals.append(prop_data)

    def _handle_hallucination_incident(self, workflow_id: str, incident: Dict,
                                         current_ts: str, credits: float, current_date: str):
        fix = self.incidents.handle_hallucination_incident(workflow_id, incident, current_ts, credits)
        if not fix:
            return

        cur = self.api.get_config(workflow_id)
        old_prompt = cur["config"].get("prompt_version", "P1")
        new_prompt = fix["prompt_version"]
        affected = self.experiments.estimate_affected_leads(workflow_id, current_date)

        prop_data = {
            "workflow_id": workflow_id,
            "proposed_config": fix["config"],
            "change_summary": f"Fix hallucination spike: switch from {old_prompt} to {new_prompt}",
            "diff": {"prompt_version": {"old": old_prompt, "new": new_prompt}},
            "affected_leads": affected,
            "dry_run": {"old_prompt": old_prompt, "new_prompt": new_prompt, "pass_rate": fix["pass_rate"]},
            "impact_score": 0.8,
            "rationale": f"Hallucination spike detected. Prompt {new_prompt} validated with {fix['pass_rate']:.1%} pass rate.",
            "change_type": "incident",
            "evidence": fix["evidence"],
        }

        if self.governance.is_within_approver_hours(workflow_id, current_ts):
            ok, _, _ = self.governance.submit_proposal(
                workflow_id=workflow_id, proposed_config=fix["config"],
                change_summary=prop_data["change_summary"], diff=prop_data["diff"],
                affected_leads=affected, dry_run=prop_data["dry_run"],
                impact_score=0.8, rationale=prop_data["rationale"],
                change_type="incident", evidence=fix["evidence"], current_ts=current_ts,
            )
            if ok:
                self.db.update_incident(incident["incident_id"], status="mitigating")
        else:
            self._queued_proposals.append(prop_data)

    # ── Experiment evaluation & promotion ─────────────────────────────

    def _evaluate_and_promote(self, current_date: str, current_ts: str):
        workflows = self.db.get_all_workflows()
        for wf in workflows:
            wf_id = wf["workflow_id"]
            conclusions = self.experiments.evaluate_experiments(wf_id, current_ts, current_date)
            for conclusion in conclusions:
                winner = conclusion.get("winner")
                result = conclusion.get("result", {})
                if result.get("significant") and winner:
                    self._promote_winner(conclusion, current_ts, current_date)

    def _promote_winner(self, conclusion: Dict, current_ts: str, current_date: str):
        workflow_id = conclusion["workflow_id"]
        dimension = conclusion["dimension"]
        cohort_id = conclusion["cohort_id"]
        winner = conclusion["winner"]
        result = conclusion["result"]

        cur = self.api.get_config(workflow_id)
        current_config = cur["config"]

        new_config = self.experiments.build_winner_config(
            workflow_id, dimension, cohort_id, winner, current_config
        )

        old_alloc = current_config.get("allocations", {}).get(cohort_id, current_config.get("allocations", {}).get("default", {})).get(dimension, {})
        new_alloc = new_config.get("allocations", {}).get(cohort_id, new_config.get("allocations", {}).get("default", {})).get(dimension, {})
        affected = self.experiments.estimate_affected_leads(workflow_id, current_date)

        # Try auto-apply if allowed
        wf_cache = self.db.get_workflow(workflow_id)
        gov = json.loads(wf_cache["governance_json"]) if wf_cache else {}
        auto_cfg = gov.get("auto_apply", {})
        tv_dist = compute_total_variation_distance(old_alloc or {"_": 1}, new_alloc or {"_": 1})

        if (auto_cfg.get("enabled") and dimension in auto_cfg.get("allowed_dims", [])
                and tv_dist <= auto_cfg.get("max_shift", 0)):
            success, _ = self.governance.try_auto_apply(
                workflow_id=workflow_id, proposed_config=new_config,
                rationale=f"Promote {dimension}={winner} (rate={result['winner_rate']:.4f}, p={result['p_value']:.5f})",
                current_ts=current_ts,
            )
            if success:
                self.experiments.record_promotion(
                    workflow_id, cohort_id, dimension, winner,
                    result["winner_rate"], conclusion["experiment_id"], current_ts
                )
                return

        # Otherwise submit approval
        dry_run = self.experiments.compute_dry_run(
            workflow_id, dimension, current_config, new_config, current_date
        )
        impact = compute_impact_score(
            result.get("runner_up_rate", 0), result.get("winner_rate", 0),
            affected, max(affected, 1)
        )

        success, _, _ = self.governance.submit_proposal(
            workflow_id=workflow_id,
            proposed_config=new_config,
            change_summary=f"Promote winning {dimension}={winner} (rate={result['winner_rate']:.4f}, p={result['p_value']:.5f})",
            diff={f"allocations.{cohort_id}.{dimension}": new_alloc},
            affected_leads=affected,
            dry_run=dry_run,
            impact_score=impact,
            rationale=(
                f"Statistical test: {winner} significantly outperformed champion in '{cohort_id}'. "
                f"Rate={result['winner_rate']:.4f} vs {result['runner_up_rate']:.4f} "
                f"(Z={result['z_statistic']:.2f}, p={result['p_value']:.5f})."
            ),
            change_type="experiment",
            current_ts=current_ts,
        )
        if success:
            self.experiments.record_promotion(
                workflow_id, cohort_id, dimension, winner,
                result["winner_rate"], conclusion["experiment_id"], current_ts
            )

    # ── Start new experiments ─────────────────────────────────────────

    def _start_new_experiments(self, current_date: str, current_ts: str):
        workflows = self.db.get_all_workflows()
        for wf in workflows:
            wf_id = wf["workflow_id"]
            if self.db.get_pending_approvals(wf_id):
                continue
            if len(self.db.get_active_experiments(wf_id)) >= 2:
                continue

            opportunities = self.experiments.identify_opportunities(wf_id, current_ts, current_date)
            for opp in opportunities[:1]:
                self._deploy_experiment(wf_id, opp, current_ts, current_date)

    def _deploy_experiment(self, workflow_id: str, opportunity: Dict,
                            current_ts: str, current_date: str):
        cur = self.api.get_config(workflow_id)
        experiment = self.experiments.design_experiment(opportunity, cur["config"], current_ts)
        if not experiment:
            return

        experiment["workflow_id"] = workflow_id
        new_config = experiment["config"]
        dimension = experiment["dimension"]

        old_alloc = cur["config"].get("allocations", {}).get("default", {}).get(dimension, {})
        new_alloc = new_config.get("allocations", {}).get("default", {}).get(dimension, {})

        if old_alloc == new_alloc:
            self.experiments.start_experiment(experiment, current_ts, cur["version"])
            return

        tv_dist = compute_total_variation_distance(old_alloc or {"_": 1}, new_alloc or {"_": 1})

        wf_cache = self.db.get_workflow(workflow_id)
        gov = json.loads(wf_cache["governance_json"]) if wf_cache else {}
        auto_cfg = gov.get("auto_apply", {})

        applied = False
        if (auto_cfg.get("enabled") and dimension in auto_cfg.get("allowed_dims", [])
                and tv_dist <= auto_cfg.get("max_shift", 0)):
            success, _ = self.governance.try_auto_apply(
                workflow_id=workflow_id, proposed_config=new_config,
                rationale=f"Champion-vs-Challenger: test {dimension}={opportunity['challenger']} (15%)",
                current_ts=current_ts,
            )
            if success:
                applied = True
                cur = self.api.get_config(workflow_id)

        if not applied:
            affected = self.experiments.estimate_affected_leads(workflow_id, current_date)
            dry_run = self.experiments.compute_dry_run(workflow_id, dimension, cur["config"], new_config, current_date)
            success, _, _ = self.governance.submit_proposal(
                workflow_id=workflow_id,
                proposed_config=new_config,
                change_summary=f"Test challenger {dimension}={opportunity['challenger']} (15%) vs champion (85%)",
                diff={f"allocations.default.{dimension}": new_alloc},
                affected_leads=affected,
                dry_run=dry_run,
                impact_score=0.25,
                rationale=opportunity["reason"],
                change_type="experiment",
                current_ts=current_ts,
            )
            if not success:
                return

        self.experiments.start_experiment(experiment, current_ts, cur["version"])
        logger.info(f"  {workflow_id}: Started test on {dimension}: challenger {opportunity['challenger']} (15%)")

    # ── Decay detection ───────────────────────────────────────────────

    def _run_decay_checks(self, current_date: str, current_ts: str, credits: float):
        workflows = self.db.get_all_workflows()
        for wf in workflows:
            wf_id = wf["workflow_id"]
            decayed = self.experiments.check_decay(wf_id, current_date, current_ts)
            for d in decayed:
                self._handle_decay(wf_id, d, current_ts, current_date)

    def _handle_decay(self, workflow_id: str, decayed: Dict, current_ts: str, current_date: str):
        cur = self.api.get_config(workflow_id)
        new_config = self.experiments.build_rollback_config(workflow_id, decayed, cur["config"])
        dimension = decayed["promotion"]["dimension"]
        variant = decayed["promotion"]["variant"]
        promo_id = decayed["promotion"]["id"]
        affected = self.experiments.estimate_affected_leads(workflow_id, current_date)

        success, _ = self.governance.try_auto_apply(
            workflow_id=workflow_id, proposed_config=new_config,
            rationale=f"Rollback {dimension}={variant} due to performance decay",
            current_ts=current_ts,
        )
        if success:
            self.db.mark_promotion_rolled_back(promo_id, current_ts)
            return

        dry_run = {"action": f"Rollback {variant}", "drop": decayed["drop"]}
        old_alloc = cur["config"].get("allocations", {}).get("default", {}).get(dimension, {})
        new_alloc = new_config.get("allocations", {}).get("default", {}).get(dimension, {})

        ok, _, _ = self.governance.submit_proposal(
            workflow_id=workflow_id,
            proposed_config=new_config,
            change_summary=f"Rollback {dimension}={variant} (performance decayed {decayed['drop']:.3f})",
            diff={f"allocations.default.{dimension}": new_alloc},
            affected_leads=affected,
            dry_run=dry_run,
            impact_score=0.5,
            rationale=f"{variant} baseline decayed from {decayed['promotion']['baseline_rate']:.4f} to {decayed['current_rate']:.4f}.",
            change_type="rollback",
            current_ts=current_ts,
        )
        if ok:
            self.db.mark_promotion_rolled_back(promo_id, current_ts)


def main():
    parser = argparse.ArgumentParser(description="Outcome-First Agent Loop Controller")
    parser.add_argument("--url", required=True, help="Simulator base URL")
    parser.add_argument("--db", default="controller_state.db", help="SQLite database path")
    args = parser.parse_args()

    controller = Controller(sim_url=args.url, db_path=args.db)
    controller.run()


if __name__ == "__main__":
    main()
