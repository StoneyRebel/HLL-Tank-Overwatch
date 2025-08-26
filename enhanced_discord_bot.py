#!/usr/bin/env python3
"""
HLL Discord Bot with API Key CRCON Integration
Time Control Focused - Win by controlling the center point longest!
"""

import asyncio
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

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

clocks = {}
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0')) if os.getenv('LOG_CHANNEL_ID', '0').isdigit() else 0
RESULTS_TARGET = None  # Will store channel/thread ID for results

class APIKeyCRCONClient:
    """CRCON client using API key authentication"""
    
    def __init__(self):
        self.base_url = os.getenv('CRCON_URL', 'http://localhost:8010')
        self.api_key = os.getenv('CRCON_API_KEY')
        self.session = None
        self.timeout = aiohttp.ClientTimeout(total=int(os.getenv('CRCON_TIMEOUT', '15')))
    
    async def __aenter__(self):
        """Async context manager entry"""
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        self.session = aiohttp.ClientSession(
            timeout=self.timeout,
            headers=headers
        )
        
        # Test connection
        try:
            async with self.session.get(f"{self.base_url}/api/get_status") as response:
                if response.status != 200:
                    await self.session.close()
                    raise Exception(f"CRCON connection failed: {response.status}")
        except Exception as e:
            if self.session:
                await self.session.close()
            raise e
        
        logger.info("Successfully connected to CRCON with API key")
        return self
    
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
                self._get_endpoint('/api/get_players')
            ]
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results safely
            game_state = results[0] if not isinstance(results[0], Exception) else {}
            team_view = results[1] if not isinstance(results[1], Exception) else {}
            map_info = results[2] if not isinstance(results[2], Exception) else {}
            players = results[3] if not isinstance(results[3], Exception) else {}
            
            return {
                'game_state': game_state,
                'team_view': team_view,
                'map_info': map_info,
                'players': players,
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
            # First get all connected players
            logger.info(f"Getting player list to send message: {message}")
            
            async with self.session.get(f"{self.base_url}/api/get_playerids") as response:
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
                        
                        async with self.session.post(f"{self.base_url}/api/message_player", json=payload) as msg_response:
                            if msg_response.status == 200:
                                success_count += 1
                                logger.debug(f"Message sent to {player_name}")
                            else:
                                logger.debug(f"Failed to message {player_name}: {msg_response.status}")
                                
                    except Exception as e:
                        logger.debug(f"Error messaging individual player: {e}")
                        continue
                
                logger.info(f"Message sent to {success_count}/{total_players} players")
                return success_count > 0
                
        except Exception as e:
            logger.error(f"Error sending message to all players: {e}")
            return False

class ClockState:
    """Enhanced clock state with live updating team times"""
    
    def __init__(self):
        self.time_a = 0.0  # Explicitly use floats
        self.time_b = 0.0
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
        
        # Add these fields for mid and fourth point tracking
        self.mid_point_time_a = 0.0  # Allies hold time (seconds, index 2)
        self.mid_point_time_b = 0.0  # Axis hold time (seconds, index 2)
        self.fourth_point_time_a = 0.0  # Allies hold time (seconds, index 3)
        self.fourth_point_time_b = 0.0  # Axis hold time (seconds, index 3)
        self.mid_point_owner = None
        self.fourth_point_owner = None
        self.mid_point_last_switch = None
        self.fourth_point_last_switch = None
        
        logger.info("ClockState initialized with time_a=0.0, time_b=0.0")

    def get_time_remaining(self):
        """Get time remaining in match"""
        if self.countdown_end:
            now = datetime.datetime.now(timezone.utc)
            remaining = (self.countdown_end - now).total_seconds()
            return max(0, int(remaining))
        return 4500  # Default 1h 15m

    def get_current_elapsed(self):
        """Get elapsed time since last switch"""
        if self.last_switch and self.clock_started and self.active:
            elapsed = (datetime.datetime.now(timezone.utc) - self.last_switch).total_seconds()
            # Only cap if it's truly abnormal (more than 4 hours in one session)
            if elapsed > 14400:  # 4 hours
                logger.error(f"Abnormal elapsed time detected: {elapsed} seconds. Resetting to 0.")
                return 0
            return max(0, elapsed)
        return 0

    def total_time(self, team):
        """Get total time for a team INCLUDING current elapsed time"""
        if team == "A":
            base_time = self.time_a
            # Add current elapsed time if Allies are currently active
            if self.active == "A" and self.clock_started:
                current_elapsed = self.get_current_elapsed()
                base_time += current_elapsed
            return max(0, base_time)
        elif team == "B":
            base_time = self.time_b
            # Add current elapsed time if Axis are currently active
            if self.active == "B" and self.clock_started:
                current_elapsed = self.get_current_elapsed()
                base_time += current_elapsed
            return max(0, base_time)
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
                except:
                    pass
            
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
            
            # Only check for auto-switch if we have previous scores to compare
            # This prevents false triggers on first connection
            if self.auto_switch and self.started and hasattr(self, '_first_update_done'):
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
        logger.info(f"Score check - Allied: {self.last_scores['allied']} -> {current_allied}, Axis: {self.last_scores['axis']} -> {current_axis}")
        
        # Check for score increases (point captures)
        if current_allied > self.last_scores['allied']:
            logger.info(f"Allied score increased! Switching to Allies")
            await self._auto_switch_to('A', "Allies captured the center point")
        elif current_axis > self.last_scores['axis']:
            logger.info(f"Axis score increased! Switching to Axis") 
            await self._auto_switch_to('B', "Axis captured the center point")
        else:
            logger.debug(f"No score changes detected")
        
        # Update last known scores
        self.last_scores = {'allied': current_allied, 'axis': current_axis}
    
    async def _auto_switch_to(self, team: str, reason: str = "Auto-switch"):
        """Auto-switch teams with proper time tracking"""
        if self.active == team:
            return
        
        now = datetime.datetime.now(timezone.utc)
        
        # IMPORTANT: Update accumulated time BEFORE switching
        if self.active and self.last_switch and self.clock_started:
            elapsed = (now - self.last_switch).total_seconds()
            
            # Safeguard: Don't allow negative or unrealistic elapsed times (more than 4 hours)
            if elapsed < 0 or elapsed > 14400:  # More than 4 hours
                logger.error(f"Invalid elapsed time: {elapsed} seconds. Not adding to totals.")
            else:
                if self.active == "A":
                    self.time_a += elapsed
                    logger.info(f"Added {elapsed:.1f}s to Allies. Total: {self.time_a:.1f}s")
                elif self.active == "B":
                    self.time_b += elapsed
                    logger.info(f"Added {elapsed:.1f}s to Axis. Total: {self.time_b:.1f}s")
        
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
        
        # Send notification to game (if messaging works)
        if self.crcon_client:
            team_name = "Allies" if team == "A" else "Axis"
            
            # For game messages, use the accumulated times (not including current session)
            # This prevents timing confusion during switches
            allies_time = self.format_time(self.time_a)
            axis_time = self.format_time(self.time_b)
            
            logger.info(f"Sending game message - Allies: {self.time_a}s ({allies_time}), Axis: {self.time_b}s ({axis_time})")
            
            await self.crcon_client.send_message(f"🔄 {team_name} captured the center point! | Allies: {allies_time} | Axis: {axis_time}")
        
        # IMPORTANT: Update the Discord embed immediately
        if self.message:
            try:
                await self.message.edit(embed=build_embed(self))
                logger.info(f"Discord embed updated after auto-switch to {team}")
            except Exception as e:
                logger.error(f"Failed to update Discord embed: {e}")
            
        logger.info(f"Auto-switched to team {team}: {reason}")
    
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
        team_view = self.game_data.get('team_view', {})
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
        """Format seconds into readable time"""
        # Handle None, negative, or invalid values
        if secs is None or secs < 0:
            return "0:00:00"
        
        # Ensure we have a valid number
        try:
            secs = int(secs)
        except (ValueError, TypeError):
            logger.warning(f"Invalid time value for formatting: {secs}")
            return "0:00:00"
        
        # Cap at reasonable maximum (24 hours)
        if secs > 86400:
            logger.warning(f"Capping large time value: {secs}s -> 24:00:00")
            secs = 86400
            
        return str(datetime.timedelta(seconds=secs))

    def get_mid_point_minutes(self):
        """Return points for mid point hold time (1pt per minute)"""
        a_pts = int(self.mid_point_time_a // 60)
        b_pts = int(self.mid_point_time_b // 60)
        return a_pts, b_pts

    def get_bonus_points(self):
        """Return bonus points for holding both mid and fourth points (1.5/min)"""
        # Find overlap time (minimum of both hold times for each team)
        a_overlap = min(self.mid_point_time_a, self.fourth_point_time_a)
        b_overlap = min(self.mid_point_time_b, self.fourth_point_time_b)
        a_bonus = int(a_overlap // 60 * 1.5)
        b_bonus = int(b_overlap // 60 * 1.5)
        return a_bonus, b_bonus

def user_is_admin(interaction: discord.Interaction):
    admin_role = os.getenv('ADMIN_ROLE_NAME', 'admin').lower()
    return any(role.name.lower() == admin_role for role in interaction.user.roles)

def build_embed(clock: ClockState):
    """Build Discord embed focused on TIME CONTROL"""
    embed = discord.Embed(
        title="🎯 🔥 HLL Tank Overwatch 🔥 🎯",
        description="**Control the center point to win!**",
        color=0x800020
    )
    
    # Add game information
    game_info = clock.get_game_info()
    
    # Start with map and players
    embed.description += f"\n🗺️ **Map:** {game_info['map']}\n👥 **Players:** {game_info['players']}/100"
    
    # Add server game time instead of match duration
    if game_info['game_time'] > 0:
        embed.description += f"\n⏰ **Server Game Time:** `{clock.format_time(game_info['game_time'])}`"
    
    # Get live status for both teams
    allies_status = clock.get_live_status('A')
    axis_status = clock.get_live_status('B')
    
    # Build team information focused on TIME CONTROL
    allies_value = f"**Control Time:** `{clock.format_time(allies_status['total_time'])}`\n**Status:** {allies_status['status']}"
    axis_value = f"**Control Time:** `{clock.format_time(axis_status['total_time'])}`\n**Status:** {axis_status['status']}"
    
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
    
    embed.add_field(name="🇺🇸 Allies", value=allies_value, inline=False)
    embed.add_field(name="🇩🇪 Axis", value=axis_value, inline=False)
    
    # --- Add live mid point and bonus points ---
    a_pts, b_pts = clock.get_mid_point_minutes()
    a_bonus, b_bonus = clock.get_bonus_points()
    embed.add_field(
        name="🏅 Mid Point Hold Points (Live)",
        value=f"🇺🇸 Allies: `{a_pts}` pts\n🇩🇪 Axis: `{b_pts}` pts",
        inline=False
    )
    embed.add_field(
        name="🏅 Bonus Points (Hold 3rd & 4th, Live)",
        value=f"🇺🇸 Allies: `{a_bonus}` pts\n🇩🇪 Axis: `{b_bonus}` pts",
        inline=False
    )

    # Add current leader status
    if allies_status['total_time'] > axis_status['total_time']:
        leader_text = "🏆 **Current Leader:** Allies"
    elif axis_status['total_time'] > allies_status['total_time']:
        leader_text = "🏆 **Current Leader:** Axis"
    else:
        leader_text = "⚖️ **Status:** Tied"
    
    embed.add_field(name="🎯 Point Control", value=leader_text, inline=False)
    
    # Footer with connection status
    connection_status = f"🟢 CRCON Connected" if clock.crcon_client else "🔴 CRCON Disconnected"
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
        await clock.message.edit(embed=build_embed(clock), view=view)
        await interaction.followup.send("✅ Match started! Connecting to CRCON...", ephemeral=True)

        # Connect to CRCON after responding to Discord
        crcon_connected = await clock.connect_crcon()
        
        if crcon_connected:
            clock.auto_switch = os.getenv('CRCON_AUTO_SWITCH', 'false').lower() == 'true'
            await clock.crcon_client.send_message("🎯 HLL Tank Overwatch Match Started! Center point control timer active.")
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
        await clock.message.edit(embed=build_embed(clock), view=self)
        
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
        if old_clock.crcon_client:
            await old_clock.crcon_client.__aexit__(None, None, None)

        clocks[self.channel_id] = ClockState()
        clock = clocks[self.channel_id]
        view = StartControls(self.channel_id)

        await interaction.response.defer()
        embed = build_embed(clock)
        await interaction.followup.send(embed=embed, view=view)
        clock.message = await interaction.original_response()

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
    async def stop_timer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not user_is_admin(interaction):
            return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

        clock = clocks[self.channel_id]
        
        # IMPORTANT: Finalize the current session before stopping
        if clock.active and clock.last_switch:
            elapsed = (datetime.datetime.now(timezone.utc) - clock.last_switch).total_seconds()
            if clock.active == "A":
                clock.time_a += elapsed
            elif clock.active == "B":
                clock.time_b += elapsed

        clock.active = None
        clock.started = False

        # Send final message to game
        if clock.crcon_client:
            winner_msg = ""
            if clock.time_a > clock.time_b:
                winner_msg = "Allies controlled the center longer!"
            elif clock.time_b > clock.time_a:
                winner_msg = "Axis controlled the center longer!"
            else:
                winner_msg = "Perfect tie - equal control time!"
            
            await clock.crcon_client.send_message(
                f"🏁 Match Complete! {winner_msg} Allies: {clock.format_time(clock.time_a)} | Axis: {clock.format_time(clock.time_b)}"
            )

        # Create final embed
        embed = discord.Embed(title="🏁 Match Complete - Time Control Results!", color=0x800020)
        
        game_info = clock.get_game_info()
        if game_info['connection_status'] == 'Connected':
            embed.add_field(name="🗺️ Map", value=game_info['map'], inline=True)
            embed.add_field(name="👥 Players", value=f"{game_info['players']}/100", inline=True)

        # Final CONTROL times
        embed.add_field(name="🇺🇸 Allies Control Time", value=f"`{clock.format_time(clock.time_a)}`", inline=False)
        embed.add_field(name="🇩🇪 Axis Control Time", value=f"`{clock.format_time(clock.time_b)}`", inline=False)
        
        # Determine winner by TIME CONTROL
        time_diff = abs(clock.time_a - clock.time_b)
        if clock.time_a > clock.time_b:
            winner = f"🏆 **Allies Victory**\n*+{clock.format_time(time_diff)} control advantage*"
        elif clock.time_b > clock.time_a:
            winner = f"🏆 **Axis Victory**\n*+{clock.format_time(time_diff)} control advantage*"
        else:
            winner = "🤝 **Perfect Draw**\n*Equal control time*"
        
        embed.add_field(name="🎯 Point Control Winner", value=winner, inline=False)
        embed.add_field(name="🔄 Total Switches", value=str(len(clock.switches)), inline=True)

        await interaction.response.defer()
        await clock.message.edit(embed=embed, view=None)

        # Log results
        await log_results(clock, game_info)

    async def _switch_team(self, interaction: discord.Interaction, team: str):
        if not user_is_admin(interaction):
            return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

        clock = clocks[self.channel_id]
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
            if clock.active and clock.last_switch:
                elapsed = (now - clock.last_switch).total_seconds()
                
                # Safeguard: Don't allow negative or unrealistic elapsed times (more than 4 hours)
                if elapsed < 0 or elapsed > 14400:  # More than 4 hours
                    logger.error(f"Invalid elapsed time in manual switch: {elapsed} seconds. Not adding to totals.")
                else:
                    # Add elapsed time to the previously active team
                    if clock.active == "A":
                        clock.time_a += elapsed
                        logger.info(f"Manual switch: Added {elapsed:.1f}s to Allies. Total: {clock.time_a:.1f}s")
                    elif clock.active == "B":
                        clock.time_b += elapsed
                        logger.info(f"Manual switch: Added {elapsed:.1f}s to Axis. Total: {clock.time_b:.1f}s")
            
            # Switch to new team
            clock.active = team
            clock.last_switch = now
            clock.switches.append(switch_data)

        # Send notification
        if clock.crcon_client:
            team_name = "Allies" if team == "A" else "Axis"
            
            # For game messages, use the accumulated times (not including current session)
            # This prevents timing confusion during switches  
            allies_time = clock.format_time(clock.time_a)
            axis_time = clock.format_time(clock.time_b)
            
            logger.info(f"Manual switch - Sending game message - Allies: {clock.time_a}s ({allies_time}), Axis: {clock.time_b}s ({axis_time})")
            
            await clock.crcon_client.send_message(f"⚔️ {team_name} captured the center point! | Allies: {allies_time} | Axis: {axis_time}")

        await interaction.response.defer()
        await clock.message.edit(embed=build_embed(clock), view=self)

async def log_results(clock: ClockState, game_info: dict):
    """Log match results focused on time control"""
    global RESULTS_TARGET
    
    # Use the configured target (thread or channel)
    if RESULTS_TARGET:
        target = bot.get_channel(RESULTS_TARGET)
    elif LOG_CHANNEL_ID:
        target = bot.get_channel(LOG_CHANNEL_ID)
    else:
        return  # No logging configured
        
    if not target:
        return
    
    embed = discord.Embed(title="🏁 HLL Tank Overwatch Match Complete", color=0x800020)
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
    
    await target.send(embed=embed)

# Update task - shows in-game time
@tasks.loop(seconds=int(os.getenv('UPDATE_INTERVAL', '15')))
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
        # Only check for auto-stop if match has been running for at least 2 minutes
        # This prevents false triggers on startup
        match_duration = (datetime.datetime.now(timezone.utc) - clock.match_start_time).total_seconds()
        game_info = clock.get_game_info()
        
        if (match_duration > 120 and  # Match running for at least 2 minutes
            game_info['connection_status'] == 'Connected' and 
            game_info['game_time'] <= 30 and 
            game_info['game_time'] > 0):  # Make sure we have valid game time data
            logger.info("Game time ended, automatically stopping match")
            await auto_stop_match(clock, game_info)
            return

        # Update display with current game time
        try:
            await clock.message.edit(embed=build_embed(clock))
        except discord.HTTPException as e:
            logger.warning(f"Could not update message: {e}")

    except Exception as e:
        logger.error(f"Error in match updater: {e}")

async def auto_stop_match(clock: ClockState, game_info: dict):
    """Automatically stop match when game time ends"""
    try:
        # IMPORTANT: Finalize the current session before stopping
        if clock.active and clock.last_switch:
            elapsed = (datetime.datetime.now(timezone.utc) - clock.last_switch).total_seconds()
            if clock.active == "A":
                clock.time_a += elapsed
            elif clock.active == "B":
                clock.time_b += elapsed

        clock.active = None
        clock.started = False

        # Send final message to game
        if clock.crcon_client:
            winner_msg = ""
            if clock.time_a > clock.time_b:
                winner_msg = "Allies controlled the center longer!"
            elif clock.time_b > clock.time_a:
                winner_msg = "Axis controlled the center longer!"
            else:
                winner_msg = "Perfect tie - equal control time!"
            
            await clock.crcon_client.send_message(
                f"🏁 Match Complete! {winner_msg} Allies: {clock.format_time(clock.time_a)} | Axis: {clock.format_time(clock.time_b)}"
            )

        # Create final embed
        embed = discord.Embed(title="🏁 Match Complete - Time Control Results!", color=0x800020)
        embed.add_field(name="🕒 End Reason", value="⏰ Game Time Expired", inline=False)
        
        if game_info['connection_status'] == 'Connected':
            embed.add_field(name="🗺️ Map", value=game_info['map'], inline=True)
            embed.add_field(name="👥 Players", value=f"{game_info['players']}/100", inline=True)

        # Final CONTROL times
        embed.add_field(name="🇺🇸 Allies Control Time", value=f"`{clock.format_time(clock.time_a)}`", inline=False)
        embed.add_field(name="🇩🇪 Axis Control Time", value=f"`{clock.format_time(clock.time_b)}`", inline=False)
        
        # Determine winner by TIME CONTROL
        time_diff = abs(clock.time_a - clock.time_b)
        if clock.time_a > clock.time_b:
            winner = f"🏆 **Allies Victory**\n*+{clock.format_time(time_diff)} control advantage*"
        elif clock.time_b > clock.time_a:
            winner = f"🏆 **Axis Victory**\n*+{clock.format_time(time_diff)} control advantage*"
        else:
            winner = "🤝 **Perfect Draw**\n*Equal control time*"
        
        embed.add_field(name="🎯 Point Control Winner", value=winner, inline=False)
        embed.add_field(name="🔄 Total Switches", value=str(len(clock.switches)), inline=True)

        # Update the message with final results
        await clock.message.edit(embed=embed, view=None)
        
        # Also post to the channel (not just edit the existing message)
        channel = clock.message.channel
        await channel.send("🏁 **MATCH COMPLETE!** 🏁", embed=embed)

        # Log results to log channel
        await log_results(clock, game_info)
        
        logger.info("Match automatically stopped due to game time expiring")

    except Exception as e:
        logger.error(f"Error in auto_stop_match: {e}")

# Bot commands
@bot.tree.command(name="setup_results", description="Configure where match results are posted")
async def setup_results(interaction: discord.Interaction, 
                       channel: discord.TextChannel = None, 
                       thread: discord.Thread = None):
    try:
        if not user_is_admin(interaction):
            return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
        
        # Store the choice globally (you could also use a simple file or database)
        global RESULTS_TARGET
        
        if thread:
            RESULTS_TARGET = thread.id
            await interaction.response.send_message(f"✅ Match results will be posted to thread: {thread.name}", ephemeral=True)
            logger.info(f"Results target set to thread: {thread.name} ({thread.id}) by {interaction.user}")
        elif channel:
            RESULTS_TARGET = channel.id
            await interaction.response.send_message(f"✅ Match results will be posted to channel: {channel.name}", ephemeral=True)
            logger.info(f"Results target set to channel: {channel.name} ({channel.id}) by {interaction.user}")
        else:
            RESULTS_TARGET = None
            await interaction.response.send_message("✅ Match results posting disabled", ephemeral=True)
            logger.info(f"Results posting disabled by {interaction.user}")
            
    except Exception as e:
        logger.error(f"Error in setup_results command: {e}")
        try:
            await interaction.response.send_message(f"❌ Error setting up results: {str(e)}", ephemeral=True)
        except:
            pass

@bot.tree.command(name="reverse_clock", description="Start the HLL Tank Overwatch time control clock")
async def reverse_clock(interaction: discord.Interaction):
    try:
        # Respond IMMEDIATELY to prevent timeout
        await interaction.response.send_message("⏳ Creating clock...", ephemeral=True)
        
        channel_id = interaction.channel_id
        clocks[channel_id] = ClockState()

        embed = build_embed(clocks[channel_id])
        view = StartControls(channel_id)

        # Send the actual clock to the channel
        posted_message = await interaction.channel.send(embed=embed, view=view)
        clocks[channel_id].message = posted_message
        
        # Update the ephemeral message
        await interaction.edit_original_response(content="✅ HLL Tank Overwatch clock ready!")
        
        logger.info(f"Clock created successfully by {interaction.user} in channel {channel_id}")
        
    except Exception as e:
        logger.error(f"Error in reverse_clock command: {e}")
        try:
            await interaction.edit_original_response(content=f"❌ Error creating clock: {str(e)}")
        except:
            try:
                await interaction.followup.send(f"❌ Error creating clock: {str(e)}", ephemeral=True)
            except:
                pass

@bot.tree.command(name="crcon_status", description="Check CRCON connection status")
async def crcon_status(interaction: discord.Interaction):
    # Respond immediately
    await interaction.response.send_message("🔍 Checking CRCON status...", ephemeral=True)
    
    embed = discord.Embed(title="🔗 CRCON Status", color=0x0099ff)
    
    try:
        test_client = APIKeyCRCONClient()
        async with test_client as client:
            live_data = await client.get_live_game_state()
            
            if live_data:
                game_state = live_data.get('game_state', {})
                embed.add_field(name="Connection", value="✅ Connected", inline=True)
                embed.add_field(name="API Key", value="✅ Valid", inline=True)
                embed.add_field(name="Data", value="✅ Available", inline=True)
                embed.add_field(name="Current Map", value=game_state.get('current_map', 'Unknown'), inline=True)
                embed.add_field(name="Players", value=f"{game_state.get('nb_players', 0)}/100", inline=True)
                embed.add_field(name="Server Status", value="🟢 Online", inline=True)
            else:
                embed.add_field(name="Connection", value="🟡 Connected", inline=True)
                embed.add_field(name="Data", value="❌ No data", inline=True)
                
    except Exception as e:
        embed.add_field(name="Connection", value="❌ Failed", inline=True)
        embed.add_field(name="Error", value=str(e)[:500], inline=False)
    
    # Configuration info
    embed.add_field(name="URL", value=os.getenv('CRCON_URL', 'Not set'), inline=True)
    embed.add_field(name="API Key", value=f"{os.getenv('CRCON_API_KEY', 'Not set')[:8]}..." if os.getenv('CRCON_API_KEY') else 'Not set', inline=True)
    
    await interaction.edit_original_response(content="", embed=embed)

@bot.tree.command(name="server_info", description="Get current HLL server information")
async def server_info(interaction: discord.Interaction):
    # Respond immediately
    await interaction.response.send_message("🔍 Getting server info...", ephemeral=True)
    
    try:
        test_client = APIKeyCRCONClient()
        async with test_client as client:
            live_data = await client.get_live_game_state()
            
            if not live_data:
                return await interaction.edit_original_response(content="❌ Could not retrieve server information")
            
            embed = discord.Embed(title="🎮 HLL Server Information", color=0x00ff00)
            
            game_state = live_data.get('game_state', {})
            map_info = live_data.get('map_info', {})
            
            # Extract map info
            map_name = 'Unknown'
            if isinstance(map_info, dict):
                if 'pretty_name' in map_info:
                    map_name = map_info['pretty_name']
                elif 'name' in map_info:
                    map_name = map_info['name']
                elif 'map' in map_info and isinstance(map_info['map'], dict):
                    map_name = map_info['map'].get('pretty_name', map_info['map'].get('name', 'Unknown'))
            
            embed.add_field(name="🗺️ Map", value=map_name, inline=True)
            embed.add_field(name="👥 Players", value=f"{game_state.get('nb_players', 0)}/100", inline=True)
            
            if game_state.get('time_remaining', 0) > 0:
                time_remaining = game_state['time_remaining']
                embed.add_field(name="⏱️ Game Time", value=f"{time_remaining//60}:{time_remaining%60:02d}", inline=True)
            
            embed.timestamp = datetime.datetime.now(timezone.utc)
            await interaction.edit_original_response(content="", embed=embed)
            
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Error retrieving server info: {str(e)}")

@bot.tree.command(name="test_map", description="Quick map data test")
async def test_map(interaction: discord.Interaction):
    # Respond immediately  
    await interaction.response.send_message("🧪 Testing map data...", ephemeral=True)
    
    try:
        test_client = APIKeyCRCONClient()
        async with test_client as client:
            live_data = await client.get_live_game_state()
            
            if not live_data:
                return await interaction.edit_original_response(content="❌ No data")
            
            map_info = live_data.get('map_info', {})
            game_state = live_data.get('game_state', {})
            
            msg = f"**Map Info:** {map_info}\n\n**Game State:** {game_state}"
            
            # Truncate if too long
            if len(msg) > 1900:
                msg = msg[:1900] + "..."
            
            await interaction.edit_original_response(content=f"```\n{msg}\n```")
            
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Error: {str(e)}")

@bot.tree.command(name="send_message", description="Send a message to the HLL server")
async def send_server_message(interaction: discord.Interaction, message: str):
    if not user_is_admin(interaction):
        return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
    
    # Respond immediately
    await interaction.response.send_message("📤 Sending message to server...", ephemeral=True)
    
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
            
            await interaction.edit_original_response(content="", embed=embed)
            
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Error: {str(e)}")

@bot.tree.command(name="test_bot", description="Test if the bot is working correctly")
async def test_bot(interaction: discord.Interaction):
    try:
        await interaction.response.send_message("✅ Bot is working! All systems operational.", ephemeral=True)
        logger.info(f"Test command used successfully by {interaction.user}")
    except Exception as e:
        logger.error(f"Error in test_bot command: {e}")

@bot.tree.command(name="test_times", description="Test current time calculations (admin only)")
async def test_times(interaction: discord.Interaction):
    if not user_is_admin(interaction):
        return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
    
    await interaction.response.send_message("🧪 Testing time calculations...", ephemeral=True)
    
    # Find an active clock
    active_clock = None
    for clock in clocks.values():
        if clock.started:
            active_clock = clock
            break
    
    if not active_clock:
        return await interaction.edit_original_response(content="❌ No active match found. Start a match first with /reverse_clock")
    
    try:
        # Get current times
        allies_accumulated = active_clock.time_a
        axis_accumulated = active_clock.time_b
        allies_total = active_clock.total_time('A')
        axis_total = active_clock.total_time('B')
        current_elapsed = active_clock.get_current_elapsed()
        
        # Format times
        allies_acc_formatted = active_clock.format_time(allies_accumulated)
        axis_acc_formatted = active_clock.format_time(axis_accumulated) 
        allies_total_formatted = active_clock.format_time(allies_total)
        axis_total_formatted = active_clock.format_time(axis_total)
        current_elapsed_formatted = active_clock.format_time(current_elapsed)
        
        debug_info = f"""**Time Debug Info:**

**Accumulated Times:**
• Allies: {allies_accumulated}s → {allies_acc_formatted}
• Axis: {axis_accumulated}s → {axis_acc_formatted}

**Total Times (with current session):**
• Allies: {allies_total}s → {allies_total_formatted}  
• Axis: {axis_total}s → {axis_total_formatted}

**Current Session:**
• Active Team: {active_clock.active or 'None'}
• Session Time: {current_elapsed}s → {current_elapsed_formatted}
• Clock Started: {active_clock.clock_started}

**Game Message Would Show:**
⚔️ Test captured the center point! | Allies: {allies_acc_formatted} | Axis: {axis_acc_formatted}"""

        await interaction.edit_original_response(content=debug_info)
        
        # Also send a test message to the game server
        if active_clock.crcon_client:
            await active_clock.crcon_client.send_message(f"🧪 TEST MESSAGE | Allies: {allies_acc_formatted} | Axis: {axis_acc_formatted}")
            
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Error testing times: {str(e)}")

@bot.tree.command(name="help_clock", description="Show help for the time control clock")
async def help_clock(interaction: discord.Interaction):
    embed = discord.Embed(title="🎯 HLL Tank Overwatch Clock Help", color=0x0099ff)
    
    embed.add_field(
        name="📋 Commands",
        value=(
            "`/reverse_clock` - Start a new time control clock\n"
            "`/setup_results` - Choose where match results are posted\n"
            "`/test_bot` - Test if the bot is working\n"
            "`/test_times` - Debug time calculations (admin)\n"
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
    logger.error(f"Slash command error: {error} | Command: {interaction.command.name if interaction.command else 'Unknown'} | User: {interaction.user}")
    
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(error_msg, ephemeral=True)
        else:
            await interaction.followup.send(error_msg, ephemeral=True)
    except Exception as e:
        logger.error(f"Could not send error message: {e}")

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
    
    # Sync commands
    await bot.wait_until_ready()
    try:
        synced = await bot.tree.sync()
        logger.info(f"✅ Synced {len(synced)} slash commands")
        command_names = [cmd.name for cmd in synced]
        logger.info(f"Commands: {', '.join(command_names)}")
        print(f"🎉 HLL Tank Overwatch Clock ready! Use /reverse_clock to start")
    except Exception as e:
        logger.error(f"❌ Command sync failed: {e}")

# Main execution
if __name__ == "__main__":
    print("🚀 Starting HLL Tank Overwatch Bot...")
    
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
    
    # Show configuration
    print(f"🔗 CRCON: {os.getenv('CRCON_URL', 'http://localhost:8010')}")
    print(f"🔑 API Key: {api_key[:8]}...")
    print(f"👑 Admin Role: {os.getenv('ADMIN_ROLE_NAME', 'admin')}")
    print(f"🤖 Bot Name: {os.getenv('BOT_NAME', 'HLLTankBot')}")
    print(f"⏱️ Update Interval: {os.getenv('UPDATE_INTERVAL', '15')}s")
    print(f"🔄 Auto-Switch: {os.getenv('CRCON_AUTO_SWITCH', 'true')}")
    
    log_channel = os.getenv('LOG_CHANNEL_ID', '0')
    if log_channel != '0':
        print(f"📋 Log Channel: {log_channel}")
    else:
        print("📋 Log Channel: Disabled")
    
    print("🎯 Focus: TIME CONTROL - Win by holding the center point longest!")
    
    try:
        bot.run(token)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"❌ Bot startup failed: {e}")

# This is a helper function for testing, not a Discord slash command.
async def run_simulation_series(interaction):
    """Run 10 point control simulations for testing."""
    simulations = [
        ("A", "A", 600, 600),
        ("A", "B", 600, 600),
        ("B", "A", 600, 600),
        ("B", "B", 600, 600),
        ("A", "A", 1200, 300),
        ("A", "B", 300, 1200),
        ("B", "A", 1200, 300),
        ("B", "B", 300, 1200),
        ("A", "A", 1800, 1800),
        ("B", "B", 1800, 1800),
    ]
    results = []
    for idx, (mid_owner, fourth_owner, mid_secs, fourth_secs) in enumerate(simulations, 1):
        await simulate_points(interaction, mid_owner, fourth_owner, mid_secs, fourth_secs)
        results.append(f"Simulation {idx}: Mid={mid_owner}({mid_secs}s), Fourth={fourth_owner}({fourth_secs}s)")
    await interaction.followup.send("\n".join(results), ephemeral=True)
