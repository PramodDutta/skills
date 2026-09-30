"""
Capstone - an Autonomous API QA Agent
=====================================
Everything from Lessons 1-9 in one workflow:

  START -> plan_tests (LLM, L4) -> review_plan (quality gate, L4) --revise--> plan_tests
                                        | approved
                                        v
                                      login (a "session fixture") -> execute_test x N  (Send, L8)
                                                                      + RetryPolicy on 503 (L3)
                                        v
                                      triage (router, L2) --no bugs--> write_report -> END
                                        | bugs
                                        v
                                      draft_bugs (LLM) -> human_review (interrupt, L7) -> file_bugs -> write_report
  ...all of it checkpointed (L6), so a run can pause for a human and resume later.

Run it:
  python lessons/capstone_api_qa_agent.py            # you approve the bugs
  python lessons/capstone_api_qa_agent.py --auto     # approve every bug automatically
"""

import json
import operator
import sys
from pathlib import Path
from typing import Annotated, Literal, TypedDict

import requests
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, Send, interrupt
from pydantic import BaseModel, Field

from booking_api import API_SPEC, start_booking_api
from qa_llm import get_llm, last_human_text, mode_banner

MAX_PLAN_DRAFTS = 2
JIRA: list[dict] = []


# --- LLM output schemas ------------------------------------------------------------
class APITest(BaseModel):
    id: str = Field(description="T01, T02, ...")
    name: str
    rule: str = Field(description="The spec rule this test verifies")
    method: Literal["GET", "POST", "PUT", "DELETE"]
    path: str = Field(description="Concrete path, e.g. /booking/1")
    body_json: str = Field(description='JSON request body as a string, or "" for none')
    needs_token: bool = Field(description="Send the auth token cookie")
    expected_status: int = Field(description="The status the SPEC requires")


class TestPlan(BaseModel):
    tests: list[APITest]


class BugReport(BaseModel):
    title: str
    severity: Literal["critical", "major", "minor"]
    steps_to_reproduce: list[str]
    expected: str
    actual: str


class BugDrafts(BaseModel):
    bugs: list[BugReport]


# --- State ---------------------------------------------------------------------------
class QAState(TypedDict):
    base_url: str
    plan: list[dict]
    plan_issues: list[str]
    plan_drafts: int
    token: str
    results: Annotated[list[dict], operator.add]
    findings: list[dict]
    bug_drafts: list[dict]
    filed: list[str]
    report: str


PLANNER_PROMPT = """You are a senior API test designer. Write an API test plan for the spec below.
- Cover every endpoint and every validation rule; include positive and negative tests.
- Seed data: bookings 1, 2 and 3 exist. Use 999999 for an id that does not exist.
- Tests run IN PARALLEL: no test may depend on another. Only booking 3 may be changed or deleted,
  and by one test at most.
- expected_status is what the SPEC requires, even where you suspect the API is buggy."""

BUG_WRITER_PROMPT = """You are a QA engineer. Write one precise bug report per finding below:
exact request, expected vs actual. Severity: critical = data loss/security/outage,
major = wrong behaviour in a main flow, minor = rare edge case."""


class ServiceUnavailable(Exception):
    """Raised for a 503 so LangGraph's RetryPolicy re-runs the test (flaky environment)."""


# --- Plain-Python nodes -------------------------------------------------------------
def review_plan(state: QAState) -> dict:
    tests, issues = state["plan"], []
    if len(tests) < 8:
        issues.append(f"Only {len(tests)} tests; write at least 8.")
    for method, prefix in (("GET", "/booking/"), ("POST", "/booking"), ("PUT", "/booking/"), ("DELETE", "/booking/")):
        if not any(t["method"] == method and t["path"].startswith(prefix) for t in tests):
            issues.append(f"No test for {method} {prefix}{'{id}' if prefix.endswith('/') else ''}.")
    if sum(t["expected_status"] >= 400 for t in tests) < 4:
        issues.append("Fewer than 4 negative tests.")
    for t in tests:
        if not t["path"].startswith("/"):
            issues.append(f"{t['id']}: path must start with '/'.")
        if t["body_json"]:
            try:
                json.loads(t["body_json"])
            except json.JSONDecodeError:
                issues.append(f"{t['id']}: body_json is not valid JSON.")
    return {"plan_issues": issues}


def route_plan(state: QAState) -> Literal["revise", "run"]:
    return "revise" if state["plan_issues"] and state["plan_drafts"] < MAX_PLAN_DRAFTS else "run"


def login(state: QAState) -> dict:
    """Log in once for the whole run, like a session-scoped fixture."""
    response = requests.post(f"{state['base_url']}/auth",
                             json={"username": "admin", "password": "password123"}, timeout=10)
    return {"token": response.json()["token"]}


def fan_out(state: QAState) -> list[Send]:
    return [Send("execute_test", {"base_url": state["base_url"], "token": state["token"], "test": t})
            for t in state["plan"]]


