import discord
import yt_dlp
import asyncio
import os
from discord import Option

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = discord.Bot(intents=intents)

pending_tracks = {}
queue = []
history = []
loop_mode = "off"
bitrate_mode = "default"
autoqueue_enabled = False
now_playing_message = None
autoqueue_message = None

def generate_feed_query(info):
    base = info['artist'] or ""
    genre = info['genre'][0] if info['genre'] else ""
    title_keywords = " ".join(info['title'].split()[:3])
    return f"{base} {genre} similar {title_keywords}".strip()

def get_audio_info(query):
    global bitrate_mode
    format_filter = 'bestaudio'
    if bitrate_mode == "low":
        format_filter = 'bestaudio[ext=webm][abr<=160]'

    ydl_opts = {
        'format': format_filter,
        'quiet': True,
        'default_search': 'ytsearch',
        'noplaylist': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        entry = info['entries'][0] if 'entries' in info else info
        return {
            'url': entry['url'],
            'title': entry['title'],
            'thumbnail': entry.get('thumbnail'),
            'artist': entry.get('uploader'),
            'genre': entry.get('categories', []),
            'views': entry.get('view_count', 0)
        }

async def auto_feed(ctx, song_info):
    global autoqueue_message
    query = generate_feed_query(song_info)
    try:
        recommended = get_audio_info(query)
        queue.append(recommended)

        embed = discord.Embed(
            title="Auto-Queued",
            description=f"{recommended['title']}\n*(based on {song_info['title']})*",
            color=0x1DB954
        )
        if recommended['thumbnail']:
            embed.set_thumbnail(url=recommended['thumbnail'])

        if autoqueue_message:
            await autoqueue_message.edit(embed=embed)
        else:
            autoqueue_message = await ctx.respond(embed=embed)
    except Exception as e:
        await ctx.respond(f"Feed error: {str(e)}")

async def play_next(ctx):
    voice_client = ctx.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await ctx.respond("⚠️ I'm not connected to a voice channel.")
        return

    if voice_client.is_playing():
        voice_client.stop()

    try:
        source = discord.FFmpegPCMAudio(next_song['url'], **ffmpeg_options)
        voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        )
    except Exception as e:
        await ctx.respond(f"⚠️ Playback error: {str(e)}")
        return

class PlaybackControls(discord.ui.View):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.grey)
    async def rewind(self, button: discord.ui.Button, interaction: discord.Interaction):
        if history:
            last_song = history[-1]
            source = discord.FFmpegPCMAudio(last_song['url'], options='-vn')
            self.ctx.guild.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(self.ctx), bot.loop))
            await asyncio.sleep(1)
            await interaction.response.send_message(f"Replaying: {last_song['title']}", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.grey)
    async def skip(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.ctx.guild.voice_client.stop()
        await interaction.response.send_message("Skipped to next track ⏭️", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.grey)
    async def toggle_loop(self, button: discord.ui.Button, interaction: discord.Interaction):
        global loop_mode
        loop_mode = "one" if loop_mode == "off" else "off"
        await interaction.response.send_message(f"Loop mode: {loop_mode}", ephemeral=True)

@bot.slash_command(description="Check bot voice status")
async def status(ctx):
    vc = ctx.guild.voice_client
    if vc and vc.is_connected():
        await ctx.respond(f"Connected to: {vc.channel.name}")
    else:
        await ctx.respond("Not connected to any voice channel.")

@bot.slash_command(description="Force bot to leave and rejoin voice channel")
async def fixvoice(ctx):
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect()
        await asyncio.sleep(1)

    if ctx.author.voice:
        try:
            await ctx.author.voice.channel.connect()
            await ctx.respond("Reconnected to your voice channel.")
        except Exception as e:
            await ctx.respond(f"Failed to reconnect: {str(e)}")
    else:
        await ctx.respond("You're not in a voice channel.")

@bot.slash_command(description="Join your voice channel")
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        vc = ctx.guild.voice_client

        if vc and vc.is_connected():
            await ctx.respond("I'm already connected to a voice channel.")
            return

        try:
            await channel.connect()
            await ctx.respond("Joined your voice channel.")
        except discord.ClientException as e:
            await ctx.respond(f"Voice connection error: {str(e)}. Try `/leave` and then `/join` again.")
        except IndexError:
            await ctx.respond("Discord voice server returned invalid data. Try `/leave` and then `/join` again.")
        except Exception as e:
            await ctx.respond(f"Unexpected error: {str(e)}")
    else:
        await ctx.respond("You are not connected to a voice channel.")

@bot.slash_command(description="Play a song by search or link")
async def play(ctx, query: str):
    voice_client = ctx.guild.voice_client
    if not voice_client:
        await ctx.respond("I am not connected to a voice channel.")
        return

    await ctx.respond(f"Searching for: {query}")

    try:
        info = get_audio_info(query)
        pending_tracks[ctx.author.id] = info

        embed = discord.Embed(title="Confirm Playback", description=info['title'], color=0x1DB954)
        if info['thumbnail']:
            embed.set_thumbnail(url=info['thumbnail'])
        embed.set_footer(text="Click a button to confirm.")

        view = ConfirmView(info, ctx)
        await ctx.send(embed=embed, view=view)
    except Exception as e:
        await ctx.respond(f"Error: {str(e)}")

class ConfirmView(discord.ui.View):
    def __init__(self, info, ctx):
        super().__init__()
        self.info = info
        self.ctx = ctx

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return

        queue.append(self.info)
        await interaction.response.send_message(f"Added to queue: {self.info['title']} 🎶")

        if not history:
            await interaction.followup.send("Want me to auto-queue similar tracks after each song? Type `/autoqueue` to enable.")

        if not self.ctx.guild.voice_client.is_playing():
            await play_next(self.ctx)

        new_embed = discord.Embed(title="Queued", description=self.info['title'], color=0x1DB954)
        if self.info['thumbnail']:
            new_embed.set_thumbnail(url=self.info['thumbnail'])
        await interaction.message.edit(embed=new_embed, view=None)

    @discord.ui.button(label="No", style=discord.ButtonStyle.red)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return

        await interaction.response.send_message("Playback cancelled")

@bot.slash_command(description="Enable auto-queue based on listening history")
async def autoqueue(ctx):
    global autoqueue_enabled
    autoqueue_enabled = True
    await ctx.respond("Auto-queue enabled. I’ll keep adding similar tracks after each song.")

@bot.slash_command(description="Set audio bitrate mode")
async def bitrate(ctx, mode: Option(str, choices=["default", "low"])):
    global bitrate_mode
    bitrate_mode = mode
    await ctx.respond(f"Bitrate mode set to: `{bitrate_mode}`")

@bot.slash_command(description="Leave the voice channel")
async def leave(ctx):
    await ctx.guild.voice_client.disconnect()
    await ctx.respond("Left the voice channel.")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.sync_commands()
    print("Slash commands synced")

print("Starting Sleppstream...")
bot.run(os.getenv("DISCORD_TOKEN"))