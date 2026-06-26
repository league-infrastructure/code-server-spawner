# Fixing noVNC connection drops / timeouts

**Audience:** an engineer or agent maintaining a service that streams a desktop/app to the browser via **noVNC** (RFB over WebSocket, usually through **websockify**), where **sessions drop and the user has to reload the page to get them back.**

This document is self-contained. You do not need any other context to act on it.

---

## TL;DR

If the symptom is *"the VNC view freezes/drops, a page reload fixes it instantly, and it happens often,"* it is **almost always an idle WebSocket timeout** — the connection is being closed by some hop in the network when no data flows (a static screen). Apply all three layers:

1. **Server heartbeat** (the real fix): run websockify with `--heartbeat=25`.
2. **Raise the proxy/load-balancer idle timeout** (so your own infra stops closing idle sockets).
3. **Client auto-reconnect** (backstop, so a drop self-heals instead of needing a reload).

Do **1 and 2** to stop the drops; keep **3** so any residual drop is invisible to the user.

---

## 1. Why this happens

VNC/RFB only transmits bytes **when the screen changes or the user interacts**. When the user pauses — reading, thinking, typing in another window — the framebuffer is static and **zero bytes flow in either direction**. Many network components treat a connection with no traffic as dead and close it:

- reverse proxies (nginx, Apache, Traefik, HAProxy) with a default idle/read timeout (often **60s**),
- cloud load balancers (AWS ALB **60s**, Cloudflare **~100s**),
- Kubernetes ingress controllers (**60s** default),
- **NAT gateways and school/corporate firewalls** — frequently the shortest and the one you don't control.

The display server (Xvnc/x11vnc/Xtightvnc) keeps running, so a **page reload opens a fresh WebSocket and it works again immediately**. That "reload fixes it" behavior is the signature of an idle-timeout drop, not a crash. How often it happens just tracks how often a user idles longer than the shortest timeout in the path.

