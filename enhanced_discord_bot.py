#!/usr/bin/env python3
"""
HLL Discord Bot with API Key CRCON Integration
Time Control Focused - Win by controlling the center point longest!
"""

import asyncio
import os
import discord
import datetime
import aiohttp
import logging
import random
from pathlib import Path
from dotenv import load_dotenv
from discord.ext import commands, tasks
from discord import app_commands
from datetime import timezone, timedelta
from kill_feed_client import KillFeedClient

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Environment and Config ---
load_dotenv()
if not os.getenv('RAILWAY_ENVIRONMENT'):
    for directory in ['logs', 'match_reports', 'match_data', 'backups']:
        os.makedirs(directory, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

clocks = {}
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0')) if os.getenv('LOG_CHANNEL_ID', '0').isdigit() else 0
RESULTS_TARGET = None

KILLFEED_ENABLED = os.getenv('KILLFEED_ENABLED', 'true').lower() == 'true'
KILLFEED_CHANNEL_ID = int(os.getenv('KILLFEED_CHANNEL_ID', '0')) if os.getenv('KILLFEED_CHANNEL_ID', '0').isdigit() else 0
KILLFEED_SERVER_URL = os.getenv('KILLFEED_SERVER_URL', 'http://localhost:3000')

kill_feed_client = KillFeedClient(KILLFEED_SERVER_URL) if KILLFEED_ENABLED else None
class ClockState:
    """
    Tracks match timing, control times, CRCON integration, and game state.
    Add all necessary attributes and methods here.
    """
    def __init__(self):
        self.time_a = 0
        self.time_b = 0
        self.active = None
        self.last_switch = None
        self.started = False
        self.clock_started = False
        self.switches = []
        self.message = None
        self.crcon_client = None
        self.auto_switch = False
        self.match_start_time = datetime.datetime.now(timezone.utc)
        self.mid_point_time_a = 0
        self.mid_point_time_b = 0
        self.fourth_point_time_a = 0
        self.fourth_point_time_b = 0
        self.mid_point_owner = None
        self.fourth_point_owner = None
        # Add other attributes as needed

    def format_time(self, seconds):
        return str(datetime.timedelta(seconds=int(seconds)))

    def total_time(self, team):
        if team == "A":
            return self.time_a
        elif team == "B":
            return self.time_b
        return 0

    def get_game_info(self):
        return {
            "map": "Unknown",
            "players": 0,
            "connection_status": "Disconnected",
            "last_update": "N/A",
            "game_time": 0
        }

    async def update_from_game(self):
        pass

    async def connect_crcon(self):
        return False

    def get_current_elapsed(self):
        return 0
def build_embed(clock: ClockState) -> discord.Embed:
    """
    Build and return a Discord embed showing the current match state.
    """
    embed = discord.Embed(
        title="HLL Tank Overwatch Clock",
        description="Live match time control status",
        color=0x0099ff
    )
    embed.add_field(name="🇺🇸 Allies Control Time", value=f"`{clock.format_time(clock.time_a)}`", inline=True)
    embed.add_field(name="🇩🇪 Axis Control Time", value=f"`{clock.format_time(clock.time_b)}`", inline=True)
    embed.add_field(name="Active Team", value=clock.active or "None", inline=True)
    embed.add_field(name="Switches", value=str(len(clock.switches)), inline=True)
    embed.timestamp = datetime.datetime.now(timezone.utc)
    return embed

class APIKeyCRCONClient:
    """CRCON client using API key authentication"""
    def __init__(self):
        self.base_url = os.getenv('CRCON_URL', 'http://localhost:8010')
        self.api_key = os.getenv('CRCON_API_KEY')
        self.session = None
        self.timeout = aiohttp.ClientTimeout(total=int(os.getenv('CRCON_TIMEOUT', '15')))

    async def __aenter__(self):
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        self.session = aiohttp.ClientSession(timeout=self.timeout, headers=headers)
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
        if self.session:
            await self.session.close()
class StartControls(discord.ui.View):
    """View for starting the match clock."""
    def __init__(self, channel_id):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="▶️ Start Match", style=discord.ButtonStyle.success)
    async def start_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        clock = clocks[self.channel_id]
        clock.started = True
        clock.match_start_time = datetime.datetime.now(timezone.utc)
        view = TimerControls(self.channel_id)
        await interaction.response.edit_message(embed=build_embed(clock), view=view)
        
