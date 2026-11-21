"""Actual focus dispatch uses inert AX/metadata fixtures, never host input."""
import copy
import sys
from types import SimpleNamespace

import pytest

from skills.impl import gui_computer_use as gui
from skills.impl import macos_ax


@pytest.fixture
def rig(monkeypatch):
    row = {'app': 'Google Chrome', 'title': 'Synthetic Reference', 'pid': 812, 'window_id': 71,
           'x': 20, 'y': 40, 'width': 300, 'height': 200, 'focused': False}
    selected = object()
    app = object()
    system = object()
    other = object()
    controls = {'rows': [dict(row)], 'windows': [selected], 'front_pid': 999, 'focused': other,
                'trusted': True, 'activate_status': 0, 'raise_status': 0, 'apply': True,
                'title': 'Synthetic Reference - Chrome - Synthetic Profile', 'bounds': {k: row[k] for k in ('x','y','width','height')},
                'after_raise': None, 'extras': {}}
    mutations = []
    def attr(element, name):
        if element is system and name == 'AXFocusedApplication':
            return 0, app if controls['front_pid'] == 812 else other
        if element is app and name == 'AXWindows':
            return 0, controls['windows']
        if element is app and name == 'AXFocusedWindow':
            return 0, controls['focused']
        if element is selected and name == 'AXTitle':
            return 0, controls['title']
        if element in controls['extras'] and name == 'AXTitle':
            return 0, controls['extras'][element]['title']
        return -25212, None
    def pid(element, unused):
        if element in controls['extras']:
            return 0, controls['extras'][element]['pid']
        return 0, 812 if element in (app, selected) else controls['front_pid']
    def activate(element, name, value):
        mutations.append(('activate', element, name, value))
        if controls['apply'] and controls['activate_status'] == 0:
            controls['front_pid'] = 812
        return controls['activate_status']
    def raise_window(element, action):
        mutations.append(('raise', element, action))
        if controls['apply'] and controls['raise_status'] == 0:
            controls['focused'] = element
        if controls['after_raise']:
            controls['after_raise']()
        return controls['raise_status']
    ax = SimpleNamespace(AXIsProcessTrusted=lambda: controls['trusted'],
        AXUIElementCreateSystemWide=lambda: system, AXUIElementCreateApplication=lambda pid: app,
        AXUIElementSetMessagingTimeout=lambda element, timeout: 0,
        AXUIElementGetPid=pid, AXUIElementSetAttributeValue=activate,
        AXUIElementPerformAction=raise_window)
    monkeypatch.setattr(macos_ax, '_ax', lambda: ax)
    monkeypatch.setattr(macos_ax, '_attr', attr)
    monkeypatch.setattr(macos_ax, '_bounds', lambda element: controls['bounds'] if element is selected
        else controls['extras'].get(element, {}).get('bounds'))
    monkeypatch.setitem(sys.modules, 'CoreFoundation', SimpleNamespace(CFEqual=lambda a,b: a is b))
    def listing():
        value = gui.WindowListing()
        value.ok, value.source, value.windows = True, 'coregraphics', copy.deepcopy(controls['rows'])
        return value
    monkeypatch.setattr(gui, 'collect_windows', listing)
    monkeypatch.setattr(gui, 'run_osascript', lambda *args, **kw: gui.OSAResult(True, '', '', 0))
    monkeypatch.setattr(gui, 'input_pause', lambda seconds: None)
    return controls, mutations, row, selected, app


def test_successful_activation_exit_is_not_foreground_verification(rig):
    controls, _, _, _, _ = rig
    controls['apply'] = False
    result = gui._focus_window_macos('Synthetic Reference')
    assert result['ok'] is False
    assert result['error_code'] == 'window_focus_unverified'


def test_real_cg_ax_title_difference_maps_unique_handle_without_suffix_guess(rig):
    controls, mutations, row, selected, app = rig
    result = gui._focus_window_macos(row['title'])
    assert result['ok'] is True and result['window_focus_verified'] is True
    assert result['pid'] == row['pid'] and result['window_id'] == row['window_id']
    assert result['ax_title'] == controls['title']
    assert ('activate', app, 'AXFrontmost', True) in mutations
    assert ('raise', selected, 'AXRaise') in mutations


@pytest.mark.parametrize('change', ['duplicate_cg_bounds', 'duplicate_ax_bounds', 'different_bounds', 'missing_pid'])
def test_ambiguous_or_unmapped_windows_are_not_activated(rig, change):
    controls, mutations, row, selected, _ = rig
    if change == 'duplicate_cg_bounds':
        controls['rows'].append({**row, 'title':'Other title', 'window_id':72})
    elif change == 'duplicate_ax_bounds':
        controls['windows'].append(selected)
    elif change == 'different_bounds':
        controls['bounds'] = {**controls['bounds'], 'x':80}
    else:
        controls['rows'][0]['pid'] = None
    result = gui._focus_window_macos(row['title'])
    assert result['ok'] is False and not mutations


