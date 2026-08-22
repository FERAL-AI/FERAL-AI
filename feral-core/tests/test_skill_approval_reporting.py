"""Approve, reject and list have to report what actually happened.

Same defect class as the hot-reload one in ``test_skill_hot_reload.py``,
three more instances of it, all in ``api/routes/skills.py``:

* ``POST /api/skills/approve`` answered a False from
  ``SkillGenerator.approve_skill`` with HTTP 200 and ``{"ok": false,
  "skill_id": ..., "registered": false}``. No ``error`` key. The v2
  ``apiFetch`` raises on a non-2xx status, or on a 2xx body carrying
  ``error``; that shape trips neither, so ``pages/Forge.jsx`` awaited the
  call, never read the body, refreshed and moved on. A draft that was
  never registered rendered as promoted.

* ``POST /api/skills/reject`` returned an unconditional ``{"ok": true}``
  without looking at what ``reject_skill`` returned, so rejecting an id
  the brain had never heard of reported success for a no-op.

* Both dereferenced ``state.skill_gen`` without a guard, so a brain
  running without a skill generator answered an AttributeError as a 500
  traceback. ``GET /skills`` did the same with ``state.skill_registry``,
  and the Skills page rendered that as "No skills loaded / Check the
  Brain boot log", which sends the operator to debug a boot that was fine.

The decision this file pins: rejecting an id that is not in the approval
queue is an **error** (409, ``code: not_pending``), not an idempotent
no-op. The queue lives in ``SkillGenerator._pending_skills``, in memory,
and a restart empties it, so the common failure is a Forge tab left open
across a restart. Under the idempotent reading that tab answers every
click with a green tick while nothing is discarded. See the route
docstring for the full reasoning.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


class FakeGen:
    """The parts of ``SkillGenerator`` these routes touch.

    Mirrors the real semantics, including the one that matters most:
    ``approve_skill`` pops the draft off the queue **before** it tries to
    register it, so a registration failure loses the draft.
    """

    def __init__(self, pending=(), approve_ok=True, approve_raises=None,
                 reject_returns="bool"):
        self.pending = {sid: {"skill_id": sid, "name": sid} for sid in pending}
        self.approve_ok = approve_ok
        self.approve_raises = approve_raises
        self.reject_returns = reject_returns

    async def approve_skill(self, skill_id: str) -> bool:
        if self.approve_raises:
            raise self.approve_raises
        manifest = self.pending.pop(skill_id, None)
        if not manifest:
            return False
        return self.approve_ok

    def reject_skill(self, skill_id: str):
        removed = self.pending.pop(skill_id, None)
        if self.reject_returns == "bool":
            return removed is not None
        if self.reject_returns == "none":
            return None
        return object()  # a non-bool, non-None answer

    def get_pending_skills(self) -> list[dict]:
        return list(self.pending.values())


@contextmanager
def _client(*, skill_gen=None, skill_registry=None):
    mock = MagicMock()
    mock.skill_gen = skill_gen
    mock.skill_registry = skill_registry
    mock.skill_executor = None
    with patch("api.state.state", mock), patch("api.routes.skills.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def make_client():
    """Client factory whose ``state`` patches stay live for the whole test.

    ``_client`` is a context manager and the patches it installs end when
    it closes, so the client has to be built inside a scope that outlives
    the request. An ExitStack owned by the fixture is that scope.
    """
    with ExitStack() as stack:
        yield lambda **kw: stack.enter_context(_client(**kw))


@pytest.fixture
def approve_client():
    """One draft queued, and approving it succeeds."""
    with _client(skill_gen=FakeGen(pending=["draft_one"])) as c:
        yield c


# ── approve ──────────────────────────────────────────────────────


def test_approve_confirms_a_registration_that_happened(approve_client):
    r = approve_client.post("/api/skills/approve", json={"skill_id": "draft_one"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["registered"] is True
    assert body["skill_id"] == "draft_one"
    assert "error" not in body


def test_approve_of_an_unqueued_draft_is_not_a_success(make_client):
    """The headline case. A 200 with ``ok: false`` and no ``error`` is the
    one shape every generic client reads as fine, and it is what this
    route sent for two releases."""
    c = make_client(skill_gen=FakeGen(pending=["draft_one"]))
    r = c.post("/api/skills/approve", json={"skill_id": "never_queued"})

    assert r.status_code != 200, (
        "an approval that registered nothing answered 200; the v2 apiFetch "
        "error sniff and every other generic caller read that as success"
    )
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert body["registered"] is False
    assert body["code"] == "not_pending"
    assert body["error"], "a failure with no error key is invisible to apiFetch"
    assert "never_queued" in body["error"]


def test_approve_that_fails_after_dequeue_reports_a_brain_fault(make_client):
    """The draft was queued and the brain lost it on the way to disk.
    That reason exists only in the brain log, and 500 is what sends an
    operator to a log; 409 would tell them to go look at their queue."""
    c = make_client(skill_gen=FakeGen(pending=["draft_one"], approve_ok=False))
    r = c.post("/api/skills/approve", json={"skill_id": "draft_one"})

    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert body["registered"] is False
    assert body["code"] == "registration_failed"
    assert "draft_one" in body["error"]


def test_approve_surfaces_a_generator_that_raises(make_client):
    c = make_client(skill_gen=FakeGen(pending=["draft_one"],
                                       approve_raises=RuntimeError("disk on fire")))
    r = c.post("/api/skills/approve", json={"skill_id": "draft_one"})
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert "disk on fire" in body["error"]


def test_approve_answers_503_when_there_is_no_skill_generator(make_client):
    """Was an AttributeError rendered as a 500 traceback."""
    c = make_client(skill_gen=None)
    r = c.post("/api/skills/approve", json={"skill_id": "draft_one"})
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert "skill generator" in body["error"].lower()


def test_approve_without_a_skill_id_is_a_bad_request(make_client):
    c = make_client(skill_gen=FakeGen(pending=["draft_one"]))
    r = c.post("/api/skills/approve", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "skill_id is required"


# ── reject ───────────────────────────────────────────────────────


def test_reject_confirms_a_draft_it_actually_discarded(make_client):
    gen = FakeGen(pending=["draft_one"])
    c = make_client(skill_gen=gen)
    r = c.post("/api/skills/reject", json={"skill_id": "draft_one"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["rejected"] is True
    assert "draft_one" not in gen.pending


def test_reject_of_an_unknown_skill_is_reported_as_an_error(make_client):
    """Pins the decision: an unknown id is a 409 conflict, not a silent
    idempotent success. Flipping this to "idempotent no-op" is a
    defensible design change, but it must be a deliberate one, and this
    test is what makes it deliberate."""
    c = make_client(skill_gen=FakeGen(pending=["draft_one"]))
    r = c.post("/api/skills/reject", json={"skill_id": "no_such_draft"})

    assert r.status_code != 200, (
        "rejecting a draft the brain never had answered 200 with ok:true; "
        "that is a success reported for a no-op"
    )
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert body["rejected"] is False
    assert body["code"] == "not_pending"
    assert "no_such_draft" in body["error"]


def test_reject_falls_back_to_watching_the_queue_when_the_answer_is_not_a_bool(make_client):
    """A generator double, or the older copy bundled under ``desktop/``,
    may not return a bool. We do not read a truthy object as success: we
    compare the queue before and after and report what we observed."""
    gen = FakeGen(pending=["draft_one"], reject_returns="object")
    c = make_client(skill_gen=gen)

    ok = c.post("/api/skills/reject", json={"skill_id": "draft_one"})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True

    gone = c.post("/api/skills/reject", json={"skill_id": "draft_one"})
    assert gone.status_code == 409
    assert gone.json()["ok"] is False


def test_reject_answers_503_when_there_is_no_skill_generator(make_client):
    """Was an AttributeError rendered as a 500 traceback."""
    c = make_client(skill_gen=None)
    r = c.post("/api/skills/reject", json={"skill_id": "draft_one"})
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert "skill generator" in body["error"].lower()


def test_reject_without_a_skill_id_is_a_bad_request(make_client):
    c = make_client(skill_gen=FakeGen(pending=["draft_one"]))
    r = c.post("/api/skills/reject", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "skill_id is required"


# ── generate ─────────────────────────────────────────────────────


class FakeGenerator(FakeGen):
    def __init__(self, manifest=None, **kw):
        super().__init__(**kw)
        self.manifest = manifest

    async def generate_skill(self, capability, service=""):
        return self.manifest


def test_generate_without_a_capability_is_a_bad_request(make_client):
    c = make_client(skill_gen=FakeGenerator())
    r = c.post("/api/skills/generate", json={"service": "test"})
    assert r.status_code == 400
    assert r.json()["error"] == "capability is required"


def test_generate_answers_503_when_there_is_no_skill_generator(make_client):
    c = make_client(skill_gen=None)
    r = c.post("/api/skills/generate", json={"capability": "read my email"})
    assert r.status_code == 503
    assert r.json()["ok"] is False


def test_generate_reports_a_draft_that_was_never_produced(make_client):
    c = make_client(skill_gen=FakeGenerator(manifest=None))
    r = c.post("/api/skills/generate", json={"capability": "read my email"})
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "generation_failed"
    assert "read my email" in body["error"]


def test_generate_still_returns_the_manifest_on_success(make_client):
    manifest = {"skill_id": "email_reader", "name": "Email"}
    c = make_client(skill_gen=FakeGenerator(manifest=manifest))
    r = c.post("/api/skills/generate", json={"capability": "read my email"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["manifest"] == manifest
    assert body["needs_approval"] is True


# ── GET /skills ──────────────────────────────────────────────────


def test_list_skills_answers_503_rather_than_a_500_traceback(make_client):
    """``state.skill_registry.skills`` on a None registry was an
    AttributeError. The page that renders this then said "No skills
    loaded / Check the Brain boot log" about a boot that was fine."""
    c = make_client(skill_registry=None)
    r = c.get("/skills")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["error"]
    assert body["skills"] == []


