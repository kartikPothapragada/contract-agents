"""Run entry point.

Two execution modes, deliberately the same graph:

* **Suspending** (``human_responder=None``) -- the run stops at the gate and
  returns the review packet on ``ReviewRun.interrupt_payload``. Resume later,
  in another process, with :meth:`ReviewRun.resume`. This is the production
  shape.
* **Scripted** (``human_responder=fn``) -- the gate calls a function. Used by
  the demo scenarios and the evaluation sweep.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

from langgraph.types import Command

from clauseguard.llm import stub_logic  # noqa: F401  -- registers stub handlers
from clauseguard.llm.backends import LLMClient, build_backend
from clauseguard.orchestration.graph import build_graph
from clauseguard.orchestration.trace import TraceBus
from clauseguard.tools.playbook import load_playbook


class ReviewRun:
    """Handle for one contract review."""

    def __init__(
        self,
        doc_id: str,
        contract_text: str,
        *,
        backend: str | None = None,
        value_tier: str | None = None,
        human_responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        trace_dir: str | Path = "results/traces",
        echo: bool = False,
        playbook_path: str | Path | None = None,
    ) -> None:
        self.doc_id = doc_id
        self.contract_text = contract_text
        self.trace_id = f"{doc_id}-{uuid.uuid4().hex[:8]}"
        self.backend = build_backend(backend)
        self.llm = LLMClient(self.backend)
        self.playbook = (
            load_playbook(playbook_path) if playbook_path else load_playbook()
        )
        self.bus = TraceBus(
            self.trace_id,
            sink=Path(trace_dir) / f"{self.trace_id}.jsonl",
            echo=echo,
            metadata={
                "backend": self.backend.name,
                "playbook": f"v{self.playbook.version}",
                "doc_id": doc_id,
            },
        )
        self.graph = build_graph(self.llm, self.playbook, self.bus, human_responder)
        self.config = {"configurable": {"thread_id": self.trace_id}}
        self.value_tier = value_tier
        self.interrupt_payload: dict[str, Any] | None = None

    def start(self) -> dict[str, Any]:
        state = {
            "trace_id": self.trace_id,
            "doc_id": self.doc_id,
            "contract_text": self.contract_text,
            "repair_count": 0,
            "status": "received",
        }
        if self.value_tier:
            state["value_tier"] = self.value_tier
        return self._run(state)

    def resume(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Resume a suspended run with the reviewer's decision."""
        return self._run(Command(resume=decision))

    # -- internals --------------------------------------------------------- #
    def _run(self, payload) -> dict[str, Any]:
        result = self.graph.invoke(payload, config=self.config)
        # LangGraph surfaces a suspension as __interrupt__ on the returned state.
        interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
        if interrupts:
            self.interrupt_payload = getattr(interrupts[0], "value", interrupts[0])
        else:
            self.interrupt_payload = None
        return result

    @property
    def suspended(self) -> bool:
        return self.interrupt_payload is not None

    def state(self) -> dict[str, Any]:
        return self.graph.get_state(self.config).values

    def report(self) -> dict[str, Any]:
        st = self.state()
        memo = st.get("memo")
        return {
            "doc_id": self.doc_id,
            "trace_id": self.trace_id,
            "backend": self.backend.name,
            "status": st.get("status"),
            "escalated": st.get("escalation") is not None,
            "repair_passes": st.get("repair_count", 0),
            "unverified": st.get("unverified", []),
            "aggregate_risk": memo.aggregate_risk if memo else None,
            "memo": memo.model_dump() if memo else None,
            "cost": self.bus.cost_report(),
        }


def review_contract(
    doc_id: str,
    contract_text: str,
    *,
    human_responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    **kwargs,
) -> ReviewRun:
    """Convenience wrapper: build, start, return the handle."""
    run = ReviewRun(doc_id, contract_text, human_responder=human_responder, **kwargs)
    run.start()
    return run
