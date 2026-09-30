"""Lessons 4-5: testing workflows that contain an LLM - without calling one."""

import csv
import io
import json
import re

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

import l04_llm_testcase_generator as l04
import l05_tools_api_agent as l05
from qa_llm import OfflineLLM


# --- Lesson 4: the deterministic quality gate -----------------------------------------------
def gate(suite: dict) -> list[str]:
    state = {"user_story": l04.USER_STORY, "suite": l04.TestSuite.model_validate(suite)}
    return l04.review_test_cases(state)["review_issues"]


def test_gate_rejects_the_weak_first_draft_with_specific_feedback():
    assert gate(l04.FIRST_DRAFT) == [
        "Only 4 test cases; write at least 6.",
        "AC3 is not covered by any test case.",
        "AC5 is not covered by any test case.",
        "No 'edge' test case.",
        "No 'security' test case.",
    ]


def test_gate_approves_the_complete_suite():
    assert gate(l04.FULL_SUITE) == []


def test_generator_loop_revises_once_then_exports_csv():
    fake = OfflineLLM(respond=l04.offline_test_designer)
    final = l04.build_graph(llm=fake).invoke({"user_story": l04.USER_STORY})

    assert final["drafts"] == 2 and final["review_issues"] == []
    rows = list(csv.reader(io.StringIO(final["csv"])))
    assert len(rows) == 1 + 9  # header + 9 test cases


def test_reviewer_feedback_reaches_the_second_prompt():
    """Assert on what the LLM was ASKED - a fake model records every prompt."""
    fake = OfflineLLM(respond=l04.offline_test_designer)
    l04.build_graph(llm=fake).invoke({"user_story": l04.USER_STORY})

    second_prompt = fake.calls[1][-1].text
    assert "AC3 is not covered" in second_prompt
    assert '"TC-01"' in second_prompt  # the previous draft is included for revision


def test_loop_stops_at_max_drafts_when_the_llm_never_improves():
    stubborn = OfflineLLM(respond=lambda messages: l04.FIRST_DRAFT)
    final = l04.build_graph(llm=stubborn).invoke({"user_story": l04.USER_STORY})

    assert final["drafts"] == l04.MAX_DRAFTS
    assert final["review_issues"]  # approved with issues left for a human
    assert len(stubborn.calls) == l04.MAX_DRAFTS


def test_schema_sent_to_claude_restricts_test_types():
    """A contract test: inspect the exact JSON schema Claude would receive - no API call."""
    claude = ChatAnthropic(model="claude-opus-5-5", api_key="sk-not-used")
    structured = claude.with_structured_output(l04.TestSuite, method="json_schema")
    schema = structured.first.kwargs["output_config"]["format"]["schema"]

    case = schema["$defs"]["TestCase"]
    assert case["properties"]["type"]["enum"] == ["positive", "negative", "edge", "security"]
    assert set(case["required"]) >= {"covers", "steps", "expected_result"}


# --- Lesson 5: tools, then the agent ------------------------------------------------------------
def test_send_request_tool_returns_status_and_body(booking_api):
    read_spec, send_request = l05.make_tools(booking_api.base_url)
    result = json.loads(send_request.invoke({"method": "GET", "path": "booking/1"}))  # no leading slash
    assert result["status"] == 200 and "Sally" in result["body"]


def test_send_request_tool_returns_errors_instead_of_raising():
    _, send_request = l05.make_tools("http://127.0.0.1:1")  # nothing listens here
    assert "error" in json.loads(send_request.invoke({"method": "GET", "path": "/ping"}))


def failed_checks(report: str) -> set[str]:
    return set(re.findall(r"^FAIL\s+(.+?):", report, flags=re.MULTILINE))


PLANTED = {"missing firstname", "negative totalprice", "checkout before checkin", "non-numeric id"}


def test_agent_finds_exactly_the_planted_bugs(booking_api):
    """Assert on OUTCOMES (which bugs), never on the exact wording of the report."""
    graph = l05.build_agent_graph(booking_api.base_url)
    final = graph.invoke({"messages": [HumanMessage(l05.MISSION)]}, {"recursion_limit": 40})
    assert failed_checks(final["messages"][-1].text) == PLANTED


def test_prebuilt_agent_behaves_like_the_hand_built_one(booking_api):
    agent = l05.build_prebuilt_agent(booking_api.base_url)
    final = agent.invoke({"messages": [HumanMessage(l05.MISSION)]}, {"recursion_limit": 40})
    assert failed_checks(final["messages"][-1].text) == PLANTED


def test_recursion_limit_caps_a_long_running_agent(booking_api):
    graph = l05.build_agent_graph(booking_api.base_url)
    with pytest.raises(GraphRecursionError):
        graph.invoke({"messages": [HumanMessage(l05.MISSION)]}, {"recursion_limit": 6})
