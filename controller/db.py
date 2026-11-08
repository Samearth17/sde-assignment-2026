"""SQLite-backed persistent state for crash-safe controller operation.

All state mutations happen inside transactions. On restart, the controller
resumes from the last committed state without double-applying or losing data.
"""

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA = """
-- Track which event chunks we've fully processed
CREATE TABLE IF NOT EXISTS processed_chunks (
    chunk_id    INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL,          -- 'history' or 'events'
    processed_at TEXT   NOT NULL           -- ISO timestamp
);

-- Cached workflow definitions (refreshed on config_changed events)
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id     TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    primary_kpi     TEXT NOT NULL,         -- 'pickup_rate' or 'engagement_rate'
    guardrail_json  TEXT NOT NULL,         -- JSON of guardrail spec
    governance_json TEXT NOT NULL,         -- JSON of governance block
    options_json    TEXT NOT NULL,         -- JSON of available options
    objective_json  TEXT NOT NULL,         -- JSON of full objective
    dialling_window_json TEXT NOT NULL,    -- JSON [start, end]
    mid_flight      TEXT NOT NULL,         -- 'allowed' or 'next_campaign_only'
    config_version  INTEGER NOT NULL DEFAULT 0,
    config_json     TEXT NOT NULL DEFAULT '{}',
    updated_at      TEXT NOT NULL
);

-- Aggregate call statistics per (workflow, cohort, dimension, variant, day)
-- Used for significance testing and drift detection
CREATE TABLE IF NOT EXISTS call_stats (
    workflow_id TEXT    NOT NULL,
    cohort_id   TEXT    NOT NULL,          -- 'default' if no cohort match
    dimension   TEXT    NOT NULL,          -- 'voice', 'welcome_message', 'caller_id', 'prompt_version'
    variant     TEXT    NOT NULL,
    period      TEXT    NOT NULL,          -- ISO date 'YYYY-MM-DD'
    hour        INTEGER NOT NULL DEFAULT -1, -- hour of day, -1 for daily aggregate
    dials       INTEGER NOT NULL DEFAULT 0,
    connected   INTEGER NOT NULL DEFAULT 0,
    engaged     INTEGER NOT NULL DEFAULT 0,
    halluc_flags INTEGER NOT NULL DEFAULT 0,
    total_duration_s REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, cohort_id, dimension, variant, period, hour)
);

-- Per-caller-ID health tracking
CREATE TABLE IF NOT EXISTS caller_id_stats (
    workflow_id TEXT    NOT NULL,
    caller_id   TEXT    NOT NULL,
    period      TEXT    NOT NULL,          -- ISO date
    dials       INTEGER NOT NULL DEFAULT 0,
    connected   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, caller_id, period)
);

-- Experiment tracking
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    workflow_id   TEXT    NOT NULL,
    cohort_id     TEXT    NOT NULL DEFAULT 'default',
    dimension     TEXT    NOT NULL,         -- 'voice', 'welcome_message'
    variants_json TEXT    NOT NULL,         -- JSON list of variant IDs in the test
    status        TEXT    NOT NULL DEFAULT 'running', -- running|concluded|rolled_back|superseded
    kpi           TEXT    NOT NULL,         -- 'pickup_rate' or 'engagement_rate'
    created_at    TEXT    NOT NULL,
    concluded_at  TEXT,
    winner        TEXT,                    -- winning variant ID
    evidence_json TEXT,                    -- JSON of statistical test results
    config_version_at_start INTEGER
);

-- Approval lifecycle tracking
CREATE TABLE IF NOT EXISTS approvals (
    proposal_id       TEXT PRIMARY KEY,
    approval_id       TEXT,
    workflow_id       TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'pending',
    -- pending|approved|rejected|expired|applied|lapsed|stale
    base_version      INTEGER NOT NULL,
    proposed_config_json TEXT NOT NULL,
    change_summary    TEXT    NOT NULL,
    change_type       TEXT    NOT NULL DEFAULT 'experiment',
    -- experiment|incident|rollback|optimization
    submitted_at      TEXT    NOT NULL,
    decided_at        TEXT,
    applied_at        TEXT,
    expires_at        TEXT,
    apply_by          TEXT,
    reasons_json      TEXT                 -- JSON list of rejection reasons
);

-- Audit log — the single source of truth for "why did X change"
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,        -- simulated timestamp
    workflow_id   TEXT    NOT NULL,
    action        TEXT    NOT NULL,
    -- detected_drift|experiment_started|experiment_concluded|proposal_submitted|
    -- proposal_approved|proposal_rejected|config_applied|auto_applied|
    -- supervisor_edit|incident_detected|incident_resolved|rollback|
    -- caller_id_flagged|hallucination_spike|winner_promoted|decay_detected
    details_json  TEXT    NOT NULL DEFAULT '{}',
    evidence_json TEXT    NOT NULL DEFAULT '{}',
    outcome       TEXT                     -- result description
);

-- Incident tracking
CREATE TABLE IF NOT EXISTS incidents (
    incident_id   TEXT PRIMARY KEY,
    workflow_id   TEXT    NOT NULL,
    incident_type TEXT    NOT NULL,         -- caller_id_degraded|hallucination_spike|kpi_drift
    detected_at   TEXT    NOT NULL,
    resolved_at   TEXT,
    status        TEXT    NOT NULL DEFAULT 'open',  -- open|mitigating|resolved
    details_json  TEXT    NOT NULL DEFAULT '{}',
    fix_attempts_json TEXT NOT NULL DEFAULT '[]'
);

-- Track promoted winners and their ongoing performance for decay detection
CREATE TABLE IF NOT EXISTS promoted_variants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id   TEXT    NOT NULL,
    cohort_id     TEXT    NOT NULL DEFAULT 'default',
    dimension     TEXT    NOT NULL,
    variant       TEXT    NOT NULL,
    promoted_at   TEXT    NOT NULL,
    experiment_id TEXT,
    baseline_rate REAL    NOT NULL,        -- the rate when promoted
    status        TEXT    NOT NULL DEFAULT 'active',  -- active|decayed|rolled_back
    decay_detected_at TEXT,
    rolled_back_at TEXT
);

-- Per-person call outcomes for call-time learning
CREATE TABLE IF NOT EXISTS person_call_history (
    workflow_id   TEXT    NOT NULL,
    person_id     TEXT    NOT NULL,
    hour          INTEGER NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    connected     INTEGER NOT NULL DEFAULT 0,
    engaged       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, person_id, hour)
);

-- Validation run results (for prompt changes)
CREATE TABLE IF NOT EXISTS validation_runs (
    run_id          TEXT PRIMARY KEY,
    workflow_id     TEXT    NOT NULL,
    prompt_version  TEXT    NOT NULL,
    n               INTEGER NOT NULL,
    pass_rate       REAL,
    created_at      TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'completed'
);

-- Controller state (singleton, for crash recovery)
CREATE TABLE IF NOT EXISTS controller_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_call_stats_wf_period
    ON call_stats(workflow_id, period);
CREATE INDEX IF NOT EXISTS idx_caller_id_stats_wf
    ON caller_id_stats(workflow_id, caller_id, period);
CREATE INDEX IF NOT EXISTS idx_audit_wf_ts
    ON audit_log(workflow_id, ts);
CREATE INDEX IF NOT EXISTS idx_experiments_wf_status
    ON experiments(workflow_id, status);
CREATE INDEX IF NOT EXISTS idx_approvals_wf_status
    ON approvals(workflow_id, status);
CREATE INDEX IF NOT EXISTS idx_incidents_wf_status
    ON incidents(workflow_id, status);
CREATE INDEX IF NOT EXISTS idx_promoted_wf_status
    ON promoted_variants(workflow_id, status);
CREATE INDEX IF NOT EXISTS idx_person_calls
    ON person_call_history(workflow_id, person_id);
"""


