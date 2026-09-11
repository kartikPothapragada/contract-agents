"""LLM backends with structured-output enforcement, cost accounting and caching.

Four backends implement one interface:

``anthropic`` / ``openai``   cloud APIs (challenge permits these for Track A)
``ollama``                   local fallback, zero marginal cost
``stub``                     deterministic, no network -- makes the whole graph
                             runnable and unit-testable offline

Two things every agent gets for free by going through this layer:

1. **Structured output or failure.** Agents never parse prose. Each call names a
   Pydantic model; the layer extracts JSON, validates it, and on failure issues
   exactly one repair turn that feeds the validation error back to the model.
   After that it raises. A silently-malformed agent output is the failure mode
   that multi-agent systems die of, so it is made loud here rather than in six
   different agents.
2. **Model tiering.** Agents declare a *tier* (``fast`` / ``deep``), not a model
   name. Intake and extraction run on the cheap tier; policy reasoning and
   verification run on the deep tier. See write-up, cost section.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Type, TypeVar

from pydantic import BaseModel, ValidationError

from clauseguard.schemas.messages import Usage

T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens (prompt, completion). Override via config if prices move.
PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "llama3.1:8b": (0.0, 0.0),
    "qwen2.5:7b": (0.0, 0.0),
    "stub": (0.0, 0.0),
}

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _price(model: str, pt: int, ct: int) -> float:
    pin, pout = PRICING.get(model, (0.0, 0.0))
    return round(pt / 1e6 * pin + ct / 1e6 * pout, 6)


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model response.

    Models fence code, prepend 'Here is the JSON:', and occasionally emit both.
    Tolerating that here is cheaper than a repair round-trip.
    """
    m = _JSON_BLOCK.search(text)
    if m:
        return m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


class LLMError(RuntimeError):
    pass


class StructuredOutputError(LLMError):
    """Model could not produce schema-valid output after one repair turn."""


# --------------------------------------------------------------------------- #
# Backend interface
# --------------------------------------------------------------------------- #
class LLMBackend(ABC):
    name: str = "base"

    def __init__(self, fast_model: str, deep_model: str) -> None:
        self.fast_model = fast_model
        self.deep_model = deep_model

    def model_for(self, tier: str) -> str:
        return self.fast_model if tier == "fast" else self.deep_model

    @abstractmethod
    def _raw(self, system: str, user: str, model: str, temperature: float) -> tuple[str, int, int]:
        """Return (text, prompt_tokens, completion_tokens)."""


