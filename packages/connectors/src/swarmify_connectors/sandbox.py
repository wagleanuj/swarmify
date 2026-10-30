"""The connector sandbox (ADR 0021): every connector process and install step runs inside
Anthropic's sandbox-runtime (Seatbelt on macOS; bubblewrap, socat and a seccomp filter on Linux),
the layer the Agent SDK puts a Build Session's Bash in (ADR 0018).

`Sandbox.wrap` turns a command into one that `sandbox/launcher.mjs` starts inside it, with:

- reads: everything but the data directory, the home folders and the temp folders, of which only
  its package folder, the runtimes, its own scratch folder and what `Grants.reads` names are left;
- writes: only its scratch folder (its HOME and TMPDIR) and what `Grants.writes` names;
- network: only `Grants.domains`, through sandbox-runtime's proxy; a bare `localhost` lets it bind
  and reach its own loopback servers (a render's browser talking to its bundle);
- environment: what the caller gives it, which for a connector is its own Setup values only, and
  no other process's (Linux's PID namespace; on macOS, launcher.mjs's Seatbelt rules).

Every attempt the sandbox blocks lands in the launcher's `blocked.jsonl`, which `Confined.blocked`
reads: proxy denials everywhere, Seatbelt's file denials on macOS, and on Linux the reads and
writes `observe.py` sees. With `SWARMIFY_SANDBOX_RUNNER` set to a socket, the launcher runs in the
sandbox runner container instead (`runner.py`), so the API container keeps Docker's default
seccomp and AppArmor profiles (issue 109).
"""

import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).parent
LAUNCHER = HERE.parents[1] / "sandbox" / "launcher.mjs"
OBSERVER = HERE / "observe.py"
RUNNER = "SWARMIFY_SANDBOX_RUNNER"
REGISTRIES = {
    "uv": ["pypi.org", "files.pythonhosted.org"],
    "npm": ["registry.npmjs.org"],
    "container": [],
}
"""What an install step reaches by its runtime (ADR 0026); a Manifest adds `install_network`."""
LOCAL = "localhost"
SHOWN = 20
"""Blocked attempts logged per process; more are counted, not listed."""
QUIET = ("/.CFUserTextEncoding", "/Library/Preferences/", "/C/com.apple.", "/T/xcrun_db")
QUIET += ("/var/cache/fontconfig/",)
"""What system libraries probe in every process that uses them: macOS's own and its developer
tools' shims (git, python3) the home folder's preferences and Apple's caches, fontconfig (a
browser's fonts) its system cache. Their denial says nothing."""
KEEP_SECONDS = 24 * 3600
"""How long a process's scratch folder outlives it, as a long job may still use it."""


class SandboxUnavailable(OSError):
    """The sandbox cannot start here: no Node, or the launcher's dependencies not installed."""


@dataclass
class Grants:
    """What one process may use besides its package folder."""

    reads: list[Path] = field(default_factory=list)
    writes: list[Path] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)


def domain(url: str) -> str | None:
    """A URL's host and port as a network permission, as `kokoro:8880`; None for no host."""
    parts = urlsplit(url.strip())
    if not parts.hostname:
        return None
    return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname


@dataclass
class Confined:
    """A command wrapped to run in the sandbox, and where its blocked attempts land."""

    argv: list[str]
    folder: Path
    cwd: Path
    read: int = 0

    def blocked(self) -> list[str]:
        """The attempts blocked since the last call, each once, as `read /path` or
        `deny network-outbound example.com:443 (...)`; the noise a runtime makes probing what
        it may not use (sysctls, Mach services, its own caches) left out."""
        file = self.folder / "blocked.jsonl"
        try:
            lines = file.read_text().splitlines()
        except FileNotFoundError:
            return []
        new, self.read = lines[self.read :], len(lines)
        out: list[str] = []
        for raw in new:
            try:
                line = str(json.loads(raw)["line"])
            except (ValueError, KeyError, TypeError):
                continue
            said = _said(line)
            if said and said not in out:
                out.append(said)
        if len(out) > SHOWN:
            out = [*out[:SHOWN], f"and {len(out) - SHOWN} more"]
        return out


