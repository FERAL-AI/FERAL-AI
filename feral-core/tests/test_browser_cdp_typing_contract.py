"""Selector-bound CDP typing must focus, verify and insert into that field."""
import pytest

from skills.impl.browser_use import BrowserController


class TypingCDP:
    connected = True
    target_id = "page-a"

    def __init__(self, *, focused=True, changed=False):
        self.focused = focused
        self.changed = changed
        self.calls = []

    async def send_command(self, method, params=None, timeout=30):
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": self.focused}}
        if method == "Input.insertText" and self.changed:
            raise RuntimeError("target closed")
        return {}


def controller(cdp):
    ctrl = BrowserController()
    ctrl._cdp = cdp
    ctrl._attached_target_id = "page-a"
    return ctrl


@pytest.mark.asyncio
async def test_ref_typing_focuses_exact_resolved_field_then_inserts_unicode():
    cdp = TypingCDP()
    ctrl = controller(cdp)
    ctrl._aria_refs["ax2"] = {"selector": "#second"}
    result = await ctrl.type_text("ax2", "café 🦦")
    assert result["success"] is True
    assert cdp.calls[0][0] == "Runtime.evaluate"
    assert "#second" in cdp.calls[0][1]["expression"]
    assert "activeElement" in cdp.calls[0][1]["expression"]
    assert ("Input.insertText", {"text": "café 🦦"}) in cdp.calls
    assert "selectionEnd" in cdp.calls[0][1]["expression"]
    assert "value===" in cdp.calls[-1][1]["expression"]
    assert all(method != "Input.dispatchKeyEvent" for method, _ in cdp.calls)


@pytest.mark.asyncio
async def test_wrong_focus_or_absent_field_never_enters_text():
    cdp = TypingCDP(focused=False)
    result = await controller(cdp).type_text("#missing", "do not enter")
    assert result["success"] is False
    assert all(method != "Input.insertText" for method, _ in cdp.calls)


@pytest.mark.asyncio
async def test_unresolved_ref_refuses_without_typing_into_current_focus():
    cdp = TypingCDP()
    result = await controller(cdp).type_text("ax404", "do not enter")
    assert result["success"] is False
    assert not cdp.calls


@pytest.mark.asyncio
async def test_field_focus_replacement_never_dispatches_text():
    cdp = TypingCDP()
    ctrl = controller(cdp)
    original = cdp.send_command

    async def change(method, params=None, timeout=30):
        result = await original(method, params, timeout)
        ctrl._attached_target_id = "page-b"
        return result

    cdp.send_command = change
    result = await ctrl.type_text("#first", "do not enter")
    assert result["success"] is False
    assert all(method != "Input.insertText" for method, _ in cdp.calls)


@pytest.mark.asyncio
async def test_insert_failure_is_not_success():
    cdp = TypingCDP(changed=True)
    result = await controller(cdp).type_text("#first", "value")
    assert result["success"] is False


@pytest.mark.asyncio
async def test_postcondition_failure_reports_uncertain_no_automatic_second_insert():
    cdp = TypingCDP()
    original = cdp.send_command
    evaluations = 0

    async def fail_after(method, params=None, timeout=30):
        nonlocal evaluations
        result = await original(method, params, timeout)
        if method == "Runtime.evaluate":
            evaluations += 1
            if evaluations == 2:
                return {"result": {"value": False}}
        return result

    cdp.send_command = fail_after
    result = await controller(cdp).type_text("#first", "value")
    assert result["success"] is False and "unconfirmed" in result["error"]
    assert sum(method == "Input.insertText" for method, _ in cdp.calls) == 1


@pytest.mark.asyncio
async def test_cdp_exception_never_echoes_private_value():
    cdp = TypingCDP()

    async def fail(method, params=None, timeout=30):
        raise RuntimeError("PRIVATE-TYPED-VALUE")

    cdp.send_command = fail
    result = await controller(cdp).type_text("#first", "PRIVATE-TYPED-VALUE")
    assert result["success"] is False
    assert "PRIVATE-TYPED-VALUE" not in result["error"]
