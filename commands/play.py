import discord
from discord import app_commands
import time
from core.audio import get_audio_info
from utils.spotify import resolve_spotify_to_search
from views.confirm import ConfirmView
from core.state import get_state
from core.feed import play_next

def setup_play_commands(bot):
    @bot.tree.command(name="play", description="Play a song by search, YouTube, or Spotify URL")
    @app_commands.describe(query="Search terms, YouTube URL, or Spotify URL")
    async def play(interaction: discord.Interaction, query: str):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("Not connected. Use `/join` first.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        if "spotify.com" in query:
            search_terms = await resolve_spotify_to_search(query)
            if not search_terms:
                return await interaction.followup.send("Could not resolve Spotify link.", ephemeral=True)

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

            query = search_terms[0]

        info = await get_audio_info(query, state.bitrate_mode)
        info["search_query"] = query
        info["url_fetched_at"] = time.time()

        embed = discord.Embed(
            title="Confirm Playback",
            description=info["title"],
            color=0x1DB954
        )
        if info.get("thumbnail"):
            embed.set_thumbnail(url=info["thumbnail"])
        embed.set_footer(text="Click to confirm or cancel.")

        await interaction.followup.send(
            embed=embed,
            view=ConfirmView(info, interaction),
            ephemeral=True
        )