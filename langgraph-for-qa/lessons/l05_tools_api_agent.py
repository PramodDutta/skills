"""
Lesson 5 - Tools & Agents: an API Test Agent
============================================
Until now WE decided every step. An AGENT decides for itself: the LLM reads the
API spec, chooses which requests to send, looks at each response and keeps going
until it can write a test report.

  START -> agent (LLM) --has tool calls?--> tools (run them) --> agent --> ... --> END

New concepts:
  * @tool             turn a Python function into something the LLM can call
  * bind_tools        tell the model which tools exist
  * MessagesState     state = the conversation (a list of messages)
  * ToolNode          prebuilt node that runs the tool calls the LLM asked for
  * tools_condition   prebuilt router: tool calls -> "tools", otherwise -> END
  * the same agent in one line with create_agent

The API under test is the local Booking API (booking_api.py). It has planted bugs.

Run it:
  python lessons/l05_tools_api_agent.py              # hand-built agent graph
  python lessons/l05_tools_api_agent.py --prebuilt   # the same agent via create_agent
"""

import copy
import json
import sys
from typing import Literal

import requests
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from booking_api import API_SPEC, VALID_BOOKING, start_booking_api
from qa_llm import get_llm, mode_banner

SYSTEM_PROMPT = """You are an API test engineer testing the Booking API against its spec.
1. Read the spec first.
2. Send real requests: the happy path, then each validation rule, unknown and malformed ids,
   and requests without a token.
3. Compare every actual status code with what the spec requires.
4. Finish with a short report: one PASS/FAIL line per check, then a "Bugs found" list with
   the exact request that reproduces each bug."""

MISSION = "Test POST /booking (all validation rules), GET /booking/{id} and DELETE without a token."


def make_tools(base_url: str) -> list:
    """Tools are built for ONE base URL: the agent cannot call anything else."""

    @tool
    def read_api_spec() -> str:
        """Return the Booking API requirements: endpoints, fields and validation rules."""
        return API_SPEC

    @tool
    def send_request(
        method: Literal["GET", "POST", "PUT", "DELETE"],
        path: str,
        body: dict | None = None,
        token: str | None = None,
    ) -> str:
        """Send an HTTP request to the Booking API. Returns the status code and response body.

        Args:
            method: HTTP method.
            path: Path only, e.g. /booking or /booking/1. The base URL is added for you.
            body: JSON body for POST and PUT.
            token: Token from POST /auth. Sent as the 'token' cookie; PUT and DELETE need it.
        """
        try:
            response = requests.request(
                method, base_url + "/" + path.lstrip("/"), json=body,
                cookies={"token": token} if token else None, timeout=10,
            )
        except requests.RequestException as error:
            # Return errors as text so the LLM can react. An exception raised here
            # would crash the whole graph run.
            return json.dumps({"error": str(error)})
        return json.dumps({"status": response.status_code, "body": response.text[:500]})

    return [read_api_spec, send_request]


def build_agent_graph(base_url: str, llm=None):
    """The ReAct loop, built by hand so you can see every piece."""
    tools = make_tools(base_url)
    llm = (llm or get_llm(offline=offline_api_tester)).bind_tools(tools)

    def agent(state: MessagesState) -> dict:
        reply = llm.invoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]])
        return {"messages": [reply]}  # add_messages reducer: APPENDS to the conversation

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)  # tool calls? "tools" : END
    builder.add_edge("tools", "agent")  # after running tools, let the LLM look at the results
    return builder.compile()


def build_prebuilt_agent(base_url: str, llm=None):
    """Exactly the same agent in one call."""
    return create_agent(
        llm or get_llm(offline=offline_api_tester),
        tools=make_tools(base_url),
        system_prompt=SYSTEM_PROMPT,
    )


# --- Offline mode: a scripted "agent brain" -----------------------------------------
def _booking(**changes) -> dict:
    booking = copy.deepcopy(VALID_BOOKING)
    booking.update(changes)
    return booking


_NO_FIRSTNAME = {k: v for k, v in VALID_BOOKING.items() if k != "firstname"}
_DATES_REVERSED = {"checkin": "2026-02-10", "checkout": "2026-02-01"}

# (check description, tool args, status code the spec requires)
OFFLINE_PLAN = [
    ("health check", {"method": "GET", "path": "/ping"}, 201),
    ("create a valid booking", {"method": "POST", "path": "/booking", "body": _booking()}, 200),
    ("missing firstname", {"method": "POST", "path": "/booking", "body": _NO_FIRSTNAME}, 400),
    ("negative totalprice", {"method": "POST", "path": "/booking", "body": _booking(totalprice=-50)}, 400),
    ("checkout before checkin", {"method": "POST", "path": "/booking",
                                 "body": _booking(bookingdates=_DATES_REVERSED)}, 400),
    ("unknown id", {"method": "GET", "path": "/booking/999999"}, 404),
    ("non-numeric id", {"method": "GET", "path": "/booking/abc"}, 404),
    ("delete without token", {"method": "DELETE", "path": "/booking/1"}, 403),
]


def offline_api_tester(messages):
    """Pretend LLM: read the spec, run the plan one request per turn, then write the report."""
    results = [m for m in messages if isinstance(m, ToolMessage)]
    step = len(results)
    if step == 0:
        return AIMessage(content="", tool_calls=[{"name": "read_api_spec", "args": {}, "id": "call_0"}])
    if step <= len(OFFLINE_PLAN):
        _, args, _ = OFFLINE_PLAN[step - 1]
        return AIMessage(content="", tool_calls=[{"name": "send_request", "args": args, "id": f"call_{step}"}])

    lines, bugs = ["## API test report", ""], []
    for (check, args, expected), result in zip(OFFLINE_PLAN, results[1:]):
        actual = json.loads(result.content).get("status")
        verdict = "PASS" if actual == expected else "FAIL"
        lines.append(f"{verdict}  {check}: {args['method']} {args['path']} -> {actual} (spec: {expected})")
        if verdict == "FAIL":
            bugs.append(f"- {check}: {args['method']} {args['path']} returned {actual}, spec requires {expected}")
    return "\n".join(lines + ["", "### Bugs found", *(bugs or ["- none"])])


def print_step(update: dict) -> None:
    for message in update.get("messages", []):
        if isinstance(message, AIMessage) and message.tool_calls:
            for call in message.tool_calls:
                args = json.dumps(call["args"])
                print(f"[agent] -> {call['name']}({args[:90]}{'...' if len(args) > 90 else ''})")
        elif isinstance(message, ToolMessage):
            print(f"[tool]  <- {message.content[:90].replace(chr(10), ' ')}")
        elif isinstance(message, AIMessage):
            print(f"\n[agent] final answer:\n{message.text}")


if __name__ == "__main__":
    print(mode_banner())
    with start_booking_api() as api:
        prebuilt = "--prebuilt" in sys.argv
        graph = build_prebuilt_agent(api.base_url) if prebuilt else build_agent_graph(api.base_url)
        print(f"Agent: {'create_agent (prebuilt)' if prebuilt else 'hand-built graph'}\n")
        try:
            # recursion_limit caps the agent loop: no runaway agent, no runaway bill.
            for step in graph.stream({"messages": [HumanMessage(MISSION)]}, {"recursion_limit": 40},
                                     stream_mode="updates"):
                for update in step.values():
                    print_step(update or {})
        except GraphRecursionError:
            print("\nStopped: the agent hit recursion_limit before finishing.")