def _said(line: str) -> str | None:
    """One blocked attempt in words, or None for one not worth a person's attention."""
    if line.startswith(("read ", "write ", "connect ")):  # observe.py, on Linux
        return None if any(q in line for q in QUIET) else line
    if line.startswith("deny network-outbound "):  # the proxy
        return "connect " + line.removeprefix("deny network-outbound ")
    # Seatbelt: `Python(123) deny(1) file-read-data /path`
    _, _, rest = line.partition(" deny(1) ")
    op, _, target = rest.partition(" ")
    if any(q in target for q in QUIET):
        return None
    if op == "file-read-data" and target.startswith("/"):
        return f"read {target}"
    if op.startswith("file-write") and target.startswith("/"):
        return f"write {target}"
    if op == "network-outbound" and target.startswith("remote:"):  # past the proxy
        return f"connect {target.removeprefix('remote:')}"
    if op == "sysctl-read" and target.startswith("kern.procargs"):  # `kern.procargs2.<pid>`
        return f"read the environment of process {target.rpartition('.')[2]}"
    return None


def _around(reads: list[Path], writes: list[Path]) -> list[Path]:
    """`reads` with each folder holding a writable one replaced by what else is in it, down to
    the writable one: bubblewrap mounts a readable folder read-only, over a writable one inside
    it bound before, so the stage folder of a run granted whole would be read-only."""
    out: list[Path] = []
    for read in reads:
        inside = [w for w in writes if w != read and w.is_relative_to(read)]
        if not inside:
            out.append(read)
        elif read.is_dir():
            children = [c.resolve() for c in read.iterdir()]
            out += _around([c for c in children if c not in inside], inside)
    return out


