"""HTTP face of a world's Voices: the speech connector's catalog in `assets/voices/`, listed with
each clip's address and the saved workflows whose speech step names the voice, and added to,
edited and deleted through `swarmify_connectors.voices`, which writes voices.json as
`voices:seed` does."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from swarmify_connectors import voices as catalog
from swarmify_connectors.speech import VOICES
from swarmify_memory import ASSETS, Memory
from swarmify_workflows import ConnectorStage, Workflows
from swarmify_worlds import Worlds


@dataclass(frozen=True)
class UsedBy:
    id: str
    name: str


@dataclass(frozen=True)
class Voice:
    name: str
    description: str
    tags: list[str]
    engine: str
    consent: str
    preset: str | None
    clip: str | None
    """Relative to the world folder, as `/files/` serves it."""
    seconds: float | None
    used_by: list[UsedBy]
    """Saved workflows whose current version's speech step has this `voice`."""


@dataclass(frozen=True)
class Catalog:
    voices: list[Voice]
    warnings: list[str]
    """Entries of voices.json the speech connector ignores, and why."""


class EditBody(BaseModel):
    description: str
    tags: list[str] = []


def router(memory: Memory, worlds: Worlds, workflows: Workflows) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["voices"])

    def folder_of(world_id: str) -> Path:
        if worlds.get(world_id) is None:
            raise HTTPException(404, "world not found")
        return memory.world_folder(world_id) / ASSETS / VOICES

    def uses(world_id: str) -> dict[str, list[UsedBy]]:
        found: dict[str, list[UsedBy]] = {}
        for w in workflows.list_all(world_id):
            version = workflows.version(w.id, w.current_version)
            for stage in version.model.stages if version else []:
                if not (isinstance(stage, ConnectorStage) and stage.calls == "speech"):
                    continue
                if isinstance(name := stage.params.get("voice"), str):
                    found.setdefault(name, []).append(UsedBy(w.id, w.name))
        return found

    def voice(v: dict[str, Any], used: dict[str, list[UsedBy]]) -> Voice:
        clip = f"{ASSETS}/{VOICES}/{v['clip']}" if v.get("clip") else None
        return Voice(
            v["name"], v["description"], v.get("tags") or [], v["engine"], v["consent"],
            v.get("preset"), clip, v.get("seconds"), list(dict.fromkeys(used.get(v["name"], []))),
        )  # fmt: skip

    def one(world_id: str, name: str) -> Voice:
        found, _ = catalog.listed(memory.world_folder(world_id) / ASSETS)
        v = next((v for v in found if v["name"] == name), None)
        if v is None:  # saved, but the connector ignores it: say so rather than pretend
            raise HTTPException(422, f"{name} was saved, but the speech connector ignores it")
        return voice(v, uses(world_id))

    @api.get("/worlds/{world_id}/voices")
    def list_voices(world_id: str) -> Catalog:
        """The world's voices as the speech connector reads them, in catalog order."""
        found, warnings = catalog.listed(folder_of(world_id).parent)
        used = uses(world_id)
        return Catalog([voice(v, used) for v in found], warnings)

    @api.post("/worlds/{world_id}/voices", status_code=201, responses={422: {}})
    async def add_voice(
        world_id: str,
        name: Annotated[str, Form()],
        description: Annotated[str, Form()],
        consent: Annotated[str, Form()],
        clip: Annotated[UploadFile, File(description="5 to 10 s of speech, a 16-bit WAV")],
        tags: Annotated[str, Form(description="comma separated")] = "",
    ) -> Voice:
        """Add a voice: its clip saved as `<name>.wav` and its entry appended to voices.json."""
        folder = folder_of(world_id)
        try:
            catalog.add(folder, name, description, tags.split(","), consent, await clip.read())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return one(world_id, name)

    @api.patch("/worlds/{world_id}/voices/{name}", responses={404: {}, 422: {}})
    def edit_voice(world_id: str, name: str, body: EditBody) -> Voice:
        """Change a voice's description and tags."""
        try:
            catalog.edit(folder_of(world_id), name, body.description, body.tags)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return one(world_id, name)

    @api.delete("/worlds/{world_id}/voices/{name}", status_code=204, responses={404: {}})
    def delete_voice(world_id: str, name: str) -> Response:
        """Delete a voice's entry and its clip. A workflow still naming it hands the name to the
        engines as their own voice, so the page names those workflows before it asks."""
        try:
            catalog.remove(folder_of(world_id), name)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return Response(status_code=204)

    return api