class Database:
    """SQLite database for controller state, with WAL mode for crash safety."""

    def __init__(self, db_path: str = "controller_state.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ── Controller state (crash recovery) ─────────────────────────────

    def get_state(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM controller_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO controller_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    # ── Chunk tracking ────────────────────────────────────────────────

    def is_chunk_processed(self, chunk_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        return row is not None

    def mark_chunk_processed(self, chunk_id: int, kind: str, ts: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_chunks (chunk_id, kind, processed_at) VALUES (?, ?, ?)",
            (chunk_id, kind, ts),
        )
        # Don't commit here — caller batches with event processing

    def get_last_processed_chunk(self, kind: str = "events") -> int:
        row = self.conn.execute(
            "SELECT MAX(chunk_id) as max_id FROM processed_chunks WHERE kind = ?",
            (kind,),
        ).fetchone()
        return row["max_id"] if row and row["max_id"] is not None else -1

    # ── Workflow cache ────────────────────────────────────────────────

    def upsert_workflow(self, wf: Dict[str, Any], config: Dict[str, Any], version: int, ts: str):
        self.conn.execute(
            """INSERT OR REPLACE INTO workflows
            (workflow_id, tenant_id, primary_kpi, guardrail_json, governance_json,
             options_json, objective_json, dialling_window_json, mid_flight,
             config_version, config_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                wf["workflow_id"],
                wf["tenant_id"],
                wf["objective"]["primary"],
                json.dumps(wf["objective"].get("guardrail", {})),
                json.dumps(wf.get("governance", {})),
                json.dumps(wf.get("options", {})),
                json.dumps(wf.get("objective", {})),
                json.dumps(wf.get("dialling_window", [])),
                wf.get("mid_flight", "allowed"),
                version,
                json.dumps(config),
                ts,
            ),
        )
        self.conn.commit()

    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def get_all_workflows(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM workflows").fetchall()
        return [dict(r) for r in rows]

    def update_config_version(self, workflow_id: str, version: int, config: Dict, ts: str):
        self.conn.execute(
            "UPDATE workflows SET config_version = ?, config_json = ?, updated_at = ? WHERE workflow_id = ?",
            (version, json.dumps(config), ts, workflow_id),
        )
        self.conn.commit()

    # ── Call statistics ───────────────────────────────────────────────

    def record_call(
        self,
        workflow_id: str,
        cohort_id: str,
        variant: Dict[str, str],
        period: str,
        hour: int,
        connected: bool,
        engaged: bool,
        halluc_flag: bool,
        duration_s: float,
    ):
        """Record a single call outcome across all variant dimensions."""
        for dimension in ("voice", "welcome_message", "caller_id", "prompt_version"):
            var = variant.get(dimension, "unknown")
            self.conn.execute(
                """INSERT INTO call_stats
                (workflow_id, cohort_id, dimension, variant, period, hour,
                 dials, connected, engaged, halluc_flags, total_duration_s)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, cohort_id, dimension, variant, period, hour)
                DO UPDATE SET
                    dials = dials + 1,
                    connected = connected + ?,
                    engaged = engaged + ?,
                    halluc_flags = halluc_flags + ?,
                    total_duration_s = total_duration_s + ?""",
                (
                    workflow_id, cohort_id, dimension, var, period, hour,
                    int(connected), int(engaged), int(halluc_flag), duration_s,
                    int(connected), int(engaged), int(halluc_flag), duration_s,
                ),
            )

        # Also track caller_id separately for health monitoring
        caller_id = variant.get("caller_id", "unknown")
        self.conn.execute(
            """INSERT INTO caller_id_stats (workflow_id, caller_id, period, dials, connected)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(workflow_id, caller_id, period)
            DO UPDATE SET dials = dials + 1, connected = connected + ?""",
            (workflow_id, caller_id, period, int(connected), int(connected)),
        )

    def record_person_call(
        self, workflow_id: str, person_id: str, hour: int,
        connected: bool, engaged: bool,
    ):
        """Track per-person call-time outcomes."""
        self.conn.execute(
            """INSERT INTO person_call_history
            (workflow_id, person_id, hour, attempts, connected, engaged)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(workflow_id, person_id, hour)
            DO UPDATE SET
                attempts = attempts + 1,
                connected = connected + ?,
                engaged = engaged + ?""",
            (workflow_id, person_id, hour, int(connected), int(engaged),
             int(connected), int(engaged)),
        )

    def get_variant_stats(
        self,
        workflow_id: str,
        dimension: str,
        cohort_id: str = "default",
        min_date: Optional[str] = None,
        max_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get aggregated stats for variants of a dimension."""
        query = """
            SELECT variant,
                   SUM(dials) as total_dials,
                   SUM(connected) as total_connected,
                   SUM(engaged) as total_engaged,
                   SUM(halluc_flags) as total_halluc
            FROM call_stats
            WHERE workflow_id = ? AND dimension = ? AND cohort_id = ?
        """
        params: List[Any] = [workflow_id, dimension, cohort_id]
        if min_date:
            query += " AND period >= ?"
            params.append(min_date)
        if max_date:
            query += " AND period <= ?"
            params.append(max_date)
        query += " GROUP BY variant"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_all_cohort_variant_stats(
        self,
        workflow_id: str,
        dimension: str,
        min_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get stats grouped by cohort and variant for a dimension."""
        query = """
            SELECT cohort_id, variant,
                   SUM(dials) as total_dials,
                   SUM(connected) as total_connected,
                   SUM(engaged) as total_engaged,
                   SUM(halluc_flags) as total_halluc
            FROM call_stats
            WHERE workflow_id = ? AND dimension = ?
        """
        params: List[Any] = [workflow_id, dimension]
        if min_date:
            query += " AND period >= ?"
            params.append(min_date)
        query += " GROUP BY cohort_id, variant"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_caller_id_health(
        self, workflow_id: str, recent_days: int = 3,
        current_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get caller-ID pickup rates over recent days."""
        query = """
            SELECT caller_id,
                   SUM(dials) as total_dials,
                   SUM(connected) as total_connected,
                   CAST(SUM(connected) AS REAL) / MAX(SUM(dials), 1) as pickup_rate
            FROM caller_id_stats
            WHERE workflow_id = ?
        """
        params: List[Any] = [workflow_id]
        if current_date:
            query += " AND period > date(?, ?)"
            params.extend([current_date, f"-{recent_days} days"])
        query += " GROUP BY caller_id HAVING total_dials >= 20"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_hallucination_rate(
        self, workflow_id: str, recent_days: int = 2,
        current_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get hallucination rate over recent period."""
        query = """
            SELECT SUM(connected) as total_connected,
                   SUM(halluc_flags) as total_halluc
            FROM call_stats
            WHERE workflow_id = ? AND dimension = 'prompt_version'
        """
        params: List[Any] = [workflow_id]
        if current_date:
            query += " AND period > date(?, ?)"
            params.extend([current_date, f"-{recent_days} days"])
        row = self.conn.execute(query, params).fetchone()
        if not row or not row["total_connected"]:
            return {"connected": 0, "halluc": 0, "rate": 0.0}
        return {
            "connected": row["total_connected"],
            "halluc": row["total_halluc"],
            "rate": row["total_halluc"] / max(row["total_connected"], 1),
        }

    def get_daily_kpi(
        self, workflow_id: str, kpi: str, recent_days: int = 3,
        current_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get daily KPI values for drift detection."""
        query = """
            SELECT period,
                   SUM(dials) as total_dials,
                   SUM(connected) as total_connected,
                   SUM(engaged) as total_engaged
            FROM call_stats
            WHERE workflow_id = ? AND dimension = 'voice'
                  AND cohort_id = 'default'
        """
        params: List[Any] = [workflow_id]
        if current_date:
            query += " AND period > date(?, ?)"
            params.extend([current_date, f"-{recent_days} days"])
        query += " GROUP BY period ORDER BY period"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_person_preferred_hours(
        self, workflow_id: str, min_attempts: int = 5,
    ) -> List[Dict[str, Any]]:
        """Get per-person best hours based on pickup/engagement history."""
        query = """
            SELECT person_id, hour,
                   attempts, connected, engaged,
                   CAST(connected AS REAL) / MAX(attempts, 1) as pickup_rate
            FROM person_call_history
            WHERE workflow_id = ? AND attempts >= ?
            ORDER BY person_id, pickup_rate DESC
        """
        rows = self.conn.execute(query, (workflow_id, min_attempts)).fetchall()
        return [dict(r) for r in rows]

    # ── Experiments ───────────────────────────────────────────────────

    def create_experiment(self, exp: Dict[str, Any]):
        self.conn.execute(
            """INSERT OR IGNORE INTO experiments
            (experiment_id, workflow_id, cohort_id, dimension, variants_json,
             status, kpi, created_at, config_version_at_start)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exp["experiment_id"], exp["workflow_id"], exp.get("cohort_id", "default"),
                exp["dimension"], json.dumps(exp["variants"]),
                "running", exp["kpi"], exp["created_at"],
                exp.get("config_version_at_start"),
            ),
        )
        self.conn.commit()

    def conclude_experiment(self, experiment_id: str, winner: Optional[str],
                           evidence: Dict, ts: str):
        self.conn.execute(
            """UPDATE experiments
            SET status = 'concluded', winner = ?, evidence_json = ?, concluded_at = ?
            WHERE experiment_id = ?""",
            (winner, json.dumps(evidence), ts, experiment_id),
        )
        self.conn.commit()

    def get_active_experiments(self, workflow_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM experiments WHERE workflow_id = ? AND status = 'running'",
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_experiment(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        return dict(row) if row else None

    def supersede_experiments(self, workflow_id: str, dimension: str, ts: str):
        """Mark running experiments as superseded (e.g., after supervisor edit)."""
        self.conn.execute(
            """UPDATE experiments SET status = 'superseded', concluded_at = ?
            WHERE workflow_id = ? AND dimension = ? AND status = 'running'""",
            (ts, workflow_id, dimension),
        )
        self.conn.commit()

    # ── Approvals ─────────────────────────────────────────────────────

    def record_approval_submitted(self, proposal: Dict[str, Any]):
        self.conn.execute(
            """INSERT OR REPLACE INTO approvals
            (proposal_id, approval_id, workflow_id, status, base_version,
             proposed_config_json, change_summary, change_type, submitted_at,
             expires_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (
                proposal["proposal_id"],
                proposal.get("approval_id"),
                proposal["workflow_id"],
                proposal["base_version"],
                json.dumps(proposal["proposed_config"]),
                proposal["change_summary"],
                proposal.get("change_type", "experiment"),
                proposal["submitted_at"],
                proposal.get("expires_at"),
            ),
        )
        self.conn.commit()

    def update_approval_status(self, proposal_id: str = None, approval_id: str = None,
                               status: str = None, reasons: List[str] = None,
                               decided_at: str = None, applied_at: str = None,
                               apply_by: str = None):
        updates = []
        params = []
        if status:
            updates.append("status = ?")
            params.append(status)
        if reasons is not None:
            updates.append("reasons_json = ?")
            params.append(json.dumps(reasons))
        if decided_at:
            updates.append("decided_at = ?")
            params.append(decided_at)
        if applied_at:
            updates.append("applied_at = ?")
            params.append(applied_at)
        if apply_by:
            updates.append("apply_by = ?")
            params.append(apply_by)

        if proposal_id:
            where = "proposal_id = ?"
            params.append(proposal_id)
        elif approval_id:
            where = "approval_id = ?"
            params.append(approval_id)
        else:
            return

        self.conn.execute(
            f"UPDATE approvals SET {', '.join(updates)} WHERE {where}", params
        )
        self.conn.commit()

    def set_approval_id(self, proposal_id: str, approval_id: str):
        self.conn.execute(
            "UPDATE approvals SET approval_id = ? WHERE proposal_id = ?",
            (approval_id, proposal_id),
        )
        self.conn.commit()

    def get_pending_approvals(self, workflow_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM approvals WHERE status = 'pending'"
        params = []
        if workflow_id:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_approved_unapplied(self, workflow_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM approvals WHERE status = 'approved'"
        params = []
        if workflow_id:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def count_recent_approvals(self, workflow_id: str, since_ts: str) -> int:
        """Count proposals submitted in last 24h for a workflow (for fatigue limit)."""
        row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM approvals
            WHERE workflow_id = ? AND submitted_at >= ?""",
            (workflow_id, since_ts),
        ).fetchone()
        return row["cnt"] if row else 0

    # ── Audit log ─────────────────────────────────────────────────────

    def audit(self, ts: str, workflow_id: str, action: str,
              details: Dict = None, evidence: Dict = None, outcome: str = None):
        self.conn.execute(
            """INSERT INTO audit_log (ts, workflow_id, action, details_json, evidence_json, outcome)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (ts, workflow_id, action,
             json.dumps(details or {}), json.dumps(evidence or {}), outcome),
        )
        self.conn.commit()

    def query_audit(self, workflow_id: str = None, action: str = None,
                    since: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        query = "SELECT * FROM audit_log WHERE 1=1"
        params = []
        if workflow_id:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        if action:
            query += " AND action = ?"
            params.append(action)
        if since:
            query += " AND ts >= ?"
            params.append(since)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Incidents ─────────────────────────────────────────────────────

    def create_incident(self, inc: Dict[str, Any]):
        self.conn.execute(
            """INSERT OR IGNORE INTO incidents
            (incident_id, workflow_id, incident_type, detected_at, status, details_json)
            VALUES (?, ?, ?, ?, 'open', ?)""",
            (inc["incident_id"], inc["workflow_id"], inc["incident_type"],
             inc["detected_at"], json.dumps(inc.get("details", {}))),
        )
        self.conn.commit()

    def update_incident(self, incident_id: str, status: str = None,
                        resolved_at: str = None, details: Dict = None,
                        fix_attempt: Dict = None):
        updates = []
        params = []
        if status:
            updates.append("status = ?")
            params.append(status)
        if resolved_at:
            updates.append("resolved_at = ?")
            params.append(resolved_at)
        if details:
            updates.append("details_json = ?")
            params.append(json.dumps(details))
        if fix_attempt:
            updates.append("fix_attempts_json = json_insert(fix_attempts_json, '$[#]', ?)")
            params.append(json.dumps(fix_attempt))
        if updates:
            params.append(incident_id)
            self.conn.execute(
                f"UPDATE incidents SET {', '.join(updates)} WHERE incident_id = ?",
                params,
            )
            self.conn.commit()

    def get_open_incidents(self, workflow_id: str = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM incidents WHERE status IN ('open', 'mitigating')"
        params = []
        if workflow_id:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Promoted variants / decay tracking ────────────────────────────

    def record_promotion(self, workflow_id: str, cohort_id: str, dimension: str,
                         variant: str, baseline_rate: float, ts: str,
                         experiment_id: str = None):
        self.conn.execute(
            """INSERT INTO promoted_variants
            (workflow_id, cohort_id, dimension, variant, promoted_at,
             experiment_id, baseline_rate, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
            (workflow_id, cohort_id, dimension, variant, ts, experiment_id, baseline_rate),
        )
        self.conn.commit()

    def get_active_promotions(self, workflow_id: str = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM promoted_variants WHERE status = 'active'"
        params = []
        if workflow_id:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def mark_promotion_decayed(self, promotion_id: int, ts: str):
        self.conn.execute(
            "UPDATE promoted_variants SET status = 'decayed', decay_detected_at = ? WHERE id = ?",
            (ts, promotion_id),
        )
        self.conn.commit()

    def mark_promotion_rolled_back(self, promotion_id: int, ts: str):
        self.conn.execute(
            "UPDATE promoted_variants SET status = 'rolled_back', rolled_back_at = ? WHERE id = ?",
            (ts, promotion_id),
        )
        self.conn.commit()

    # ── Validation runs ───────────────────────────────────────────────

    def record_validation_run(self, run_id: str, workflow_id: str,
                              prompt_version: str, n: int, pass_rate: float, ts: str):
        self.conn.execute(
            """INSERT OR REPLACE INTO validation_runs
            (run_id, workflow_id, prompt_version, n, pass_rate, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'completed')""",
            (run_id, workflow_id, prompt_version, n, pass_rate, ts),
        )
        self.conn.commit()

    def get_validation_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM validation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── Transactions ──────────────────────────────────────────────────

    def begin(self):
        self.conn.execute("BEGIN")

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

