"""LLM providers for the planner. FakeLLM is the default: no network, no key."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from events import ACTIVE_ROLES

# Hard ceiling applied to every call, whatever the provider.
MAX_TOKENS = 4096

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

# Built once at import, so the exact same bytes go out on every call and the
# cached prefix stays valid. Never interpolate per-request data into this.
SYSTEM_PROMPT = (
    "You are the planning component of ImportAlpha, a China-to-US product "
    "sourcing intelligence service.\n\n"
    "You answer exactly two kinds of request, distinguished by the first word "
    "of the user message.\n\n"
    "PLAN: - break the stated goal into ordered steps. Reply with ONLY a JSON "
    "object, no prose and no code fences:\n"
    '{"reasoning": "<why this plan>", "steps": [{"role": "<role>", "task": "<what to do>"}]}\n'
    # sorted(): a frozenset's iteration order must not leak into the bytes we
    # send, or the cached prefix changes between processes.
    f"Allowed roles: {', '.join(sorted(ACTIVE_ROLES))}. Use at most 5 steps.\n\n"
    "SUMMARIZE: - the steps of a goal have finished and their artifacts follow. "
    "Reply with 2-3 plain sentences describing what was produced. No JSON.\n\n"
    "Never invent facts, prices, supplier names or measurements. Every real "
    "datapoint must carry a source_url, observed_at and confidence, so when you "
    "have no source, say so rather than guessing."
)


class LLM(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Return the model's text response."""


class FakeLLM(LLM):
    """Deterministic and offline. Default provider, so tests need no API key.

    Pass `responses` to script exact replies for a test; once that queue is
    drained it falls back to the canned plan/summary below.
    """

    def __init__(self, responses: Optional[List[str]] = None) -> None:
        self.responses = list(responses) if responses else []
        self.calls: List[Tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.responses:
            return self.responses.pop(0)
        if user.startswith("SUMMARIZE:"):
            return (
                "Every planned step reported an artifact. The briefs are "
                "placeholders: no sourced datapoints were attached yet."
            )
        return json.dumps(
            {
                "reasoning": "Canned offline plan from FakeLLM.",
                "steps": [
                    {"role": "product", "task": "Shortlist candidate home organization products"},
                    {"role": "product", "task": "Draft a sourcing brief for the shortlist"},
                ],
            }
        )


class AnthropicLLM(LLM):
    def __init__(self, api_key: str, model: str) -> None:
        import anthropic  # lazy: only this provider needs the SDK installed

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": user}],
        )
        # Skip thinking blocks, which are on by default for current models.
        return "".join(block.text for block in response.content if block.type == "text")


class OpenAICompatibleLLM(LLM):
    """Any OpenAI-shaped endpoint, including Qwen via DashScope's compat mode."""

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        from openai import OpenAI  # lazy: only this provider needs the SDK

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""


def get_llm() -> LLM:
    """Build the provider named by LLM_PROVIDER. Defaults to FakeLLM."""
    provider = os.environ.get("LLM_PROVIDER", "fake").strip().lower()

    if provider == "fake":
        return FakeLLM()

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(f"LLM_PROVIDER={provider} requires LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")

    if provider == "anthropic":
        return AnthropicLLM(api_key, model or DEFAULT_ANTHROPIC_MODEL)

    if provider == "openai":
        base_url = os.environ.get("LLM_BASE_URL")
        if not base_url:
            raise RuntimeError("LLM_PROVIDER=openai requires LLM_BASE_URL")
        if not model:
            raise RuntimeError("LLM_PROVIDER=openai requires LLM_MODEL")
        return OpenAICompatibleLLM(api_key, model, base_url)

    raise ValueError(f"unknown LLM_PROVIDER: {provider!r} (fake, anthropic, openai)")
