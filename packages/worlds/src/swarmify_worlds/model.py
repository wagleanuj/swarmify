from dataclasses import dataclass


@dataclass(frozen=True)
class World:
    id: str
    name: str
    purpose: str
    created_at: str
