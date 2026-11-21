"""Scoped macOS capture uses inert metadata, command, image and scene fixtures."""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from skills.impl import gui_computer_use
from skills.impl.screen_capture import ScreenCaptureSkill


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr('skills.impl.screen_capture.platform.system', lambda: 'Darwin')
    selected = {'window_id': 71, 'pid': 812, 'app': 'Google Chrome', 'title': 'Synthetic Reference',
                'x': 20, 'y': 40, 'width': 300, 'height': 200, 'focused': False}
    windows = [dict(selected), {**selected, 'window_id': 93, 'pid': 943, 'app': 'Other', 'title': 'Foreground'}]
    calls, reads = [], []
    controls = {'windows': windows, 'error': None, 'after': None, 'capture_error': None}

    def metadata():
        reads.append(1)
        return copy.deepcopy(controls['windows']), controls['error']
    monkeypatch.setattr(gui_computer_use, '_windows_via_quartz', metadata)
    skill = ScreenCaptureSkill()
    scene = type('Scene', (), {'configured': True, 'analyze_frame': AsyncMock(return_value={'answer': 'HARBOR-27'})})()
    skill._scene = scene

    async def command(argv):
        calls.append(argv)
        if controls['capture_error']:
            return {'success': False, 'error': controls['capture_error']}
        Image.new('RGB', (300, 200), 'white').save(argv[-1])
        if controls['after']:
            controls['after']()
        return {'success': True}
    monkeypatch.setattr(skill, '_run_capture_cmd', command)
    args = {'path': str(tmp_path / 'selected.png'), 'window_id': 71, 'window_pid': 812,
            'window_title': 'Synthetic Reference', 'window_app': 'Google Chrome', 'query': 'Read the synthetic card'}
    return skill, args, calls, reads, controls, scene, selected


async def test_background_window_only_never_captures_foreground_or_activates(rig):
    skill, args, calls, reads, _, scene, selected = rig
    result = await skill.execute('capture', args, {})
    assert result['success'] is True
    assert calls[0][:-1] == ['screencapture', '-x', '-o', '-l', '71']
    assert calls[0][-1] != args['path'] and not Path(calls[0][-1]).exists()
    assert len(reads) == 2
    assert result['data']['capture_source'] == 'macos_window'
    assert result['data']['window'] == {k: selected[k] for k in ('window_id', 'pid', 'app', 'title')}
    assert result['data']['region'] is None and result['data']['analysis_text'] == 'HARBOR-27'
    assert Path(args['path']).exists()
    scene.analyze_frame.assert_awaited_once()


@pytest.mark.parametrize('variant', ['optional_app', 'explicit_empty_title', 'moved_background_window'])
async def test_identity_does_not_require_focus_or_fixed_desktop_coordinates(rig, variant):
    skill, args, calls, _, controls, _, _ = rig
    if variant == 'optional_app':
        args.pop('window_app')
    elif variant == 'explicit_empty_title':
        args['window_title'] = controls['windows'][0]['title'] = ''
    else:
        controls['after'] = lambda: controls['windows'][0].update(x=400, y=100, width=500, height=300)
    result = await skill.execute('capture', args, {})
    assert result['success'] is True and calls[0][:-1] == ['screencapture', '-x', '-o', '-l', '71']


@pytest.mark.parametrize('change', [
    {'window_id': True}, {'window_id': 0}, {'window_id': -1}, {'window_id': '71'}, {'window_id': 7.1},
    {'window_id': None}, {'window_pid': True}, {'window_pid': 0}, {'window_pid': '812'},
    {'window_title': None}, {'window_app': 5}, {'region': '0,0,300,200'},
])
async def test_invalid_selection_refuses_before_capture_or_metadata(rig, change):
    skill, args, calls, reads, _, scene, _ = rig
    args.update(change)
    result = await skill.execute('capture', args, {})
    assert result['success'] is False and result['error_code'] == 'window_capture_invalid'
    assert not calls and not reads and not Path(args['path']).exists()
    scene.analyze_frame.assert_not_awaited()


@pytest.mark.parametrize('missing', ['window_pid', 'window_title', 'window_id'])
async def test_identity_is_required_not_inferred(rig, missing):
    skill, args, calls, _, _, scene, _ = rig
    args.pop(missing)
    result = await skill.execute('capture', args, {})
    assert result['success'] is False and not calls
    scene.analyze_frame.assert_not_awaited()


@pytest.mark.parametrize('change', [{'pid': 999}, {'title': 'Replaced'}, {'app': 'Other'}])
@pytest.mark.parametrize('after_capture', [False, True])
async def test_stale_or_recycled_window_does_not_return_or_analyze_pixels(rig, change, after_capture):
    skill, args, calls, _, controls, scene, _ = rig
    path = Path(args['path'])
    path.write_bytes(b'preexisting-user-output')
    if after_capture:
        controls['after'] = lambda: controls['windows'][0].update(change)
    else:
        controls['windows'][0].update(change)
    result = await skill.execute('capture', args, {})
    assert result['success'] is False and result['data'] is None
    assert result['error_code'] == 'window_capture_stale'
    assert len(calls) == int(after_capture)
    assert path.read_bytes() == b'preexisting-user-output'
    if calls:
        assert not Path(calls[0][-1]).exists()
    scene.analyze_frame.assert_not_awaited()


