"""
qa_llm.py - the ONE place every lesson gets its LLM from.

Two modes:

  REAL     ANTHROPIC_API_KEY is set          -> Claude, via langchain-anthropic
  OFFLINE  no key, or QA_OFFLINE=1           -> OfflineLLM, a small fake model that
                                                answers with a Python function you give it

Why have an offline mode at all?
  1. You can learn every LangGraph concept with no API key and no cost.
  2. It is exactly how we TEST agents (Lesson 10): swap the real model for a
     deterministic fake, so a test never depends on what the LLM felt like
     saying today.

Settings (environment variables):
  ANTHROPIC_API_KEY   your key -> switches the lessons to REAL mode
  QA_MODEL            Claude model id (default: claude-opus-5-5)
  QA_EFFORT           how hard Claude thinks: low | medium | high | xhigh | max
  QA_OFFLINE=1        force OFFLINE mode even when a key is set (the tests do this)
"""

from __future__ import annotations

import os
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, convert_to_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import PrivateAttr

DEFAULT_MODEL = "claude-opus-5-5"

# A responder receives the prompt (a list of messages) and returns the reply:
#   str             -> becomes the AI message text
#   AIMessage       -> returned as-is (use this to script tool calls)
#   dict / model    -> the answer for .with_structured_output(...)
Responder = Callable[[list[BaseMessage]], Any]


def is_offline() -> bool:
    return os.getenv("QA_OFFLINE") == "1" or not os.getenv("ANTHROPIC_API_KEY")


def get_llm(offline: Responder) -> BaseChatModel:
    """Return Claude when an API key is available, otherwise an OfflineLLM.

    `offline` is the function the fake model answers with. Every lesson passes
    one, so every lesson runs end-to-end with or without a key.
    """
    if is_offline():
        return OfflineLLM(respond=offline)

    from langchain_anthropic import ChatAnthropic

    model = os.getenv("QA_MODEL", DEFAULT_MODEL)
    extra: dict[str, Any] = {}
    if model.startswith(("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")):
        extra = {
            # Thinking depth: low | medium | high | xhigh | max.
            "reasoning_effort": os.getenv("QA_EFFORT", "medium"),
            # If a safety filter declines a request, the API re-runs it on a
            # recommended fallback model instead of returning a refusal.
            "betas": ["server-side-fallback-2026-07-01"],
            "model_kwargs": {"fallbacks": "default"},
        }
    return ChatAnthropic(model=model, max_tokens=16000, **extra)


def mode_banner() -> str:
    if is_offline():
        return "LLM mode: OFFLINE (scripted fake model - set ANTHROPIC_API_KEY to use Claude)"
    return f"LLM mode: Claude ({os.getenv('QA_MODEL', DEFAULT_MODEL)})"


class OfflineLLM(BaseChatModel):
    """A deterministic stand-in for a chat model.

    It supports the three things the lessons use:
      .invoke(messages)                 -> AIMessage
      .bind_tools(tools)                -> itself (the responder scripts tool calls)
      .with_structured_output(Schema)   -> returns Schema objects (Pydantic only)
    """

    respond: Responder
    _calls: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "offline-fake"

    @property
    def calls(self) -> list[list[BaseMessage]]:
        """Every prompt this model received - handy for assertions in tests."""
        return self._calls

    def _answer(self, messages: list[BaseMessage]) -> Any:
        self._calls.append(list(messages))
        return self.respond(list(messages))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        reply = self._answer(messages)
        message = reply if isinstance(reply, AIMessage) else AIMessage(content=str(reply))
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        # The responder decides when to "call" a tool by returning an AIMessage
        # with tool_calls, so there is nothing to bind.
        return self

    def with_structured_output(self, schema, **kwargs):
        def run(prompt: Any) -> Any:
            reply = self._answer(_to_messages(prompt))
            return reply if isinstance(reply, schema) else schema.model_validate(reply)

        return RunnableLambda(run)


def _to_messages(prompt: Any) -> list[BaseMessage]:
    if hasattr(prompt, "to_messages"):  # a ChatPromptValue
        return prompt.to_messages()
    if isinstance(prompt, str):
        return [HumanMessage(prompt)]
    return convert_to_messages(prompt)


def last_human_text(messages: list[BaseMessage]) -> str:
    """Offline responders use this to peek at what they were asked."""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.text
    return ""
