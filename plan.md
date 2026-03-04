# XDM Agent — Architecture Plan

An AI agent that connects to an X/Twitter account, responds to DMs automatically with a
configurable personality, delivers paid content (images, video, voice notes via
Bunny.net), and gates continued chat access behind X402 crypto payments.

---

## Confirmed Decisions

| Decision | Choice |
|----------|--------|
| X Automation | PinchTab (browser automation, stealth) |
| Backend | Supabase (Postgres + Auth + Edge Functions + Realtime) |
| Runtime | Node.js + TypeScript |
| LLM Provider | OpenRouter (model-agnostic, swap without code changes) |
| Media Storage | Bunny.net (Stream for video, Storage for images/audio) |
| Content Delivery | Signed Bunny.net URLs (not DM file attachments) |
| Payments | X402 (USDC on Base chain) |
| Scale scope (MVP) | Single X account |

---

## Platform Choice

The X adapter layer is intentionally abstracted
behind an interface so it can be swapped to the official Twitter API v2
(which has DM endpoints) without touching any other component.

---

## System Architecture

```
                          ┌─────────────────────────────────┐
                          │           VPS (Ubuntu 22.04)     │
                          │                                  │
  X / Twitter ────────────┤  PinchTab (Go, port 9000)       │
  DM inbox/outbox         │  Chrome + stealth profile        │
                          │         ↕ HTTP API               │
                          │                                  │
                          │  Agent Core (Node.js/TS)         │
                          │  ├── DM Poller (30–60s interval) │
                          │  ├── Paywall Guard               │
                          │  ├── OpenRouter LLM Client       │
                          │  ├── Tool Executor               │
                          │  └── Supabase Client             │
                          │                                  │
                          │  Webhook Server (Hono/Express)   │
                          │  └── X402 payment confirmations  │
                          │                                  │
                          │  Admin Panel (Next.js, port 3001)│
                          └──────────────┬──────────────────┘
                                         │
                    ┌────────────────────┼──────────────────┐
                    ▼                    ▼                   ▼
             Supabase              Bunny.net            X402 / Base
             (Postgres,            (Stream +            (USDC payments,
              Auth, Edge           Storage,              on-chain
              Functions,           signed URLs)          verification)
              Realtime)
```

---

## Component Detail

### 1. X Adapter (PinchTab)

PinchTab runs as a daemon with a persistent Chrome profile (logged-in X session
survives restarts). The agent core calls its HTTP API.

**Swappable interface:**
```typescript
interface XAdapter {
  getNewDMs(since: Date): Promise<DM[]>
  sendDM(toUserId: string, text: string): Promise<void>
}

// PinchTab implementation
class PinchTabAdapter implements XAdapter { ... }

// Future: official API implementation
class TwitterAPIAdapter implements XAdapter { ... }
```

Polling runs every 30–60 seconds. A `poll_state` table stores the last-seen DM
cursor per account so restarts don't reprocess old messages.

---

### 2. Agent Core

**Main loop (per poll tick):**
```
1. getNewDMs(lastPolledAt)
2. For each DM:
   a. Upsert x_user record
   b. Paywall check:
      - If user.free_messages_used < config.free_message_limit → ALLOW
      - Elif user.chat_access_until > now() → ALLOW (paid session active)
      - Else → send payment prompt, STOP
   c. Load last N messages from Supabase (rolling context window)
   d. Build system prompt (personality + context files)
   e. Call OpenRouter → receive text + optional tool calls
   f. Execute tool calls:
      - generate_payment_link → create X402 payment page URL
      - deliver_content(item_id) → generate signed Bunny.net URL → send DM
      - check_payment(user_id, item_id) → query Supabase payments table
   g. Send reply via XAdapter
   h. Persist messages to Supabase
   i. Increment user.free_messages_used
```

**LLM tools schema:**
```typescript
const tools = [
  {
    name: "deliver_content",
    description: "Send a signed download/stream link for a content item",
    parameters: {
      item_id: { type: "string" },
    },
  },
  {
    name: "generate_payment_link",
    description: "Create an X402 payment page URL for content or chat access",
    parameters: {
      item_type: { type: "string", enum: ["content", "chat"] },
      item_id: { type: "string", description: "content item ID, or 'session' for chat" },
    },
  },
  {
    name: "check_payment",
    description: "Check whether user has paid for a specific item",
    parameters: {
      item_type: { type: "string", enum: ["content", "chat"] },
      item_id: { type: "string" },
    },
  },
];
```