@pytest.mark.parametrize('state', ['missing', 'duplicate', 'unavailable'])
async def test_metadata_unavailable_or_ambiguous_has_no_desktop_fallback(rig, state):
    skill, args, calls, _, controls, scene, selected = rig
    if state == 'missing':
        controls['windows'] = []
    elif state == 'duplicate':
        controls['windows'] = [selected, selected]
    else:
        controls['error'] = 'private-window-canary'
    result = await skill.execute('capture', args, {})
    assert result['success'] is False and result['data'] is None and not calls
    assert 'private-window-canary' not in str(result)
    scene.analyze_frame.assert_not_awaited()


async def test_linux_window_request_refuses_but_ordinary_call_stays_compatible(rig, monkeypatch):
    skill, args, calls, reads, _, _, _ = rig
    monkeypatch.setattr('skills.impl.screen_capture.platform.system', lambda: 'Linux')
    result = await skill.execute('capture', args, {})
    assert result['success'] is False and result['error_code'] == 'window_capture_unsupported'
    assert not calls and not reads
    original = AsyncMock(return_value={'success': False, 'error': 'inert Linux capture'})
    monkeypatch.setattr(skill, '_capture_linux', original)
    ordinary = await skill.execute('capture_screen', {'path': args['path'], 'region': '1,2,3,4'}, {})
    assert ordinary['success'] is False
    original.assert_awaited_once()


async def test_default_and_region_calls_keep_original_argv(rig):
    skill, args, calls, reads, _, _, _ = rig
    for region in ('', '1,2,300,200'):
        result = await skill.execute('capture', {'path': args['path'], 'region': region}, {})
        assert result['success'] is True
        assert calls[-1] == ['screencapture', '-x', *([f'-R{region}'] if region else []), args['path']]
    assert not reads


async def test_command_failure_has_no_capture_fallback_or_provider_call(rig):
    skill, args, calls, _, controls, scene, _ = rig
    controls['capture_error'] = 'private-command-canary'
    result = await skill.execute('capture', args, {})
    assert result['success'] is False and result['data'] is None
    assert result['error_code'] == 'window_capture_failed' and len(calls) == 1
    assert 'private-command-canary' not in str(result)
    assert not Path(calls[0][-1]).exists() and not Path(args['path']).exists()
    scene.analyze_frame.assert_not_awaited()


@pytest.mark.parametrize('output', [None, b'not-an-image'])
async def test_successful_cli_without_valid_image_refuses_stale_output(rig, monkeypatch, output):
    skill, args, _, _, _, scene, _ = rig
    path = Path(args['path'])
    path.write_bytes(b'preexisting-user-output')
    staging = []
    async def command(argv):
        staging.append(argv[-1])
        if output is not None:
            Path(argv[-1]).write_bytes(output)
        return {'success': True}
    monkeypatch.setattr(skill, '_run_capture_cmd', command)
    result = await skill.execute('capture', args, {})
    assert result['success'] is False and result['error_code'] == 'window_capture_invalid_image'
    assert result['data'] is None and path.read_bytes() == b'preexisting-user-output'
    assert not Path(staging[0]).exists()
    scene.analyze_frame.assert_not_awaited()


@pytest.mark.parametrize('state', ['missing', 'duplicate', 'unavailable'])
async def test_window_disappearing_during_capture_does_not_publish(rig, state):
    skill, args, calls, _, controls, scene, selected = rig
    def change():
        if state == 'missing':
            controls['windows'] = []
        elif state == 'duplicate':
            controls['windows'] = [selected, selected]
        else:
            controls['error'] = 'private-metadata-canary'
    controls['after'] = change
    result = await skill.execute('capture', args, {})
    assert result['success'] is False and result['data'] is None
    assert len(calls) == 1 and not Path(calls[0][-1]).exists()
    assert not Path(args['path']).exists() and 'private-metadata-canary' not in str(result)
    scene.analyze_frame.assert_not_awaited()


async def test_metadata_timeout_refuses_before_command(rig, monkeypatch):
    skill, args, calls, _, _, scene, _ = rig
    async def timeout(awaitable, *, timeout):
        awaitable.close()
        raise asyncio.TimeoutError()
    monkeypatch.setattr('skills.impl.screen_capture.asyncio.wait_for', timeout)
    result = await skill.execute('capture', args, {})
    assert result['success'] is False and result['error_code'] == 'window_capture_metadata_timeout'
    assert result['status_code'] == 504 and result['data'] is None and not calls
    scene.analyze_frame.assert_not_awaited()


async def test_cancelled_capture_cleans_private_staging_without_analysis(rig, monkeypatch):
    skill, args, _, _, _, scene, _ = rig
    entered, release = asyncio.Event(), asyncio.Event()
    staging = []
    async def command(argv):
        staging.append(argv[-1])
        Image.new('RGB', (2, 2), 'white').save(argv[-1])
        entered.set()
        await release.wait()
        return {'success': True}
    monkeypatch.setattr(skill, '_run_capture_cmd', command)
    task = asyncio.create_task(skill.execute('capture', args, {}))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not Path(staging[0]).exists() and not Path(args['path']).exists()
    scene.analyze_frame.assert_not_awaited()


def test_manifest_exposes_exact_window_selection_and_global_scope_warning():
    manifest = json.loads((Path(__file__).parents[1] / 'skills/manifests/screen_capture.json').read_text())
    endpoint = manifest['endpoints'][0]
    params = {p['name']: p for p in endpoint['params']}
    assert params['window_id']['type'] == params['window_pid']['type'] == 'integer'
    assert 'window_title' in params and 'window_app' in params
    assert 'foreground' in endpoint['description'] and 'window_list' in endpoint['description']
