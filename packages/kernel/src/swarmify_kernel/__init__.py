"""Shared kernel. Bounded contexts may import this and nothing else of each other."""

from swarmify_kernel.ids import new_id

__all__ = ["new_id"]
