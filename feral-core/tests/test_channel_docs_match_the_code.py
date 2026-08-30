"""Channel docs must document the keys the channels actually read.

Every published channel page documented its own invented settings:

    telegram.mdx   allowed_user_ids, allow_groups, group_trigger,
                   max_message_length, send_typing_indicator
    discord.mdx    allowed_channel_ids, allowed_guild_ids,
                   respond_to_mentions_only, prefix
    slack.mdx      allowed_channels, respond_to_mentions_only,
                   thread_replies

None of those strings existed anywhere in the repository, and **no doc
anywhere mentioned ``allowed_senders`` or ``allowed_chats``**, which are
the only keys that actually gate channel access
(``channels/base.py:235``).

The consequence is not a security hole -- the gate is fail-closed, so an
operator who followed the docs got a channel that silently rejected every
message rather than one that was wide open. But the old tables described
``[]`` as "(all)", which is the opposite of what the code does, so the
docs were wrong about the direction of failure as well as the key names.

Rather than hardcode a list of forbidden strings, this derives the valid
key set from ``channels/base.py`` by AST. A channel that gains a real
setting starts passing automatically; a doc that invents one fails.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHANNEL_DOCS = sorted((REPO / "docs" / "mintlify" / "channels").glob("*.mdx"))

# Keys that are structural rather than read through ``config.get`` --
# the manifest/loader layer consumes these.
_STRUCTURAL = {"channels", "enabled", "type", "name", "id"}


def _real_config_keys() -> set[str]:
    """Every key any channel reads from its config, via AST.

    Scans the whole ``channels/`` package rather than just ``base.py``:
    push, matrix, signal, feishu, zalo and voice_call are separate
    modules with their own settings, and reading only base.py would flag
    every one of those as invented.
    """
    keys: set[str] = set()
    sources = [
        f for f in sorted((ROOT / "channels").rglob("*.py"))
        if "__pycache__" not in str(f)
    ]
    src = "\n".join(f.read_text() for f in sources)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        base = node.func.value
        holder = getattr(base, "attr", getattr(base, "id", ""))
        if holder in ("config", "cfg", "_config"):
            keys.add(node.args[0].value)
    # Channel *names* are object keys in the json examples, not
    # settings. Derive them from the modules present so adding a channel
    # does not make its own page fail.
    keys.update(f.stem for f in sources if not f.stem.startswith("_"))
    keys.update({"telegram", "discord", "slack", "whatsapp"})

    # ACCESS_CONFIG_KEYS is the declared contract; include it explicitly
    # so a refactor that stops calling .get() directly does not silently
    # shrink the allowlist.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "ACCESS_CONFIG_KEYS":
                    if isinstance(node.value, (ast.Tuple, ast.List)):
                        for el in node.value.elts:
                            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                                keys.add(el.value)
    return keys


def _documented_keys(doc: Path) -> set[str]:
    """Config keys a page presents inside a ``channels`` json example."""
    keys: set[str] = set()
    for block in re.findall(r"```json[^\n]*\n(.*?)```", doc.read_text(), re.S):
        if '"channels"' not in block:
            continue
        keys.update(re.findall(r'"([a-z][a-z0-9_]{2,})"\s*:', block))
    return keys - _STRUCTURAL


def test_the_access_control_keys_are_what_we_think():
    """Pins the contract the rest of this file rests on."""
    real = _real_config_keys()
    assert {"allowed_senders", "allowed_chats", "pairing_window_sec"} <= real, real


@pytest.mark.parametrize("doc", CHANNEL_DOCS, ids=lambda p: p.name)
def test_no_channel_doc_invents_a_setting(doc):
    real = _real_config_keys()
    invented = sorted(_documented_keys(doc) - real)
    assert not invented, (
        f"{doc.name} documents settings the channel layer never reads: "
        f"{invented}. Real keys come from channels/base.py -- "
        f"{sorted(real)}"
    )


@pytest.mark.parametrize(
    "doc",
    [d for d in CHANNEL_DOCS if d.name in {"telegram.mdx", "discord.mdx", "slack.mdx"}],
    ids=lambda p: p.name,
)
def test_the_real_access_keys_are_actually_documented(doc):
    """The other half. Removing the fabrications is not enough if the
    page then documents no access control at all -- the gate is
    fail-closed, so an operator who configures nothing gets a channel
    that silently ignores them."""
    text = doc.read_text()
    assert "allowed_senders" in text, (
        f"{doc.name} does not mention allowed_senders, so a reader has no "
        "way to learn how to let anyone through a fail-closed gate"
    )


def test_the_docs_do_not_describe_an_empty_allowlist_as_permissive():
    """The old tables said ``[]`` meant "(all)". It means none."""
    offenders = []
    for doc in CHANNEL_DOCS:
        for i, line in enumerate(doc.read_text().splitlines(), 1):
            if re.search(r"`\[\]`\s*\(all\)", line):
                offenders.append(f"{doc.name}:{i}")
    assert not offenders, (
        "these describe an empty allowlist as allowing everything; the "
        f"gate is fail-closed (channels/base.py:345-362): {offenders}"
    )


def test_discord_does_not_claim_slash_commands():
    """It registers none, and handles only MESSAGE_CREATE."""
    doc = REPO / "docs" / "mintlify" / "channels" / "discord.mdx"
    src = (ROOT / "channels" / "base.py").read_text()
    assert "INTERACTION_CREATE" not in src and "applications/" not in src, (
        "the brain now registers Discord application commands; this test "
        "is stale and the doc may describe them again"
    )
    text = doc.read_text()
    assert "registers none" in text.lower(), (
        "discord.mdx must say plainly that no slash commands are registered"
    )
    # A live claim is a table row; prose inside the correction is fine.
    rows = [
        line.strip()
        for line in text.splitlines()
        if line.lstrip().startswith("|") and "/feral " in line
    ]
    assert not rows, (
        f"discord.mdx still tabulates slash commands that are never "
        f"registered: {rows}"
    )
