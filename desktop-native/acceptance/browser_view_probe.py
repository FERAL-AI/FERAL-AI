"""Explicit real-Chrome, local-page browser-view acceptance.

No actions occur on import. The default is read-only preflight. --run-chrome
starts only the supplied, hash-bound Chrome binary with a fresh owned profile
and dynamic debugging port. This tests real CDP/controller + registered viewing
routes, not native GUI, model inference, application authentication or ToolRunner
authorization. The isolated route's only injected dependency is its browser
state holder; controller, manager and route handlers are actual production code.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "feral-core"
SOURCE_FILES = (
    "feral-core/skills/impl/browser_use.py",
    "feral-core/api/browser_view.py",
    "feral-core/api/routes/marketplace_browser.py",
    "desktop-native/acceptance/browser_view_probe.py",
)
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
TOTAL_DEADLINE = 90
HTML = b"""<!doctype html><meta charset=utf-8><title>Local browser fixture</title>
<style>html,body{margin:0;background:white}input{position:absolute;width:260px;
height:48px;padding:0;border:0;font:24px sans-serif;background:#aaffaa}
#wrong{left:20px;top:20px}#right{left:20px;top:80px}
#password{left:20px;top:140px;background:#ff00ff}
iframe{position:absolute;left:20px;top:220px;width:260px;height:60px;border:0}
</style><input id=wrong aria-label="Wrong field"><input id=right aria-label="Right field">
<input id=password type=password value="synthetic-password-only">
<iframe srcdoc="<body style='margin:0;background:orange'>synthetic iframe</body>"></iframe>
<script>document.getElementById('wrong').focus()</script>"""


class ProbeRefusal(Exception):
    """Bounded public failure code; no upstream text in receipts."""


def require(condition, code):
    if not condition:
        raise ProbeRefusal(code)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def source_identity(expected_source):
    require(re.fullmatch(r"[0-9a-f]{40}", expected_source) is not None,
            "full_committed_source_required")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, env=env,
                            capture_output=True, timeout=10, check=True)
    require(result.stdout.decode().strip() == expected_source, "source_revision_changed")
    hashes = {}
    for name in SOURCE_FILES:
        path = REPO / name
        require(path.is_file() and not path.is_symlink(), "source_missing_or_redirected")
        committed = subprocess.run(["git", "show", expected_source + ":" + name],
                                   cwd=REPO, env=env, capture_output=True, timeout=10)
        require(committed.returncode == 0, "source_not_in_commit")
        hashes[name] = digest(path)
        require(hashlib.sha256(committed.stdout).hexdigest() == hashes[name],
                "source_not_committed")
    return hashes


def preflight(args):
    chrome = Path(args.chrome)
    require(chrome.is_absolute() and chrome.is_file() and not chrome.is_symlink()
            and os.access(chrome, os.X_OK), "chrome_executable_invalid")
    require(chrome.name in {"Google Chrome", "Chromium"}, "chrome_binary_name_refused")
    require(re.fullmatch(r"[0-9a-f]{64}", args.expected_chrome_sha256) is not None,
            "exact_chrome_hash_required")
    require(digest(chrome) == args.expected_chrome_sha256, "chrome_binary_changed")
    require(shutil.disk_usage("/private/tmp").free >= 1024**3, "disk_headroom_low")
    hashes = source_identity(args.expected_source)
    return {"source": args.expected_source, "source_sha256": hashes,
            "chrome_sha256": args.expected_chrome_sha256,
            "scope": "Real CDP-only controller and registered local viewing routes; no native GUI/auth/model/action-authority claim"}


async def command(controller, method, params=None):
    return await asyncio.wait_for(controller._cdp.send_command(method, params or {}), 7)


async def evaluate(controller, expression):
    result = await command(controller, "Runtime.evaluate",
                           {"expression": expression, "returnByValue": True})
    require(not result.get("exceptionDetails"), "fixture_evaluation_failed")
    return result.get("result", {}).get("value")


def validate_frame(frame, target, password_box):
    from PIL import Image

    require(frame.get("success") is True and frame.get("target_id") == target,
            "frame_target_or_success_mismatch")
    require(frame.get("format") == "jpeg" and frame.get("masked_password_fields") is True,
            "frame_mask_contract_invalid")
    require(isinstance(frame.get("image_b64"), str)
            and len(frame["image_b64"]) <= MAX_FRAME_BYTES, "frame_budget_exceeded")
    raw = base64.b64decode(frame["image_b64"], validate=True)
    require(len(raw) <= MAX_FRAME_BYTES, "decoded_frame_budget_exceeded")
    with Image.open(io.BytesIO(raw)) as original:
        image = original.convert("RGB")
        require(original.format == "JPEG" and image.width == frame["width"]
                and image.height == frame["height"] and image.width * image.height <= 4 * 1024**2,
                "frame_dimensions_invalid")
        x = round(password_box["x"] * image.width / frame["viewport_width"])
        y = round(password_box["y"] * image.height / frame["viewport_height"])
        region = image.crop((x - 4, y - 4, x + 5, y + 5))
        require(all(min(pixel) >= 230 for pixel in region.getdata()),
                "password_region_not_masked")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "width": frame["width"], "height": frame["height"],
            "target_hash": hashlib.sha256(target.encode()).hexdigest()}


async def actual_acceptance(args, root, receipt):
    from aiohttp import web
    import httpx

    checks = []
    receipt["checks"] = checks
    receipt["phase"] = "local_page_start"
    clock = [time.monotonic()]
    proc = None
    controller = None
    runner = None
    log = None
    port = None
    try:
        app = web.Application()

        async def page(request):
            require(request.path == "/fixture", "unexpected_fixture_request")
            return web.Response(body=HTML, content_type="text/html")

        app.router.add_get("/fixture", page)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sockets = site._server.sockets
        require(len(sockets) == 1, "fixture_server_socket_ambiguous")
        page_port = sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{page_port}/fixture"
        profile = root / "chrome-profile"
        profile.mkdir(mode=0o700)
        log = (root / "chrome.log").open("wb")
        proc = subprocess.Popen([
            args.chrome, "--headless=new", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0", "--user-data-dir=" + str(profile),
            "--window-size=960,720", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update",
            "--disable-sync", "--disable-extensions", "--disable-default-apps",
            "--disable-features=MediaRouter,OptimizationHints", url,
        ], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        receipt["owned_chrome_pid"] = proc.pid
        receipt["phase"] = "owned_chrome_ready"
        active_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while not active_file.is_file():
            require(proc.poll() is None, "owned_chrome_exited_before_ready")
            require(time.monotonic() < deadline, "owned_chrome_ready_timeout")
            await asyncio.sleep(0.05)
        require(not active_file.is_symlink() and active_file.stat().st_size < 1024,
                "debug_endpoint_file_invalid")
        lines = active_file.read_text().splitlines()
        require(len(lines) == 2 and lines[0].isdigit(), "debug_endpoint_invalid")
        port = int(lines[0])
        require(1024 <= port <= 65535 and port != 9222, "personal_debug_port_refused")
        os.environ.update(FERAL_HOME=str(root / "feral-home"),
                          FERAL_DATA_HOME=str(root / "feral-home"),
                          FERAL_CDP_HOST="127.0.0.1", FERAL_CDP_PORT=str(port),
                          PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
                          PYTHONDONTWRITEBYTECODE="1")
        (root / "feral-home").mkdir(mode=0o700)
        sys.path.insert(0, str(CORE))
        from skills.impl.browser_use import BrowserController, CDPConnection
        from api.browser_view import BrowserViewManager

        async with httpx.AsyncClient(trust_env=False) as client:
            targets = None
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                response = await client.get(f"http://127.0.0.1:{port}/json", timeout=3)
                require(len(response.content) <= 64 * 1024, "debug_inventory_over_budget")
                targets = [entry for entry in response.json()
                           if entry.get("type") == "page" and entry.get("url") == url]
                if len(targets) == 1:
                    break
                await asyncio.sleep(0.05)
            require(targets is not None and len(targets) == 1, "owned_page_target_missing")
        target = targets[0]["id"]
        receipt["phase"] = "cdp_connect"
        controller = BrowserController()
        controller._cdp = CDPConnection(host="127.0.0.1", port=port)
        require(await asyncio.wait_for(controller._cdp.connect(prefer_page=True, target_id=target), 8),
                "owned_cdp_connection_failed")
        controller._attached_target_id = target
        for domain in ("Runtime.enable", "Page.enable", "DOM.enable"):
            await command(controller, domain)
        require(await evaluate(controller, "location.href") == url, "owned_page_identity_mismatch")
        checks.append("real_owned_cdp_target")

        receipt["phase"] = "exact_field_typing"
        selector_text = "selector café 漢字 🦊"
        ref_text = "ref naïve العربية 🦊"
        await evaluate(controller, "right.value='old-selector-value';document.getElementById('wrong').focus();true")
        result = await controller.type_text("#right", selector_text)
        require(result.get("success") is True, "selector_typing_failed")
        fields = await evaluate(controller, "({wrong:wrong.value,right:right.value,active:document.activeElement.id})")
        require(fields == {"wrong": "", "right": selector_text, "active": "right"},
                "selector_typed_wrong_field")
        await evaluate(controller, "right.value='old-ref-value';wrong.focus();true")
        require((await controller.snapshot()).get("success") is True, "actual_ax_snapshot_failed")
        refs = [ref for ref, info in controller._aria_refs.items() if info.get("selector") == "#right"]
        require(len(refs) == 1, "actual_ax_field_ref_ambiguous")
        require((await controller.type_text(refs[0], ref_text)).get("success") is True,
                "ref_typing_failed")
        require(await evaluate(controller, "({wrong:wrong.value,right:right.value,active:document.activeElement.id})")
                == {"wrong": "", "right": ref_text, "active": "right"}, "ref_typed_wrong_field")
        require((await controller.type_text("ax999999", "must-not-appear")).get("success") is False,
                "unknown_ref_was_dispatched")
        require((await controller.type_text("input", "must-not-appear")).get("success") is False,
                "ambiguous_selector_was_dispatched")
        require(await evaluate(controller, "({wrong:wrong.value,right:right.value})")
                == {"wrong": "", "right": ref_text},
                "refusal_mutated_fields")
        checks.extend(["selector_focus_exact", "ax_ref_focus_exact", "missing_and_ambiguous_refusal"])

        receipt["phase"] = "mask_pixel_control"
        password_box = await evaluate(controller, "(()=>{const r=password.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()")
        viewport = await evaluate(controller, "({width:innerWidth,height:innerHeight})")
        baseline = await command(controller, "Page.captureScreenshot", {
            "format": "jpeg", "captureBeyondViewport": False, "fromSurface": True,
            "clip": {"x": 0, "y": 0, "width": viewport["width"],
                     "height": viewport["height"], "scale": 1},
        })
        from PIL import Image
        with Image.open(io.BytesIO(base64.b64decode(baseline["data"], validate=True))) as image:
            pixel = image.convert("RGB").getpixel((round(password_box["x"]*image.width/viewport["width"]),
                                                   round(password_box["y"]*image.height/viewport["height"])))
            require(pixel[0] >= 180 and pixel[1] <= 70 and pixel[2] >= 180,
                    "synthetic_unmasked_password_control_missing")

        # Only the route's browser holder is isolated. No mocked CDP, screenshot,
        # manager, token check or FastAPI handler, and no full brain initialization.
        require("api.state" not in sys.modules, "unexpected_brain_state_already_imported")
        holder = types.ModuleType("api.state")
        holder.state = types.SimpleNamespace(browser=controller)
        sys.modules["api.state"] = holder
        from fastapi import FastAPI
        from api.routes import marketplace_browser as routes
        receipt["phase"] = "registered_view_routes"
        manager = BrowserViewManager(lambda: controller, clock=lambda: clock[0])
        routes.browser_views = manager
        api = FastAPI()
        api.include_router(routes.router)
        transport = httpx.ASGITransport(app=api, client=("127.0.0.1", 41234))
        headers = {"X-FERAL-Browser-View": "native-v1"}
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture.local",
                                     headers=headers, trust_env=False) as client:
            advertised = await client.get("/api/browser/view/targets")
            require(advertised.status_code == 200 and advertised.json()["active_target_id"] == target,
                    "actual_route_target_missing")
            require(advertised.json()["targets"] == [{"id": target, "label": "Connected browser tab"}],
                    "route_disclosed_private_page_metadata")
            denied = await client.post("/api/browser/view/start", json={"target_id": target, "consent": False})
            require(denied.status_code == 400, "view_without_consent_admitted")
            started = await client.post("/api/browser/view/start", json={"target_id": target, "consent": True})
            require(started.status_code == 200, "actual_route_view_start_failed")
            lease = {key: started.json()[key] for key in ("view_id", "token")}
            invalid = {**lease, "token": "A" * 43}
            if invalid["token"] == lease["token"]:
                invalid["token"] = "B" * 43
            require((await client.post("/api/browser/view/frame", json=invalid)).status_code == 403,
                    "foreign_view_token_admitted")
            response = await client.post("/api/browser/view/frame", json=lease)
            require(response.status_code == 200 and response.headers.get("cache-control") == "no-store",
                    "actual_route_frame_failed_or_cacheable")
            frame = response.json()
            receipt["masked_frame"] = validate_frame(frame, target, password_box)
            require(await evaluate(controller, "getComputedStyle(password).visibility==='visible'&&document.querySelectorAll('style[id^=feral-view-mask-]').length===0") is True,
                    "nonce_mask_cleanup_failed")
            require((await client.post("/api/browser/view/frame", json=lease)).status_code == 429,
                    "view_rate_limit_missing")
            clock[0] += 1
            require((await controller.hover("#right")).get("success") is True, "actual_pointer_hover_failed")
            moved = await client.post("/api/browser/view/frame", json=lease)
            cursor = moved.json().get("cursor")
            require(moved.status_code == 200 and isinstance(cursor, dict) and cursor.get("phase") == "hover",
                    "actual_pointer_identity_missing")
            validate_frame(moved.json(), target, password_box)
            require((await client.post("/api/browser/view/stop", json=lease)).status_code == 200,
                    "exact_view_stop_failed")
            require((await client.post("/api/browser/view/frame", json=lease)).status_code == 403,
                    "stopped_view_reused")
            missing_header = await client.get("/api/browser/view/targets", headers={"X-FERAL-Browser-View": ""})
            require(missing_header.status_code == 403, "csrf_header_fence_missing")
            proxied = await client.get("/api/browser/view/targets", headers={"X-Forwarded-For": "127.0.0.1"})
            require(proxied.status_code == 403, "proxied_operator_admitted")
            webpage = await client.get("/api/browser/view/targets", headers={"Origin": "http://localhost:5173"})
            require(webpage.status_code == 403, "browser_origin_admitted")
            expiring = await client.post("/api/browser/view/start", json={"target_id": target, "consent": True})
            expiring_lease = {key: expiring.json()[key] for key in ("view_id", "token")}
            clock[0] += manager.ttl + 1
            require((await client.post("/api/browser/view/frame", json=expiring_lease)).status_code == 410,
                    "expired_view_admitted")
            changed = await client.post("/api/browser/view/start", json={"target_id": target, "consent": True})
            changed_lease = {key: changed.json()[key] for key in ("view_id", "token")}
            new_page = await command(controller, "Target.createTarget", {"url": "about:blank"})
            require(isinstance(new_page.get("targetId"), str), "actual_second_target_missing")
            attached = await controller.attach_to_tab(new_page["targetId"])
            require(attached.get("success") is True, "actual_target_change_failed")
            require((await client.post("/api/browser/view/frame", json=changed_lease)).status_code == 410,
                    "changed_target_view_admitted")
            require((await controller.attach_to_tab(target)).get("success") is True,
                    "original_target_reattach_failed")
            require((await controller.capture_view_frame("foreign_fixture_target")).get("success") is False,
                    "foreign_capture_target_admitted")
        remote = httpx.ASGITransport(app=api, client=("198.51.100.1", 41234))
        async with httpx.AsyncClient(transport=remote, base_url="http://fixture.local", headers=headers) as client:
            require((await client.get("/api/browser/view/targets")).status_code == 403,
                    "remote_viewer_admitted")
        checks.extend(["actual_password_mask_pixels", "actual_pointer_pixels_bound",
                       "registered_readonly_routes", "consent_token_stop_expiry_fences",
                       "loopback_csrf_proxy_origin_fences", "foreign_target_refusal"])
        receipt["phase"] = "assertions_complete"
    finally:
        cleanup_errors = []
        if controller is not None:
            try:
                await asyncio.wait_for(controller.close(), 8)
            except Exception as error:
                cleanup_errors.append("controller_close_" + type(error).__name__)
        if proc is not None:
            try:
                if proc.poll() is None:
                    require(os.getpgid(proc.pid) == proc.pid, "owned_chrome_process_group_changed")
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        await asyncio.to_thread(proc.wait, timeout=8)
                    except subprocess.TimeoutExpired:
                        require(os.getpgid(proc.pid) == proc.pid, "owned_chrome_process_group_changed")
                        os.killpg(proc.pid, signal.SIGKILL)
                        await asyncio.to_thread(proc.wait, timeout=5)
                        receipt["forced_owned_kill"] = True
                receipt["owned_chrome_exit_code"] = proc.returncode
            except Exception as error:
                cleanup_errors.append("chrome_stop_" + type(error).__name__)
        if runner is not None:
            try:
                await asyncio.wait_for(runner.cleanup(), 5)
            except Exception as error:
                cleanup_errors.append("local_server_stop_" + type(error).__name__)
        if log is not None:
            log.close()
            if (root / "chrome.log").stat().st_size > MAX_LOG_BYTES:
                cleanup_errors.append("chrome_log_budget_exceeded")
        if port is not None:
            with socket.socket() as observer:
                observer.settimeout(0.5)
                receipt["owned_debug_listener_closed"] = observer.connect_ex(("127.0.0.1", port)) != 0
            if not receipt["owned_debug_listener_closed"]:
                cleanup_errors.append("owned_debug_listener_remains")
        count = total = 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
            for name in files:
                path = Path(directory) / name
                count += 1
                if not path.is_symlink():
                    total += path.stat().st_size
        receipt["artifact_files"] = count
        receipt["artifact_bytes"] = total
        if count > 20_000 or total > 256 * 1024**2:
            cleanup_errors.append("owned_artifact_budget_exceeded")
        receipt["cleanup_errors"] = cleanup_errors
        require(not cleanup_errors, "owned_cleanup_unconfirmed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--expected-chrome-sha256", required=True)
    parser.add_argument("--run-chrome", action="store_true", help="Explicitly launch isolated real Chrome; requires OS execution permission")
    args = parser.parse_args()
    receipt = preflight(args)
    if not args.run_chrome:
        print(json.dumps({**receipt, "status": "preflight_only", "runtime_started": False}, sort_keys=True))
        return 0
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="feral-browser-view-acceptance-", dir="/private/tmp"))
    receipt.update(root=str(root), status="running", headless=True, forced_owned_kill=False)
    started = time.monotonic()
    try:
        asyncio.run(asyncio.wait_for(actual_acceptance(args, root, receipt), TOTAL_DEADLINE))
        require(source_identity(args.expected_source) == receipt["source_sha256"], "source_changed_during_acceptance")
        receipt["status"] = "passed"
    except Exception as error:
        receipt.update(status="failed", error_code=str(error) if isinstance(error, ProbeRefusal) else type(error).__name__)
    finally:
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (root / "receipt.json").write_text(json.dumps(receipt, sort_keys=True, indent=2))
    print(json.dumps({key: receipt[key] for key in ("status", "root", "source", "elapsed_seconds")}, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
