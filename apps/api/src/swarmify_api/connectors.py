"""HTTP face of the Connectors context: the registry, test-and-list-tools, stage calls, and the
Connector Packages installed for the whole install."""

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
    InstallError,
    KeyCheck,
    ManifestError,
    Package,
    Tool,
)
from swarmify_connectors.setup import env_name, fields, scope
from swarmify_workflows import Workflows
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
    world_types: str | None = Field(default=None, max_length=60)
    """A tool answering its types for one world, given `world` and `assets_path`, as
    `{"types": {name: schema}, "note": "..."}`; a type it declares changes only there."""


class MediaFile(BaseModel):
    """A file a connector makes or takes, as its Manifest declares it."""

    description: str
    media_type: str


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
    world_types: str | None
    """The tool that answers its types for one world; None when they never depend on it."""
    label: str
    """What a person reads for it: its Manifest's label, else its name."""
    capabilities: list[str]
    """The core-owned contracts it offers, as `build_session` (Manifest `capabilities`)."""
    outputs: list[MediaFile]
    """What a stage of it makes, which a skin picks a step's panel by (ADR 0025)."""
    missing: list[str]
    """Credential names the credentials file does not hold yet."""
    version: str | None
    """The Connector Package version the world pins; None for a built-in or the world's own."""


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


class PackageSource(BaseModel):
    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    """A folder of this repository, as `connectors/speech`, or an absolute local path."""


class EnabledPackage(BaseModel):
    name: str
    version: str


class PinnedVersion(BaseModel):
    version: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    """An installed version of the package, as `1.0.0`."""


SetupValue = bool | int | float | Annotated[str, StringConstraints(max_length=20000)] | None


class SetupField(BaseModel):
    """One `[[config]]` field of a package's Setup as its Manifest declares it, with what is
    saved for it. A Credential's value is never returned, only whether it is set."""

    key: str
    label: str
    type: Literal["string", "text", "number", "integer", "boolean", "choice", "url", "path"]
    secret: bool
    required: bool
    scope: Literal["shared", "world"]
    """`shared`: one value for every world, which a world may override; `world`: per world."""
    env: str
    help: str | None
    options: list[str] | None
    default: SetupValue
    min: float | None
    max: float | None
    pattern: str | None
    shared: SetupValue
    """The shared value; null for a Credential, and for a field set per world."""
    world: SetupValue
    """This world's own value; null for a Credential."""
    shared_set: bool
    world_set: bool


class SetupView(BaseModel):
    package: str
    version: str
    help_url: str | None
    test: str | None
    """The tool Swarmify runs with the candidate values before saving; null for none."""
    fields: list[SetupField]
    missing: list[str]
    """Keys of required fields with no value for this world."""


class SavedSetup(SetupView):
    check: KeyCheck | None
    """What the package's test said when it passed; null when it declares no test."""


class SetupValues(BaseModel):
    shared: dict[str, SetupValue] = Field(default_factory=dict, max_length=100)
    """Field key to its value for every world."""
    world: dict[str, SetupValue] = Field(default_factory=dict, max_length=100)
    """Field key to this world's own value, which wins over the shared one here."""


class Changes(BaseModel):
    added: list[str]
    removed: list[str]
    changed: list[str] = Field(default_factory=list)


class PackageSwitch(BaseModel):
    """What moving a world's pin to `to` changes in the package's Manifest (issue 223)."""

    from_: str | None = Field(alias="from")
    """The version the world pins now; null when it does not enable the package yet."""
    to: str
    params: Changes
    """Its params, by property."""
    setup: Changes
    """Its Setup fields, by key."""
    required: list[str]
    """The keys of Setup fields newly required with no default."""
    permissions: Changes
    """Its permissions, as `network: api.example.org`."""
    needs: list[str]
    """The labels of newly required Setup fields with no value in this world: the switch waits
    until each has one."""


