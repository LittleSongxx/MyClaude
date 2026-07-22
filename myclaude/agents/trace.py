from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class TraceNode:
    agent_id: str
    parent_id: str | None
    trace_id: str
    agent_type: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    status: str = "running"


class TraceManager:
    def __init__(self, work_dir: str | Path | None = None) -> None:
        self._nodes: dict[str, TraceNode] = {}
        self._state_path: Path | None = None
        if work_dir is not None:
            self.configure_storage(work_dir)

    def configure_storage(self, work_dir: str | Path) -> None:
        storage = Path(work_dir).expanduser().resolve() / ".myclaude" / "agents"
        storage.mkdir(parents=True, exist_ok=True)
        storage.chmod(0o700)
        self._state_path = storage / "traces.json"
        self._load()

    def create(
        self,
        agent_type: str,
        parent_id: str | None = None,
        trace_id: str | None = None,
    ) -> TraceNode:
        agent_id = uuid.uuid4().hex[:12]
        if trace_id is None:
            trace_id = uuid.uuid4().hex[:12]
        node = TraceNode(
            agent_id=agent_id,
            parent_id=parent_id,
            trace_id=trace_id,
            agent_type=agent_type,
        )
        self._nodes[agent_id] = node
        self._persist()
        return node

    def update(self, agent_id: str, **kwargs: int | str) -> None:
        node = self._nodes.get(agent_id)
        if node is None:
            return
        for key, value in kwargs.items():
            if hasattr(node, key):
                setattr(node, key, value)
        self._persist()

    def complete(self, agent_id: str, status: str = "completed") -> None:
        node = self._nodes.get(agent_id)
        if node is None:
            return
        node.end_time = time.time()
        node.status = status
        self._persist()

    def get(self, agent_id: str) -> TraceNode | None:
        return self._nodes.get(agent_id)

    def get_tree(self, trace_id: str) -> list[TraceNode]:
        return [node for node in self._nodes.values() if node.trace_id == trace_id]

    def remove(self, agent_id: str) -> None:
        self._nodes.pop(agent_id, None)
        self._persist()

    def complete_all_running(self, parent_id: str) -> None:
        changed = False
        for node in self._nodes.values():
            if node.parent_id == parent_id and node.status == "running":
                node.status = "completed"
                node.end_time = time.time()
                changed = True
        if changed:
            self._persist()

    def get_total_tokens(self, trace_id: str) -> tuple[int, int]:
        nodes = [node for node in self._nodes.values() if node.trace_id == trace_id]
        return (
            sum(node.input_tokens for node in nodes),
            sum(node.output_tokens for node in nodes),
        )

    def _persist(self) -> None:
        if self._state_path is None:
            return
        temp = self._state_path.with_suffix(".tmp")
        try:
            payload = {
                "version": 1,
                "nodes": [asdict(node) for node in self._nodes.values()],
            }
            temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp.chmod(0o600)
            os.replace(temp, self._state_path)
        except OSError as e:
            log.warning("Could not persist agent traces: %s", e)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not load agent traces: %s", e)
            return
        changed = False
        for row in payload.get("nodes", []):
            try:
                node = TraceNode(**row)
            except (TypeError, ValueError):
                continue
            if node.status == "running":
                node.status = "detached"
                changed = True
            self._nodes[node.agent_id] = node
        if changed:
            self._persist()
