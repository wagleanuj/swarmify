"""Prefixed ids such as `run_01HX...`, sortable by creation time."""

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    """Return `<prefix>_<ulid>`. Prefix is the entity name, e.g. `run` or `world`."""
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"
