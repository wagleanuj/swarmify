"""HTTP face of the Connectors context: the registry, test-and-list-tools, and stage calls."""

from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, StringConstraints

from swarmify_connectors import (
    Call,
    Connector,
    ConnectorError,
    Connectors,
    HealthCheck,
    KeyCheck,
    Tool,
)
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
    types: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=20)
    """Input types it declares, a JSON Schema by name (lower_snake_case), as `script`."""
    input_types: list[str | None] = Field(default_factory=list, max_length=20)
    """The declared type of a stage's `input`, or of each of its `inputs` in order; null for
    an untyped one. A swarm stage that produces a typed input must declare that output_type."""


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
    health: str | None
    """The tool `GET /api/connectors/<id>/health` calls; None when it has no health check."""
    types: dict[str, dict[str, Any]]
    """Input types it declares, a JSON Schema by name."""
    input_types: list[str | None]
    """The declared type of a stage's `input`, or of each of its `inputs` in order."""
    missing: list[str]
    """Credential names the credentials file does not hold yet."""


class SavedKeys(ConnectorView):
    check: KeyCheck | None
    """The live check of a built-in's keys once all are saved (the account they sign in to, or
    why they failed; the keys are saved either way). None for a connector without a check."""


class Tested(BaseModel):
    tools: list[Tool]


class CredentialValues(BaseModel):
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
                body.types,
                body.input_types,
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
    def set_credentials(connector_id: str, body: CredentialValues) -> SavedKeys:
        """Store a connector's keys in the credentials file, shared by every world; how
        built-ins get theirs. A built-in publish connector's keys are then checked live."""
        found = connectors.get(connector_id)
        if found is None:
            raise HTTPException(404, "connector not found")
        try:
            check = connectors.set_credentials(found, body.credentials)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return SavedKeys(**view(found).model_dump(), check=check)

    @api.get("/connectors/{connector_id}/health")
    def connector_health(connector_id: str) -> list[HealthCheck]:
        """What the connector depends on and whether each answers, as a speech connector's
        engines. Empty for a connector without a health check."""
        found = connectors.get(connector_id)
        if found is None:
            raise HTTPException(404, "connector not found")
        try:
            return connectors.health(found)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc

    @api.get("/stages/{stage_id}/call")
    def get_call(stage_id: str) -> Call:
        """The raw call and response of a connector stage."""
        call = connectors.call_for(stage_id)
        if call is None:
            raise HTTPException(404, "no call recorded for this stage")
        return call

    return api
