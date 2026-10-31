"""Installing a Connector Package from git at a ref (issue 225), from a local bare repository with
two tagged versions of the echo fixture: what review shows, that nothing runs before approval,
and that an upgrade adding a permission needs it approved again. No network."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from swarmify_connectors import InstallError, ManifestError, Packages
from swarmify_connectors.packages import MANIFEST, RECORD, permissions, read_manifest

FIXTURES = Path(__file__).parent / "fixtures"
ECHO = [
    "network: api.example.org",
    "install_network: pypi.org",
    "install_network: files.pythonhosted.org",
    "folders: echo-cache",
    "folders: worlds/{world}/assets",
    "services: echo_service",
    "owned_folders: echoes",
    "credentials: ECHO_KEY",
    "credentials: ECHO_TOKEN",
    "sign_in: auth.example.org",
    "capabilities: check",
]


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    who = ["-c", "user.name=t", "-c", "user.email=t@example.org", "-c", "init.defaultBranch=main"]
    done = subprocess.run(["git", *who, *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def commit(work: Path, tag: str | None = None) -> str:
    git(work, "add", "-A")
    git(work, "commit", "-qm", tag or "change")
    if tag:
        git(work, "tag", tag)
    git(work, "push", "-q", "--tags", "origin", "main")
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    """A bare repository holding echo 1.0.0 at `v1.0.0` and 2.0.0, which reaches one more host,
    at `v2.0.0`, and its work tree."""
    bare, work = tmp_path / "echo.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    shutil.copytree(FIXTURES / "packages/echo", work)
    git(work, "init", "-q")
    git(work, "remote", "add", "origin", str(bare))
    commit(work, "v1.0.0")
    toml = work / MANIFEST
    toml.write_text(
        toml.read_text()
        .replace('version = "1.0.0"', 'version = "2.0.0"')
        .replace('network = ["api.example.org"]', 'network = ["api.example.org", "eu.example.org"]')
    )
    commit(work, "v2.0.0")
    return bare, work


@pytest.fixture
def packages(tmp_path: Path) -> Packages:
    return Packages(tmp_path / "data/packages", FIXTURES)


def test_permissions_lists_every_kind_the_manifest_declares() -> None:
    assert permissions(read_manifest(FIXTURES / "packages/echo")) == ECHO
    assert permissions({}) == []


def test_review_reads_the_manifest_at_a_tag_and_runs_nothing(
    packages: Packages, repo: tuple[Path, Path]
) -> None:
    bare, work = repo
    tagged = git(work, "rev-parse", "v1.0.0^{commit}")
    review = packages.review(str(bare), "v1.0.0")
    assert review == {
        "name": "echo",
        "version": "1.0.0",
        "label": "Echo",
        "about": "Says back what it is given, as audio. A fixture.",
        "commit": tagged,
        "permissions": ECHO,
        "upgrade": None,
    }
    assert packages.list() == [] and not packages.root.exists()


def test_nothing_runs_before_every_permission_is_approved(
    packages: Packages, repo: tuple[Path, Path]
) -> None:
    bare, _ = repo
    with pytest.raises(InstallError, match="approve these permissions first: sign_in: auth"):
        packages.install(str(bare), ref="v1.0.0", approved=ECHO[:-2])
    assert packages.list() == [] and not (packages.root / "echo").exists()  # no step ran


def test_install_at_a_reviewed_commit_records_the_ref_and_commit(
    packages: Packages, repo: tuple[Path, Path]
) -> None:
    bare, work = repo
    review = packages.review(str(bare), "v1.0.0")
    git(work, "tag", "-f", "v1.0.0", "v2.0.0")  # the tag moves after the review
    git(work, "push", "-qf", "origin", "v1.0.0")
    package = packages.install(
        str(bare), ref="v1.0.0", commit=review["commit"], approved=review["permissions"]
    )
    assert (package.version, package.ref, package.commit) == ("1.0.0", "v1.0.0", review["commit"])
    folder = packages.root / "echo/1.0.0"
    assert (folder / "installed.txt").read_text() == "ok"  # its step ran, once approved
    assert not (folder / ".git").exists()
    record = json.loads((folder / RECORD).read_text())
    assert record["source"] == str(bare) and record["commit"] == review["commit"]


def test_a_branch_or_commit_installs_and_the_default_branch_without_a_ref(
    packages: Packages, repo: tuple[Path, Path]
) -> None:
    bare, work = repo
    first = git(work, "rev-parse", "v1.0.0^{commit}")
    assert packages.review(str(bare), first)["version"] == "1.0.0"
    assert packages.review(str(bare), "main")["version"] == "2.0.0"
    git(work, "push", "-q", "origin", f"{first}:refs/heads/old")
    assert packages.review(str(bare), "old")["commit"] == first


def test_an_upgrade_that_adds_a_permission_asks_again(
    packages: Packages, repo: tuple[Path, Path]
) -> None:
    bare, _ = repo
    packages.install(str(bare), ref="v1.0.0", approved=ECHO)
    review = packages.review(str(bare), "v2.0.0")
    added = ["network: eu.example.org"]
    assert review["upgrade"] == {"from": "1.0.0", "added": added, "removed": []}
    with pytest.raises(InstallError, match="first: network: eu.example.org"):
        packages.install(str(bare), ref="v2.0.0", approved=ECHO)
    package = packages.install(str(bare), ref="v2.0.0", approved=review["permissions"])
    assert [p.version for p in packages.list()] == ["1.0.0", "2.0.0"] and package.ref == "v2.0.0"
    with pytest.raises(InstallError, match="echo 2.0.0 is already installed"):
        packages.review(str(bare), "v2.0.0")


@pytest.mark.parametrize(
    ("source", "said"),
    [
        ("http://example.org/echo.git", "http:// is refused"),
        ("ssh://git@example.org/echo.git", "ssh:// is refused"),
        ("git://example.org/echo.git", "git:// is refused"),
        ("file:///tmp/echo.git", "file:// is refused"),
        ("https://me:token@example.org/echo.git", "the URL carries credentials"),
        ("https://token@example.org/echo.git", "the URL carries credentials"),
    ],
)
def test_other_urls_and_credentials_are_refused(packages: Packages, source: str, said: str) -> None:
    with pytest.raises(InstallError, match=said):
        packages.review(source, "v1.0.0")


def test_a_bad_or_unknown_ref_is_refused(packages: Packages, repo: tuple[Path, Path]) -> None:
    bare, _ = repo
    for ref in ("--upload-pack=touch x", "v1 0", "HEAD@{1}", ":/change"):
        with pytest.raises(InstallError, match="is not a tag, branch or commit"):
            packages.review(str(bare), ref)
    with pytest.raises(InstallError, match="has no tag, branch or commit v9"):
        packages.review(str(bare), "v9")


def test_a_symlink_out_of_the_package_is_refused(
    packages: Packages, repo: tuple[Path, Path], tmp_path: Path
) -> None:
    bare, work = repo
    secret = tmp_path / "id_rsa"
    secret.write_text("key")
    (work / "key").symlink_to(secret)
    (work / "inside").symlink_to("server.py")  # one inside the package is fine
    commit(work, "v3.0.0")
    with pytest.raises(InstallError, match="key is a symlink out of the package folder"):
        packages.review(str(bare), "v3.0.0")
    with pytest.raises(InstallError, match="key is a symlink out"):
        packages.install(str(work))  # a local folder too, whose copy would follow it
    (work / "key").unlink()
    (work / ".venv").mkdir()
    (work / ".venv/python").symlink_to(secret)  # the copy skips it
    assert packages.review(str(work), None)["version"] == "2.0.0"


def test_a_package_in_a_subfolder_installs_from_it(packages: Packages, tmp_path: Path) -> None:
    bare, work = tmp_path / "mono.git", tmp_path / "mono"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    shutil.copytree(FIXTURES / "packages/echo", work / "connectors/echo")
    git(work, "init", "-q")
    git(work, "remote", "add", "origin", str(bare))
    commit(work, "v1.0.0")
    review = packages.review(str(bare), "v1.0.0", "connectors/echo")
    assert (review["name"], review["permissions"]) == ("echo", ECHO)
    package = packages.install(str(bare), ref="v1.0.0", approved=ECHO, subfolder="connectors/echo")
    assert package.subfolder == "connectors/echo"
    assert (packages.root / "echo/1.0.0/installed.txt").read_text() == "ok"
    with pytest.raises(ManifestError, match="has no swarmify-connector.toml"):
        packages.review(str(bare), "v1.0.0")  # its root is not the package
    with pytest.raises(InstallError, match="has no folder connectors/nope"):
        packages.review(str(bare), "v1.0.0", "connectors/nope")
    for bad in ("../echo", "connectors/../..", "/etc", "a//b", "./x"):
        with pytest.raises(InstallError, match="is not a folder inside the repository"):
            packages.review(str(bare), "v1.0.0", bad)
