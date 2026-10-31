"""The connector sandbox (ADR 0021): what a process may use, and that a connector trying to leave
it is blocked each time and each block reported with its name, on macOS (Seatbelt) and on Linux
(bubblewrap and the tripwire in observe.py)."""

import json
import os
import runpy
import shutil
import socket
import struct
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from swarmify_connectors import Connectors, Credentials, McpTransport, SqliteStorage, observe
from swarmify_connectors.packages import MANIFEST
from swarmify_connectors.sandbox import LAUNCHER, Confined, Grants, Sandbox, SandboxUnavailable

FIXTURES = Path(__file__).parent / "fixtures/packages"
ADDRESS = "203.0.113.7:80"
"""TEST-NET-3: an address no one answers, so a test never reaches a real host."""
SEATBELT = sys.platform == "darwin"
"""macOS reports Seatbelt's blocks through its log, which drops some under load: there they are
blocked all the same, but may go unreported."""


def reported(said: list[str], expected: set[str]) -> None:
    """Each block in `expected` was reported, or with Seatbelt, which reports only some, that
    what was reported is among them."""
    if SEATBELT:
        assert set(said) <= expected, (said, expected)
    else:
        assert expected <= set(said), (said, expected)


def install(
    connectors: Connectors, tmp_path: Path, fixture: str, name: str = "", network: str = ""
) -> None:
    """Install a copy of a fixture package, started with this Python, as `name`, reaching
    `network` instead of what it declares."""
    source = tmp_path / "src" / (name or fixture)
    shutil.copytree(FIXTURES / fixture, source)
    toml = (source / MANIFEST).read_text()
    toml = toml.replace('["python3", "server.py"]', json.dumps([sys.executable, "server.py"]))
    if network:
        toml = toml.replace('network = ["api.example.org"]', f'network = ["{network}"]')
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
    data = tmp_path.resolve()
    first = [line for _, at, said in blocked for line in said]
    reported(first, {f"read {data / 'setup.json'}"})
    # its Setup test's message on the Connectors page says what the sandbox blocked
    assert check is not None and check.ok
    said = f" The sandbox blocked: {'; '.join(first)}." if first else ""
    assert check.message == f"Signed in.{said}"
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
    assert {name for name, _, _ in blocked} == {"intruder"}
    assert {at for _, at, _ in blocked} - {None} == {("run_1", "s1")}
    lines = [line for _, at, said in blocked if at for line in said]
    proxy = [line for line in lines if line.startswith("connect example.com:80 ")]
    assert proxy, lines  # the proxy's own, reported everywhere
    dialed = ADDRESS if sys.platform == "linux" else "*:80"  # Seatbelt names only the port
    expected = {f"read {data / 'worlds' / 'w2'}", f"read {data / 'setup.json'}"}
    expected.add(f"connect {dialed}")
    reported([line for line in lines if line not in proxy], expected)


@pytest.mark.skipif(sys.platform != "darwin", reason="Linux's PID namespace hides them")
@pytest.mark.parametrize("network", ["", "localhost"])  # localhost: a browser's wider profile
def test_a_connector_on_macos_cannot_read_another_process_environment(
    tmp_path: Path, db: Path, network: str
) -> None:
    """Not the API's (a process started with its secret stands in) nor a running connector's,
    by any route the intruder knows, while outside the sandbox those routes read both. Seatbelt's
    log drops most of a flood of denials, so which of these are reported is not held."""
    real = Connectors(SqliteStorage(db), McpTransport(), Credentials(tmp_path / ".env"), 0.05)
    install(real, tmp_path, "intruder", network=network)
    install(real, tmp_path, "probe", "other")
    for name in ("intruder", "other"):
        real.enable("w1", name, "1.0.0")
    real.setups.put("intruder", "w1", {"api_key": "mine"}, {})
    real.setups.put("other", "w1", {"api_key": "other-secret"}, {})
    other = real.find("w1", "other")
    assert other is not None
    env = real._env(other)
    _, sandboxed = real.confine(other, env)
    api = [sys.executable, "-c", "import time; time.sleep(60)"]
    values = ["api-secret", "other-secret"]
    with (
        subprocess.Popen(api, env={**os.environ, "SWARMIFY_PASSWORD": "api-secret"}) as stand_in,
        McpTransport().session(sandboxed, env) as call,
    ):
        try:
            assert call("environ", {})["env"]["OTHER_KEY"] == "other-secret"  # it runs
            snoop = runpy.run_path(str(FIXTURES / "intruder" / "server.py"))["snoop"]
            assert snoop(values)["procargs2"] == f"got {values}"  # the routes work out here
            params = {"tool": "snoop", "values": values}
            tried = real.run_stage("w1", "run_1", "s1", {"calls": "intruder", "params": params})
        finally:
            stand_in.kill()
    assert set(tried.response or {}) == {"procargs2", "procargs", "ps"}
    assert all(v.startswith("refused") for v in (tried.response or {}).values()), tried.response


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


