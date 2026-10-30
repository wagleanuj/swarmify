"""The tripwire of the connector sandbox on Linux (ADR 0021), run inside it:

    python observe.py '{"denied": [...], "readable": [...], "writable": [...]}' -- command ...

bubblewrap hides what a process may not read (an empty folder where the data directory is) and
its network namespace has no route out, and neither says anything when a process tries, so this
reports the attempts the way Seatbelt's log does on macOS (the proxy reports those through it).
It runs the command under a seccomp filter that hands every `open` and `connect` to this
process, which reads the arguments, lets the call go on unchanged, and writes one line to stderr,
after the launcher's nonce, for each path inside a `denied` folder and outside the `readable`
ones (`read <path>`), opened for writing outside the `writable` ones (`write <path>`), or each
address off this host it dials past the proxy (`connect <address>:<port>`). The mounts and the
namespace are the wall; this is a tripwire, and what it reads can be changed under it, so it
never decides anything.

It ends once every process under the filter has ended, not only the command: a long job the
command left running keeps the sandbox up until it is done. stdin and stdout are the command's
alone, so the launcher sees the MCP session end when the command closes its stdout. Where the
filter cannot be installed (an architecture it does not know, a kernel before 5.8), it runs the
command unobserved and says so once.
"""

import ctypes
import errno
import fcntl
import ipaddress
import json
import os
import platform
import select
import socket
import struct
import sys
from typing import NoReturn

ARCHES = {
    # AUDIT_ARCH, seccomp, then the syscalls it watches by name
    "x86_64": (0xC000003E, 317, {"open": 2, "openat": 257, "openat2": 437, "connect": 42}),
    "aarch64": (0xC00000B7, 277, {"openat": 56, "openat2": 437, "connect": 203}),
}
NOTIF_RECV, NOTIF_SEND = 0xC0502100, 0xC0182101  # _IOWR('!', 0 | 1, seccomp_notif | _resp)
CONTINUE, NEW_LISTENER, AT_FDCWD = 1, 8, -100
RET_ALLOW, RET_NOTIFY = 0x7FFF0000, 0x7FC00000
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
PROC = ("/proc", "/dev")


def program(arch: int, numbers: list[int]) -> bytes:
    """Classic BPF: hand each open to the listener, allow everything else."""
    ld, jeq, ret = 0x20, 0x15, 0x06
    out = [(ld, 0, 0, 4), (jeq, 0, len(numbers) + 1, arch), (ld, 0, 0, 0)]
    out += [(jeq, len(numbers) - i, 0, n) for i, n in enumerate(numbers)]
    out += [(ret, 0, 0, RET_ALLOW), (ret, 0, 0, RET_NOTIFY)]
    return b"".join(struct.pack("HBBI", *op) for op in out)


