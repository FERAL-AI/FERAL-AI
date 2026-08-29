"""A human can take the floor off. It is their computer.

``SandboxPolicy._COMMAND_DENY_FLOOR`` refuses six command shapes, and it
refused them at every autonomy tier including ``loose``, with nothing in
the policy file able to remove them: ``denied_command_patterns`` let an
operator *add* rules and never drop the built-in ones.

That made ``loose`` a promise the runtime did not keep. ``loose`` is
documented as "nothing needs approval", and the tier system is meant to
be the single authority on what FERAL is allowed to do. A floor that
outranks the tier is a second authority, and an operator who chose loose
was not told about it.

The sharpest case was not a destructive one. ``sudo reboot`` sat in the
floor next to ``mkfs``: rebooting your own machine is ordinary work, it
is reversible, and FERAL refused it at every tier with no override.

``execution.full_authority: true`` is the answer. Off by default, so the
floor still stands for everybody who has not thought about it, and the
model's ability to spell ``mkfs`` for its own reasons is still contained.
Set, it means the human sat down and said which side of that line they
are on.

Policy file only. Not an environment variable: an env var is something
you can be handed by a script or a parent process, and this is something
you have to mean.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security.exec_mode import MODE_REFUSED, resolve_execution_mode  # noqa: E402
from security.sandbox_policy import SandboxPolicy  # noqa: E402

# Spelled in pieces so this file does not contain the literal command
# text. Nothing here executes anything: every assertion below asks the
# policy layer what it *would* decide.
_WIPE_ROOT = "rm" + " -rf /"
_WIPE_ROOT_FORCED = _WIPE_ROOT + " --no-preserve-root"
_FORMAT = "mkfs" + ".ext4 /dev/sda1"
_REBOOT = "sudo " + "reboot"

FLOOR_COMMANDS = [_WIPE_ROOT, _WIPE_ROOT_FORCED, _FORMAT, _REBOOT]


def _policy(path, **execution) -> SandboxPolicy:
    """A policy that reaches the floor.

    The workspace grant is load-bearing, not scenery. A bare
    ``SandboxPolicy()`` grants no workspace, so ``resolve_execution_mode``
    refuses every command with ``needs=workspace_grant`` long before the
    deny floor is consulted. A test built on one passes green while the
    floor does nothing, which is precisely the failure this file exists
    to detect. ``grant_folder`` also satisfies strict autonomy, which
    demands an explicit grant rather than a policy read path, so the same
    fixture is honest at all three tiers.
    """
    policy = SandboxPolicy.load_default()
    policy.grant_folder(str(path), mode="readwrite")
    policy._data["execution"] = {
        **policy._data.get("execution", {}),
        "allow_shell_commands": True,
        **execution,
    }
    return policy


def _refused_by_floor(command: str, policy: SandboxPolicy, cwd, tier="loose") -> bool:
    """Did the *deny floor* refuse, as opposed to some other rule?

    ``resolve_execution_mode`` refuses for several unrelated reasons.
    Matching on ``MODE_REFUSED`` alone would let this file pass while
    the floor did nothing.
    """
    decision = resolve_execution_mode(
        command, policy=policy, cwd=str(cwd), autonomy_mode=tier
    )
    return decision.mode == MODE_REFUSED and "denied pattern" in (decision.reason or "")


# ----------------------------------------------------------------------
# Default: the floor stands
# ----------------------------------------------------------------------

def test_full_authority_is_off_unless_asked_for():
    assert SandboxPolicy().full_authority() is False


@pytest.mark.parametrize("command", FLOOR_COMMANDS)
@pytest.mark.parametrize("tier", ["strict", "hybrid", "loose"])
def test_by_default_the_floor_holds_at_every_tier(command, tier, tmp_path):
    """Unchanged behaviour for anyone who has not set the key.

    ``loose`` is the one that matters: it is the tier that means
    "nothing needs approval", and the floor outranks it.
    """
    assert _refused_by_floor(command, _policy(tmp_path), tmp_path, tier)


def test_an_ordinary_command_is_not_refused_by_the_default_floor(tmp_path):
    """The fixture reaches the floor rather than failing earlier.

    Without this, every assertion above would still pass if the policy
    refused everything for an unrelated reason.
    """
    decision = resolve_execution_mode(
        "ls -la", policy=_policy(tmp_path), cwd=str(tmp_path), autonomy_mode="loose"
    )
    assert decision.mode != MODE_REFUSED, decision.reason


# ----------------------------------------------------------------------
# Granted: the floor gets out of the way
# ----------------------------------------------------------------------

@pytest.mark.parametrize("command", FLOOR_COMMANDS)
@pytest.mark.parametrize("tier", ["strict", "hybrid", "loose"])
def test_a_granted_human_is_not_refused_by_the_floor(command, tier, tmp_path):
    assert not _refused_by_floor(
        command, _policy(tmp_path, full_authority=True), tmp_path, tier
    )


def test_the_floor_is_empty_under_a_grant(tmp_path):
    """Directly, so this does not depend on how exec_mode composes."""
    assert _policy(tmp_path, full_authority=True).denied_command_patterns() == []


def test_the_operators_own_rules_survive_the_grant(tmp_path):
    """Two separate decisions, both of them the operator's.

    Someone who granted full authority and then wrote their own deny
    list meant both things. Dropping their rules along with the floor
    would be the runtime overriding them a second time.
    """
    policy = _policy(
        tmp_path,
        full_authority=True,
        denied_command_patterns=[r"terraform\s+destroy"],
    )
    assert _refused_by_floor("terraform destroy", policy, tmp_path)
    assert not _refused_by_floor(_WIPE_ROOT, policy, tmp_path)


# ----------------------------------------------------------------------
# The shape of the switch
# ----------------------------------------------------------------------

def test_the_grant_is_not_readable_from_the_environment():
    """An env var can be handed to you. This has to be meant.

    Checked against the source rather than by setting candidate names,
    which would only cover the names someone thought to guess, and would
    leave them behind for the suite's env-leak guard to report.

    If a future change wires this to the environment, that should be a
    decision made on purpose, and it should fail here first.
    """
    names = set(SandboxPolicy.full_authority.__code__.co_names)
    assert not names & {"environ", "getenv"}, (
        f"full_authority reads the environment ({names}); it is meant to "
        "require an explicit edit to the policy file"
    )


@pytest.mark.parametrize("value", [False, None, 0, ""])
def test_only_a_real_yes_counts(value, tmp_path):
    """A key present but falsey is not a grant."""
    assert _policy(tmp_path, full_authority=value).full_authority() is False
    assert _policy(tmp_path, full_authority=value).denied_command_patterns()


# ----------------------------------------------------------------------
# The other three refusals, which are the point of the key
# ----------------------------------------------------------------------
#
# Lifting the deny floor alone would leave a key that does not mean what
# it is called. Under loose autonomy with a granted workspace, three
# things still refused, and none of them was a prompt the operator could
# answer -- they were hard noes:
#
#   generated code without Docker   needs=docker
#   a cwd outside every grant       needs=workspace_grant
#   a path argument outside policy  needs=workspace_grant
#
# The first is the sharpest. An operator on a machine with no Docker
# could not run generated code at any autonomy tier by any means.

# Outside the default read_paths (~/.feral/, /tmp/feral/) and NOT on
# blocked_paths, so it isolates "outside the allow list" from "explicitly
# carved out". A world-readable system file, read only.
_OUTSIDE_POLICY = "cat /etc/hosts"

# On the default blocked_paths, which is a different rule that survives
# the key. See test_credential_directories_stay_blocked.
_BLOCKED_PATH = "cat ~/.ssh/" + "id_rsa"


def _decide(policy, command="echo hi", cwd=None, tier="loose", **kw):
    return resolve_execution_mode(
        command, policy=policy, cwd=str(cwd) if cwd else None,
        autonomy_mode=tier, **kw,
    )


@pytest.mark.parametrize("kwargs", [
    {"skill_id": "code_interpreter"},
    {"requires_sandbox": True},
])
def test_generated_code_still_needs_docker_by_default(kwargs, tmp_path):
    d = _decide(_policy(tmp_path), cwd=tmp_path, docker_available=False, **kwargs)
    assert d.mode == MODE_REFUSED
    assert d.needs == "docker"


@pytest.mark.parametrize("kwargs", [
    {"skill_id": "code_interpreter"},
    {"requires_sandbox": True},
])
def test_generated_code_runs_on_the_host_under_full_authority(kwargs, tmp_path):
    """The absence of Docker stops being a reason to refuse."""
    d = _decide(
        _policy(tmp_path, full_authority=True),
        cwd=tmp_path, docker_available=False, **kwargs
    )
    assert d.mode != MODE_REFUSED, d.reason


def test_docker_is_still_preferred_when_it_is_there(tmp_path):
    """The key removes a refusal, it does not remove the sandbox.

    Running generated code in Docker when Docker is available has no
    downside for the operator, so full_authority should not drag it onto
    the host.
    """
    d = _decide(
        _policy(tmp_path, full_authority=True),
        cwd=tmp_path, skill_id="code_interpreter", docker_available=True,
    )
    assert d.mode == "docker"


def test_a_cwd_outside_every_grant_is_refused_by_default(tmp_path):
    outside = tmp_path.parent / "not-granted"
    outside.mkdir(exist_ok=True)
    d = _decide(_policy(tmp_path), cwd=outside)
    assert d.mode == MODE_REFUSED
    assert d.needs == "workspace_grant"


def test_full_authority_makes_the_whole_machine_the_workspace(tmp_path):
    outside = tmp_path.parent / "not-granted"
    outside.mkdir(exist_ok=True)
    d = _decide(_policy(tmp_path, full_authority=True), cwd=outside)
    assert d.mode != MODE_REFUSED, d.reason
    assert d.workspace is not None, "workspace left as None for downstream callers"


def test_strict_accepts_full_authority_as_the_explicit_act_it_wants(tmp_path):
    """Strict demands the operator named the folder. This names all of them.

    Strict exists to reject a workspace *inherited* from a policy read
    path. A key written into the policy file by hand is not inherited.
    """
    outside = tmp_path.parent / "not-granted"
    outside.mkdir(exist_ok=True)
    assert _decide(_policy(tmp_path), cwd=outside, tier="strict").mode == MODE_REFUSED
    d = _decide(_policy(tmp_path, full_authority=True), cwd=outside, tier="strict")
    assert d.mode != MODE_REFUSED, d.reason


def test_a_path_argument_outside_policy_is_refused_by_default(tmp_path):
    d = _decide(_policy(tmp_path), command=_OUTSIDE_POLICY, cwd=tmp_path)
    assert d.mode == MODE_REFUSED
    assert d.needs == "workspace_grant"


def test_full_authority_reaches_paths_outside_the_policy(tmp_path):
    d = _decide(
        _policy(tmp_path, full_authority=True), command=_OUTSIDE_POLICY, cwd=tmp_path
    )
    assert d.mode != MODE_REFUSED, d.reason


@pytest.mark.parametrize("full_authority", [False, True])
def test_the_shell_and_the_file_tools_agree_either_way(full_authority, tmp_path):
    """Both sides move together. That is what the path check guarantees.

    ``exec_mode`` step 6 exists so a path the file tools refuse is not
    reachable by naming it on a command line. The invariant is that the
    two *agree*, not that either is restrictive, so lifting the shell
    without lifting ``can_read_path`` would break it in the other
    direction: ``cat`` would reach what ``read_file`` could not.

    An earlier version of this test asserted only that the shell
    followed the key, which passed while the file tools still refused.
    """
    policy = _policy(tmp_path, full_authority=full_authority)

    file_tools_ok = policy.can_read_path("/etc/hosts")
    shell_ok = _decide(policy, command=_OUTSIDE_POLICY, cwd=tmp_path).mode != MODE_REFUSED

    assert file_tools_ok is full_authority
    assert shell_ok == file_tools_ok, (
        f"shell says {shell_ok}, file tools say {file_tools_ok}; the two "
        "must answer the same question the same way"
    )


@pytest.mark.parametrize("directory", ["~/.ssh/", "~/.aws/", "~/.gnupg/"])
def test_credential_directories_stay_blocked(directory):
    """The default blocked_paths outlive the key, and they are the
    credential stores.

    This is the property that makes the key defensible rather than
    reckless: an operator who grants full authority still does not hand
    the model their SSH keys, AWS credentials or GPG keyring, because
    those are carved out by the shipped policy rather than by the floor.
    """
    policy = SandboxPolicy.load_default()
    policy._data["execution"] = {
        **policy._data.get("execution", {}),
        "allow_shell_commands": True,
        "full_authority": True,
    }
    assert directory in policy._data["filesystem"]["blocked_paths"], (
        "the shipped policy no longer blocks this directory; that is a "
        "separate change and this test is the wrong place to make it"
    )
    target = Path(directory).expanduser() / "credential"
    assert policy.can_read_path(str(target)) is False


def test_an_operators_own_blocked_path_survives_the_key(tmp_path):
    """``blocked_paths`` is the one scope that still carves things out.

    It is the operator's own list rather than part of the floor. Someone
    who wrote both it and this key meant both, and overriding it would
    leave them no way to say "everything except this".
    """
    secret = tmp_path / "secrets"
    secret.mkdir()
    policy = _policy(tmp_path, full_authority=True)
    policy._data["filesystem"] = {
        **policy._data.get("filesystem", {}),
        "blocked_paths": [str(secret)],
    }

    assert policy.can_read_path(str(secret / "x.txt")) is False
    assert _decide(policy, cwd=secret).mode == MODE_REFUSED


class TestTheKeyIsNotReachableOverTheApi:
    """The key must not be settable by anything the model can drive.

    ``full_authority`` lifts the four boundaries that contain a prompt
    injection. ``POST /api/policy/update`` takes a whole policy document
    and persists it, so without this it *would* set the key, and injected
    text reaching that endpoint (a ``curl`` under loose autonomy is
    enough) could grant itself the authority those boundaries withhold.
    That would make the key an escalation path rather than a choice.

    It is documented as settable only by editing the policy file by
    hand. These pin that rather than trusting the sentence.
    """

    @staticmethod
    def _client(policy, monkeypatch):
        """Install ``policy`` as the live one, restored at teardown.

        ``monkeypatch`` is not decoration. A bare assignment to
        ``state.policy`` is a process-global that outlives the test: it
        left every later test running against a policy whose only read
        path was one tmp_path and no network config, which broke two
        unrelated domain-gating tests in the same run and passed in
        isolation.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.routes import security_and_hardware as mod

        app = FastAPI()
        app.include_router(mod.router)
        monkeypatch.setattr(mod.state, "policy", policy, raising=False)
        return TestClient(app, raise_server_exceptions=False)

    def _document(self, tmp_path, full_authority):
        return {
            "version": "1.0",
            "filesystem": {
                "read_paths": [str(tmp_path)], "write_paths": [], "blocked_paths": [],
            },
            "execution": {
                "allow_shell_commands": True, "full_authority": full_authority,
            },
        }

    def test_the_api_cannot_turn_it_on(self, tmp_path, monkeypatch):
        policy = _policy(tmp_path)
        assert policy.full_authority() is False

        resp = self._client(policy, monkeypatch).post(
            "/api/policy/update", json=self._document(tmp_path, True)
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["field"] == "execution.full_authority"

    def test_a_refused_request_does_not_change_the_live_policy(self, tmp_path, monkeypatch):
        """The refusal must land before the swap, not after."""
        from api.routes import security_and_hardware as mod

        policy = _policy(tmp_path)
        self._client(policy, monkeypatch).post(
            "/api/policy/update", json=self._document(tmp_path, True)
        )
        assert mod.state.policy.full_authority() is False

    def test_the_api_can_still_turn_it_off(self, tmp_path, monkeypatch):
        """Authority is given up over the API and taken at the keyboard."""
        from api.routes import security_and_hardware as mod

        granted = _policy(tmp_path, full_authority=True)
        assert granted.full_authority() is True
        monkeypatch.setattr(
            "security.sandbox_policy.SandboxPolicy.save", lambda self, path=None: None
        )

        resp = self._client(granted, monkeypatch).post(
            "/api/policy/update", json=self._document(tmp_path, False)
        )

        assert resp.status_code == 200, resp.text
        assert mod.state.policy.full_authority() is False

    def test_the_generic_config_setter_cannot_reach_the_policy_file(self):
        """``/api/config/update`` writes settings.json, a different store."""
        import inspect

        source = inspect.getsource(SandboxPolicy.load_default)
        assert "policies" in source
        assert "settings.json" not in source


def test_granting_authority_is_announced(tmp_path, caplog):
    """A machine with the floor off should say so where it is findable."""
    import logging

    policy = _policy(tmp_path, full_authority=True)
    with caplog.at_level(logging.WARNING):
        policy.denied_command_patterns()
    assert any(
        "full_authority" in r.message and "OFF" in r.message
        for r in caplog.records
    ), "no warning names the state the machine is in"


def test_the_announcement_does_not_repeat_per_command(tmp_path, caplog):
    """Standing state, logged once. A line on every call trains skipping."""
    import logging

    policy = _policy(tmp_path, full_authority=True)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            policy.denied_command_patterns()
    warnings = [r for r in caplog.records if "full_authority" in r.message]
    assert len(warnings) == 1, f"logged {len(warnings)} times, expected once"
