"""One actual FERAL browser task against a disposable localhost application.

Default invocation is read-only preflight. --run opens one synthetic page in
regular Chrome, starts the real source backend with an isolated FERAL_HOME,
attaches that exact page through the registered consent/selection endpoints,
and submits one tracked chat turn. Credentials stay in process memory. No
message, purchase, personal profile copy or personal runtime restart occurs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "feral-core"
SOURCES = (
    "feral-core/agents/orchestrator.py", "feral-core/agents/tool_runner.py",
    "feral-core/agents/tool_list.py", "feral-core/agents/llm_provider.py",
    "feral-core/skills/executor.py", "feral-core/skills/impl/browser_use.py",
    "feral-core/skills/impl/existing_chrome.py", "feral-core/api/server.py",
    "feral-core/api/existing_chrome_connection.py", "feral-core/api/job_chrome_grants.py",
    "feral-core/perception/scene.py", "feral-core/skills/impl/screen_capture.py",
    "feral-core/voice/realtime_proxy.py", "feral-core/voice/tool_result_envelope.py",
)
EXPECTED = {
    "project": "Harbor weekend", "city": "Lisbon", "departure": "2026-11-07",
    "budget": "450", "code": "HARBOR-27",
    "items": [{"name": "Museum", "quantity": 2}, {"name": "Tram pass", "quantity": 3}],
    "subject": "Harbor weekend plan",
    "body": "Museum x2; Tram pass x3; total 55 EUR.",
}
HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>FERAL local trip desk</title>
<style>body{font:18px system-ui;margin:35px;max-width:900px}button,input,select,textarea{font:inherit;margin:7px;padding:9px}label{display:block}nav{margin-bottom:20px}.panel[hidden]{display:none}.error{color:#b00020}.row{display:flex;gap:10px}textarea{width:90%;height:120px}#result{padding:20px;background:#e6ffe6}</style></head><body>
<h1>Local trip desk</h1><p>This synthetic application only saves a local draft. It never sends email or makes purchases.</p>
<nav><button type="button" onclick="tab('trip')">Trip details</button><button type="button" onclick="tab('reference');fetch('/reference-visited',{method:'POST'})">Reference</button><button type="button" onclick="tab('draft')">Compose draft</button></nav>
<section class="panel" id="trip"><h2>Trip details</h2><label>Project name<input id="project" aria-label="Project name"></label>
<label>City<select id="city" aria-label="City"><option value="">Choose city</option><option>Lisbon</option><option>Porto</option></select></label>
<label>Departure date<input id="departure" type="date" aria-label="Departure date"></label>
<label>Budget in EUR<input id="budget" type="number" aria-label="Budget in EUR"></label>
<label>Reference code<input id="code" aria-label="Reference code"></label>
<button type="button" id="validate" onclick="validateTrip()">Validate trip</button><p id="error" role="alert" class="error"></p>
<h3>Activities</h3><div id="items"></div><button type="button" onclick="addRow()">Add activity</button>
<button type="button" onclick="saveTrip()">Save itinerary</button><p id="trip-status" role="status"></p></section>
<section class="panel" id="reference" hidden><h2>Reference</h2><p>Reference code is shown in this visual card:</p><canvas id="visual-code" width="400" height="90" aria-label="Visual reference card"></canvas><p>Museum: 20 EUR per ticket. Tram pass: 5 EUR per pass.</p></section>
<section class="panel" id="draft" hidden><h2>Compose local draft</h2><label>Subject<input id="subject" aria-label="Subject"></label><label>Message body<textarea id="body" aria-label="Message body"></textarea></label><button type="button" onclick="saveDraft()">Save draft locally</button><div id="result" role="status">No draft saved</div></section>
<script>
let savedTrip=null;let rowCount=0;
const card=document.getElementById('visual-code').getContext('2d');card.fillStyle='#143c31';card.fillRect(0,0,400,90);card.fillStyle='#fff';card.font='bold 32px monospace';card.fillText('HARBOR-27',30,58);
function tab(name){document.querySelectorAll('.panel').forEach(p=>p.hidden=p.id!==name)}
function value(name){return document.getElementById(name).value}
function addRow(){rowCount++;let d=document.createElement('div');d.className='row';d.innerHTML=`<label>Activity ${rowCount}<input aria-label="Activity ${rowCount}" class="activity"></label><label>Quantity ${rowCount}<input type="number" min="1" aria-label="Quantity ${rowCount}" class="quantity" value="1"></label>`;document.getElementById('items').append(d)}
async function validateTrip(){let ok=value('code')==='HARBOR-27';document.getElementById('error').textContent=ok?'':'Reference code is required. Read the Reference tab.';await fetch('/validation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({valid:ok})});return ok}
async function saveTrip(){if(!await validateTrip())return;let data={project:value('project'),city:value('city'),departure:value('departure'),budget:value('budget'),code:value('code'),items:[...document.querySelectorAll('.row')].map(d=>({name:d.querySelector('.activity').value,quantity:Number(d.querySelector('.quantity').value)}))};let r=await fetch('/itinerary',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(r.ok){savedTrip=data;document.getElementById('trip-status').textContent='Itinerary saved locally'}else document.getElementById('error').textContent='Complete all trip details and both activities.'}
async function saveDraft(){let r=await fetch('/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subject:value('subject'),body:value('body')})});document.getElementById('result').textContent=r.ok?'Draft saved locally. Nothing sent.':'Save the itinerary and complete the draft first.'}
</script></body></html>"""


