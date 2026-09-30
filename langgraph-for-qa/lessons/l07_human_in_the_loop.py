"""
Lesson 7 - Human-in-the-loop: the QA Lead approves bug reports
==============================================================
An agent may DRAFT a bug report. A human decides whether it gets FILED.

  START -> draft_bug_report (LLM) -> qa_lead_review (PAUSES) --approve/edit--> file_in_jira -> END
                                                             --reject-------> close_as_not_a_bug -> END

New concepts:
  * interrupt(payload)      pause the graph and hand `payload` to a human
  * Command(resume=value)   continue the paused run with the human's answer
  * a checkpointer is REQUIRED - the paused run is saved while it waits (minutes or days)
  * on resume, the paused node runs again FROM ITS START - keep side effects after interrupt()

Run it:
  python lessons/l07_human_in_the_loop.py          # YOU are the QA lead: type your decisions
  python lessons/l07_human_in_the_loop.py --auto   # scripted decisions: approve, edit, reject
"""

import json
import sys
from typing import Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from qa_llm import get_llm, last_human_text, mode_banner

JIRA: list[dict] = []  # a pretend Jira project
SEVERITIES = ("critical", "major", "minor")


class BugReport(BaseModel):
    title: str = Field(description="One line: what is broken and where")
    severity: Literal["critical", "major", "minor"]
    component: str
    steps_to_reproduce: list[str]
    expected: str
    actual: str


class BugState(TypedDict):
    evidence: dict
    draft: dict  # BugReport.model_dump(): checkpointed state should be plain data
    decision: dict
    ticket: str
    outcome: str


SYSTEM_PROMPT = """You are a QA engineer turning test evidence into a bug report.
Be precise: the exact request, the expected and the actual result.
Severity: critical = data loss, security hole or outage; major = wrong behaviour in a main flow;
minor = cosmetic or rare edge case."""


def qa_lead_review(state: BugState) -> dict:
    # Everything BEFORE interrupt() runs again when the graph resumes, so keep it side-effect free.
    decision = interrupt({
        "question": "File this bug? approve / edit / reject",
        "draft": state["draft"],
    })
    return {"decision": decision}


def route_decision(state: BugState) -> Literal["file", "close"]:
    return "close" if state["decision"]["action"] == "reject" else "file"


def file_in_jira(state: BugState) -> dict:
    changes = state["decision"].get("changes", {}) if state["decision"]["action"] == "edit" else {}
    # model_validate re-checks the report, including the human's edits: validate human input too!
    report = BugReport.model_validate({**state["draft"], **changes})
    ticket = f"QA-{101 + len(JIRA)}"
    JIRA.append({"key": ticket, **report.model_dump()})
    return {"ticket": ticket, "outcome": f"filed {ticket} [{report.severity}] {report.title}"}


def close_as_not_a_bug(state: BugState) -> dict:
    return {"outcome": f"closed, not a bug: {state['decision'].get('reason', 'no reason given')}"}


def build_graph(llm=None, checkpointer=None):
    writer = (llm or get_llm(offline=offline_bug_writer)).with_structured_output(
        BugReport, method="json_schema")

    def draft_bug_report(state: BugState) -> dict:
        evidence = json.dumps(state["evidence"], indent=2)
        draft = writer.invoke([SystemMessage(SYSTEM_PROMPT), HumanMessage(f"Test evidence:\n{evidence}")])
        return {"draft": draft.model_dump()}

    builder = StateGraph(BugState)
    builder.add_node("draft_bug_report", draft_bug_report)
    builder.add_node("qa_lead_review", qa_lead_review)
    builder.add_node("file_in_jira", file_in_jira)
    builder.add_node("close_as_not_a_bug", close_as_not_a_bug)
    builder.add_edge(START, "draft_bug_report")
    builder.add_edge("draft_bug_report", "qa_lead_review")
    builder.add_conditional_edges("qa_lead_review", route_decision,
                                  {"file": "file_in_jira", "close": "close_as_not_a_bug"})
    builder.add_edge("file_in_jira", END)
    builder.add_edge("close_as_not_a_bug", END)
    # interrupt() needs a checkpointer: the paused state has to be stored somewhere.
    return builder.compile(checkpointer=checkpointer or InMemorySaver())


# Evidence from Lesson 5's agent run (plus one "finding" that is not really a bug).
EVIDENCE = [
    {"check": "missing firstname", "request": "POST /booking with a body that has no firstname",
     "expected_status": 400, "actual_status": 500, "actual_body": "Internal Server Error"},
    {"check": "negative totalprice", "request": "POST /booking with totalprice -50",
     "expected_status": 400, "actual_status": 200, "actual_body": '{"bookingid": 5, ...}'},
    {"check": "delete a booking", "request": "DELETE /booking/1 with a valid token",
     "expected_status": 200, "actual_status": 201, "actual_body": "Created"},
]

AUTO_DECISIONS = [
    {"action": "approve"},
    {"action": "edit", "changes": {"severity": "major"}},
    {"action": "reject", "reason": "documented quirk - the API spec says DELETE returns 201"},
]


def offline_bug_writer(messages):
    """Pretend LLM: fills a bug report template from the evidence in the prompt."""
    evidence = json.loads(last_human_text(messages).split("Test evidence:\n", 1)[1])
    method, path = evidence["request"].split()[:2]
    return BugReport(
        title=f"{method} {path} returns {evidence['actual_status']} instead of "
              f"{evidence['expected_status']} ({evidence['check']})",
        severity="major" if evidence["actual_status"] >= 500 else "minor",
        component="Booking API",
        steps_to_reproduce=[f"Send {evidence['request']}", "Check the response status code"],
        expected=f"HTTP {evidence['expected_status']}",
        actual=f"HTTP {evidence['actual_status']}: {evidence['actual_body']}",
    )


def ask_qa_lead() -> dict:
    """The human side of the interrupt: read a decision from the keyboard."""
    while True:
        answer = input("approve / edit / reject > ").strip().lower()
        if answer in ("approve", "a"):
            return {"action": "approve"}
        if answer in ("reject", "r"):
            return {"action": "reject", "reason": input("reason > ").strip()}
        if answer in ("edit", "e"):
            severity = ""
            while severity not in SEVERITIES:
                severity = input(f"new severity {SEVERITIES} > ").strip().lower()
            return {"action": "edit", "changes": {"severity": severity}}


if __name__ == "__main__":
    auto = "--auto" in sys.argv
    print(mode_banner())
    graph = build_graph()

    for number, evidence in enumerate(EVIDENCE):
        config = {"configurable": {"thread_id": f"bug-{number + 1}"}}
        result = graph.invoke({"evidence": evidence}, config)

        while "__interrupt__" in result:  # the graph is paused, waiting for a human
            draft = result["__interrupt__"][0].value["draft"]
            print(f"\n--- PAUSED before {graph.get_state(config).next[0]}: the QA lead must decide ---")
            print(f"  title:    {draft['title']}")
            print(f"  severity: {draft['severity']}")
            print(f"  expected: {draft['expected']}   actual: {draft['actual']}")
            decision = AUTO_DECISIONS[number] if auto else ask_qa_lead()
            print(f"  QA lead says: {decision}")
            result = graph.invoke(Command(resume=decision), config)  # resume the SAME thread

        print(f"==> {result['outcome']}")

    print("\nJira project QA:")
    for ticket in JIRA:
        print(f"  {ticket['key']} [{ticket['severity']}] {ticket['title']}")
