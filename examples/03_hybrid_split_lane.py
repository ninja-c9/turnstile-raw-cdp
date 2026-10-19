#!/usr/bin/env python3
"""Example 03: Split-Lane ("Hybrid Direct-Turnstile + Proxy-POST") Architecture.

1. Fast Direct Lane: Launch Google Chrome on the server's fast direct connection,
   let Cloudflare Turnstile solve in ~3s, hook window.fetch to intercept the
   outgoing registration JSON (including turnstile_token) without sending it
   from the datacenter IP, and immediately close Chrome.
2. Rotating Proxy Lane: Send the lightweight <1KB JSON POST via a rotating
   residential proxy using Python's standard library urllib.request.
"""
import argparse
import base64
import json
import os
import random
import shutil
import socket
import string
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
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


def capture_turnstile_payload(signup_url: str, email: str, secret_value: str, chrome_bin: str):
    """Solve Turnstile on fast direct Chrome and intercept the JSON payload via window.fetch."""
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
    user_dir = tempfile.mkdtemp(prefix="split_lane_")
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
        signup_url,
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        targets = None
        for _ in range(20):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
                    targets = json.loads(r.read().decode())
                    if any(t.get("type") == "page" for t in targets):
                        break
            except Exception:
                pass
            time.sleep(0.4)
        if not targets:
            return None

        page_ws = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")
        sock = ws_connect(page_ws)
        for _ in range(20):
            if cdp_eval(sock, 1, "!!document.querySelector('input[name=\"email\"]')"):
                break
            time.sleep(0.4)

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

        if cdp_eval(sock, 50, "document.querySelector('button[type=\"submit\"]')?.disabled") is not False:
            return None

        # Dismiss any lazy consent modal, hook window.fetch, and submit form in-page
        capture_js = f"""(() => {{
            for (const b of document.querySelectorAll('button')) {{
                if (b.textContent.trim() === 'Decline') b.click();
            }}
            window.__capturedPayload = null;
            window.fetch = async (url, init) => {{
                if (init && init.body) {{
                    window.__capturedPayload = init.body;
                }}
                return new Response(JSON.stringify({{status: 'ok'}}), {{
                    status: 202,
                    headers: {{'content-type': 'application/json'}}
                }});
            }};
            const setVal = (sel, val) => {{
                const el = document.querySelector(sel);
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }};
            setVal('input[name="email"]', {json.dumps(email)});
            setVal('input[name="password"]', {json.dumps(secret_value)});
            const cb = document.querySelector('input#accepted_terms');
            if (cb && !cb.checked) cb.click();
            document.querySelector('button[type="submit"]').click();
            return true;
        }})()"""
        cdp_eval(sock, 60, capture_js)
        for _ in range(15):
            time.sleep(0.2)
            raw = cdp_eval(sock, 70, "window.__capturedPayload")
            if raw:
                return json.loads(raw)
        return None
    finally:
        proc.kill()
        proc.wait()
        if xvfb:
            xvfb.kill()
            xvfb.wait()
        shutil.rmtree(user_dir, ignore_errors=True)


def post_via_proxy(api_url: str, origin: str, payload: dict, proxy_url: str = "") -> tuple[int, str]:
    handlers = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": origin,
            "Referer": origin + "/sign-up",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def main():
    ap = argparse.ArgumentParser(description="Split-Lane Turnstile Token Extraction + Proxy POST")
    ap.add_argument("--signup-url", required=True, help="Frontend signup page URL")
    ap.add_argument("--register-api", required=True, help="Backend register API endpoint")
    ap.add_argument("--email", default=f"user{random.randint(1000,9999)}@example.com")
    ap.add_argument("--proxy", default="", help="Optional HTTP proxy for the POST request")
    ap.add_argument("--chrome", default=default_chrome_bin())
    args = ap.parse_args()

    secret_val = " Ex!" + "".join(random.choices(string.ascii_letters + string.digits, k=13))
    payload = capture_turnstile_payload(args.signup_url, args.email, secret_val.strip(), args.chrome)
    if not payload:
        raise SystemExit("Failed to capture Turnstile payload")

    origin = "/".join(args.signup_url.split("/")[:3])
    status, body = post_via_proxy(args.register_api, origin, payload, args.proxy)
    print(json.dumps({"status": status, "token_length": len(payload.get("turnstile_token", "")), "body": body}, indent=2))


if __name__ == "__main__":
    main()