> Key fact: **a browser cannot send WebSocket ping frames from JavaScript** (the browser WebSocket API doesn't expose ping). So you cannot keep the socket alive with client-side JS over the RFB stream. The keepalive must come from the **server side** (websockify) or a **proxy** that injects WebSocket pings. This is why the heartbeat below lives on websockify.

---

## 2. Diagnose (2 minutes, do this first)

**A. Read the WebSocket close code.** Open the browser DevTools → **Network → WS** → click the VNC socket. When it drops, note the close code:

| Close code | Meaning | Implication |
|-----------|---------|-------------|
| **1006** (abnormal, no close frame) | A network hop/intermediary killed the TCP/WebSocket | **Idle timeout / firewall** → this document's main fix |
| **1000 / 1001** (clean) | The app or server closed it on purpose | Different cause → see [§6](#6-if-the-close-is-clean-10001001) |
| **1011 / 1013** | Server error / try-later | Backend problem; check server logs |

**B. Idle test.** Connect, then **do not touch the mouse or keyboard**. If it dies at a suspiciously round time (~**60s**, ~**100s**, ~**180s**), that confirms an idle timeout and even tells you which hop (match the number to the table in [§4](#4-raise-proxy--load-balancer-idle-timeouts)).

If A shows **1006** and/or B reproduces on idle → proceed with the fix. If A shows a **clean** code → jump to [§6](#6-if-the-close-is-clean-10001001).

---

## 3. Fix layer 1 — server-side heartbeat (the real fix)

A heartbeat sends a WebSocket **ping** on a timer so the socket is **never idle**; the browser auto-replies with a pong. Nothing in the path ever sees "no traffic," so nothing closes it — **including hops you don't control, like a school firewall.**

**websockify has this built in:**

```bash
websockify --heartbeat=25 6080 localhost:5900
```

- `6080` = the port websockify listens on (your WS endpoint); `localhost:5900` = your VNC server's RFB port. Use your real values.
- **`--heartbeat=N` sends a ping every N seconds.** Set **N below the shortest idle timeout in your path.** `25` safely beats a 60s limit; use **`15`** if you suspect an aggressive firewall (<60s).

If websockify is started by **systemd**, edit the unit's `ExecStart` to add `--heartbeat=25`, then `systemctl daemon-reload && systemctl restart <unit>`.

If it's started by **supervisor**, edit the program's `command=` line and `supervisorctl reread && supervisorctl update && supervisorctl restart <prog>`.

If it's in a **container/compose**, add `--heartbeat=25` to the command/entrypoint and redeploy.

> If your `websockify` build doesn't accept `--heartbeat`, update it (recent `websockify` supports it) or see [§7](#7-if-you-are-not-using-websockify) for alternatives.

This single change resolves most "users keep reloading" reports. Still do layer 2 so your own proxy isn't independently closing sockets, and layer 3 so the rare drop is invisible.

---

## 4. Raise proxy / load-balancer idle timeouts

Even with a heartbeat, set generous timeouts so nothing in *your* infrastructure closes the socket. Find your stack:

### nginx
A WebSocket also requires the upgrade plumbing — include all of this:

```nginx
# in the http {} block (once):
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

# your VNC WebSocket route:
location /websockify/ {                 # adjust to your path
    proxy_pass http://127.0.0.1:6080;   # your websockify host:port
    proxy_http_version 1.1;             # required for WebSockets
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;   # see note below

    proxy_read_timeout 86400;           # default is 60s — the usual culprit
    proxy_send_timeout 86400;
    proxy_socket_keepalive on;          # nginx >= 1.15.6: OS TCP keepalive to upstream
}
```
Reload: `nginx -t && nginx -s reload`.

### Apache (mod_proxy_wstunnel)
```apache
ProxyTimeout 86400
# ensure the WS tunnel is proxied, e.g.:
ProxyPass        /websockify/ ws://127.0.0.1:6080/
ProxyPassReverse /websockify/ ws://127.0.0.1:6080/
```

### HAProxy
WebSockets use `timeout tunnel` after the upgrade (default falls back to the much shorter `timeout client`/`timeout server`):
```
defaults
    timeout client  1h
    timeout server  1h
    timeout tunnel  1h        # governs established WebSocket connections
```

### Traefik (v2/v3)
Disable the responding/idle timeouts on the entrypoint (default `idleTimeout` is 180s):
```yaml
entryPoints:
  websecure:
    transport:
      respondingTimeouts:
        readTimeout: 0
        idleTimeout: 0
```

### Kubernetes ingress-nginx
Annotate the Ingress:
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "86400"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "86400"
```

### AWS ALB
Raise the load balancer **idle timeout** attribute (default **60s**, max **4000s**) — Console → Load Balancer → Attributes, or:
```bash
aws elbv2 modify-load-balancer-attributes --load-balancer-arn <arn> \
  --attributes Key=idle_timeout.timeout_seconds,Value=4000
```
(With the heartbeat from §3 a 60s ALB timeout won't fire anyway, but raise it for safety. NLB/TCP doesn't have this issue.)

### Cloudflare (proxied / orange-cloud)
Cloudflare closes idle WebSockets after **~100s** and the limit isn't user-configurable — the **§3 heartbeat (≤ ~90s) is the fix.** If problems persist, either set the WS hostname to **DNS-only (grey cloud)** to bypass the CF proxy, or use **Cloudflare Spectrum**.

> **Mixed-content note (separate but common):** on an `https://` page the WebSocket URL must be `wss://`. If your edge terminates TLS but the app builds the URL as `ws://` (because it didn't see `X-Forwarded-Proto: https`), the browser blocks the socket and you get a blank/non-connecting view. Ensure the proxy sets `X-Forwarded-Proto` and the app honors it.

---

## 5. Fix layer 3 — client auto-reconnect (backstop)

So a residual drop self-heals (a brief screen blip) instead of forcing a manual reload. With noVNC's `RFB` object, reconnect on **unclean** disconnects only, with a bounded attempt count:

```js
let attempts = 0;
const MAX = 25;

function connect() {
  const rfb = new RFB(document.getElementById('screen'), wsUrl);
  rfb.scaleViewport = true;
  rfb.addEventListener('connect', () => { attempts = 0; });
  rfb.addEventListener('disconnect', (e) => {
    // reconnect only if the close was NOT clean (i.e. dropped, not user-initiated)
    if (!e.detail.clean && attempts++ < MAX) {
      setTimeout(connect, 1000);     // small backoff
    }
  });
}
connect();
```

Reconnect is a safety net, **not** a substitute for §3/§4 — without the heartbeat you'll just reconnect over and over.

---

## 6. If the close is *clean* (1000/1001)

Then something is closing it **deliberately**; a heartbeat won't help. Check, in order:

- **Application inactivity timer** — many VNC frontends disconnect "idle" sessions on purpose. Find and lengthen/disable it.
- **Server-side session cap** — a manager/orchestrator that releases sessions after N minutes.
- **The VNC server dropping the client** — e.g. x11vnc started without `-forever` exits after the first client disconnects; also avoid a short `-timeout`. Prefer `x11vnc -forever -shared`. (A persistent `Xvnc`/`Xtightvnc` display avoids this class entirely.)
- **Load balancer without sticky sessions** — if multiple backends sit behind a non-sticky LB, a rebalanced connection lands on a backend that doesn't have the session. Enable session affinity, or ensure one backend per session.

---

## 7. If you are **not** using websockify

The heartbeat must come from whatever bridges the WebSocket. Equivalents:

- **A WebSocket-aware proxy that injects pings.** Most reverse proxies do **not** send WS ping frames on their own. Some sidecars / API gateways can; check yours.
- **Go/Node VNC proxies** (e.g. custom `gorilla/websocket` bridges) — add a `time.Ticker`/`setInterval` that sends a ping frame every ~25s on the server side.
- **noVNC built-ins** — noVNC has no client-initiated ping (browser limitation). It does support a "continuous updates" path, but a server-side ping is simpler and more reliable.
- **Last resort: OS TCP keepalive** (`net.ipv4.tcp_keepalive_time` etc.) — defaults are ~2h, far too long to beat a 60s app-layer timeout, and many proxies ignore TCP keepalive for the WS layer. Use only as a supplement.

---

## 8. Verification

1. Apply §3 (`--heartbeat`) and §4 (proxy timeout); restart the relevant services.
2. In DevTools → Network → WS, confirm you see periodic small frames (the pings) while the screen is idle.
3. Connect and **leave it untouched for 5+ minutes.** It should stay live (previously it would drop near your shortest timeout).
4. Confirm a forced drop recovers on its own (kill the socket from DevTools, or restart the proxy) thanks to §5 — no manual reload.

---

## 9. Checklist

- [ ] Reproduced the drop on **idle**; close code is **1006** (not a clean 1000/1001).
- [ ] websockify (or your WS bridge) running with **`--heartbeat=25`** (≤ shortest path timeout).
- [ ] Proxy/LB **idle/read timeout raised** for the WS route (nginx `proxy_read_timeout`, ALB idle timeout, etc.).
- [ ] WebSocket **upgrade headers** present (HTTP/1.1, `Upgrade`, `Connection`).
- [ ] On HTTPS, URL is **`wss://`** and edge sends **`X-Forwarded-Proto`**.
- [ ] Client **auto-reconnects** on unclean disconnect (bounded).
- [ ] Verified: idle for 5+ minutes with no drop; forced drop self-heals.

---

### One-line summary for the fix

> Run websockify with `--heartbeat=25`, raise the reverse-proxy/LB idle timeout (e.g. nginx `proxy_read_timeout 86400`), and add a bounded client-side auto-reconnect. The heartbeat is the part that matters most — it keeps the WebSocket from ever looking idle, which is what was getting it killed.