def within(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def verdict(path: str, flags: int, rules: dict[str, list[str]]) -> str | None:
    """`read` or `write` when the sandbox blocks this open, else None."""
    if flags & WRITE and not within(path, rules["writable"]) and not within(path, list(PROC)):
        return "write"
    if within(path, rules["denied"]) and not within(path, rules["readable"]):
        return "read"
    return None


class _Iovec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


_libc = ctypes.CDLL(None, use_errno=True)
if sys.platform == "linux":
    _libc.process_vm_readv.restype = ctypes.c_ssize_t


def _bytes(pid: int, address: int, n: int) -> bytes:
    """Up to `n` bytes of the target's memory at `address`, fewer where a page ends unreadable."""
    buf = ctypes.create_string_buffer(n)
    local, remote = _Iovec(ctypes.addressof(buf), n), _Iovec(address, n)
    got = _libc.process_vm_readv(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    if got < 0 and n > 4096 - address % 4096:  # the rest of this page, then
        return _bytes(pid, address, 4096 - address % 4096)
    return buf.raw[: max(got, 0)]


def _string(pid: int, address: int) -> str:
    return _bytes(pid, address, 4096).split(b"\0", 1)[0].decode(errors="replace")


def dialed(address: bytes) -> str | None:
    """A sockaddr's `address:port` when it leaves this host (IPv4 or IPv6), else None."""
    if len(address) < 8:
        return None
    family, port = struct.unpack_from("<H", address)[0], struct.unpack_from(">H", address, 2)[0]
    if family == socket.AF_INET:
        ip = ipaddress.ip_address(address[4:8])
    elif family == socket.AF_INET6 and len(address) >= 24:
        ip = ipaddress.ip_address(address[8:24])
    else:
        return None
    return None if ip.is_loopback else f"{ip}:{port}"


def _opened(pid: int, nr: int, args: tuple[int, ...], names: dict[str, int]) -> tuple[str, int]:
    """The absolute path and flags of an open the target is making."""
    if nr == names.get("open"):
        dirfd, path, flags = AT_FDCWD, _string(pid, args[0]), args[1]
    else:
        dirfd, path = ctypes.c_int32(args[0] & 0xFFFFFFFF).value, _string(pid, args[1])
        if nr == names["openat2"]:  # struct open_how starts with its u64 flags
            flags = struct.unpack("Q", _bytes(pid, args[2], 8).ljust(8, b"\0"))[0]
        else:
            flags = args[2]
    if not path.startswith("/"):
        base = f"/proc/{pid}/cwd" if dirfd == AT_FDCWD else f"/proc/{pid}/fd/{dirfd}"
        path = os.path.join(os.readlink(base), path)
    return os.path.realpath(path), flags


def run(command: list[str]) -> NoReturn:
    """Become the command, or end as a shell does when it cannot: 127 for none such."""
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        os.write(2, f"{command[0]}: {exc.strerror}\n".encode())
        os._exit(127 if exc.errno == errno.ENOENT else 126)


def main(argv: list[str]) -> int:
    rules, command = json.loads(argv[0]), argv[2:]
    nonce = os.environ.pop("SWARMIFY_OBSERVE_NONCE", "")
    arch = ARCHES.get(platform.machine())
    libc = _libc
    libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: the command cannot read our nonce

    def report(line: str) -> None:
        os.write(2, f"\x1fswarmify-blocked {nonce} {line}\n".encode())

    if arch is None:
        report(f"note: no read tripwire on {platform.machine()}")
        run(command)
    audit, seccomp, names = arch
    filt = program(audit, list(names.values()))
    buf = ctypes.create_string_buffer(filt)
    prog = struct.pack("HxxxxxxP", len(filt) // 8, ctypes.addressof(buf))
    ours, theirs = socket.socketpair()
    pid = os.fork()
    if pid == 0:  # the command: under the filter, the listener handed back, then exec
        ours.close()
        try:
            if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
                raise OSError(ctypes.get_errno(), "no_new_privs")
            fd = libc.syscall(seccomp, 1, NEW_LISTENER, ctypes.c_char_p(prog))
            if fd < 0:
                raise OSError(ctypes.get_errno(), "seccomp")
            socket.send_fds(theirs, [b"l"], [fd])
            os.close(fd)
        except OSError as exc:
            theirs.send(f"{exc}".encode()[:200] or b"?")
        theirs.close()
        run(command)
    theirs.close()
    message, fds, _, _ = socket.recv_fds(ours, 256, 1)
    ours.close()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)  # the command's stdin and stdout are its own
    os.dup2(devnull, 1)
    if not fds:
        report(f"note: no read tripwire: {message.decode(errors='replace')}")
        return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    listener, seen = fds[0], set()
    poll = select.poll()
    poll.register(listener, select.POLLIN)
    while True:
        events = dict(poll.poll())
        if events.get(listener, 0) & (select.POLLHUP | select.POLLERR):
            break  # no process uses the filter any more
        request = bytearray(80)
        try:
            fcntl.ioctl(listener, NOTIF_RECV, request, True)
        except OSError:
            continue  # the process ended before we read it
        ident, target = struct.unpack_from("QI", request)
        nr = struct.unpack_from("i", request, 16)[0]
        args = struct.unpack_from("6Q", request, 32)
        try:
            if nr == names["connect"]:
                kind, path = "connect", dialed(_bytes(target, args[1], min(args[2], 128)))
            else:
                path, flags = _opened(target, nr, args, names)
                kind = verdict(path, flags, rules)
            if kind and path and (kind, path) not in seen:
                seen.add((kind, path))
                report(f"{kind} {path}")
        except (OSError, ValueError, struct.error):
            pass  # a diagnostic: never hold the call up
        try:
            answer = bytearray(struct.pack("QqiI", ident, 0, 0, CONTINUE))
            fcntl.ioctl(listener, NOTIF_SEND, answer, True)
        except OSError:
            pass
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
