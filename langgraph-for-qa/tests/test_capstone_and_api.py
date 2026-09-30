"""The capstone end-to-end, the practice API itself, and a smoke run of every lesson script."""

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests
from langgraph.checkpoint.memory import InMemorySaver

import capstone_api_qa_agent as cap
from booking_api import TOKEN, VALID_BOOKING


# --- Capstone -----------------------------------------------------------------------------------
@pytest.fixture
def empty_jira(monkeypatch):
    monkeypatch.setattr(cap, "JIRA", [])


def test_capstone_finds_retries_and_files_all_planted_bugs(flaky_booking_api, empty_jira, no_sleep):
    final = cap.run(flaky_booking_api.base_url, auto=True)
    assert final["filed"] == ["QA-101", "QA-102", "QA-103", "QA-104"]
    assert {f["id"] for f in final["findings"]} == {"T05", "T07", "T08", "T09"}
    assert "**9/13 tests passed**" in final["report"]  # the 503s were retried, not reported


def test_capstone_files_only_the_bugs_the_human_approved(booking_api, empty_jira, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "1,3")
    final = cap.run(booking_api.base_url, auto=False)
    assert len(final["filed"]) == 2 and len(cap.JIRA) == 2


def test_capstone_waits_for_approval_before_filing(booking_api, empty_jira):
    graph = cap.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t"}}
    paused = graph.invoke({"base_url": booking_api.base_url}, config)
    assert graph.get_state(config).next == ("human_review",)
    assert len(paused["__interrupt__"][0].value["bugs"]) == 4
    assert cap.JIRA == []


@pytest.mark.parametrize("result, category", [
    ({"passed": True, "actual": 200, "expected_status": 200}, "passed"),
    ({"passed": False, "actual": 500, "expected_status": 400}, "product_bug"),
    ({"passed": False, "actual": 200, "expected_status": 400}, "product_bug"),
    ({"passed": False, "actual": 404, "expected_status": 400}, "needs_review"),
])
def test_triage_classification(result, category):
    assert cap.classify(result) == category


def test_plan_gate_rejects_a_thin_plan():
    thin = [cap.OFFLINE_PLAN[0], cap.OFFLINE_PLAN[2]]
    issues = cap.review_plan({"plan": thin})["plan_issues"]
    assert "Only 2 tests; write at least 8." in issues
    assert "No test for DELETE /booking/{id}." in issues


# --- The practice API: known bugs as strict xfail -----------------------------------------------
def post(api, body):
    return requests.post(f"{api.base_url}/booking", json=body, timeout=10)


def test_valid_booking_is_created(booking_api):
    response = post(booking_api, VALID_BOOKING)
    assert response.status_code == 200 and response.json()["booking"]["firstname"] == "Jim"


def test_delete_needs_a_token(booking_api):
    assert requests.delete(f"{booking_api.base_url}/booking/1", timeout=10).status_code == 403
    assert requests.delete(f"{booking_api.base_url}/booking/1", cookies={"token": TOKEN},
                           timeout=10).status_code == 201


# strict=True: the day a bug gets fixed, the xfail "unexpectedly passes" and the suite tells you.
@pytest.mark.xfail(strict=True, reason="planted bug: missing field returns 500")
def test_missing_field_returns_400(booking_api):
    body = {k: v for k, v in VALID_BOOKING.items() if k != "lastname"}
    assert post(booking_api, body).status_code == 400


@pytest.mark.xfail(strict=True, reason="planted bug: negative price accepted")
def test_negative_price_returns_400(booking_api):
    assert post(booking_api, {**VALID_BOOKING, "totalprice": -1}).status_code == 400


@pytest.mark.xfail(strict=True, reason="planted bug: reversed dates accepted")
def test_checkout_before_checkin_returns_400(booking_api):
    body = copy.deepcopy(VALID_BOOKING)
    body["bookingdates"] = {"checkin": "2026-02-10", "checkout": "2026-02-01"}
    assert post(booking_api, body).status_code == 400


@pytest.mark.xfail(strict=True, reason="planted bug: non-numeric id returns 500")
def test_non_numeric_id_returns_404(booking_api):
    assert requests.get(f"{booking_api.base_url}/booking/abc", timeout=10).status_code == 404


# --- Every lesson script still runs (the course's own smoke test) -----------------------------------
LESSONS = Path(__file__).parent.parent / "lessons"


@pytest.mark.parametrize("script, args", [
    ("l01_first_graph.py", []), ("l02_conditional_routing.py", []), ("l03_loops_retry.py", []),
    ("l04_llm_testcase_generator.py", []), ("l05_tools_api_agent.py", []),
    ("l05_tools_api_agent.py", ["--prebuilt"]), ("l06_memory_checkpoints.py", []),
    ("l07_human_in_the_loop.py", ["--auto"]), ("l08_parallel_send.py", []),
    ("l09_multi_agent_qa_team.py", []), ("capstone_api_qa_agent.py", ["--auto"]),
])
def test_lesson_script_runs(script, args, tmp_path):
    result = subprocess.run([sys.executable, str(LESSONS / script), *args], cwd=tmp_path,
                            env={**os.environ, "QA_OFFLINE": "1"}, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