---

### 3. Personality & Context System

Stored in Supabase (editable via Admin Panel). Assembled into one system prompt
at agent startup and hot-reloaded when updated.

**`agent_config` table fields:**
- `system_prompt` — core personality, tone, rules
- `context_about` — who the persona is
- `context_faq` — common Q&A
- `context_offers` — what's for sale, prices, descriptions
- `context_style` — vocabulary, hard limits, phrases to avoid
- `free_message_limit` — how many free DMs before paywall
- `chat_session_price_usd` — cost for paid chat access
- `chat_session_hours` — duration of paid access
- `model` — OpenRouter model slug (e.g. `anthropic/claude-sonnet-4-6`)
- `max_context_messages` — rolling window size (e.g. 20)

---

### 4. Media Storage (Bunny.net)

**Two Bunny services used:**

| Content Type | Bunny Service | Delivery |
|--------------|--------------|---------|
| Images | Bunny Storage | Signed URL (time-limited) |
| Voice notes | Bunny Storage | Signed URL (time-limited) |
| Video | Bunny Stream | Signed Stream URL or embed link |

**Signed URL generation** (server-side only, never exposed to client):
```typescript
// Bunny Storage signed URL
function signedStorageUrl(path: string, expirySeconds = 3600): string {
  const expiry = Math.floor(Date.now() / 1000) + expirySeconds;
  const token = sha256(`${BUNNY_SIGNING_KEY}${path}${expiry}`);
  return `https://${BUNNY_CDN_HOSTNAME}${path}?token=${token}&expires=${expiry}`;
}

// Bunny Stream signed URL
function signedStreamUrl(videoId: string, expirySeconds = 3600): string {
  const expiry = Math.floor(Date.now() / 1000) + expirySeconds;
  const token = sha256(`${BUNNY_STREAM_KEY}${videoId}${expiry}`);
  return `https://iframe.mediadelivery.net/embed/${BUNNY_LIBRARY_ID}/${videoId}?token=${token}&expires=${expiry}`;
}
```

The agent DMs a short URL pointing to your VPS (`/view/item-001?user=...`).
The VPS server verifies payment, then redirects to the Bunny signed URL.
The Bunny URL itself is never exposed directly — the redirect server is
the access gate.

---

### 5. Payment System (X402)

X402 is an HTTP 402-based micropayment protocol using USDC on Base chain.

**Payment flow:**

```
User receives DM:
"Here's your link: https://yourdomain.com/pay/item-001?uid=<x_user_id>"

User visits link:
  → VPS serves a payment page
  → Shows: amount, USDC address on Base, QR code

User pays on-chain:
  → Webhook / on-chain polling detects payment
  → Supabase Edge Function marks payment confirmed
  → If chat: sets user.chat_access_until = now() + session_hours
  → If content: creates a content_access record

User returns to link (or agent sends confirmation DM):
  → Server checks payment status → confirmed
  → Redirects to signed Bunny.net URL (content)
  → Or: agent DM confirms chat is now unlocked
```

**X402 server implementation** (using `x402-express` npm package):
```typescript
import { paymentMiddleware } from "x402-express";

app.use(
  "/pay/:itemId",
  paymentMiddleware({
    facilitatorUrl: "https://x402.org/facilitator",
    payments: (req) => ({
      scheme: "exact",
      network: "base-mainnet",
      maxAmountRequired: getItemPriceInUSDC(req.params.itemId),
      resource: req.originalUrl,
      description: getItemDescription(req.params.itemId),
      mimeType: "text/html",
      payTo: AGENT_WALLET_ADDRESS,
      maxTimeoutSeconds: 300,
      asset: USDC_CONTRACT_ADDRESS_BASE,
      extra: { uid: req.query.uid },
    }),
  }),
  handlePaymentConfirmed
);
```

---

### 6. Supabase Schema

```sql
-- X users who have DM'd the agent
CREATE TABLE x_users (
  id TEXT PRIMARY KEY,                    -- X user ID (numeric string)
  username TEXT,
  display_name TEXT,
  free_messages_used INTEGER DEFAULT 0,
  chat_access_until TIMESTAMPTZ,          -- NULL = no paid access
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ
);