def test_ambiguous_app_match_requires_specific_window(rig):
    controls, mutations, row, _, _ = rig
    controls['rows'].append({**row, 'title':'Other title', 'window_id':72, 'x':600})
    result = gui._focus_window_macos('Google Chrome')
    assert result['ok'] is False and result['error_code'] == 'window_focus_ambiguous'
    assert not mutations


@pytest.mark.parametrize('change', [{'pid':99}, {'title':'Replaced'}, {'window_id':99}])
def test_selected_window_replaced_during_raise_is_not_verified(rig, change):
    controls, _, row, _, _ = rig
    controls['after_raise'] = lambda: controls['rows'][0].update(change)
    result = gui._focus_window_macos(row['title'])
    assert result['ok'] is False and result['error_code'] == 'window_focus_unverified'


@pytest.mark.parametrize('denial', ['trust', 'activate', 'raise'])
def test_refused_input_does_not_become_verified_success(rig, denial):
    controls, mutations, row, _, _ = rig
    if denial == 'trust':
        controls['trusted'] = False
    elif denial == 'activate':
        controls['activate_status'] = -25211
    else:
        controls['raise_status'] = -25211
    result = gui._focus_window_macos(row['title'])
    assert result['ok'] is False
    if denial == 'trust':
        assert not mutations
    if denial == 'activate':
        assert all(action[0] != 'raise' for action in mutations)


def test_cancellation_after_activation_fences_raise(rig, monkeypatch):
    _, mutations, row, _, _ = rig
    def check():
        if mutations:
            raise gui.DesktopInputRevoked('inert cancellation')
    monkeypatch.setattr(gui, 'check_input', check)
    with pytest.raises(gui.DesktopInputRevoked):
        gui._focus_window_macos(row['title'])
    assert [action[0] for action in mutations] == ['activate']


def test_selected_window_remains_exact_with_multiple_chrome_windows(rig):
    controls, mutations, row, selected, _ = rig
    second = object()
    second_row = {**row, 'window_id':72, 'x':700, 'title':'Other Chrome window'}
    controls['rows'].append(second_row)
    controls['extras'][second] = {'pid':812, 'title':'Other Chrome window - Chrome - Synthetic Profile',
                                'bounds':second_row}
    controls['windows'].append(second)
    result = gui._focus_window_macos(row['title'])
    assert result['ok'] is True and result['window_id'] == 71 and result['identity_match'] == 'bounds'
    assert [effect[1] for effect in mutations if effect[0] == 'raise'] == [selected]


def test_exact_observed_title_is_usable_when_window_geometry_overlaps(rig):
    controls, _, row, _, _ = rig
    controls['title'] = row['title']
    controls['rows'].append({**row, 'window_id':72, 'title':'Different window title'})
    result = gui._focus_window_macos(row['title'])
    assert result['ok'] is True and result['identity_match'] == 'observed_title'


def test_window_list_marks_only_actual_window_not_every_frontmost_pid_window(rig):
    controls, _, row, selected, _ = rig
    controls['front_pid'], controls['focused'] = 812, selected
    rows = [dict(row), {**row, 'title':'Other title', 'window_id':72, 'x':800},
            {**row, 'app':'Other app', 'pid':943, 'window_id':93}]
    gui._annotate_window_focus(rows)
    assert [w['app_frontmost'] for w in rows] == [True, True, False]
    assert [w['focused'] for w in rows] == [True, False, False]
    assert all(w['focused_known'] and w['app_frontmost_known'] for w in rows)


def test_overlapping_cg_rows_with_different_ax_title_have_unknown_window_focus(rig):
    controls, _, row, selected, _ = rig
    controls['front_pid'], controls['focused'] = 812, selected
    rows = [dict(row), {**row, 'title':'Other title', 'window_id':72}]
    gui._annotate_window_focus(rows)
    assert all(w['app_frontmost'] and w['app_frontmost_known'] for w in rows)
    assert not any(w['focused'] or w['focused_known'] for w in rows)


def test_unavailable_foreground_probe_never_uses_cached_workspace(rig, monkeypatch):
    controls, _, row, _, _ = rig
    controls['trusted'] = False
    monkeypatch.setattr(macos_ax, '_workspace', lambda: (_ for _ in ()).throw(AssertionError('cached workspace consulted')))
    rows = [dict(row)]
    gui._annotate_window_focus(rows)
    assert rows[0]['app_frontmost_known'] is False and rows[0]['focused_known'] is False
    assert gui._frontmost_pid() is None


@pytest.mark.parametrize('pid', [None, True, 0, -1, '812'])
def test_unusable_fresh_pid_is_unknown(rig, monkeypatch, pid):
    ax = macos_ax._ax()
    monkeypatch.setattr(ax, 'AXUIElementGetPid', lambda *args: (0, pid))
    assert gui._frontmost_pid() is None


