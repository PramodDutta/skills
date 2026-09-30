"""Lessons 6-9: checkpoints, human-in-the-loop, parallelism and multi-agent teams."""

import pytest
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from pydantic import ValidationError

import l06_memory_checkpoints as l06
import l07_human_in_the_loop as l07
import l08_parallel_send as l08
import l09_multi_agent_qa_team as l09
from qa_llm import OfflineLLM


# --- Lesson 6: checkpoints ---------------------------------------------------------------------
@pytest.fixture
def fresh_l06(monkeypatch):
    monkeypatch.setattr(l06, "executions", l06.Counter())
    monkeypatch.setitem(l06.grid, "up", True)
    return l06


def test_crashed_run_resumes_without_rerunning_passed_stages(fresh_l06):
    graph = fresh_l06.build_graph()
    config = {"configurable": {"thread_id": "t1"}}
    fresh_l06.grid["up"] = False
    with pytest.raises(ConnectionError):
        graph.invoke({"build": "2.4.1", "channel": "#qa"}, config)

    assert graph.get_state(config).next == ("ui_tests",)

    fresh_l06.grid["up"] = True
    final = graph.invoke(None, config)
    assert len(final["results"]) == 4
    assert fresh_l06.executions["smoke_tests"] == 1 and fresh_l06.executions["ui_tests"] == 2


def test_fork_from_an_old_checkpoint_reruns_only_what_follows(fresh_l06):
    graph = fresh_l06.build_graph()
    config = {"configurable": {"thread_id": "t2"}}
    graph.invoke({"build": "2.4.1", "channel": "#qa"}, config)
    before_publish = next(c for c in graph.get_state_history(config) if c.next == ("publish_report",))

    fork = graph.update_state(before_publish.config, {"channel": "#release"})
    assert "#release" in graph.invoke(None, fork)["report"]
    assert fresh_l06.executions["api_tests"] == 1 and fresh_l06.executions["publish_report"] == 2


def test_memory_is_per_thread():
    assistant = l06.build_assistant()
    first, second = ({"configurable": {"thread_id": t}} for t in ("a", "b"))
    assistant.invoke({"messages": [HumanMessage("We are testing build 1.2.3")]}, first)
    ask = {"messages": [HumanMessage("Which build am I testing?")]}
    assert "1.2.3" in assistant.invoke(ask, first)["messages"][-1].text
    assert "don't know" in assistant.invoke(ask, second)["messages"][-1].text


# --- Lesson 7: human-in-the-loop -----------------------------------------------------------------
@pytest.fixture
def paused_bug(monkeypatch):
    """A run paused at qa_lead_review, waiting for the human."""
    monkeypatch.setattr(l07, "JIRA", [])
    graph = l07.build_graph()
    config = {"configurable": {"thread_id": "bug"}}
    result = graph.invoke({"evidence": l07.EVIDENCE[1]}, config)
    return graph, config, result


def test_run_pauses_with_the_draft_for_the_qa_lead(paused_bug):
    graph, config, result = paused_bug
    payload = result["__interrupt__"][0].value
    assert graph.get_state(config).next == ("qa_lead_review",)
    assert payload["draft"]["title"].startswith("POST /booking returns 200 instead of 400")
    assert l07.JIRA == []  # nothing filed before the human decides


@pytest.mark.parametrize("decision, outcome_start, tickets", [
    ({"action": "approve"}, "filed QA-101 [minor]", 1),
    ({"action": "edit", "changes": {"severity": "critical"}}, "filed QA-101 [critical]", 1),
    ({"action": "reject", "reason": "duplicate"}, "closed, not a bug: duplicate", 0),
])
def test_every_human_decision_path(paused_bug, decision, outcome_start, tickets):
    graph, config, _ = paused_bug
    final = graph.invoke(Command(resume=decision), config)
    assert final["outcome"].startswith(outcome_start)
    assert len(l07.JIRA) == tickets


def test_invalid_human_edits_are_rejected(paused_bug):
    graph, config, _ = paused_bug
    with pytest.raises(ValidationError):
        graph.invoke(Command(resume={"action": "edit", "changes": {"severity": "blocker"}}), config)


# --- Lesson 8: parallel execution -----------------------------------------------------------------
@pytest.mark.parametrize("tag, count", [("smoke", 4), ("regression", 12)])
def test_fan_out_runs_one_worker_per_selected_test(booking_api, monkeypatch, tag, count):
    monkeypatch.setattr(l08, "SIMULATED_TEST_SECONDS", 0)
    final = l08.build_graph().invoke({"base_url": booking_api.base_url, "tag": tag})
    assert len(final["results"]) == count


def test_regression_run_catches_the_planted_bugs(booking_api, monkeypatch):
    monkeypatch.setattr(l08, "SIMULATED_TEST_SECONDS", 0)
    final = l08.build_graph().invoke({"base_url": booking_api.base_url, "tag": "regression"},
                                     {"max_concurrency": 4})
    assert {r["id"] for r in final["results"] if not r["passed"]} == {"TC05", "TC07", "TC08", "TC09"}
    assert final["summary"].startswith("8/12 passed")


# --- Lesson 9: multi-agent team ----------------------------------------------------------------------
def test_lead_delegates_in_the_expected_order(booking_api):
    final = l09.build_graph().invoke({"feature": "bookings", "base_url": booking_api.base_url},
                                     {"recursion_limit": 25})
    decisions = [line.split("-> ")[1].split(":")[0] for line in final["log"] if line.startswith("test_lead")]
    assert decisions == ["test_designer", "test_executor", "bug_reporter", "FINISH"]
    assert len(final["bug_reports"]) == sum(not r["passed"] for r in final["results"]) == 2


def test_a_confused_lead_is_stopped_by_the_step_budget(booking_api):
    def always_design(messages):
        if messages[0].text.startswith("You are the Test Lead"):
            return l09.LeadDecision(next="test_designer", reason="design more!")
        return l09.offline_team(messages)

    graph = l09.build_graph(llm=OfflineLLM(respond=always_design))
    with pytest.raises(GraphRecursionError):
        graph.invoke({"feature": "bookings", "base_url": booking_api.base_url}, {"recursion_limit": 25})


def test_request_bodies_survive_structured_output():
    """body_json is a string in the schema; a free-form dict would only allow {}."""
    schema = l09.TestPlan.model_json_schema()["$defs"]["APITest"]["properties"]
    assert schema["body_json"]["type"] == "string"