-- Conversation history (LLM context)
CREATE TABLE messages (
  id BIGSERIAL PRIMARY KEY,
  x_user_id TEXT NOT NULL REFERENCES x_users(id),
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  tool_calls JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX messages_user_created ON messages(x_user_id, created_at DESC);

-- Content library
CREATE TABLE content_items (
  id TEXT PRIMARY KEY,                    -- slug, e.g. 'pack-01-video'
  type TEXT NOT NULL CHECK (type IN ('image', 'video', 'voice')),
  title TEXT NOT NULL,
  description TEXT,
  bunny_path TEXT NOT NULL,               -- Storage path or Stream video ID
  price_usd NUMERIC(10,2) NOT NULL,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Payment records
CREATE TABLE payments (
  id TEXT PRIMARY KEY,
  x_user_id TEXT NOT NULL REFERENCES x_users(id),
  item_type TEXT NOT NULL CHECK (item_type IN ('chat', 'content')),
  item_id TEXT,
  amount_usd NUMERIC(10,2),
  usdc_amount NUMERIC(20,6),
  wallet_address TEXT,
  tx_hash TEXT,
  status TEXT DEFAULT 'pending' CHECK (status IN ('pending','confirmed','expired')),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  confirmed_at TIMESTAMPTZ
);
CREATE INDEX payments_user ON payments(x_user_id, status);

-- Content access grants (after payment confirmed)
CREATE TABLE content_access (
  x_user_id TEXT NOT NULL REFERENCES x_users(id),
  item_id TEXT NOT NULL REFERENCES content_items(id),
  granted_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (x_user_id, item_id)
);

-- Agent configuration (one row for MVP single-account)
CREATE TABLE agent_config (
  id TEXT PRIMARY KEY DEFAULT 'default',
  x_account_handle TEXT,
  model TEXT DEFAULT 'anthropic/claude-sonnet-4-6',
  system_prompt TEXT,
  context_about TEXT,
  context_faq TEXT,
  context_offers TEXT,
  context_style TEXT,
  free_message_limit INTEGER DEFAULT 3,
  chat_session_price_usd NUMERIC(10,2),
  chat_session_hours INTEGER DEFAULT 24,
  max_context_messages INTEGER DEFAULT 20,
  active BOOLEAN DEFAULT TRUE,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- DM poll cursor
CREATE TABLE poll_state (
  agent_id TEXT PRIMARY KEY DEFAULT 'default',
  last_polled_at TIMESTAMPTZ,
  last_dm_id TEXT
);
```

---

### 7. Supabase Edge Functions

| Function | Trigger | Purpose |
|----------|---------|---------|
| `x402-webhook` | HTTP POST | Receive X402 payment confirmation, mark payment confirmed, update user access |
| `generate-bunny-url` | HTTP GET (internal) | Create signed Bunny.net URL for confirmed payments |
| `content-gate` | HTTP GET | Redirect endpoint — verify payment then redirect to Bunny URL |

---

### 8. Admin Panel (Next.js)

Deployed alongside agent core on VPS. Protected by Supabase Auth.

**Routes:**
```
/               → Dashboard (active users, messages today, revenue)
/config         → Edit system prompt + context fields + pricing
/content        → Upload/manage content library (drag-drop → Bunny upload)
/conversations  → Browse DM history per user
/payments       → Payment log (pending / confirmed)
```

**Content upload flow:**
1. Admin drags file onto `/content` panel
2. Panel uploads directly to Bunny Storage via presigned upload URL
3. Panel saves metadata (type, title, description, price) to Supabase
4. Content item is immediately available for the LLM to offer

---

### 9. Project Structure

```
xdm-agent/
├── packages/
│   ├── agent-core/          ← Main Node.js daemon
│   │   ├── src/
│   │   │   ├── index.ts             ← Entry point, poll loop
│   │   │   ├── adapters/
│   │   │   │   ├── pinchtab.ts      ← PinchTab XAdapter impl
│   │   │   │   └── twitter-api.ts   ← Future: official API impl
│   │   │   ├── llm/
│   │   │   │   ├── client.ts        ← OpenRouter client
│   │   │   │   ├── tools.ts         ← Tool definitions + handlers
│   │   │   │   └── prompt.ts        ← System prompt assembly
│   │   │   ├── paywall.ts           ← Paywall guard logic
│   │   │   ├── payments/
│   │   │   │   ├── x402.ts          ← X402 payment page server
│   │   │   │   └── bunny.ts         ← Signed URL generation
│   │   │   └── db/
│   │   │       └── supabase.ts      ← Typed Supabase client
│   │   ├── package.json
│   │   └── tsconfig.json
│   │
│   └── admin/               ← Next.js admin panel
│       ├── app/
│       │   ├── (dashboard)/
│       │   ├── config/
│       │   ├── content/
│       │   ├── conversations/
│       │   └── payments/
│       └── package.json
│
├── supabase/
│   ├── migrations/          ← SQL schema migrations
│   └── functions/
│       ├── x402-webhook/
│       ├── content-gate/
│       └── generate-bunny-url/
│
├── infra/
│   ├── pinchtab.service     ← systemd unit
│   ├── xdm-agent.service    ← systemd unit
│   └── nginx.conf
│
├── .env.example
├── package.json             ← Workspace root (pnpm workspaces)
└── PLAN.md
```

---

### 10. Environment Variables

```bash
# PinchTab
PINCHTAB_URL=http://localhost:9000
PINCHTAB_PROFILE_DIR=/opt/xdm-agent/profiles

# Supabase
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=

# OpenRouter
OPENROUTER_API_KEY=

# Bunny.net
BUNNY_STORAGE_API_KEY=
BUNNY_STORAGE_ZONE=
BUNNY_CDN_HOSTNAME=          # e.g. your-zone.b-cdn.net
BUNNY_SIGNING_KEY=           # for signed URL token
BUNNY_STREAM_LIBRARY_ID=
BUNNY_STREAM_API_KEY=

# X402
X402_FACILITATOR_URL=https://x402.org/facilitator
AGENT_WALLET_ADDRESS=        # USDC on Base recipient address
USDC_CONTRACT_BASE=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913

# Admin panel
NEXTAUTH_SECRET=
ADMIN_EMAIL=
```

---

## Build Phases

### Phase 1 — Core DM Loop
- [ ] PinchTab running, Chrome profile logged into X
- [ ] `agent-core` polls DMs, builds prompt, calls OpenRouter, sends reply
- [ ] Messages persisted to Supabase
- [ ] Free message limit enforced
- [ ] Deployed on VPS with systemd, Nginx

### Phase 2 — Paywall (Chat)
- [ ] X402 payment page server (`/pay/chat?uid=...`)
- [ ] On payment: Supabase Edge Function marks user chat access
- [ ] Agent resumes replies after payment confirmed
- [ ] Payment DM prompt auto-sent when limit hit

### Phase 3 — Content Library & Sales
- [ ] Bunny.net Storage + Stream zones configured
- [ ] Content items table + signed URL generation
- [ ] LLM tools: `deliver_content`, `generate_payment_link`, `check_payment`
- [ ] Content gate redirect (verify payment → Bunny signed URL)
- [ ] X402 payment page for content items

### Phase 4 — Admin Panel
- [ ] Next.js panel with Supabase Auth
- [ ] System prompt + context editing
- [ ] Content upload (drag-drop → Bunny → Supabase metadata)
- [ ] Conversation viewer, payment log

### Phase 5 — Polish & Scale
- [ ] Multi-account support (multiple PinchTab profiles + agent configs)
- [ ] Context window summarization (for long-running conversations)
- [ ] Model switching via admin panel
- [ ] Monitoring / alerting (uptime, DM response latency)

---

## Open Questions (for later)

- **Wallet UX**: How will users pay? MetaMask via the payment page? Coinbase
  Wallet? A QR code for mobile? This affects the X402 payment page UI.
- **Content pricing**: per-item or bundles/packs?
- **Voice notes**: direct Bunny Storage download or use Bunny Stream for audio
  too? (Stream supports HLS, Storage is simpler for audio files.)
- **Rate limiting**: should the agent enforce a per-user DM rate limit to avoid
  flooding if someone spams messages?
