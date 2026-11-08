"""HTTP client wrapper for the simulator API.

Provides a typed, resilient interface to every simulator endpoint.
All methods raise on unexpected HTTP errors but return structured
data on expected 4xx responses (stale version, auto-apply denied, etc.).
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


class SimulatorClient:
    """Typed HTTP client for the Formi simulator."""

    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self):
        self.client.close()

    # ── Simulator control ─────────────────────────────────────────────

    def get_state(self) -> Dict[str, Any]:
        """GET /sim/state → current sim time, credits, finished flag."""
        r = self.client.get("/sim/state")
        r.raise_for_status()
        return r.json()

    def advance(self, minutes: int = 60) -> Dict[str, Any]:
        """POST /sim/advance → {ts, finished, new_chunks}."""
        r = self.client.post("/sim/advance", json={"minutes": minutes})
        if r.status_code == 409:
            data = r.json()
            if "finished" in str(data):
                return {"ts": data.get("ts", ""), "finished": True, "new_chunks": []}
            raise RuntimeError(f"Advance failed: {data}")
        r.raise_for_status()
        return r.json()

    def health(self) -> Dict[str, Any]:
        r = self.client.get("/health")
        r.raise_for_status()
        return r.json()

    # ── Workflows ─────────────────────────────────────────────────────

    def list_workflows(self) -> List[Dict[str, Any]]:
        """GET /workflows → list of workflow definitions."""
        r = self.client.get("/workflows")
        r.raise_for_status()
        return r.json()

    def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """GET /workflows/{id} → workflow definition with options, objective, governance."""
        r = self.client.get(f"/workflows/{workflow_id}")
        r.raise_for_status()
        return r.json()

    def get_config(self, workflow_id: str) -> Dict[str, Any]:
        """GET /workflows/{id}/config → {config, version}."""
        r = self.client.get(f"/workflows/{workflow_id}/config")
        r.raise_for_status()
        return r.json()

    def get_config_history(self, workflow_id: str) -> List[Dict[str, Any]]:
        """GET /workflows/{id}/config/history → list of config versions."""
        r = self.client.get(f"/workflows/{workflow_id}/config/history")
        r.raise_for_status()
        return r.json()

    # ── Config changes ────────────────────────────────────────────────

    def auto_apply(
        self,
        workflow_id: str,
        proposal_id: str,
        base_version: int,
        proposed_config: Dict,
        rationale: str,
        evidence: Optional[Dict] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """PUT /workflows/{id}/config → auto-apply within policy.

        Returns (success, response_body). On 403/409, returns (False, body).
        """
        body: Dict[str, Any] = {
            "proposal_id": proposal_id,
            "base_version": base_version,
            "proposed_config": proposed_config,
            "rationale": rationale,
        }
        if evidence:
            body["evidence"] = evidence
        r = self.client.put(f"/workflows/{workflow_id}/config", json=body)
        if r.status_code in (200, 201):
            return True, r.json()
        if r.status_code in (403, 409):
            return False, r.json()
        r.raise_for_status()
        return False, {}  # unreachable

    def apply_approval(
        self, workflow_id: str, approval_id: str
    ) -> Tuple[bool, Dict[str, Any]]:
        """POST /workflows/{id}/apply → apply an approved config.

        Returns (success, body). 409 stale_base or 410 lapsed → (False, body).
        """
        r = self.client.post(
            f"/workflows/{workflow_id}/apply", json={"approval_id": approval_id}
        )
        if r.status_code in (200, 201):
            return True, r.json()
        if r.status_code in (409, 410):
            return False, r.json()
        r.raise_for_status()
        return False, {}

    # ── Approvals ─────────────────────────────────────────────────────

    def submit_approval(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /approvals → submit a change proposal.

        Returns (status_code, body). 200 = submitted, 409 = duplicate/stale.
        """
        r = self.client.post("/approvals", json=body)
        return r.status_code, r.json()

    def get_approval(self, approval_id: str) -> Dict[str, Any]:
        """GET /approvals/{id} → approval status and details."""
        r = self.client.get(f"/approvals/{approval_id}")
        r.raise_for_status()
        return r.json()

    def list_approvals(
        self,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """GET /approvals?workflow_id=&status= → list of approvals."""
        params: Dict[str, str] = {}
        if workflow_id:
            params["workflow_id"] = workflow_id
        if status:
            params["status"] = status
        r = self.client.get("/approvals", params=params)
        r.raise_for_status()
        return r.json()

    # ── Events ────────────────────────────────────────────────────────

    def list_chunks(
        self, kind: str = "events", after: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """GET /events/chunks?kind=&after= → chunk metadata list."""
        params: Dict[str, Any] = {"kind": kind}
        if after is not None:
            params["after"] = after
        r = self.client.get("/events/chunks", params=params)
        r.raise_for_status()
        return r.json()

    def get_chunk(self, chunk_id: int) -> List[Dict[str, Any]]:
        """GET /events/chunks/{n} → list of events (parsed from NDJSON)."""
        r = self.client.get(f"/events/chunks/{chunk_id}")
        r.raise_for_status()
        events = []
        for line in r.text.strip().splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    # ── LLM & Validation (cost credits) ──────────────────────────────

    def suggest_fixes(
        self, workflow_id: str, problem: str
    ) -> Tuple[int, Any]:
        """POST /llm/suggest-fixes → up to 3 prompt alternatives. Costs 3 credits.

        Returns (status_code, body). 402 = out of credits.
        """
        r = self.client.post(
            "/llm/suggest-fixes",
            json={"workflow_id": workflow_id, "problem": problem},
        )
        return r.status_code, r.json()

    def run_validation(
        self, workflow_id: str, prompt_version: str, n: int = 200
    ) -> Tuple[int, Any]:
        """POST /validation/runs → validation pass_rate. Costs ⌈n/100⌉ credits.

        Returns (status_code, body). 402 = out of credits.
        """
        n = max(50, min(2000, n))
        r = self.client.post(
            "/validation/runs",
            json={
                "workflow_id": workflow_id,
                "prompt_version": prompt_version,
                "n": n,
            },
        )
        return r.status_code, r.json()

