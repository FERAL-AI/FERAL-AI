"""The guard against `getattr(<MagicMock>, name, default)` never defaulting.

A MagicMock has every attribute, so the default branch of `getattr` is
dead code against one. This repo shipped that defect twice in one week:
`/api/dashboard` answered 500 for a whole release on
`float(getattr(state, "started_at", 0.0))`, and `_somatic_state_for_turn`
put a MagicMock on the wire of a chat response.

Both were repaired at the call site, which does nothing about the third
occurrence. `tests/conftest.py` now wraps any bare mock installed as the
`state` attribute of an `api.*` module so that `getattr` defaults work
again. These tests are the proof that the wrapper actually fires; a guard
nobody has seen catch anything is worse than no guard, because it is
believed.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

import api.state


def test_missing_attribute_falls_back_to_the_getattr_default():
    """The literal shape the task names: a name BrainState does not have.

    Without the guard this returns a MagicMock and the assertion fails
    with `<MagicMock id=...> != "default"`.
    """
    mock = MagicMock()
    with patch("api.state.state", mock):
        state = api.state.state
        assert getattr(state, "definitely_not_a_brainstate_field", "default") == "default"


def test_bare_access_to_a_missing_attribute_fails_at_the_line_that_did_it():
    """No default to fall back on, so the trap is loud instead of silent."""
    mock = MagicMock()
    with patch("api.state.state", mock):
        with pytest.raises(AttributeError) as excinfo:
            api.state.state.definitely_not_a_brainstate_field

    message = str(excinfo.value)
    assert "definitely_not_a_brainstate_field" in message
    # The message has to carry the explanation, or the next person to hit
    # it "fixes" it by deleting the guard.
    assert "BrainState" in message


def test_the_dashboard_uptime_regression_cannot_recur():
    """The exact `/api/dashboard` 500, reproduced.

    `started_at` is a REAL float on a real BrainState, so a check based on
    "does BrainState have this name" would wave this through. What broke
    production was the mock answering with a MagicMock where a float was
    declared, and then `time.time() - <MagicMock>` raising TypeError.
    """
    mock = MagicMock()
    with patch("api.state.state", mock):
        state = api.state.state
        started = float(getattr(state, "started_at", 0.0) or 0.0)
        # The arithmetic that raised TypeError for a whole release.
        uptime = max(0.0, time.time() - started) if started > 0 else 0.0

    assert started == 0.0
    assert uptime == 0.0


def test_the_real_dashboard_endpoint_survives_a_bare_mock_state():
    """End to end through the actual route function, not a re-enactment."""
    from api.routes import dashboard

    mock = MagicMock()
    with patch("api.state.state", mock), patch.object(dashboard, "state", mock):
        assert dashboard._uptime_seconds() == 0.0


def test_a_string_field_does_not_become_a_magicmock():
    """`/v1/session` resolved its id this way and failed Pydantic far away."""
    mock = MagicMock()
    with patch("api.state.state", mock):
        assert getattr(api.state.state, "primary_session_id", "") == ""


def test_object_valued_collaborators_are_still_mockable():
    """The guard must not take away what mocks are legitimately for.

    ~115 sites patch `api.state.state` to stand in for a collaborator.
    Refusing those would be a guard that breaks the suite rather than one
    that catches a bug, so object-valued and None-valued fields stay
    auto-vivifying.
    """
    mock = MagicMock()
    with patch("api.state.state", mock):
        orchestrator = api.state.state.orchestrator
        assert orchestrator is not None
        # Still a configurable mock all the way down.
        api.state.state.orchestrator.some_call.return_value = 7
        assert api.state.state.orchestrator.some_call() == 7


def test_an_explicitly_set_scalar_is_honoured():
    """A test that says what it wants gets exactly that, guard or no guard."""
    mock = MagicMock()
    mock.started_at = 1234.0
    with patch("api.state.state", mock):
        assert getattr(api.state.state, "started_at", 0.0) == 1234.0


def test_a_spec_mock_is_not_wrapped_because_it_needs_no_help():
    """`spec=` already raises AttributeError for names it does not have."""
    from api.state import BrainState

    mock = MagicMock(spec=BrainState)
    with patch("api.state.state", mock):
        assert getattr(api.state.state, "not_on_the_spec", "default") == "default"


def test_brain_state_mock_fixture_is_spec_enforced(brain_state_mock):
    """The offered replacement for a bare MagicMock."""
    assert getattr(brain_state_mock, "not_on_the_spec", "default") == "default"
    with pytest.raises(AttributeError):
        brain_state_mock.not_on_the_spec


def test_assertions_on_the_original_mock_still_work():
    """Wrapping must stay invisible to the test's own handle on the mock."""
    mock = MagicMock()
    with patch("api.state.state", mock):
        api.state.state.orchestrator.run("x")
    mock.orchestrator.run.assert_called_once_with("x")


def test_the_real_state_is_restored_unwrapped():
    """The proxy must not outlive the patch and leak into the session."""
    from unittest.mock import NonCallableMock

    before = api.state.state
    with patch("api.state.state", MagicMock()):
        pass
    assert api.state.state is before
    assert not isinstance(api.state.state, NonCallableMock)
