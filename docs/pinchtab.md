# PinchTab Reference

PinchTab is a 12MB Go binary that wraps Chrome with an accessibility-first HTTP API
designed for AI agents. Exposes the accessibility tree (not screenshots).

## Default port: 9867
Configured via `PINCHTAB_URL` env var. Binds to `127.0.0.1` by default.
For remote access set `BRIDGE_BIND=0.0.0.0` + `BRIDGE_TOKEN`.

## Binary location on this VPS
```
/home/ben/.npm-global/bin/pinchtab          ← wrapper (in PATH)
/home/ben/.pinchtab/bin/0.7.6/pinchtab-linux-amd64  ← versioned binary
```
`PINCHTAB_BINARY_PATH` env var tells the wrapper which versioned binary to use.

---

## CRITICAL: Daemon mode vs Instances API

**When PinchTab runs as a systemd daemon, it exposes BARE top-level endpoints — NOT tab/instance-prefixed ones.**

```
POST /navigate     ✅  works in daemon mode
GET  /snapshot     ✅  works in daemon mode
POST /action       ✅  works in daemon mode
POST /evaluate     ✅  works in daemon mode

POST /tabs/{id}/navigate   ❌  returns 404 for daemon-mode tab IDs
GET  /instances            ❌  returns [] in daemon mode
```

The tab ID returned by `GET /tabs` in daemon mode is a raw CDP ID
(e.g. `448298D8DA9B36E4AC830D348B93AF0A`) — this does NOT work with `/tabs/{id}/...` endpoints.

**Adapter must use bare endpoints.** See [packages/agent-core/src/adapters/pinchtab.ts](../packages/agent-core/src/adapters/pinchtab.ts).

---

## Dashboard

- URL: `http://localhost:9867/dashboard` (NOT the root `/` — that 404s)
- SSH tunnel to access locally: `ssh -L 9867:localhost:9867 ben@VPS_IP`
- Required env vars for full dashboard: `BRIDGE_MODE=dashboard`, `BRIDGE_STEALTH=full`
- UI bug: warning "profile management requires BRIDGE_MODE=dashboard" shows even when the
  env var IS set. Verify with: `cat /proc/$(pgrep -f pinchtab)/environ | tr '\0' '\n' | grep BRIDGE`
- Profile creation via dashboard UI silently fails in bridge mode — use API instead

---

## Profile API

```bash
GET    /profiles              # list all — returns [{id, name, diskUsage, running, ...}]
POST   /profiles              # create   {"name":"x-account","description":"...","useWhen":"..."}
POST   /profiles/import       # import existing Chrome dir: {"name":"..","sourcePath":"/path/to/chrome-profile"}
GET    /profiles/{id}
PATCH  /profiles/{id}
DELETE /profiles/{id}
POST   /profiles/{id}/reset   # clear cookies/cache/storage
GET    /profiles/{id}/logs
GET    /profiles/{id}/analytics
```

Profile IDs are 12-char hex hashes (e.g. `77b2ff2f665e`).
Profile data stored at `~/.pinchtab/profiles/`.
Daemon default Chrome profile stored at `~/.pinchtab/chrome-profile/`.

### Import gotcha
When importing from `~/.pinchtab/chrome-profile`, Chrome may have cleaned up
`SingletonCookie` after an unclean exit. Fix:
```bash
touch /home/ben/.pinchtab/chrome-profile/SingletonCookie
curl -X POST http://localhost:9867/profiles/import \
  -H "Content-Type: application/json" \
  -d '{"name":"x-account","sourcePath":"/home/ben/.pinchtab/chrome-profile"}'
```

---

## Instance API

```bash
GET    /instances                     # list (returns [] in daemon mode — use /profiles instead)
POST   /instances/start               # {"profileId":"77b2ff2f665e","mode":"headless"}
POST   /instances/{id}/stop
GET    /instances/{id}/logs
```

Modes: `headless` (default) | `headed` (requires Xvfb on VPS for visual display)

### Starting an instance with the x-account profile
```bash
curl -X POST http://localhost:9867/instances/start \
  -H "Content-Type: application/json" \
  -d '{"profileId":"77b2ff2f665e","mode":"headless"}'
```

---

## Tabs API (only works for API-created instances, NOT daemon-mode tabs)

```bash
POST /instances/{id}/tabs/open        # {"url":"https://x.com"}  → {"tabId":"tab_abc123"}
GET  /tabs                            # returns {"tabs":[...]}  NOT a flat array
GET  /tabs/{id}/snapshot?interactive=true&compact=true
POST /tabs/{id}/navigate              # {"url":"...","timeout":20}
POST /tabs/{id}/evaluate              # {"expression":"document.title"}
POST /tabs/{id}/action                # {"kind":"click","ref":"e5"}
POST /tabs/{id}/cookies
```

---

## Bare endpoints (daemon mode — what we actually use)

