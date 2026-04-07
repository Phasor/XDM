# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

X DM Bot — a Python bot that automatically replies to X.com (Twitter) direct messages using an LLM. It uses SeleniumBase with undetected ChromeDriver to monitor the DM inbox, detects new messages, generates replies via OpenRouter, and persists conversation history in Supabase.

## Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Run
python src/main.py
# or on Windows:
bot_runner.bat
```

## Configuration

All config lives in `config.json` (auto-created from template on first run). Credentials can also be set via environment variables: `X_USERNAME`, `X_PASSWORD`, `X_PASSCODE` (4-digit encrypted DM passcode), `OPENROUTER_API_KEY`, `SUPABASE_URL`, `SUPABASE__SECRET_KEY` (double underscore), `SUPABASE_DB_URL`.

The LLM personality/system prompt is in `prompts/personality.txt`.

## Architecture

**Entry point:** `src/main.py` — `XAutomation` class orchestrates everything:
1. Initializes Chrome (SeleniumBase undetected driver), Supabase client, and LLM client
2. Logs into X.com (with cookie persistence in `cookies/`)
3. Navigates to DM inbox and enters polling loop

**Main loop** (`XAutomation.main_loop`): polls DM list for changes → opens new tab for conversation → reads new messages → sends to LLM → types reply → saves to Supabase → closes tab.

### Key modules

- `src/x_automation/dm_manager.py` — Two classes:
  - `DmListener`: scrapes the DM sidebar to detect new/changed conversations by diffing current vs previous state. First poll sets baseline. Uses `commit()` to mark a conversation as processed.
  - `OpenChat`: reads messages from an opened conversation thread, sends replies via the composer textarea. Identifies authors by CSS class (`justify-end` = assistant, `justify-start` = user).

- `src/x_automation/login.py` — `Login` handles auth with cookie save/restore. Uses CDP `Input.dispatchKeyEvent` for typing (anti-detection). `HumanLikeMovement` generates Bezier-curve mouse paths for human-like interaction.

- `src/llm/llm_client.py` — `LLM` sends chat completions to OpenRouter. `get_conversation_context` merges consecutive same-role messages and prepends the system prompt from `personality.txt`.

- `src/storage/supabase_client.py` — `SupaBase` manages two tables (`conversations`, `messages`). Auto-creates schema via psycopg on init. Uses Supabase client (PostgREST) for CRUD, psycopg for DDL.

### Data flow

```
DM sidebar poll → detect change → open conv tab → read messages (Selenium)
    → fetch history (Supabase) → build context + get LLM reply (OpenRouter)
    → type reply (Selenium) → save messages (Supabase) → close tab
```

## Important Details

- Message author detection in `OpenChat.extract_messages` relies on CSS classes (`justify-end`/`justify-start`) — these are X.com implementation details that may break on UI changes.
- `DmListener` parses `aria-description` attributes on conversation items to extract username, last message, and unread status.
- All X.com element selectors use `data-testid` attributes where possible.
- The bot opens each conversation in a new browser tab, processes it, then closes the tab and returns to the main DM list.
- Supabase table names are configurable in config but default to `conversations` and `messages`.
