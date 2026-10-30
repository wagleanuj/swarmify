"""The sandbox runner (issue 109): in Docker, connector processes start in a container of their
own, as does a Build Session's Claude CLI (ADR 0018, whose Bash it sandboxes), so the API
container, which holds the Claude token, the sign-in password and every Setup value, keeps
Docker's default seccomp and AppArmor profiles. bubblewrap needs user namespaces and its own
/proc, which those profiles refuse; only the runner gets the narrow exceptions it needs
(`docker/seccomp-sandbox.json`, compose.yaml), and it mounts no secrets: each process brings the
values it is given.

    python -m swarmify_connectors.runner serve <socket>       # the runner container
    python -m swarmify_connectors.runner <socket> <command>   # the API container, per process

The API side stands in for the process: it sends the command, its environment and its folder
over the socket, then its stdin as frames (kind, length, bytes; an empty one ends it), and gets
back its stdout, stderr and exit code as frames. The runner starts only `node <launcher.mjs> ...`
and the Claude CLI, both of which run what they are given inside the sandbox, and stops a
process whose caller went away.
"""

import asyncio
import json
import os
import shutil
import signal
import socket
import struct
import sys
import threading
from pathlib import Path

from swarmify_connectors.sandbox import LAUNCHER

OUT, ERR, EXIT = 1, 2, 3
HEAD = struct.Struct("!BI")


async def _serve_one(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    def send(kind: int, data: bytes) -> None:
        writer.write(HEAD.pack(kind, len(data)) + data)

    try:
        ask = json.loads(await reader.readline())
        argv, env, cwd = list(ask["argv"]), dict(ask["env"]), str(ask["cwd"])
        launcher = len(argv) > 1 and Path(argv[1]).resolve() == LAUNCHER.resolve()
        if not (launcher or argv and argv[0] == shutil.which("claude")):
            raise ValueError("the runner starts only the sandbox launcher and the Claude CLI")
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (ValueError, KeyError, TypeError, OSError) as exc:
        send(ERR, f"the sandbox runner refused the command: {exc}\n".encode())
        send(EXIT, struct.pack("!i", 1))
        await writer.drain()
        writer.close()
        return
    assert process.stdin and process.stdout and process.stderr

    async def feed() -> None:
        """The caller's stdin as frames, then an empty one for its end; the socket closing
        before that means the caller went away, and what it started is stopped."""
        try:
            while True:
                kind, size = HEAD.unpack(await reader.readexactly(HEAD.size))
                data = await reader.readexactly(size)
                if not data:
                    process.stdin.close()  # type: ignore[union-attr]
                    break
                process.stdin.write(data)  # type: ignore[union-attr]
                await process.stdin.drain()  # type: ignore[union-attr]
        except (asyncio.IncompleteReadError, ConnectionError):
            _stop(process)
            return
        await reader.read()  # until the caller goes
        if process.returncode is None:
            _stop(process)

    async def pump(stream: asyncio.StreamReader, kind: int) -> None:
        while data := await stream.read(65536):
            send(kind, data)
            await writer.drain()

    feeding = asyncio.create_task(feed())
    try:
        await asyncio.gather(pump(process.stdout, OUT), pump(process.stderr, ERR))
        code = await process.wait()
        send(EXIT, struct.pack("!i", code))
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        feeding.cancel()
        _stop(process)
        writer.close()


def _stop(process: asyncio.subprocess.Process) -> None:
    """SIGTERM to the launcher, which stops the sandbox's process group in turn."""
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


async def serve(path: str) -> None:
    Path(path).unlink(missing_ok=True)
    server = await asyncio.start_unix_server(_serve_one, path)
    os.chmod(path, 0o600)
    async with server:
        await server.serve_forever()


def run(path: str, argv: list[str]) -> int:
    """Run `argv` through the runner at `path` as if it ran here; its exit code."""
    with socket.socket(socket.AF_UNIX) as conn:
        conn.connect(path)
        head = {"argv": argv, "env": dict(os.environ), "cwd": os.getcwd()}
        conn.sendall(json.dumps(head).encode() + b"\n")

        def feed() -> None:
            try:
                while data := os.read(0, 65536):
                    conn.sendall(HEAD.pack(OUT, len(data)) + data)
                conn.sendall(HEAD.pack(OUT, 0))  # our stdin ended
            except OSError:
                pass

        threading.Thread(target=feed, daemon=True).start()
        file = conn.makefile("rb")
        while head_bytes := file.read(HEAD.size):
            kind, size = HEAD.unpack(head_bytes)
            data = file.read(size)
            if kind == EXIT:
                return int(struct.unpack("!i", data)[0])
            os.write(1 if kind == OUT else 2, data)
    return 1  # the runner went away


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        asyncio.run(serve(sys.argv[2]))
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
        sys.exit(run(sys.argv[1], sys.argv[2:]))
