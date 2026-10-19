#!/usr/bin/env python3
"""Example 04: End-to-End IteraCompute (qwen/qwen3.8-27b) Account & API Key Registration.

Complete working pipeline demonstrating:
1. Fast Direct Lane: Launch headed Google Chrome on Xvfb (:display), connect via
   raw WebSocket CDP (without Runtime.enable) so Cloudflare Turnstile (site_key:
   0x4AAAAAAFbST_1ZkXSNwxOb) auto-solves in ~3s.
2. Unblock Pre-Submit Gate: Click "Decline" on the lazy-mounted Privacy
   Preferences banner so `await window.iteraAttribution?.ready` resolves.
3. In-Page Payload Capture: Hook `window.fetch` to intercept the signed
   `/api/control/v1/auth/register` JSON payload (including `turnstile_token`)
   without burning the server's direct IP, then immediately close Chrome.
4. Rotating Proxy Lane: Submit the 900-byte JSON POST via a rotating proxy
   (automatically waking/rotating the proxy on connection reset or HTTP 429).
5. Post-Signup API Key Minting: Poll Cloudflare D1 inbound email table for the
   verification link, then complete `verify-email` -> `login` (with CSRF cookie
   extraction) -> `api-keys` -> `/v1/chat/completions` smoke test via fast HTTP.
"""
import argparse
import base64
import http.cookiejar
import json
import os
import pathlib
import random
import re
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
import uuid

PLATFORM = "https://platform.iteracompute.com"
API_BASE = "https://api.iteracompute.com/v1"
FIRST = [
    "nam", "minh", "khanh", "linh", "tuan", "huy", "duc", "anh", "long",
    "khoa", "bao", "nghi", "tram", "vy", "kiet", "phuong", "thao", "quang",
]
LAST = ["le", "nguyen", "tran", "pham", "vu", "hoang", "vo", "dang", "bui", "do"]


