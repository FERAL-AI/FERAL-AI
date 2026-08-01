"""A skill archive is untrusted input and must not write outside its sandbox.

`MarketplaceClient._install_from_archive` unpacks a tar or zip downloaded
from the registry or a GitHub index. Before this guard it called
`extractall` with no member validation, which on Python 3.11 is the
CVE-2007-4559 behaviour: a member named `../../x` escapes the destination
and writes anywhere the brain process can write. Installing one hostile
skill was arbitrary file write.

These tests assert the escape fails loudly rather than being silently
dropped, because a partially-installed skill whose payload landed
somewhere unexpected is worse than a refused install.
"""
from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from skills.marketplace import MarketplaceClient, _safe_extract_zip


def _tar_with_member(name: str, body: bytes = b"pwned\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=name)
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _zip_with_member(name: str, body: bytes = b"pwned\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, body)
    return buf.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member",
    ["../escaped.txt", "../../escaped.txt", "a/../../escaped.txt"],
)
async def test_tar_traversal_member_never_lands_outside(tmp_path, member):
    client = MarketplaceClient()
    outside = tmp_path / "escaped.txt"

    result = await client._install_from_archive("evil", _tar_with_member(member))

    assert result["success"] is False
    assert not outside.exists(), f"{member!r} escaped the extract directory"


@pytest.mark.asyncio
@pytest.mark.parametrize("member", ["../escaped.txt", "../../escaped.txt"])
async def test_zip_traversal_member_never_lands_outside(tmp_path, member):
    client = MarketplaceClient()
    outside = tmp_path / "escaped.txt"

    result = await client._install_from_archive("evil", _zip_with_member(member))

    assert result["success"] is False
    assert not outside.exists(), f"{member!r} escaped the extract directory"


def test_zip_helper_refuses_traversal_by_name(tmp_path):
    data = _zip_with_member("../escaped.txt")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(ValueError, match="escapes the extract directory"):
            _safe_extract_zip(zf, str(tmp_path))


def test_zip_helper_refuses_symlink_members(tmp_path):
    """A symlink's target lives in the member body, not its name.

    So the name check cannot see it, and an unguarded extract would happily
    create a link pointing at anything on disk.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0xA1FF << 16)  # S_IFLNK | 0777
        zf.writestr(info, "/etc/passwd")

    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        with pytest.raises(ValueError, match="refusing symlink"):
            _safe_extract_zip(zf, str(tmp_path))


def test_ordinary_members_still_extract(tmp_path):
    data = _zip_with_member("pkg/manifest.json", b'{"skill_id": "ok"}')
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        _safe_extract_zip(zf, str(tmp_path))

    assert (tmp_path / "pkg" / "manifest.json").read_bytes() == b'{"skill_id": "ok"}'
