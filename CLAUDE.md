# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

X DM Bot — a Python bot that automatically replies to X.com (Twitter) direct messages using an LLM. It uses SeleniumBase with undetected ChromeDriver to monitor the DM inbox, detects new messages, generates replies via OpenRouter, and persists conversation history in Supabase. Designed for unattended VPS deployment with crash recovery.

## Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
python src/main.py
# or on Windows:
bot_runner.bat

# VPS deployment (Ubuntu 24.04)
sudo bash deploy/setup.sh <username> [branch]
sudo systemctl start xdm
```

## Configuration

All config lives in `config.json` (auto-created from template on first run). Credentials can also be set via environment variables: `X_USERNAME`, `X_PASSWORD`, `X_PASSCODE` (4-digit encrypted DM passcode), `OPENROUTER_API_KEY`, `SUPABASE_URL`, `SUPABASE__SECRET_KEY` (double underscore), `SUPABASE_DB_URL`.

The LLM personality/system prompt is in `prompts/personality.txt`.

### Proxy (required for VPS / datacenter IPs)

X.com blocks login from datacenter IPs. Use a residential proxy relay:
1. Install `gost` on VPS: `sudo snap install gost`
2. Run relay: `gost -L=:8888 -F=http://user:pass@proxy-host:port &`
3. Set in config: `"proxy": "http://127.0.0.1:8888"`

Cookie-based session restore is also supported — export cookies locally and upload to VPS `cookies/` directory.

## Architecture

**Entry point:** `src/main.py` — `run_forever()` wraps `XAutomation` with crash recovery and exponential backoff. `XAutomation` class orchestrates everything:
1. Initializes Chrome (SeleniumBase undetected driver), Supabase client, and LLM client
2. Logs into X.com (with cookie persistence in `cookies/`)
3. Navigates to DM inbox and enters polling loop
4. Periodic Chrome restart every 6 hours for memory management

**Main loop** (`XAutomation.main_loop`):
1. Poll DM sidebar for changes
2. Open conversation in new tab
3. Fetch ALL saved messages from Supabase (for hash comparison)
4. Extract on-screen messages and find new ones via content-addressed hashing
5. Save new user messages to Supabase immediately
6. Re-fetch recent history for LLM context
7. Get LLM reply (Supabase is the single source of truth — no dual-source context)
8. Send reply, save reply with deterministic hash
9. Close tab, return to inbox

### Message Deduplication

X.com regenerates `data-testid` message IDs on every page load, so they cannot be used as stable identifiers. Instead:

- **Content-addressed hashing**: `generate_message_id(conv_id, sender, text, occurrence)` produces a deterministic SHA-256 hash from message content.
- **`normalize_text()`**: Canonical Unicode NFC normalization, strips zero-width characters, collapses whitespace. Used everywhere text is compared or stored.
- **Occurrence counting**: Handles duplicate messages (e.g., "ok" sent twice) by tracking `(sender, text)` pair occurrences.
- **Supabase as single source of truth**: LLM context is built exclusively from Supabase history — never from on-screen messages directly.

### Key modules

- `src/x_automation/dm_manager.py` — Three concerns:
  - `normalize_text()` / `generate_message_id()`: Text normalization and content-addressed hashing for dedup.
  - `DmListener`: Scrapes the DM sidebar to detect new/changed conversations by diffing current vs previous state. First poll sets baseline, then drains any unread messages from the startup queue. Uses `commit()` to mark a conversation as processed.
  - `OpenChat`: Reads messages from an opened conversation thread (scrolls to bottom first), diffs against Supabase via hash comparison, sends replies via the composer textarea. Identifies authors by CSS class (`justify-end` = assistant, `justify-start` = user).

- `src/x_automation/login.py` — `Login` handles auth with cookie save/restore (tries cookies first, falls back to username/password). Uses CDP `Input.dispatchKeyEvent` for typing (anti-detection). `HumanLikeMovement` generates Bezier-curve mouse paths for human-like interaction.

- `src/llm/llm_client.py` — `LLM` sends chat completions to OpenRouter. `get_conversation_context(chat_history)` takes Supabase messages only (single source), merges consecutive same-role messages, and prepends the system prompt from `personality.txt`. Retries on transient network errors.

- `src/storage/supabase_client.py` — `SupaBase` manages two tables (`conversations`, `messages`). Auto-creates schema via psycopg on init. Uses Supabase client (PostgREST) for CRUD, psycopg for DDL. `@_retry_on_network_error` decorator retries all DB operations with exponential backoff. `save_message()` uses upsert on `message_id` (content hash).

### Data flow

```
DM sidebar poll → detect change → open conv tab → scroll to bottom
    → extract on-screen messages → hash each message
    → compare hashes against ALL saved Supabase messages
    → save NEW user messages to Supabase immediately
    → re-fetch recent history (for LLM context window)
    → get LLM reply (single source: Supabase only)
    → send reply → save reply with deterministic hash → close tab
```

### VPS Resilience

- `run_forever()`: Crash recovery loop with exponential backoff (30s → 5min)
- `_kill_orphaned_chrome()`: Cleans up leftover Chrome processes on startup
- `_reinitialize_driver()`: Full Chrome teardown and rebuild for crash recovery
- `ensure_session()`: Detects expired sessions and re-authenticates
- Periodic Chrome restart every 6 hours to prevent memory leaks
- Per-conversation try/except: one failure doesn't crash the bot
- Failure counter: skips a conversation after 3 consecutive failures
- systemd service (`deploy/xdm.service`): auto-restart, memory/CPU limits, clean shutdown

## Important Details

- Message author detection in `OpenChat.extract_messages` relies on CSS classes (`justify-end`/`justify-start`) — these are X.com implementation details that may break on UI changes.
- `DmListener` parses `aria-description` attributes on conversation items to extract username, last message, and unread status.
- All X.com element selectors use `data-testid` attributes where possible.
- The bot opens each conversation in a new browser tab, processes it, then closes the tab and returns to the main DM list.
- Supabase table names are configurable in config but default to `conversations` and `messages`.
- Conversation isolation: all DB queries filter by `conversation_id`, ensuring conversations between different users never mix.
- First-run with empty Supabase: only processes the last user message (avoids dumping entire on-screen history).
