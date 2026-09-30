"""
Lesson 9 - Multi-agent: a QA team led by a Test Lead (supervisor)
=================================================================
One agent doing everything gets confused and its prompt keeps growing. A team of
specialists with narrow jobs, coordinated by a lead, stays focused:

                      +--> test_designer  (LLM) ------+
  START -> test_lead -+--> test_executor  (subgraph) -+--> back to test_lead ... --> END
                      +--> bug_reporter   (LLM) ------+

New concepts:
  * the supervisor pattern   an LLM decides which specialist works next
  * Command(goto, update)    a node that updates the state AND picks the next node
  * subgraphs                a whole compiled graph (the executor) used inside another graph
  * constrained decisions    the lead can only choose from a fixed list (Literal)

Run it:
  python lessons/l09_multi_agent_qa_team.py
"""

import json
import operator
import re
from typing import Annotated, Literal, TypedDict

import requests
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send
from pydantic import BaseModel, Field

from booking_api import API_SPEC, start_booking_api
from qa_llm import get_llm, last_human_text, mode_banner


# --- What each agent must return -------------------------------------------------
class LeadDecision(BaseModel):
    next: Literal["test_designer", "test_executor", "bug_reporter", "FINISH"]
    reason: str


class APITest(BaseModel):
    id: str
    name: str
    method: Literal["GET", "POST", "PUT", "DELETE"]
    path: str
    # A JSON *string*, not a dict: structured output only allows objects with known fields,
    # so a free-form dict would always come back empty.
    body_json: str = Field(description='The JSON request body as a string, or "" for no body')
    expected_status: int = Field(description="The status code the SPEC requires")


class TestPlan(BaseModel):
    tests: list[APITest]


class BugReports(BaseModel):
    reports: list[str] = Field(description="One concise bug report per failed test")


class TeamState(TypedDict):
    feature: str
    base_url: str
    tests: list[dict]
    results: list[dict]
    bug_reports: list[str]
    log: Annotated[list[str], operator.add]  # the team's conversation, for humans to read


LEAD_PROMPT = """You are the Test Lead of a QA team. Decide who works next:
- test_designer: designs API test cases (only when no tests exist yet)
- test_executor: runs the designed tests against the API (only when tests exist but have not run)
- bug_reporter: writes bug reports for failed tests (only when failures have no reports yet)
- FINISH: when there is nothing left to do"""

DESIGNER_PROMPT = """You are an API test designer. Design 6-10 API test cases for the feature:
the happy path plus every validation rule in the spec. Use paths only, e.g. /booking/1.
expected_status is what the SPEC requires, even if you suspect the API is buggy."""

REPORTER_PROMPT = """You are a bug reporter. For each failed test write one concise bug report:
a title, the exact request, expected vs actual status."""


# --- The executor is its own small graph (Lesson 8), used here as a subgraph ------------
class ExecutorState(TypedDict):
    base_url: str
    tests: list[dict]
    results: Annotated[list[dict], operator.add]


def run_one(job: dict) -> dict:
    test = job["test"]
    body = json.loads(test["body_json"]) if test["body_json"] else None
    response = requests.request(test["method"], job["base_url"] + test["path"], json=body, timeout=10)
    return {"results": [{
        "id": test["id"], "name": test["name"], "request": f"{test['method']} {test['path']}",
        "body_json": test["body_json"], "expected": test["expected_status"],
        "actual": response.status_code, "passed": response.status_code == test["expected_status"],
    }]}


def build_executor():
    builder = StateGraph(ExecutorState)
    builder.add_node("run_one", run_one)
    builder.add_conditional_edges(
        START, lambda s: [Send("run_one", {"base_url": s["base_url"], "test": t}) for t in s["tests"]],
        ["run_one"])
    builder.add_edge("run_one", END)
    return builder.compile()


def status_report(state: TeamState) -> str:
    results = state.get("results", [])
    failed = [r for r in results if not r["passed"]]
    return (f"Feature under test: {state['feature']}\n"
            f"Test cases designed: {len(state.get('tests', []))}\n"
            f"Tests executed: {len(results)} ({len(failed)} failed)\n"
            f"Bug reports written: {len(state.get('bug_reports', []))}")


