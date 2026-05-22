"""Public entrypoint for the FERAL Python node SDK.

Re-exports `FeralNode` (the public class vendors build daemons against) and
the `capability` enum module so a one-line import is enough for most apps:
``from feral_node_sdk import FeralNode, capability``.
"""

from __future__ import annotations

from . import capability, schemas
from .node import FeralNode
from .pairing import load_key, save_key
from .discovery import discover_brain

__all__ = [
    "FeralNode",
    "capability",
    "schemas",
    "load_key",
    "save_key",
    "discover_brain",
]

__version__ = "1.3.0"
HUP_VERSION = "1.3.0"
