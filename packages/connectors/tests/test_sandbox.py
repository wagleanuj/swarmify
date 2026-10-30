"""The connector sandbox (ADR 0021): what a process may use, and that a connector trying to leave
it is blocked each time and each block reported with its name, on macOS (Seatbelt) and on Linux
(bubblewrap and the tripwire in observe.py)."""

import json
import os
import shutil
import socket
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

from swarmify_connectors import Connectors, Credentials, McpTransport, SqliteStorage, observe
from swarmify_connectors.packages import MANIFEST
from swarmify_connectors.sandbox import LAUNCHER, Confined, Grants, Sandbox

FIXTURES = Path(__file__).parent / "fixtures/packages"
ADDRESS = "203.0.113.7:80"
"""TEST-NET-3: an address no one answers, so a test never reaches a real host."""


def install(connectors: Connectors, tmp_path: Path, fixture: str, name: str = "") -> None:
    """Install a copy of a fixture package, started with this Python, as `name`."""
    source = tmp_path / "src" / (name or fixture)
    shutil.copytree(FIXTURES / fixture, source)
    toml = (source / MANIFEST).read_text()
    toml = toml.replace('["python3", "server.py"]', json.dumps([sys.executable, "server.py"]))
    if name:
        toml = toml.replace(f'name = "{fixture}"', f'name = "{name}"')
        toml = toml.replace('env = "PROBE_KEY"', 'env = "OTHER_KEY"')
    (source / MANIFEST).write_text(toml)
    connectors.packages.install(str(source))


def test_a_connector_trying_to_leave_its_sandbox_is_blocked_and_each_block_is_reported(
    tmp_path: Path, db: Path
) -> None:
    blocked: list[tuple[str, Any, list[str]]] = []
    real = Connectors(
        SqliteStorage(db),
        McpTransport(),
        Credentials(tmp_path / ".env"),
        0.05,
        on_blocked=lambda c, at, lines: blocked.append((c.name, at, lines)),
    )
    install(real, tmp_path, "intruder")
    install(real, tmp_path, "probe", "other")  # another package, its key saved beside ours
    for name in ("intruder", "other"):
        real.enable("w1", name, "1.0.0")
    real.setups.put("other", "w1", {"api_key": "other-secret"}, {})
    check = real.save_setup("w1", "intruder", {"api_key": "mine"}, {})
    # its Setup test's message on the Connectors page says what the sandbox blocked
    assert check is not None and check.ok
    assert check.message.startswith("Signed in. The sandbox blocked: ")
    assert f"read {tmp_path.resolve() / 'setup.json'}" in check.message
    (tmp_path / "worlds" / "w2" / "notes").mkdir(parents=True)  # another world's folder

    params = {"tool": "trespass", "data_dir": str(tmp_path), "other_world": "w2"}
    params |= {"other_key": "OTHER_KEY", "address": ADDRESS}
    call = real.run_stage("w1", "run_1", "s1", {"calls": "intruder", "params": params})

    tried = call.response or {}
    assert set(tried) == {
        "undeclared_folder",
        "credential_file",
        "credential_env",
        "undeclared_domain",
        "undeclared_address",
    }
    assert all(v.startswith("refused") for v in tried.values()), tried
    assert "other-secret" not in json.dumps(tried)
    assert {(name, at) for name, at, _ in blocked} == {
        ("intruder", None),
        ("intruder", ("run_1", "s1")),
    }
    lines = [line for _, at, said in blocked if at for line in said]
    data = tmp_path.resolve()
    assert f"read {data / 'worlds' / 'w2'}" in lines, lines
    assert f"read {data / 'setup.json'}" in lines, lines
    assert any(line.startswith("connect example.com:80 ") for line in lines), lines
    dialed = ADDRESS if sys.platform == "linux" else "*:80"  # Seatbelt names only the port
    assert f"connect {dialed}" in lines, lines


