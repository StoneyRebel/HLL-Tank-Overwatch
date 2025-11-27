# HLL Tank Overwatch Bot Setup Guide

## Quick Start

1. **Copy the environment template:**
   ```bash
   cp .env.template .env
   ```

2. **Edit the .env file with your settings:**
   - Get your Discord bot token from [Discord Developer Portal](https://discord.com/developers/applications)
   - Get your CRCON API key from your CRCON web interface
   - Update other settings as needed

3. **Install Python dependencies:**
   ```bash
   pip install discord.py aiohttp python-dotenv
   ```

4. **Run the bot:**
   ```bash
   python enhanced_discord_bot.py
   ```

## Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `DISCORD_TOKEN` | Your Discord bot token | `MTIzNDU2Nzg5...` |
| `CRCON_API_KEY` | Your CRCON API key | `d1c56ecf-eac1-...` |

## Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CRCON_URL` | `http://localhost:8010` | Your CRCON server URL |
| `CRCON_TIMEOUT` | `15` | API timeout in seconds |
| `CRCON_AUTO_SWITCH` | `true` | Auto-switch on point captures |
| `UPDATE_INTERVAL` | `15` | Discord update frequency (seconds) |
| `ADMIN_ROLE_NAME` | `admin` | Discord role required to control bot |
| `BOT_NAME` | `HLLTankBot` | Name shown in game messages |
| `BOT_AUTHOR` | `StoneyRebel` | Author shown in embed footer |
| `LOG_CHANNEL_ID` | `0` | Discord channel for match logs (0 = disabled) |
| `ENABLE_KILL_FEED` | `false` | Enable tank-kill tracking via CRCON webhook |
| `KILL_WEBHOOK_HOST` | `0.0.0.0` | Host/interface to bind the webhook listener |
| `KILL_WEBHOOK_PORT` | `8081` | Port for the webhook listener (use `$PORT` on Railway) |
| `KILL_WEBHOOK_PATH` | `/kill-webhook` | Path for the webhook POST endpoint |
| `KILL_WEBHOOK_SECRET` | – | Optional shared secret expected in `X-Webhook-Secret` header |
| `TANK_WEAPON_KEYWORDS` | – | JSON string or file path listing weapon keywords that count as tank kills |

## Discord Setup

1. **Create a Discord Application:**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - Click "New Application"
   - Give it a name like "HLL Tank Overwatch"

2. **Create a Bot:**
   - Go to the "Bot" section
   - Click "Add Bot"
   - Copy the token and put it in your `.env` file

3. **Set Bot Permissions:**
   - In "OAuth2" > "URL Generator"
   - Select "bot" and "applications.commands"
   - Select permissions: "Send Messages", "Use Slash Commands", "Embed Links"
   - Use the generated URL to invite the bot to your server

4. **Create Admin Role:**
   - In your Discord server, create a role named "admin" (or whatever you set in `ADMIN_ROLE_NAME`)
   - Give this role to users who should control the bot

## CRCON Setup

1. **Get your API Key:**
   - Open your CRCON web interface
   - Go to Settings/Admin panel
   - Find your API key
   - Put it in your `.env` file

2. **Set CRCON URL:**
   - If CRCON is on the same machine: `http://localhost:8010`
   - If CRCON is remote: `http://your-server-ip:8010`
   - Include `http://` or `https://`

## Usage

1. **Start a match:**
   - Use `/reverse_clock` in Discord
   - Click "▶️ Start Match"

2. **Control the clock:**
   - Use "Allies" and "Axis" buttons to manually switch control
   - Toggle "🤖 Auto" for automatic switching on point captures
   - Click "⏹️ Stop" to end the match

3. **View stats:**
   - Click "📊 Stats" for live match statistics
   - Use `/server_info` for current server information
   - Run `/killfeed_status` to check kill feed health when tank tracking is enabled

## Local Kill Feed Test (Webhook)

For manual testing without a live CRCON feed:

1. Start the bot locally with `ENABLE_KILL_FEED=true` (defaults: `KILL_WEBHOOK_PORT=8081`, `KILL_WEBHOOK_PATH=/kill-webhook`).
2. Send a test kill payload:
   ```bash
   curl -X POST http://localhost:8081/kill-webhook \
     -H "Content-Type: application/json" \
     -d '{"killer_team":"allies","victim_team":"axis","weapon":"75mm","killer_name":"Allied Gunner","victim_name":"Axis Tank","vehicle":"Panzer IV"}'
   ```
   Include `-H "X-Webhook-Secret: <value>"` if you set `KILL_WEBHOOK_SECRET`.
3. Run `/killfeed_status` to confirm the webhook listener is running and the last event is captured.

## Troubleshooting

- **Bot won't start:** Check your `.env` file has valid `DISCORD_TOKEN` and `CRCON_API_KEY`
- **No admin permissions:** Make sure you have the role specified in `ADMIN_ROLE_NAME`
- **CRCON not connecting:** Verify `CRCON_URL` and `CRCON_API_KEY` are correct
- **Auto-switch not working:** Enable `CRCON_AUTO_SWITCH=true` and ensure CRCON connection is stable
- **Kill feed idle:** Confirm `ENABLE_KILL_FEED=true`, CRCON webhook URL/secret are correct, and inspect `/killfeed_status` for listener details

## Support

Check the logs in the `logs/bot.log` file for detailed error information.
