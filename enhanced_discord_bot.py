#!/usr/bin/env python3
"""
HLL Discord Bot with API Key CRCON Integration
Time Control Focused - Win by controlling the center point longest!
Updated with corrected CRCON API endpoints
"""

import asyncio
import inspect
import os
import discord
import datetime
import json
import aiohttp
from aiohttp import web
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from dotenv import load_dotenv
from discord.ext import commands, tasks
from datetime import timezone

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

# Constants
DEFAULT_MATCH_DURATION = 4500  # 1h 15m in seconds
GAME_END_THRESHOLD = 30  # Stop match when server time is below this
MESSAGE_TRUNCATE_LENGTH = 1900  # Max length for test messages
MIN_UPDATE_INTERVAL = 5  # Minimum seconds between updates
MAX_UPDATE_INTERVAL = 300  # Maximum seconds between updates
from config import (
    crcon_api_key,
    crcon_timeout,
    crcon_url,
    enable_kill_feed,
    kill_webhook_host,
    kill_webhook_path,
    kill_webhook_port,
    kill_webhook_secret,
    tanks_file,
)

ENABLE_KILL_FEED = enable_kill_feed()
WEBHOOK_PORT = kill_webhook_port()
WEBHOOK_HOST = kill_webhook_host()
WEBHOOK_PATH = kill_webhook_path()
WEBHOOK_SECRET = kill_webhook_secret()
TANK_EVENT_HISTORY_LIMIT = 25
DEFAULT_TANK_WEAPON_KEYWORDS: Dict[str, List[str]] = {
    "cannon_shells": [
        "75mm", "76mm", "88mm", "90mm", "122mm",
        "kwk", "pak", "he shell", "ap shell", "heat shell"
    ],
    "tank_names": [
        "sherman", "panther", "tiger", "t-34", "is-2", "churchill"
    ],
    "launchers": [
        "bazooka", "panzerschreck", "panzerfaust"
    ],
    "anti_tank": [
        "at gun", "anti tank", "flak 36"
    ],
    "explosives": [
        "satchel", "mine"
    ]
}


def _load_tank_weapon_keywords() -> Tuple[Dict[str, List[str]], str]:
    raw_value = os.getenv('TANK_WEAPON_KEYWORDS', '').strip()
    base = DEFAULT_TANK_WEAPON_KEYWORDS.copy()
    source = 'default'
    if raw_value:
        parsed = _parse_keyword_mapping(raw_value)
        if parsed:
            base = parsed
            source = 'file' if Path(raw_value).exists() else 'env'
        else:
            logger.warning("Invalid TANK_WEAPON_KEYWORDS value. Falling back to defaults.")
    derived = _derive_keywords_from_tanks_file(tanks_file())
    merged = _merge_keyword_sets(base, derived)
    final_source = f"{source}+tanks" if derived else source
    return merged, final_source


def _parse_keyword_mapping(value: str) -> Optional[Dict[str, List[str]]]:
    data = None
    potential_path = Path(value)
    if potential_path.exists():
        try:
            data = json.loads(potential_path.read_text(encoding='utf-8'))
        except Exception as exc:
            logger.warning(
                "Failed to read tank keyword file %s: %s",
                potential_path,
                exc,
            )
            return None
    else:
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return None

    if not isinstance(data, dict):
        return None

    normalized: Dict[str, List[str]] = {}
    for key, values in data.items():
        if isinstance(values, (list, tuple)):
            normalized[str(key)] = [str(item).strip() for item in values if str(item).strip()]
    return normalized


def _derive_keywords_from_tanks_file(path: str) -> Dict[str, List[str]]:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding='utf-8'))
    except Exception as exc:
        logger.warning("Failed to parse tanks file %s: %s", file_path, exc)
        return {}
    if not isinstance(data, list):
        return {}

    calibers: set = set()
    vehicle_names: set = set()
    classes: set = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        gun = entry.get("gun") or ""
        vehicle = entry.get("vehicle") or ""
        vehicle_class = entry.get("class") or ""

        # Extract calibers like "75mm" or "17-pounder"
        gun_lower = str(gun).lower()
        for token in gun_lower.replace("/", " ").replace("(", " ").replace(")", " ").split():
            if token.endswith("mm"):
                calibers.add(token)
            if "pounder" in token:
                calibers.add(token)

        if vehicle:
            vehicle_names.add(str(vehicle).lower())
        if vehicle_class:
            classes.add(str(vehicle_class).lower())

    return {
        "calibers": sorted(calibers),
        "vehicles": sorted(vehicle_names),
        "classes": sorted(classes),
    }


def _merge_keyword_sets(base: Dict[str, List[str]], extra: Dict[str, List[str]]) -> Dict[str, List[str]]:
    merged: Dict[str, List[str]] = {k: list(v) for k, v in base.items()}
    for group, values in extra.items():
        if not values:
            continue
        existing = merged.get(group, [])
        seen = {v.lower() for v in existing}
        for val in values:
            if val.lower() not in seen:
                existing.append(val)
                seen.add(val.lower())
        merged[group] = existing
    return merged


TANK_WEAPON_KEYWORDS, TANK_KEYWORD_SOURCE = _load_tank_weapon_keywords()

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

clocks = {}
# Parse LOG_CHANNEL_ID safely
log_channel_str = os.getenv('LOG_CHANNEL_ID', '0')
LOG_CHANNEL_ID = int(log_channel_str) if log_channel_str.isdigit() else 0