@bot.event
async def on_ready():
    logger.info(f"✅ Bot logged in as {bot.user}")
    logger.info(f"🔗 CRCON URL: {os.getenv('CRCON_URL', 'Not configured')}")
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
    await bot.wait_until_ready()
    try:
        synced = await bot.tree.sync()
        logger.info(f"✅ Synced {len(synced)} slash commands")
        command_names = [cmd.name for cmd in synced]
        logger.info(f"Commands: {', '.join(command_names)}")
        print(f"🎉 HLL Tank Overwatch Clock ready! Use /reverse_clock to start")
    except Exception as e:
        logger.error(f"❌ Command sync failed: {e}")
    if KILLFEED_ENABLED and kill_feed_client:
        bot.loop.create_task(kill_feed_client.run(bot, KILLFEED_CHANNEL_ID))

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
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "your_discord_bot_token_here":
        print("❌ DISCORD_TOKEN not configured!")
        exit(1)
    api_key = os.getenv("CRCON_API_KEY")
    if not api_key or api_key == "your_crcon_api_key_here":
        print("❌ CRCON_API_KEY not configured!")
        exit(1)
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

@bot.tree.command(name="simulate_points", description="Simulate CRCON point control for testing (admin only)")
async def simulate_points(
    interaction: discord.Interaction,
    mid_owner: str,
    fourth_owner: str,
    mid_secs: int = 300,
    fourth_secs: int = 180
):
    """
    Simulate CRCON data for mid and fourth point control.
    mid_owner/fourth_owner: 'A' for Allies, 'B' for Axis
    mid_secs/fourth_secs: seconds held by each team
    """
    if not user_is_admin(interaction):
        return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)

    # Find an active clock
    active_clock = None
    for clock in clocks.values():
        if clock.started:
            active_clock = clock
            break

    if not active_clock:
        return await interaction.response.send_message("❌ No active match found. Start a match first with /reverse_clock", ephemeral=True)

    # Simulate hold times
    if mid_owner == "A":
        active_clock.mid_point_time_a = mid_secs
        active_clock.mid_point_time_b = 0
        active_clock.mid_point_owner = "A"
    else:
        active_clock.mid_point_time_b = mid_secs
        active_clock.mid_point_time_a = 0
        active_clock.mid_point_owner = "B"

    if fourth_owner == "A":
        active_clock.fourth_point_time_a = fourth_secs
        active_clock.fourth_point_time_b = 0
        active_clock.fourth_point_owner = "A"
    else:
        active_clock.fourth_point_time_b = fourth_secs
        active_clock.fourth_point_time_a = 0
        active_clock.fourth_point_owner = "B"

    # Update the embed
    await active_clock.message.edit(embed=build_embed(active_clock))
    await interaction.response.send_message(
        f"✅ Simulated: Mid ({mid_owner}) {mid_secs}s, Fourth ({fourth_owner}) {fourth_secs}s",
        ephemeral=True
    )

@bot.tree.command(name="simulate_all", description="Run 10 point control simulations for testing (admin only)")
async def simulate_all(interaction: discord.Interaction):
    if not user_is_admin(interaction):
        return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
    await interaction.response.send_message("🧪 Running 10 simulations...", ephemeral=True)
    await run_simulation_series(interaction)

@bot.tree.command(name="simulate_mass", description="Simulate 100 games with random results (admin only)")
async def simulate_mass(interaction: discord.Interaction, num_games: int = 100):
    if not user_is_admin(interaction):
        return await interaction.response.send_message("❌ Admin role required.", ephemeral=True)
    await interaction.response.send_message(f"🧪 Running {num_games} random simulations...", ephemeral=True)
    await run_mass_simulation(interaction, num_games)






