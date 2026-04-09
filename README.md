# XDM - X/Twitter DM Auto-Reply Bot

A Python bot that monitors your X.com (Twitter) direct messages and automatically generates replies using an LLM. It uses SeleniumBase with undetected ChromeDriver to interact with the X.com web interface, OpenRouter for LLM-powered responses, and Supabase for conversation history persistence.

## Features

- **Automated DM monitoring** — Polls your DM inbox for new or changed conversations
- **LLM-powered replies** — Generates natural, human-like responses via OpenRouter (supports any model available on OpenRouter)
- **Encrypted DM support** — Automatically enters your 4-digit chat passcode to decrypt DMs
- **Conversation history** — Persists all messages in Supabase so the LLM has full conversation context
- **Anti-detection measures** — Uses undetected ChromeDriver, CDP-based keystrokes, and Bezier-curve mouse movements to mimic human behavior
- **Cookie persistence** — Saves and restores session cookies to avoid repeated logins
- **Customizable personality** — Define the bot's tone and behavior via a plain-text system prompt

## How It Works

```
DM inbox poll → detect new message → open conversation in new tab
    → enter chat passcode (if needed) → scroll to bottom
    → extract on-screen messages → hash & compare against Supabase
    → save NEW user messages to Supabase → get LLM reply
    → send reply → save reply → close tab → repeat
```

The bot uses a change-detection approach: on each polling cycle it scrapes the DM sidebar, diffs it against the previous state, and only processes conversations where new user messages appear. The first poll establishes a baseline so existing conversations aren't re-processed.

Messages are deduplicated using **content-addressed hashing** (SHA-256 of conversation ID + sender + normalized text + occurrence count). This is necessary because X.com regenerates message IDs on every page load, making them unsuitable as stable identifiers. Supabase is the single source of truth for conversation history — the LLM context is built exclusively from saved messages.

## Prerequisites

- **Python 3.10+**
- **Google Chrome** installed
- **Supabase** account (free tier works) — for conversation storage
- **OpenRouter** API key — for LLM access
- **X.com** account with encrypted DMs enabled

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/Phasor/XDM.git
cd XDM
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure

On first run, a `config.json` file is auto-created from the built-in template. Alternatively, copy the provided template:

```bash
cp config.template.json config.json
```

Edit `config.json` with your credentials:

```json
{
    "x.com": {
        "username": "your_x_username",
        "password": "your_x_password",
        "passcode": "1234",
        "polling_interval": 5
    },
    "supabase": {
        "project_url": "https://your-project.supabase.co",
        "secret_key": "your-supabase-secret-key",
        "db_url": "postgresql://postgres.your-project:password@aws-0-region.pooler.supabase.com:5432/postgres"
    },
    "openrouter": {
        "api_key": "sk-or-v1-your-openrouter-key",
        "model": "openai/gpt-3.5-turbo"
    }
}
```

All credentials can alternatively be set via environment variables:

| Environment Variable | Config Key | Description |
|---|---|---|
| `X_USERNAME` | `x.com.username` | X.com username |
| `X_PASSWORD` | `x.com.password` | X.com password |
| `X_PASSCODE` | `x.com.passcode` | 4-digit encrypted DM passcode |
| `OPENROUTER_API_KEY` | `openrouter.api_key` | OpenRouter API key |
| `SUPABASE_URL` | `supabase.project_url` | Supabase project URL |
| `SUPABASE__SECRET_KEY` | `supabase.secret_key` | Supabase secret key (note: double underscore) |
| `SUPABASE_DB_URL` | `supabase.db_url` | PostgreSQL connection string |

### 5. Customize the personality

Edit `prompts/personality.txt` to define how the bot responds. This file is used as the LLM system prompt. The default personality writes casual, human-like replies.

### 6. Supabase setup

The bot automatically creates the required database tables (`conversations` and `messages`) on first run using the direct PostgreSQL connection (`db_url`). No manual schema setup is needed.

**To find your Supabase credentials:**
1. Go to your Supabase project dashboard
2. **Project URL** and **Secret Key**: Settings > API
3. **Database URL**: Settings > Database > Connection string (use the "Transaction" pooler connection string)

## Usage

```bash
python src/main.py
```

Or on Windows:

```bash
bot_runner.bat
```

A Chrome window will open and the bot will:
1. Navigate to X.com and log in (or restore an existing session)
2. Navigate to the DM inbox
3. Enter the chat encryption passcode if prompted
4. Begin polling for new messages

Press `Ctrl+C` to stop the bot gracefully.

## Configuration Reference

### `config.json`