def test_unique_running_app_without_window_verifies_only_app_pid(rig, monkeypatch):
    controls, mutations, _, _, _ = rig
    controls['rows'] = []
    monkeypatch.setattr(macos_ax, '_running_apps', lambda: [SimpleNamespace(name='Google Chrome', pid=812, bundle_id='fixture.chrome')])
    result = gui._focus_window_macos('Google Chrome')
    assert result['ok'] is True and result['foreground_verified'] is True
    assert result['window_focus_verified'] is False and result['window_id'] is None
    assert result['strategy'] == 'app_name'
    assert all(effect[0] == 'activate' for effect in mutations)


@pytest.mark.parametrize('apps', [[], [SimpleNamespace(name='Same', pid=812, bundle_id='fixture.one'),
                                    SimpleNamespace(name='Same', pid=813, bundle_id='fixture.two')]])
def test_missing_or_ambiguous_running_app_never_launches_or_activates(rig, monkeypatch, apps):
    controls, mutations, _, _, _ = rig
    controls['rows'] = []
    monkeypatch.setattr(macos_ax, '_running_apps', lambda: apps)
    result = gui._focus_window_macos('Same')
    assert result['ok'] is False and not mutations


@pytest.mark.parametrize('pid_column', ['', '\t812'])
def test_applescript_rows_use_observed_pid_or_mark_focus_unknown(rig, monkeypatch, pid_column):
    controls, _, _, selected, _ = rig
    controls['front_pid'], controls['focused'] = 812, selected
    monkeypatch.setattr(gui, 'run_osascript', lambda *args, **kwargs: gui.OSAResult(True,
        'Google Chrome\tSynthetic Reference\t20\t40\t300\t200'+pid_column+'\n', '', 0))
    rows, error, _ = gui._windows_via_applescript()
    assert error is None and len(rows) == 1
    assert rows[0]['focused'] is bool(pid_column)
    assert rows[0]['app_frontmost_known'] is bool(pid_column)


async def test_real_async_focus_endpoint_returns_verified_receipt(rig, monkeypatch):
    _, _, row, _, _ = rig
    monkeypatch.setattr(gui.platform, 'system', lambda: 'Darwin')
    result = await gui.GUIComputerUseSkill().execute('window_focus', {'title':row['title']}, {})
    assert result['success'] is True and result['data']['foreground_verified'] is True
    assert result['data']['window_focus_verified'] is True and result['data']['window_id'] == row['window_id']


def test_explicit_observed_ax_title_resolves_distinct_titles_with_same_geometry(rig):
    controls, mutations, row, selected, _ = rig
    second = object()
    second_row = {**row, 'window_id':72, 'title':'Other Chrome window'}
    controls['rows'].append(second_row)
    controls['extras'][second] = {'pid':812, 'title':'Other Chrome window - Chrome - Synthetic Profile',
                                'bounds':second_row}
    controls['windows'].append(second)
    ordinary = gui._focus_window_macos(row['title'])
    assert ordinary['ok'] is False and not mutations
    explicit = gui._focus_window_macos(row['title'], controls['title'])
    assert explicit['ok'] is True and explicit['window_id'] == 71
    assert explicit['identity_match'] == 'explicit_observed_titles_and_bounds'
    assert [effect[1] for effect in mutations if effect[0] == 'raise'] == [selected]


@pytest.mark.parametrize('variant', ['wrong_exact_title', 'cg_prefix_collision', 'ax_prefix_collision', 'wrong_bounds'])
def test_explicit_ax_title_never_overrides_ambiguous_or_wrong_window(rig, variant):
    controls, mutations, row, _, _ = rig
    requested = controls['title']
    if variant == 'wrong_exact_title':
        requested = 'Unobserved title'
    elif variant == 'cg_prefix_collision':
        controls['rows'].append({**row, 'window_id':72, 'title':'Synthetic'})
    elif variant == 'ax_prefix_collision':
        extra = object()
        controls['windows'].append(extra)
        controls['extras'][extra] = {'pid':812, 'title':'Synthetic Reference hidden window', 'bounds':row}
    else:
        controls['bounds']['x'] = 999
    result = gui._focus_window_macos(row['title'], requested)
    assert result['ok'] is False and not mutations


async def test_async_optional_ax_title_reaches_actual_worker(rig, monkeypatch):
    controls, _, row, _, _ = rig
    monkeypatch.setattr(gui.platform, 'system', lambda: 'Darwin')
    result = await gui.GUIComputerUseSkill().execute('window_focus',
        {'title':row['title'], 'window_ax_title':controls['title']}, {})
    assert result['success'] is True and result['data']['identity_match'] == 'explicit_observed_titles_and_bounds'
