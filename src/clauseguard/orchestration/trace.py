"""Trace bus -- agent observability.

Every inter-agent message goes through :meth:`TraceBus.emit`. Nothing else is
the channel. That single chokepoint is what makes the question "what did the
system actually do?" answerable after the fact, which the challenge calls out as
the thing strong agentic submissions demonstrate.

What the bus gives you that per-agent logging does not:

* **A causal DAG, not a flat log.** ``parent_msg_id`` means you can ask "what
  caused this escalation?" and walk backwards, rather than reading timestamps
  and guessing.
* **Cost attributed per agent.** Multi-agent systems get expensive in a way that
  is invisible until it is billed. Aggregating usage at the transport means the
  cost report is always complete, even for an agent someone added last week and
  forgot to instrument.
* **Replayable JSONL.** A run can be re-rendered, diffed against another run, or
  shipped to a reviewer without re-executing any LLM call.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clauseguard.schemas.messages import (
    AgentMessage,
    AgentRole,
    MessageType,
    Payload,
    Usage,
)


class TraceBus:
    def __init__(
        self,
        trace_id: str,
        sink: str | Path | None = None,
        echo: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.trace_id = trace_id
        # Carries the backend and playbook version into the rendered trace. A
        # cost table reading $0.00000 with no context looks like broken
        # accounting rather than a deterministic backend that spends no tokens.
        self.metadata = metadata or {}
        self.messages: list[AgentMessage] = []
        self.echo = echo
        self.sink = Path(sink) if sink else None
        if self.sink:
            self.sink.parent.mkdir(parents=True, exist_ok=True)
            self.sink.write_text("", encoding="utf-8")
        self.started_at = datetime.now(timezone.utc)

    # -- emit -------------------------------------------------------------- #
    def emit(
        self,
        sender: AgentRole,
        recipient: AgentRole,
        msg_type: MessageType,
        payload: Payload,
        parent: AgentMessage | str | None = None,
        usage: Usage | None = None,
        latency_ms: float | None = None,
    ) -> AgentMessage:
        parent_id = parent.msg_id if isinstance(parent, AgentMessage) else parent
        msg = AgentMessage(
            trace_id=self.trace_id,
            parent_msg_id=parent_id,
            sender=sender,
            recipient=recipient,
            msg_type=msg_type,
            payload=payload,
            usage=usage,
            latency_ms=latency_ms,
        )
        self.messages.append(msg)
        if self.sink:
            with self.sink.open("a", encoding="utf-8") as fh:
                fh.write(msg.model_dump_json() + "\n")
        if self.echo:
            print(f"  [trace] {msg.summary()}")
        return msg

    # -- query ------------------------------------------------------------- #
    def of_type(self, msg_type: MessageType) -> list[AgentMessage]:
        return [m for m in self.messages if m.msg_type is msg_type]

    def lineage(self, msg_id: str) -> list[AgentMessage]:
        """Walk parent links back to the root. Answers 'what caused this?'."""
        by_id = {m.msg_id: m for m in self.messages}
        chain: list[AgentMessage] = []
        cur = by_id.get(msg_id)
        seen: set[str] = set()
        while cur and cur.msg_id not in seen:
            seen.add(cur.msg_id)
            chain.append(cur)
            cur = by_id.get(cur.parent_msg_id) if cur.parent_msg_id else None
        return list(reversed(chain))

    # -- aggregate --------------------------------------------------------- #
    def cost_report(self) -> dict[str, Any]:
        by_agent: dict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0}
        )
        total = Usage()
        for m in self.messages:
            if not m.usage:
                continue
            row = by_agent[m.sender.value]
            row["calls"] += 1
            row["prompt_tokens"] += m.usage.prompt_tokens
            row["completion_tokens"] += m.usage.completion_tokens
            row["usd"] = round(row["usd"] + m.usage.usd, 6)
            total = total + m.usage
        latencies = [m.latency_ms for m in self.messages if m.latency_ms]
        return {
            "trace_id": self.trace_id,
            "messages": len(self.messages),
            "llm_calls": sum(1 for m in self.messages if m.usage),
            "total_prompt_tokens": total.prompt_tokens,
            "total_completion_tokens": total.completion_tokens,
            "total_usd": round(total.usd, 6),
            "wall_ms": round(sum(latencies), 1),
            "by_agent": dict(by_agent),
        }

    # -- render ------------------------------------------------------------ #
    def render_markdown(self, title: str = "Agent interaction trace") -> str:
        meta = "  ·  ".join(f"{k} = `{v}`" for k, v in self.metadata.items())
        lines = [f"# {title}", "", f"`trace_id = {self.trace_id}`", ""]
        if meta:
            lines += [meta, ""]
        for i, m in enumerate(self.messages, 1):
            body = m.payload.model_dump(exclude={"kind"})
            cost = ""
            if m.usage and m.usage.usd:
                cost = f" · ${m.usage.usd:.5f} ({m.usage.prompt_tokens}p/{m.usage.completion_tokens}c)"
            lat = f" · {m.latency_ms:.0f}ms" if m.latency_ms else ""
            lines += [
                f"### {i}. `{m.sender.value}` → `{m.recipient.value}` — "
                f"**{m.msg_type.value}**{lat}{cost}",
                "",
                f"<sub>`{m.msg_id}`"
                + (f" ← `{m.parent_msg_id}`" if m.parent_msg_id else "")
                + "</sub>",
                "",
                "```json",
                json.dumps(body, indent=2, default=str)[:2400],
                "```",
                "",
            ]
        rep = self.cost_report()
        lines += [
            "---",
            "",
            "## Run cost",
            "",
            "| agent | llm calls | prompt tok | completion tok | usd |",
            "|---|---:|---:|---:|---:|",
        ]
        for agent, row in sorted(rep["by_agent"].items()):
            lines.append(
                f"| {agent} | {int(row['calls'])} | {int(row['prompt_tokens'])} | "
                f"{int(row['completion_tokens'])} | ${row['usd']:.5f} |"
            )
        lines += [
            f"| **total** | **{rep['llm_calls']}** | "
            f"**{rep['total_prompt_tokens']}** | "
            f"**{rep['total_completion_tokens']}** | **${rep['total_usd']:.5f}** |",
            "",
            f"Messages exchanged: **{rep['messages']}** · "
            f"LLM wall time: **{rep['wall_ms']:.0f} ms**",
            "",
        ]
        if self.metadata.get("backend") == "stub":
            lines += [
                "> Zero tokens and zero cost because this run used the "
                "deterministic rule-engine backend, which spends no tokens. "
                "Re-run with `--backend anthropic` for real usage figures.",
                "",
            ]
        return "\n".join(lines)
