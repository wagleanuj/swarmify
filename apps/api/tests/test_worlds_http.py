from pathlib import Path

from fastapi.testclient import TestClient


def test_create_list_get_delete_world(client: TestClient, tmp_path: Path) -> None:
    created = client.post("/api/worlds", json={"name": " Video ", "purpose": "Weekly shorts."})
    assert created.status_code == 201
    world = created.json()
    assert world["name"] == "Video" and world["purpose"] == "Weekly shorts."
    assert world["id"].startswith("world_") and world["created_at"]
    assert (tmp_path / "worlds" / world["id"]).is_dir()

    assert client.get("/api/worlds").json() == [world]
    assert client.get(f"/api/worlds/{world['id']}").json() == world

    assert client.delete(f"/api/worlds/{world['id']}").status_code == 204
    assert not (tmp_path / "worlds" / world["id"]).exists()
    assert client.get("/api/worlds").json() == []
    assert client.get(f"/api/worlds/{world['id']}").status_code == 404
    assert client.delete(f"/api/worlds/{world['id']}").status_code == 404


def test_create_world_rejects_blank_or_too_long_names(client: TestClient) -> None:
    assert client.post("/api/worlds", json={"name": "   "}).status_code == 422
    assert client.post("/api/worlds", json={"name": "x" * 121}).status_code == 422
    assert client.post("/api/worlds", json={}).status_code == 422
    assert client.get("/api/worlds").json() == []


def test_purpose_is_optional(client: TestClient) -> None:
    assert client.post("/api/worlds", json={"name": "Video"}).json()["purpose"] == ""


PNG = b"\x89PNG\r\n\x1a\n"


def install_pack(data_dir: Path, pack_id: str, manifest: str, frames: dict[str, list[str]]) -> Path:
    """Copy a pack into the install the way an operator does: a folder in `sprite-packs/`."""
    folder = data_dir / "sprite-packs" / pack_id
    folder.mkdir(parents=True)
    for character, files in frames.items():
        (folder / character).mkdir(parents=True, exist_ok=True)
        for name in files:
            (folder / character / name).write_bytes(PNG)
    (folder / "pack.toml").write_text(manifest)
    return folder


OFFICE = """name = "Office folk"
licence = "Mine, do not share."
frame = [64, 64]
[characters.ana]
name = "Ana"
[characters.ana.poses]
idle = ["idle.png"]
work = ["work0.png", "work1.png"]
[casting.planner]
character = "ana"
name = "Ana"
"""


def test_the_pack_list_has_the_bundled_pack_and_one_copied_into_the_install(
    client: TestClient, tmp_path: Path
) -> None:
    install_pack(tmp_path, "office-folk", OFFICE, {"ana": ["idle.png", "work0.png", "work1.png"]})
    packs = client.get("/api/sprite-packs").json()
    assert [p["id"] for p in packs] == ["kenney-toon", "office-folk"]
    kenney, folk = packs
    assert kenney["name"] == "Toon Characters" and kenney["problems"] == []
    assert kenney["licence"].startswith("Creative Commons Zero")
    assert len(kenney["characters"]) == 6 and kenney["frame"] == [192, 256]
    assert kenney["characters"]["robot"]["poses"]["idle"] == ["idle.png"]
    assert kenney["casting"]["planner"]["character"] == "femaleAdventurer"
    assert folk == {
        "id": "office-folk",
        "name": "Office folk",
        "licence": "Mine, do not share.",
        "author": None,
        "frame": [64, 64],
        "characters": {
            "ana": {
                "name": "Ana",
                "poses": {"idle": ["idle.png"], "work": ["work0.png", "work1.png"]},
            }
        },
        "casting": {
            "planner": {"character": "ana", "name": "Ana", "colour": None, "catchphrase": None}
        },
        "problems": [],
    }


def test_a_broken_pack_is_listed_with_what_is_wrong(client: TestClient, tmp_path: Path) -> None:
    install_pack(tmp_path, "garbled", "name = [", {})
    install_pack(
        tmp_path, "gappy", OFFICE.replace('idle = ["idle.png"]\n', ""), {"ana": ["work0.png"]}
    )
    install_pack(
        tmp_path, "wrong", OFFICE.replace('"ana"\nname', '"bob"\nname'), {"ana": ["idle.png"]}
    )
    problems = {p["id"]: p["problems"] for p in client.get("/api/sprite-packs").json()}
    assert problems["kenney-toon"] == []
    assert len(problems["garbled"]) == 1 and problems["garbled"][0].startswith(
        "unreadable manifest"
    )
    assert problems["gappy"] == ["ana has no idle pose", "ana work: missing frame work1.png"]
    assert problems["wrong"] == [
        "ana work: missing frame work0.png",
        "ana work: missing frame work1.png",
        "casting for planner: no character bob",
    ]


def test_pack_files_are_served_and_never_from_outside_a_pack(
    client: TestClient, tmp_path: Path
) -> None:
    folder = install_pack(tmp_path, "office-folk", OFFICE, {"ana": ["idle.png"]})
    bundled = client.get("/api/sprite-packs/kenney-toon/robot/idle.png")
    assert bundled.status_code == 200 and bundled.headers["content-type"] == "image/png"
    assert client.get("/api/sprite-packs/office-folk/ana/idle.png").content == PNG
    (tmp_path / "secret.txt").write_text("keys")
    (folder / "ana" / "out.png").symlink_to(tmp_path / "secret.txt")
    for path in [
        "office-folk/..%2F..%2Fsecret.txt",
        "office-folk/ana/..%2F..%2F..%2Fsecret.txt",
        "office-folk/%2F" + str(tmp_path / "secret.txt").lstrip("/").replace("/", "%2F"),
        "office-folk/ana/out.png",
        "office-folk/ana/missing.png",
        "office-folk/ana",
        "nobody/ana/idle.png",
        "..%2Fsprite-packs/office-folk/ana/idle.png",
    ]:
        assert client.get(f"/api/sprite-packs/{path}").status_code == 404, path