ROOT = LAUNCHER.parents[3]
README = ROOT / "README.md"
INSIDE = f"""
from pathlib import Path
try:
    print(Path({str(README)!r}).read_text()[:0] + "read the checkout")
except OSError:
    print("inside")
"""
APP = f"""
import multiprocessing, subprocess, sys
from pathlib import Path
from swarmify_connectors.sandbox import Grants, Sandbox

def run(data):
    confined = Sandbox(Path(data)).wrap([sys.executable, "-c", {INSIDE!r}], None, Grants())
    out = subprocess.run(confined.argv, capture_output=True, text=True, timeout=60)
    return out.stdout, out.returncode

def worker(data, queue):
    assert {str(ROOT)!r} in sys.path, sys.path  # multiprocessing made uvicorn's '' absolute
    queue.put(run(data))

if __name__ == "__main__":
    mode, data = sys.argv[1:]
    if mode.startswith("uvicorn"):
        sys.path.insert(0, "")  # its --app-dir
    if mode == "uvicorn --reload":  # its worker is a multiprocessing spawn
        queue = multiprocessing.get_context("spawn").Queue()
        multiprocessing.get_context("spawn").Process(target=worker, args=(data, queue)).start()
        print(queue.get(timeout=90))
    else:
        print(run(data))
"""


@pytest.mark.parametrize("mode", ["pytest", "python", "uvicorn", "uvicorn --reload"])
def test_a_connector_runs_but_cannot_read_the_checkout_however_the_app_was_started(
    tmp_path: Path, mode: str
) -> None:
    """Swarmify's own install lends the sandbox only what it runs with (its Python, the read
    tripwire, on Linux bubblewrap's seccomp helper), found the same way whatever is on `sys.path`:
    the checkout under pytest (the root conftest) and in `uvicorn --reload`'s worker, none of it
    under plain uvicorn (`''`) or `python -c` (as `mise run gen-client` starts the app)."""
    assert README.is_file()
    app = tmp_path / "app.py"
    app.write_text(APP)
    if mode == "pytest":
        assert str(ROOT) in sys.path, sys.path
        assert runpy.run_path(str(app))["run"](tmp_path) == ("inside\n", 0)
        return
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    argv = [sys.executable, str(app), mode, str(tmp_path)]
    out = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == repr(("inside\n", 0)), out.stdout


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


NODE_FETCHES = """
const http = require("node:http");
const [declared, undeclared] = process.argv.slice(1);
const said = (how, url, what) =>
  console.log(`${how} ${url === declared ? "declared" : "undeclared"} ${what}`);
for (const url of [declared, undeclared]) {
  fetch(url).then(
    (r) => r.text().then((t) => said("fetch", url, `${r.status} ${t}`)),
    (e) => said("fetch", url, e.cause?.code ?? e.message),
  );
  http.get(url, (r) => {
    let t = "";
    r.on("data", (d) => (t += d));
    r.on("end", () => said("http", url, `${r.statusCode} ${t}`));
  }).on("error", (e) => said("http", url, e.code));
}
"""


def test_node_reaches_a_declared_host_through_the_proxy_and_no_other(tmp_path: Path) -> None:
    """Node's fetch and http, as an install step's downloads use them (Remotion's browser), go
    through the sandbox's proxy, which lets the declared host through: no network needed, the
    hosts are two local servers, one declared."""

    class Hello(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Hello) for _ in range(2)]
    for server in servers:
        Thread(target=server.serve_forever, daemon=True).start()
    declared, undeclared = (f"localhost:{s.server_address[1]}" for s in servers)
    try:
        argv = ["node", "-e", NODE_FETCHES, f"http://{declared}/", f"http://{undeclared}/"]
        confined = Sandbox(tmp_path).wrap(argv, None, Grants(domains=[declared]))
        done = subprocess.run(confined.argv, capture_output=True, text=True, timeout=60)
    finally:
        for server in servers:
            server.shutdown()
    said = done.stdout.splitlines()
    assert {"fetch declared 200 hello", "http declared 200 hello"} <= set(said), done
    refused = [line for line in said if line.startswith(("fetch undeclared", "http undeclared"))]
    assert len(refused) == 2 and not any("hello" in line for line in refused), said
    assert f"connect {undeclared} " in " ".join(confined.blocked()) + " "


def test_a_node_that_ignores_the_proxy_is_refused(tmp_path: Path) -> None:
    """Node before 22.21 ignores `NODE_USE_ENV_PROXY` and dials hosts itself, which the sandbox
    refuses (`ENOTFOUND`): the sandbox says so instead of starting it."""
    old = tmp_path / "bin"
    old.mkdir()
    (old / "node").write_text("#!/bin/sh\necho v22.16.0\n")
    (old / "node").chmod(0o755)
    with pytest.raises(SandboxUnavailable, match=r"Node 22\.21 or later .* is 22\.16\.0"):
        Sandbox(tmp_path).wrap(["npx", "x"], None, Grants(), path=f"{old}{os.pathsep}/usr/bin")


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
        "Python(1) deny(1) sysctl-read kern.procargs2.42",  # `ps eww`
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
        "read the environment of process 42",
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
