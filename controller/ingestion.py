"""Event ingestion — processes event chunks into the database.

Handles call, campaign_created, lead, config_changed, and approval_decided
events. Idempotent: re-processing a chunk is a no-op.
"""

import json
import logging
from typing import Any, Dict, List, Tuple

from .db import Database

logger = logging.getLogger(__name__)


class ChunkResult:
    """Result of processing a single event chunk."""
    def __init__(self):
        self.counts: Dict[str, int] = {}
        self.config_changes: List[Dict] = []
        self.approval_decisions: List[Dict] = []


class EventIngester:
    """Processes simulator events into the database."""

    def __init__(self, db: Database):
        self.db = db

    def process_chunk(self, chunk_id: int, events: List[Dict[str, Any]],
                      kind: str, current_ts: str) -> ChunkResult:
        """Process all events in a chunk. Returns a ChunkResult.
        
        Idempotent: already-processed chunks return empty result.
        """
        result = ChunkResult()
        if self.db.is_chunk_processed(chunk_id):
            return result

        self.db.begin()
        try:
            for event in events:
                event_type = event.get("type", "unknown")
                result.counts[event_type] = result.counts.get(event_type, 0) + 1

                if event_type == "call":
                    self._process_call(event)
                elif event_type == "config_changed":
                    result.config_changes.append(event)
                elif event_type == "approval_decided":
                    result.approval_decisions.append(event)

            self.db.mark_chunk_processed(chunk_id, kind, current_ts)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return result

    def _process_call(self, event: Dict[str, Any]):
        """Process a single call event into aggregate statistics."""
        wf_id = event.get("workflow_id", "")
        cohort_id = event.get("cohort_id", "default")
        variant = event.get("variant", {})
        ts = event.get("ts", "")
        period = ts[:10] if ts else ""
        hour = int(ts[11:13]) if len(ts) >= 13 else -1

        connected = event.get("connected", False)
        engaged = event.get("engaged", False)
        duration_s = event.get("duration_s", 0)

        qa = event.get("qa", {})
        halluc_flag = qa.get("hallucination_flag", False) if qa else False

        self.db.record_call(
            workflow_id=wf_id,
            cohort_id=cohort_id,
            variant=variant,
            period=period,
            hour=hour,
            connected=connected,
            engaged=engaged,
            halluc_flag=halluc_flag,
            duration_s=duration_s,
        )

        person_id = event.get("person_id", "")
        if person_id:
            self.db.record_person_call(
                workflow_id=wf_id,
                person_id=person_id,
                hour=hour,
                connected=connected,
                engaged=engaged,
            )
