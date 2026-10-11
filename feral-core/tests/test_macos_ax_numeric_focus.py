"""Inert actual AX paths: numeric writes and fresh app observation."""
from types import SimpleNamespace

import pytest

from skills.impl import macos_ax as ax


@pytest.fixture
def incrementor(monkeypatch):
    element = object()
    state = {'current': 0.0, 'reads': [], 'writes': [], 'actions': [], 'secure': False, 'role': 'AXIncrementor'}
    node = ax.AXNode(ref='', role='AXIncrementor', subrole='', label='Synthetic month',
                     label_source='AXTitle', depth=1, actions=['AXIncrement', 'AXDecrement'])
    store = ax._RefStore()
    ref = store.add(element, node, 'Synthetic Chrome', 4321)
    monkeypatch.setattr(ax, '_REFS', store)
    monkeypatch.setattr(ax, '_role', lambda unused: state['role'])
    monkeypatch.setattr(ax, '_subrole', lambda unused: 'AXSecureTextField' if state['secure'] else '')
    monkeypatch.setattr(ax, 'time', SimpleNamespace(sleep=lambda unused: None))

    def attribute(unused, name):
        state['reads'].append(name)
        assert not state['secure'] or name != 'AXValue'
        return (0, state['current']) if name == 'AXValue' else (-25205, None)

    def write(unused, name, value):
        state['writes'].append(value)
        # A settable native numeric field rejects NSString writes.
        if type(value) is not type(state['current']):
            return -25201
        state['current'] = value
        return 0

    monkeypatch.setattr(ax, '_attr', attribute)
    monkeypatch.setattr(ax, '_ax', lambda: SimpleNamespace(
        AXUIElementIsAttributeSettable=lambda element, name, unused: (0, True),
        AXUIElementSetAttributeValue=write,
    ))
    monkeypatch.setattr(ax, '_actions', lambda unused: ['AXIncrement', 'AXDecrement'])
    monkeypatch.setattr(ax.MacOSAccessibilitySkill, '_perform', lambda self, element, action: state['actions'].append(action) or 0)
    return SimpleNamespace(state=state, ref=ref, skill=ax.MacOSAccessibilitySkill())


@pytest.mark.parametrize('current,requested_text,expected', [(0.0, '11', 11.0), (0, '2026', 2026), (1.0, '7.5', 7.5)])
def test_observed_native_numeric_incrementor_writes_numeric_values(incrementor, current, requested_text, expected):
    incrementor.state['current'] = current
    result = incrementor.skill._set_value({'ref': incrementor.ref, 'value': requested_text})
    assert result['success'] and result['data']['verified'] is True
    assert incrementor.state['writes'] == [expected]
    assert type(incrementor.state['writes'][0]) is type(current)
    assert result['data']['value'] == requested_text


@pytest.mark.parametrize('value', ['nan', 'inf', '1e999', 'not-a-number', ''])
def test_numeric_invalid_input_does_not_dispatch_a_write(incrementor, value):
    result = incrementor.skill._set_value({'ref': incrementor.ref, 'value': value})
    assert result['success'] is False and result['status_code'] == 422
    assert incrementor.state['writes'] == []


def test_native_string_value_does_not_receive_numeric_coercion(incrementor):
    incrementor.state['current'] = 'old string'
    result = incrementor.skill._set_value({'ref': incrementor.ref, 'value': '11'})
    assert result['success'] and result['data']['verified']
    assert incrementor.state['writes'] == ['11']


@pytest.mark.parametrize('value', ['7.5', '9223372036854775808'])
def test_integer_incrementor_refuses_fraction_or_out_of_range_value(incrementor, value):
    incrementor.state['current'] = 0
    result = incrementor.skill._set_value({'ref': incrementor.ref, 'value': value})
    assert result['status_code'] == 422 and incrementor.state['writes'] == []


def test_whole_date_field_does_not_guess_a_native_date_type(incrementor):
    incrementor.state['role'] = 'AXDateField'
    ax._REFS._entries[incrementor.ref].role = 'AXDateField'
    incrementor.skill._set_value({'ref': incrementor.ref, 'value': '2026-11-07'})
    assert incrementor.state['writes'] == ['2026-11-07']