def test_list_skills_still_lists_a_real_registry(make_client):
    from skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_builtin_skills()
    c = make_client(skill_registry=registry)
    r = c.get("/skills")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert rows
    assert {"skill_id", "name", "description", "endpoints", "trigger_phrases"} <= set(rows[0])


def test_list_skills_sends_endpoints_as_a_list_not_a_count(make_client):
    """``endpoints`` was ``len(s.endpoints)``, an integer.

    Both readers of this payload, ``pages/Skills.jsx`` and
    ``components/SkillsLauncher.jsx``, guard the endpoint chip with
    ``Array.isArray(s.endpoints) && s.endpoints.length``. An integer
    fails that guard, so the chip was dead code in two places and no
    client could show what a skill can actually do. Sending the list is
    what makes the Skills page's detail sheet possible at all.
    """
    from skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_builtin_skills()
    c = make_client(skill_registry=registry)
    rows = c.get("/skills").json()

    with_endpoints = [r for r in rows if r["endpoint_count"] > 0]
    assert with_endpoints, "no builtin skill declares an endpoint, so this proves nothing"
    for row in with_endpoints:
        assert isinstance(row["endpoints"], list)
        assert len(row["endpoints"]) == row["endpoint_count"]
        first = row["endpoints"][0]
        assert {"id", "method", "description", "read_only"} <= set(first)
        assert isinstance(first["id"], str) and first["id"]


def test_list_skills_sends_the_categories_the_icon_is_derived_from(make_client):
    """No manifest carries an icon, so the Skills page derives one.

    It derives it from ``categories``, which every shipped manifest
    declares and which this payload did not carry. Without it the page
    would have to invent an icon from nothing, and every skill would get
    the same fallback glyph. ``version`` is here for the same reason:
    the page rendered a ``v{version}`` chip against a key the payload
    never sent, so the chip never appeared.
    """
    from skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_builtin_skills()
    c = make_client(skill_registry=registry)
    rows = c.get("/skills").json()

    assert rows
    for row in rows:
        assert isinstance(row["categories"], list)
        assert isinstance(row["version"], str)
    # Not merely present: actually populated, for most of them. A payload
    # of 42 empty lists would pass a shape check and leave the grid
    # uniformly iconless.
    with_categories = [r for r in rows if r["categories"]]
    assert len(with_categories) == len(rows), (
        "every shipped manifest declares categories; a row without them means "
        "the field is being dropped somewhere between the manifest and the wire"
    )
