#!/usr/bin/env python3
"""
HLL Discord Bot - Direct HLL RCON V2 Connection
Time Control Focused - Win by controlling the center point longest!
"""

import asyncio
import base64
import struct
import os
import discord
import datetime
import json
import aiohttp
import logging
from pathlib import Path
from dotenv import load_dotenv
from discord.ext import commands, tasks
from discord import app_commands
from datetime import timezone, timedelta

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Railway captures stdout
    ]
)
logger = logging.getLogger(__name__)

# Create directories if running locally (Railway handles this differently)
if not os.getenv('RAILWAY_ENVIRONMENT'):
    for directory in ['logs', 'match_reports', 'match_data', 'backups']:
        os.makedirs(directory, exist_ok=True)

load_dotenv()

# Devil Dave Integration
DEVIL_DAVE_URL      = os.getenv('DEVIL_DAVE_URL', '')       # e.g. https://www.the504thdevils.us
DEVIL_DAVE_API_KEY  = os.getenv('DEVIL_DAVE_API_KEY', '')   # same as EVENT_STATS_API_KEY in Devil Dave
DEVIL_DAVE_EVENT_ID = os.getenv('DEVIL_DAVE_EVENT_ID', '')  # optional: override active event lookup

# Constants
DEFAULT_MATCH_DURATION = 4500  # 1h 15m in seconds
GAME_END_THRESHOLD = 5        # Stop match when server time hits this (seconds)
FAST_POLL_THRESHOLD = 20      # Switch to 2-second polling within this many seconds of end
MESSAGE_TRUNCATE_LENGTH = 1900
MIN_UPDATE_INTERVAL = 5
MAX_UPDATE_INTERVAL = 300
STALE_DATA_SECONDS = 90       # game data older than this is flagged in the embed

# Errors that mean the RCON socket is dead and has to be rebuilt.
# These MUST be allowed to propagate up to the reconnect handlers — swallowing
# them is what used to freeze the bot permanently on 'Connection reset by peer'.
RCON_TRANSPORT_ERRORS = (
    ConnectionError,               # incl. ConnectionResetError (Errno 104)
    OSError,                       # incl. broken pipe (32), timed out (110)
    asyncio.IncompleteReadError,   # peer closed mid-packet
    asyncio.TimeoutError,          # not an OSError before Python 3.11
    json.JSONDecodeError,          # garbage body = desynced stream
    struct.error,                  # unparseable header = desynced stream
)

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

clocks = {}
# Parse LOG_CHANNEL_ID safely
log_channel_str = os.getenv('LOG_CHANNEL_ID', '0')
LOG_CHANNEL_ID = int(log_channel_str) if log_channel_str.isdigit() else 0

class HLLRconV2Client:
    """Direct HLL RCON V2 client — connects straight to the game server via TCP."""

    MAGIC          = 0xDE450508
    HEADER_FORMAT  = '<III'   # magic, request_id, body_length  (12 bytes)
    HEADER_SIZE    = struct.calcsize('<III')  # 12

    def __init__(self):
        self.host     = os.getenv('RCON_HOST', '')
        self.port     = int(os.getenv('RCON_PORT', '7779'))
        self.password = os.getenv('RCON_PASSWORD', '')
        self.timeout  = int(os.getenv('RCON_TIMEOUT', '30'))
        self.reader     = None
        self.writer     = None
        self.xor_key    = None
        self.auth_token = ''
        self._msg_id    = 0
        self._lock      = asyncio.Lock()
        self.connected  = False   # only True between a successful login and a failure

    # ── Internal protocol helpers ────────────────────────────────────────────

    def _next_id(self) -> int:
        self._msg_id = (self._msg_id + 1) & 0xFFFFFFFF
        return self._msg_id

    def _xor(self, data: bytes) -> bytes:
        key = self.xor_key
        return bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))

    def _build_packet(self, payload: dict, encrypt: bool = True) -> bytes:
        # contentBody must be a JSON string when it's a dict, not a nested object
        p = dict(payload)
        if isinstance(p.get('contentBody'), dict):
            p['contentBody'] = json.dumps(p['contentBody'], separators=(',', ':'))
        body = json.dumps(p, separators=(',', ':')).encode('utf-8')
        if encrypt and self.xor_key:
            body = self._xor(body)
        header = struct.pack(self.HEADER_FORMAT, self.MAGIC, self._next_id(), len(body))
        return header + body

    async def _send(self, payload: dict, encrypt: bool = True):
        self.writer.write(self._build_packet(payload, encrypt))
        await self.writer.drain()

    async def _recv(self, decrypt: bool = True) -> dict:
        header = await asyncio.wait_for(
            self.reader.readexactly(self.HEADER_SIZE), timeout=self.timeout
        )
        magic, _, body_len = struct.unpack(self.HEADER_FORMAT, header)
        if magic != self.MAGIC:
            raise ConnectionError(f"Bad magic: {magic:#x}")
        body = await asyncio.wait_for(
            self.reader.readexactly(body_len), timeout=self.timeout
        )
        if decrypt and self.xor_key:
            body = self._xor(body)
        return json.loads(body.decode('utf-8'))

    # ── Connection lifecycle ─────────────────────────────────────────────────

    async def connect(self):
        self.connected = False
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.timeout
        )
        # Step 1: ServerConnect — the only unencrypted command
        await self._send({
            "authToken": "", "version": 2,
            "name": "ServerConnect", "contentBody": ""
        }, encrypt=False)
        resp = await self._recv(decrypt=False)
        if resp.get('statusCode', 0) != 200:
            raise ConnectionError(f"ServerConnect failed: {resp.get('statusMessage')}")

        # XOR key is Base64-encoded in contentBody
        self.xor_key = base64.b64decode(resp.get('contentBody', ''))

        # Step 2: Login — encrypted from this point on
        await self._send({
            "authToken": "", "version": 2,
            "name": "Login", "contentBody": self.password
        })
        resp = await self._recv()
        if resp.get('statusCode', 0) != 200:
            raise ConnectionError(f"RCON login failed: {resp.get('statusMessage')}")
        self.auth_token = resp.get('contentBody', '')
        self.connected  = True
        logger.info(f"Connected to HLL Server at {self.host}:{self.port}")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        self.connected = False
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    # ── Command execution ────────────────────────────────────────────────────

    async def command(self, name: str, content_body=None) -> dict:
        """Send an authenticated command and return the full response dict.

        A transport failure marks the client dead and re-raises. A timeout is
        treated the same way on purpose: _recv() reads whatever packet arrives
        next without matching request ids, so a late response would desync the
        stream for good. Rebuilding the connection is the only safe recovery.
        """
        async with self._lock:
            try:
                await self._send({
                    "authToken":   self.auth_token,
                    "version":     2,
                    "name":        name,
                    "contentBody": content_body if content_body is not None else ""
                })
                return await self._recv()
            except RCON_TRANSPORT_ERRORS:
                self.connected = False
                raise

    @staticmethod
    def _parse_body(raw):
        """Ensure contentBody is a dict (server sometimes returns a JSON string)."""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                pass
        return {}

    # ── Bot-facing API ───────────────────────────────────────────────────────

    async def get_live_game_state(self):
        """Return a flat dict of live game state directly from RCON V2."""
        try:
            # Session: scores, time, map, player counts
            sess_resp = await self.command('GetServerInformation', {"Name": "session", "Value": ""})
            session   = self._parse_body(sess_resp.get('contentBody', {}))

            # The session tells us which faction index = allied and which = axis.
            # This is dynamic per map — do NOT hardcode 1=allied/2=axis.
            allied_faction = int(session.get('alliedFaction', 1))
            axis_faction   = int(session.get('axisFaction',   0))
            logger.info(f"Faction map: alliedFaction={allied_faction}, axisFaction={axis_faction}")

            # Player list
            pl_resp  = await self.command('GetServerInformation', {"Name": "players", "Value": ""})
            pl_body  = self._parse_body(pl_resp.get('contentBody', {}))
            raw_list = pl_body.get('players', []) if isinstance(pl_body, dict) else []

            # Fetch per-player detail (team, platoon, combat score)
            players = []
            for p in raw_list:
                if not isinstance(p, dict):
                    continue
                pid = p.get('iD', p.get('id', p.get('Id', '')))
                if not pid:
                    continue
                try:
                    pr   = await self.command('GetServerInformation', {"Name": "player", "Value": pid})
                    body = self._parse_body(pr.get('contentBody', {}))
                    # Field may be 'Team' or 'team' — check both (RCON V2 docs show capital T)
                    team_int = body.get('Team', body.get('team', -1))
                    if team_int == allied_faction:
                        team = 'allies'
                    elif team_int == axis_faction:
                        team = 'axis'
                    else:
                        team = ''
                    logger.info(f"Player {p.get('name')}: team_int={team_int} -> {team!r}")
                    score_data = body.get('scoreData', body.get('ScoreData', {})) or {}
                    if isinstance(score_data, str):
                        try: score_data = json.loads(score_data)
                        except: score_data = {}
                    # combat field has inconsistent casing (cOMBAT) — search case-insensitively
                    combat = 0
                    for k, v in score_data.items():
                        if k.lower() == 'combat':
                            combat = int(v or 0)
                            break
                    players.append({
                        'name':    p.get('name', ''),
                        'id':      pid,
                        'team':    team,
                        'platoon': body.get('Platoon', body.get('platoon', '')) or '',
                        'combat':  combat,
                    })
                except RCON_TRANSPORT_ERRORS:
                    raise      # socket is gone; do NOT return half a roster
                except Exception as e:
                    logger.warning(f"Player detail error {p.get('name')}: {e}")

            # Roster non-empty but no detail came back at all: the data would
            # zero out every combat score, so treat it as a failed poll.
            if raw_list and not players:
                raise ConnectionError(
                    f"No player detail returned for any of {len(raw_list)} players"
                )

            return {
                'allied_score':   int(session.get('alliedScore',       0)),
                'axis_score':     int(session.get('axisScore',          0)),
                'time_remaining': int(session.get('remainingMatchTime', 0)),
                'allied_players': int(session.get('alliedPlayerCount',  0)),
                'axis_players':   int(session.get('axisPlayerCount',    0)),
                'map':            str(session.get('mapName', 'Unknown')),
                'players':        players,
                'timestamp':      datetime.datetime.now(timezone.utc),
            }
        except RCON_TRANSPORT_ERRORS as e:
            # Must propagate: the callers' reconnect handlers depend on it.
            logger.warning(f"RCON connection lost while reading game state: {e}")
            self.connected = False
            raise
        except Exception as e:
            logger.error(f"Error getting game state: {e}")
            return None

    async def send_message(self, message: str) -> bool:
        """Send a message to every player individually via MessagePlayer."""
        try:
            msg     = message[:500]
            pl_resp = await self.command('GetServerInformation', {"Name": "players", "Value": ""})
            pl_body = self._parse_body(pl_resp.get('contentBody', {}))
            players = pl_body.get('players', []) if isinstance(pl_body, dict) else []

            if not players:
                logger.info("No players online to message")
                return True

            success = 0
            for p in players:
                if not isinstance(p, dict):
                    continue
                pid = p.get('iD', p.get('id', p.get('Id', '')))
                if not pid:
                    continue
                try:
                    resp = await self.command('MessagePlayer', {"Message": msg, "PlayerId": pid})
                    if resp.get('statusCode', 0) == 200:
                        success += 1
                except Exception as e:
                    logger.debug(f"MessagePlayer error {p.get('name')}: {e}")

            logger.info(f"MessagePlayer: {success}/{len(players)}")
            return success > 0
        except Exception as e:
            logger.error(f"Error in send_message: {e}")
            return False

