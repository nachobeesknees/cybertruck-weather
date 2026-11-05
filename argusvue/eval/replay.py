"""Replay (SPEC §10): rebuild the analyst packet as of a past date (observed_at <= DATE) and run the review loop
in dry-run mode. Used to ask whether the system would have flagged a turn with the evidence available then."""

from __future__ import annotations

from datetime import UTC, datetime, time
from datetime import date as date_type

import psycopg

from argusvue.agents.packet import build_packet
from argusvue.agents.review import review_thesis
from argusvue.store.blobs import BlobStore


def replay(conn: psycopg.Connection, thesis_id: int, as_of: date_type, *, client=None, store: BlobStore | None = None,
           force: bool = True) -> dict:
    as_of_dt = datetime.combine(as_of, time.max).replace(tzinfo=UTC)
    packet = build_packet(conn, thesis_id, as_of=as_of_dt)
    outcome = review_thesis(conn, thesis_id, trigger=f"replay:{as_of.isoformat()}", force=force, as_of=as_of_dt, dry_run=True,
                            client=client, store=store)
    return {"as_of": as_of.isoformat(), "packet": {"claims": len(packet.claims), "indicators": len(packet.indicators),
                                                   "delta": packet.delta, "version": packet.current["version"]},
            "outcome": outcome.summary(), "proposal": outcome.proposal.model_dump() if outcome.proposal else None,
            "critic": outcome.critic.model_dump() if outcome.critic else None,
            "validation": {"errors": outcome.validation.errors, "flags": outcome.validation.flags} if outcome.validation else None}
