"""HTTP face of the Connectors context: the registry, test-and-list-tools, stage calls, and the
Connector Packages installed for the whole install."""

import hashlib
from dataclasses import asdict
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, StringConstraints

from swarmify_api.auth import COOKIE
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
from swarmify_connectors.oauth import CALLBACK, SignInNeeded
from swarmify_connectors.setup import env_name, fields, needs_sign_in, scope, secret
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
    """Key name to value; each name becomes a Credential of the package's Setup. An empty value
    keeps the one saved already. Values are never returned."""
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
    """The tool its health check calls; None when it has none."""
    services: list[str]
    """The Services a package declares, which the operator runs and its health check polls."""
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
    """Credential names the credentials file does not hold yet; for a package, the keys of
    required Setup fields with no value for this world."""
    sign_in: list[str]
    """Keys of a package's sign-in fields the provider refused, to sign in to again."""
    version: str | None
    """The Connector Package version the world pins; None for a built-in."""


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
    type: Literal[
        "string", "text", "number", "integer", "boolean", "choice", "url", "path", "oauth"
    ]
    """`oauth`: a sign-in Swarmify runs (`POST .../setup/oauth/{key}`), never typed."""
    secret: bool
    required: bool
    scope: Literal["shared", "world"]
    """`shared`: one value for every world, which a world may override; `world`: per world."""
    env: str | None
    """The variable it reaches the process as; null for an OAuth app's client ID and secret,
    which only Swarmify uses."""
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
    needs_sign_in: bool
    """An `oauth` field whose sign-in the provider refused, or the connector reported failing."""


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


class SignInStart(BaseModel):
    url: str
    """The provider's sign-in page, to send the browser to."""
    redirect_uri: str
    """Where the provider sends it back: register this with the provider's app."""


def router(connectors: Connectors, worlds: Worlds, workflows: Workflows) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["connectors"])
    packages = connectors.packages

    def view(connector: Connector) -> ConnectorView:
        m = connector.manifest
        return ConnectorView(
            **asdict(connector),
            label=m.get("label") or connector.name,
            capabilities=m.get("capabilities", []),
            services=[s["name"] for s in m.get("services", [])],
            outputs=m.get("outputs", []),
            missing=connectors.missing(connector),
            sign_in=connectors.sign_in_needed(connector),
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
        """Write a minimal Manifest for the server, install it as a local Connector Package,
        enable it in this world and save its keys as the package's shared Credentials (issue
        211). An empty key takes the value the package or the credentials file holds already."""
        world_or_404(world_id)
        try:
            return view(connectors.add(draft(world_id, body), body.credentials))
        except (ValueError, PermissionError, InstallError) as exc:
            raise HTTPException(422, str(exc)) from exc

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
        """What a built-in or a world's own connector depends on and whether each answers.
        Empty for one without a health check; a package's is under its world."""
        found = connectors.get(connector_id)
        if found is None:
            raise HTTPException(404, "connector not found")
        try:
            return connectors.health(found)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc

    @api.get("/worlds/{world_id}/packages/{name}/health")
    def package_health(world_id: str, name: str) -> list[HealthCheck]:
        """Whether each Service the enabled package declares answers its health path, one check
        per URL its Setting holds, then what its health tool finds (ADR 0026). 404 when the
        world does not enable it, 502 when the tool cannot be called."""
        world_or_404(world_id)
        found = connectors.find(world_id, name)
        if found is None or found.version is None:
            raise HTTPException(404, f"this world does not enable {name}")
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
            return None if secret(f) else values.get(f["key"])

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
                        "secret": secret(f),
                        "required": f.get("required", False),
                        "scope": scope(f),
                        "env": None if "client_of" in f else env_name(f),
                        "shared": None if scope(f) == "world" else shown(f, shared),
                        "world": shown(f, world),
                        "shared_set": scope(f) == "shared" and shared.get(f["key"]) is not None,
                        "world_set": world.get(f["key"]) is not None,
                        "needs_sign_in": needs_sign_in(
                            f, (world if scope(f) == "world" else shared).get(f["key"])
                        ),
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

    def session_digest(request: Request) -> str:
        """What a sign-in's state is bound to: the session cookie, hashed (empty without one, as
        on an install with no password)."""
        return hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()

    @api.post("/worlds/{world_id}/packages/{name}/setup/oauth/{key}")
    def start_sign_in(world_id: str, name: str, key: str, request: Request) -> SignInStart:
        """Begin signing in to the package's `oauth` field `key`: the browser goes to the
        returned URL, and the provider sends it back to `/api/setup/oauth/callback` at the
        origin this request came to (behind a proxy, its X-Forwarded-Proto and -Host), which
        is the redirect URI to register with the provider. 422 until the field's client ID is
        saved."""
        world_or_404(world_id)
        setup_view(world_id, name, None)

        def first(header: str) -> str:  # the client-facing proxy's, when several add one
            return request.headers.get(header, "").split(",")[0].strip()

        host = first("x-forwarded-host") or first("host")
        proto = first("x-forwarded-proto") or request.url.scheme
        redirect_uri = f"{proto}://{host}{CALLBACK}"
        try:
            url = connectors.start_sign_in(
                world_id, name, key, redirect_uri, session_digest(request)
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return SignInStart(url=url, redirect_uri=redirect_uri)

    @api.get(CALLBACK.removeprefix("/api"), response_class=RedirectResponse, status_code=303)
    def finish_sign_in(
        request: Request, state: str = "", code: str = "", error: str = ""
    ) -> RedirectResponse:
        """Where the provider sends the browser back. The state must be one this session began
        in the last ten minutes, once; the code is traded for the token set, stored as the
        field's Credential. Redirects to the world's Connectors page with `sign_in` (the
        package) or `sign_in_error` (why not) in the query. 400 for a stale or foreign state."""
        try:
            pending = connectors.sign_in_for(state, session_digest(request))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        back = f"/worlds/{pending['world_id']}/connectors?"
        if error or not code:
            why = (
                "you said no"
                if error == "access_denied"
                else f"the provider said {error or 'nothing'}"
            )
            return RedirectResponse(
                back + urlencode({"sign_in_error": f"Not signed in: {why}."}), 303
            )
        try:
            connectors.finish_sign_in(pending, code)
        except (ValueError, SignInNeeded, PermissionError) as exc:
            return RedirectResponse(
                back + urlencode({"sign_in_error": f"Not signed in. {exc}"}), 303
            )
        return RedirectResponse(back + urlencode({"sign_in": pending["name"]}), 303)

    @api.get("/stages/{stage_id}/call")
    def get_call(stage_id: str) -> Call:
        """The raw call and response of a connector stage."""
        call = connectors.call_for(stage_id)
        if call is None:
            raise HTTPException(404, "no call recorded for this stage")
        return call

    return api