class ClockState:
    """Enhanced clock state with live updating team times"""

    def __init__(self):
        self.time_a = 0
        self.time_b = 0
        self.active = None
        self.last_switch = None
        self.match_start_time = None
        self.countdown_end = None  # Add this back
        self.message = None
        self.started = False
        self.clock_started = False

        # RCON V2 integration
        self.rcon_client = None
        self.game_data = None
        self.auto_switch = False
        self.last_scores = {'allied': 0, 'axis': 0}
        self.switches = []
        self.last_update = None
        self._first_update_done = False
        self._fast_polling = False
        self._lock = asyncio.Lock()
        self._reconnect_delay   = 5     # seconds, doubles on each failed retry
        self._next_reconnect_at = None
        # Who held mid when the match ended. Both stop paths clear `active`
        # before scoring, which zeroed the held-mid bonus in every final
        # result even though it showed correctly all match long.
        self.final_holder = None

        # DMT Scoring (always enabled)
        self.tournament_mode = True  # Always use DMT scoring
        self.team_names = {'allied': 'Allies', 'axis': 'Axis'}
        self.ingame_messages = False  # Toggle for sending messages to players in-game
        # Squad mapping: which squads represent which crews
        self.squad_config = {
            'allied': {
                'crew1': 'Able',
                'crew2': 'Baker',
                'crew3': 'Charlie',
                'crew4': 'Dog',
                'commander': 'Command'
            },
            'axis': {
                'crew1': 'Able',
                'crew2': 'Baker',
                'crew3': 'Charlie',
                'crew4': 'Dog',
                'commander': 'Command'
            }
        }
        # Player scores by team
        self.player_scores = {'allied': {}, 'axis': {}}

    def get_time_remaining(self):
        """Get time remaining in match"""
        if self.countdown_end:
            now = datetime.datetime.now(timezone.utc)
            remaining = (self.countdown_end - now).total_seconds()
            return max(0, int(remaining))
        return DEFAULT_MATCH_DURATION

    def get_current_elapsed(self):
        """Get elapsed time since last switch"""
        if self.last_switch and self.clock_started and self.active:
            return (datetime.datetime.now(timezone.utc) - self.last_switch).total_seconds()
        return 0

    def total_time(self, team):
        """Get total time for a team INCLUDING current elapsed time"""
        if team == "A":
            base_time = self.time_a
            # Add current elapsed time if Allies are currently active
            if self.active == "A" and self.clock_started:
                base_time += self.get_current_elapsed()
            return base_time
        elif team == "B":
            base_time = self.time_b
            # Add current elapsed time if Axis are currently active
            if self.active == "B" and self.clock_started:
                base_time += self.get_current_elapsed()
            return base_time
        return 0

    def get_live_status(self, team):
        """Get live status with current timing info"""
        total = self.total_time(team)
        
        if self.active == team and self.clock_started:
            # Currently active - they're defending the point they control
            current_elapsed = self.get_current_elapsed()
            return {
                'total_time': total,
                'status': '🛡️ Defending',
                'current_session': current_elapsed,
                'is_active': True
            }
        else:
            # Not active - they're trying to attack and take the point
            return {
                'total_time': total,
                'status': '⚔️ Attacking',
                'current_session': 0,
                'is_active': False
            }

    async def connect_rcon(self):
        """Connect to HLL Server directly."""
        try:
            if self.rcon_client:
                try:
                    await self.rcon_client.close()
                except Exception as e:
                    logger.warning(f"Error closing existing RCON connection: {e}")

            self.rcon_client = HLLRconV2Client()
            await self.rcon_client.connect()
            logger.info("Connected to HLL Server successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to HLL Server: {e}")
            self.rcon_client = None
            return False
    
    async def ensure_rcon(self):
        """Make sure the RCON socket is alive, rebuilding it if not.

        connect_rcon() sets rcon_client to None when it fails, so callers must
        never gate the retry on rcon_client being truthy — that is exactly how
        a single reset used to kill the bot for the rest of the match.
        """
        if self.is_rcon_connected():
            self._reconnect_delay   = 5
            self._next_reconnect_at = None
            return True

        now = datetime.datetime.now(timezone.utc)
        if self._next_reconnect_at and now < self._next_reconnect_at:
            return False   # backing off

        logger.info("RCON down — attempting to reconnect")
        if await self.connect_rcon():
            self._reconnect_delay   = 5
            self._next_reconnect_at = None
            logger.info("RCON reconnected")
            return True

        self._reconnect_delay   = min(self._reconnect_delay * 2, 60)
        self._next_reconnect_at = now + timedelta(seconds=self._reconnect_delay)
        logger.warning(f"RCON reconnect failed; next attempt in {self._reconnect_delay}s")
        return False

    async def update_from_game(self):
        """Update from RCON V2 game data."""
        if not self.rcon_client:
            return
        try:
            data = await self.rcon_client.get_live_game_state()
            if not data:
                return
            self.game_data  = data
            self.last_update = datetime.datetime.now(timezone.utc)
            self.update_player_scores()

            if self.auto_switch and self.started and self._first_update_done:
                await self._check_score_changes()
            else:
                self.last_scores = {
                    'allied': data.get('allied_score', 0),
                    'axis':   data.get('axis_score',   0),
                }
                self._first_update_done = True
        except RCON_TRANSPORT_ERRORS:
            raise      # handled by match_updater / fast_poll_end -> reconnect
        except Exception as e:
            logger.error(f"Error updating from game: {e}")

    async def _check_score_changes(self):
        """Detect point captures and trigger auto-switch."""
        if not self.game_data:
            return
        current_allied = self.game_data.get('allied_score', 0)
        current_axis   = self.game_data.get('axis_score',   0)
        logger.info(f"Score check - Allied: {self.last_scores['allied']} -> {current_allied}, Axis: {self.last_scores['axis']} -> {current_axis}")
        if current_allied > self.last_scores['allied']:
            await self._auto_switch_to('A', "Allies captured the center point")
        elif current_axis > self.last_scores['axis']:
            await self._auto_switch_to('B', "Axis captured the center point")
        self.last_scores = {'allied': current_allied, 'axis': current_axis}
    
    async def _auto_switch_to(self, team: str, reason: str = "Auto-switch"):
        """Auto-switch teams with proper time tracking"""
        if self.active == team:
            return

        async with self._lock:
            now = datetime.datetime.now(timezone.utc)

            # IMPORTANT: Update accumulated time BEFORE switching
            if self.active == "A" and self.last_switch:
                elapsed = (now - self.last_switch).total_seconds()
                self.time_a += elapsed
            elif self.active == "B" and self.last_switch:
                elapsed = (now - self.last_switch).total_seconds()
                self.time_b += elapsed

            # Record the switch
            switch_data = {
                'from_team': self.active,
                'to_team': team,
                'timestamp': now,
                'method': 'auto',
                'reason': reason
            }
            self.switches.append(switch_data)

            # Set new active team and reset timer
            self.active = team
            self.last_switch = now

            # Start the clock if this is the first switch
            if not self.clock_started:
                self.clock_started = True
        
        # Send notification to game with DMT scores (if enabled)
        if self.rcon_client and self.ingame_messages:
            team_name = self.team_names.get('allied' if team == 'A' else 'axis', 'Allies' if team == 'A' else 'Axis')
            allied_scores = self.calculate_dmt_score('allied')
            axis_scores = self.calculate_dmt_score('axis')
            team_a_name = self.team_names['allied']
            team_b_name = self.team_names['axis']

            msg = f"🔄 {team_name} captured the point! | {team_a_name}: Combat {allied_scores['combat_total']:,.0f} + Cap {allied_scores['cap_score']:,.0f} = {allied_scores['total_dmt']:,.0f} DMT | {team_b_name}: Combat {axis_scores['combat_total']:,.0f} + Cap {axis_scores['cap_score']:,.0f} = {axis_scores['total_dmt']:,.0f} DMT"
            await self.rcon_client.send_message(msg)
        
        # IMPORTANT: Update the Discord embed immediately
        if self.message:
            success = await safe_edit_message(self.message, embed=build_embed(self))
            if success:
                logger.info(f"Discord embed updated after auto-switch to {team}")
            else:
                self.message = None
            
        logger.info(f"Auto-switched to team {team}: {reason}")
    
    def is_rcon_connected(self):
        """True only if the socket is actually alive right now."""
        return bool(self.rcon_client and self.rcon_client.connected)

    def data_age_seconds(self):
        """Seconds since the last successful poll, or None if never polled."""
        if not self.last_update:
            return None
        return (datetime.datetime.now(timezone.utc) - self.last_update).total_seconds()

    def get_game_info(self):
        """Get formatted game information directly from RCON V2 data.

        connection_status reflects the LIVE socket, never the presence of stale
        data. Callers gate auto-stop on it, so reporting 'Connected' off a
        frozen snapshot would end matches on numbers minutes out of date.
        """
        age    = self.data_age_seconds()
        alive  = self.is_rcon_connected()
        stale  = age is not None and age > STALE_DATA_SECONDS

        if not self.game_data:
            return {
                'map': 'No Connection', 'players': 0, 'game_time': 0,
                'connection_status': 'Connected' if alive else 'Disconnected',
                'last_update': 'Never', 'stale': True, 'data_age': age,
            }

        if alive and not stale:
            status = 'Connected'
        elif alive:
            status = 'Stale'
        else:
            status = 'Disconnected'

        return {
            'map':               self.game_data.get('map', 'Unknown'),
            'players':           self.game_data.get('allied_players', 0) + self.game_data.get('axis_players', 0),
            'game_time':         self.game_data.get('time_remaining', 0),
            'connection_status': status,
            'last_update':       self.last_update.strftime('%H:%M:%S') if self.last_update else 'Never',
            'stale':             stale or not alive,
            'data_age':          age,
        }

    def format_time(self, secs):
        return str(datetime.timedelta(seconds=max(0, int(secs))))

    def update_player_scores(self):
        """Build player_scores from the flat RCON V2 player list."""
        self.player_scores = {'allied': {}, 'axis': {}}
        if not self.game_data:
            return
        for p in self.game_data.get('players', []):
            team    = p.get('team', '')
            if team not in ('allies', 'axis'):
                continue
            team_key = 'allied' if team == 'allies' else 'axis'
            platoon  = (p.get('platoon', '') or 'unknown').lower()
            if platoon not in self.player_scores[team_key]:
                self.player_scores[team_key][platoon] = []
            self.player_scores[team_key][platoon].append({
                'name':         p.get('name', 'Unknown'),
                'combat_score': p.get('combat', 0),
            })

    def calculate_dmt_score(self, team_key):
        """Calculate DMT Total Score for a team"""
        if not self.tournament_mode:
            return 0

        player_scores = self.player_scores.get(team_key, {})

        # Calculate combat score: 3 × (sum of highest scorer from EACH squad)
        # Count ALL squads, not just configured ones
        crew_scores = []
        commander_score = 0

        for squad_name, squad_players in player_scores.items():
            if not squad_players:
                continue

            # Find highest combat score in this squad
            highest_score = max(p['combat_score'] for p in squad_players)

            # Check if this is a commander squad (usually 'command' or 'commander')
            if squad_name.lower() in ['command', 'commander', 'cmd']:
                commander_score = highest_score
            else:
                crew_scores.append(highest_score)

        # 3 × (crew highs + commander) — commander is inside the multiplier
        combat_total = 3 * (sum(crew_scores) + commander_score)

        logger.debug(f"DMT Calc [{team_key}]: crews={crew_scores}, commander={commander_score}, combat_total={combat_total}")

        # Cap score
        cap_seconds = self.total_time('A' if team_key == 'allied' else 'B')
        cap_score   = cap_seconds * 0.5

        # First cap bonus (285) — team that got the first auto/manual switch
        first_cap_bonus = 0
        if self.switches:
            first_team = self.switches[0].get('to_team')
            if (team_key == 'allied' and first_team == 'A') or (team_key == 'axis' and first_team == 'B'):
                first_cap_bonus = 285

        # Held mid bonus (285) — whoever holds the point, or held it at the
        # final whistle. The final_holder fallback is what makes this bonus
        # survive into the finished score.
        holder = self.active or self.final_holder
        held_mid_bonus = 0
        if holder:
            if (team_key == 'allied' and holder == 'A') or (team_key == 'axis' and holder == 'B'):
                held_mid_bonus = 285

        total_dmt = combat_total + cap_score + first_cap_bonus + held_mid_bonus

        return {
            'crew_scores':      crew_scores,
            'commander_score':  commander_score,
            'combat_total':     combat_total,
            'cap_seconds':      cap_seconds,
            'cap_score':        cap_score,
            'first_cap_bonus':  first_cap_bonus,
            'held_mid_bonus':   held_mid_bonus,
            'total_dmt':        total_dmt,
        }

