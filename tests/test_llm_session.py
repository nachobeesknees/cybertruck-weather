"""Session provider: requests land in the inbox, answers are read from the outbox, validated and logged."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from argusvue import llm
from argusvue.settings import get_settings
from argusvue.store.blobs import BlobStore

pytestmark = pytest.mark.db


class Verdict(BaseModel):
    material: bool
    reason: str


@pytest.fixture
def session_mode(monkeypatch, tmp_path, db):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "session")
    monkeypatch.setattr(s, "out_dir", tmp_path)
    with db.cursor() as cur:
        cur.execute("TRUNCATE agent_runs, open_questions RESTART IDENTITY CASCADE")
    db.commit()
    return db, tmp_path


def test_request_written_then_pending(session_mode):
    db, out = session_mode
    with pytest.raises(llm.PendingAnswer) as ei:
        llm.complete(db, "triage", system="SYS", user="packet", schema=Verdict, store=BlobStore(out / "b"))
    req = json.loads(ei.value.path.read_text())
    assert req["role"] == "triage" and req["user"] == "packet" and req["model"] == "claude-haiku-5-5" and "schema" in req
    assert (out / "llm" / "inbox" / f"{ei.value.request_id}.json").exists()
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM agent_runs")
        assert cur.fetchone()["n"] == 0


def test_answer_is_validated_and_logged(session_mode):
    db, out = session_mode
    rid = llm.request_id_for("triage", "SYS", "packet")
    (out / "llm" / "outbox").mkdir(parents=True, exist_ok=True)
    (out / "llm" / "outbox" / f"{rid}.json").write_text(json.dumps({"answered_by": "sonnet-agent-3", "answer": {"material": True, "reason": "new permits"}}))
    res = llm.complete(db, "triage", system="SYS", user="packet", schema=Verdict, store=BlobStore(out / "b"))
    assert res.parsed.material is True and res.cost_usd == 0.0 and res.model == "session"
    with db.cursor() as cur:
        cur.execute("SELECT model, ok, cost_usd FROM agent_runs")
        r = cur.fetchone()
    assert r["model"] == "session:sonnet-agent-3" and r["ok"] is True and float(r["cost_usd"]) == 0.0


def test_bad_answer_creates_a_reprompt_request(session_mode):
    db, out = session_mode
    rid = llm.request_id_for("triage", "SYS", "packet")
    (out / "llm" / "outbox").mkdir(parents=True, exist_ok=True)
    (out / "llm" / "outbox" / f"{rid}.json").write_text('{"material": "maybe"}')
    with pytest.raises(llm.PendingAnswer) as ei:
        llm.complete(db, "triage", system="SYS", user="packet", schema=Verdict, store=BlobStore(out / "b"))
    second = json.loads(ei.value.path.read_text())
    assert second["attempt"] == 2 and "failed validation" in second["user"]
    with db.cursor() as cur:
        cur.execute("SELECT ok, attempt FROM agent_runs ORDER BY id")
        assert [(r["ok"], r["attempt"]) for r in cur.fetchall()] == [(False, 1)]