class Refusal(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Refusal(code)


def identity(expected):
    top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=REPO).decode().strip()
    require(top == str(REPO), "repository_root_changed")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip()
    require(head == expected, "source_head_changed")
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in SOURCES}


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def credential(vault_path, root):
    # Existing native coordinator factory authenticates ciphertext without
    # migration, key creation or persistence. The mandatory read audit is kept
    # in this acceptance directory, never in the personal runtime.
    sys.path.insert(0, str(CORE))
    os.environ["FERAL_AUDIT_LOG_PATH"] = str(root / "credential-read-audit.jsonl")
    from security.vault_coordinator import readonly_vault_factory
    from security.vault_keys import get_active_key
    vault = readonly_vault_factory(vault_path)()
    key = get_active_key("openai", vault=vault)
    require(isinstance(key, str) and bool(key.strip()), "selected_openai_credential_unavailable")
    return key


async def fixture(root):
    from aiohttp import web
    state = {"validation_errors": 0, "reference_visits": 0, "itinerary": None, "draft": None}
    app = web.Application(client_max_size=16 * 1024)

    async def page(request):
        return web.Response(text=HTML, content_type="text/html")

    async def change(request):
        name = request.match_info["name"]
        data = await request.json() if name != "reference-visited" else None
        if name == "validation":
            state["validation_errors"] += int(data.get("valid") is False)
        elif name == "reference-visited":
            state["reference_visits"] += 1
        elif name == "itinerary":
            if data != {key: value for key, value in EXPECTED.items() if key not in {"subject", "body"}}:
                return web.json_response({"saved": False}, status=422)
            state["itinerary"] = data
        elif name == "draft":
            if state["itinerary"] is None or data != {key: EXPECTED[key] for key in ("subject", "body")}:
                return web.json_response({"saved": False}, status=422)
            state["draft"] = data
        (root / "fixture-state.json").write_text(json.dumps(state, indent=2))
        return web.json_response({"saved": True})

    app.router.add_get("/fixture", page)
    app.router.add_post("/{name:validation|reference-visited|itinerary|draft}", change)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/fixture", state


def backend_settings(root):
    home = root / "feral-home"
    home.mkdir(mode=0o700)
    settings = {
        "llm": {"provider": "openai", "model": "gpt-5.6-sol", "base_url": "https://api.openai.com/v1", "fallback_providers": [], "max_tokens": 2048},
        "vision": {"enabled": False, "provider": "openai", "model": "gpt-5.6-sol", "base_url": "https://api.openai.com/v1"},
        "features": {"vision": False, "multi_agent": False, "proactive": False, "self_learning": False},
        "agents": {"max_tool_iterations": 24, "tool_loop_max_seconds": 480},
        "security": {"autonomy_mode": "loose"},
        "memory": {"sync": {"enabled": False}},
        "cost": {"enabled": True, "global_per_hour_usd": 2.0, "global_per_day_usd": 2.0},
    }
    (home / "settings.json").write_text(json.dumps(settings, indent=2))
    from security.sandbox_policy import SandboxPolicy
    policy = SandboxPolicy._default_policy()
    allowed = {"browser", "self_introspection", "feral_workflows"}
    skills = {json.loads(path.read_text())["skill_id"] for path in (CORE / "skills/manifests").glob("*.json")}
    policy["skills"]["blocked_skill_ids"] = sorted(skills - allowed)
    policy["skills"]["allow_generation"] = False
    policy["network"]["allowed_domains"] = ["127.0.0.1", "localhost", "api.openai.com"]
    (home / "policies").mkdir()
    (home / "policies/default.json").write_text(json.dumps(policy))
    return home


async def request(client, base, path, body):
    response = await client.post(base + path, json=body, timeout=25)
    require(response.status_code == 200, "http_contract_refused_" + str(response.status_code))
    data = response.json()
    require(data.get("success") is True, data.get("error_code", "http_contract_unconfirmed"))
    return data