def user_is_admin(interaction: discord.Interaction):
    admin_role = os.getenv('ADMIN_ROLE_NAME', 'admin').lower()
    return any(role.name.lower() == admin_role for role in interaction.user.roles)

async def safe_edit_message(message, **kwargs):
    """Safely edit a Discord message with error handling"""
    if not message:
        return False
    try:
        await message.edit(**kwargs)
        return True
    except discord.NotFound:
        logger.warning("Message was deleted, cannot update")
        return False
    except discord.HTTPException as e:
        logger.error(f"Failed to edit message: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error editing message: {e}")
        return False

def build_embed(clock: ClockState):
    """Build Discord embed with DMT Scoring"""
    embed = discord.Embed(
        title="⚙️ DMT Score Keeper ⚙️",
        description="",
        color=0xFFD700  # Gold color
    )

    # Add game information
    game_info = clock.get_game_info()

    # Start with map and players
    embed.description += f"\n🗺️ **Map:** {game_info['map']}\n👥 **Players:** {game_info['players']}/100"

    # Add server game time instead of match duration
    if game_info['game_time'] > 0:
        embed.description += f"\n⏰ **Game Time:** `{clock.format_time(game_info['game_time'])}`"
    
    # Get live status for both teams
    allies_status = clock.get_live_status('A')
    axis_status = clock.get_live_status('B')
    
    # Build team information focused on TIME CONTROL
    allies_value = f"**Cap Time:** `{clock.format_time(allies_status['total_time'])}`\n**Status:** {allies_status['status']}"
    axis_value = f"**Cap Time:** `{clock.format_time(axis_status['total_time'])}`\n**Status:** {axis_status['status']}"
    
    # Add current session info for active team
    if allies_status['is_active'] and allies_status['current_session'] > 0:
        allies_value += f"\n**Current Hold:** `{clock.format_time(allies_status['current_session'])}`"
    elif axis_status['is_active'] and axis_status['current_session'] > 0:
        axis_value += f"\n**Current Hold:** `{clock.format_time(axis_status['current_session'])}`"
    
    # Add time advantage calculation
    time_diff = abs(allies_status['total_time'] - axis_status['total_time'])
    if allies_status['total_time'] > axis_status['total_time']:
        allies_value += f"\n**Advantage:** `+{clock.format_time(time_diff)}`"
    elif axis_status['total_time'] > allies_status['total_time']:
        axis_value += f"\n**Advantage:** `+{clock.format_time(time_diff)}`"
    
    # Get team names
    allied_name = clock.team_names.get('allied', 'Allies')
    axis_name = clock.team_names.get('axis', 'Axis')

    embed.add_field(name=f"🇺🇸 {allied_name}", value=allies_value, inline=False)
    embed.add_field(name=f"🇩🇪 {axis_name}", value=axis_value, inline=False)

    # Calculate and show DMT scores
    allied_scores = clock.calculate_dmt_score('allied')
    axis_scores = clock.calculate_dmt_score('axis')

    # Show DMT scores
    dmt_allied = f"**TOTAL SCORE: {allied_scores['total_dmt']:,.1f}**\n"
    dmt_allied += f"Combat: {allied_scores['combat_total']:,.0f} | Cap: {allied_scores['cap_score']:,.1f}"
    if allied_scores['first_cap_bonus']:
        dmt_allied += f" | First Cap: +285"
    if allied_scores['held_mid_bonus']:
        dmt_allied += f" | Held Mid: +285"

    dmt_axis = f"**TOTAL SCORE: {axis_scores['total_dmt']:,.1f}**\n"
    dmt_axis += f"Combat: {axis_scores['combat_total']:,.0f} | Cap: {axis_scores['cap_score']:,.1f}"
    if axis_scores['first_cap_bonus']:
        dmt_axis += f" | First Cap: +285"
    if axis_scores['held_mid_bonus']:
        dmt_axis += f" | Held Mid: +285"

    embed.add_field(name=f"🇺🇸 {allied_name}", value=dmt_allied, inline=True)
    embed.add_field(name=f"🇩🇪 {axis_name}", value=dmt_axis, inline=True)

    # Show leader
    if allied_scores['total_dmt'] > axis_scores['total_dmt']:
        diff = allied_scores['total_dmt'] - axis_scores['total_dmt']
        leader_text = f"🏆 **{allied_name}** leads by {diff:,.1f} points"
    elif axis_scores['total_dmt'] > allied_scores['total_dmt']:
        diff = axis_scores['total_dmt'] - allied_scores['total_dmt']
        leader_text = f"🏆 **{axis_name}** leads by {diff:,.1f} points"
    else:
        leader_text = "⚖️ **Tied**"

    embed.add_field(name="📊 Current Leader", value=leader_text, inline=False)
    
    # Footer with connection status — driven by the live socket, not by the
    # presence of a stale snapshot.
    status_label = game_info.get('connection_status', 'Disconnected')
    if status_label == 'Connected':
        connection_status = "🟢 RCON Connected"
    elif status_label == 'Stale':
        age = game_info.get('data_age') or 0
        connection_status = f"🟡 RCON Stale ({int(age)}s)"
    else:
        connection_status = "🔴 RCON Disconnected"
    auto_status = " | 🤖 Auto ON" if clock.auto_switch else " | 🤖 Auto OFF"
    msg_status = " | 💬 Msgs ON" if clock.ingame_messages else " | 💬 Msgs OFF"

    footer_text = f"Match Clock by {os.getenv('BOT_AUTHOR', 'StoneyRebel')} | {connection_status}{auto_status}{msg_status}"
    if game_info.get('last_update'):
        footer_text += f" | Updated: {game_info['last_update']}"
    
    embed.set_footer(text=footer_text)
    return embed

