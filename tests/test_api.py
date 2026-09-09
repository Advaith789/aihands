"""The agent-facing surface and the operator console's backend."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aihands.api.app import build_api
from aihands.api import store

CAP = "mendota.member.open_savings_subaccount"
OP = {"operator_id": "OP-77"}


@pytest.fixture
def client():
    return TestClient(build_api(), follow_redirects=False)


@pytest.fixture
def restore_approval():
    """Approval is written back to disk, so a test that changes it must put it
    back -- otherwise the next test sees state it did not create."""
    before = store.load(CAP)[0].approval
    yield
    cap, path = store.load(CAP)
    import json
    path.write_text(json.dumps(cap.model_copy(update={"approval": before})
                               .model_dump(mode="json"), indent=2))


def test_health_reports_the_catalogue(client):
    body = client.get("/api/health").json()
    assert body["ok"] and body["capabilities"] >= 1


def test_the_catalogue_lists_capabilities(client):
    rows = client.get("/api/capabilities").json()
    assert any(r["capability_id"] == CAP for r in rows)


@pytest.mark.parametrize("field", [
    "capability_id", "version", "name", "description", "input_schema",
    "output_schema", "outcomes", "approved", "requires_approval", "commits_state",
])
def test_an_agent_sees_everything_it_needs_to_decide(client, field):
    row = next(r for r in client.get("/api/capabilities").json()
               if r["capability_id"] == CAP)
    assert field in row


def test_the_input_schema_is_valid_json_schema(client):
    schema = client.get(f"/api/capabilities/{CAP}").json()["input_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) <= set(schema["properties"])


def test_declared_outcomes_are_published_with_their_category(client):
    outcomes = client.get(f"/api/capabilities/{CAP}").json()["outcomes"]
    assert outcomes and {"name", "code", "category"} <= set(outcomes[0])
    assert any(o["category"] == "BUSINESS" for o in outcomes)


def test_the_detail_view_shows_steps_without_leaking_locator_internals(client):
    steps = client.get(f"/api/capabilities/{CAP}").json()["steps"]
    assert steps and {"id", "intent", "action", "risk", "commits"} <= set(steps[0])
    assert "candidates" not in steps[0]


def test_an_unknown_capability_is_a_404(client):
    assert client.get("/api/capabilities/nope").status_code == 404
    assert client.post("/api/capabilities/nope/invoke",
                       json={"params": {}}).status_code == 404


def test_a_draft_capability_that_changes_state_will_not_run(client, restore_approval):
    store.revoke(CAP)
    body = client.post(f"/api/capabilities/{CAP}/invoke",
                       json={"params": {**OP, "member_id": "M-1001", "deposit": 500},
                             "allow_risky": True}).json()
    assert body["code"] == "NOT_APPROVED"


def test_invoking_returns_the_typed_result(client, reset, restore_approval):
    reset()
    store.approve(CAP, "reviewer@mendota")
    body = client.post(f"/api/capabilities/{CAP}/invoke",
                       json={"params": {**OP, "member_id": "M-1001", "deposit": 500},
                             "allow_risky": True}).json()
    assert body["category"] == "SUCCESS"
    assert body["outputs"]["confirmation_number"].startswith("SAV-")
    assert body["llm_calls"] == 0


def test_a_business_answer_comes_back_as_a_successful_call(client, reset, restore_approval):
    reset()
    store.approve(CAP, "reviewer@mendota")
    body = client.post(f"/api/capabilities/{CAP}/invoke",
                       json={"params": {**OP, "member_id": "M-9999", "deposit": 500},
                             "allow_risky": True}).json()
    assert body["category"] == "BUSINESS" and body["outcome"]["name"] == "member_not_found"
    assert body["error"] is None


def test_nothing_is_waiting_on_a_human_by_default(client):
    assert client.get("/api/interventions").json() == []


def test_acting_on_an_unknown_run_is_a_404(client):
    assert client.post("/api/interventions/nope/take",
                       json={"decision": "", "operator": "x"}).status_code == 404
    assert client.post("/api/interventions/nope/resume",
                       json={"decision": "CONTINUE"}).status_code == 404


def test_the_console_renders(client):
    page = client.get("/")
    assert page.status_code == 200 and "aihands" in page.text