def router(connectors: Connectors, worlds: Worlds, workflows: Workflows) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["connectors"])
    packages = connectors.packages

    def view(connector: Connector) -> ConnectorView:
        m = connector.manifest
        return ConnectorView(
            **asdict(connector),
            label=m.get("label") or connector.name,
            capabilities=m.get("capabilities", []),
            outputs=m.get("outputs", []),
            missing=connectors.missing(connector),
        )

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
                body.world_types,
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

    @api.get("/packages")
    def list_packages() -> list[Package]:
        """Every Connector Package version installed for the whole install (ADR 0023)."""
        return packages.list()

    @api.post("/packages", status_code=201)
    def install_package(body: PackageSource) -> Package:
        """Copy the package to `<data dir>/packages/<name>/<version>/` and run its install step
        there. 422 naming the field for an invalid Manifest, or saying why it did not install
        (that version is installed already, the step failed); nothing new is left behind."""
        try:
            return packages.install(body.source)
        except ManifestError as exc:
            raise HTTPException(422, f"invalid Manifest: {exc}") from exc
        except InstallError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.delete("/packages/{name}/{version}", status_code=204)
    def uninstall_package(name: str, version: str) -> Response:
        """Remove an installed version. 409 naming every world that pins it and every saved
        workflow version that records it, while any does: those keep running it."""
        names = {w.id: w.name for w in worlds.list_all()}
        users = [f"world {names.get(w, w)} pins it" for w in connectors.pinned_by(name, version)]
        users += [
            f"{wf.name} v{n} in world {names.get(wf.world_id, wf.world_id)} was saved with it"
            for wf, n in workflows.using(name, version)
        ]
        if users:
            raise HTTPException(409, f"{name} {version} is in use: {'; '.join(users)}")
        try:
            packages.uninstall(name, version)
        except InstallError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(status_code=204)

    @api.get("/worlds/{world_id}/packages/{name}/switch")
    def package_switch(world_id: str, name: str, version: str) -> PackageSwitch:
        """What moving this world's pin of the package to `version` changes in its Manifest
        (params, Setup fields, permissions), and the new required fields it waits for."""
        world_or_404(world_id)
        try:
            return PackageSwitch(**connectors.switch(world_id, name, version))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/worlds/{world_id}/packages")
    def enabled_packages(world_id: str) -> list[EnabledPackage]:
        """The Connector Packages this world enables, each at the version it pins (ADR 0023)."""
        world_or_404(world_id)
        return [EnabledPackage(name=n, version=v) for n, v in connectors.enabled(world_id).items()]

    @api.put("/worlds/{world_id}/packages/{name}")
    def enable_package(world_id: str, name: str, body: PinnedVersion) -> EnabledPackage:
        """Enable an installed package in this world at a version, or move its pin there; its
        Connector then exists in this world, called by the package's name. 422 saying why not,
        as a switch to a version with a new required Setup field that has no value yet. Saved
        workflow versions keep the version they recorded."""
        world_or_404(world_id)
        try:
            connectors.enable(world_id, name, body.version)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return EnabledPackage(name=name, version=body.version)

    @api.delete("/worlds/{world_id}/packages/{name}", status_code=204)
    def disable_package(world_id: str, name: str) -> Response:
        """Stop this world using the package; other worlds that enable it are untouched."""
        world_or_404(world_id)
        if not connectors.disable(world_id, name):
            raise HTTPException(404, f"this world does not enable {name}")
        return Response(status_code=204)

    def setup_view(world_id: str, name: str, version: str | None) -> SetupView:
        try:
            connector, shared, world = connectors.setup_values(world_id, name, version)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        m = connector.manifest

        def shown(f: dict[str, Any], values: dict[str, Any]) -> Any:
            return None if f.get("secret") else values.get(f["key"])

        return SetupView(
            package=name,
            version=connector.version or "",
            help_url=m.get("setup", {}).get("help_url"),
            test=m.get("setup", {}).get("test"),
            fields=[
                SetupField.model_validate(
                    {
                        **{k: f.get(k) for k in ("key", "label", "type", "help", "options")},
                        **{k: f.get(k) for k in ("default", "min", "max", "pattern")},
                        "secret": f.get("secret", False),
                        "required": f.get("required", False),
                        "scope": scope(f),
                        "env": env_name(f),
                        "shared": None if scope(f) == "world" else shown(f, shared),
                        "world": shown(f, world),
                        "shared_set": scope(f) == "shared" and shared.get(f["key"]) is not None,
                        "world_set": world.get(f["key"]) is not None,
                    }
                )
                for f in fields(m)
            ],
            missing=connectors.missing(connector),
        )

    @api.get("/worlds/{world_id}/packages/{name}/setup")
    def get_setup(world_id: str, name: str, version: str | None = None) -> SetupView:
        """The Setup form of a package this world enables, drawn from its Manifest's
        `[[config]]` fields with what is saved for each (ADR 0024). Credentials are only set
        or not. `version`, an installed version other than the pinned one, draws that
        version's form, so the fields a pin switch waits for are filled in before it. 404 when
        the world does not enable it."""
        world_or_404(world_id)
        return setup_view(world_id, name, version)

    @api.put("/worlds/{world_id}/packages/{name}/setup")
    def save_setup(
        world_id: str, name: str, body: SetupValues, version: str | None = None
    ) -> SavedSetup:
        """Save the package's shared values and this world's own. An empty value clears a
        Setting; an empty Credential keeps the saved one, null clears it. The package's test
        tool runs with the candidate values first; when it fails nothing is saved and the 422
        carries its message. 422 also names a field that is invalid or required. `version` as
        for the form."""
        world_or_404(world_id)
        setup_view(world_id, name, version)  # 404 before anything else
        try:
            check = connectors.save_setup(world_id, name, body.shared, body.world, version)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if check is not None and not check.ok:
            raise HTTPException(422, f"Not saved. The test said: {check.message}")
        return SavedSetup(**setup_view(world_id, name, version).model_dump(), check=check)

    @api.get("/stages/{stage_id}/call")
    def get_call(stage_id: str) -> Call:
        """The raw call and response of a connector stage."""
        call = connectors.call_for(stage_id)
        if call is None:
            raise HTTPException(404, "no call recorded for this stage")
        return call

    return api
