"""HTTP face of the Connectors context: the registry, test-and-list-tools, and stage calls."""

from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, StringConstraints

from swarmify_connectors import Call, Connector, ConnectorError, Connectors, Tool
from swarmify_worlds import Worlds


class NewConnector(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    kind: Literal["tool", "stage", "publish"]
    server: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    """An MCP command line, or an http(s) URL."""
    credentials: dict[str, Annotated[str, StringConstraints(max_length=4000)]] = Field(
        default_factory=dict, max_length=20
    )
    """Credential name to value. An empty value means the file already holds it. Values go to
    the credentials file only and are never returned."""
    stage_types: list[Literal["swarm", "connector"]] = Field(min_length=1)


class ConnectorView(BaseModel):
    id: str
    world_id: str | None
    name: str
    kind: Literal["tool", "stage", "publish"]
    command: list[str]
    url: str | None
    credentials: list[str]
    stage_types: list[str]
    builtin: bool
    about: str
    created_at: str | None
    missing: list[str]
    """Credential names the credentials file does not hold yet."""


class Tested(BaseModel):
    tools: list[Tool]


class Keys(BaseModel):
    credentials: dict[str, Annotated[str, StringConstraints(max_length=4000)]] = Field(
        min_length=1, max_length=20
    )
    """Credential name to value, for the connector's own names only. Never returned."""


def router(connectors: Connectors, worlds: Worlds) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["connectors"])

    def view(connector: Connector) -> ConnectorView:
        return ConnectorView(**asdict(connector), missing=connectors.missing(connector))

    def world_or_404(world_id: str) -> None:
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")

    def draft(world_id: str, body: NewConnector) -> Connector:
        try:
            return connectors.draft(
                world_id,
                body.name,
                body.kind,
                body.server,
                list(body.credentials),
                list(body.stage_types),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/worlds/{world_id}/connectors")
    def list_connectors(world_id: str) -> list[ConnectorView]:
        world_or_404(world_id)
        return [view(c) for c in connectors.list_all(world_id)]

    @api.post("/worlds/{world_id}/connectors/test")
    def test_connector(world_id: str, body: NewConnector) -> Tested:
        """Start the server once with its credentials and list its tools. Saves nothing."""
        world_or_404(world_id)
        try:
            return Tested(tools=connectors.test(draft(world_id, body), body.credentials))
        except (ConnectorError, PermissionError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/worlds/{world_id}/connectors", status_code=201)
    def add_connector(world_id: str, body: NewConnector) -> ConnectorView:
        world_or_404(world_id)
        try:
            return view(connectors.add(draft(world_id, body), body.credentials))
        except (ValueError, PermissionError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.delete("/connectors/{connector_id}", status_code=204)
    def delete_connector(connector_id: str) -> Response:
        found = connectors.get(connector_id)
        if found is None:
            raise HTTPException(404, "connector not found")
        if found.builtin:
            raise HTTPException(409, "built-in connectors cannot be removed")
        connectors.delete(connector_id)
        return Response(status_code=204)

    @api.put("/connectors/{connector_id}/credentials")
    def set_credentials(connector_id: str, body: Keys) -> ConnectorView:
        """Store a connector's keys in the credentials file; how built-ins get theirs."""
        found = connectors.get(connector_id)
        if found is None:
            raise HTTPException(404, "connector not found")
        try:
            connectors.set_credentials(found, body.credentials)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return view(found)

    @api.get("/stages/{stage_id}/call")
    def get_call(stage_id: str) -> Call:
        """The raw call and response of a connector stage."""
        call = connectors.call_for(stage_id)
        if call is None:
            raise HTTPException(404, "no call recorded for this stage")
        return call

    return api
