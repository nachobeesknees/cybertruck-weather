"""llm.py contract with a fake client: spend guard, logging, validation re-prompt, refusal."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, field_validator

from argusvue import llm
from argusvue.store.blobs import BlobStore

pytestmark = pytest.mark.db


class Verdict(BaseModel):
    material: bool
    reason: str

    @field_validator("reason")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must not be empty")
        return v


def _resp(parsed=None, text="", stop="end_turn", inp=1000, out=500, cache_read=0, searches=0):
    usage = SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cache_read,
                            cache_creation_input_tokens=0,
                            server_tool_use=SimpleNamespace(web_search_requests=searches) if searches else None)
    content = [SimpleNamespace(type="text", text=text, citations=None)] if text else []
    return SimpleNamespace(parsed_output=parsed, usage=usage, content=content, stop_reason=stop, stop_details=None,
                           model="claude-sonnet-5-5")


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = self

    def parse(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def create(self, **kw):
        return self.parse(**kw)


@pytest.fixture
def clean_runs(db):
    with db.cursor() as cur:
        cur.execute("TRUNCATE agent_runs, open_questions RESTART IDENTITY CASCADE")
    db.commit()
    return db


def _runs(db):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM agent_runs ORDER BY id")
        return cur.fetchall()


def test_success_logs_run_with_cost(clean_runs, tmp_path):
    client = FakeClient([_resp(parsed=Verdict(material=True, reason="new permit data"), text='{"material": true}')])
    res = llm.complete(clean_runs, "triage", system="SYS", user="packet", schema=Verdict, region_id="phx",
                       client=client, store=BlobStore(tmp_path))
    assert res.parsed.material is True and res.attempts == 1
    runs = _runs(clean_runs)
    assert len(runs) == 1
    r = runs[0]
    assert r["role"] == "triage" and r["model"] == "claude-haiku-5-5" and r["ok"] is True
    assert r["input_tokens"] == 1000 and r["output_tokens"] == 500
    # haiku: 1000 * 0.10/1M + 500 * 0.50/1M
    assert float(r["cost_usd"]) == pytest.approx(0.0001 + 0.00025)
    assert r["prompt_hash"] == llm.prompt_hash("SYS") and r["input_ref"].startswith("local://")
    assert client.calls[0]["model"] == "claude-haiku-5-5"
    assert client.calls[0]["output_config"] == {"effort": "low"}


def test_validation_failure_reprompts_once_with_errors(clean_runs, tmp_path):
    client = FakeClient([
        _resp(parsed=None, text='{"material": true, "reason": "   "}'),
        _resp(parsed=Verdict(material=False, reason="nothing new")),
    ])
    res = llm.complete(clean_runs, "triage", system="SYS", user="packet", schema=Verdict, client=client,
                       store=BlobStore(tmp_path))
    assert res.parsed.reason == "nothing new" and res.attempts == 2
    runs = _runs(clean_runs)
    assert [r["ok"] for r in runs] == [False, True] and [r["attempt"] for r in runs] == [1, 2]
    assert "reason must not be empty" in runs[0]["error"]
    second_user = client.calls[1]["messages"][0]["content"]
    assert "failed validation" in second_user and "reason must not be empty" in second_user


def test_two_failures_raise_loudly(clean_runs, tmp_path):
    client = FakeClient([_resp(parsed=None, text="not json at all"), _resp(parsed=None, text="still not json")])
    with pytest.raises(llm.LLMError, match="failed validation twice"):
        llm.complete(clean_runs, "triage", system="SYS", user="p", schema=Verdict, client=client, store=BlobStore(tmp_path))
    assert [r["ok"] for r in _runs(clean_runs)] == [False, False]


def test_spend_cap_blocks_before_calling(clean_runs, tmp_path, monkeypatch):
    with clean_runs.cursor() as cur:
        cur.execute("""INSERT INTO agent_runs(role, model, input_tokens, output_tokens, cost_usd, ok)
                       VALUES ('analyst', 'claude-opus-5-5', 1, 1, %s, true)""", (llm.get_settings().daily_spend_cap_usd,))
    clean_runs.commit()
    client = FakeClient([_resp(parsed=Verdict(material=True, reason="x"))])
    with pytest.raises(llm.SpendCapExceeded):
        llm.complete(clean_runs, "triage", system="SYS", user="p", schema=Verdict, client=client, store=BlobStore(tmp_path))
    assert client.calls == []
    assert len(_runs(clean_runs)) == 1


def test_refusal_is_logged_and_raised(clean_runs, tmp_path):
    client = FakeClient([_resp(parsed=None, text="", stop="refusal")])
    with pytest.raises(llm.LLMError, match="refusal"):
        llm.complete(clean_runs, "analyst", system="SYS", user="p", schema=Verdict, client=client, store=BlobStore(tmp_path))
    r = _runs(clean_runs)[0]
    assert r["ok"] is False and r["model"] == "claude-opus-5-5"


def test_tools_path_parses_text_json_and_bills_searches(clean_runs, tmp_path):
    client = FakeClient([_resp(parsed=None, text='Here you go:\n```json\n{"material": true, "reason": "found"}\n```',
                               inp=2000, out=300, searches=3)])
    res = llm.complete(clean_runs, "scout", system="SYS", user="p", schema=Verdict, client=client,
                       store=BlobStore(tmp_path), tools=[llm.web_search_tool(max_uses=3)])
    assert res.parsed.reason == "found" and res.usage.search_uses == 3
    r = _runs(clean_runs)[0]
    # sonnet: 2000*2/1M + 300*10/1M + 3 * 10/1000
    assert float(r["cost_usd"]) == pytest.approx(0.004 + 0.003 + 0.03)
    assert client.calls[0]["tools"][0]["type"] == llm.WEB_SEARCH_TOOL


def test_today_spend_sums_local_day(clean_runs):
    with clean_runs.cursor() as cur:
        cur.execute("INSERT INTO agent_runs(role, model, cost_usd, ok) VALUES ('x','m',1.25,true), ('y','m',0.5,false)")
        cur.execute("INSERT INTO agent_runs(role, model, cost_usd, ok, created_at) VALUES ('z','m',9,true, now() - interval '2 days')")
    clean_runs.commit()
    assert llm.today_spend(clean_runs) == pytest.approx(1.75)
