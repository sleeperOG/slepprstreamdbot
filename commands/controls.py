from discord import app_commands
import discord
from core.state import get_state

def setup_control_commands(bot):
    @bot.tree.command(name="pause", description="Pause the current song")
    async def pause(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        vc.pause()
        get_state(interaction.guild.id).paused = True
        await interaction.response.send_message("⏸ Paused.", ephemeral=True)

    @bot.tree.command(name="resume", description="Resume playback")
    async def resume(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            return await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        vc.resume()
        get_state(interaction.guild.id).paused = False
        await interaction.response.send_message("▶️ Resumed.", ephemeral=True)

    @bot.tree.command(name="skip", description="Skip the current song")
    async def skip(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        vc.stop()
        await interaction.response.send_message("⏭ Skipped.", ephemeral=True)

    @bot.tree.command(name="stop", description="Stop playback and clear the queue")
    async def stop(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        state = get_state(interaction.guild.id)
        state.queue.clear()
        await interaction.response.send_message("⏹ Stopped and cleared the queue.", ephemeral=True)