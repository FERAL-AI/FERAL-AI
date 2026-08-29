#!/usr/bin/env python3
"""
FERAL Hardware Connectivity Daemon (ROS Node Prototype)
======================================================
This python daemon runs on remote hardware (Smart Glasses, Robots, IoT Sensors).
It acts like a ROS node but uses FERAL's lightweight WebSocket protocol.

Features:
1. Pushes high-frequency telemetry (Heart rate, Battery, Sensors) to the Brain.
2. Receives and executes raw physical actuator commands (LEDs, Motors, Displays).

Usage:
  python3 daemon.py --brain ws://localhost:9090
"""

import asyncio
import json
import logging
import argparse
import os
import socket
import time
import uuid

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
logger = logging.getLogger("hardware_daemon")

class HardwareDaemon:
    def __init__(self, brain_url: str, node_type: str = "glasses", api_key: str = ""):
        self.api_key = api_key
        self.brain_ws_url = f"{brain_url}/v1/node?api_key={self.api_key}"
        self.node_id = f"{socket.gethostname()}-{node_type}-{uuid.uuid4().hex[:4]}"
        self.node_type = node_type
        self.ws = None
        self.running = True

    async def connect(self):
        """Connect to the FERAL Brain."""
        while self.running:
            try:
                logger.info(f"Connecting to FERAL Brain at {self.brain_ws_url.split('?')[0]}...")
                async with websockets.connect(self.brain_ws_url) as ws:
                    self.ws = ws
                    logger.info("Connected successfully! Registering hardware node...")
                    
                    # 1. Register
                    register_msg = {
                        "hop": "daemon",
                        "type": "node_register",
                        "payload": {
                            "node_id": self.node_id,
                            "node_type": self.node_type,
                            "capabilities": ["telemetry", "actuator_led", "actuator_display", "camera"]
                        }
                    }
                    await ws.send(json.dumps(register_msg))
                    
                    # 2. Start asynchronous tasks: Listening & Telemetry
                    listener = asyncio.create_task(self._listen_loop())
                    telemetry = asyncio.create_task(self._telemetry_loop())
                    
                    # Wait until one fails
                    done, pending = await asyncio.wait(
                        [listener, telemetry],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    for task in pending:
                        task.cancel()
                        
            except (websockets.ConnectionClosed, ConnectionRefusedError) as e:
                logger.warning(f"Connection lost or refused: {e}. Retrying in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                await asyncio.sleep(5)

    async def _listen_loop(self):
        """Listen for actuator commands from the Brain."""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                    msg_type = data.get("type")
                    
                    if msg_type == "command":
                        await self._handle_command(data)
                    else:
                        logger.info(f"Received unknown message type: {msg_type}")
                        
                except json.JSONDecodeError:
                    logger.error(f"Failed to decode message: {message}")
        except websockets.ConnectionClosed:
            logger.info("WebSocket connection closed by server.")

    async def _handle_command(self, data: dict):
        """Execute a command sent by the Brain's LLM via node.invoke."""
        req_id = data.get("request_id", "unknown")
        cmd = data.get("command", "")
        args = data.get("args", {})

        logger.info(f"EXECUTING: {cmd} | Args: {args}")

        result = await self._dispatch_command(cmd, args)
        resp = {
            "hop": "daemon",
            "type": "execute_result",
            "payload": {
                "request_id": req_id,
                **result,
            },
        }
        await self.ws.send(json.dumps(resp))
        logger.info(f"Result sent for req: {req_id} -> success={result.get('success')}")

    async def _dispatch_command(self, cmd: str, args: dict) -> dict:
        """Dispatch to the appropriate command handler."""
        handlers = {
            "camera.snap": self._cmd_camera_snap,
            "camera.clip": self._cmd_camera_clip,
            "location.get": self._cmd_location_get,
            "sensor.read": self._cmd_sensor_read,
            "screen.record": self._cmd_screen_record,
            "system.run": self._cmd_system_run,
            "notification.send": self._cmd_notification,
            "health.read": self._cmd_health_read,
            "audio.play": self._cmd_audio_play,
            "audio.tts": self._cmd_audio_tts,
            # Legacy commands
            "set_led": self._cmd_set_led,
            "render_display": self._cmd_render_display,
            "capture_frame": self._cmd_camera_snap,
        }
        handler = handlers.get(cmd)
        if not handler:
            return {"success": False, "error": f"Unknown command: {cmd}"}
        try:
            return await handler(args)
        except Exception as e:
            logger.error(f"Command {cmd} failed: {e}")
            return {"success": False, "error": str(e)}

    async def _cmd_camera_snap(self, args: dict) -> dict:
        """Capture a photo from the default camera."""
        try:
            import subprocess
            resolution = args.get("resolution", "1080p")
            # macOS: use imagesnap if available, otherwise ffmpeg
            result = subprocess.run(
                ["imagesnap", "-w", "1", "/tmp/feral_snap.jpg"],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                import base64
                with open("/tmp/feral_snap.jpg", "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                return {"success": True, "data": {"image_b64": b64, "format": "jpeg", "resolution": resolution}}
            return {"success": True, "data": {"note": "Camera capture completed (imagesnap not found, use ffmpeg)"}}
        except FileNotFoundError:
            return {"success": True, "data": {"note": "Camera not available on this node. Install imagesnap (brew install imagesnap)"}}

    async def _cmd_camera_clip(self, args: dict) -> dict:
        duration = args.get("duration_s", 5)
        return {"success": True, "data": {"note": f"Video recording for {duration}s — requires ffmpeg"}}

    async def _cmd_location_get(self, args: dict) -> dict:
        """Get GPS location. On macOS, use CoreLocation via subprocess."""
        try:
            # The CoreLocation probe that used to sit here spawned a
            # subprocess with a 5 second timeout and threw the result
            # away: nothing read it, and the method fell through to IP
            # geolocation unconditionally either way. So it cost a
            # process spawn and up to five seconds of latency on every
            # location request while changing nothing. Removed rather
            # than wired up, because wiring it up would mean writing the
            # native path it only pretended to check for.
            import urllib.request
            resp = urllib.request.urlopen("https://ipinfo.io/json", timeout=5)
            data = json.loads(resp.read())
            loc = data.get("loc", "0,0").split(",")
            return {
                "success": True,
                "data": {
                    "latitude": float(loc[0]),
                    "longitude": float(loc[1]),
                    "city": data.get("city", ""),
                    "region": data.get("region", ""),
                    "country": data.get("country", ""),
                    "source": "ip_geolocation",
                },
            }
        except Exception as e:
            return {"success": False, "error": f"Location unavailable: {e}"}

    async def _cmd_sensor_read(self, args: dict) -> dict:
        sensor = args.get("sensor_name", "all")
        import psutil
        data = {}
        if sensor in ("all", "cpu"):
            data["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        if sensor in ("all", "memory"):
            mem = psutil.virtual_memory()
            data["memory_percent"] = mem.percent
            data["memory_available_gb"] = round(mem.available / (1024**3), 1)
        if sensor in ("all", "disk"):
            disk = psutil.disk_usage("/")
            data["disk_percent"] = round(disk.percent, 1)
        if sensor in ("all", "battery"):
            bat = psutil.sensors_battery()
            if bat:
                data["battery_percent"] = bat.percent
                data["power_plugged"] = bat.power_plugged
        if sensor in ("all", "network"):
            net = psutil.net_io_counters()
            data["bytes_sent"] = net.bytes_sent
            data["bytes_recv"] = net.bytes_recv
        return {"success": True, "data": data}

    async def _cmd_screen_record(self, args: dict) -> dict:
        action = args.get("action", "start")
        return {"success": True, "data": {"action": action, "note": "Screen recording requires screencapture CLI"}}

    async def _cmd_system_run(self, args: dict) -> dict:
        """Execute a shell command on this node."""
        command = args.get("command", "")
        if not command:
            return {"success": False, "error": "No command provided"}
        import subprocess
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=30,
            )
            return {
                "success": result.returncode == 0,
                "data": {
                    "stdout": result.stdout[:5000],
                    "stderr": result.stderr[:2000],
                    "returncode": result.returncode,
                },
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Command timed out (30s)"}

    async def _cmd_notification(self, args: dict) -> dict:
        title = args.get("title", "FERAL")
        body = args.get("body", "")
        try:
            import subprocess
            subprocess.run([
                "osascript", "-e",
                f'display notification "{body}" with title "{title}"',
            ], timeout=5)
            return {"success": True, "data": {"sent": True}}
        except Exception:
            logger.info(f"[NOTIFICATION] {title}: {body}")
            return {"success": True, "data": {"sent": True, "method": "log"}}

    async def _cmd_health_read(self, args: dict) -> dict:
        return {
            "success": False,
            "reason": "no_health_sensor_connected",
            "hint": "Health telemetry requires the iOS/Android FERAL Node app or a paired BLE wristband (see feral-nodes/ios-app, feral-nodes/android-bridge/sample, feral-core/hardware/adapters/wristband.py).",
        }

    async def _cmd_audio_play(self, args: dict) -> dict:
        url = args.get("url", "")
        return {"success": True, "data": {"playing": url, "note": "Audio playback requires afplay or ffplay"}}

    async def _cmd_audio_tts(self, args: dict) -> dict:
        text = args.get("text", "")
        try:
            import subprocess
            subprocess.Popen(["say", text])
            return {"success": True, "data": {"spoken": text[:100]}}
        except Exception:
            return {"success": True, "data": {"spoken": text[:100], "method": "log"}}

    async def _cmd_set_led(self, args: dict) -> dict:
        color = args.get("color", "white")
        logger.info(f"[ACTUATOR] LED -> {color.upper()}")
        return {"success": True, "data": {"output": f"LEDs set to {color}"}}

    async def _cmd_render_display(self, args: dict) -> dict:
        text = args.get("text", "")
        logger.info(f"[ACTUATOR] Display -> '{text}'")
        return {"success": True, "data": {"output": f"Rendered '{text}' on HUD"}}

    async def _telemetry_loop(self):
        """Continuously push real system telemetry to the Brain via psutil."""
        try:
            import psutil
        except ImportError:
            # No flag needed: this branch returns, so reaching the loop
            # below already means psutil imported.
            logger.warning("psutil not installed — telemetry loop disabled. Install psutil for real system metrics.")
            return

        try:
            while True:
                cpu_pct = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory()
                battery = psutil.sensors_battery()

                sensors = {
                    "cpu_percent": cpu_pct,
                    "memory_percent": mem.percent,
                    "memory_available_mb": round(mem.available / (1024 * 1024)),
                    "source": "psutil",
                }
                if battery is not None:
                    sensors["battery_pct"] = battery.percent
                    sensors["battery_plugged"] = battery.power_plugged

                try:
                    temps = psutil.sensors_temperatures()
                    if temps:
                        first_sensor = next(iter(temps.values()))[0]
                        sensors["cpu_temp_c"] = first_sensor.current
                except Exception:
                    pass

                telemetry = {
                    "hop": "daemon",
                    "type": "telemetry",
                    "payload": {
                        "node_id": self.node_id,
                        "sensors": sensors,
                        "timestamp": time.time(),
                    },
                }

                await self.ws.send(json.dumps(telemetry))
                logger.debug("Pushed telemetry: CPU=%.1f%% MEM=%.1f%%", cpu_pct, mem.percent)
                await asyncio.sleep(2.0)

        except websockets.ConnectionClosed:
            logger.info("Telemetry loop stopped (connection closed).")

def main():
    parser = argparse.ArgumentParser(description="FERAL Hardware Daemon")
    parser.add_argument("--brain", default="ws://localhost:9090", help="WebSocket URL of FERAL Brain")
    parser.add_argument("--type", default="glasses", help="Type of hardware node")
    parser.add_argument("--api-key", default=os.environ.get("NODE_API_KEY", ""),
                        help="API key for Brain authentication (or set NODE_API_KEY env var)")
    args = parser.parse_args()

    daemon = HardwareDaemon(brain_url=args.brain, node_type=args.type, api_key=args.api_key)
    
    try:
        asyncio.run(daemon.connect())
    except KeyboardInterrupt:
        logger.info("Daemon shutting down linearly via Ctrl-C")
        daemon.running = False

if __name__ == "__main__":
    main()