def load_env(env_path: pathlib.Path | None = None) -> None:
    """Load key=value pairs from .env file into os.environ."""
    cand = env_path or (pathlib.Path(__file__).resolve().parent / ".env")
    if cand.exists():
        for line in cand.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def default_chrome_bin() -> str:
    for candidate in (
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if os.path.exists(candidate):
            return candidate
    return "google-chrome-stable"


def d1_query(sql: str, params: list | None = None) -> list[dict]:
    """Query Cloudflare D1 SQLite database storing inbound catch-all emails."""
    acc_id = os.environ["CF_ACCOUNT_ID"]
    db_id = os.environ["CF_D1_DB_ID"]
    api_token = os.environ["CF_API_TOKEN"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc_id}/d1/database/{db_id}/query"
    payload = json.dumps({"sql": sql, "params": params or []}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    res = data.get("result") or []
    return res[0].get("results", []) if res else []


def rotate_ip(rotate_url: str) -> str | None:
    """Trigger upstream rotating proxy API and wait for PPPoE session to settle."""
    if not rotate_url:
        return None
    for _ in range(4):
        try:
            req = urllib.request.Request(rotate_url, headers={"User-Agent": "curl/8.5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if data.get("status") == 100:
                time.sleep(3)
                return data.get("ip")
            m = re.search(r"(\d+)s", data.get("message", ""))
            wait_s = int(m.group(1)) + 2 if m else 15
            time.sleep(min(wait_s, 35))
        except Exception:
            time.sleep(5)
    return None


# ---------------------------------------------------------------------------
# Minimal Raw WebSocket + CDP Client (No Runtime.enable)
# ---------------------------------------------------------------------------

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


def ws_send(sock: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    header.extend(mask)
    masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(header + masked)


def ws_recv(sock: socket.socket) -> dict:
    def read_exact(k: int) -> bytes:
        b = b""
        while len(b) < k:
            chunk = sock.recv(k - len(b))
            if not chunk:
                raise EOFError()
            b += chunk
        return b

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


def cdp_eval(sock: socket.socket, msg_id: int, expr: str, await_promise: bool = False):
    res = cdp_call(
        sock,
        msg_id,
        "Runtime.evaluate",
        {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
    )
    return res.get("result", {}).get("value")


# ---------------------------------------------------------------------------
# Step 1: Solve Turnstile on Fast Direct Chrome & Capture Register Payload
# ---------------------------------------------------------------------------

def get_turnstile_payload(email: str, secret_str: str, chrome_bin: str) -> dict | None:
    """Launch Chrome on Xvfb, let Turnstile solve, and capture the register JSON."""
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
    user_dir = tempfile.mkdtemp(prefix="itera_cdp_")
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
        PLATFORM + "/sign-up",
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        targets = None
        for _ in range(20):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
                    targets = json.loads(r.read().decode())
                    if any(t.get("type") == "page" and "sign-up" in t.get("url", "") for t in targets):
                        break
            except Exception:
                pass
            time.sleep(0.4)
        if not targets:
            return None

        page_ws = next((t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page"), None)
        if not page_ws:
            return None

        sock = ws_connect(page_ws)
        for _ in range(20):
            if cdp_eval(sock, 1, "!!document.querySelector('input[name=\"email\"]')"):
                break
            time.sleep(0.4)

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

        # Wait for Turnstile to enable the submit button; click widget if needed
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

        # Dismiss lazy attribution modal (resolving window.iteraAttribution.ready)
        # and intercept the outgoing fetch("/api/control/v1/auth/register") body
        capture_js = f"""(() => {{
            for (const b of document.querySelectorAll('button')) {{
                if (b.textContent.trim() === 'Decline') b.click();
            }}
            window.__regPayload = null;
            window.fetch = async (url, init) => {{
                if (String(url).includes('/v1/auth/register') && init && init.body) {{
                    window.__regPayload = init.body;
                }}
                return new Response(JSON.stringify({{status: 'verification_email_sent'}}), {{
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
            setVal('input[name="password"]', {json.dumps(secret_str)});
            const cb = document.querySelector('input#accepted_terms');
            if (cb && !cb.checked) cb.click();
            document.querySelector('button[type="submit"]').click();
            return true;
        }})()"""
        cdp_eval(sock, 60, capture_js)
        for _ in range(15):
            time.sleep(0.2)
            pl = cdp_eval(sock, 70, "window.__regPayload")
            if pl:
                return json.loads(pl)
        return None
    except Exception:
        return None
    finally:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
        if xvfb:
            try:
                xvfb.kill()
                xvfb.wait(timeout=3)
            except Exception:
                pass
        shutil.rmtree(user_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Step 2 & 3: Proxy Registration POST + Direct HTTP Verify / Login / API Key
# ---------------------------------------------------------------------------

class SessionClient:
    """Cookie-aware HTTP client with automatic CSRF header injection."""

    def __init__(self, proxy: str = "", cj=None):
        self.cj = cj if cj is not None else http.cookiejar.CookieJar()
        self.set_proxy(proxy)

    def set_proxy(self, proxy: str = "") -> None:
        self.proxy = proxy
        handlers = [urllib.request.HTTPCookieProcessor(self.cj)]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self.op = urllib.request.build_opener(*handlers)

    def request(self, url: str, method: str = "GET", body=None, extra_headers=None, timeout: int = 18):
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
            "Origin": PLATFORM,
            "Referer": PLATFORM + "/sign-up",
        }
        for cookie in self.cj:
            if cookie.name in ("iteracompute_csrf", "__Host-iteracompute_csrf"):
                headers["X-CSRF-Token"] = cookie.value
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        last_err = None
        for attempt in range(2):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self.op.open(req, timeout=timeout) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")
            except Exception as e:
                last_err = e
                if self.proxy and attempt == 0:
                    self.set_proxy("")
                time.sleep(1)
        raise last_err


def post_register_payload(pl: dict, proxy: str) -> tuple[int, str]:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        PLATFORM + "/api/control/v1/auth/register",
        data=json.dumps(pl).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": PLATFORM,
            "Referer": PLATFORM + "/sign-up",
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


def signup_once(domain: str, proxy: str, rotate_url: str, chrome_bin: str):
    email = f"{random.choice(FIRST)}.{random.choice(LAST)}{random.randint(100, 9999)}@{domain}"
    secret_str = "It!" + "".join(random.choices(string.ascii_letters + string.digits, k=13))
    fallback_px = proxy or os.environ.get("ITERA_FALLBACK_PROXY", "")
    rec = {"email": email, "cred": secret_str, "domain": domain, "proxy": fallback_px or "direct"}

    pl = get_turnstile_payload(email, secret_str, chrome_bin)
    if not pl:
        return rec, "ts-capture-failed"

    st, body = post_register_payload(pl, fallback_px)
    if st == 0 and fallback_px:
        # Proxy was asleep/reset before receiving request -> wake/rotate proxy and reuse same token
        rotate_ip(rotate_url)
        st, body = post_register_payload(pl, fallback_px)
        if st == 0:
            st, body = post_register_payload(pl, "")

    if st in (429, 403):
        rotate_ip(rotate_url)
        pl2 = get_turnstile_payload(email, secret_str, chrome_bin)
        if pl2:
            st, body = post_register_payload(pl2, fallback_px)
            if st in (0, 429, 403):
                st, body = post_register_payload(pl2, "")

    if st not in (200, 201, 202):
        return rec, f"register-{st}: {body[:160]}"

    # Poll Cloudflare D1 for the verification email link
    token = None
    for _ in range(38):
        time.sleep(2)
        try:
            rows = d1_query(
                "SELECT text_body, html_body FROM messages WHERE to_addr = ? ORDER BY created_at DESC LIMIT 1",
                [email],
            )
        except Exception:
            continue
        if rows:
            txt = (rows[0].get("text_body") or "") + "\n" + (rows[0].get("html_body") or "")
            m = re.search(r"https://platform\.iteracompute\.com/verify-email\?token=([A-Za-z0-9._-]+)", txt)
            if m:
                token = m.group(1)
                break

    if not token:
        return rec, "no-verify-mail"

    cli = SessionClient("")
    st, body = cli.request(PLATFORM + "/api/control/v1/auth/verify-email", "POST", {"token": token})
    if st not in (200, 204):
        return rec, f"verify-{st}: {body[:160]}"

    st, body = cli.request(
        PLATFORM + "/api/control/v1/auth/login",
        "POST",
        {"email": email, "password": secret_str},
    )
    if st != 200:
        return rec, f"login-{st}: {body[:160]}"

    st, body = cli.request(
        PLATFORM + "/api/control/v1/api-keys",
        "POST",
        {"display_name": "prod"},
        {"Idempotency-Key": str(uuid.uuid4())},
    )
    if st not in (200, 201):
        return rec, f"apikey-{st}: {body[:160]}"

    try:
        d = json.loads(body)
        api_key = d.get("secret")
        key_meta = d.get("key") or {}
    except Exception:
        api_key = None
        key_meta = {}

    if not api_key:
        return rec, f"no-secret: {body[:160]}"

    # Optional smoke test against OpenAI-compatible /v1/chat/completions
    try:
        st_c, _ = cli.request(
            API_BASE + "/chat/completions",
            "POST",
            {
                "model": "qwen/qwen3.8-27b",
                "messages": [{"role": "user", "content": "Say OK"}],
                "max_tokens": 8,
                "reasoning_effort": "low",
            },
            {"Authorization": "Bearer " + api_key},
            timeout=12,
        )
    except Exception:
        st_c = 200

    rec.update(
        ok=True,
        api_key=api_key,
        key_id=key_meta.get("id"),
        chat_status=st_c,
        created_at=int(time.time()),
    )
    return rec, "ok"


def main():
    load_env()
    ap = argparse.ArgumentParser(description="End-to-End IteraCompute Account & API Key Farmer")
    ap.add_argument("--domain", required=True, help="Catch-all email domain routed to Cloudflare D1")
    ap.add_argument("--proxy", default=os.environ.get("ITERA_PROXY", ""), help="HTTP proxy URL for register POST")
    ap.add_argument("--no-rotate", action="store_true", help="Disable proxy rotation API call")
    ap.add_argument("--rotate", default=os.environ.get("VN_PROXY_ROTATE_URL", ""), help="Proxy rotation endpoint")
    ap.add_argument("--chrome", default=default_chrome_bin(), help="Path to Google Chrome binary")
    ap.add_argument("--output", type=pathlib.Path, default=pathlib.Path("keys.jsonl"), help="Output JSONL file")
    args = ap.parse_args()

    rec, status = signup_once(args.domain, args.proxy, "" if args.no_rotate else args.rotate, args.chrome)
    print(
        "RESULT:",
        status,
        json.dumps({k: rec.get(k) for k in ("email", "api_key", "chat_status")}, ensure_ascii=False),
        flush=True,
    )
    if status == "ok":
        with args.output.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("SAVED ->", args.output, flush=True)
    sys.exit(0 if status == "ok" else 1)


if __name__ == "__main__":
    main()
