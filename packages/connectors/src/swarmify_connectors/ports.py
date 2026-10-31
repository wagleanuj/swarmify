"""Ports of the Connectors context (ADR 0012): ConnectorTransport and Storage.

MCP over stdio or Streamable HTTP is the transport adapter, a recording fake the other; SQLite is
the storage adapter, Postgres the seam.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol

from swarmify_connectors.model import Call, Connector, Tool


class ConnectorTransport(Protocol):
    """Talks to one connector's server. `env` holds its credentials and nothing else; a command
    gets them as its process environment, a URL as request headers."""

    def list_tools(self, connector: Connector, env: dict[str, str]) -> list[Tool]:
        """Start the server once and return its tools. Raises ConnectorError."""
        ...

    def call(
        self, connector: Connector, tool: str, arguments: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        """Call one tool and return its result as JSON. Raises ConnectorError."""
        ...

    def session(
        self, connector: Connector, env: dict[str, str]
    ) -> AbstractContextManager[Callable[[str, dict[str, Any]], dict[str, Any]]]:
        """Start the server once for several calls (a stage's call and its polls), each made
        with a tool name and arguments as `call` would. Raises ConnectorError."""
        ...


class Storage(Protocol):
    def world_connectors(self) -> list[Connector]:
        """Every world's connector rows from before Connector Packages, oldest first; only the
        one-time move to local packages (issue 211) reads them."""
        ...

    def delete_connector(self, connector_id: str) -> bool: ...

    def get_call(self, idempotency_key: str) -> Call | None: ...

    def save_call(self, call: Call) -> Call:
        """Upsert the call and append its `connector_call.updated` event in one transaction."""
        ...

    def enabled_packages(self, world_id: str) -> dict[str, str]:
        """Package name to the version the world pins, for each package it enables."""
        ...

    def enable_package(self, world_id: str, package: str, version: str, at: str) -> None:
        """Enable the package in the world at `version`, or move its pin there."""
        ...

    def disable_package(self, world_id: str, package: str) -> bool: ...

    def package_pinned_by(self, package: str, version: str) -> list[str]:
        """The ids of the worlds that pin `package` at `version`."""
        ...

    def offered_defaults(self, world_id: str) -> set[str]:
        """The default packages the world has been given once, whatever it did with them."""
        ...

    def offer_default(self, world_id: str, package: str, at: str) -> None: ...

    def delete_world(self, world_id: str) -> None: ...