class Sandbox:
    """Wraps commands for one data directory. `scratch` holds each process's own folder (its
    plan, its blocked attempts and its HOME), shared with the runner container in Docker."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.scratch = self.data_dir / "sandbox"
        self.runner = os.environ.get(RUNNER) or None

    def wrap(
        self,
        argv: list[str],
        cwd: Path | None,
        grants: Grants,
        names: Iterable[str] = (),
        *,
        session: bool = False,
        path: str | None = None,
    ) -> Confined:
        """`argv`, run from `cwd` (its package folder; None for its own scratch folder, as for a
        command the add form drafted), as a command that runs it in the sandbox
        with `grants` and, of the environment it is started with, only the variables `names`
        (its Setup values) and the few every process needs (launcher.mjs). `session` marks an
        MCP server over stdio; `path`, the PATH it gets when not ours, whose folders it may read.
        Raises SandboxUnavailable when the sandbox cannot start here."""
        node = shutil.which("node")
        if node is None:
            raise SandboxUnavailable("the connector sandbox needs Node (node on PATH)")
        if not (LAUNCHER.parent / "node_modules" / "@anthropic-ai").is_dir():
            raise SandboxUnavailable(
                f"the connector sandbox is not installed: run pnpm install ({LAUNCHER.parent})"
            )
        self.prune()
        folder = self.scratch / f"{int(time.time())}-{secrets.token_hex(6)}"
        home = folder / "home"
        home.mkdir(parents=True)
        cwd = cwd.resolve() if cwd else home
        writes = [home, *(p.resolve() for p in grants.writes)]
        what = [cwd, home, *_around([p.resolve() for p in grants.reads], writes), *writes]
        reads = self._readable(what, path or os.environ.get("PATH", ""), cwd)  # and writes
        denied = self.denied()
        hosts = list(dict.fromkeys(grants.domains))
        nonce = secrets.token_hex(16)
        plan: dict[str, object] = {
            "sandbox": {
                "network": {
                    "allowedDomains": [h for h in hosts if h != LOCAL],
                    "deniedDomains": [],
                    "allowLocalBinding": LOCAL in hosts,
                },
                "filesystem": {
                    "denyRead": [str(p) for p in denied],
                    "allowRead": [str(p) for p in reads],
                    "allowWrite": [str(p) for p in writes],
                    "denyWrite": [],
                },
            },
            "env": {
                "HOME": str(home),
                "TMPDIR": str(home),
                "PYTHONDONTWRITEBYTECODE": "1",  # its package folder is read-only
                "NODE_USE_ENV_PROXY": "1",  # Node reaches the network through the proxy too
                "npm_config_update_notifier": "false",  # npx asks the registry for none
                "SWARMIFY_OBSERVE_NONCE": nonce,
            },
            "cwd": str(cwd),
            "pass": sorted(set(names)),
            "blocked": str(folder / "blocked.jsonl"),
            "nonce": nonce,
            "session": session,
            "observe": self._observer(denied, reads, writes) if sys.platform == "linux" else None,
        }
        (folder / "plan.json").write_text(json.dumps(plan))
        command = [node, str(LAUNCHER), str(folder / "plan.json"), "--", *argv]
        if self.runner:
            command = [sys.executable, "-m", "swarmify_connectors.runner", self.runner, *command]
        return Confined(command, folder, cwd)

    def denied(self) -> list[Path]:
        """The folders a sandboxed process cannot read but where granted: the data directory,
        every home folder and the temp folders, as they really are (symlinks resolved)."""
        roots = [self.data_dir, Path.home(), Path(tempfile.gettempdir()), Path("/tmp")]
        roots += [Path(p) for p in ("/Users", "/Volumes", "/var/folders", "/home", "/root")]
        roots += [Path(p) for p in ("/mnt", "/media", "/srv")]  # a server's own data
        return list(dict.fromkeys(p.resolve() for p in roots if p.exists()))

    def _readable(self, grants: list[Path], path: str, cwd: Path) -> list[Path]:
        """The grants plus the runtimes a package's command needs (the Python its `.venv` was
        made from, uv, Node, the folders on its PATH, the read tripwire, and Swarmify's own
        environment and code, whose Python a local package's command may name) where they live
        inside a denied folder, as in a home folder. A folder holding a denied one (`/`, the home
        folder itself) is never granted, nor the folder above one on PATH (`~/.cargo` holds
        crates.io's token beside its `bin`)."""
        found = [Path(sys.base_prefix), Path(sys.prefix), OBSERVER]
        found += [Path(p) for p in sys.path if p.startswith("/") and Path(p).is_dir()]
        found += [Path(p) for p in path.split(os.pathsep) if p.startswith("/")]
        venv = cwd / ".venv" / "pyvenv.cfg"
        if venv.is_file():  # `home = <its Python's bin>`
            for key, _, value in (line.partition("=") for line in venv.read_text().splitlines()):
                if key.strip() == "home" and value.strip().startswith("/"):
                    found.append(Path(value.strip()).parent)
        uv_pythons = os.environ.get("UV_PYTHON_INSTALL_DIR")
        found.append(Path(uv_pythons) if uv_pythons else Path.home() / ".local/share/uv/python")
        for tool in ("node", "uv", "git"):
            if where := shutil.which(tool):
                found.append(Path(where).resolve().parents[1])
        denied = self.denied()
        out = [p.resolve() for p in grants]
        for p in (p.resolve() for p in found if p.exists()):
            if any(d.is_relative_to(p) for d in denied):
                continue  # it would open what is denied
            if any(p.is_relative_to(d) for d in denied):
                out.append(p)
        return list(dict.fromkeys(out))

    @staticmethod
    def _observer(denied: list[Path], reads: list[Path], writes: list[Path]) -> list[str]:
        rules = {
            "denied": [str(p) for p in denied],
            "readable": [str(p) for p in reads],
            "writable": [str(p) for p in writes],
        }
        python = str(Path(sys.executable).resolve())
        return [python, str(OBSERVER), json.dumps(rules), "--"]

    def prune(self) -> None:
        """Remove the scratch folders of processes started more than a day ago."""
        if not self.scratch.is_dir():
            return
        cutoff = time.time() - KEEP_SECONDS
        for folder in self.scratch.iterdir():
            started = folder.name.split("-", 1)[0]
            if started.isdigit() and int(started) < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
