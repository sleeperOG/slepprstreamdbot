import os
import sys
import logging
import asyncio

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
import yt_dlp

# ─── Logging Configuration ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ─── Environment & Token ───────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
masked = TOKEN[:6] + "…" + TOKEN[-6:] if TOKEN else "None"
logging.info(f"TOKEN loaded: {masked}")
if not TOKEN:
    logging.critical("DISCORD_TOKEN missing in .env")
    sys.exit(1)

# ─── Bot & Intents ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ─── Constants ────────────────────────────────────────────────────────────────
FFMPEG_OPTIONS = {"options": "-vn"}

# ─── Per-Guild State Management ───────────────────────────────────────────────
class GuildState:
    def __init__(self):
        self.queue = []
        self.history = []
        self.loop_mode = "off"
        self.bitrate_mode = "default"
        self.autoqueue_enabled = False
        self.now_playing_message = None
        self.autoqueue_message = None

guild_states: dict[int, GuildState] = {}

def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]

# ─── Utilities: YT-DLP & Feed Query ─────────────────────────────────────────────
def generate_feed_query(info: dict) -> str:
    # Remove common noise words from title
    title = info.get("title", "")
    noise_words = ["official", "video", "lyrics", "audio", "hd", "hq", "mv"]
    filtered_title = " ".join(
        [w for w in title.split() if w.lower() not in noise_words]
    )

    artist = info.get("artist") or ""
    genres = " ".join(info.get("genre", []))
    return f"{artist} {genres} related {filtered_title} audio".strip()

async def get_audio_info(query: str, bitrate_mode: str, exclude_url: str = None) -> dict:
    def extract():
        fmt = "bestaudio"
        if bitrate_mode == "low":
            fmt = "bestaudio[ext=webm][abr<=160]"
        opts = {
            "format": fmt,
            "quiet": True,
            "default_search": "ytsearch10",  # get top 10 results
            "noplaylist": True
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            entries = info["entries"] if "entries" in info else [info]

            # Filter out unwanted results
            filtered = [
                e for e in entries
                if (exclude_url is None or e.get("url") != exclude_url)
                and 90 <= e.get("duration", 0) <= 900
            ]
            if not filtered:
                raise ValueError("No suitable results found.")

            entry = filtered[0]
            return {
                "url": entry["url"],
                "title": entry["title"],
                "thumbnail": entry.get("thumbnail"),
                "artist": entry.get("uploader"),
                "genre": entry.get("categories", []),
                "views": entry.get("view_count", 0),
                "duration": entry.get("duration", 0)
            }

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, extract)

# ─── Playback & Auto-Feed ─────────────────────────────────────────────────────
async def auto_feed(interaction: discord.Interaction, song_info: dict):
    state = get_state(interaction.guild.id)
    query = generate_feed_query(song_info)
    try:
        rec = await get_audio_info(query, state.bitrate_mode, exclude_url=song_info["url"])
        state.queue.append(rec)

        embed = discord.Embed(
            title="Auto-Queued",
            description=f"{rec['title']}\n*(based on {song_info['title']})*",
            color=0x1DB954
        )
        if rec["thumbnail"]:
            embed.set_thumbnail(url=rec["thumbnail"])

        if state.autoqueue_message:
            await state.autoqueue_message.edit(embed=embed)
        else:
            state.autoqueue_message = await interaction.channel.send(embed=embed)
    except Exception as e:
        logging.error(f"Auto-feed error: {e}")
        await interaction.channel.send(f"Feed error: {e}")