def test_secure_and_stale_refs_refuse_before_value_read_or_write(incrementor):
    incrementor.state['secure'] = True
    assert incrementor.skill._set_value({'ref': incrementor.ref, 'value': '11'})['status_code'] == 403
    assert incrementor.state['reads'] == [] and incrementor.state['writes'] == []
    incrementor.state['secure'] = False
    incrementor.state['role'] = 'AXButton'
    assert incrementor.skill._set_value({'ref': incrementor.ref, 'value': '11'})['status_code'] == 409
    assert incrementor.state['reads'] == [] and incrementor.state['writes'] == []


def test_increment_decrement_are_already_explicit_supported_actions(incrementor):
    for action in ['AXIncrement', 'AXDecrement']:
        assert incrementor.skill._perform_action({'ref': incrementor.ref, 'action': action})['success']
    assert incrementor.state['actions'] == ['AXIncrement', 'AXDecrement']
    assert incrementor.skill._perform_action({'ref': incrementor.ref, 'action': 'AXPress'})['status_code'] == 422


def test_numeric_readback_rewrite_is_not_verified(incrementor, monkeypatch):
    def rewrite(element, name, value):
        incrementor.state['writes'].append(value)
        incrementor.state['current'] = 12.0
        return 0
    monkeypatch.setattr(ax, '_ax', lambda: SimpleNamespace(
        AXUIElementIsAttributeSettable=lambda element, name, unused: (0, True), AXUIElementSetAttributeValue=rewrite))
    result = incrementor.skill._set_value({'ref': incrementor.ref, 'value': '11'})
    assert result['success'] and result['data']['verified'] is False


def test_text_readback_cannot_verify_a_numeric_write(incrementor, monkeypatch):
    def rewrite(element, name, value):
        incrementor.state['writes'].append(value)
        incrementor.state['current'] = '11'
        return 0
    monkeypatch.setattr(ax, '_ax', lambda: SimpleNamespace(
        AXUIElementIsAttributeSettable=lambda element, name, unused: (0, True),
        AXUIElementSetAttributeValue=rewrite,
    ))
    result = incrementor.skill._set_value({'ref': incrementor.ref, 'value': '11'})
    assert result['success'] and result['data']['verified'] is False


def test_fresh_frontmost_probe_resolves_exact_ax_pid(monkeypatch):
    focused = object()
    reads = []
    monkeypatch.setattr(ax, '_ax', lambda: SimpleNamespace(
        AXIsProcessTrusted=lambda: True,
        AXUIElementCreateSystemWide=lambda: object(),
        AXUIElementCopyAttributeValue=lambda element, name, unused: reads.append(name) or (0, focused),
        AXUIElementGetPid=lambda element, unused: (0, 4321) if element is focused else (-25200, None),
    ))
    monkeypatch.setattr(ax, '_app_for_pid', lambda pid: ax._App('Synthetic Chrome', pid, 'synthetic.chrome'))
    monkeypatch.setattr(ax, '_workspace', lambda: (_ for _ in ()).throw(AssertionError('Do not trust cached workspace frontmost')))
    observed = ax._frontmost_app()
    assert observed and observed.pid == 4321 and observed.name == 'Synthetic Chrome'
    assert reads == ['AXFocusedApplication']


@pytest.mark.parametrize('trusted,error,pid', [(False, 0, 4321), (True, -25211, 4321), (True, 0, 0), (True, 0, True)])
def test_unavailable_foreground_probe_never_falls_back_to_cached_app(monkeypatch, trusted, error, pid):
    reads = []
    monkeypatch.setattr(ax, '_ax', lambda: SimpleNamespace(
        AXIsProcessTrusted=lambda: trusted,
        AXUIElementCreateSystemWide=lambda: object(),
        AXUIElementCopyAttributeValue=lambda element, name, unused: reads.append(name) or (0, object()),
        AXUIElementGetPid=lambda element, unused: (error, pid),
    ))
    monkeypatch.setattr(ax, '_workspace', lambda: (_ for _ in ()).throw(AssertionError('No cached fallback')))
    assert ax._frontmost_app() is None
    if not trusted:
        assert reads == []


def test_check_permission_marks_unknown_foreground_truthfully(monkeypatch):
    monkeypatch.setattr(ax, 'accessibility_trusted', lambda: True)
    monkeypatch.setattr(ax, '_running_apps', lambda: [])
    monkeypatch.setattr(ax, '_frontmost_app', lambda: None)
    result = ax.MacOSAccessibilitySkill()._check_permission({})
    assert result['success'] and result['data']['frontmost_app'] == ''
    assert result['data']['frontmost_observed'] is False and result['data']['frontmost_pid'] is None