class StartControls(discord.ui.View):
    def __init__(self, channel_id):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="▶️ Start Match", style=discord.ButtonStyle.success)
    async def start_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not user_is_admin(interaction):
            return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

        # Respond to Discord immediately to prevent timeout
        await interaction.response.defer()

        clock = clocks[self.channel_id]
        clock.match_start_time = datetime.datetime.now(timezone.utc)
        clock.started = True

        # Start the updater first
        if not match_updater.is_running():
            match_updater.start(self.channel_id)

        view = TimerControls(self.channel_id)

        # Update the embed first
        await safe_edit_message(clock.message, embed=build_embed(clock), view=view)
        await interaction.followup.send("✅ Match started! Connecting to RCON V2...", ephemeral=True)

        # Connect to RCON V2 after responding to Discord
        rcon_connected = await clock.connect_rcon()

        if rcon_connected:
            clock.auto_switch = os.getenv('RCON_AUTO_SWITCH', 'true').lower() == 'true'

            # Send start message with DMT scoring info (if enabled)
            if clock.ingame_messages:
                team_a = clock.team_names['allied']
                team_b = clock.team_names['axis']
                start_msg = f"🏆 DMT Score Keeper: {team_a} vs {team_b} | DMT Scoring Active | Combat + Cap Time = Total Score"
                await clock.rcon_client.send_message(start_msg)

            await interaction.edit_original_response(content="✅ Match started with RCON V2!")
        else:
            await interaction.edit_original_response(content="✅ Match started (RCON connection failed)")

    @discord.ui.button(label="🔗 Test Connection", style=discord.ButtonStyle.secondary)
    async def test_rcon(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            test_client = HLLRconV2Client()
            await test_client.connect()
            await test_client.close()
            embed = discord.Embed(title="🟢 Connection Test - SUCCESS", color=0x00ff00)
            embed.add_field(name="Status", value="✅ Connected & authenticated", inline=False)
            embed.add_field(name="Host",   value=f"{os.getenv('RCON_HOST')}:{os.getenv('RCON_PORT', '7779')}", inline=True)
        except Exception as e:
            embed = discord.Embed(title="🔴 Connection Test - FAILED", color=0xff0000)
            embed.add_field(name="Error", value=str(e)[:1000], inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

class TimerControls(discord.ui.View):
    def __init__(self, channel_id):
        super().__init__(timeout=None)
        self.channel_id = channel_id

  #  @discord.ui.button(label="Allies", style=discord.ButtonStyle.success, emoji="🇺🇸")
   # async def switch_to_a(self, interaction: discord.Interaction, button: discord.ui.Button):
    #    await self._switch_team(interaction, "A")

  #  @discord.ui.button(label="Axis", style=discord.ButtonStyle.secondary, emoji="🇩🇪")
   # async def switch_to_b(self, interaction: discord.Interaction, button: discord.ui.Button):
    #    await self._switch_team(interaction, "B")

  #  @discord.ui.button(label="🤖 Auto", style=discord.ButtonStyle.secondary)
   # async def toggle_auto_switch(self, interaction: discord.Interaction, button: discord.ui.Button):
    #    if not user_is_admin(interaction):
     #       return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

      #  clock = clocks[self.channel_id]
       # clock.auto_switch = not clock.auto_switch

       # status = "enabled" if clock.auto_switch else "disabled"

       # await interaction.response.defer()
       # await safe_edit_message(clock.message, embed=build_embed(clock), view=self)

       # if clock.rcon_client and clock.ingame_messages:
        #    await clock.rcon_client.send_message(f"🤖 Auto-switch {status}")

   # @discord.ui.button(label="💬 Msgs", style=discord.ButtonStyle.secondary)
   # async def toggle_ingame_messages(self, interaction: discord.Interaction, button: discord.ui.Button):
    #    if not user_is_admin(interaction):
     #       return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

      #  clock = clocks[self.channel_id]
       # clock.ingame_messages = not clock.ingame_messages

       # status = "ON" if clock.ingame_messages else "OFF"

      #  await interaction.response.defer()
       # await safe_edit_message(clock.message, embed=build_embed(clock), view=self)
      #  await interaction.followup.send(f"💬 In-game messages: **{status}**", ephemeral=True)

  #  @discord.ui.button(label="📊 Stats", style=discord.ButtonStyle.secondary)
   # async def show_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
    #    clock = clocks[self.channel_id]
     #   await interaction.response.defer(ephemeral=True)
        
      #  if not clock.rcon_client:
       #     return await interaction.followup.send("❌ RCON not connected.", ephemeral=True)
        
      #  try:
       #     await clock.update_from_game()
        #    game_info = clock.get_game_info()
            
         #   embed = discord.Embed(title="📊 Live Match Stats", color=0x00ff00)
          #  embed.add_field(name="🗺️ Map", value=game_info['map'], inline=True)
          #  embed.add_field(name="👥 Players", value=f"{game_info['players']}/100", inline=True)
          #  embed.add_field(name="🔄 Point Switches", value=str(len(clock.switches)), inline=True)
            
            # Control time breakdown
            # allies_time = clock.total_time('A')
            # axis_time = clock.total_time('B')
            # total_control = allies_time + axis_time
            
           # if total_control > 0:
             #   allies_percent = (allies_time / total_control) * 100
            #    axis_percent = (axis_time / total_control) * 100
             #   
              #  embed.add_field(name="🇺🇸 Allies Control", value=f"{allies_percent:.1f}%", inline=True)
              #  embed.add_field(name="🇩🇪 Axis Control", value=f"{axis_percent:.1f}%", inline=True)
            
          #  embed.add_field(name="🤖 Auto-Switch", value="On" if clock.auto_switch else "Off", inline=True)
          #  embed.add_field(name="📡 Last Update", value=game_info['last_update'], inline=True)
            
           # await interaction.followup.send(embed=embed, ephemeral=True)
            
      #  except Exception as e:
          #  await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

   # @discord.ui.button(label="↺ Reset", style=discord.ButtonStyle.primary)
   # async def reset_timer(self, interaction: discord.Interaction, button: discord.ui.Button):
     #   if not user_is_admin(interaction):
         #   return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

       # old_clock = clocks[self.channel_id]
       # if old_clock.rcon_client:
         #   await old_clock.rcon_client.__aexit__(None, None, None)

     #   clocks[self.channel_id] = ClockState()
      #  clock = clocks[self.channel_id]
      #  view = StartControls(self.channel_id)

     #   await interaction.response.defer()
     #   embed = build_embed(clock)
     #   await interaction.followup.send(embed=embed, view=view)
      #  clock.message = await interaction.original_response()

    @discord.ui.button(label="⏹️ Manually Stop", style=discord.ButtonStyle.danger)
    async def stop_timer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not user_is_admin(interaction):
            return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

        clock = clocks[self.channel_id]

        # IMPORTANT: Finalize the current session before stopping
        async with clock._lock:
            if clock.active and clock.last_switch:
                elapsed = (datetime.datetime.now(timezone.utc) - clock.last_switch).total_seconds()
                if clock.active == "A":
                    clock.time_a += elapsed
                elif clock.active == "B":
                    clock.time_b += elapsed

            # Must happen BEFORE active is cleared, or the held-mid bonus
            # scores as zero in the final result.
            clock.final_holder = clock.active or clock.final_holder
            clock.active = None
            clock.started = False
            clock._fast_polling = False

        # Send final message to game with DMT scores (if enabled)
        if clock.rcon_client and clock.ingame_messages:
            allied_scores = clock.calculate_dmt_score('allied')
            axis_scores = clock.calculate_dmt_score('axis')
            team_a_name = clock.team_names['allied']
            team_b_name = clock.team_names['axis']

            if allied_scores['total_dmt'] > axis_scores['total_dmt']:
                winner_msg = f"{team_a_name} WINS!"
            elif axis_scores['total_dmt'] > allied_scores['total_dmt']:
                winner_msg = f"{team_b_name} WINS!"
            else:
                winner_msg = "DRAW!"

            await clock.rcon_client.send_message(
                f"🏁 MATCH COMPLETE! {winner_msg} | {team_a_name}: Combat {allied_scores['combat_total']:,.0f} + Cap {allied_scores['cap_score']:,.0f} = {allied_scores['total_dmt']:,.0f} DMT | {team_b_name}: Combat {axis_scores['combat_total']:,.0f} + Cap {axis_scores['cap_score']:,.0f} = {axis_scores['total_dmt']:,.0f} DMT"
            )

        # Create final embed with DMT scores
        allied_scores = clock.calculate_dmt_score('allied')
        axis_scores = clock.calculate_dmt_score('axis')
        team_a_name = clock.team_names['allied']
        team_b_name = clock.team_names['axis']

        embed = discord.Embed(title="🏁 Match Complete - DMT Results!", color=0xFFD700)

        game_info = clock.get_game_info()
        if game_info['connection_status'] == 'Connected':
            embed.add_field(name="🗺️ Map", value=game_info['map'], inline=True)
            embed.add_field(name="👥 Players", value=f"{game_info['players']}/100", inline=True)

        # Final DMT scores
        embed.add_field(
            name=f"🇺🇸 {team_a_name} - Final DMT",
            value=f"**{allied_scores['total_dmt']:,.1f} DMT**\nCombat: {allied_scores['combat_total']:,.0f}\nCap: {allied_scores['cap_score']:,.1f} ({clock.format_time(clock.time_a)})",
            inline=True
        )
        embed.add_field(
            name=f"🇩🇪 {team_b_name} - Final DMT",
            value=f"**{axis_scores['total_dmt']:,.1f} DMT**\nCombat: {axis_scores['combat_total']:,.0f}\nCap: {axis_scores['cap_score']:,.1f} ({clock.format_time(clock.time_b)})",
            inline=True
        )

        # Determine winner by DMT score
        if allied_scores['total_dmt'] > axis_scores['total_dmt']:
            dmt_diff = allied_scores['total_dmt'] - axis_scores['total_dmt']
            winner = f"🏆 **{team_a_name} Victory**\n*+{dmt_diff:,.1f} DMT advantage*"
        elif axis_scores['total_dmt'] > allied_scores['total_dmt']:
            dmt_diff = axis_scores['total_dmt'] - allied_scores['total_dmt']
            winner = f"🏆 **{team_b_name} Victory**\n*+{dmt_diff:,.1f} DMT advantage*"
        else:
            winner = "🤝 **Perfect Draw**\n*Equal DMT scores*"

        embed.add_field(name="🎯 DMT Winner", value=winner, inline=False)
        embed.add_field(name="🔄 Total Switches", value=str(len(clock.switches)), inline=True)

        await interaction.response.defer()
        await safe_edit_message(clock.message, embed=embed, view=None)

        # Log results
        await log_results(clock, game_info)

        # Post to Devil Dave in the background (don't block Discord response)
        dd_winner = 'draw'
        if allied_scores['total_dmt'] > axis_scores['total_dmt']:
            dd_winner = 'allies'
        elif axis_scores['total_dmt'] > allied_scores['total_dmt']:
            dd_winner = 'axis'
        asyncio.create_task(post_results_to_devil_dave(
            clock, dd_winner,
            posted_by=str(interaction.user) if hasattr(interaction, 'user') else None
        ))

    async def _switch_team(self, interaction: discord.Interaction, team: str):
        if not user_is_admin(interaction):
            return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

        clock = clocks[self.channel_id]

        async with clock._lock:
            now = datetime.datetime.now(timezone.utc)

            switch_data = {
                'from_team': clock.active,
                'to_team': team,
                'timestamp': now,
                'method': 'manual'
            }

            if not clock.clock_started:
                # First switch - start the clock
                clock.clock_started = True
                clock.last_switch = now
                clock.active = team
                clock.switches = [switch_data]
            else:
                # Subsequent switches - accumulate time properly
                elapsed = (now - clock.last_switch).total_seconds()

                # Add elapsed time to the previously active team
                if clock.active == "A":
                    clock.time_a += elapsed
                elif clock.active == "B":
                    clock.time_b += elapsed

                # Switch to new team
                clock.active = team
                clock.last_switch = now
                clock.switches.append(switch_data)

        # Send notification with DMT scores (if enabled)
        if clock.rcon_client and clock.ingame_messages:
            team_name = clock.team_names.get('allied' if team == 'A' else 'axis', 'Allies' if team == 'A' else 'Axis')
            allied_scores = clock.calculate_dmt_score('allied')
            axis_scores = clock.calculate_dmt_score('axis')
            team_a_name = clock.team_names['allied']
            team_b_name = clock.team_names['axis']

            msg = f"⚔️ {team_name} captured the point! | {team_a_name}: Combat {allied_scores['combat_total']:,.0f} + Cap {allied_scores['cap_score']:,.0f} = {allied_scores['total_dmt']:,.0f} DMT | {team_b_name}: Combat {axis_scores['combat_total']:,.0f} + Cap {axis_scores['cap_score']:,.0f} = {axis_scores['total_dmt']:,.0f} DMT"
            await clock.rcon_client.send_message(msg)

        await interaction.response.defer()
        await safe_edit_message(clock.message, embed=build_embed(clock), view=self)

async def post_results_to_devil_dave(clock: 'ClockState', winner: str, posted_by: str = None):
    """Post match results to Devil Dave event stats API."""
    if not DEVIL_DAVE_URL or not DEVIL_DAVE_API_KEY:
        return  # Not configured, skip silently

    try:
        url = f"{DEVIL_DAVE_URL.rstrip('/')}/api/event-stats/dmt/result"

        # Build player scores summary
        raw_data = {
            'player_scores': {}
        }
        if hasattr(clock, 'player_scores') and clock.player_scores:
            for team, squads in clock.player_scores.items():
                raw_data['player_scores'][team] = {}
                for squad, players in squads.items():
                    raw_data['player_scores'][team][squad] = players

        # Calculate final DMT scores
        allied_scores = clock.calculate_dmt_score('allied')
        axis_scores = clock.calculate_dmt_score('axis')

        # Get map and player count from game info
        game_info = clock.get_game_info()
        current_map = game_info.get('map', None)
        player_count = game_info.get('players', 0)

        payload = {
            'allies_team_name':   clock.team_names.get('allied', 'Allies'),
            'axis_team_name':     clock.team_names.get('axis', 'Axis'),
            'allies_dmt':         round(float(allied_scores['total_dmt'] or 0), 2),
            'axis_dmt':           round(float(axis_scores['total_dmt'] or 0), 2),
            'allies_combat':      round(float(allied_scores['combat_total'] or 0), 2),
            'axis_combat':        round(float(axis_scores['combat_total'] or 0), 2),
            'allies_cap_seconds': int(clock.time_a or 0),
            'axis_cap_seconds':   int(clock.time_b or 0),
            'winner':             winner,  # 'allies', 'axis', or 'draw'
            'map_name':           current_map,
            'player_count':       int(player_count or 0),
            'switch_count':       len(clock.switches or []),
            'posted_by':          posted_by,
            'raw_data':           raw_data,
        }

        # Add optional event_id override
        if DEVIL_DAVE_EVENT_ID:
            payload['event_id'] = int(DEVIL_DAVE_EVENT_ID)

        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key':    DEVIL_DAVE_API_KEY,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print(f"[DevilDave] Match result posted: {data.get('winner', '?').upper()} wins, match #{data.get('match_number', '?')}")
                else:
                    text = await resp.text()
                    print(f"[DevilDave] Failed to post result (HTTP {resp.status}): {text[:200]}")
    except Exception as e:
        print(f"[DevilDave] Error posting match result: {e}")


async def log_results(clock: ClockState, game_info: dict):
    """Log match results focused on time control"""
    if not LOG_CHANNEL_ID:
        return
        
    results_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not results_channel:
        return
    
    embed = discord.Embed(title="🏁 DMT Score Keeper - Match Complete", color=0x800020)
    embed.add_field(name="🇺🇸 Allies Control Time", value=f"`{clock.format_time(clock.time_a)}`", inline=True)
    embed.add_field(name="🇩🇪 Axis Control Time", value=f"`{clock.format_time(clock.time_b)}`", inline=True)
    
    # Winner by time control
    if clock.time_a > clock.time_b:
        winner = "🏆 Allies"
        advantage = clock.format_time(clock.time_a - clock.time_b)
    elif clock.time_b > clock.time_a:
        winner = "🏆 Axis"
        advantage = clock.format_time(clock.time_b - clock.time_a)
    else:
        winner = "🤝 Draw"
        advantage = "0:00:00"
    
    embed.add_field(name="Winner", value=winner, inline=True)
    embed.add_field(name="Advantage", value=f"`+{advantage}`", inline=True)
    
    if game_info['connection_status'] == 'Connected':
        embed.add_field(name="🗺️ Map", value=game_info['map'], inline=True)
    
    embed.add_field(name="🔄 Switches", value=str(len(clock.switches)), inline=True)
    embed.timestamp = datetime.datetime.now(timezone.utc)
    
    await results_channel.send(embed=embed)

# Validate and parse update interval
def get_update_interval():
    """Get and validate update interval from environment"""
    try:
        interval = int(os.getenv('UPDATE_INTERVAL', '15'))
        # Clamp to reasonable bounds
        return max(MIN_UPDATE_INTERVAL, min(interval, MAX_UPDATE_INTERVAL))
    except ValueError:
        logger.warning(f"Invalid UPDATE_INTERVAL, using default: 15")
        return 15

async def fast_poll_end(channel_id):
    """Poll every 2 seconds once the match is nearly over, stop at exactly 0."""
    clock = clocks.get(channel_id)
    if not clock:
        return
    logger.info("Fast poll started — match ending soon")
    while clock.started and clock._fast_polling:
        try:
            if await clock.ensure_rcon():
                try:
                    await clock.update_from_game()
                except RCON_TRANSPORT_ERRORS as e:
                    logger.warning(f"Fast poll: RCON lost ({e}) — reconnecting")
                except Exception as e:
                    logger.error(f"Fast poll RCON error: {e}")

            game_info = clock.get_game_info()
            time_left  = game_info['game_time']

            await safe_edit_message(clock.message, embed=build_embed(clock))

            # Only end the match on a LIVE reading. Ending on a frozen snapshot
            # would post a score built from minutes-old data.
            if game_info['connection_status'] == 'Connected' and time_left <= GAME_END_THRESHOLD:
                logger.info(f"Fast poll: time={time_left}s — triggering match end")
                clock._fast_polling = False
                await auto_stop_match(clock, game_info)
                return

            # Clock climbed back out of the endgame window (new round, or the
            # first good reading after a reconnect) — hand back to the slow loop
            # instead of spinning here at 2s forever.
            if game_info['connection_status'] == 'Connected' and time_left > FAST_POLL_THRESHOLD:
                logger.info(f"Fast poll: time back up to {time_left}s — resuming normal polling")
                clock._fast_polling = False
                return
        except Exception as e:
            logger.error(f"Error in fast poll: {e}")

        await asyncio.sleep(2)

    clock._fast_polling = False
    logger.info("Fast poll stopped")

# Update task - shows in-game time
@tasks.loop(seconds=get_update_interval())
async def match_updater(channel_id):
    """Update match display with live game time"""
    clock = clocks.get(channel_id)
    if not clock or not clock.started or not clock.message:
        return

    # If fast poll is active, let it handle everything
    if clock._fast_polling:
        return

    try:
        # Rebuild the socket first if it died, then poll.
        if await clock.ensure_rcon():
            try:
                await clock.update_from_game()
            except RCON_TRANSPORT_ERRORS as e:
                logger.warning(f"RCON lost during update ({e}) — reconnecting next tick")
            except Exception as e:
                logger.error(f"RCON update failed: {e}")

        game_info = clock.get_game_info()
        time_left  = game_info['game_time']

        # Switch to fast polling when close to end
        if game_info['connection_status'] == 'Connected' and 0 < time_left <= FAST_POLL_THRESHOLD:
            if not clock._fast_polling:
                clock._fast_polling = True
                asyncio.create_task(fast_poll_end(channel_id))
            return

        # Update display with current game time
        success = await safe_edit_message(clock.message, embed=build_embed(clock))
        if not success:
            clock.message = None

    except Exception as e:
        logger.error(f"Error in match updater: {e}")

async def auto_stop_match(clock: ClockState, game_info: dict):
    """Automatically stop match when game time ends"""
    try:
        # IMPORTANT: Finalize the current session before stopping
        async with clock._lock:
            if clock.active and clock.last_switch:
                elapsed = (datetime.datetime.now(timezone.utc) - clock.last_switch).total_seconds()
                if clock.active == "A":
                    clock.time_a += elapsed
                elif clock.active == "B":
                    clock.time_b += elapsed

            # Must happen BEFORE active is cleared, or the held-mid bonus
            # scores as zero in the final result.
            clock.final_holder = clock.active or clock.final_holder
            clock.active = None
            clock.started = False
            clock._fast_polling = False

        # Send final message to game with DMT scores (if enabled)
        if clock.rcon_client and clock.ingame_messages:
            allied_scores = clock.calculate_dmt_score('allied')
            axis_scores = clock.calculate_dmt_score('axis')
            team_a_name = clock.team_names['allied']
            team_b_name = clock.team_names['axis']

            if allied_scores['total_dmt'] > axis_scores['total_dmt']:
                winner_msg = f"{team_a_name} WINS!"
            elif axis_scores['total_dmt'] > allied_scores['total_dmt']:
                winner_msg = f"{team_b_name} WINS!"
            else:
                winner_msg = "DRAW!"

            await clock.rcon_client.send_message(
                f"🏁 MATCH COMPLETE! {winner_msg} | {team_a_name}: Combat {allied_scores['combat_total']:,.0f} + Cap {allied_scores['cap_score']:,.0f} = {allied_scores['total_dmt']:,.0f} DMT | {team_b_name}: Combat {axis_scores['combat_total']:,.0f} + Cap {axis_scores['cap_score']:,.0f} = {axis_scores['total_dmt']:,.0f} DMT"
            )

        # Create final embed with DMT scores
        allied_scores = clock.calculate_dmt_score('allied')
        axis_scores = clock.calculate_dmt_score('axis')
        team_a_name = clock.team_names['allied']
        team_b_name = clock.team_names['axis']

        embed = discord.Embed(title="🏁 Match Complete - DMT Results!", color=0xFFD700)
        embed.add_field(name="🕐 End Reason", value="⏰ Game Time Expired", inline=False)

        if game_info['connection_status'] == 'Connected':
            embed.add_field(name="🗺️ Map", value=game_info['map'], inline=True)
            embed.add_field(name="👥 Players", value=f"{game_info['players']}/100", inline=True)

        # Final DMT scores
        embed.add_field(
            name=f"🇺🇸 {team_a_name}",
            value=f"TOTAL: **{allied_scores['total_dmt']:,.1f}**\nCombat Score: {allied_scores['combat_total']:,.0f}\nCap Score: {allied_scores['cap_score']:,.1f} ({clock.format_time(clock.time_a)})",
            inline=True
        )
        embed.add_field(
            name=f"🇩🇪 {team_b_name}",
            value=f"TOTAL: **{axis_scores['total_dmt']:,.1f}**\nCombat Score: {axis_scores['combat_total']:,.0f}\nCap Score: {axis_scores['cap_score']:,.1f} ({clock.format_time(clock.time_b)})",
            inline=True
        )

        # Determine winner by DMT score
        if allied_scores['total_dmt'] > axis_scores['total_dmt']:
            dmt_diff = allied_scores['total_dmt'] - axis_scores['total_dmt']
            winner = f"🏆 **{team_a_name} Victory**\n*+{dmt_diff:,.1f} DMT advantage*"
        elif axis_scores['total_dmt'] > allied_scores['total_dmt']:
            dmt_diff = axis_scores['total_dmt'] - allied_scores['total_dmt']
            winner = f"🏆 **{team_b_name} Victory**\n*+{dmt_diff:,.1f} DMT advantage*"
        else:
            winner = "🤝 **Perfect Draw**\n*Equal DMT scores*"

        embed.add_field(name="🎯 Winner", value=winner, inline=False)
        embed.add_field(name="🔄 Total Switches", value=str(len(clock.switches)), inline=True)

        # Update the message with final results
        await safe_edit_message(clock.message, embed=embed, view=None)
        
        # Also post to the channel (not just edit the existing message)
        channel = clock.message.channel
        await channel.send("🏁 **MATCH COMPLETE!** 🏁", embed=embed)

        # Log results to log channel
        await log_results(clock, game_info)

        # Post to Devil Dave in the background (don't block)
        dd_winner = 'draw'
        if allied_scores['total_dmt'] > axis_scores['total_dmt']:
            dd_winner = 'allies'
        elif axis_scores['total_dmt'] > allied_scores['total_dmt']:
            dd_winner = 'axis'
        asyncio.create_task(post_results_to_devil_dave(clock, dd_winner, posted_by='auto'))

        logger.info("Match automatically stopped due to game time expiring")

    except Exception as e:
        logger.error(f"Error in auto_stop_match: {e}")

# Bot commands
@bot.tree.command(name="reverse_clock", description="Start the DMT Score Keeper")
async def reverse_clock(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    clocks[channel_id] = ClockState()

    embed = build_embed(clocks[channel_id])
    view = StartControls(channel_id)

    await interaction.response.send_message("✅ DMT Score Keeper is ready!", ephemeral=True)
    posted_message = await interaction.channel.send(embed=embed, view=view)
    clocks[channel_id].message = posted_message

@bot.tree.command(name="rcon_status", description="Check HLL Server connection status")
async def rcon_status(interaction: discord.Interaction):
    await interaction.response.defer()

    embed = discord.Embed(title="🔗 RCON V2 Status", color=0x0099ff)

    try:
        test_client = HLLRconV2Client()
        await test_client.connect()
        await test_client.close()
        embed.add_field(name="Connection", value="✅ Connected",             inline=True)
        embed.add_field(name="Auth",       value="✅ Authenticated",          inline=True)
        embed.add_field(name="Server",     value="🟢 Online",                 inline=True)
    except Exception as e:
        embed.add_field(name="Connection", value="❌ Failed",       inline=True)
        embed.add_field(name="Error",      value=str(e)[:500],      inline=False)

    embed.add_field(name="Host", value=os.getenv('RCON_HOST', 'Not set'), inline=True)
    embed.add_field(name="Port", value=os.getenv('RCON_PORT', '7779'),    inline=True)
    embed.add_field(name="Password", value="✅ Set" if os.getenv('RCON_PASSWORD') else '❌ Not set', inline=True)

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="server_info", description="Get current HLL server information")
async def server_info(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        test_client = HLLRconV2Client()
        async with test_client as client:
            live_data = await client.get_live_game_state()

            if not live_data:
                return await interaction.followup.send("❌ Could not retrieve server information")

            gs           = live_data.get('game_state', {}).get('result', {})
            map_name     = live_data.get('map_info', {}).get('result', {}).get('pretty_name', 'Unknown')
            player_count = gs.get('num_allied_players', 0) + gs.get('num_axis_players', 0)
            time_rem     = gs.get('time_remaining', 0)

            embed = discord.Embed(title="🎮 HLL Server Information", color=0x00ff00)
            embed.add_field(name="🗺️ Map",     value=map_name,              inline=True)
            embed.add_field(name="👥 Players", value=f"{player_count}/100", inline=True)
            if time_rem > 0:
                embed.add_field(name="⏱️ Game Time", value=f"{time_rem//60}:{time_rem%60:02d}", inline=True)
            embed.add_field(name="🇺🇸 Allied Score", value=str(gs.get('allied_score', 0)), inline=True)
            embed.add_field(name="🇩🇪 Axis Score",   value=str(gs.get('axis_score', 0)),  inline=True)
            embed.timestamp = datetime.datetime.now(timezone.utc)
            await interaction.followup.send(embed=embed)
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error retrieving server info: {str(e)}")

@bot.tree.command(name="test_map", description="Debug game state and player count")
async def test_map(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        test_client = HLLRconV2Client()
        async with test_client as client:
            live_data = await client.get_live_game_state()

            if not live_data:
                return await interaction.followup.send("❌ No data", ephemeral=True)

            game_state     = live_data.get('game_state', {})
            game_state_str = json.dumps(game_state, indent=2)

            embed = discord.Embed(title="📊 Game State Debug (RCON V2)", color=0x00ff00)
            gs = game_state.get('result', {})
            if isinstance(gs, dict):
                embed.add_field(name="Keys", value=str(list(gs.keys())), inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)

            chunk_size = 1900
            for i in range(0, min(len(game_state_str), 3800), chunk_size):
                chunk = game_state_str[i:i+chunk_size]
                await interaction.followup.send(f"```json\n{chunk}\n```", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="test_player_scores", description="Test RCON V2 player combat score data")
async def test_player_scores(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        test_client = HLLRconV2Client()
        async with test_client as client:
            live_data = await client.get_live_game_state()

            if not live_data or 'detailed_players' not in live_data:
                return await interaction.followup.send("❌ No detailed player data available", ephemeral=True)

            detailed = live_data['detailed_players']
            data_str = json.dumps(detailed, indent=2)

            embed = discord.Embed(title="📊 RCON V2 Player Data", color=0x00ff00)
            players_dict = detailed.get('result', {}).get('players', {})
            embed.add_field(name="Players found", value=str(len(players_dict)), inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)

            chunk_size = 1900
            for i in range(0, min(len(data_str), 5700), chunk_size):
                chunk = data_str[i:i+chunk_size]
                await interaction.followup.send(f"```json\n{chunk}\n```", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="send_message", description="Send a message to the HLL server")
async def send_server_message(interaction: discord.Interaction, message: str):
    if not user_is_admin(interaction):
        return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

    # Input validation
    if not message or not message.strip():
        return await interaction.response.send_message("❌ Message cannot be empty.", ephemeral=True)

    # Sanitize message - limit length
    message = message.strip()[:500]  # Limit to 500 chars to prevent abuse

    await interaction.response.defer(ephemeral=True)

    try:
        test_client = HLLRconV2Client()
        async with test_client as client:
            success = await client.send_message(f"📢 [Discord] {message}")

            if success:
                embed = discord.Embed(
                    title="📢 Message Sent",
                    description=f"Successfully sent to players:\n\n*{message}*",
                    color=0x00ff00
                )
            else:
                embed = discord.Embed(
                    title="⚠️ Message Failed",
                    description="MessagePlayer command did not succeed",
                    color=0xffaa00
                )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="set_team_names", description="Set custom team names for the match")
async def set_team_names_cmd(interaction: discord.Interaction, team_a: str = "Allies", team_b: str = "Axis"):
    """Set custom team names for DMT scoring display"""
    if not user_is_admin(interaction):
        return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

    channel_id = interaction.channel_id
    if channel_id not in clocks:
        return await interaction.response.send_message("❌ No active clock in this channel. Use /reverse_clock first.", ephemeral=True)

    clock = clocks[channel_id]
    clock.team_names['allied'] = team_a
    clock.team_names['axis'] = team_b

    embed = discord.Embed(title="✅ Team Names Updated", color=0x00ff00)
    embed.add_field(name="Allied Team", value=team_a, inline=True)
    embed.add_field(name="Axis Team", value=team_b, inline=True)
    embed.add_field(name="Scoring", value="DMT Total Score (Always Active)", inline=False)
    embed.add_field(name="Formula", value="Combat: 3×(Crew1+Crew2+Crew3+Crew4) + Commander\nCap: Seconds × 0.5\nTotal DMT: Combat + Cap", inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="set_crew_squads", description="Configure which squads represent which crews")
async def set_crew_squads(
    interaction: discord.Interaction,
    team: str,
    crew1: str = "Able",
    crew2: str = "Baker",
    crew3: str = "Charlie",
    crew4: str = "Dog",
    commander: str = "Command"
):
    """Configure squad-to-crew mapping"""
    if not user_is_admin(interaction):
        return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

    channel_id = interaction.channel_id
    if channel_id not in clocks:
        return await interaction.response.send_message("❌ No active clock in this channel.", ephemeral=True)

    clock = clocks[channel_id]
    team_key = 'allied' if team.lower() in ['allied', 'allies', 'a'] else 'axis'

    clock.squad_config[team_key] = {
        'crew1': crew1,
        'crew2': crew2,
        'crew3': crew3,
        'crew4': crew4,
        'commander': commander
    }

    team_name = clock.team_names[team_key]
    embed = discord.Embed(title=f"⚙️ Squad Configuration - {team_name}", color=0x0099ff)
    embed.add_field(name="Crew 1", value=crew1, inline=True)
    embed.add_field(name="Crew 2", value=crew2, inline=True)
    embed.add_field(name="Crew 3", value=crew3, inline=True)
    embed.add_field(name="Crew 4", value=crew4, inline=True)
    embed.add_field(name="Commander", value=commander, inline=True)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="dmt_scores", description="Show current DMT scores")
async def dmt_scores(interaction: discord.Interaction):
    """Display current DMT scores"""
    channel_id = interaction.channel_id
    if channel_id not in clocks:
        return await interaction.response.send_message("❌ No active clock in this channel.", ephemeral=True)

    clock = clocks[channel_id]
    await interaction.response.defer()

    # Calculate DMT scores
    allied_scores = clock.calculate_dmt_score('allied')
    axis_scores = clock.calculate_dmt_score('axis')

    embed = discord.Embed(title="🏆 DMT Tournament Scores", color=0xFFD700)

    # Allied team
    allied_name = clock.team_names['allied']
    embed.add_field(
        name=f"🇺🇸 {allied_name}",
        value=f"**Total DMT: {allied_scores['total_dmt']:,.1f}**\n"
              f"Combat: {allied_scores['combat_total']:,.0f}\n"
              f"Cap: {allied_scores['cap_score']:,.1f} ({clock.format_time(allied_scores['cap_seconds'])})",
        inline=False
    )

    # Show crew breakdown
    crew_breakdown = f"Crews: {' | '.join(f'{s:,}' for s in allied_scores['crew_scores'])}\n"
    crew_breakdown += f"Commander: {allied_scores['commander_score']:,}"
    embed.add_field(name=f"{allied_name} Breakdown", value=crew_breakdown, inline=False)

    # Axis team
    axis_name = clock.team_names['axis']
    embed.add_field(
        name=f"🇩🇪 {axis_name}",
        value=f"**Total DMT: {axis_scores['total_dmt']:,.1f}**\n"
              f"Combat: {axis_scores['combat_total']:,.0f}\n"
              f"Cap: {axis_scores['cap_score']:,.1f} ({clock.format_time(axis_scores['cap_seconds'])})",
        inline=False
    )

    # Show crew breakdown
    crew_breakdown = f"Crews: {' | '.join(f'{s:,}' for s in axis_scores['crew_scores'])}\n"
    crew_breakdown += f"Commander: {axis_scores['commander_score']:,}"
    embed.add_field(name=f"{axis_name} Breakdown", value=crew_breakdown, inline=False)

    # Winner
    if allied_scores['total_dmt'] > axis_scores['total_dmt']:
        diff = allied_scores['total_dmt'] - axis_scores['total_dmt']
        winner = f"🏆 **{allied_name}** leads by {diff:,.1f} points"
    elif axis_scores['total_dmt'] > allied_scores['total_dmt']:
        diff = axis_scores['total_dmt'] - allied_scores['total_dmt']
        winner = f"🏆 **{axis_name}** leads by {diff:,.1f} points"
    else:
        winner = "⚖️ **Tied**"

    embed.add_field(name="Current Leader", value=winner, inline=False)

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="help_clock", description="Show help for the time control clock")
async def help_clock(interaction: discord.Interaction):
    embed = discord.Embed(title="🎯 DMT Score Keeper Help", color=0x0099ff)
    
    embed.add_field(
        name="📋 Commands",
        value=(
            "`/reverse_clock` - Start a new time control clock\n"
            "`/rcon_status` - Check RCON V2 connection\n"
            "`/server_info` - Get current server info\n"
            "`/send_message` - Send message to server (admin)\n"
            "`/test_map` - Test map data retrieval\n"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🎮 How to Use",
        value=(
            "1. Use `/reverse_clock` to create a clock\n"
            "2. Click **▶️ Start Match** to begin\n"
            "3. Use **Allies**/**Axis** buttons to switch control\n"
            "4. Toggle **🤖 Auto** for automatic switching\n"
            "5. Click **⏹️ Stop** when match ends\n"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🏆 How to Win",
        value=(
            "**Win by controlling the center point longer!**\n"
            "• Whoever holds the point accumulates time\n"
            "• Team with most control time wins\n"
            "• Captures matter, not kills or other scores"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚙️ Auto-Switch",
        value=(
            "When enabled, the clock automatically switches teams "
            "when point captures are detected from the game server."
        ),
        inline=False
    )
    
    embed.add_field(
        name="👑 Admin Requirements",
        value="You need the **Admin** role to control the clock.",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Error handling
@bot.event
async def on_error(event, *args, **kwargs):
    logger.error(f"Bot error in {event}: {args}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    error_msg = f"❌ Error: {str(error)}"
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(error_msg, ephemeral=True)
        else:
            await interaction.followup.send(error_msg, ephemeral=True)
    except discord.HTTPException as e:
        logger.error(f"Could not send error message via Discord: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending error message: {e}")

@bot.event
async def on_ready():
    logger.info(f"✅ Bot logged in as {bot.user}")
    logger.info(f"🔗 RCON Host: {os.getenv('RCON_HOST', 'Not configured')}:{os.getenv('RCON_PORT', '7779')}")

    # Test RCON V2 connection on startup (connect + login only)
    try:
        test_client = HLLRconV2Client()
        await test_client.connect()
        await test_client.close()
        logger.info("✅ RCON V2 connection verified on startup")
    except Exception as e:
        logger.warning(f"⚠️ RCON V2 connection test failed: {e}")
    
    # Sync commands
    await bot.wait_until_ready()
    try:
        synced = await bot.tree.sync()
        logger.info(f"✅ Synced {len(synced)} slash commands")
        print(f"🎉 DMT Score Keeper is ready! Use /reverse_clock to start")
    except Exception as e:
        logger.error(f"❌ Command sync failed: {e}")

# Main execution
if __name__ == "__main__":
    print("🚀 Starting DMT Score Keeper (RCON V2)...")

    # Check for Discord token
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "your_discord_bot_token_here":
        print("❌ DISCORD_TOKEN not configured!")
        print("Edit .env and set DISCORD_TOKEN=your_actual_token")
        exit(1)

    # Check for RCON credentials
    rcon_host = os.getenv("RCON_HOST", "")
    rcon_password = os.getenv("RCON_PASSWORD", "")
    if not rcon_host:
        print("❌ RCON_HOST not configured!")
        print("Edit .env and set RCON_HOST=your_server_ip")
        exit(1)
    if not rcon_password:
        print("❌ RCON_PASSWORD not configured!")
        print("Edit .env and set RCON_PASSWORD=your_rcon_password")
        exit(1)

    # Show configuration (no sensitive data)
    print(f"🔗 RCON Host: {rcon_host}:{os.getenv('RCON_PORT', '7779')}")
    print(f"🔑 Password:  {'*' * 8} (configured)")
    print(f"👑 Admin Role: {os.getenv('ADMIN_ROLE_NAME', 'admin')}")
    print(f"🤖 Bot Name:   {os.getenv('BOT_NAME', 'HLLTankBot')}")
    print(f"⏱️ Update Interval: {get_update_interval()}s")
    print(f"🔄 Auto-Switch: {os.getenv('RCON_AUTO_SWITCH', 'false')}")

    log_channel = os.getenv('LOG_CHANNEL_ID', '0')
    print(f"📋 Log Channel: {log_channel if log_channel != '0' else 'Disabled'}")

    print("🎯 Focus: TIME CONTROL - Win by holding the center point longest!")

    try:
        bot.run(token)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"❌ Bot startup failed: {e}")
