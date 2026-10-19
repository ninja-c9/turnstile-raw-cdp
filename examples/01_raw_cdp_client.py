#!/usr/bin/env python3
"""Example 01: Zero-dependency Raw WebSocket + CDP client without Runtime.enable.

Demonstrates how to connect to Chrome's --remote-debugging-port using 100%
Python standard library (socket, struct, json) and evaluate JavaScript via
Runtime.evaluate without ever sending Runtime.enable.
"""
import argparse
import base64
import json
import os
import random
import shutil
import socket
import struct
import subprocess
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
    """Open a raw RFC 6455 WebSocket connection to Chrome's CDP endpoint."""
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
    """Send a client-masked text WebSocket frame."""
    data = json.dumps(payload_obj).encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])  # FIN + text frame
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
    """Receive and decode a single WebSocket JSON frame."""
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
    """Call a CDP method and wait for the matching response ID."""
    ws_send(sock, {"id": msg_id, "method": method, "params": params})
    while True:
        msg = ws_recv(sock)
        if msg.get("id") == msg_id:
            return msg.get("result", {})


def cdp_eval(sock: socket.socket, msg_id: int, expression: str, await_promise: bool = False):
    """Evaluate JS via Runtime.evaluate WITHOUT enabling the Runtime domain."""
    res = cdp_call(
        sock,
        msg_id,
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        },
    )
    return res.get("result", {}).get("value")


def main():
    ap = argparse.ArgumentParser(description="Minimal Raw CDP evaluation without Runtime.enable")
    ap.add_argument("--url", default="https://example.com", help="Target URL to open")
    ap.add_argument("--chrome", default=default_chrome_bin(), help="Path to Google Chrome binary")
    args = ap.parse_args()

    port = random.randint(19400, 19899)
    user_dir = tempfile.mkdtemp(prefix="raw_cdp_")
    cmd = [
        args.chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_dir}",
        args.url,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        page_ws = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")
        sock = ws_connect(page_ws)

        # Verify that console.table / console.dir getters do NOT fire when Runtime.enable is omitted
        probe_js = """(() => {
            let getterFired = false;
            const probe = {};
            Object.defineProperty(probe, 'trap', {
                get() { getterFired = true; return 'caught'; }
            });
            console.dir(probe);
            console.table(probe);
            return {
                title: document.title,
                webdriver: navigator.webdriver,
                cdpGetterFired: getterFired
            };
        })()"""
        result = cdp_eval(sock, 1, probe_js)
        print(json.dumps(result, indent=2))
    finally:
        proc.kill()
        proc.wait()
        shutil.rmtree(user_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
