import os
import sys
import logging
import asyncio
import time
import re
import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp.utils import DownloadError

load_dotenv()
SPOTIPY_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
TOKEN = os.getenv("DISCORD_TOKEN")

# match open.spotify.com/track/{id}
SPOTIFY_TRACK_RE = re.compile(
    r"https://open\.spotify\.com/track/([A-Za-z0-9]+)"
)
# match open.spotify.com/playlist/{id}
SPOTIFY_PLAYLIST_RE = re.compile(
    r"https://open\.spotify\.com/playlist/([A-Za-z0-9]+)"
)
# match open.spotify.com/album/{id}
SPOTIFY_ALBUM_RE = re.compile(
    r"https://open\.spotify\.com/album/([A-Za-z0-9]+)"
)

# app‐only Spotify client
_sp = spotipy.Spotify(
    client_credentials_manager=SpotifyClientCredentials(
        client_id=SPOTIPY_ID,
        client_secret=SPOTIPY_SECRET
    )
)

async def one_ephemeral_ack(interaction: discord.Interaction, content: str):
    """
    Deletes the previous ephemeral ack in this guild (if any),
    then defers & sends a new one, returning the Message.
    """
    state = get_state(interaction.guild.id)

    # delete last ack if it’s still around
    if state.last_ack:
        try:
            await state.last_ack.delete()
        except Exception:
            pass

    # defer ephemerally and send new ack
    await interaction.response.defer(ephemeral=True)
    msg = await interaction.followup.send(content, ephemeral=True)

    # store for next time
    state.last_ack = msg
    return msg

# ─── Logging Configuration ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ─── Environment & Token ───────────────────────────────────────────────────────
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

FFMPEG_OPTIONS = {"options": "-vn"}
class GuildState:
    def __init__(self):
        self.queue = []
        self.history = []
        self.loop_mode = "off"
        self.bitrate_mode = "default"
        self.autoqueue_enabled = False
        self.now_playing_message = None
        self.autoqueue_message = None
        self.paused = False

        # add this:
        self.last_ack: discord.Message | None = None

guild_states: dict[int, GuildState] = {}

def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]

GENRE_MAP = {
    "Don Toliver": "trap",
    "Young Thug": "trap",
    "Drake": "hip hop",
    "Arctic Monkeys": "indie rock",
    "Metro Boomin": "trap",
    "SZA": "r&b",
    "Kendrick Lamar": "conscious rap",
    "Playboi Carti": "rage",
    "PinkPantheress": "breakcore",
    "Aphex Twin": "ambient",
    # Add more mappings as needed
}

def infer_genre(info: dict) -> list[str]:
    artist = info.get("artist", "")
    for key, genre in GENRE_MAP.items():
        if key.lower() in artist.lower():
            return [genre]
    return []

# Optional: map known artists to genres
GENRE_MAP = {
    "Don Toliver": "trap",
    "Young Thug": "trap",
    "Drake": "hip hop",
    "Arctic Monkeys": "indie rock",
    "Metro Boomin": "trap",
    "SZA": "r&b",
    "Kendrick Lamar": "conscious rap",
    "Playboi Carti": "rage",
    "PinkPantheress": "breakcore",
    "Aphex Twin": "ambient",
}

def infer_genre(info: dict) -> list[str]:
    artist = info.get("artist", "")
    for key, genre in GENRE_MAP.items():
        if key.lower() in artist.lower():
            return [genre]
    return []