```bash
POST /navigate    {"url":"https://x.com/messages","timeout":20}
GET  /snapshot    ?interactive=true&compact=true
GET  /snapshot    ?compact=true
POST /evaluate    {"expression":"...JS..."}
POST /action      {"kind":"click|type|press|fill|hover|scroll","ref":"e5","text":"...","key":"..."}
GET  /health      → {"cdp":"","status":"ok","tabs":1}
```

### Snapshot format
```
{"count":23,"nodes":[{"ref":"e0","role":"RootWebArea","name":"..."},{"ref":"e1",...},...]}
```
Or compact text format:
```
e0:link "View keyboard shortcuts"
e5:textbox "Phone, email, or username"
e6:button "Next"
```
Parse with: `/^(e\d+):(\w+)\s+"?([^"]*)"?/`

---

## Logging into X via the API (step by step)

When the session is lost, log in via API actions using the snapshot element refs.

```bash
# 1. Navigate to messages (redirects to login)
curl -X POST http://localhost:9867/navigate -d '{"url":"https://x.com/messages","timeout":20}'
sleep 5

# 2. Snapshot to find the username field ref
curl -s "http://localhost:9867/snapshot?compact=true"
# Look for: e??:textbox "Phone, email, or username"  → note the ref (e.g. e22)
#           e??:button "Next"                         → note the ref (e.g. e6)

# 3. Type username
curl -X POST http://localhost:9867/action -d '{"kind":"type","ref":"e22","text":"YOUR_USERNAME"}'

# 4. Click Next
curl -X POST http://localhost:9867/action -d '{"kind":"click","ref":"e6"}'
sleep 3

# 5. Snapshot again to find password field
curl -s "http://localhost:9867/snapshot?compact=true"
# Look for: e??:textbox "Password"  and  e??:button "Log in"

# 6. Type password
curl -X POST http://localhost:9867/action -d '{"kind":"type","ref":"eXX","text":"YOUR_PASSWORD"}'

# 7. Click Log in
curl -X POST http://localhost:9867/action -d '{"kind":"click","ref":"eXX"}'
sleep 5

# 8. Verify logged in
curl -s "http://localhost:9867/snapshot?compact=true" | head -5
# Should show "Messages / X" not "Log in to X"
```

**If 2FA is required:** snapshot will show a code input field. Get the code from
email/authenticator, type it with an action, then click Confirm.

---

## systemd service (pinchtab.service)

```ini
[Unit]
Description=PinchTab browser automation daemon
After=network.target

[Service]
Type=simple
User=ben
WorkingDirectory=/home/ben
Environment=PINCHTAB_BINARY_PATH=/home/ben/.pinchtab/bin/0.7.6/pinchtab-linux-amd64
ExecStart=/home/ben/.npm-global/bin/pinchtab
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**Session survival:** X session cookies are saved in the Chrome profile and survive
service restarts. If they don't (unclean exit clears sessions), re-login via API or
re-import the profile.

**To add dashboard support to the service** (optional, for debugging):
```ini
Environment=BRIDGE_MODE=dashboard
Environment=BRIDGE_STEALTH=full
```

---

## X profile on this VPS

- Profile name: `x-account`
- Profile ID: `77b2ff2f665e`
- Imported from: `/home/ben/.pinchtab/chrome-profile`

---

## How we use PinchTab in xdm

The adapter in [packages/agent-core/src/adapters/pinchtab.ts](../packages/agent-core/src/adapters/pinchtab.ts)
uses **bare endpoints only** (daemon mode):

- `POST /navigate` → go to x.com/messages or a specific conversation
- `POST /evaluate` → JS to extract conversation list and message content
- `GET /snapshot` → get element refs for clicking/typing
- `POST /action` → click, type, press to send messages

**`fromUserId` convention:** X conversation IDs (e.g. `"123456-789012"`) stored as `x_users.id`.
For replies: if ID contains `-` → navigate to `/messages/{id}`, else use compose URL.

**Key X DOM selectors:**
- `[data-testid="conversation"]` — conversation list items
- `[data-testid="messageEntry"]` — individual messages
- `[data-testid="sent-message"]` — ancestor marks outgoing messages
- `[data-testid="tweetText"]` — message text
- `div[dir="auto"]` — fallback for message text

---

## Headed mode on VPS (for visual login)

```bash
sudo apt-get install -y xvfb
Xvfb :99 -screen 0 1920x1080x24 &
DISPLAY=:99 BRIDGE_MODE=dashboard BRIDGE_STEALTH=full \
  PINCHTAB_BINARY_PATH=/home/ben/.pinchtab/bin/0.7.6/pinchtab-linux-amd64 \
  /home/ben/.npm-global/bin/pinchtab &
```
Then SSH tunnel and open `http://localhost:9867/dashboard`.
The dashboard shows a live screencast but requires a `headed` instance to be started.