# --------------------------------------------------------------------------- #
# Concrete backends
# --------------------------------------------------------------------------- #
class AnthropicBackend(LLMBackend):
    name = "anthropic"

    def __init__(
        self,
        fast_model: str = "claude-haiku-4-5",
        deep_model: str = "claude-sonnet-4-5",
    ) -> None:
        super().__init__(fast_model, deep_model)
        from anthropic import Anthropic  # imported lazily: optional dependency

        self._client = Anthropic()

    def _raw(self, system: str, user: str, model: str, temperature: float):
        resp = self._client.messages.create(
            model=model,
            max_tokens=4096,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return text, resp.usage.input_tokens, resp.usage.output_tokens


class OpenAIBackend(LLMBackend):
    name = "openai"

    def __init__(self, fast_model: str = "gpt-4o-mini", deep_model: str = "gpt-4o") -> None:
        super().__init__(fast_model, deep_model)
        from openai import OpenAI

        self._client = OpenAI()

    def _raw(self, system: str, user: str, model: str, temperature: float):
        resp = self._client.chat.completions.create(
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        u = resp.usage
        return resp.choices[0].message.content or "", u.prompt_tokens, u.completion_tokens


class OllamaBackend(LLMBackend):
    name = "ollama"

    def __init__(
        self,
        fast_model: str = "qwen2.5:7b",
        deep_model: str = "llama3.1:8b",
        host: str = "http://localhost:11434",
    ) -> None:
        super().__init__(fast_model, deep_model)
        self.host = host

    def _raw(self, system: str, user: str, model: str, temperature: float):
        import httpx

        r = httpx.post(
            f"{self.host}/api/chat",
            json={
                "model": model,
                "stream": False,
                "format": "json",
                "options": {"temperature": temperature},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=180.0,
        )
        r.raise_for_status()
        d = r.json()
        return (
            d["message"]["content"],
            d.get("prompt_eval_count", 0),
            d.get("eval_count", 0),
        )


class StubBackend(LLMBackend):
    """Deterministic, network-free backend.

    Not a mock in the testing sense -- it is a real (rule-based) implementation
    of every judgement the agents ask for, so the full graph, the HITL gate, the
    repair loop and the evaluation harness all run end to end with no API key.
    That keeps the submission reproducible for a reviewer, and it makes the
    orchestration logic testable independently of model non-determinism.

    Handlers are registered by ``clauseguard.llm.stub_logic`` to avoid a circular
    import; if none matches, the call raises rather than inventing an answer.
    """

    name = "stub"
    _handlers: dict[str, Any] = {}

    def __init__(self, fast_model: str = "stub", deep_model: str = "stub") -> None:
        super().__init__(fast_model, deep_model)

    @classmethod
    def register(cls, task: str, fn) -> None:
        cls._handlers[task] = fn

    def _raw(self, system: str, user: str, model: str, temperature: float):
        raise LLMError("StubBackend routes by task, not raw text; use call() with task=")


# --------------------------------------------------------------------------- #
# Client facade: caching, repair loop, usage accounting
# --------------------------------------------------------------------------- #
class LLMClient:
    def __init__(self, backend: LLMBackend, cache: bool = True) -> None:
        self.backend = backend
        self._cache: dict[str, tuple[str, int, int]] = {}
        self._cache_enabled = cache
        self.total = Usage()
        self.call_count = 0
        self.cache_hits = 0

    # -- public ------------------------------------------------------------ #
    def call(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: Type[T],
        tier: str = "fast",
        temperature: float = 0.0,
        context: dict[str, Any] | None = None,
    ) -> tuple[T, Usage, float]:
        """Return (validated_object, usage, latency_ms)."""
        started = time.perf_counter()

        if isinstance(self.backend, StubBackend):
            handler = StubBackend._handlers.get(task)
            if handler is None:
                raise LLMError(
                    f"stub backend has no handler for task {task!r}; "
                    "register one in clauseguard.llm.stub_logic"
                )
            obj = handler(context or {})
            validated = schema.model_validate(obj)
            usage = Usage(model="stub")
            self.call_count += 1
            return validated, usage, (time.perf_counter() - started) * 1000

        model = self.backend.model_for(tier)
        system_full = (
            f"{system}\n\nRespond with a single JSON object matching this schema. "
            f"No prose, no markdown fences.\nSchema: "
            f"{json.dumps(schema.model_json_schema())}"
        )

        text, pt, ct = self._raw_cached(system_full, user, model, temperature)
        try:
            validated = schema.model_validate_json(_extract_json(text))
        except ValidationError as first_err:
            repair_user = (
                f"{user}\n\nYour previous reply failed schema validation:\n"
                f"{first_err}\n\nPrevious reply:\n{text}\n\n"
                "Return corrected JSON only."
            )
            text2, pt2, ct2 = self.backend._raw(system_full, repair_user, model, temperature)
            pt, ct = pt + pt2, ct + ct2
            try:
                validated = schema.model_validate_json(_extract_json(text2))
            except ValidationError as second_err:
                self._account(model, pt, ct)
                raise StructuredOutputError(
                    f"task={task} failed schema validation twice: {second_err}"
                ) from second_err

        usage = self._account(model, pt, ct)
        return validated, usage, (time.perf_counter() - started) * 1000

    # -- internals --------------------------------------------------------- #
    def _raw_cached(self, system: str, user: str, model: str, temperature: float):
        if not self._cache_enabled or temperature > 0:
            return self.backend._raw(system, user, model, temperature)
        key = hashlib.sha256(f"{model}|{system}|{user}".encode()).hexdigest()
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
        out = self.backend._raw(system, user, model, temperature)
        self._cache[key] = out
        return out

    def _account(self, model: str, pt: int, ct: int) -> Usage:
        usage = Usage(model=model, prompt_tokens=pt, completion_tokens=ct, usd=_price(model, pt, ct))
        self.total = self.total + usage
        self.call_count += 1
        return usage


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def build_backend(name: str | None = None) -> LLMBackend:
    """Resolve a backend, degrading gracefully.

    Resolution order when ``name`` is None or ``"auto"``:
    anthropic (if key) -> openai (if key) -> ollama (if reachable) -> stub.

    The fallback chain is the point: a reviewer with no keys and no GPU still
    gets a working system, and a production deployment degrades to a local model
    rather than failing the request outright.
    """
    name = (name or os.getenv("CLAUSEGUARD_BACKEND") or "auto").lower()

    def _ollama_up() -> bool:
        try:
            import httpx

            host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            return httpx.get(f"{host}/api/tags", timeout=2.0).status_code == 200
        except Exception:
            return False

    if name == "anthropic":
        return AnthropicBackend()
    if name == "openai":
        return OpenAIBackend()
    if name == "ollama":
        return OllamaBackend()
    if name == "stub":
        return StubBackend()

    if os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicBackend()
    if os.getenv("OPENAI_API_KEY"):
        return OpenAIBackend()
    if _ollama_up():
        return OllamaBackend()
    return StubBackend()
