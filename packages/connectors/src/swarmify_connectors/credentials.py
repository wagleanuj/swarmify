"""Credentials: one env file in the home data dir (`~/.swarmify/.env`), mode 600.

Values never enter the database, a run, a workflow or an API response; the service reads them
only to hand a connector's own names to its transport.
"""

import os
import re
import stat
from pathlib import Path

NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class Credentials:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, str]:
        """Every `NAME=value` line. Refuses a file that group or others can read."""
        if not self.path.exists():
            return {}
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"{self.path} is mode {mode:o}; run `chmod 600 {self.path}` so only you can read it"
            )
        values: dict[str, str] = {}
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip().removeprefix("export ").strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
        return values

    def set(self, values: dict[str, str]) -> None:
        """Merge `values` into the file, written atomically with mode 600."""
        for key, value in values.items():
            if not NAME.match(key):
                raise ValueError(f"credential names are UPPER_SNAKE_CASE: {key!r}")
            if "\n" in value or "\r" in value:
                raise ValueError(f"credential {key} holds a line break")
        merged = {**self.read(), **values}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(f"{k}={v}\n" for k, v in merged.items())
        os.chmod(tmp, 0o600)  # O_CREAT's mode is masked by umask and ignored for an existing tmp
        os.replace(tmp, self.path)
