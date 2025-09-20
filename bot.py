import discord
from discord.ext import commands
import logging
from config import TOKEN
from commands.play import setup_play_commands
from commands.controls import setup_control_commands
from commands.voice import setup_voice_commands

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user}")
    await bot.tree.sync()

setup_play_commands(bot)
setup_control_commands(bot)
setup_voice_commands(bot)

bot.run(TOKEN)