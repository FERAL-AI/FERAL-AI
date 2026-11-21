"""Observed Playwright selector input; no bounding-box guesses."""
import asyncio
import base64
import io
import os
from pathlib import Path

from PIL import Image
import pytest
from skills.impl.browser_use import BrowserController, CDPConnection
from tests.test_browser_view_capture import CaptureCDP


class Observer:
    def __init__(self):
        self.point = {'x': 65, 'y': 130}
        self.stopped = self.disposed = False
        self.on_stop = None

    async def evaluate(self, expression):
        if 'stop()' in expression:
            self.stopped = True
            if self.on_stop:
                self.on_stop()
            return None
        return self.point

    async def dispose(self):
        self.disposed = True


class Element:
    def __init__(self, observer):
        self.observer = observer
        self.disposed = False

    async def evaluate_handle(self, expression, phase):
        assert 'event.isTrusted' in expression
        assert 'event.composedPath().includes(element)' in expression
        assert 'window.top !== window' in expression
        return self.observer

    async def dispose(self):
        self.disposed = True


class Page:
    def __init__(self):
        self.context = self
        self.target = 'page-a'
        self.detached = False
        self.observer = Observer()
        self.element = Element(self.observer)
        self.calls = []
        self.on_action = self.on_query = None

    async def new_cdp_session(self, page):
        assert page is self
        return self

    async def send(self, method):
        assert method == 'Target.getTargetInfo'
        return {'targetInfo': {'targetId': self.target}}

    async def detach(self):
        self.detached = True

    async def query_selector(self, selector):
        if self.on_query:
            self.on_query()
        return self.element

    async def click(self, selector, timeout):
        self.calls.append(('click', selector, timeout))
        if self.on_action:
            await self.on_action()

    async def hover(self, selector, timeout):
        self.calls.append(('hover', selector, timeout))
        if self.on_action:
            await self.on_action()

    async def fill(self, selector, value, timeout):
        self.calls.append(('fill', selector, value, timeout))


def setup():
    ctrl = BrowserController()
    ctrl._cdp = CaptureCDP()
    ctrl._cdp.root['children'] = [{'nodeType': 1, 'backendNodeId': 11}]
    ctrl._attached_target_id = 'page-a'
    ctrl._page = Page()
    return ctrl, ctrl._page, ctrl._cdp


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['click', 'hover'])
async def test_observed_event_scaled_frame_and_cleanup(action):
    ctrl, page, cdp = setup()
    assert (await getattr(ctrl, action)('#button'))['success'] is True
    assert page.calls == [(action, '#button', 5000)]
    assert (await ctrl.capture_view_frame('page-a'))['cursor'] == {'x': 104, 'y': 208, 'phase': action}
    assert page.observer.stopped and page.observer.disposed and page.element.disposed and page.detached


@pytest.mark.asyncio
@pytest.mark.parametrize('point', [None, {'x': True, 'y': 3}, {'x': float('nan'), 'y': 3}])
async def test_invalid_observed_event_never_guesses_marker(point):
    ctrl, page, cdp = setup()
    page.observer.point = point
    assert (await ctrl.click('#button'))['success'] is True
    assert ctrl._view_pointer is None
    assert page.observer.stopped and page.detached


@pytest.mark.asyncio
async def test_foreign_page_preserves_action_without_marker():
    ctrl, page, cdp = setup()
    page.target = 'page-b'
    assert (await ctrl.hover('#button'))['success'] is True
    assert ctrl._view_pointer is None and page.detached and not page.observer.stopped


@pytest.mark.asyncio
async def test_observation_failure_preserves_original_dispatch(caplog):
    ctrl, page, cdp = setup()

    async def unavailable(selector):
        raise RuntimeError('private metadata sentinel')

    page.query_selector = unavailable
    assert (await ctrl.click('text=button'))['success'] is True
    assert page.calls == [('click', 'text=button', 5000)]
    assert ctrl._view_pointer is None and page.detached
    assert 'private metadata sentinel' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['document', 'document_element', 'page', 'cdp', 'target', 'fill'])
async def test_race_after_dispatch_discards_marker(change):
    ctrl, page, cdp = setup()

    async def mutate():
        if change == 'document':
            cdp.root['backendNodeId'] = 99
        elif change == 'document_element':
            cdp.root['children'][0]['backendNodeId'] = 99
        elif change == 'page':
            ctrl._page = Page()
        elif change == 'cdp':
            ctrl._cdp = CaptureCDP()
        elif change == 'target':
            ctrl._attached_target_id = 'page-b'
        else:
            assert (await ctrl.fill('#field', 'synthetic'))['success'] is True

    page.on_action = mutate
    assert (await ctrl.click('#button'))['success'] is True
    assert ctrl._view_pointer is None and page.observer.stopped and page.detached


@pytest.mark.asyncio
async def test_rebind_during_preparation_refuses_dispatch():
    ctrl, page, cdp = setup()
    page.on_query = lambda: setattr(ctrl, '_page', Page())
    assert (await ctrl.click('#button'))['success'] is False
    assert page.calls == [] and ctrl._view_pointer is None
    assert page.element.disposed and page.detached


