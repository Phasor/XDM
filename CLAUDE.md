# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

X DM Bot — a Python bot that automatically replies to X.com (Twitter) direct messages using an LLM. It uses SeleniumBase with undetected ChromeDriver to monitor the DM inbox, detects new messages, generates replies via OpenRouter, and persists conversation history in Supabase.

## Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Run (working directory must be the repo root — imports use src/ as the package root)
python src/main.py

# On Windows:
bot_runner.bat
```

There are no tests. There is no linter or formatter configured. The project has no `pyproject.toml` or `setup.py`.

## Configuration

All config lives in `config.json` (auto-created from `config.template.json` on first run). Credentials can also be set via environment variables, which take precedence over config values:

| Env Variable | Config Path | Description |
|---|---|---|
| `X_USERNAME` | `x.com.username` | X.com login username |
| `X_PASSWORD` | `x.com.password` | X.com login password |
| `X_PASSCODE` | `x.com.passcode` | 4-digit encrypted DM passcode (validated at startup) |
| `OPENROUTER_API_KEY` | `openrouter.api_key` | OpenRouter API key |
| `SUPABASE_URL` | `supabase.project_url` | Supabase project URL |
| `SUPABASE__SECRET_KEY` | `supabase.secret_key` | Supabase service key (note: double underscore in env var) |
| `SUPABASE_DB_URL` | `supabase.db_url` | Direct PostgreSQL connection string (used for DDL) |

The LLM personality/system prompt is in `prompts/personality.txt`.

**Note:** `config.json` is gitignored. `config.template.json` is the checked-in template with empty credentials and sensible defaults (model: `openai/gpt-3.5-turbo`, polling interval: 5s, context limit: 20 messages).

## File Structure

```
src/
  main.py                      # Entry point — Chrome, XAutomation, config loading
  x_automation/
    dm_manager.py              # DmListener (inbox polling) + OpenChat (read/send messages)
    login.py                   # Login flow + HumanLikeMovement (anti-detection)
  llm/
    llm_client.py              # OpenRouter API client, context building
  storage/
    supabase_client.py         # Supabase + psycopg storage layer
prompts/
  personality.txt              # LLM system prompt
deploy/
  setup.sh                     # Ubuntu 24.04 VPS setup script (Chrome, venv, systemd)
  xdm.service                  # systemd unit file (uses xvfb-run for headless)
