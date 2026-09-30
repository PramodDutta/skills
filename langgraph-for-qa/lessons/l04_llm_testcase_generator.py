"""
Lesson 4 - Adding Claude: a Test Case Generator with a quality gate
===================================================================
Give it a user story + acceptance criteria, get back reviewed, structured test cases.

  START -> generate_test_cases (LLM) -> review_test_cases (plain Python) -> export_csv -> END
                  ^                               |
                  +------- "revise" + feedback ---+

New concepts:
  * calling an LLM inside a node
  * structured output: the LLM returns a Pydantic object, not free text to parse
  * the generator -> reviewer loop (also called "evaluator-optimizer")
  * deterministic checks as guardrails around a non-deterministic LLM

Run it:
  python lessons/l04_llm_testcase_generator.py                              # offline
  ANTHROPIC_API_KEY=sk-ant-... python lessons/l04_llm_testcase_generator.py # real Claude
"""

import csv
import io
import re
from pathlib import Path
from typing import Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from qa_llm import get_llm, last_human_text, mode_banner

MAX_DRAFTS = 3
MIN_CASES = 6
REQUIRED_TYPES = ("positive", "negative", "edge", "security")


# --- The shape we want back from the LLM ---------------------------------------
class TestCase(BaseModel):
    id: str = Field(description="Short id such as TC-01")
    title: str
    type: Literal["positive", "negative", "edge", "security"]
    priority: Literal["P1", "P2", "P3"]
    covers: list[str] = Field(description="Acceptance criteria ids this case verifies, e.g. ['AC1']")
    preconditions: str
    steps: list[str] = Field(description="One concrete action per step")
    expected_result: str = Field(description="Observable, specific outcome")


class TestSuite(BaseModel):
    test_cases: list[TestCase]


class DesignState(TypedDict):
    user_story: str
    suite: TestSuite
    drafts: int
    review_issues: list[str]
    csv: str


SYSTEM_PROMPT = """You are a senior QA engineer writing manual test cases.
- Cover every acceptance criterion and list the AC ids each case verifies in `covers`.
- Include positive, negative, edge-case (boundaries, expiry, limits) and security test cases.
- Write concrete steps a new tester can follow, one action per step, with real test data.
- Expected results must be observable and specific: exact messages, states or values."""


# --- Nodes -----------------------------------------------------------------------
def review_test_cases(state: DesignState) -> dict:
    """A deterministic quality gate. Don't just trust the LLM - check its work."""
    cases = state["suite"].test_cases
    issues = []

    if len(cases) < MIN_CASES:
        issues.append(f"Only {len(cases)} test cases; write at least {MIN_CASES}.")
    required_acs = re.findall(r"^(AC\d+):", state["user_story"], flags=re.MULTILINE)
    covered = {ac for case in cases for ac in case.covers}
    for ac in required_acs:
        if ac not in covered:
            issues.append(f"{ac} is not covered by any test case.")
    present_types = {case.type for case in cases}
    for case_type in REQUIRED_TYPES:
        if case_type not in present_types:
            issues.append(f"No '{case_type}' test case.")
    for case in cases:
        if len(case.steps) < 2:
            issues.append(f"{case.id} needs at least 2 steps.")
        if not case.expected_result.strip():
            issues.append(f"{case.id} has no expected result.")
    titles = [case.title.lower() for case in cases]
    if len(titles) != len(set(titles)):
        issues.append("Duplicate test case titles.")

    return {"review_issues": issues}


def export_csv(state: DesignState) -> dict:
    """CSV that test-management tools (TestRail, Xray, Zephyr...) can import."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ID", "Title", "Type", "Priority", "Covers", "Preconditions", "Steps", "Expected"])
    for case in state["suite"].test_cases:
        steps = "\n".join(f"{i}. {step}" for i, step in enumerate(case.steps, start=1))
        writer.writerow([case.id, case.title, case.type, case.priority, ",".join(case.covers),
                         case.preconditions, steps, case.expected_result])
    return {"csv": buffer.getvalue()}


def route_after_review(state: DesignState) -> Literal["revise", "approve"]:
    if state["review_issues"] and state["drafts"] < MAX_DRAFTS:
        return "revise"
    return "approve"  # clean, or out of drafts (the issues stay in the state for a human)


def build_graph(llm=None):
    """llm=None uses Claude or the offline fake (see qa_llm.py). Tests pass their own."""
    llm = llm or get_llm(offline=offline_test_designer)
    # json_schema = Claude's native structured output: the reply is guaranteed to match TestSuite.
    designer = llm.with_structured_output(TestSuite, method="json_schema")

    def generate_test_cases(state: DesignState) -> dict:
        messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(state["user_story"])]
        if state.get("review_issues"):
            feedback = "\n".join(f"- {issue}" for issue in state["review_issues"])
            messages.append(HumanMessage(
                f"The reviewer rejected your previous draft:\n{feedback}\n\n"
                f"Previous draft:\n{state['suite'].model_dump_json(indent=2)}\n\n"
                "Return the complete, corrected test suite."
            ))
        suite = designer.invoke(messages)
        return {"suite": suite, "drafts": state.get("drafts", 0) + 1}

    builder = StateGraph(DesignState)
    builder.add_node("generate_test_cases", generate_test_cases)
    builder.add_node("review_test_cases", review_test_cases)
    builder.add_node("export_csv", export_csv)
    builder.add_edge(START, "generate_test_cases")
    builder.add_edge("generate_test_cases", "review_test_cases")
    builder.add_conditional_edges("review_test_cases", route_after_review,
                                  {"revise": "generate_test_cases", "approve": "export_csv"})
    builder.add_edge("export_csv", END)
    return builder.compile()


USER_STORY = """\
Story: Password reset via email
As a registered user I want to reset my password through an emailed link,
so that I can get back into my account when I forget my password.

