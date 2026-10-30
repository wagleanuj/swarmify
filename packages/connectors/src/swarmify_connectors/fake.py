"""The fake ConnectorTransport: records every start and call, answers with declared outputs."""

from collections.abc import Callable
from typing import Any

from swarmify_connectors.model import Connector, ConnectorError, Tool

Answer = dict[str, Any] | BaseException | Callable[[dict[str, Any]], dict[str, Any]]


class FakeTransport:
    """`tools` maps a connector name to what it lists; a connector missing there fails to start.
    `outputs` maps a tool name to its answers in order (the last repeats), or `connector.tool`
    for one connector's answers to a tool several offer, which win. An exception answer
    is raised; a BaseException stands in for a crash; a function answers with what it returns
    for the call's arguments. Undeclared tools answer `{"ok": True}`."""

    def __init__(
        self,
        tools: dict[str, list[Tool]] | None = None,
        outputs: dict[str, list[Answer]] | None = None,
    ) -> None:
        self.tools = tools or {}
        self.outputs = outputs or {}
        self.starts: list[tuple[str, dict[str, str]]] = []
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
        self.versions: list[str | None] = []
        """The package version of each call's connector, beside `calls`."""

    def list_tools(self, connector: Connector, env: dict[str, str]) -> list[Tool]:
        self.starts.append((connector.name, env))
        if connector.name not in self.tools:
            raise ConnectorError(f"{connector.name}: could not start")
        return self.tools[connector.name]

    def call(
        self, connector: Connector, tool: str, arguments: dict[str, Any], env: dict[str, str]
    ) -> dict[str, Any]:
        self.calls.append((connector.name, tool, arguments, env))
        self.versions.append(connector.version)
        answers = self.outputs.get(f"{connector.name}.{tool}") or self.outputs.get(tool)
        if not answers:
            return {"ok": True}
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer(arguments) if callable(answer) else answer