def execute_test(job: dict) -> dict:
    test = job["test"]
    response = requests.request(
        test["method"], job["base_url"] + test["path"],
        json=json.loads(test["body_json"]) if test["body_json"] else None,
        cookies={"token": job["token"]} if test["needs_token"] else None, timeout=10)
    if response.status_code == 503:
        # one write() per line: parallel workers can't interleave their output
        sys.stdout.write(f"   {test['id']} got 503 Service Unavailable - RetryPolicy will re-run it\n")
        raise ServiceUnavailable(test["id"])
    return {"results": [{**test, "actual": response.status_code, "body": response.text[:200],
                         "passed": response.status_code == test["expected_status"]}]}


def classify(result: dict) -> str:
    actual, expected = result["actual"], result["expected_status"]
    if result["passed"]:
        return "passed"
    if actual >= 500:
        return "product_bug"  # the server crashed on a request the spec defines
    if actual < 300 and expected >= 400:
        return "product_bug"  # invalid input was accepted: validation is missing
    return "needs_review"     # e.g. 404 vs 400 - a spec gap or a test design issue


def triage(state: QAState) -> dict:
    findings = [{**r, "category": classify(r)} for r in state["results"] if not r["passed"]]
    return {"findings": sorted(findings, key=lambda f: f["id"])}


def route_triage(state: QAState) -> Literal["bugs", "clean"]:
    return "bugs" if any(f["category"] == "product_bug" for f in state["findings"]) else "clean"


def human_review(state: QAState) -> dict:
    answer = interrupt({"question": "Which bugs should be filed? A list of numbers, 'all' or 'none'.",
                        "bugs": state["bug_drafts"]})
    chosen = range(len(state["bug_drafts"])) if answer == "all" else [] if answer == "none" else answer
    return {"bug_drafts": [state["bug_drafts"][i] for i in chosen]}


def file_bugs(state: QAState) -> dict:
    keys = []
    for bug in state["bug_drafts"]:
        keys.append(f"QA-{101 + len(JIRA)}")
        JIRA.append({"key": keys[-1], **bug})
    return {"filed": keys}


def write_report(state: QAState) -> dict:
    results = sorted(state["results"], key=lambda r: r["id"])
    passed = sum(r["passed"] for r in results)
    lines = [
        "# API QA Report - Booking API", "",
        f"**{passed}/{len(results)} tests passed**", "",
        "| Test | Request | Expected | Actual | Result |", "|---|---|---|---|---|",
        *[f"| {r['id']} {r['name']} | `{r['method']} {r['path']}` | {r['expected_status']} | "
          f"{r['actual']} | {'PASS' if r['passed'] else 'FAIL'} |" for r in results],
        "", "## Bugs filed",
        *([f"- {key}: {bug['title']} ({bug['severity']})"
           for key, bug in zip(state.get("filed", []), state.get("bug_drafts", []))] or ["- none"]),
        "", "## Needs human review",
        *([f"- {f['id']} {f['name']}: expected {f['expected_status']}, got {f['actual']}"
           for f in state.get("findings", []) if f["category"] == "needs_review"] or ["- none"]),
    ]
    return {"report": "\n".join(lines)}


# --- The graph -------------------------------------------------------------------------
def build_graph(llm=None, checkpointer=None):
    llm = llm or get_llm(offline=offline_capstone)
    planner = llm.with_structured_output(TestPlan, method="json_schema")
    bug_writer = llm.with_structured_output(BugDrafts, method="json_schema")

    def plan_tests(state: QAState) -> dict:
        messages = [SystemMessage(PLANNER_PROMPT), HumanMessage(f"API spec:\n{API_SPEC}")]
        if state.get("plan_issues"):
            messages.append(HumanMessage("Fix these problems in your previous plan:\n- "
                                         + "\n- ".join(state["plan_issues"])))
        plan = planner.invoke(messages)
        return {"plan": [t.model_dump() for t in plan.tests], "plan_drafts": state.get("plan_drafts", 0) + 1}

    def draft_bugs(state: QAState) -> dict:
        bugs = [f for f in state["findings"] if f["category"] == "product_bug"]
        drafts = bug_writer.invoke([SystemMessage(BUG_WRITER_PROMPT),
                                    HumanMessage(f"Findings:\n{json.dumps(bugs, indent=2)}")])
        return {"bug_drafts": [bug.model_dump() for bug in drafts.bugs]}

    builder = StateGraph(QAState)
    builder.add_node("plan_tests", plan_tests)
    builder.add_node("review_plan", review_plan)
    builder.add_node("login", login)
    builder.add_node("execute_test", execute_test,
                     retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.2, retry_on=ServiceUnavailable))
    builder.add_node("triage", triage)
    builder.add_node("draft_bugs", draft_bugs)
    builder.add_node("human_review", human_review)
    builder.add_node("file_bugs", file_bugs)
    builder.add_node("write_report", write_report)

    builder.add_edge(START, "plan_tests")
    builder.add_edge("plan_tests", "review_plan")
    builder.add_conditional_edges("review_plan", route_plan, {"revise": "plan_tests", "run": "login"})
    builder.add_conditional_edges("login", fan_out, ["execute_test"])
    builder.add_edge("execute_test", "triage")
    builder.add_conditional_edges("triage", route_triage, {"bugs": "draft_bugs", "clean": "write_report"})
    builder.add_edge("draft_bugs", "human_review")
    builder.add_edge("human_review", "file_bugs")
    builder.add_edge("file_bugs", "write_report")
    builder.add_edge("write_report", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())