def build_graph(llm=None):
    llm = llm or get_llm(offline=offline_team)
    decide = llm.with_structured_output(LeadDecision, method="json_schema")
    design = llm.with_structured_output(TestPlan, method="json_schema")
    write_bugs = llm.with_structured_output(BugReports, method="json_schema")
    executor = build_executor()

    # The Command return type tells LangGraph (and the diagram) where this node can go.
    def test_lead(state: TeamState) -> Command[Literal["test_designer", "test_executor", "bug_reporter", "__end__"]]:
        decision = decide.invoke([SystemMessage(LEAD_PROMPT), HumanMessage(status_report(state))])
        return Command(
            goto=END if decision.next == "FINISH" else decision.next,
            update={"log": [f"test_lead     -> {decision.next}: {decision.reason}"]},
        )

    def test_designer(state: TeamState) -> dict:
        plan = design.invoke([SystemMessage(DESIGNER_PROMPT),
                              HumanMessage(f"Feature: {state['feature']}\n\nAPI spec:\n{API_SPEC}")])
        tests = [test.model_dump() for test in plan.tests]
        return {"tests": tests, "log": [f"test_designer: designed {len(tests)} tests"]}

    def test_executor(state: TeamState) -> dict:
        # Call the subgraph like any runnable, mapping our state to its input.
        out = executor.invoke({"base_url": state["base_url"], "tests": state["tests"]})
        failed = sum(not r["passed"] for r in out["results"])
        return {"results": out["results"],
                "log": [f"test_executor: ran {len(out['results'])} tests in parallel, {failed} failed"]}

    def bug_reporter(state: TeamState) -> dict:
        failed = [r for r in state["results"] if not r["passed"]]
        reports = write_bugs.invoke([SystemMessage(REPORTER_PROMPT),
                                     HumanMessage(f"Failed tests:\n{json.dumps(failed, indent=2)}")]).reports
        return {"bug_reports": reports, "log": [f"bug_reporter : wrote {len(reports)} bug reports"]}

    builder = StateGraph(TeamState)
    builder.add_node("test_lead", test_lead)
    builder.add_node("test_designer", test_designer)
    builder.add_node("test_executor", test_executor)
    builder.add_node("bug_reporter", bug_reporter)
    builder.add_edge(START, "test_lead")
    for worker in ("test_designer", "test_executor", "bug_reporter"):
        builder.add_edge(worker, "test_lead")  # every specialist reports back to the lead
    return builder.compile()


# --- Offline mode: one pretend LLM that plays all three roles ---------------------------
OFFLINE_TESTS = [
    {"id": "T1", "name": "health check", "method": "GET", "path": "/ping", "body_json": "", "expected_status": 201},
    {"id": "T2", "name": "get existing booking", "method": "GET", "path": "/booking/1", "body_json": "", "expected_status": 200},
    {"id": "T3", "name": "get non-numeric id", "method": "GET", "path": "/booking/abc", "body_json": "", "expected_status": 404},
    {"id": "T4", "name": "create valid booking", "method": "POST", "path": "/booking", "expected_status": 200,
     "body_json": '{"firstname": "Asha", "lastname": "Rao", "totalprice": 120, "depositpaid": true, '
                  '"bookingdates": {"checkin": "2026-07-01", "checkout": "2026-07-03"}}'},
    {"id": "T5", "name": "create without lastname", "method": "POST", "path": "/booking", "expected_status": 400,
     "body_json": '{"firstname": "Asha", "totalprice": 120, "depositpaid": true, '
                  '"bookingdates": {"checkin": "2026-07-01", "checkout": "2026-07-03"}}'},
    {"id": "T6", "name": "create with string price", "method": "POST", "path": "/booking", "expected_status": 400,
     "body_json": '{"firstname": "Asha", "lastname": "Rao", "totalprice": "abc", "depositpaid": true, '
                  '"bookingdates": {"checkin": "2026-07-01", "checkout": "2026-07-03"}}'},
    {"id": "T7", "name": "delete without token", "method": "DELETE", "path": "/booking/2", "body_json": "", "expected_status": 403},
]


def offline_team(messages):
    role = messages[0].text
    status = last_human_text(messages)
    if role.startswith("You are the Test Lead"):
        counts = status.split("\n", 1)[1]  # skip the feature line, read the 4 numbers
        designed, executed, failed, reported = (int(n) for n in re.findall(r"\d+", counts))
        if designed == 0:
            return LeadDecision(next="test_designer", reason="no test cases yet")
        if executed == 0:
            return LeadDecision(next="test_executor", reason=f"{designed} tests are ready to run")
        if failed > reported:
            return LeadDecision(next="bug_reporter", reason=f"{failed} failures need bug reports")
        return LeadDecision(next="FINISH", reason="tests ran and every failure has a bug report")
    if role.startswith("You are an API test designer"):
        return TestPlan(tests=OFFLINE_TESTS)
    failed = json.loads(status.split("Failed tests:\n", 1)[1])
    return BugReports(reports=[
        f"{r['request']} ({r['name']}) returns {r['actual']}, expected {r['expected']}" for r in failed])


if __name__ == "__main__":
    print(mode_banner())
    with start_booking_api() as api:
        graph = build_graph()
        final = graph.invoke(
            {"feature": "Booking API: create, read and delete bookings", "base_url": api.base_url},
            {"recursion_limit": 25},  # the team as a whole gets a step budget too
        )
    print("\nTeam log:")
    for line in final["log"]:
        print(f"  {line}")
    print("\nBug reports:")
    for report in final.get("bug_reports", []):
        print(f"  - {report}")