class APIKeyCRCONClient:
    """CRCON client using API key authentication"""

    def __init__(self):
        self.base_url = crcon_url()
        self.api_key = crcon_api_key()
        self.session = None
        self.timeout = aiohttp.ClientTimeout(total=crcon_timeout())

    async def __aenter__(self):
        """Async context manager entry"""
        if not self.api_key:
            raise Exception("CRCON_API_KEY not configured")

        # Support both Bearer and x-api-key header styles.
        # Some CRCON builds expect one or the other, so cycle through options.
        base_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        header_variants = [
            (
                "bearer",
                {**base_headers, 'Authorization': f'Bearer {self.api_key}'},
            ),
            ("x-api-key", {**base_headers, 'x-api-key': self.api_key}),
            (
                "raw-authorization",
                {**base_headers, 'Authorization': self.api_key},
            ),
        ]

        last_status = None
        last_error = None

        for variant_name, headers in header_variants:
            # Close any previous session before trying next header style
            if self.session:
                await self.session.close()

            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers=headers,
            )

            try:
                async with self.session.get(
                    f"{self.base_url}/api/get_status"
                ) as response:
                    last_status = response.status
                    if response.status == 200:
                        logger.info(
                            "Successfully connected to CRCON with %s auth header",
                            variant_name,
                        )
                        return self
                    if response.status in (401, 403):
                        logger.warning(
                            "CRCON %s auth rejected with %s, trying fallback",
                            variant_name,
                            response.status,
                        )
                        continue
                    # Other failures are treated as hard errors
                    raise Exception(f"CRCON connection failed: {response.status}")
            except Exception as e:
                last_error = e
                logger.debug(
                    "Error during CRCON connect with %s headers: %s",
                    variant_name,
                    e,
                )
                continue

        # If we exhausted all header variants without success, clean up and raise a meaningful error
        if self.session:
            await self.session.close()

        if last_status in (401, 403):
            raise Exception(
                "CRCON connection failed: unauthorized (tried Bearer, x-api-key, and Authorization headers). "
                "Verify API key and CRCON API access."
            )
        if last_error:
            raise last_error
        raise Exception(
            "CRCON connection failed: unknown error while negotiating authentication"
        )

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()

    async def get_live_game_state(self):
        """Get comprehensive live game state"""
        try:
            # Get data concurrently
            tasks = [
                self._get_endpoint('/api/get_gamestate'),
                self._get_endpoint('/api/get_team_view'),
                self._get_endpoint('/api/get_map'),
                self._get_endpoint('/api/get_players'),
                self._get_endpoint('/api/get_detailed_players')  # Detailed player info with combat scores
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results safely
            game_state = results[0] if not isinstance(results[0], Exception) else {}
            team_view = results[1] if not isinstance(results[1], Exception) else {}
            map_info = results[2] if not isinstance(results[2], Exception) else {}
            players = results[3] if not isinstance(results[3], Exception) else {}
            detailed_players = results[4] if not isinstance(results[4], Exception) else {}

            return {
                'game_state': game_state,
                'team_view': team_view,
                'map_info': map_info,
                'players': players,
                'detailed_players': detailed_players,
                'timestamp': datetime.datetime.now(timezone.utc)
            }

        except Exception as e:
            logger.error(f"Error getting game state: {e}")
            return None

    async def _get_endpoint(self, endpoint):
        """Helper to get data from an endpoint"""
        try:
            async with self.session.get(f"{self.base_url}{endpoint}") as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.warning(f"Endpoint {endpoint} returned {response.status}")
                    return {}
        except Exception as e:
            logger.error(f"Error getting {endpoint}: {e}")
            return {}

    async def send_message(self, message: str):
        """Send message to all players individually"""
        try:
            # First get all connected players - UPDATED ENDPOINT
            logger.info(f"Getting player list to send message: {message}")

            # FIXED: Using correct endpoint with underscore
            async with self.session.get(f"{self.base_url}/api/get_player_ids") as response:
                if response.status != 200:
                    logger.warning(f"Failed to get player list: {response.status}")
                    return False

                player_data = await response.json()
                logger.info(f"Player data response: {player_data}")

                # Extract player list from the result
                if isinstance(player_data, dict) and 'result' in player_data:
                    players = player_data['result']
                else:
                    players = player_data

                if not players:
                    logger.info("No players online to send message to")
                    return True

                success_count = 0
                total_players = len(players)

                # Send message to each player individually
                for player in players:
                    try:
                        # Handle both list format [name, id] and dict format
                        if isinstance(player, list) and len(player) >= 2:
                            player_name = player[0]
                            player_id = player[1]
                        elif isinstance(player, dict):
                            player_name = player.get('name', '')
                            player_id = player.get('steam_id_64', '')
                        else:
                            continue

                        # Send individual message
                        payload = {
                            "player_name": player_name,
                            "player_id": player_id,
                            "message": message,
                            "by": os.getenv('BOT_NAME', 'HLLTankBot')
                        }

                        async with self.session.post(
                            f"{self.base_url}/api/message_player",
                            json=payload,
                        ) as msg_response:
                            if msg_response.status == 200:
                                success_count += 1
                                logger.debug(
                                    "Message sent to %s",
                                    player_name,
                                )
                            else:
                                logger.debug(
                                    "Failed to message %s: %s",
                                    player_name,
                                    msg_response.status,
                                )

                    except Exception as e:
                        logger.debug("Error messaging individual player: %s", e)
                        continue

                logger.info(
                    "Message sent to %s/%s players",
                    success_count,
                    total_players,
                )
                return success_count > 0

        except Exception as e:
            logger.error("Error sending message to all players: %s", e)
            return False


@dataclass
class KillFeedEvent:
    raw: str
    payload: Any
    received_at: datetime.datetime


KillFeedCallback = Callable[[KillFeedEvent], Optional[Awaitable[None]]]


@dataclass
class TankKillDetection:
    killer_name: str
    killer_team: str
    victim_name: str
    victim_team: str
    weapon: str
    vehicle: str
    keyword_group: Optional[str]
    keyword_match: Optional[str]
    raw_payload: Any


def normalize_team_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    lowered = value.lower()
    if any(token in lowered for token in ["alli", "usa", "american", "soviet", "uk", "commonwealth"]):
        return 'allied'
    if any(token in lowered for token in ["axis", "german", "wehr", "panzer", "ost", "ss"]):
        return 'axis'
    return None


def _flatten_kill_payload(payload: Any) -> Optional[Dict[str, Any]]:
    if payload is None:
        return None
    if isinstance(payload, dict):
        if 'data' in payload and isinstance(payload['data'], dict):
            merged = payload.copy()
            merged.update(payload['data'])
            return merged
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return _flatten_kill_payload(parsed)
        except json.JSONDecodeError:
            return {'line_without_timestamp': payload, 'raw': payload}
    return None


def _extract_value(source: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _match_keywords(texts: List[str], keywords: Dict[str, List[str]]) -> Tuple[Optional[str], Optional[str]]:
    for text in texts:
        lowered = text.lower()
        for group, group_keywords in keywords.items():
            for keyword in group_keywords:
                if not keyword:
                    continue
                if keyword.lower() in lowered:
                    return group, keyword
    return None, None


def _looks_like_tank_vehicle(text: str) -> bool:
    lowered = text.lower()
    tokens = [
        "tank",
        "armor",
        "armour",
        "vehicle",
        "panzer",
        "sherman",
    ]
    return any(token in lowered for token in tokens)


def detect_tank_kill(
    payload: Any,
    keyword_mapping: Optional[Dict[str, List[str]]] = None,
) -> Optional[TankKillDetection]:
    keywords = keyword_mapping or TANK_WEAPON_KEYWORDS
    data = _flatten_kill_payload(payload)
    if not isinstance(data, dict):
        return None

    weapon_text = _extract_value(
        data,
        ['weapon', 'weapon_name', 'weapon_id', 'message', 'line_without_timestamp'],
    )
    vehicle_text = _extract_value(
        data,
        ['target_vehicle', 'victim_vehicle', 'vehicle', 'destroyed_vehicle'],
    )
    vehicle_class = _extract_value(
        data,
        ['target_vehicle_class', 'victim_vehicle_class', 'vehicle_class'],
    )
    subtype = _extract_value(
        data,
        ['sub_type', 'category', 'type'],
    )

    keyword_group, keyword_match = _match_keywords(
        [weapon_text, vehicle_text, vehicle_class or "", subtype or ""],
        keywords,
    )
    if not keyword_group and vehicle_class and _looks_like_tank_vehicle(vehicle_class):
        keyword_group, keyword_match = 'vehicle_class', vehicle_class
    if not keyword_group and subtype and _looks_like_tank_vehicle(subtype):
        keyword_group, keyword_match = 'sub_type', subtype
    if not keyword_group and vehicle_text and _looks_like_tank_vehicle(vehicle_text):
        keyword_group, keyword_match = 'vehicle', vehicle_text
    if not keyword_group:
        return None

    killer_name = _extract_value(
        data,
        ['killer_name', 'attacker_name', 'player_name_1', 'source_player', 'player', 'instigator'],
    )
    killer_team = _extract_value(
        data,
        ['killer_team', 'attacker_team', 'player_team_1', 'team'],
    )
    victim_name = _extract_value(
        data,
        ['victim_name', 'target_name', 'player_name_2', 'killed_player'],
    )
    victim_team = _extract_value(
        data,
        ['victim_team', 'target_team', 'player_team_2'],
    )
    vehicle_name = vehicle_text or vehicle_class or subtype

    return TankKillDetection(
        killer_name=killer_name or 'Unknown',
        killer_team=killer_team or 'Unknown',
        victim_name=victim_name or 'Unknown',
        victim_team=victim_team or 'Unknown',
        weapon=weapon_text or 'Unknown',
        vehicle=vehicle_name or 'Unknown',
        keyword_group=keyword_group,
        keyword_match=keyword_match,
        raw_payload=payload
    )


class KillWebhookServer:
    """Receives kill webhooks and feeds them into the kill detection pipeline."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        secret: str = "",
    ):
        self.host = host
        self.port = port
        self.path = path
        self.secret = secret or ""
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._runner: Optional[aiohttp.web.AppRunner] = None
        self._site: Optional[aiohttp.web.TCPSite] = None
        self._task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self.last_event: Optional[KillFeedEvent] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def start(self):
        if self.is_running:
            return
        self._task = asyncio.create_task(self._run(), name="crcon-kill-webhook")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def get_queue(self) -> asyncio.Queue:
        return self._queue

    async def _run(self):
        app = web.Application()
        app.router.add_post(self.path, self._handle_webhook)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self._connected.set()
        logger.info(
            "Kill webhook listening on http://%s:%s%s",
            self.host,
            self.port,
            self.path,
        )
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            self._connected.clear()
            if self._runner:
                await self._runner.cleanup()
            logger.info("Kill webhook listener stopped")

    async def _handle_webhook(self, request: web.Request):
        if self.secret:
            provided = request.headers.get("X-Webhook-Secret", "")
            if provided != self.secret:
                return web.json_response({"status": "forbidden"}, status=403)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"status": "bad json"}, status=400)

        event = KillFeedEvent(
            raw=payload,
            payload=payload,
            received_at=datetime.datetime.now(timezone.utc),
        )
        self.last_event = event
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                _ = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(event)

        return web.json_response({"status": "ok"})


kill_webhook_server: Optional[KillWebhookServer] = None
kill_feed_consumer_task: Optional[asyncio.Task] = None
active_kill_feed_channel_id: Optional[int] = None
last_kill_feed_event: Optional[KillFeedEvent] = None
last_tank_kill_detection: Optional[TankKillDetection] = None
last_tank_kill_event: Optional[KillFeedEvent] = None


def ensure_kill_feed_listener():
    """Start the kill feed listener if the feature is enabled."""
    global kill_webhook_server, kill_feed_consumer_task
    if not ENABLE_KILL_FEED:
        logger.debug("Kill feed listener skipped (ENABLE_KILL_FEED=false)")
        return

    if not kill_webhook_server:
        logger.info("Initializing kill webhook listener on %s:%s%s", WEBHOOK_HOST, WEBHOOK_PORT, WEBHOOK_PATH)
        kill_webhook_server = KillWebhookServer(
            host=WEBHOOK_HOST,
            port=WEBHOOK_PORT,
            path=WEBHOOK_PATH,
            secret=WEBHOOK_SECRET,
        )

    if not kill_webhook_server.is_running:
        logger.info("Starting kill webhook listener task")
        kill_webhook_server.start()

    if not kill_feed_consumer_task or kill_feed_consumer_task.done():
        logger.info("Starting tank kill detection loop")
        kill_feed_consumer_task = asyncio.create_task(_kill_feed_detection_loop(), name="crcon-kill-detector")


async def _kill_feed_detection_loop():
    if not kill_webhook_server:
        return

    queue = kill_webhook_server.get_queue()
    while True:
        try:
            event: KillFeedEvent = await queue.get()
            global last_kill_feed_event
            last_kill_feed_event = event
            detection = detect_tank_kill(event.payload)
            if not detection:
                continue
            team_key = resolve_detection_team(detection)
            if not team_key:
                continue
            global last_tank_kill_detection, last_tank_kill_event
            last_tank_kill_detection = detection
            last_tank_kill_event = event
            await _broadcast_tank_kill(team_key, detection, event)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception(f"Tank kill detection loop error: {exc}")


async def _broadcast_tank_kill(team_key: str, detection: TankKillDetection, event: KillFeedEvent):
    targets = _get_target_clocks()
    if not targets:
        return

    for clock in targets:
        try:
            await clock.record_tank_kill(team_key, detection, event)
        except Exception as exc:
            logger.exception(f"Failed to record tank kill for clock {clock.channel_id}: {exc}")


def resolve_detection_team(detection: TankKillDetection) -> Optional[str]:
    killer_team = normalize_team_name(detection.killer_team)
    if killer_team in ('allied', 'axis'):
        return killer_team

    victim_team = normalize_team_name(detection.victim_team)
    if victim_team == 'allied':
        return 'axis'
    if victim_team == 'axis':
        return 'allied'
    return None


def set_active_kill_feed_channel(channel_id: Optional[int]):
    global active_kill_feed_channel_id
    if channel_id is not None:
        previous = active_kill_feed_channel_id
        active_kill_feed_channel_id = channel_id
        logger.info("Kill feed routed to channel %s (was %s)", channel_id, previous)


def release_kill_feed_channel(channel_id: Optional[int]):
    global active_kill_feed_channel_id
    if channel_id is None:
        return
    if active_kill_feed_channel_id == channel_id:
        next_channel = _find_next_active_channel(exclude=channel_id)
        active_kill_feed_channel_id = next_channel
        logger.info("Kill feed channel released from %s -> %s", channel_id, next_channel)


def _find_next_active_channel(exclude: Optional[int] = None) -> Optional[int]:
    for cid, clock in clocks.items():
        if cid == exclude:
            continue
        if clock.started:
            return cid
    return None


def _get_target_clocks() -> List['ClockState']:
    clock: Optional['ClockState'] = None
    if active_kill_feed_channel_id is not None:
        clock = clocks.get(active_kill_feed_channel_id)
        if clock and not clock.started:
            clock = None
    if clock:
        return [clock]
    return [c for c in clocks.values() if c.started]


def _format_relative_time(moment: datetime.datetime) -> str:
    delta = datetime.datetime.now(timezone.utc) - moment
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 1:
        return "just now"
    units = (
        ('d', 86400),
        ('h', 3600),
        ('m', 60),
        ('s', 1),
    )
    parts: List[str] = []
    for suffix, size in units:
        if seconds >= size and len(parts) < 2:
            value, seconds = divmod(seconds, size)
            parts.append(f"{value}{suffix}")
    return " ".join(parts)


def _shorten_text(text: str, limit: int = 200) -> str:
    stripped = " ".join(text.split())
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit - 1]}…"


class ClockState:
    """Enhanced clock state with live updating team times"""

    def __init__(self, channel_id: Optional[int] = None):
        self.channel_id = channel_id
        self.time_a = 0
        self.time_b = 0
        self.active = None
        self.last_switch = None
        self.match_start_time = None
        self.countdown_end = None  # Add this back
        self.message = None
        self.started = False
        self.clock_started = False

        # CRCON integration
        self.crcon_client = None
        self.game_data = None
        self.auto_switch = False
        self.last_scores = {'allied': 0, 'axis': 0}
        self.switches = []
        self.last_update = None
        self._first_update_done = False  # Track if first update completed
        self._lock = asyncio.Lock()  # Thread safety for time updates

        # DMT Scoring (always enabled)
        self.tournament_mode = True  # Always use DMT scoring
        self.team_names = {'allied': 'Allies', 'axis': 'Axis'}
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
        self.reset_tank_kills()

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

    async def connect_crcon(self):
        """Connect to CRCON with API key"""
        try:
            # Close any existing connection first
            if self.crcon_client:
                try:
                    await self.crcon_client.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning(f"Error closing existing CRCON connection: {e}")

            self.crcon_client = APIKeyCRCONClient()
            await self.crcon_client.__aenter__()
            logger.info("Connected to CRCON successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to CRCON: {e}")
            self.crcon_client = None
            return False

    async def update_from_game(self):
        """Update from CRCON game data"""
        if not self.crcon_client:
            return

        try:
            live_data = await self.crcon_client.get_live_game_state()
            if not live_data:
                return

            self.game_data = live_data
            self.last_update = datetime.datetime.now(timezone.utc)

            # Update player scores if in tournament mode
            if self.tournament_mode:
                self.update_player_scores()

            # Only check for auto-switch if we have previous scores to compare
            # This prevents false triggers on first connection
            if self.auto_switch and self.started and self._first_update_done:
                await self._check_score_changes()
            else:
                # First update - just store the scores without triggering auto-switch
                game_state = self.game_data.get('game_state', {})
                if isinstance(game_state, dict) and 'result' in game_state:
                    result = game_state['result']
                    if isinstance(result, dict):
                        self.last_scores = {
                            'allied': result.get('allied_score', 0),
                            'axis': result.get('axis_score', 0)
                        }
                self._first_update_done = True

        except Exception as e:
            logger.error(f"Error updating from game: {e}")

    async def _check_score_changes(self):
        """Check for captures to trigger auto-switch - focus on point control"""
        if not self.game_data or 'game_state' not in self.game_data:
            return

        game_state = self.game_data['game_state']

        # Parse your CRCON's result format for scores
        current_allied = 0
        current_axis = 0

        if isinstance(game_state, dict) and 'result' in game_state:
            result = game_state['result']
            if isinstance(result, dict):
                current_allied = result.get('allied_score', 0)
                current_axis = result.get('axis_score', 0)

        # Debug logging to see what's happening
        logger.info(
            "Score check - Allied: %s -> %s, Axis: %s -> %s",
            self.last_scores['allied'],
            current_allied,
            self.last_scores['axis'],
            current_axis,
        )

        # Check for score increases (point captures)
        if current_allied > self.last_scores['allied']:
            logger.info("Allied score increased! Switching to Allies")
            await self._auto_switch_to('A', "Allies captured the center point")
        elif current_axis > self.last_scores['axis']:
            logger.info("Axis score increased! Switching to Axis")
            await self._auto_switch_to('B', "Axis captured the center point")
        else:
            logger.debug("No score changes detected")

        # Update last known scores
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

        # Send notification to game with DMT scores
        if self.crcon_client:
            team_name = self.team_names.get(
                'allied' if team == 'A' else 'axis',
                'Allies' if team == 'A' else 'Axis',
            )
            allied_scores = self.calculate_dmt_score('allied')
            axis_scores = self.calculate_dmt_score('axis')
            team_a_name = self.team_names['allied']
            team_b_name = self.team_names['axis']

            allied_combat = allied_scores['combat_total']
            allied_cap = allied_scores['cap_score']
            allied_total = allied_scores['total_dmt']
            axis_combat = axis_scores['combat_total']
            axis_cap = axis_scores['cap_score']
            axis_total = axis_scores['total_dmt']

            allies_summary = (
                f"{team_a_name}: Combat {allied_combat:,.0f} + "
                f"Cap {allied_cap:,.0f} = {allied_total:,.0f} DMT"
            )
            axis_summary = (
                f"{team_b_name}: Combat {axis_combat:,.0f} + "
                f"Cap {axis_cap:,.0f} = {axis_total:,.0f} DMT"
            )

            tank_line = self.format_tank_scoreline()
            msg_header = f"🔄 {team_name} captured the point!"
            msg = " | ".join([msg_header, allies_summary, axis_summary])
            msg = f"{msg}\n{tank_line}"
            await self.crcon_client.send_message(msg)

        # IMPORTANT: Update the Discord embed immediately
        if self.message:
            success = await safe_edit_message(self.message, embed=build_embed(self))
            if success:
                logger.info("Discord embed updated after auto-switch to %s", team)
            else:
                self.message = None

            logger.info("Auto-switched to team %s: %s", team, reason)

    def get_game_info(self):
        """Get formatted game information"""
        if not self.game_data:
            return {
                'map': 'No Connection',
                'players': 0,
                'game_time': 0,
                'connection_status': 'Disconnected'
            }

        game_state = self.game_data.get('game_state', {})
        map_info = self.game_data.get('map_info', {})

        # Extract map name - handle your CRCON's result wrapper
        current_map = 'Unknown'

        if isinstance(map_info, dict) and 'result' in map_info:
            result = map_info['result']
            if isinstance(result, dict):
                # Try the pretty_name first (should be "Elsenborn Ridge Warfare")
                if 'pretty_name' in result:
                    current_map = result['pretty_name']
                # Fallback to nested map object
                elif 'map' in result and isinstance(result['map'], dict):
                    current_map = result['map'].get('pretty_name', result['map'].get('name', 'Unknown'))

        # Extract player count from your CRCON result format
        player_count = 0
        if isinstance(game_state, dict) and 'result' in game_state:
            result = game_state['result']
            if isinstance(result, dict):
                # Your format shows num_allied_players and num_axis_players
                allied_players = result.get('num_allied_players', 0)
                axis_players = result.get('num_axis_players', 0)
                player_count = allied_players + axis_players

        # Fallback: If game_state returns 0 players, count from detailed_players
        if player_count == 0:
            detailed_players = self.game_data.get('detailed_players', {})
            if isinstance(detailed_players, dict) and 'result' in detailed_players:
                result = detailed_players['result']
                if isinstance(result, dict) and 'players' in result:
                    players_data = result['players']
                    if isinstance(players_data, dict):
                        # Count players in the dictionary
                        player_count = len(players_data)

        # Extract game time from your CRCON result format - convert to remaining time display
        game_time_remaining = 0
        if isinstance(game_state, dict) and 'result' in game_state:
            result = game_state['result']
            if isinstance(result, dict):
                # Get the raw time remaining from server
                raw_time = result.get('time_remaining', 0)
                if raw_time > 0:
                    game_time_remaining = raw_time

        # Track scores internally for auto-switch using your CRCON format
        allied_score = 0
        axis_score = 0

        if isinstance(game_state, dict) and 'result' in game_state:
            result = game_state['result']
            if isinstance(result, dict):
                allied_score = result.get('allied_score', 0)
                axis_score = result.get('axis_score', 0)

        # Store scores for auto-switch logic
        self.last_scores = {'allied': allied_score, 'axis': axis_score}

        return {
            'map': current_map,
            'players': player_count,
            'game_time': game_time_remaining,  # This is now the server's remaining time
            'connection_status': 'Connected',
            'last_update': self.last_update.strftime('%H:%M:%S') if self.last_update else 'Never'
        }

    def format_time(self, secs):
        return str(datetime.timedelta(seconds=max(0, int(secs))))

    def update_player_scores(self):
        """Extract player scores from detailed_players data and organize by squad"""
        if not self.game_data or 'detailed_players' not in self.game_data:
            return

        detailed_players = self.game_data['detailed_players']

        # Reset player scores
        self.player_scores = {'allied': {}, 'axis': {}}

        # Parse detailed_players structure
        if isinstance(detailed_players, dict) and 'result' in detailed_players:
            result = detailed_players['result']

            # Process players - handle format where players are keyed by player_id
            if isinstance(result, dict) and 'players' in result:
                players_data = result['players']

                # Check if players are keyed by player_id (dict format)
                if isinstance(players_data, dict):
                    # Check if it looks like player_id keys (not 'allied'/'axis' keys)
                    first_key = next(iter(players_data.keys()), None) if players_data else None
                    if first_key and first_key not in ['allied', 'axis', 'allies']:
                        # Players keyed by player_id - iterate and organize by team
                        for player_id, player_data in players_data.items():
                            if isinstance(player_data, dict):
                                # Get team - normalize "allies" to "allied"
                                team = player_data.get('team', '')
                                if team == 'allies':
                                    team_key = 'allied'
                                elif team == 'axis':
                                    team_key = 'axis'
                                else:
                                    continue  # Skip if no valid team

                                # Get squad/unit name
                                squad_name = player_data.get('unit_name', player_data.get('unit', 'Unknown'))

                                # Add player score
                                self._add_player_score(player_data, squad_name, team_key)
                    else:
                        # Players organized by team
                        self._process_team_scores(players_data.get('allied', []), 'allied')
                        self._process_team_scores(players_data.get('axis', []), 'axis')
                # Or direct allied/axis keys
                elif 'allied' in result or 'axis' in result:
                    self._process_team_scores(result.get('allied', []), 'allied')
                    self._process_team_scores(result.get('axis', []), 'axis')
        # Handle direct list format
        elif isinstance(detailed_players, dict) and ('allied' in detailed_players or 'axis' in detailed_players):
            self._process_team_scores(detailed_players.get('allied', []), 'allied')
            self._process_team_scores(detailed_players.get('axis', []), 'axis')

    def _process_team_scores(self, team_data, team_key):
        """Process individual team's player scores"""
        # Handle list of players directly
        if isinstance(team_data, list):
            for player in team_data:
                if isinstance(player, dict):
                    # Get squad/unit name from player data
                    squad_name = player.get('unit_name', player.get('unit', player.get('squad', 'Unknown')))
                    self._add_player_score(player, squad_name, team_key)
            return

        # Handle dict format
        if not isinstance(team_data, dict):
            return

        # Team data might have 'players' or 'squads' key
        players = team_data.get('players', [])
        squads = team_data.get('squads', {})

        # If we have squad data organized by squad
        if squads and isinstance(squads, dict):
            for squad_name, squad_info in squads.items():
                if isinstance(squad_info, dict) and 'players' in squad_info:
                    for player in squad_info['players']:
                        self._add_player_score(player, squad_name, team_key)
                elif isinstance(squad_info, list):
                    # Squad info is a list of players
                    for player in squad_info:
                        self._add_player_score(player, squad_name, team_key)

        # If we have flat player list with squad info
        elif players and isinstance(players, list):
            for player in players:
                squad_name = player.get('unit_name', player.get('unit', player.get('squad', 'Unknown')))
                self._add_player_score(player, squad_name, team_key)

    def _add_player_score(self, player_data, squad_name, team_key):
        """Add individual player score to tracking"""
        if not isinstance(player_data, dict):
            return

        player_name = player_data.get('player', player_data.get('name', 'Unknown'))
        combat_score = player_data.get('combat', player_data.get('combat_score', 0))

        # Normalize squad name to lowercase for case-insensitive matching
        squad_name_lower = squad_name.lower() if squad_name else 'unknown'

        # Store player score by squad
        if squad_name_lower not in self.player_scores[team_key]:
            self.player_scores[team_key][squad_name_lower] = []

        self.player_scores[team_key][squad_name_lower].append({
            'name': player_name,
            'combat_score': combat_score
        })

    def calculate_dmt_score(self, team_key):
        """Calculate DMT Total Score for a team"""
        if not self.tournament_mode:
            return 0

        # Get squad configuration for this team
        squad_config = self.squad_config.get(team_key, {})
        player_scores = self.player_scores.get(team_key, {})

        # Calculate combat score: 3 × (Crew1 High + Crew2 High + Crew3 High + Crew4 High)
        crew_scores = []
        for crew_num in range(1, 5):
            crew_key = f'crew{crew_num}'
            squad_name = squad_config.get(crew_key, '')

            # Get all players in this squad (case-insensitive lookup)
            squad_name_lower = squad_name.lower() if squad_name else ''
            squad_players = player_scores.get(squad_name_lower, [])

            if squad_players:
                # Find highest combat score in this crew
                highest_score = max(p['combat_score'] for p in squad_players)
                crew_scores.append(highest_score)
            else:
                crew_scores.append(0)

        # Get commander score (case-insensitive lookup)
        commander_squad = squad_config.get('commander', '')
        commander_squad_lower = commander_squad.lower() if commander_squad else ''
        commander_players = player_scores.get(commander_squad_lower, [])
        commander_score = max((p['combat_score'] for p in commander_players), default=0)

        # Calculate combat total
        combat_total = 3 * sum(crew_scores) + commander_score

        # Calculate cap score (time in seconds × 0.5)
        cap_seconds = self.total_time('A' if team_key == 'allied' else 'B')
        cap_score = cap_seconds * 0.5

        # Total DMT score
        total_dmt = combat_total + cap_score

        return {
            'crew_scores': crew_scores,
            'commander_score': commander_score,
            'combat_total': combat_total,
            'cap_seconds': cap_seconds,
            'cap_score': cap_score,
            'total_dmt': total_dmt
        }

    def reset_tank_kills(self):
        self.tank_kill_counts = {'allied': 0, 'axis': 0}
        self.tank_kill_events: List[Dict[str, Any]] = []

    def get_tank_kill_count(self, team_key: str) -> int:
        return self.tank_kill_counts.get(team_key, 0)

    def describe_last_tank_kill(self) -> Optional[str]:
        if not self.tank_kill_events:
            return None
        latest = self.tank_kill_events[-1]
        killer = latest.get('killer', 'Unknown')
        victim = latest.get('victim', 'Unknown')
        detail = latest.get('keyword_match') or latest.get('vehicle') or 'Tank'
        summary = f"{killer} → {victim} ({detail})"
        timestamp = latest.get('timestamp')
        if timestamp:
            try:
                parsed = datetime.datetime.fromisoformat(timestamp)
                summary = f"{summary} • {_format_relative_time(parsed)} ago"
            except ValueError:
                summary = f"{summary} • {timestamp}"
        return summary

    def format_tank_scoreline(self) -> str:
        allied_name = self.team_names.get('allied', 'Allies')
        axis_name = self.team_names.get('axis', 'Axis')
        allied_count = self.get_tank_kill_count('allied')
        axis_count = self.get_tank_kill_count('axis')
        return f"💥 Tanks - {allied_name}: {allied_count} | {axis_name}: {axis_count}"

    async def record_tank_kill(self, team_key: str, detection: TankKillDetection, event: KillFeedEvent):
        if not self.started or team_key not in self.tank_kill_counts:
            return

        entry = {
            'timestamp': event.received_at.isoformat(),
            'killer': detection.killer_name,
            'killer_team': detection.killer_team,
            'victim': detection.victim_name,
            'victim_team': detection.victim_team,
            'weapon': detection.weapon,
            'vehicle': detection.vehicle,
            'keyword_group': detection.keyword_group,
            'keyword_match': detection.keyword_match,
            'credited_team': team_key
        }

        async with self._lock:
            self.tank_kill_counts[team_key] += 1
            self.tank_kill_events.append(entry)
            if len(self.tank_kill_events) > TANK_EVENT_HISTORY_LIMIT:
                self.tank_kill_events = self.tank_kill_events[-TANK_EVENT_HISTORY_LIMIT:]


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


def build_tank_kill_field(clock: 'ClockState', include_last: bool = False) -> str:
    allied_name = clock.team_names.get('allied', 'Allies')
    axis_name = clock.team_names.get('axis', 'Axis')
    lines = [
        f"🇺🇸 {allied_name}: `{clock.get_tank_kill_count('allied')}`",
        f"🇩🇪 {axis_name}: `{clock.get_tank_kill_count('axis')}`",
    ]
    if include_last:
        last_summary = clock.describe_last_tank_kill()
        if last_summary:
            lines.append(f"Last: {last_summary}")
        else:
            lines.append("Last: _No tank kills yet_")
    return "\n".join(lines)


def build_embed(clock: ClockState):
    """Build Discord embed with DMT Scoring"""
    embed = discord.Embed(
        title="🏆 HLL Tank Overwatch - DMT Scoring 🏆",
        description="**Win by highest DMT Total Score!**",
        color=0xFFD700  # Gold color
    )

    # Add game information
    game_info = clock.get_game_info()

    # Start with map and players
    embed.description += (
        f"\n🗺️ **Map:** {game_info['map']}"
        f"\n👥 **Players:** {game_info['players']}/100"
    )

    # Add server game time instead of match duration
    if game_info['game_time'] > 0:
        embed.description += (
            f"\n⏰ **Server Game Time:** "
            f"`{clock.format_time(game_info['game_time'])}`"
        )

    # Get live status for both teams
    allies_status = clock.get_live_status('A')
    axis_status = clock.get_live_status('B')

    # Build team information focused on TIME CONTROL
    allies_value = (
        f"**Control Time:** `{clock.format_time(allies_status['total_time'])}`\n"
        f"**Status:** {allies_status['status']}"
    )
    axis_value = (
        f"**Control Time:** `{clock.format_time(axis_status['total_time'])}`\n"
        f"**Status:** {axis_status['status']}"
    )

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
    dmt_allied = f"**DMT Score: {allied_scores['total_dmt']:,.1f}**\n"
    dmt_allied += f"Combat: {allied_scores['combat_total']:,.0f} | Cap: {allied_scores['cap_score']:,.1f}"

    dmt_axis = f"**DMT Score: {axis_scores['total_dmt']:,.1f}**\n"
    dmt_axis += f"Combat: {axis_scores['combat_total']:,.0f} | Cap: {axis_scores['cap_score']:,.1f}"

    embed.add_field(name=f"🏆 {allied_name} DMT", value=dmt_allied, inline=True)
    embed.add_field(name=f"🏆 {axis_name} DMT", value=dmt_axis, inline=True)
    embed.add_field(name="💥 Tank Kills", value=build_tank_kill_field(clock, include_last=True), inline=False)

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

    # Footer with connection status
    connection_status = "🟢 CRCON Connected" if clock.crcon_client else "🔴 CRCON Disconnected"
    auto_status = " | 🤖 Auto ON" if clock.auto_switch else " | 🤖 Auto OFF"

    footer_text = f"Match Clock by {os.getenv('BOT_AUTHOR', 'StoneyRebel')} | {connection_status}{auto_status}"
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
            return await interaction.response.send_message(
                "❌ Admin role required.",
                ephemeral=True,
            )

        # Respond to Discord immediately to prevent timeout
        await interaction.response.defer()

        clock = clocks[self.channel_id]
        clock.match_start_time = datetime.datetime.now(timezone.utc)
        clock.started = True
        clock.reset_tank_kills()
        set_active_kill_feed_channel(self.channel_id)

        # Start the updater first
        if not match_updater.is_running():
            match_updater.start(self.channel_id)

        view = TimerControls(self.channel_id)

        # Update the embed first
        await safe_edit_message(clock.message, embed=build_embed(clock), view=view)
        await interaction.followup.send("✅ Match started! Connecting to CRCON...", ephemeral=True)

        # Connect to CRCON after responding to Discord
        crcon_connected = await clock.connect_crcon()

        if crcon_connected:
            clock.auto_switch = os.getenv('CRCON_AUTO_SWITCH', 'false').lower() == 'true'

            # Send start message with DMT scoring info
            team_a = clock.team_names['allied']
            team_b = clock.team_names['axis']
            start_msg = (
                f"🏆 HLL Tank Overwatch: {team_a} vs {team_b} | "
                "DMT Scoring Active | Combat + Cap Time = Total Score"
            )

            await clock.crcon_client.send_message(start_msg)
            await interaction.edit_original_response(content="✅ Match started with CRCON!")
        else:
            await interaction.edit_original_response(content="✅ Match started (CRCON connection failed)")

    @discord.ui.button(label="🔗 Test CRCON", style=discord.ButtonStyle.secondary)
    async def test_crcon(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            test_client = APIKeyCRCONClient()
            async with test_client as client:
                live_data = await client.get_live_game_state()

                if live_data:
                    game_state = live_data.get('game_state', {})
                    map_info = live_data.get('map_info', {})
                    embed = discord.Embed(title="🟢 CRCON Test - SUCCESS", color=0x00ff00)
                    embed.add_field(name="Status", value="✅ Connected", inline=True)

                    # Extract map name
                    map_name = 'Unknown'
                    if isinstance(map_info, dict):
                        if 'pretty_name' in map_info:
                            map_name = map_info['pretty_name']
                        elif 'name' in map_info:
                            map_name = map_info['name']
                        elif 'map' in map_info and isinstance(map_info['map'], dict):
                            map_name = map_info['map'].get('pretty_name', 'Unknown')

                    embed.add_field(name="Map", value=map_name, inline=True)
                    embed.add_field(name="Players", value=f"{game_state.get('nb_players', 0)}/100", inline=True)
                else:
                    embed = discord.Embed(title="🟡 CRCON Test - PARTIAL", color=0xffaa00)
                    embed.add_field(name="Status", value="Connected but no data", inline=False)

        except Exception as e:
            embed = discord.Embed(title="🔴 CRCON Test - FAILED", color=0xff0000)
            embed.add_field(name="Error", value=str(e)[:1000], inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


class TimerControls(discord.ui.View):
    def __init__(self, channel_id):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="Allies", style=discord.ButtonStyle.success, emoji="🇺🇸")
    async def switch_to_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch_team(interaction, "A")

    @discord.ui.button(label="Axis", style=discord.ButtonStyle.secondary, emoji="🇩🇪")
    async def switch_to_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch_team(interaction, "B")

    @discord.ui.button(label="🤖 Auto", style=discord.ButtonStyle.secondary)
    async def toggle_auto_switch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not user_is_admin(interaction):
            return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

        clock = clocks[self.channel_id]
        clock.auto_switch = not clock.auto_switch

        status = "enabled" if clock.auto_switch else "disabled"

        await interaction.response.defer()
        await safe_edit_message(clock.message, embed=build_embed(clock), view=self)

        if clock.crcon_client:
            await clock.crcon_client.send_message(f"🤖 Auto-switch {status}")

    @discord.ui.button(label="📊 Stats", style=discord.ButtonStyle.secondary)
    async def show_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        clock = clocks[self.channel_id]
        await interaction.response.defer(ephemeral=True)

        if not clock.crcon_client:
            return await interaction.followup.send("❌ CRCON not connected.", ephemeral=True)

        try:
            await clock.update_from_game()
            game_info = clock.get_game_info()

            embed = discord.Embed(title="📊 Live Match Stats", color=0x00ff00)
            embed.add_field(name="🗺️ Map", value=game_info['map'], inline=True)
            embed.add_field(name="👥 Players", value=f"{game_info['players']}/100", inline=True)
            embed.add_field(name="🔄 Point Switches", value=str(len(clock.switches)), inline=True)

            # Control time breakdown
            allies_time = clock.total_time('A')
            axis_time = clock.total_time('B')
            total_control = allies_time + axis_time

            if total_control > 0:
                allies_percent = (allies_time / total_control) * 100
                axis_percent = (axis_time / total_control) * 100

                embed.add_field(name="🇺🇸 Allies Control", value=f"{allies_percent:.1f}%", inline=True)
                embed.add_field(name="🇩🇪 Axis Control", value=f"{axis_percent:.1f}%", inline=True)

            embed.add_field(name="🤖 Auto-Switch", value="On" if clock.auto_switch else "Off", inline=True)
            embed.add_field(name="📡 Last Update", value=game_info['last_update'], inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="↺ Reset", style=discord.ButtonStyle.primary)
    async def reset_timer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not user_is_admin(interaction):
            return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

        old_clock = clocks[self.channel_id]
        release_kill_feed_channel(self.channel_id)
        if old_clock.crcon_client:
            await old_clock.crcon_client.__aexit__(None, None, None)

        clocks[self.channel_id] = ClockState(self.channel_id)
        clock = clocks[self.channel_id]
        view = StartControls(self.channel_id)

        await interaction.response.defer()
        embed = build_embed(clock)
        await interaction.followup.send(embed=embed, view=view)
        clock.message = await interaction.original_response()

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
    async def stop_timer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not user_is_admin(interaction):
            return await interaction.response.send_message(
                "❌ Admin role required.",
                ephemeral=True,
            )

        clock = clocks[self.channel_id]

        # IMPORTANT: Finalize the current session before stopping
        async with clock._lock:
            if clock.active and clock.last_switch:
                elapsed = (datetime.datetime.now(timezone.utc) - clock.last_switch).total_seconds()
                if clock.active == "A":
                    clock.time_a += elapsed
                elif clock.active == "B":
                    clock.time_b += elapsed

            clock.active = None
            clock.started = False

        # Send final message to game with DMT scores
        if clock.crcon_client:
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

            tank_line = clock.format_tank_scoreline()
            allied_combat = allied_scores['combat_total']
            allied_cap = allied_scores['cap_score']
            allied_total = allied_scores['total_dmt']
            axis_combat = axis_scores['combat_total']
            axis_cap = axis_scores['cap_score']
            axis_total = axis_scores['total_dmt']

            allies_summary = (
                f"{team_a_name}: Combat {allied_combat:,.0f} + "
                f"Cap {allied_cap:,.0f} = {allied_total:,.0f} DMT"
            )
            axis_summary = (
                f"{team_b_name}: Combat {axis_combat:,.0f} + "
                f"Cap {axis_cap:,.0f} = {axis_total:,.0f} DMT"
            )

            msg_header = f"🏁 MATCH COMPLETE! {winner_msg}"
            msg = " | ".join([msg_header, allies_summary, axis_summary])
            msg = f"{msg}\n{tank_line}"
            await clock.crcon_client.send_message(msg)

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
        allied_value = (
            f"**{allied_scores['total_dmt']:,.1f} DMT**\n"
            f"Combat: {allied_scores['combat_total']:,.0f}\n"
            f"Cap: {allied_scores['cap_score']:,.1f} "
            f"({clock.format_time(clock.time_a)})"
        )
        axis_value = (
            f"**{axis_scores['total_dmt']:,.1f} DMT**\n"
            f"Combat: {axis_scores['combat_total']:,.0f}\n"
            f"Cap: {axis_scores['cap_score']:,.1f} "
            f"({clock.format_time(clock.time_b)})"
        )
        embed.add_field(
            name=f"🇺🇸 {team_a_name} - Final DMT",
            value=allied_value,
            inline=True,
        )
        embed.add_field(
            name=f"🇩🇪 {team_b_name} - Final DMT",
            value=axis_value,
            inline=True,
        )
        embed.add_field(
            name="💥 Tank Kills",
            value=build_tank_kill_field(clock, include_last=True),
            inline=False,
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
        release_kill_feed_channel(clock.channel_id)
        clock.reset_tank_kills()

    async def _switch_team(self, interaction: discord.Interaction, team: str):
        if not user_is_admin(interaction):
            return await interaction.response.send_message(
                "❌ Admin role required.",
                ephemeral=True,
            )

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

        # Send notification with DMT scores
        if clock.crcon_client:
            team_name = clock.team_names.get(
                'allied' if team == 'A' else 'axis',
                'Allies' if team == 'A' else 'Axis',
            )
            allied_scores = clock.calculate_dmt_score('allied')
            axis_scores = clock.calculate_dmt_score('axis')
            team_a_name = clock.team_names['allied']
            team_b_name = clock.team_names['axis']

            tank_line = clock.format_tank_scoreline()
            allied_combat = allied_scores['combat_total']
            allied_cap = allied_scores['cap_score']
            allied_total = allied_scores['total_dmt']
            axis_combat = axis_scores['combat_total']
            axis_cap = axis_scores['cap_score']
            axis_total = axis_scores['total_dmt']

            allies_summary = (
                f"{team_a_name}: Combat {allied_combat:,.0f} + "
                f"Cap {allied_cap:,.0f} = {allied_total:,.0f} DMT"
            )
            axis_summary = (
                f"{team_b_name}: Combat {axis_combat:,.0f} + "
                f"Cap {axis_cap:,.0f} = {axis_total:,.0f} DMT"
            )

            msg_header = f"⚔️ {team_name} captured the point!"
            msg = " | ".join([msg_header, allies_summary, axis_summary])
            msg = f"{msg}\n{tank_line}"
            await clock.crcon_client.send_message(msg)

        await interaction.response.defer()
        await safe_edit_message(clock.message, embed=build_embed(clock), view=self)


async def log_results(clock: ClockState, game_info: dict):
    """Log match results focused on time control"""
    if not LOG_CHANNEL_ID:
        return

    results_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not results_channel:
        return

    embed = discord.Embed(title="🏁 HLL Tank Overwatch Match Complete", color=0x800020)
    embed.add_field(name="🇺🇸 Allies Control Time", value=f"`{clock.format_time(clock.time_a)}`", inline=True)
    embed.add_field(name="🇩🇪 Axis Control Time", value=f"`{clock.format_time(clock.time_b)}`", inline=True)
    embed.add_field(name="💥 Tank Kills", value=build_tank_kill_field(clock), inline=True)

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
        logger.warning("Invalid UPDATE_INTERVAL, using default: 15")
        return 15

# Update task - shows in-game time


@tasks.loop(seconds=get_update_interval())
async def match_updater(channel_id):
    """Update match display with live game time"""
    clock = clocks.get(channel_id)
    if not clock or not clock.started or not clock.message:
        return

    try:
        # Update from CRCON if connected
        if clock.crcon_client:
            try:
                await clock.update_from_game()
            except Exception as e:
                logger.warning(f"CRCON update failed, attempting reconnect: {e}")
                # Try to reconnect if the session failed
                await clock.connect_crcon()

        # Check if game has ended (time remaining is 0 or very low)
        game_info = clock.get_game_info()
        if game_info['connection_status'] == 'Connected' and game_info['game_time'] <= GAME_END_THRESHOLD:
            logger.info("Game time ended, automatically stopping match")
            await auto_stop_match(clock, game_info)
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

            clock.active = None
            clock.started = False

        # Send final message to game with DMT scores
        if clock.crcon_client:
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

            tank_line = clock.format_tank_scoreline()
            allied_combat = allied_scores['combat_total']
            allied_cap = allied_scores['cap_score']
            allied_total = allied_scores['total_dmt']
            axis_combat = axis_scores['combat_total']
            axis_cap = axis_scores['cap_score']
            axis_total = axis_scores['total_dmt']

            allies_summary = (
                f"{team_a_name}: Combat {allied_combat:,.0f} + "
                f"Cap {allied_cap:,.0f} = {allied_total:,.0f} DMT"
            )
            axis_summary = (
                f"{team_b_name}: Combat {axis_combat:,.0f} + "
                f"Cap {axis_cap:,.0f} = {axis_total:,.0f} DMT"
            )

            msg_header = f"🏁 MATCH COMPLETE! {winner_msg}"
            msg = " | ".join([msg_header, allies_summary, axis_summary])
            msg = f"{msg}\n{tank_line}"
            await clock.crcon_client.send_message(msg)

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
        allied_value = (
            f"**{allied_scores['total_dmt']:,.1f} DMT**\n"
            f"Combat: {allied_scores['combat_total']:,.0f}\n"
            f"Cap: {allied_scores['cap_score']:,.1f} "
            f"({clock.format_time(clock.time_a)})"
        )
        axis_value = (
            f"**{axis_scores['total_dmt']:,.1f} DMT**\n"
            f"Combat: {axis_scores['combat_total']:,.0f}\n"
            f"Cap: {axis_scores['cap_score']:,.1f} "
            f"({clock.format_time(clock.time_b)})"
        )
        embed.add_field(
            name=f"🇺🇸 {team_a_name} - Final DMT",
            value=allied_value,
            inline=True,
        )
        embed.add_field(
            name=f"🇩🇪 {team_b_name} - Final DMT",
            value=axis_value,
            inline=True,
        )
        embed.add_field(
            name="💥 Tank Kills",
            value=build_tank_kill_field(clock, include_last=True),
            inline=False,
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

        # Update the message with final results
        await safe_edit_message(clock.message, embed=embed, view=None)

        # Also post to the channel (not just edit the existing message)
        channel = clock.message.channel
        await channel.send("🏁 **MATCH COMPLETE!** 🏁", embed=embed)

        # Log results to log channel
        await log_results(clock, game_info)

        release_kill_feed_channel(clock.channel_id)
        clock.reset_tank_kills()

        logger.info("Match automatically stopped due to game time expiring")

    except Exception as e:
        logger.error(f"Error in auto_stop_match: {e}")

# Bot commands


@bot.tree.command(name="reverse_clock", description="Start the HLL Tank Overwatch time control clock")
async def reverse_clock(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    clocks[channel_id] = ClockState(channel_id)

    embed = build_embed(clocks[channel_id])
    view = StartControls(channel_id)

    await interaction.response.send_message("✅ HLL Tank Overwatch clock ready!", ephemeral=True)
    posted_message = await interaction.channel.send(embed=embed, view=view)
    clocks[channel_id].message = posted_message


@bot.tree.command(name="crcon_status", description="Check CRCON connection status")
async def crcon_status(interaction: discord.Interaction):
    await interaction.response.defer()

    embed = discord.Embed(title="🔗 CRCON Status", color=0x0099ff)

    try:
        test_client = APIKeyCRCONClient()
        async with test_client as client:
            live_data = await client.get_live_game_state()

            if live_data:
                game_state = live_data.get('game_state', {})
                map_info = live_data.get('map_info', {})

                embed.add_field(name="Connection", value="✅ Connected", inline=True)
                embed.add_field(name="API Key", value="✅ Valid", inline=True)
                embed.add_field(name="Data", value="✅ Available", inline=True)

                # Extract map name properly
                map_name = 'Unknown'
                if isinstance(map_info, dict) and 'result' in map_info:
                    result = map_info['result']
                    if isinstance(result, dict):
                        if 'pretty_name' in result:
                            map_name = result['pretty_name']
                        elif 'map' in result and isinstance(result['map'], dict):
                            map_name = result['map'].get('pretty_name', result['map'].get('name', 'Unknown'))

                # Extract player count properly
                player_count = 0
                if isinstance(game_state, dict) and 'result' in game_state:
                    result = game_state['result']
                    if isinstance(result, dict):
                        allied_players = result.get('num_allied_players', 0)
                        axis_players = result.get('num_axis_players', 0)
                        player_count = allied_players + axis_players

                embed.add_field(name="Current Map", value=map_name, inline=True)
                embed.add_field(name="Players", value=f"{player_count}/100", inline=True)
                embed.add_field(name="Server Status", value="🟢 Online", inline=True)
            else:
                embed.add_field(name="Connection", value="🟡 Connected", inline=True)
                embed.add_field(name="Data", value="❌ No data", inline=True)

    except Exception as e:
        embed.add_field(name="Connection", value="❌ Failed", inline=True)
        embed.add_field(name="Error", value=str(e)[:500], inline=False)

    # Configuration info (avoid exposing partial API key)
    embed.add_field(name="URL", value=os.getenv('CRCON_URL', 'Not set'), inline=True)
    embed.add_field(name="API Key", value="✅ Configured" if os.getenv('CRCON_API_KEY') else '❌ Not set', inline=True)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="killfeed_status", description="Show CRCON kill feed listener health")
async def killfeed_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(title="💥 Kill Feed Status", color=0x3498db)
    feature_state = "🟢 ENABLE_KILL_FEED=true" if ENABLE_KILL_FEED else "🔴 ENABLE_KILL_FEED=false"
    embed.add_field(name="Feature", value=feature_state, inline=False)

    if not ENABLE_KILL_FEED:
        embed.add_field(
            name="Next Steps",
            value="Set ENABLE_KILL_FEED=true in your environment to start tracking tank kills.",
            inline=False
        )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    listener = kill_webhook_server
    if not listener:
        embed.add_field(
            name="Listener",
            value="⚪ Not started (run /reverse_clock and start a match to attach)",
            inline=False
        )
    else:
        running = "🟢 Running" if listener.is_running else "🔴 Stopped"
        connected = "🟢 Connected" if listener.is_connected else "🟡 Starting"
        queue_size = listener.get_queue().qsize()
        embed.add_field(
            name="Listener",
            value=f"Webhook\n{running}\n{connected}\nQueue: {queue_size}/1000",
            inline=False
        )

    channel_value = "None"
    if active_kill_feed_channel_id is not None:
        channel_obj = bot.get_channel(active_kill_feed_channel_id)
        if hasattr(channel_obj, 'name'):
            prefix = f"#{channel_obj.name}"
            if getattr(channel_obj, 'guild', None):
                prefix = f"{channel_obj.guild.name} {prefix}"
            channel_value = f"{prefix} ({active_kill_feed_channel_id})"
        else:
            channel_value = str(active_kill_feed_channel_id)
    embed.add_field(name="Active Match Channel", value=channel_value, inline=False)

    config_lines = [
        f"Webhook: http://{WEBHOOK_HOST}:{WEBHOOK_PORT}{WEBHOOK_PATH}",
        f"Secret: {'set' if WEBHOOK_SECRET else 'not set'}",
    ]
    embed.add_field(name="Configuration", value="\n".join(config_lines), inline=False)

    if last_kill_feed_event:
        raw_preview = (
            last_kill_feed_event.raw
            if isinstance(last_kill_feed_event.raw, str)
            else ''
        )
        if not raw_preview:
            try:
                raw_preview = (
                    json.dumps(last_kill_feed_event.payload)
                    if last_kill_feed_event.payload is not None
                    else "<no payload>"
                )
            except TypeError:
                raw_preview = str(last_kill_feed_event.payload)
        preview = _shorten_text(raw_preview)
        embed.add_field(
            name="Last Event",
            value=f"{_format_relative_time(last_kill_feed_event.received_at)} ago\n{preview}",
            inline=False
        )
    else:
        embed.add_field(name="Last Event", value="No kill feed events received yet.", inline=False)

    if last_tank_kill_detection:
        detection = last_tank_kill_detection
        detection_time = (
            last_tank_kill_event.received_at
            if last_tank_kill_event
            else datetime.datetime.now(timezone.utc)
        )
        detail = detection.keyword_match or detection.vehicle or detection.weapon
        detection_summary = (
            f"{detection.killer_name} ({detection.killer_team}) → {detection.victim_name} ({detection.victim_team})"
            f" with {detail}"
        )
        embed.add_field(
            name="Last Tank Kill",
            value=f"{_format_relative_time(detection_time)} ago\n{detection_summary}",
            inline=False
        )
    else:
        embed.add_field(name="Last Tank Kill", value="No tank kills detected yet.", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="server_info", description="Get current HLL server information")
async def server_info(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        test_client = APIKeyCRCONClient()
        async with test_client as client:
            live_data = await client.get_live_game_state()

            if not live_data:
                return await interaction.followup.send("❌ Could not retrieve server information")

            embed = discord.Embed(title="🎮 HLL Server Information", color=0x00ff00)

            game_state = live_data.get('game_state', {})
            map_info = live_data.get('map_info', {})

            # Extract map info properly
            map_name = 'Unknown'
            if isinstance(map_info, dict) and 'result' in map_info:
                result = map_info['result']
                if isinstance(result, dict):
                    if 'pretty_name' in result:
                        map_name = result['pretty_name']
                    elif 'map' in result and isinstance(result['map'], dict):
                        map_name = result['map'].get('pretty_name', result['map'].get('name', 'Unknown'))

            # Extract player count properly
            player_count = 0
            if isinstance(game_state, dict) and 'result' in game_state:
                result = game_state['result']
                if isinstance(result, dict):
                    allied_players = result.get('num_allied_players', 0)
                    axis_players = result.get('num_axis_players', 0)
                    player_count = allied_players + axis_players

            embed.add_field(name="🗺️ Map", value=map_name, inline=True)
            embed.add_field(name="👥 Players", value=f"{player_count}/100", inline=True)

            # Extract time remaining properly
            time_remaining = 0
            if isinstance(game_state, dict) and 'result' in game_state:
                result = game_state['result']
                if isinstance(result, dict):
                    time_remaining = result.get('time_remaining', 0)

            if time_remaining > 0:
                embed.add_field(
                    name="⏱️ Game Time",
                    value=f"{time_remaining // 60}:{time_remaining % 60:02d}",
                    inline=True,
                )

            embed.timestamp = datetime.datetime.now(timezone.utc)
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error retrieving server info: {str(e)}")


@bot.tree.command(name="test_map", description="Debug game state and player count")
async def test_map(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        test_client = APIKeyCRCONClient()
        async with test_client as client:
            live_data = await client.get_live_game_state()

            if not live_data:
                return await interaction.followup.send("❌ No data", ephemeral=True)

            game_state = live_data.get('game_state', {})

            # Format game_state data
            import json
            game_state_str = json.dumps(game_state, indent=2)

            # Send overview
            embed = discord.Embed(title="📊 Game State Debug", color=0x00ff00)
            embed.add_field(name="Data Type", value=str(type(game_state)), inline=True)

            if isinstance(game_state, dict):
                embed.add_field(name="Top Keys", value=str(list(game_state.keys())[:10]), inline=False)

                # Try to extract player count different ways
                if 'result' in game_state:
                    result = game_state['result']
                    if isinstance(result, dict):
                        embed.add_field(name="Result Keys", value=str(list(result.keys())[:15]), inline=False)

                        # Show player count fields
                        player_fields = {k: v for k, v in result.items() if 'player' in k.lower() or 'num' in k.lower()}
                        if player_fields:
                            embed.add_field(name="Player-related Fields", value=str(player_fields), inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)

            # Send raw data in chunks
            chunk_size = 1900
            for i in range(0, min(len(game_state_str), 3800), chunk_size):  # Max 2 messages
                chunk = game_state_str[i:i+chunk_size]
                await interaction.followup.send(f"```json\n{chunk}\n```", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)


@bot.tree.command(name="test_player_scores", description="Test if CRCON provides player combat scores")
async def test_player_scores(interaction: discord.Interaction):
    """Test command to see what player stats CRCON provides"""
    await interaction.response.defer(ephemeral=True)

    try:
        test_client = APIKeyCRCONClient()
        async with test_client as client:
            # Get detailed players data
            live_data = await client.get_live_game_state()

            if not live_data or 'detailed_players' not in live_data:
                return await interaction.followup.send("❌ No detailed player data available", ephemeral=True)

            detailed_players = live_data['detailed_players']

            # Format data structure overview
            import json
            data_str = json.dumps(detailed_players, indent=2)

            # Send first chunk as embed with overview
            embed = discord.Embed(title="📊 CRCON Player Data Structure", color=0x00ff00)
            embed.add_field(name="✅ Endpoint", value="/api/get_detailed_players", inline=False)
            embed.add_field(name="Data Type", value=str(type(detailed_players)), inline=True)

            if isinstance(detailed_players, dict):
                embed.add_field(name="Top-level Keys", value=str(list(detailed_players.keys())[:10]), inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)

            # Send raw data in chunks (Discord has 2000 char limit per message)
            chunk_size = 1900
            for i in range(0, min(len(data_str), 5700), chunk_size):  # Max 3 messages
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
        test_client = APIKeyCRCONClient()
        async with test_client as client:
            success = await client.send_message(f"📢 [Discord] {message}")

            if success:
                embed = discord.Embed(
                    title="📢 Message Sent",
                    description=f"Successfully sent to server:\n\n*{message}*",
                    color=0x00ff00
                )
            else:
                embed = discord.Embed(
                    title="⚠️ Message Not Sent",
                    description="Message endpoints not available on this CRCON version",
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
        return await interaction.response.send_message(
            "❌ No active clock in this channel. Use /reverse_clock first.",
            ephemeral=True
        )

    clock = clocks[channel_id]
    clock.team_names['allied'] = team_a
    clock.team_names['axis'] = team_b

    embed = discord.Embed(title="✅ Team Names Updated", color=0x00ff00)
    embed.add_field(name="Allied Team", value=team_a, inline=True)
    embed.add_field(name="Axis Team", value=team_b, inline=True)
    embed.add_field(name="Scoring", value="DMT Total Score (Always Active)", inline=False)
    embed.add_field(
        name="Formula",
        value=(
            "Combat: 3×(Crew1+Crew2+Crew3+Crew4) + Commander\n"
            "Cap: Seconds × 0.5\n"
            "Total DMT: Combat + Cap"
        ),
        inline=False
    )

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
    embed = discord.Embed(title="🎯 HLL Tank Overwatch Clock Help", color=0x0099ff)

    embed.add_field(
        name="📋 Commands",
        value=(
            "`/reverse_clock` - Start a new time control clock\n"
            "`/crcon_status` - Check CRCON connection\n"
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
    logger.info(f"🔗 CRCON URL: {os.getenv('CRCON_URL', 'Not configured')}")

    # Test CRCON connection on startup
    try:
        test_client = APIKeyCRCONClient()
        async with test_client as client:
            live_data = await client.get_live_game_state()
            if live_data:
                logger.info("✅ CRCON connection verified on startup")
            else:
                logger.warning("🟡 CRCON connected but no game data")
    except Exception as e:
        logger.warning(f"⚠️ CRCON connection test failed: {e}")

    if ENABLE_KILL_FEED:
        ensure_kill_feed_listener()

    # Sync commands
    await bot.wait_until_ready()
    try:
        synced = await bot.tree.sync()
        logger.info(f"✅ Synced {len(synced)} slash commands")
        print("🎉 HLL Tank Overwatch Clock ready! Use /reverse_clock to start")
    except Exception as e:
        logger.error(f"❌ Command sync failed: {e}")

# Main execution
if __name__ == "__main__":
    print("🚀 Starting HLL Tank Overwatch Bot...")
    print("📝 Version: Updated with corrected CRCON endpoints")

    # Check for Discord token
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "your_discord_bot_token_here":
        print("❌ DISCORD_TOKEN not configured!")
        print("1. Create a Discord bot at https://discord.com/developers/applications")
        print("2. Copy the bot token")
        print("3. Edit .env file and set DISCORD_TOKEN=your_actual_token")
        exit(1)

    # Check for API key
    api_key = os.getenv("CRCON_API_KEY")
    if not api_key or api_key == "your_crcon_api_key_here":
        print("❌ CRCON_API_KEY not configured!")
        print("Edit .env file and set CRCON_API_KEY=your_crcon_api_key_here")
        exit(1)

    # Validate CRCON URL
    crcon_url_value = os.getenv('CRCON_URL', 'http://localhost:8010')
    if not crcon_url_value.startswith(('http://', 'https://')):
        print("⚠️ WARNING: CRCON_URL should start with http:// or https://")

    # Validate kill feed prerequisites if enabled
    kill_feed_enabled = ENABLE_KILL_FEED
    if kill_feed_enabled and not WEBHOOK_HOST:
        print("❌ ENABLE_KILL_FEED=true but webhook host is missing (KILL_WEBHOOK_HOST)")
        exit(1)

    # Show configuration (without sensitive data)
    print(f"🔗 CRCON: {crcon_url_value}")
    print(f"🔑 API Key: {'*' * 8}... (configured)")
    print(f"👑 Admin Role: {os.getenv('ADMIN_ROLE_NAME', 'admin')}")
    print(f"🤖 Bot Name: {os.getenv('BOT_NAME', 'HLLTankBot')}")
    print(f"⏱️ Update Interval: {get_update_interval()}s")
    print(f"🔄 Auto-Switch: {os.getenv('CRCON_AUTO_SWITCH', 'true')}")
    if kill_feed_enabled:
        print("💥 Kill Feed: Enabled (Webhook mode)")
        print(f"   Webhook: http://{WEBHOOK_HOST}:{WEBHOOK_PORT}{WEBHOOK_PATH}")
        print(f"   Secret set: {'yes' if WEBHOOK_SECRET else 'no'}")
        keyword_total = sum(len(values) for values in TANK_WEAPON_KEYWORDS.values())
        print(f"   Tank Keywords: {keyword_total} entries ({TANK_KEYWORD_SOURCE})")
    else:
        print("💥 Kill Feed: Disabled")

    log_channel = os.getenv('LOG_CHANNEL_ID', '0')
    if log_channel != '0':
        print(f"📋 Log Channel: {log_channel}")
    else:
        print("📋 Log Channel: Disabled")

    print("🎯 Focus: TIME CONTROL - Win by holding the center point longest!")
    print("✅ Endpoint Update: Fixed get_player_ids endpoint")

    try:
        bot.run(token)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"❌ Bot startup failed: {e}")
