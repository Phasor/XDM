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
    → enter chat passcode (if needed) → read messages via Selenium
    → fetch history from Supabase → build context + get LLM reply via OpenRouter
    → type reply in chat → save messages to Supabase → close tab → repeat
```

The bot uses a change-detection approach: on each polling cycle it scrapes the DM sidebar, diffs it against the previous state, and only processes conversations where new user messages appear. The first poll establishes a baseline so existing conversations aren't re-processed.

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
│   ├── main.py                  # Entry point, orchestration, and polling loop
│   ├── llm/
│   │   └── llm_client.py        # OpenRouter API client, context builder
│   ├── storage/
│   │   └── supabase_client.py   # Supabase CRUD, auto schema setup
│   └── x_automation/
│       ├── dm_manager.py        # DM listener (sidebar scraper) + chat reader/sender
│       └── login.py             # Login flow, cookie management, human-like mouse movement
├── prompts/
│   └── personality.txt          # LLM system prompt / personality definition
├── config.template.json         # Config template (no secrets)
├── bot_runner.bat               # Windows launcher
├── requirements.txt             # Python dependencies
└── CLAUDE.md                    # Development guidance for Claude Code
```

## Important Notes

- **X.com selectors may break** — The bot relies on `data-testid` attributes and CSS classes (`justify-end`/`justify-start`) from the X.com web UI. These can change without notice in X.com updates.
- **Not a headless-first tool** — While headless mode is configurable, the bot is designed to run with a visible browser window. Some anti-bot detection may block headless sessions.
- **Rate limiting** — The default 5-second polling interval is conservative. Lowering it may trigger rate limits or anti-automation measures.
- **One conversation at a time** — The bot processes DMs sequentially: detect change, open tab, reply, close tab, repeat.

## License

This project is for personal/educational use.