async def play_next(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client

    if not vc or not vc.is_connected():
        return await interaction.channel.send("Not connected to a voice channel.")

    if vc.is_playing() or vc.is_paused():
        vc.stop()

    # If queue is empty, try auto‑queue before giving up
    if not state.queue:
        if state.autoqueue_enabled and state.history:
            await auto_feed(interaction, state.history[-1])
        if not state.queue:
            return await interaction.channel.send("Queue is empty.")

    # Pop the next song
    song = state.queue.pop(0)
    state.history.append(song)

    source = discord.FFmpegPCMAudio(song["url"], **FFMPEG_OPTIONS)

    def _after_play(err):
        coro = play_next(interaction)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)

    vc.play(source, after=_after_play)

    # Now playing embed
    embed = discord.Embed(title="Now Playing", description=song["title"], color=0x1DB954)
    if song["thumbnail"]:
        embed.set_thumbnail(url=song["thumbnail"])

    if state.now_playing_message:
        await state.now_playing_message.edit(embed=embed)
    else:
        state.now_playing_message = await interaction.channel.send(embed=embed)

    # Queue the next recommendation if auto‑queue is enabled
    if state.autoqueue_enabled:
        await auto_feed(interaction, song)

    def _after_play(err):
        coro = play_next(interaction)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)

    vc.play(source, after=_after_play)

    # Now playing embed
    embed = discord.Embed(title="Now Playing", description=song["title"], color=0x1DB954)
    if song["thumbnail"]:
        embed.set_thumbnail(url=song["thumbnail"])

    if state.now_playing_message:
        await state.now_playing_message.edit(embed=embed)
    else:
        state.now_playing_message = await interaction.channel.send(embed=embed)

    # Queue the next recommendation if auto‑queue is enabled
    if state.autoqueue_enabled:
        await auto_feed(interaction, song)

# ─── UI: Confirmation View ────────────────────────────────────────────────────
class ConfirmView(View):
    def __init__(self, info: dict, interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.info = info
        self.interaction = interaction

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.interaction.user.id:
            return await interaction.response.send_message(
                "Not your confirmation.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        state = get_state(self.interaction.guild.id)
        state.queue.append(self.info)

        vc = self.interaction.guild.voice_client
        if vc and not vc.is_playing():
            await play_next(self.interaction)

        await interaction.edit_original_response(
            embed=None,
            content="Added to queue.",
            view=None
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.interaction.user.id:
            return await interaction.response.send_message(
                "Not your confirmation.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        await interaction.edit_original_response(
            embed=None,
            content="Playback cancelled.",
            view=None
        )
        self.stop()

# ─── Slash Commands ──────────────────────────────────────────────────────────
@bot.tree.command(name="status", description="Check bot voice status")
async def status(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    msg = f"Connected to {vc.channel.name}" if vc and vc.is_connected() else "Not connected."
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("You need to be in a voice channel.", ephemeral=True)

    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        return await interaction.response.send_message("Already connected.", ephemeral=True)

    await channel.connect()
    await interaction.response.send_message("Joined your voice channel.", ephemeral=True)

@bot.tree.command(name="leave", description="Leave the voice channel")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        await vc.disconnect()
        await interaction.response.send_message(
            "Left voice channel.",
            ephemeral=True
        )
    else:
                                                                                           # Silently acknowledge without sending a message
        await interaction.response.defer(ephemeral=True)

@bot.tree.command(name="play", description="Play a song by search or link")
@app_commands.describe(query="Search terms or YouTube URL")
async def play(interaction: discord.Interaction, query: str):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message(
            "Not connected. Use `/join` first.",
            ephemeral=True
        )

    # Defer the response as ephemeral so the whole flow stays private
    await interaction.response.defer(ephemeral=True)
    try:
        info = await get_audio_info(query, state.bitrate_mode)
        embed = discord.Embed(
            title="Confirm Playback",
            description=info["title"],
            color=0x1DB954
        )
        if info["thumbnail"]:
            embed.set_thumbnail(url=info["thumbnail"])
        embed.set_footer(text="Click to confirm or cancel.")

        # Send the confirmation prompt as ephemeral
        await interaction.followup.send(
            embed=embed,
            view=ConfirmView(info, interaction),
            ephemeral=True
        )
    except Exception as e:
        logging.error(f"Play command error: {e}")
        await interaction.followup.send(f"Error: {e}", ephemeral=True)

@bot.tree.command(name="autoqueue", description="Enable auto-queue of similar tracks")
async def autoqueue(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state.autoqueue_enabled = True
    await interaction.response.send_message("Auto-queue enabled.", ephemeral=True)

@bot.tree.command(name="bitrate", description="Set audio bitrate mode")
@app_commands.describe(mode="Which bitrate to use")
@app_commands.choices(mode=[
    Choice(name="default", value="default"),
    Choice(name="low", value="low")
])
async def bitrate(interaction: discord.Interaction, mode: str):
    state = get_state(interaction.guild.id)
    state.bitrate_mode = mode
    await interaction.response.send_message(f"Bitrate set to `{mode}`.", ephemeral=True)

# ─── Startup & Command Sync ───────────────────────────────────────────────────
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user}")
    await bot.tree.sync()
    logging.info("Slash commands synced.")

bot.run(TOKEN)