# --- Offline mode ------------------------------------------------------------------------
def _t(id, name, rule, method, path, expected, body=None, needs_token=False):
    return {"id": id, "name": name, "rule": rule, "method": method, "path": path, "needs_token": needs_token,
            "body_json": json.dumps(body) if body is not None else "", "expected_status": expected}


_BOOKING = {"firstname": "Asha", "lastname": "Rao", "totalprice": 120, "depositpaid": True,
            "bookingdates": {"checkin": "2026-07-01", "checkout": "2026-07-03"}}
_NO_FIRSTNAME = {k: v for k, v in _BOOKING.items() if k != "firstname"}

OFFLINE_PLAN = [
    _t("T01", "health check", "GET /ping -> 201", "GET", "/ping", 201),
    _t("T02", "list bookings", "GET /booking -> 200", "GET", "/booking", 200),
    _t("T03", "get existing booking", "GET /booking/{id} -> 200", "GET", "/booking/1", 200),
    _t("T04", "get unknown booking", "unknown id -> 404", "GET", "/booking/999999", 404),
    _t("T05", "get non-numeric id", "non-numeric id -> 404", "GET", "/booking/abc", 404),
    _t("T06", "create valid booking", "POST /booking -> 200", "POST", "/booking", 200, _BOOKING),
    _t("T07", "create without firstname", "required fields -> 400", "POST", "/booking", 400, _NO_FIRSTNAME),
    _t("T08", "create with negative price", "totalprice >= 0", "POST", "/booking", 400, {**_BOOKING, "totalprice": -1}),
    _t("T09", "create with checkout before checkin", "checkout after checkin", "POST", "/booking", 400,
       {**_BOOKING, "bookingdates": {"checkin": "2026-07-03", "checkout": "2026-07-01"}}),
    _t("T10", "create with text price", "totalprice is a number", "POST", "/booking", 400, {**_BOOKING, "totalprice": "abc"}),
    _t("T11", "update without token", "PUT without token -> 403", "PUT", "/booking/1", 403, _BOOKING),
    _t("T12", "update booking 3 with token", "PUT -> 200", "PUT", "/booking/3", 200, _BOOKING, needs_token=True),
    _t("T13", "delete without token", "DELETE without token -> 403", "DELETE", "/booking/2", 403),
]


def offline_capstone(messages):
    """Pretend LLM for both LLM steps: the planner and the bug writer."""
    if messages[0].text.startswith("You are a senior API test designer"):
        return {"tests": OFFLINE_PLAN}
    findings = json.loads(last_human_text(messages).split("Findings:\n", 1)[1])
    return {"bugs": [{
        "title": f"{f['method']} {f['path']} returns {f['actual']} instead of {f['expected_status']} ({f['name']})",
        "severity": "major" if f["actual"] >= 500 else "minor",
        "steps_to_reproduce": [f"Send {f['method']} {f['path']}" + (f" with body {f['body_json']}" if f["body_json"] else ""),
                               "Check the response status code"],
        "expected": f"HTTP {f['expected_status']} ({f['rule']})",
        "actual": f"HTTP {f['actual']}: {f['body'][:60]}",
    } for f in findings]}


def ask_which_bugs(bugs: list[dict]):
    answer = input("file which bugs? e.g. 1,3 | all | none > ").strip().lower()
    if answer in ("all", "none"):
        return answer
    return [int(n) - 1 for n in answer.replace(" ", "").split(",") if n.isdigit() and 0 < int(n) <= len(bugs)]


def run(base_url: str, auto: bool, graph=None) -> dict:
    graph = graph or build_graph()
    config = {"configurable": {"thread_id": "capstone-run-1"}, "recursion_limit": 60}
    result = graph.invoke({"base_url": base_url}, config)
    while "__interrupt__" in result:
        bugs = result["__interrupt__"][0].value["bugs"]
        print(f"\n--- PAUSED for human review: {len(bugs)} bug draft(s) ---")
        for number, bug in enumerate(bugs, start=1):
            print(f"  {number}. [{bug['severity']}] {bug['title']}")
        answer = "all" if auto else ask_which_bugs(bugs)
        print(f"  reviewer: {answer}")
        result = graph.invoke(Command(resume=answer), config)
    return result


if __name__ == "__main__":
    print(mode_banner())
    # flaky_503=2: the first two GET /booking/{id} calls fail with 503, to exercise RetryPolicy.
    with start_booking_api(flaky_503=2) as api:
        final = run(api.base_url, auto="--auto" in sys.argv)
    out = Path("reports/capstone_report.md")
    out.parent.mkdir(exist_ok=True)
    out.write_text(final["report"], encoding="utf-8")
    print("\n" + final["report"])
    print(f"\nReport saved to {out}")
