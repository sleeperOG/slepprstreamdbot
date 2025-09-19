import os
import sys
import logging
import asyncio
import time

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
        rec["search_query"] = query  # store the query for later refresh
        rec["url_fetched_at"] = time.time()  # store fetch time to skip unnecessary refresh
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

import time

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

    # Decide if we need to refresh
    needs_refresh = (
        not song.get("url") or
        not song.get("url_fetched_at") or
        (time.time() - song["url_fetched_at"] > 900)  # 15 min
    )

    if needs_refresh:
        logging.info(f"[play_next] Refreshing URL for: {song['title']}")
        try:
            search_term = song.get("search_query", song["title"])
            refreshed_info = await get_audio_info(search_term, state.bitrate_mode)
            song["url"] = refreshed_info["url"]
            song["url_fetched_at"] = time.time()
        except Exception as e:
            logging.error(f"Failed to refresh stream URL: {e}")
            return await interaction.channel.send(f"Error refreshing stream for {song['title']}.")
    else:
        logging.info(f"[play_next] Using cached URL for: {song['title']} (age: {round(time.time() - song['url_fetched_at'], 1)}s)")

    # Use from_probe with reconnect options for stability
    source = await discord.FFmpegOpusAudio.from_probe(
        song["url"],
        before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        options='-vn'
    )

    def _after_play(err):
        coro = play_next(interaction)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)

    vc.play(source, after=_after_play)

    # Now playing embed
    embed = discord.Embed(title="Now Playing", description=song["title"], color=0x1DB954)
    if song.get("thumbnail"):
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

        # Debug log for when the song is officially queued
        logging.info(f"[confirm] Added to queue: {self.info['title']} "
                     f"(search_query='{self.info.get('search_query')}')")

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

        # Debug log for when playback is cancelled
        logging.info(f"[confirm] Playback cancelled for: {self.info['title']} "
                     f"(search_query='{self.info.get('search_query')}')")

        await interaction.edit_original_response(
            embed=None,
            content="Playback cancelled.",
            view=None
        )
        self.stop()

# ─── Slash Commands ──────────────────────────────────────────────────────────
@bot.tree.command(name="status", description="Check bot voice status and queue age")
async def status(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client

    if vc and vc.is_connected():
        msg = f"Connected to **{vc.channel.name}**"
        if hasattr(vc.channel, "bitrate"):
            msg += f" — Channel bitrate: {round(vc.channel.bitrate / 1000, 1)} kbps"
    else:
        msg = "Not connected to a voice channel."

    # Show queue with age info
    if state.queue:
        msg += "\n\n**Queue:**"
        now = time.time()
        for idx, song in enumerate(state.queue, start=1):
            age = None
            if song.get("url_fetched_at"):
                age = round(now - song["url_fetched_at"], 1)
            age_str = f"{age}s old" if age is not None else "no timestamp"
            msg += f"\n`{idx}.` {song['title']} — {age_str}"
    else:
        msg += "\n\nQueue is empty."

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
        info["search_query"] = query  # store the original search query for URL refresh
        info["url_fetched_at"] = time.time()

        # Debug log for tracking when the URL was fetched
        logging.info(f"[/play] URL fetched for: {info['title']} at {info['url_fetched_at']}")

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

@bot.tree.command(name="autoqueue", description="Toggle auto-queue of similar tracks")
async def autoqueue(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state.autoqueue_enabled = not state.autoqueue_enabled
    status = "enabled" if state.autoqueue_enabled else "disabled"
    await interaction.response.send_message(f"Auto-queue {status}.", ephemeral=True)

@bot.tree.command(name="bitrate", description="Set or view audio bitrate modes")
@app_commands.describe(mode="Which bitrate to use (leave empty to view all modes)")
@app_commands.choices(mode=[
    Choice(name="default", value="default"),
    Choice(name="low", value="low")
])
async def bitrate(interaction: discord.Interaction, mode: str = None):
    # Map modes to approximate audio specs
    bitrate_map = {
        "default": {"kbps": 160, "khz": 48, "bits": 16},
        "low": {"kbps": 96, "khz": 48, "bits": 16}
    }

    # Get actual Discord voice channel bitrate (bps → kbps)
    vc = interaction.guild.voice_client
    actual_bitrate_kbps = None
    if vc and vc.channel and hasattr(vc.channel, "bitrate"):
        actual_bitrate_kbps = round(vc.channel.bitrate / 1000, 1)

    # If no mode provided, list all available modes
    if mode is None:
        msg = "**Available bitrate modes:**\n"
        for m, specs in bitrate_map.items():
            msg += f"• `{m}` → {specs['kbps']} kbps (~{specs['khz']} kHz / {specs['bits']}-bit PCM)\n"
        if actual_bitrate_kbps:
            msg += f"\n**Channel bitrate limit:** {actual_bitrate_kbps} kbps"
        return await interaction.response.send_message(msg, ephemeral=True)

    # Set the mode
    state = get_state(interaction.guild.id)
    state.bitrate_mode = mode
    specs = bitrate_map.get(mode, {"kbps": "?", "khz": "?", "bits": "?"})
    kbps = specs["kbps"]
    khz = specs["khz"]
    bits = specs["bits"]

    # Build the response
    msg = (
        f"Bitrate mode set to `{mode}`.\n"
        f"**Approximate audio quality:** {kbps} kbps (~{khz} kHz / {bits}-bit PCM equivalent)"
    )

    if actual_bitrate_kbps:
        msg += f"\n**Channel bitrate limit:** {actual_bitrate_kbps} kbps"
        if kbps > actual_bitrate_kbps:
            msg += " (Your setting is higher than the channel's cap — audio will be limited)"
        elif kbps < actual_bitrate_kbps:
            msg += " (Your setting is below the channel's max — no quality loss from Discord cap)"

    await interaction.response.send_message(msg, ephemeral=True)

# ─── Startup & Command Sync ───────────────────────────────────────────────────
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user}")
    await bot.tree.sync()
    logging.info("Slash commands synced.")

bot.run(TOKEN)