def test_a_process_gets_its_own_values_and_the_sandbox_none_of_the_apis(
    tmp_path: Path, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher passes on only the variables named for it: its Setup values, not the API's
    own (the sign-in password, the Claude token), whatever started it."""
    monkeypatch.setenv("SWARMIFY_PASSWORD", "hunter2")
    real = Connectors(SqliteStorage(db), McpTransport(), Credentials(tmp_path / ".env"), 0.05)
    install(real, tmp_path, "probe")
    real.enable("w1", "probe", "1.0.0")
    real.save_setup("w1", "probe", {"api_key": "good"}, {})
    connector = real.find("w1", "probe")
    assert connector is not None
    env = {**os.environ, **real._env(connector)}  # all of the API's, as a CLI passes them on
    confined, sandboxed = real.confine(connector, real._env(connector))
    assert confined is not None
    seen = McpTransport().call(sandboxed, "environ", {}, env)["env"]
    assert seen["PROBE_KEY"] == "good" and "SWARMIFY_PASSWORD" not in seen
    assert seen["HOME"] == seen["TMPDIR"] == str(confined.folder / "home")


def test_what_a_process_may_use(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path)
    package, stage = tmp_path / "packages/p/1.0.0", tmp_path / "worlds/w/runs/r/s"
    earlier = stage.with_name("before")
    for folder in (package, stage, earlier):
        folder.mkdir(parents=True)
    grants = Grants(reads=[stage.parent], writes=[stage], domains=["api.example.org", "localhost"])
    confined = sandbox.wrap(["python3", "server.py"], package, grants, ["KEY"], session=True)
    assert confined.argv[1:3] == [str(LAUNCHER), str(confined.folder / "plan.json")]
    assert confined.argv[3:] == ["--", "python3", "server.py"]
    plan = json.loads((confined.folder / "plan.json").read_text())
    fs, net = plan["sandbox"]["filesystem"], plan["sandbox"]["network"]
    data = str(tmp_path.resolve())
    assert data in fs["denyRead"] and str(Path.home().resolve()) in fs["denyRead"]
    assert data not in fs["allowRead"]  # only what is granted of it
    home = str(confined.folder / "home")
    for granted in (str(package), str(earlier), str(stage), home):
        assert granted in fs["allowRead"]
    # the run's folder holds the writable stage folder: bubblewrap would mount it read-only
    # over it, so what else is in it is granted instead
    assert str(stage.parent) not in fs["allowRead"]
    assert fs["allowWrite"] == [home, str(stage)]
    assert net == {
        "allowedDomains": ["api.example.org"],
        "deniedDomains": [],
        "allowLocalBinding": True,
    }
    assert plan["pass"] == ["KEY"] and plan["session"] is True and plan["cwd"] == str(package)
    assert plan["env"]["HOME"] == home and plan["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_a_runtime_folder_holding_a_denied_one_is_never_granted(tmp_path: Path) -> None:
    path = os.pathsep.join(["/", str(Path.home()), "/usr", str(Path.home() / "bin")])
    readable = Sandbox(tmp_path)._readable([], path, tmp_path)
    assert Path("/") not in readable and Path.home().resolve() not in readable


def test_blocked_attempts_read_as_words_once_each_without_the_noise(tmp_path: Path) -> None:
    lines = [
        "Python(1) deny(1) file-read-data /Users/x/.ssh/id_rsa",
        "Python(1) deny(1) file-read-data /Users/x/.ssh/id_rsa",  # once
        "Python(1) deny(1) file-write-create /data/planted.txt",
        "deny network-outbound example.com:443 (host is not on the allow list)",
        "Python(1) deny(1) network-outbound remote:*:80",
        "Python(1) deny(1) sysctl-read kern.iossupportversion",  # a runtime probing: noise
        "Python(1) deny(1) file-read-data /Users/x/.CFUserTextEncoding",
        "Python(1) deny(1) file-read-data /Users/x/Library/Preferences/.GlobalPreferences.plist",
        "read /data/setup.json",
        "write /var/cache/fontconfig/x.cache-9",  # a browser's font cache: noise
        "connect 203.0.113.7:80",
    ]
    (tmp_path / "blocked.jsonl").write_text("".join(json.dumps({"line": x}) + "\n" for x in lines))
    confined = Confined([], tmp_path, tmp_path)
    assert confined.blocked() == [
        "read /Users/x/.ssh/id_rsa",
        "write /data/planted.txt",
        "connect example.com:443 (host is not on the allow list)",
        "connect *:80",
        "read /data/setup.json",
        "connect 203.0.113.7:80",
    ]
    assert confined.blocked() == []  # what was read is not reported again
    with (tmp_path / "blocked.jsonl").open("a") as f:
        f.writelines(json.dumps({"line": f"read /x/{n}"}) + "\n" for n in range(25))
    later = confined.blocked()
    assert len(later) == 21 and later[-1] == "and 5 more"


def test_the_tripwire_judges_each_open_and_dial_as_the_sandbox_does() -> None:
    rules = {"denied": ["/data", "/home"], "readable": ["/data/packages/p"], "writable": ["/w"]}
    assert observe.verdict("/data/setup.json", 0, rules) == "read"
    assert observe.verdict("/data/packages/p/server.py", 0, rules) is None
    assert observe.verdict("/data2/x", 0, rules) is None  # a sibling, not inside
    assert observe.verdict("/usr/lib/libc.so", 0, rules) is None
    assert observe.verdict("/etc/passwd", observe.os.O_WRONLY, rules) == "write"
    assert observe.verdict("/w/out.mp4", observe.os.O_CREAT | observe.os.O_WRONLY, rules) is None
    assert observe.verdict("/dev/null", observe.os.O_WRONLY, rules) is None

    def sockaddr(family: int, port: int, address: bytes) -> bytes:
        head = struct.pack("<H", family) + struct.pack(">H", port)
        return head + (address if family == socket.AF_INET else b"\0" * 4 + address)

    assert observe.dialed(sockaddr(socket.AF_INET, 80, bytes([203, 0, 113, 7]))) == ADDRESS
    assert observe.dialed(sockaddr(socket.AF_INET, 3128, bytes([127, 0, 0, 1]))) is None  # proxy
    six = socket.inet_pton(socket.AF_INET6, "2001:db8::1")
    assert observe.dialed(sockaddr(socket.AF_INET6, 443, six)) == "2001:db8::1:443"
    assert observe.dialed(b"\x01\x00/tmp/sock") is None  # a unix socket


def test_the_tripwire_filter_hands_on_only_its_syscalls() -> None:
    """Classic BPF: past the arch check, each watched number jumps to the notify return, every
    other syscall falls through to allow."""
    numbers = [2, 257, 437, 42]
    ops = list(struct.iter_unpack("HBBI", observe.program(7, numbers)))
    allow, notify = len(ops) - 2, len(ops) - 1

    def run(arch: int, nr: int) -> int:
        at, acc, data = 0, 0, {0: nr, 4: arch}
        while True:
            code, jt, jf, k = ops[at]
            if code == 0x20:
                acc, at = data[k], at + 1
            elif code == 0x15:
                at = at + 1 + (jt if acc == k else jf)
            else:
                return at

    assert all(run(7, n) == notify for n in numbers)
    assert run(7, 0) == allow and run(8, 257) == allow
