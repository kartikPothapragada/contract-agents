"""Shared agent scaffolding.

An agent here is a narrow, auditable unit: one role, one output schema, one
place it is allowed to emit from. Agents never call each other directly -- they
read the graph state and emit typed messages onto the bus. That constraint is
what keeps the interaction trace complete; a back-channel call between two
agents would be invisible to the bus and therefore invisible to the reviewer.
"""

from __future__ import annotations

from typing import Any, Type, TypeVar

from pydantic import BaseModel

from clauseguard.llm.backends import LLMClient
from clauseguard.orchestration.trace import TraceBus
from clauseguard.schemas.messages import (
    AgentError,
    AgentMessage,
    AgentRole,
    MessageType,
    Payload,
    ToolCall,
    ToolResult,
)
from clauseguard.tools.playbook import PlaybookStore

T = TypeVar("T", bound=BaseModel)


class Agent:
    role: AgentRole = AgentRole.ORCHESTRATOR
    tier: str = "fast"

    def __init__(self, llm: LLMClient, playbook: PlaybookStore, bus: TraceBus) -> None:
        self.llm = llm
        self.playbook = playbook
        self.bus = bus

    # -- emission helpers -------------------------------------------------- #
    def emit(
        self,
        msg_type: MessageType,
        payload: Payload,
        *,
        recipient: AgentRole = AgentRole.ORCHESTRATOR,
        parent: AgentMessage | str | None = None,
        usage=None,
        latency_ms: float | None = None,
    ) -> AgentMessage:
        return self.bus.emit(
            self.role, recipient, msg_type, payload, parent, usage, latency_ms
        )

    def emit_error(self, error_type: str, message: str, recoverable: bool = True) -> AgentMessage:
        return self.emit(
            MessageType.AGENT_ERROR,
            AgentError(error_type=error_type, message=message, recoverable=recoverable),
        )

    def call_tool(self, name: str, arguments: dict[str, Any], fn) -> tuple[Any, AgentMessage]:
        """Invoke a deterministic tool with both call and result traced.

        Tracing the call *and* the result separately is what makes a hallucinated
        or malformed tool invocation visible. If an agent asks for a clause type
        that is not in the playbook, that shows up in the trace as a failed tool
        result rather than as a silently empty analysis.
        """
        call_msg = self.emit(MessageType.TOOL_CALL, ToolCall(tool_name=name, arguments=arguments))
        try:
            result = fn()
            self.emit(
                MessageType.TOOL_RESULT,
                ToolResult(tool_name=name, ok=True, result=result),
                parent=call_msg,
            )
            return result, call_msg
        except Exception as exc:  # noqa: BLE001 - traced, then re-raised by caller
            self.emit(
                MessageType.TOOL_RESULT,
                ToolResult(tool_name=name, ok=False, error=f"{type(exc).__name__}: {exc}"),
                parent=call_msg,
            )
            raise

    # -- llm helper -------------------------------------------------------- #
    def ask(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: Type[T],
        context: dict[str, Any] | None = None,
        tier: str | None = None,
    ) -> tuple[T, Any, float]:
        return self.llm.call(
            task=task,
            system=system,
            user=user,
            schema=schema,
            tier=tier or self.tier,
            context=context,
        )
