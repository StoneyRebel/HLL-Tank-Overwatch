# HLL Tank Overwatch – Copilot Instructions

## Architecture & Flow
- Everything lives in `enhanced_discord_bot.py`: globals (`bot`, `clocks`, constants), slash commands, Discord UI views, CRCON client, and startup guard.
- Each Discord channel gets a `ClockState` stored in `clocks[channel_id]`; `StartControls.start_match` seeds it, replaces the view with `TimerControls`, and starts `match_updater`.
- `ClockState` owns timers, CRCON session, embed reference, DMT data, and squad config. `match_updater` polls CRCON, updates embeds, and auto-stops when `time_remaining <= GAME_END_THRESHOLD`.
- Local runs auto-create `logs/`, `match_reports/`, `match_data/`, `backups/`; Railway skips this logic.

## State & Concurrency
- Guard every mutation touching `time_a`, `time_b`, `active`, `last_switch`, or `switches` with `async with clock._lock` (see `_auto_switch_to`, `_switch_team`, stop helpers).
- Finalize elapsed time before clearing `active` (manual stop, `auto_stop_match`, button stop). Forgetting this loses cap seconds and corrupts DMT scores.
- Always call `ClockState.connect_crcon()` instead of instantiating `APIKeyCRCONClient` directly so previous sessions close cleanly.

## Discord Interaction Patterns
- Slash commands use `@bot.tree.command`; defer immediately (`await interaction.response.defer(...)`) and respond via `interaction.followup`. Missing defer hits Discord’s 3‑second timeout.
- Admin-restricted operations must reuse `user_is_admin` (matches `ADMIN_ROLE_NAME` case-insensitively). UI buttons already check this; copy the pattern for new controls.
- Views (`StartControls`, `TimerControls`) declare `timeout=None` for persistence. Whenever mutating controls or embeds, call `safe_edit_message` to swallow `NotFound/HTTPException` and null `clock.message` on failure.

## CRCON Integration & Data Handling
- `APIKeyCRCONClient` cycles Bearer/x-api-key/raw headers until `/api/get_status` returns 200; reuse its async context manager so sessions share the configured 15s timeout.
- `get_live_game_state()` concurrently fetches `/api/get_gamestate`, `/api/get_team_view`, `/api/get_map`, `/api/get_players`, `/api/get_detailed_players`. Every handler unwraps payloads guarded by `isinstance(..., dict) and 'result' in ...`—preserve those fallbacks to survive CRCON version drift.
- Messaging (`send_message`) first pulls `/api/get_player_ids` and DM’s each player individually. Broadcast strings must stay under ~500 chars or requests start failing.

## DMT Scoring & Messaging Consistency
- `calculate_dmt_score` enforces the tournament formula: `3 × (top crew scores) + commander + cap_seconds × 0.5`. Update this helper plus every embed/broadcast string (`build_embed`, `_auto_switch_to`, `_switch_team`, stop helpers) together to keep Discord and in-game text aligned.
- `ClockState.last_scores` mirrors CRCON `allied_score/axis_score`. Auto-switch compares these values; when seeding manual scores or custom data, update the dict to avoid thrashing.

## Match Lifecycle & Background Tasks
- `match_updater` is decorated with `@tasks.loop(seconds=get_update_interval())`; intervals are bound at import time, so changing `UPDATE_INTERVAL` requires a restart. The loop silently exits when `clock.started` or `clock.message` is falsy—new tasks should follow the same guard pattern.
- Auto stop triggers when CRCON reports `time_remaining <= GAME_END_THRESHOLD` (30s). Manual stop, auto stop, and button resets all share the same finalize-update-log flow; use them as blueprints for new end states.

## Configuration & Secrets
- Any new environment variable must be added to `.env.template`, documented in `README.md` and `SETUP.md`, validated in the startup block inside `if __name__ == "__main__"`, and referenced in CI if needed.
- Startup halts when `DISCORD_TOKEN` or `CRCON_API_KEY` is missing; preserve that behavior so Railway deploys fail fast. `LOG_CHANNEL_ID=0` disables match logging—always guard `bot.get_channel(LOG_CHANNEL_ID)`.

## User-Facing Commands & UI
- Slash command expectations from the README: `/reverse_clock`, `/crcon_status`, `/server_info`, `/send_message`, `/help_clock`, plus debug helpers (`/test_map`, `/test_player_scores`). Keep these names stable so docs stay accurate.
- Button layout inside `TimerControls`: Allies, Axis, 🤖 Auto, 📊 Stats, ↺ Reset, ⏹️ Stop. Any behavior change must be mirrored in README usage docs.
- Usage flow in README: `/reverse_clock` ➜ Start Match button ➜ optional Auto toggle ➜ Stats/Stop buttons. Ensure new flows preserve that simple story.

## Developer Workflow & CI Expectations
- Local run: `pip install -r requirements.txt`, copy `.env.template` to `.env`, populate `DISCORD_TOKEN`/`CRCON_API_KEY`, then `python enhanced_discord_bot.py`. Procfile/Railway use the same command.
- CI (`.github/workflows/test.yml`) installs deps, runs flake8 twice (errors then warnings), ensures `.env.template` contains required keys, and imports `enhanced_discord_bot` after generating a throwaway `.env`. Keep import side effects minimal so this smoke test passes.
- Dependencies are deliberately lean (`discord.py`, `aiohttp`, `python-dotenv`); adding new ones impacts Railway’s Nixpacks build time, so justify additions.

## Deployment & Environment Parity
- README promises one-click Railway deploy plus manual steps: fork repo, connect Railway project, set `DISCORD_TOKEN`, `CRCON_API_KEY`, `CRCON_URL`. Keep `Procfile` and runtime behavior consistent with those instructions.
- Local setup instructions (clone → `pip install -r requirements.txt` → copy `.env.template` → run bot) double as sanity tests; avoid extra prerequisites beyond Python ≥3.8.
- README troubleshooting references `logs/bot.log`, slash commands, and CRCON URLs. When adding new debug tooling, update both README and this file so guidance stays aligned.

## Debug & Diagnostics Patterns
- Debug slash commands (`/test_map`, `/test_player_scores`) paginate large JSON dumps by 1,900-character chunks. Reuse that chunking strategy for any new verbose command.
- Logging uses the module-level `logger`; include context (channel id, team) when adding new log lines, but never print tokens or API keys.
