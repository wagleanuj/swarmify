"""Ports of the Learning context (ADR 0012): Storage (SQLite now) and the agent runtime, as
the one question the Retro asks it: a short read-only session answering structured JSON."""

from collections.abc import Callable
from typing import Any, Protocol

from swarmify_kernel import Event
from swarmify_learning.model import Proposal, SkillVersion


class Storage(Protocol):
    def add_version(self, version: SkillVersion, event: Event) -> SkillVersion:
        """Insert the version and record `event` in one transaction."""
        ...

    def versions(self, world_id: str, name: str | None = None) -> list[SkillVersion]:
        """Oldest first; every skill of the world, or one."""
        ...

    def add_proposal(self, proposal: Proposal, event: Event) -> Proposal: ...

    def get_proposal(self, proposal_id: str) -> Proposal | None: ...

    def pending(self, run_id: str) -> Proposal | None:
        """The run's proposal awaiting its gate."""
        ...

    def list_proposals(self, world_id: str) -> list[Proposal]:
        """Oldest first."""
        ...

    def decide_proposal(
        self, proposal_id: str, status: str, note: str, decided_at: str, event: Event
    ) -> Proposal | None: ...

    def delete_world(self, world_id: str) -> None: ...


class Ask(Protocol):
    """The agent runtime as the Retro uses it: one session with `system` and `prompt` that
    answers JSON following `schema`; `parse` turns the answer into the value returned, and a
    ValueError from it means the answer was unusable (the runtime may try once more)."""

    def __call__(
        self, system: str, prompt: str, schema: dict[str, Any], parse: Callable[[Any], Any]
    ) -> Any: ...
