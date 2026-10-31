"""Reading the curated index (issue 226): a local file, https through a fake transport (no
network), the size cap, the timeout, what is refused, and that the index prepared for the
swarmify-connectors repository lists every first-party package as its review would."""

from pathlib import Path

import httpx
import pytest

from swarmify_connectors import index
from swarmify_connectors.index import IndexUnavailable, read_index
from swarmify_connectors.packages import permissions, read_manifest

REPO = Path(__file__).parents[3]
ENTRY = """
[[packages]]
name = "echo"
description = "Says it back."
git = "https://example.org/echo.git"
ref = "v1.0.0"
permissions = ["network: api.example.org"]
future_field = "ignored"
"""


def test_a_local_index_lists_its_entries(tmp_path: Path) -> None:
    file = tmp_path / "index.toml"
    file.write_text(ENTRY)
    assert read_index(str(file)) == [
        {
            "name": "echo",
            "description": "Says it back.",
            "git": "https://example.org/echo.git",
            "subfolder": None,
            "ref": "v1.0.0",
            "permissions": ["network: api.example.org"],
        }
    ]


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("packages = [", "it is not TOML"),
        ("", "index: 'packages' is a required property"),
        (ENTRY.replace('ref = "v1.0.0"', 'ref = "--upload-pack=x"'), "packages[0].ref: "),
        (ENTRY.replace('name = "echo"', ""), "packages[0]: 'name' is a required property"),
    ],
)
def test_a_bad_index_names_why(tmp_path: Path, text: str, said: str) -> None:
    file = tmp_path / "index.toml"
    file.write_text(text)
    with pytest.raises(IndexUnavailable, match=said.replace("[", r"\[").replace("]", r"\]")):
        read_index(str(file))


def test_only_https_and_files_are_read(tmp_path: Path) -> None:
    for url in ("http://example.org/index.toml", "file:///etc/hosts", "ftp://example.org/i"):
        with pytest.raises(IndexUnavailable, match="is refused: the index is fetched over https"):
            read_index(url)
    with pytest.raises(IndexUnavailable, match="there is no file at"):
        read_index(str(tmp_path / "missing.toml"))
    big = tmp_path / "big.toml"
    big.write_text("#" * (index.MAX_BYTES + 1))
    with pytest.raises(IndexUnavailable, match="it is over 1000 kB"):
        read_index(str(big))


def fake(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


def test_https_reads_it_within_the_cap_and_the_timeout() -> None:
    url = "https://example.org/index.toml"
    ok = fake(lambda r: httpx.Response(200, text=ENTRY))
    assert read_index(url, ok)[0]["name"] == "echo"
    with pytest.raises(IndexUnavailable, match="it answered 404"):
        read_index(url, fake(lambda r: httpx.Response(404)))
    moved = fake(lambda r: httpx.Response(302, headers={"location": "http://example.org/i"}))
    with pytest.raises(IndexUnavailable, match="it answered 302"):  # no redirects off https
        read_index(url, moved)
    with pytest.raises(IndexUnavailable, match="it is over 1000 kB"):
        read_index(url, fake(lambda r: httpx.Response(200, content=b"#" * 2_000_000)))

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(IndexUnavailable, match="it took over 10 s"):
        read_index(url, fake(slow))

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(IndexUnavailable, match="it could not be reached"):
        read_index(url, fake(down))


def test_the_prepared_index_lists_every_first_party_package_as_reviewed() -> None:
    entries = read_index(str(REPO / "docs/index-repo/index.toml"))
    first_party = sorted(p.name for p in (REPO / "connectors").iterdir() if p.is_dir())
    assert [e["name"] for e in entries] == first_party
    for e in entries:
        manifest = read_manifest(REPO / e["subfolder"])
        assert e["subfolder"] == f"connectors/{e['name']}" and manifest["name"] == e["name"]
        assert e["git"] == "https://github.com/wagleanuj/swarmify.git"
        assert e["permissions"] == permissions(manifest), e["name"]