config.template.json           # Config template (checked in)
bot_runner.bat                 # Windows launcher
requirements.txt               # Pinned dependencies
```

## Architecture

**Entry point:** `src/main.py`

- `Chrome` class: initializes SeleniumBase undetected driver with proxy, window size/position, and cache-disabled flags.
- `XAutomation` class: orchestrates the full lifecycle:
  1. Loads config (creates `config.json` from `CONFIG_TEMPLATE` if missing)
  2. Initializes Supabase client, LLM client, Chrome driver
  3. Logs into X.com (with cookie persistence)
  4. Navigates to DM inbox, enters passcode if prompted
  5. Runs `main_loop()` — the polling loop

**Main loop** (`XAutomation.main_loop`): polls DM list for changes → opens new tab for conversation → reads new messages → sends to LLM → types reply → saves to Supabase → closes tab.

### Key Modules

**`src/x_automation/dm_manager.py`** — Two classes:

- `DmListener`: scrapes the DM sidebar to detect new/changed conversations by diffing current vs previous state. First poll sets baseline, but also processes any unread messages from while the bot was offline. Uses `commit(conv_id)` to mark a single conversation as processed (updates `prev_chats`). Detects message author via `<span>` text ("You:" prefix = assistant).
- `OpenChat`: reads messages from an opened conversation thread, sends replies via the composer textarea. Identifies authors by CSS class (`justify-end` = assistant, `justify-start` = user). `read_messages(latest_msg_id)` returns only messages newer than the given ID. `send_message()` uses the standard `send_keys` method (not CDP typing).

**`src/x_automation/login.py`** — Two classes:

- `Login`: handles auth with optional cookie save/restore (`SAVE_COOKIES` constant, currently `False`). Uses CDP `Input.dispatchKeyEvent` for typing username/password (anti-detection). Env vars override config for credentials.
- `HumanLikeMovement`: generates Bézier-curve mouse paths via CDP `Input.dispatchMouseEvent` for human-like interaction. Used during login only.

**`src/llm/llm_client.py`** — `LLM` class:

- Sends chat completions to OpenRouter via `requests.post`.
- `get_conversation_context()` merges Supabase history + new messages, combines consecutive same-role messages into one, and prepends system prompt from `personality.txt`.
- Returns `None` on any request error (caller skips reply).

**`src/storage/supabase_client.py`** — `SupaBase` class:

- Manages two tables (`conversations`, `messages`) with auto-creation via `psycopg` (DDL).
- Uses Supabase PostgREST client for all CRUD operations.
- `messages` table has a foreign key to `conversations` on `conversation_id`, with `CASCADE` delete.
- `message_id` (from X.com's `data-testid`) is stored as `UNIQUE` — this is used to detect which messages are new.
- `context_message_limit` (default 20) limits how many historical messages are fetched for LLM context.

### Data Flow

```
DM sidebar poll → detect change → open conv tab → enter passcode if needed
    → read messages (Selenium) → fetch history (Supabase)
    → build context + get LLM reply (OpenRouter)
    → type reply (Selenium) → save all new messages + reply (Supabase)
    → close tab → return to inbox
```

### Error Handling & Resilience

- `main_loop` catches all exceptions per-conversation and continues to the next poll cycle.
- `ensure_session()` checks for a login button on each idle poll and re-authenticates if the session has expired.
- Tab cleanup in `finally` block handles cases where the conversation tab fails to open or process.
- The bot navigates back to the chat URL on `WebDriverException` during DM detection.

## Deployment

VPS deployment (Ubuntu 24.04) is supported via:

- `deploy/setup.sh` — installs Chrome, Python, clones repo, creates venv, sets up systemd service. Run as root: `sudo bash deploy/setup.sh [username]`.
- `deploy/xdm.service` — systemd unit that runs the bot under `xvfb-run` for headless operation. Restarts on failure with 30s delay. Placeholders `__APP_USER__` and `__APP_DIR__` are replaced by `setup.sh`.

## Important Details & Gotchas

- **CSS-based author detection** in `OpenChat.extract_messages` relies on `justify-end`/`justify-start` classes — these are X.com implementation details that may break on UI changes.
- **`DmListener` parses `aria-description`** attributes on conversation items to extract username, last message, and unread status. Format: comma-separated parts where index 1 is `@username`, last part may be "unread".
- **All X.com element selectors use `data-testid` attributes** where possible (e.g., `dm-conversation-item`, `dm-message-list`, `dm-composer-textarea`, `dm-composer-send-button`, `loginButton`, `LoginForm_Login_Button`).
- **Tab-per-conversation model**: the bot opens each conversation in a new browser tab, processes it, then closes the tab. The main tab stays on the DM inbox.
- **Conversation IDs** come from `data-testid` attributes, with colons replaced by hyphens.
- **Config typo**: `undetected_chromedirver` (missing 'e' in 'driver') — this is intentional/legacy, do not rename without updating all references.
- **No `__init__.py` files** — the project runs with `src/` as the working directory (Python adds CWD to `sys.path`).
- **`SAVE_COOKIES` is hardcoded to `False`** in `login.py` — cookie persistence is disabled by default.
- **Passcode re-entry**: when navigating to a conversation tab, the passcode may be required again. `main_loop` handles this and re-navigates to the conversation URL after passcode entry.
- **Message ordering**: Supabase messages are saved with slight delays (`time.sleep(0.5)`) to ensure `created_at` ordering is correct.