Acceptance criteria:
AC1: The "Forgot password" page accepts a registered email and shows "Check your inbox".
AC2: An unregistered email shows the SAME message (never reveal which emails exist).
AC3: The reset link expires after 30 minutes and works only once.
AC4: The new password must be 8-64 characters with at least one number and one special character.
AC5: After a successful reset, all other active sessions of the user are logged out.
"""


# --- Offline mode: what the fake "LLM" answers ------------------------------------
def _case(id, title, type, priority, covers, preconditions, steps, expected):
    return {"id": id, "title": title, "type": type, "priority": priority, "covers": covers,
            "preconditions": preconditions, "steps": steps, "expected_result": expected}


FIRST_DRAFT = {"test_cases": [
    _case("TC-01", "Request a reset link for a registered email", "positive", "P1", ["AC1"],
          "priya@example.com is a registered user",
          ["Open /forgot-password", "Enter priya@example.com", "Click 'Send reset link'"],
          "'Check your inbox' is shown and the reset email arrives within 1 minute"),
    _case("TC-02", "Unregistered email gets the same message", "negative", "P1", ["AC2"],
          "nobody@example.com is not registered",
          ["Open /forgot-password", "Enter nobody@example.com", "Click 'Send reset link'"],
          "The same 'Check your inbox' message is shown and no email is sent"),
    _case("TC-03", "Reset with a valid new password", "positive", "P1", ["AC4"],
          "A fresh reset link for priya@example.com",
          ["Open the reset link", "Enter Secure#2026 in both password fields", "Click 'Reset password'"],
          "Success message is shown and priya can log in with Secure#2026"),
    _case("TC-04", "Reject a password without a special character", "negative", "P2", ["AC4"],
          "A fresh reset link for priya@example.com",
          ["Open the reset link", "Enter Password2026 in both fields", "Click 'Reset password'"],
          "Error 'Password must contain a number and a special character'; password unchanged"),
]}

FULL_SUITE = {"test_cases": FIRST_DRAFT["test_cases"] + [
    _case("TC-05", "Reset link expires after 30 minutes", "edge", "P1", ["AC3"],
          "A reset link was requested",
          ["Request a reset link", "Wait 31 minutes (or move the test server clock)", "Open the link"],
          "'This link has expired' is shown and the password cannot be changed"),
    _case("TC-06", "Reset link cannot be used twice", "security", "P1", ["AC3"],
          "A reset link was requested",
          ["Reset the password using the link", "Open the same link again"],
          "'This link has already been used' is shown"),
    _case("TC-07", "Password length boundaries 7/8/64/65", "edge", "P2", ["AC4"],
          "A fresh reset link for priya@example.com",
          ["Try the 7-character password Ab#1234", "Try the 8-character password Ab#12345",
           "Try a 64-character password", "Try a 65-character password"],
          "7 and 65 characters are rejected; 8 and 64 characters are accepted"),
    _case("TC-08", "Other sessions are logged out after a reset", "security", "P1", ["AC5"],
          "priya is logged in on Chrome and on Firefox",
          ["Reset the password from Chrome", "Refresh any page in Firefox"],
          "The Firefox session is logged out and redirected to the login page"),
    _case("TC-09", "Response time does not reveal registered emails", "security", "P2", ["AC2"],
          "One registered and one unregistered email",
          ["Submit the registered email 10 times and record response times",
           "Submit the unregistered email 10 times and record response times"],
          "Average response times differ by less than 100 ms"),
]}


def offline_test_designer(messages):
    """Pretend LLM: a weak first draft, then a complete suite once it gets feedback."""
    if "rejected your previous draft" in last_human_text(messages):
        return FULL_SUITE
    return FIRST_DRAFT


if __name__ == "__main__":
    print(mode_banner())
    graph = build_graph()

    final_state = {}
    for step in graph.stream({"user_story": USER_STORY}, stream_mode="updates"):
        for node, update in step.items():
            if node == "generate_test_cases":
                print(f"\n[draft {update['drafts']}] {len(update['suite'].test_cases)} test cases generated")
            elif node == "review_test_cases":
                issues = update["review_issues"]
                print("[review] APPROVED" if not issues else f"[review] {len(issues)} issue(s):")
                for issue in issues:
                    print(f"   - {issue}")
            final_state.update(update)

    print("\nFinal test suite:")
    for case in final_state["suite"].test_cases:
        print(f"  {case.id} [{case.type:8}] {case.priority} {','.join(case.covers):7} {case.title}")

    out = Path("reports/l04_test_cases.csv")
    out.parent.mkdir(exist_ok=True)
    out.write_text(final_state["csv"], encoding="utf-8")
    print(f"\nCSV for TestRail / Xray / Zephyr written to {out}")