@pytest.mark.asyncio
async def test_cleanup_await_supersession_discards_marker():
    ctrl, page, cdp = setup()
    page.observer.on_stop = lambda: setattr(ctrl, '_page', Page())
    assert (await ctrl.hover('#button'))['success'] is True
    assert ctrl._view_pointer is None


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_failed_cancelled_actions_clear_old_marker_and_listener(cancel):
    ctrl, page, cdp = setup()
    ctrl._view_pointer = {'old': True}

    async def fail():
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError('synthetic failure')

    page.on_action = fail
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await ctrl.click('#button')
    else:
        assert (await ctrl.click('#button'))['success'] is False
    assert ctrl._view_pointer is None and page.observer.stopped and page.observer.disposed and page.detached


@pytest.mark.asyncio
async def test_fill_and_external_navigation_retire_marker():
    ctrl, page, cdp = setup()
    assert (await ctrl.click('#button'))['success'] is True
    assert ctrl._view_pointer is not None
    assert (await ctrl.fill('#field', 'synthetic'))['success'] is True
    assert ctrl._view_pointer is None
    assert (await ctrl.click('#button'))['success'] is True
    cdp.root['backendNodeId'] = 99
    assert (await ctrl.capture_view_frame('page-a'))['cursor'] is None
    assert ctrl._view_pointer is None


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('FERAL_BROWSER_VIEW_REAL') != '1', reason='explicit disposable Chrome execution')
async def test_real_selector_coordinates_scroll_layout_move_and_frame(tmp_path):
    import socket
    from playwright.async_api import async_playwright

    executable = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    assert executable.is_file(), 'Installed Chrome required; never download'
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    assert port != 9222
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(tmp_path / 'owned-chrome'), executable_path=str(executable), headless=True,
            args=[f'--remote-debugging-port={port}', '--disable-background-networking',
                  '--disable-component-update', '--disable-sync', '--disable-extensions',
                  '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'],
            viewport={'width': 800, 'height': 600},
        )
        ctrl = BrowserController()
        ctrl._cdp = CDPConnection(host='127.0.0.1', port=port)
        try:
            page = context.pages[0]
            await page.set_content('''<style>body{margin:0;height:1800px;background:white}
                #button{position:absolute;left:100px;top:1200px;width:160px;height:80px;
                background:blue;border:0}#field{position:fixed;left:20px;top:20px}</style>
                <input id=field><button id=button>synthetic</button>
                <script>window.events=[];for(const type of ['click','mousemove'])
                document.addEventListener(type,e=>{if(e.target.id==='button')
                events.push({type,x:e.clientX,y:e.clientY,trusted:e.isTrusted})},true);
                button.onclick=()=>button.style.background='rgb(0,220,0)';</script>''')
            session = await context.new_cdp_session(page)
            target = (await session.send('Target.getTargetInfo'))['targetInfo']['targetId']
            await session.detach()
            assert await ctrl._cdp.connect(target_id=target)
            ctrl._attached_target_id = target
            ctrl._page = page
            original_query = page.query_selector

            async def moving_query(selector):
                element = await original_query(selector)
                original_observe = element.evaluate_handle

                async def observe_then_move(expression, phase):
                    observer = await original_observe(expression, phase)
                    await page.evaluate("button.style.left='310px'")
                    return observer

                element.evaluate_handle = observe_then_move
                return element

            page.query_selector = moving_query
            assert (await ctrl.click('#button'))['success'] is True
            assert await page.evaluate('scrollY') > 0
            event = await page.evaluate("events.filter(e=>e.type==='click').at(-1)")
            assert event['trusted'] is True and event['x'] >= 310
            assert ctrl._view_pointer['x'] == event['x'] and ctrl._view_pointer['y'] == event['y']
            frame = await ctrl.capture_view_frame(target)
            assert frame['success'] is True and frame['cursor']['phase'] == 'click'
            scale = frame['width'] / frame['viewport_width']
            assert frame['cursor']['x'] == pytest.approx(event['x'] * scale, abs=0.5)
            assert frame['cursor']['y'] == pytest.approx(event['y'] * scale, abs=0.5)
            with Image.open(io.BytesIO(base64.b64decode(frame['image_b64']))) as image:
                pixel = image.getpixel((round(frame['cursor']['x'] - 50 * scale), round(frame['cursor']['y'])))
                assert pixel[1] > 180 and pixel[0] < 60 and pixel[2] < 60
            page.query_selector = original_query
            await page.mouse.move(0, 0)
            await page.evaluate("button.style.left='500px'")
            assert (await ctrl.hover('#button'))['success'] is True
            event = await page.evaluate("events.filter(e=>e.type==='mousemove').at(-1)")
            frame = await ctrl.capture_view_frame(target)
            assert frame['cursor']['phase'] == 'hover'
            assert frame['cursor']['x'] == pytest.approx(event['x'] * scale, abs=0.5)
            assert frame['cursor']['y'] == pytest.approx(event['y'] * scale, abs=0.5)
            assert (await ctrl.fill('#field', 'café 漢字 🦊'))['success'] is True
            assert (await ctrl.capture_view_frame(target))['cursor'] is None
            assert (await ctrl.click('#missing'))['success'] is False
            assert ctrl._view_pointer is None
            assert (await ctrl.click('#button'))['success'] is True
            await page.set_content('<body>new synthetic document</body>')
            assert (await ctrl.capture_view_frame(target))['cursor'] is None
            assert ctrl._view_pointer is None
        finally:
            await ctrl._cdp.disconnect()
            await context.close()
    with socket.socket() as observer:
        observer.settimeout(0.5)
        assert observer.connect_ex(('127.0.0.1', port)) != 0
