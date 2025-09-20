from discord import app_commands
import discord

def setup_voice_commands(bot):
    @bot.tree.command(name="join", description="Join your voice channel")
    async def join(interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("You're not in a voice channel.", ephemeral=True)
        await interaction.user.voice.channel.connect()
        await interaction.response.send_message("✅ Joined voice channel.", ephemeral=True)

    @bot.tree.command(name="leave", description="Leave the voice channel")
    async def leave(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("Not connected to voice.", ephemeral=True)
        await vc.disconnect()
        await interaction.response.send_message("👋 Left voice channel.", ephemeral=True)