async def actual(args, root, receipt):
    import httpx
    import websockets
    from skills.impl.browser_use import CDPConnection

    runner, url, state = await fixture(root)
    receipt["fixture_url"] = url
    print(json.dumps({"phase": "fixture_ready", "fixture_url": url, "receipt": str(root / "receipt.json")}), flush=True)
    home = backend_settings(root)
    server_port = port()
    base = f"http://127.0.0.1:{server_port}"
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
           "FERAL_HOME": str(home), "FERAL_DATA_HOME": str(home / "data"), "FERAL_PORT": str(server_port),
           "FERAL_API_KEY": "synthetic-acceptance-" + uuid4().hex, "PYTHONNOUSERSITE": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "FERAL_NATIVE_DEFER_VAULT": "1", "FERAL_EMBED_MODEL_CACHE_ONLY": "1",
           "FERAL_MAX_ITERATIONS": "24", "FERAL_TOOL_LOOP_MAX_SECONDS": "480", "FERAL_AUTONOMY": "loose",
           "FERAL_PROACTIVE": "0", "FERAL_VISION": "0", "OPENAI_API_KEY": credential(args.credential_vault, root)}
    env["FERAL_VLM_API_KEY"] = env["OPENAI_API_KEY"]
    child = None
    cdp = None
    connection = None
    session_id = "browser-acceptance-" + uuid4().hex
    try:
        with (root / "backend.log").open("wb") as log:
            os.chmod(root / "backend.log", 0o600)
            child = subprocess.Popen([str(REPO / ".venv/bin/python"), "-m", "api.server"], cwd=CORE,
                                     env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        receipt["backend_pid"] = child.pid
        receipt["server_port"] = server_port
        async with httpx.AsyncClient(trust_env=False, headers={"Authorization": "Bearer " + env["FERAL_API_KEY"]}) as client:
            for _ in range(200):
                require(child.poll() is None, "source_backend_exited")
                try:
                    health = await client.get(base + "/health", timeout=2)
                    if health.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.25)
            else:
                raise Refusal("source_backend_health_timeout")
            subprocess.run(["/usr/bin/open", "-a", "Google Chrome", "--", url], check=True, timeout=15)
            connection = await request(client, base, "/api/browser/existing/connect", {"session_id": session_id, "consent": True})
            profile = Path(os.environ["HOME"]) / "Library/Application Support/Google/Chrome"
            cdp_port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
            target = None
            for _ in range(40):
                # The registered API intentionally withholds page URLs. Resolve
                # only our newly opened localhost URL from the local inventory,
                # then require its ID in the actual connector's selectable set.
                inventory = await client.get(f"http://127.0.0.1:{cdp_port}/json/list", timeout=5)
                candidates = [item for item in inventory.json() if item.get("type") == "page" and item.get("url") == url]
                if len(candidates) == 1:
                    target = candidates[0]
                    break
                await asyncio.sleep(.25)
                connection = await request(client, base, "/api/browser/existing/status", {"session_id": session_id})
            require(target is not None, "owned_regular_chrome_target_missing")
            target_id = target.get("target_id", target.get("id"))
            require(target_id in {item["id"] for item in connection["targets"]}, "owned_target_not_selectable")
            selected = await request(client, base, "/api/browser/existing/select", {"session_id": session_id, "connection_id": connection["connection_id"], "target_id": target_id})
            receipt["selected_target_hash"] = hashlib.sha256(target_id.encode()).hexdigest()
            require(selected.get("selected_target_id") == target_id, "selected_target_mismatch")
            prompt = (
                f"Complete this authorized synthetic task in the selected existing Chrome tab at {url}. "
                "Use browser UI tools only, without evaluate, shell, scripts, delegation, external accounts or external messages. "
                "It has internal UI tabs, not separate browser tabs. First click Validate trip with the reference code empty and observe its error. "
                "Read the Reference tab for the code and prices. Its reference code is drawn on a canvas: use browser__screenshot to read the actual visual card; do not use evaluate or inspect source. Fill Project name Harbor weekend, City Lisbon, Departure date 2026-11-07, "
                "Budget in EUR 450 and the reference code. Add two activity rows: Museum quantity 2, Tram pass quantity 3. "
                "Save itinerary. Then open Compose draft, subject Harbor weekend plan, "
                "body exactly Museum x2; Tram pass x3; total 55 EUR. Save draft locally. "
                "Verify the on-screen local saved result. Complete the task, do not give manual instructions. Nothing should be sent or purchased."
            )
            frames = []
            started = time.monotonic()
            async with websockets.connect(f"ws://127.0.0.1:{server_port}/v1/session?session_id={session_id}", proxy=None, max_size=2**20) as ws:
                await ws.send(json.dumps({"type": "req", "id": str(uuid4()), "method": "chat.capabilities", "params": {}}))
                while True:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if frame.get("type") == "res":
                        require(frame.get("payload", {}).get("turn_contract_versions") == [1], "tracked_turn_not_negotiated")
                        break
                request_id = str(uuid4())
                await ws.send(json.dumps({"type": "text_command", "msg_id": request_id, "payload": {"text": prompt, "turn_contract_version": 1}}))
                while time.monotonic() - started < 520:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 120))
                    frames.append(frame)
                    if frame.get("type") == "chat_turn_terminal":
                        receipt["terminal"] = {key: frame["payload"].get(key) for key in ("processing_outcome", "action_outcome", "request_id", "turn_id")}
                        break
                else:
                    raise Refusal("single_task_deadline_exceeded")
            (root / "synthetic-task-frames.json").write_text(json.dumps(frames, indent=2))
            receipt["task_elapsed_seconds"] = round(time.monotonic() - started, 2)
            receipt["tool_results"] = [{"name": f.get("payload", {}).get("tool_name", f.get("payload", {}).get("name")),
                                        "success": f.get("payload", {}).get("success")} for f in frames if f.get("type") == "tool_result"]
            receipt["fixture_state"] = state
            require(state["validation_errors"] >= 1, "validation_error_not_observed")
            require(state["reference_visits"] >= 1, "reference_tab_not_read")
            require(state["itinerary"] is not None and state["draft"] is not None, "local_task_outcome_not_saved")
            cdp = CDPConnection(host="127.0.0.1", port=cdp_port)
            require(await cdp.connect(prefer_page=True, target_id=target_id), "independent_dom_probe_unavailable")
            result = await cdp.send_command("Runtime.evaluate", {"expression": "JSON.stringify({url:location.href,result:document.querySelector('#result').textContent,subject:document.querySelector('#subject').value,body:document.querySelector('#body').value})", "returnByValue": True})
            dom = json.loads(result["result"]["value"])
            require(dom == {"url": url, "result": "Draft saved locally. Nothing sent.", "subject": EXPECTED["subject"], "body": EXPECTED["body"]}, "saved_dom_receipt_mismatch")
            receipt["dom_receipt"] = dom
            receipt["outcome_verified"] = True
    finally:
        if cdp is not None:
            await cdp.disconnect()
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(child.wait, 12)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                await asyncio.to_thread(child.wait, 5)
        receipt["backend_exit_code"] = child.poll() if child is not None else None
        if (home / "cost.db").is_file():
            with sqlite3.connect(f"file:{home / 'cost.db'}?mode=ro", uri=True) as db:
                rows = db.execute("SELECT call_site,model,prompt_tokens,completion_tokens,reasoning_tokens,dollars FROM cost_events").fetchall()
            receipt["usage"] = [{"call_site": row[0], "model": row[1], "prompt_tokens": row[2], "completion_tokens": row[3], "reasoning_tokens": row[4], "approximate_usd": row[5]} for row in rows]
            receipt["approximate_total_usd"] = sum(row[5] for row in rows)
        await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--credential-vault", type=Path)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    hashes = identity(args.expected_source)
    root = args.root.resolve()
    require(str(root).startswith("/private/tmp/feral-browser-full-") and not root.exists(), "fresh_owned_root_required")
    if not args.run:
        print(json.dumps({"source": args.expected_source, "source_hashes": hashes, "preflight": "passed", "launched": False}))
        return
    require(args.credential_vault is not None and args.credential_vault.is_absolute(), "explicit_credential_vault_required")
    root.mkdir(mode=0o700)
    receipt = {"source": args.expected_source, "source_hashes": hashes, "scope": "One synthetic localhost task through actual source FERAL and selected regular Chrome; native GUI separate", "outcome_verified": False}
    try:
        sys.path.insert(0, str(CORE))
        asyncio.run(actual(args, root, receipt))
    except Exception as error:
        receipt["failure_code"] = str(error) if isinstance(error, Refusal) else type(error).__name__
    finally:
        receipt["source_unchanged"] = identity(args.expected_source) == hashes
        (root / "receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps({"receipt": str(root / "receipt.json"), "outcome_verified": receipt["outcome_verified"], "failure_code": receipt.get("failure_code")}), flush=True)
    raise SystemExit(0 if receipt["outcome_verified"] and receipt["source_unchanged"] else 1)


if __name__ == "__main__":
    main()
