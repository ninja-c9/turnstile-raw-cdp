# turnstile-raw-cdp

**Bypassing Cloudflare Turnstile at Scale via Raw Chrome DevTools Protocol (No `Runtime.enable`), Headed `Xvfb` Chrome, and Split-Lane Token Extraction**

A step-by-step technical breakdown and 100% Python standard-library reference implementation explaining why standard browser automation frameworks (Playwright, Puppeteer, Selenium) fail modern Cloudflare Turnstile challenges—and how to build a fast, deterministic solver on Linux servers.

---

## Table of Contents

1. [Why Playwright & Puppeteer Get Caught (`Runtime.enable` Leak)](#1-why-playwright--puppeteer-get-caught-runtimeenable-leak)
2. [Why `headless_shell` & Unbranded Chromium Fail](#2-why-headless_shell--unbranded-chromium-fail)
3. [Architecture Overview: The 4-Layer Bypass](#3-architecture-overview-the-4-layer-bypass)
4. [Step-by-Step Implementation Guide](#4-step-by-step-implementation-guide)
   - [Step 1: Environment Setup (`google-chrome-stable` + `Xvfb` + Mesa)](#step-1-environment-setup-google-chrome-stable--xvfb--mesa)
   - [Step 2: Zero-Dependency Raw CDP Client (Without `Runtime.enable`)](#step-2-zero-dependency-raw-cdp-client-without-runtimeenable)
   - [Step 3: Closed Shadow DOM Clicking & Pre-Submit Promise Unblocking](#step-3-closed-shadow-dom-clicking--pre-submit-promise-unblocking)
   - [Step 4: Split-Lane Token Extraction (Fast Direct Turnstile + Rotating Proxy POST)](#step-4-split-lane-token-extraction-fast-direct-turnstile--rotating-proxy-post)
5. [Repository Structure & Running the Examples](#5-repository-structure--running-the-examples)

---

## 1. Why Playwright & Puppeteer Get Caught (`Runtime.enable` Leak)

Even if you pass `--disable-blink-features=AutomationControlled` and patch `navigator.webdriver = undefined`, Playwright and Puppeteer automatically send the Chrome DevTools Protocol command **`Runtime.enable`** to every page and iframe target upon connection (including the cross-origin `https://challenges.cloudflare.com` Turnstile iframe).

When inspecting console events emitted by Cloudflare Turnstile's challenge script inside an instrumented browser, you will see telemetry probes like this:

```text
startGroupCollapsed:
debug: Error
log: /.*.*=.*/
log: JSHandle@node
count: \u0000: 1
debug: %c%d font-size:0;color:transparent NaN
dir:  OROhR9: RmGBF2
dirxml: %c%d font-size:0;color:transparent OROhR9: RmGBF2
table: %c%d font-size:0;color:transparent OROhR9: RmGBF2
endGroup: console.groupEnd
```

### How the Detector Works in V8 (`v8_console.cc`)

1. Turnstile constructs a JavaScript object (or `Error` instance) with a property getter attached (e.g., property `OROhR9`) and passes it to `console.dir()`, `console.table()`, and `console.debug()`.
2. In Chromium's V8 inspector implementation (`v8_console.cc`), `console.dir` and `console.table` are **no-ops** when no DevTools/CDP session has enabled the `Runtime` domain. The property getter is **never invoked** in a normal user browser.
3. However, as soon as Playwright or Puppeteer sends `Runtime.enable`, V8 serializes the arguments of `console.dir` / `console.table` to build the `Runtime.consoleAPICalled` CDP event. Serializing the object invokes the getter, immediately flagging the session as automated (`! Verification failed`).

### The Fix: `Runtime.evaluate` Without `Runtime.enable`

Chromium's CDP implementation allows calling **`Runtime.evaluate`** (`returnByValue: true`) and **`Input.dispatchMouseEvent`** on a page target **without ever calling `Runtime.enable`**. Because `Runtime.enable` is never sent:
- No `Runtime.consoleAPICalled` serialization occurs.
- Turnstile's `console.dir` / `console.table` property getters never fire.
- We never attach to the cross-origin `challenges.cloudflare.com` iframe target at all.

---

## 2. Why `headless_shell` & Unbranded Chromium Fail

Besides `Runtime.enable`, Turnstile inspects the browser environment inside its challenge worker (`blob:https://challenges.cloudflare.com/...`):

| Signal | Playwright `chromium_headless_shell` | Official `google-chrome-stable` + `Xvfb` |
| :--- | :--- | :--- |
| `navigator.userAgentData.brands` | Contains only `"Chromium"`, missing `"Google Chrome"` | Includes `"Google Chrome"` brand |
| WebGL Renderer | Software fallback (`SwiftShader` deprecation warning) | Mesa Gallium / `llvmpipe` X11 GLX context |
| Display Server | Headless Ozone platform (`--ozone-platform=headless`) | Real X11 display (`DISPLAY=:99` via `Xvfb`) |
| Turnstile Result | `401` on `/h/b/pat/...` $\rightarrow$ `Verification failed` | Auto-solves (`Success!`) in ~2.5–3.5 seconds |

---

## 3. Architecture Overview: The 4-Layer Bypass

```mermaid
flowchart LR
    subgraph Direct["1. Fast Direct Lane (Server 10Gbps)"]
        A["Xvfb :display + google-chrome-stable"] --> B["Raw WebSocket CDP (No Runtime.enable)"]
        B --> C["Turnstile Auto-Solves (~3s)"]
        C --> D["Hook window.fetch & Capture turnstile_token"]
    end

    subgraph Proxy["2. Rotating Proxy Lane (<1KB HTTP)"]
        D --> E["Python urllib POST /register (900B JSON)"]
        E --> F["Rotating Residential Proxy"]
    end
```

---

## 4. Step-by-Step Implementation Guide

### Step 1: Environment Setup (`google-chrome-stable` + `Xvfb` + Mesa)

On an Ubuntu/Debian server, install the official branded Google Chrome binary, Xvfb virtual framebuffer, and Mesa GL drivers:

```bash
curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb
sudo apt-get update
sudo apt-get install -y /tmp/chrome.deb xvfb libgl1-mesa-dri mesa-vulkan-drivers
```

### Step 2: Zero-Dependency Raw CDP Client (Without `Runtime.enable`)

Instead of importing Playwright or Puppeteer, launch `google-chrome-stable` on an `Xvfb` display with `--remote-debugging-port` and connect to the page target's `webSocketDebuggerUrl` using Python's standard library (`socket`, `struct`, `json`):

```python
def cdp_eval(sock, msg_id: int, expr: str, await_promise: bool = False):
    # Note: Runtime.enable is NEVER called!
    ws_send(sock, {
        "id": msg_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
        },
    })
    while True:
        msg = ws_recv(sock)
        if msg.get("id") == msg_id:
            return msg.get("result", {}).get("result", {}).get("value")
```

### Step 3: Closed Shadow DOM Clicking & Pre-Submit Promise Unblocking

1. **Closed Shadow DOM Fallback Click:**
   Turnstile mounts its `<iframe>` inside a **Closed Shadow Root** (`#shadow-root (closed)`), so `document.querySelector('iframe')` from the main page will not see it. If Turnstile asks for a checkbox click instead of auto-solving, locate the host container relative to the submit button (`btn.previousElementSibling.getBoundingClientRect()`) and dispatch a native mouse click at `(box.x + 28, box.y + box.height / 2)`:
   ```python
   for ev in ("mouseMoved", "mousePressed", "mouseReleased"):
       cdp_call(sock, msg_id, "Input.dispatchMouseEvent", {
           "type": ev,
           "x": box["x"] + 28,
           "y": box["y"] + box["h"] / 2,
           "button": "left" if ev != "mouseMoved" else "none",
           "clickCount": 1 if ev != "mouseMoved" else 0,
       })
   ```
2. **Unblocking Pre-Submit Consent Gates:**
   Many React/Next.js frontends gate form submission behind an async consent/attribution promise (e.g., `await window.attribution?.ready`) that only resolves after clicking `Decline` or `Allow` on a lazy-mounted banner. Always dismiss consent banners **immediately before** triggering the form submit.

### Step 4: Split-Lane Token Extraction (Fast Direct Turnstile + Rotating Proxy POST)

Running a full Chrome browser through a rotating residential proxy is slow and brittle: loading 10–15 MB of Next.js bundles and Cloudflare WASM blobs over a proxy often triggers `ERR_CONNECTION_RESET` or times out.

At the same time, most backends verifying Turnstile tokens via `https://challenges.cloudflare.com/turnstile/v0/siteverify` only validate `{secret, response: turnstile_token}` and do **not** enforce strict IP binding (since mobile and dual-stack IPv4/IPv6 users frequently change egress IPs between page load and form submission).

**The Split-Lane Pattern:**
1. **Direct Lane (Chrome):** Run Chrome on the server's fast direct connection (no proxy). Wait ~3 seconds for Turnstile to solve (`submitBtn.disabled === false`).
2. **In-Page `window.fetch` Interception:** Before clicking submit, override `window.fetch` inside the page to capture the outgoing registration JSON payload (containing `turnstile_token`) **without sending the request from the server's IP**:
   ```javascript
   window.__capturedPayload = null;
   window.fetch = async (url, init) => {
     if (String(url).includes('/auth/register') && init && init.body) {
       window.__capturedPayload = init.body;
     }
     return new Response(JSON.stringify({ status: 'ok' }), {
       status: 202,
       headers: { 'content-type': 'application/json' },
     });
   };
   ```
3. **Proxy Lane (Python `urllib`):** Read `window.__capturedPayload` via `cdp_eval`, terminate Chrome and `Xvfb` immediately, and send the `<1 KB` JSON `POST` through your rotating residential proxy.
   - If the proxy connection resets before reaching the server (`status == 0`), rotate the proxy and **reuse the exact same `turnstile_token`** (since it was never sent to the server).

---

## 5. Repository Structure & Running the Examples

```text
.
├── README.md
├── LICENSE
└── examples/
    ├── 01_raw_cdp_client.py        # Minimal zero-dependency WebSocket + CDP client
    ├── 02_xvfb_turnstile_solver.py # Headed Chrome + Xvfb Turnstile solver & screenshot tool
    └── 03_hybrid_split_lane.py     # Full Split-Lane pattern (direct Turnstile + proxy POST)
```

### Quick Start

```bash
# 1. Test raw CDP evaluation without Runtime.enable
python3 examples/01_raw_cdp_client.py --url "https://example.com"

# 2. Solve Turnstile on a target signup page and save a verification screenshot
python3 examples/02_xvfb_turnstile_solver.py \
  --url "https://your-target.example.com/sign-up" \
  --screenshot /tmp/turnstile_solved.png

# 3. Run the Split-Lane pattern (Direct Chrome token capture -> Rotating Proxy POST)
python3 examples/03_hybrid_split_lane.py \
  --signup-url "https://your-target.example.com/sign-up" \
  --register-api "https://your-target.example.com/api/v1/auth/register" \
  --proxy "http://127.0.0.1:8080"
```

---

## Disclaimer

This repository is provided for educational purposes, security research, and authorized end-to-end testing of web applications.