def normalise_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r'\[.*?\]|\(.*?\)', '', title)
    title = re.sub(r'[^a-z0-9\s]', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip()

def is_duplicate(candidate: dict, history: list) -> bool:
    cand_title = normalise_title(candidate.get("title", ""))
    cand_dur = candidate.get("duration")
    for song in history:
        hist_title = normalise_title(song.get("title", ""))
        hist_dur = song.get("duration")
        if cand_title == hist_title:
            return True
        if cand_dur and hist_dur and abs(cand_dur - hist_dur) <= 3:
            return True
    return False

def generate_feed_query(info: dict) -> str:
    title = info.get("title", "")
    artist = info.get("artist", "")
    genres = info.get("genre", []) or infer_genre(info)
    genre_str = " ".join(genres)

    # Remove bracketed tags and noise words
    title = re.sub(r'\[.*?\]|\(.*?\)', '', title)
    noise_words = ["official", "video", "lyrics", "audio", "hd", "hq", "mv"]
    filtered_title = " ".join([w for w in title.split() if w.lower() not in noise_words])

    # Build a broader discovery query
    query_parts = [
        artist,
        genre_str,
        "popular",
        "related songs",
        "new music",
        "recommended",
        filtered_title
    ]
    return " ".join([q for q in query_parts if q]).strip()

async def auto_feed(interaction: discord.Interaction, song_info: dict):
    state = get_state(interaction.guild.id)
    query = generate_feed_query(song_info)
    logging.info(f"[auto_feed] Discovery query: {query}")

    try:
        # Fetch a deeper pool of candidates
        candidates = await get_audio_info(
            query,
            state.bitrate_mode,
            exclude_url=song_info["url"],
            max_results=10
        )
        if isinstance(candidates, dict):
            candidates = [candidates]

        rec = None
        for c in candidates:
            # skip anything already played or queued
            if is_duplicate(c, state.history + state.queue):
                logging.info(f"[auto_feed] Skipped duplicate (played/queued): {c['title']}")
                continue

            # skip same-channel repeats (optional)
            if state.history and c.get("channel") == state.history[-1].get("channel"):
                logging.info(f"[auto_feed] Skipped same-channel: {c['channel']}")
                continue

            rec = c
            logging.info(
                f"[auto_feed] Picked rec: {rec['title']} "
                f"({rec.get('view_count', 'N/A')} views) "
                f"from {rec.get('channel', 'unknown')}"
            )
            break

        if not rec:
            logging.warning(f"[auto_feed] No suitable new track found for query: {query}")
            return

        # Queue up the recommendation
        rec["search_query"] = query
        rec["url_fetched_at"] = time.time()
        state.queue.append(rec)

        # Notify via embed
        embed = discord.Embed(
            title="Auto-Queued",
            description=f"{rec['title']}\n*(based on {song_info['title']})*",
            color=0x1DB954
        )
        if thumb := rec.get("thumbnail"):
            embed.set_thumbnail(url=thumb)

        if state.autoqueue_message:
            await state.autoqueue_message.edit(embed=embed)
        else:
            state.autoqueue_message = await interaction.channel.send(embed=embed)

    except Exception as e:
        logging.error(f"[auto_feed] error: {e}")
        await interaction.channel.send(f"Feed error: {e}")

async def get_audio_info(
    query: str,
    bitrate_mode: str = "default",
    exclude_url: str = None,
    max_results: int = 1
) -> dict | list[dict]:

    bitrate_map = {"default": 160, "low": 96}
    kbps = bitrate_map.get(bitrate_mode, bitrate_map["default"])

    ydl_opts = {
        "format": f"bestaudio[abr<={kbps}]/bestaudio",
        "quiet": True,
        "noplaylist": True,
        "default_search": "ytsearch",
        "skip_download": True,
    }

    search_term = f"ytsearch{max_results}:{query}" if max_results > 1 else query

    def _extract(arg):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(arg, download=False)

    # 4) Run yt-dlp off the main thread
    info = await asyncio.to_thread(_extract, search_term)

    # 5) Normalize into a flat list of entries
    if max_results > 1 and "entries" in info:
        entries = [e for e in info["entries"] if e]
    else:
        # single result comes back either as info or info["entries"][0]
        if "entries" in info and info["entries"]:
            entries = [info["entries"][0]]
        else:
            entries = [info]

    # 6) Exclude the previous track if requested
    def page_url(e):
        return e.get("webpage_url") or e.get("url")
    if exclude_url:
        entries = [e for e in entries if page_url(e) != exclude_url]

    # 7) Sort by “official” channel boost then view_count descending
    entries.sort(
        key=lambda e: (
            (
                "official" in (e.get("channel") or "").lower()
                or "vevo" in (e.get("channel") or "").lower()
                or "topic" in (e.get("channel") or "").lower()
            ),
            e.get("view_count") or 0
        ),
        reverse=True
    )

    # 8) Build the final payload(s)
    out = []
    for e in entries[:max_results]:
        out.append({
            "title": e.get("title"),
            "url": page_url(e),
            # Because we set format to bestaudio, info["url"] is the direct stream link
            "stream_url": e.get("url"),
            "duration": e.get("duration"),
            "thumbnail": e.get("thumbnail"),
            "view_count": e.get("view_count"),
            "channel": e.get("channel"),
        })

    # 9) Return a single dict when max_results == 1
    return out[0] if max_results == 1 else out

# ─── Playback & Auto-Feed ─────────────────────────────────────────────────────

async def play_next(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client

    # 1️⃣ Ensure we're connected
    if not vc or not vc.is_connected():
        return await interaction.channel.send("Not connected to a voice channel.")

    # 2️⃣ Stop any current playback
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    # 3️⃣ Populate queue if empty and autoqueue is on
    if not state.queue:
        if getattr(state, "autoqueue_enabled", False) and state.history:
            await auto_feed(interaction, state.history[-1])
        if not state.queue:
            return await interaction.channel.send("Queue is empty.")

    # 4️⃣ Pop the next song & append to history
    song = state.queue.pop(0)
    state.history.append(song)

    # 5️⃣ Handle loop modes
    if state.loop_mode == "one":
        state.queue.insert(0, song)
    elif state.loop_mode == "all":
        state.queue.append(song)

    # 6️⃣ Check if URL is stale (older than 15m) or missing
    needs_refresh = (
        not song.get("url")
        or not song.get("url_fetched_at")
        or (time.time() - song["url_fetched_at"] > 900)
    )

    if needs_refresh:
        logging.info(f"[play_next] Refreshing URL for: {song['title']}")
        try:
            search_term = song.get("search_query", song["title"])
            refreshed = await get_audio_info(search_term, state.bitrate_mode)
            song["url"] = refreshed["url"]
            song["stream_url"] = refreshed.get("stream_url", refreshed["url"])
            song["url_fetched_at"] = time.time()
        except Exception as e:
            logging.error(f"[play_next] URL refresh failed: {e}")
            return await interaction.channel.send(f"Error refreshing stream for {song['title']}.")
    else:
        age = round(time.time() - song["url_fetched_at"], 1)
        logging.info(f"[play_next] Using cached URL for: {song['title']} (age: {age}s)")

    # ─── DEBUG: inspect what's in song before probing ───────────────────
    logging.info(f"[play_next-debug] song keys: {list(song.keys())}")
    logging.info(f"[play_next-debug] stream_url: {song.get('stream_url')}")

    # 7️⃣ Build our audio source from the direct stream_url (or fallback to page URL)
    audio_source = song.get("stream_url") or song["url"]
    source = await discord.FFmpegOpusAudio.from_probe(
        audio_source,
        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        options="-vn"
    )

    # 8️⃣ Schedule the next track when this one ends
    def _after_play(err):
        if err:
            logging.error(f"[play_next] playback error: {err}")
        fut = asyncio.run_coroutine_threadsafe(play_next(interaction), interaction.client.loop)
        try:
            fut.result()
        except Exception as ex:
            logging.error(f"[play_next] after_play callback error: {ex}")

    vc.play(source, after=_after_play)
    state.paused = False

    # 9️⃣ Send or update the Now Playing embed with controls
    embed = discord.Embed(title="Now Playing", description=song["title"], color=0x1DB954)
    if thumb := song.get("thumbnail"):
        embed.set_thumbnail(url=thumb)

    controls = PlaybackControls(interaction.guild.id)
    if state.now_playing_message:
        await state.now_playing_message.edit(embed=embed, view=controls)
    else:
        state.now_playing_message = await interaction.channel.send(embed=embed, view=controls)

    # 🔟 Trigger auto-feed for the next recommendation
    if getattr(state, "autoqueue_enabled", False):
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

class PlaybackControls(discord.ui.View):
    def __init__(self, interaction: discord.Interaction):
        super().__init__(timeout=None)  # persistent
        self.interaction = interaction

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.grey)
    async def rewind(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        if not state.history:
            return await interaction.response.send_message("No song to rewind.", ephemeral=True)
        current_song = state.history[-1]
        state.queue.insert(0, current_song)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
        await interaction.response.send_message(f"Rewinding: {current_song['title']}", ephemeral=True)

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.grey)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        state = get_state(interaction.guild.id)
        if not vc:
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        if vc.is_playing():
            vc.pause()
            state.paused = True
            await interaction.response.send_message("Paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            state.paused = False
            await interaction.response.send_message("Resumed.", ephemeral=True)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.grey)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        vc.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        state = get_state(interaction.guild.id)
        if vc:
            vc.stop()
        state.queue.clear()
        await interaction.response.send_message("Stopped and cleared queue.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.grey)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        modes = ["off", "one", "all"]
        state.loop_mode = modes[(modes.index(state.loop_mode) + 1) % len(modes)]
        await interaction.response.send_message(f"Loop mode: `{state.loop_mode}`", ephemeral=True)

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

@bot.tree.command(name="leave", description="Disconnect the bot from voice")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        # always send *some* response
        return await interaction.response.send_message(
            "I’m not in a voice channel right now.",
            ephemeral=True
        )

    await vc.disconnect()
    # optionally clear queue/history here:
    state = get_state(interaction.guild.id)
    state.queue.clear()
    state.history.clear()

    await interaction.response.send_message(
        "Left the voice channel and cleared the queue.",
        ephemeral=True
    )

async def resolve_spotify_to_search(query: str) -> list[str]:
    """
    If query is a Spotify track/album/playlist URL,
    use the Web API to get title+artist and return search terms.
    Otherwise fall back to yt-dlp for everything else.
    """
    # 1) Spotify track
    if (m := SPOTIFY_TRACK_RE.search(query)):
        sp_id = m.group(1)
        data = _sp.track(sp_id)
        return [f"{data['name']} {data['artists'][0]['name']}"]

    # 2) Spotify album
    if (m := SPOTIFY_ALBUM_RE.search(query)):
        sp_id = m.group(1)
        items = _sp.album_tracks(sp_id)["items"]
        return [f"{t['name']} {t['artists'][0]['name']}" for t in items]

    # 3) Spotify playlist
    if (m := SPOTIFY_PLAYLIST_RE.search(query)):
        sp_id = m.group(1)
        items = _sp.playlist_tracks(sp_id)["items"]
        return [f"{i['track']['name']} {i['track']['artists'][0]['name']}" for i in items]

    # 4) Everything else → yt-dlp  
    def _extract():
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            return ydl.extract_info(query, download=False)

    info = await asyncio.to_thread(_extract)

    # multi‐video case (YouTube playlist/multi search)
    if info.get("_type") in ("playlist", "multi_video") and info.get("entries"):
        return [
            f"{e['title']} {e.get('uploader','')}".strip()
            for e in info["entries"] if e
        ]

    # single‐track fallback
    return [f"{info['title']} {info.get('uploader','')}".strip()]

@bot.tree.command(name="play", description="Play a song by search, YouTube, or Spotify URL")
@app_commands.describe(query="Search terms, YouTube URL, or Spotify URL")
async def play(interaction: discord.Interaction, query: str):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message(
            "Not connected. Use `/join` first.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    # Detect Spotify URL
    if SPOTIFY_TRACK_RE.search(query) or SPOTIFY_PLAYLIST_RE.search(query) or SPOTIFY_ALBUM_RE.search(query):
        search_terms = await resolve_spotify_to_search(query)
        if not search_terms:
            return await interaction.followup.send("Could not resolve Spotify link.", ephemeral=True)

        # Playlist or album → queue all tracks
        if len(search_terms) > 1:
            for term in search_terms:
                track_info = await get_audio_info(term, state.bitrate_mode)
                track_info["search_query"] = term
                track_info["url_fetched_at"] = time.time()
                state.queue.append(track_info)
            await interaction.followup.send(f"Queued {len(search_terms)} tracks from Spotify.", ephemeral=True)
            if not vc.is_playing():
                await play_next(interaction)
            return

        # Single track → replace query with resolved search term
        query = search_terms[0]

    try:
        info = await get_audio_info(query, state.bitrate_mode)
        info["search_query"] = query
        info["url_fetched_at"] = time.time()

        logging.info(f"[/play] URL fetched for: {info['title']} at {info['url_fetched_at']}")

        embed = discord.Embed(
            title="Confirm Playback",
            description=info["title"],
            color=0x1DB954
        )
        if info["thumbnail"]:
            embed.set_thumbnail(url=info["thumbnail"])
        embed.set_footer(text="Click to confirm or cancel.")

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

# ─── Music Control Commands ────────────────────────────────────────────────

@bot.tree.command(name="clearqueue", description="Clear all pending songs from the queue")
async def clearqueue(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    pending = len(state.queue)
    state.queue.clear()
    await interaction.response.send_message(
        f"Cleared {pending} song{'s' if pending != 1 else ''} from the queue.",
        ephemeral=True
    )

@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    vc.pause()
    get_state(interaction.guild.id).paused = True
    await interaction.response.send_message("Paused.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        return await interaction.response.send_message("Nothing is paused.", ephemeral=True)
    vc.resume()
    get_state(interaction.guild.id).paused = False
    await interaction.response.send_message("▶Resumed.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    state = get_state(interaction.guild.id)
    state.queue.clear()
    await interaction.response.send_message("Stopped and cleared the queue.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    vc.stop()  # triggers play_next()
    await interaction.response.send_message("⏭ Skipped.", ephemeral=True)


@bot.tree.command(name="rewind", description="Replay the current song from the start")
async def rewind(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if not state.history:
        return await interaction.response.send_message("No song to rewind.", ephemeral=True)
    current_song = state.history[-1]
    state.queue.insert(0, current_song)  # put it back at the front
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
    await interaction.response.send_message(f"⏮ Rewinding: {current_song['title']}", ephemeral=True)


@bot.tree.command(name="loop", description="Set loop mode")
@app_commands.describe(mode="Loop mode: off, one, or all")
@app_commands.choices(mode=[
    Choice(name="off", value="off"),
    Choice(name="one", value="one"),
    Choice(name="all", value="all")
])
async def loop(interaction: discord.Interaction, mode: str):
    state = get_state(interaction.guild.id)
    state.loop_mode = mode
    await interaction.response.send_message(f"Loop mode set to `{mode}`.", ephemeral=True)

# ─── Startup & Command Sync ───────────────────────────────────────────────────
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user}")
    await bot.tree.sync()
    logging.info("Slash commands synced.")

bot.run(TOKEN)