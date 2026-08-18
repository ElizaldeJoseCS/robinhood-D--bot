# Robinhood Discord Bot

A Discord bot that displays your Robinhood portfolio and recommends stocks using a momentum + fundamental analysis pipeline. Runs as two services: a C++ Discord bot and a Python FastAPI backend.

## Features

- **`/portfolio`** — Live Robinhood portfolio stats (equity, crypto, market value)
- **`/recommend`** — Top 5 stock picks ranked by momentum and fundamentals
- Auto-posts portfolio updates to a channel on a timer

## Prerequisites

- Linux (tested on Ubuntu) or WSL
- C++ compiler with [D++](https://dpp.dev/) and [nlohmann/json](https://github.com/nlohmann/json) installed
- Python 3.10+
- A [Robinhood](https://robinhood.com/) account
- A [Discord bot token](https://discord.com/developers/applications)

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/ElizaldeJoseCS/robinhood-D--bot.git
cd robinhood-D--bot
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your values:

| Variable | Description |
|---|---|
| `ROBINHOOD_USER` | Your Robinhood email |
| `ROBINHOOD_PASS` | Your Robinhood password |
| `DISCORD_BOT_TOKEN` | From the [Discord Developer Portal](https://discord.com/developers/applications) → Bot → Token |
| `GUILD_ID` | Right-click your server name in Discord (with Developer Mode on) → Copy Server ID |
| `CHANNEL_ID` | Right-click the channel for auto-posts → Copy Channel ID |

> **Never commit your `.env` file.** It is already in `.gitignore`.

### 3. Set up the Python backend

```bash
cd robin
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn robin_stocks yfinance pandas gunicorn requests
```

### 4. Compile the C++ bot

```bash
cd mybot
g++ mybot.cpp -o mybot -ldpp -lpthread
```

> You may need additional include/library flags depending on how D++ and nlohmann/json were installed.

### 5. Run

Start the Python backend first, then the C++ bot:

```bash
# Terminal 1 — Python backend (port 8000)
cd robin
source .venv/bin/activate
uvicorn perf_stocks:app --host 0.0.0.0 --port 8000

# Terminal 2 — C++ bot
cd mybot
./mybot
```

## systemd (optional)

Example service files for running as system services:

```ini
# /etc/systemd/system/stocks-backend.service
[Unit]
Description=FastAPI Robinhood Analysis Backend
After=network.target

[Service]
User=YOUR_USER
WorkingDirectory=/path/to/robinhood-D--bot/robin
EnvironmentFile=/path/to/robinhood-D--bot/.env
ExecStart=/path/to/robinhood-D--bot/robin/.venv/bin/gunicorn perf_stocks:app -w 1 -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/discord-bot.service
[Unit]
Description=C++ DPP Discord Market Bot
After=stocks-backend.service

[Service]
User=YOUR_USER
WorkingDirectory=/path/to/robinhood-D--bot/mybot
EnvironmentFile=/path/to/robinhood-D--bot/.env
ExecStart=/path/to/robinhood-D--bot/mybot/mybot
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stocks-backend discord-bot
```

## How it works

```
Discord User
    │
    ▼
C++ Bot (D++) ──HTTP GET──▶ Python Backend (FastAPI)
    │                           │
    ▼                           ▼
Discord Embed            Robinhood API + yfinance
                         (portfolio data + stock screening)
```

- The Python backend authenticates with Robinhood on startup and refreshes stock recommendations every 6 hours.
- The C++ bot is a thin presentation layer — it receives slash commands, calls the backend, and renders Discord embeds.
- Token refresh is handled automatically to reduce re-authentication frequency.

## License

MIT