| Key | Type | Default | Description |
|---|---|---|---|
| `logging_level` | string | `"INFO"` | `"INFO"` or `"DEBUG"` |
| `chrome.headless` | bool | `false` | Run Chrome in headless mode |
| `chrome.proxy` | string | `""` | HTTP proxy (e.g., `"http://host:port"`) |
| `chrome.width_height` | array | `[780, 820]` | Browser window dimensions `[width, height]` |
| `chrome.window_position` | array | `[755, 0]` | Browser window position `[x, y]` |
| `chrome.user_data_dir` | string | `"chrome_profile"` | Chrome profile directory for session persistence |
| `x.com.passcode` | string | `"1234"` | 4-digit encrypted DM passcode |
| `x.com.polling_interval` | int | `5` | Seconds between DM inbox polls |
| `openrouter.model` | string | `"openai/gpt-3.5-turbo"` | Any model available on [OpenRouter](https://openrouter.ai/models) |
| `openrouter.timeout` | int | `30` | LLM API request timeout in seconds |
| `openrouter.personality_file` | string | `"prompts/personality.txt"` | Path to the system prompt file |
| `supabase.conversations_table` | string | `"conversations"` | Name of the conversations table |
| `supabase.messages_table` | string | `"messages"` | Name of the messages table |
| `supabase.context_message_limit` | int | `20` | Max messages to include as LLM context |

## Project Structure

```
XDM/
├── src/
│   ├── main.py                  # Entry point, orchestration, polling loop, crash recovery
│   ├── llm/
│   │   └── llm_client.py        # OpenRouter API client, single-source context builder
│   ├── storage/
│   │   └── supabase_client.py   # Supabase CRUD, auto schema setup, retry decorator
│   └── x_automation/
│       ├── dm_manager.py        # DM listener, message hashing/dedup, chat reader/sender
│       └── login.py             # Login flow, cookie management, human-like mouse movement
├── deploy/
│   ├── setup.sh                 # VPS setup script (Ubuntu 24.04)
│   └── xdm.service             # systemd service definition
├── prompts/
│   └── personality.txt          # LLM system prompt / personality definition
├── config.template.json         # Config template (no secrets)
├── bot_runner.bat               # Windows launcher
├── requirements.txt             # Python dependencies
└── CLAUDE.md                    # Development guidance for Claude Code
```

## VPS Deployment

The bot is designed for unattended operation on a VPS. See `deploy/setup.sh` for automated setup on Ubuntu 24.04.

### Quick Start

```bash
sudo bash deploy/setup.sh <username> [branch]
nano ~/xdm/config.json   # fill in credentials
sudo systemctl start xdm
sudo journalctl -u xdm -f
```

### Proxy Setup (Required for Datacenter IPs)

X.com blocks automated login from datacenter IPs. Use a residential proxy relay:

```bash
sudo snap install gost
gost -L=:8888 -F=http://user:pass@proxy-host:port &
```

Then set in `config.json`:
```json
"proxy": "http://127.0.0.1:8888"
```

### Production Resilience

- **Auto-restart**: systemd service with `Restart=always` (30s delay, max 10/10min)
- **Crash recovery**: `run_forever()` loop with exponential backoff
- **Chrome health check**: Detects dead browser and reinitializes
- **Periodic restart**: Chrome restarts every 6 hours to prevent memory leaks
- **Session recovery**: Detects expired X.com sessions and re-authenticates
- **Resource limits**: 2GB memory cap, 80% CPU quota via systemd
- **Orphan cleanup**: Kills leftover Chrome processes on startup

### Useful Commands

| Command | Description |
|---|---|
| `sudo systemctl status xdm` | Check bot status |
| `sudo journalctl -u xdm -f` | Live log stream |
| `sudo systemctl restart xdm` | Restart the bot |
| `sudo systemctl stop xdm` | Stop the bot |
| `cd ~/xdm && git pull && sudo systemctl restart xdm` | Deploy updates |

## Important Notes

- **X.com selectors may break** — The bot relies on `data-testid` attributes and CSS classes (`justify-end`/`justify-start`) from the X.com web UI. These can change without notice in X.com updates.
- **Datacenter IPs are blocked** — X.com blocks login from known datacenter IPs. A residential proxy is required for VPS deployment.
- **Rate limiting** — The default 5-second polling interval is conservative. Lowering it may trigger rate limits or anti-automation measures.
- **One conversation at a time** — The bot processes DMs sequentially: detect change, open tab, reply, close tab, repeat.
- **Conversation isolation** — All database queries filter by `conversation_id`, ensuring conversations between different users never mix.

## License

This project is for personal/educational use.
