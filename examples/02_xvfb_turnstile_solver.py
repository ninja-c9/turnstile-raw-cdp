#!/usr/bin/env python3
"""Example 02: Headed Google Chrome + Xvfb Cloudflare Turnstile Solver.

Launches an isolated Xvfb virtual display on Linux (or headed Chrome on macOS),
connects via raw CDP without Runtime.enable, dismisses consent modals, clicks
the Closed Shadow DOM Turnstile checkbox if needed, and optionally saves a PNG
screenshot via Page.captureScreenshot.
"""
import argparse
import base64
import json
import os
import pathlib
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request


def default_chrome_bin() -> str:
    for candidate in (
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if os.path.exists(candidate):
            return candidate
    return "google-chrome-stable"


def ws_connect(ws_url: str) -> socket.socket:
    parts = ws_url.replace("ws://", "").split("/", 1)
    host, port = parts[0].split(":")
    path = "/" + parts[1]
    sock = socket.create_connection((host, int(port)), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(handshake.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    return sock


def ws_send(sock: socket.socket, payload_obj: dict) -> None:
    data = json.dumps(payload_obj).encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(header + masked)


def ws_recv(sock: socket.socket) -> dict:
    def read_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("WebSocket closed")
            buf += chunk
        return buf

    b1, b2 = read_exact(2)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(8))[0]
    mask = read_exact(4) if (b2 & 0x80) else None
    payload = read_exact(length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return json.loads(payload.decode("utf-8", "ignore"))


def cdp_call(sock: socket.socket, msg_id: int, method: str, params: dict) -> dict:
    ws_send(sock, {"id": msg_id, "method": method, "params": params})
    while True:
        msg = ws_recv(sock)
        if msg.get("id") == msg_id:
            return msg.get("result", {})


def cdp_eval(sock: socket.socket, msg_id: int, expression: str, await_promise: bool = False):
    res = cdp_call(
        sock,
        msg_id,
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
    )
    return res.get("result", {}).get("value")


def solve_turnstile(url: str, chrome_bin: str, screenshot_path: str = "") -> dict:
    xvfb = None
    env = os.environ.copy()
    if sys.platform.startswith("linux") and shutil.which("Xvfb"):
        disp_num = random.randint(100, 299)
        xvfb = subprocess.Popen(
            ["Xvfb", f":{disp_num}", "-screen", "0", "1440x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.4)
        env["DISPLAY"] = f":{disp_num}"

    port = random.randint(19400, 19899)
    user_dir = tempfile.mkdtemp(prefix="ts_solver_")
    cmd = [
        chrome_bin,
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,1080",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_dir}",
        url,
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        targets = None
        for _ in range(24):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
                    targets = json.loads(r.read().decode())
                    if any(t.get("type") == "page" for t in targets):
                        break
            except Exception:
                pass
            time.sleep(0.4)

        page_ws = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")
        sock = ws_connect(page_ws)

        for _ in range(20):
            if cdp_eval(sock, 1, "document.readyState === 'complete'"):
                break
            time.sleep(0.4)

        # Dismiss optional consent/attribution banners and scroll submit button into view
        cdp_eval(
            sock,
            2,
            """(() => {
                for (const b of document.querySelectorAll('button')) {
                    if (b.textContent.trim() === 'Decline') b.click();
                }
                const sub = document.querySelector('button[type="submit"]');
                if (sub) sub.scrollIntoView({block: 'center'});
            })()""",
        )

        # Poll submit button disabled state; click Closed Shadow DOM Turnstile container if needed
        box = None
        for tick in range(22):
            disabled = cdp_eval(sock, 10 + tick, "document.querySelector('button[type=\"submit\"]')?.disabled")
            if disabled is False:
                break
            if tick in (6, 12, 17):
                box = cdp_eval(
                    sock,
                    100 + tick,
                    """(() => {
                        const btn = document.querySelector('button[type="submit"]');
                        const prev = btn ? btn.previousElementSibling : null;
                        if (!prev) return null;
                        const r = prev.getBoundingClientRect();
                        return {x: r.x, y: r.y, w: r.width, h: r.height};
                    })()""",
                )
                if box and box.get("h", 0) > 10:
                    cx = box["x"] + 28
                    cy = box["y"] + box["h"] / 2
                    for ev in ("mouseMoved", "mousePressed", "mouseReleased"):
                        cdp_call(
                            sock,
                            200 + tick,
                            "Input.dispatchMouseEvent",
                            {
                                "type": ev,
                                "x": cx,
                                "y": cy,
                                "button": "left" if ev != "mouseMoved" else "none",
                                "clickCount": 1 if ev != "mouseMoved" else 0,
                            },
                        )
            time.sleep(0.5)

        if screenshot_path:
            shot = cdp_call(sock, 300, "Page.captureScreenshot", {"format": "png"})
            if shot.get("data"):
                pathlib.Path(screenshot_path).write_bytes(base64.b64decode(shot["data"]))

        disabled = cdp_eval(sock, 400, "document.querySelector('button[type=\"submit\"]')?.disabled")
        return {"solved": disabled is False, "submit_disabled": disabled, "widget_box": box}
    finally:
        proc.kill()
        proc.wait()
        if xvfb:
            xvfb.kill()
            xvfb.wait()
        shutil.rmtree(user_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Headed Chrome + Xvfb Turnstile Solver")
    ap.add_argument("--url", required=True, help="Signup URL containing Cloudflare Turnstile")
    ap.add_argument("--chrome", default=default_chrome_bin(), help="Path to Google Chrome binary")
    ap.add_argument("--screenshot", default="", help="Optional path to save PNG screenshot")
    args = ap.parse_args()

    res = solve_turnstile(args.url, args.chrome, args.screenshot)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
