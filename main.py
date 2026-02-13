import discord
from discord.ext import commands
import os
import io
from flask import Flask
from threading import Thread
import aiosqlite
import asyncio
import matplotlib.pyplot as plt
import numpy as np
from datetime import timedelta

TOKEN = os.getenv('DISCORD_TOKEN')
STREAM_ROLE_NAME = "Streaming stat"
DB_PATH = os.getenv('DB_PATH', 'voice_stats.db')

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True  # For slash commands

bot = commands.Bot(command_prefix="!", intents=intents)

app = Flask('')

@app.route('/')
def home():
    return "Bot alive!"

def run_flask():
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS voice_time (
                guild_id INTEGER,
                user_id INTEGER,
                total_seconds INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')
        await db.commit()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await init_db()
    Thread(target=run_flask, daemon=True).start()
    print("Slash commands ready!")

join_times = {}

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or not member.guild:
        return
        
    guild_id = member.guild.id
    user_id = member.id
    key = (guild_id, user_id)
    
    # Voice tracking
    if before.channel is None and after.channel:
        join_times[key] = asyncio.get_event_loop().time()
    elif before.channel and after.channel is None:
        if key in join_times:
            session_time = int(asyncio.get_event_loop().time() - join_times[key])
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute('''
                    INSERT OR REPLACE INTO voice_time (guild_id, user_id, total_seconds) 
                    VALUES (?, ?, COALESCE((SELECT total_seconds FROM voice_time WHERE guild_id=? AND user_id=?), 0) + ?)
                ''', (guild_id, user_id, guild_id, user_id, session_time))
                await db.commit()
            del join_times[key]
    
    # Streaming role
    role = discord.utils.get(member.guild.roles, name=STREAM_ROLE_NAME)
    if role:
        if after.self_stream and not before.self_stream and role not in member.roles:
            try:
                await member.add_roles(role, reason="Started streaming")
            except:
                pass
        elif before.self_stream and (not after.self_stream or after.channel is None) and role in member.roles:
            try:
                await member.remove_roles(role, reason="Stopped streaming/disconnected")
            except:
                pass

@bot.tree.command(name="leaderboard", description="Show voice activity top 10")
@app.describe(limit="Top N (default 10)")
async def leaderboard_slash(interaction: discord.Interaction, limit: int = 10):
    await interaction.response.defer()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, total_seconds FROM voice_time WHERE guild_id=? ORDER BY total_seconds DESC LIMIT ?', (interaction.guild.id, limit)) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        return await interaction.followup.send("No voice stats yet!")
    
    embed = discord.Embed(title=f"🎤 Top {limit} Voice Activity", color=0x00ff00)
    for i, (user_id, secs) in enumerate(rows, 1):
        user = interaction.guild.get_member(user_id)
        name = user.display_name if user else f"ID {user_id}"
        embed.add_field(name=f"{i}. {name}", value=str(timedelta(seconds=secs)), inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="chart", description="Voice activity bar chart")
@app.describe(limit="Top N (default 10)")
async def chart_slash(interaction: discord.Interaction, limit: int = 10):
    await interaction.response.defer()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, total_seconds FROM voice_time WHERE guild_id=? ORDER BY total_seconds DESC LIMIT ?', (interaction.guild.id, limit)) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        return await interaction.followup.send("No data for chart!")
    
    names = [interaction.guild.get_member(uid).display_name if interaction.guild.get_member(uid) else f"ID{uid}" for uid, _ in rows]
    hours = [t / 3600 for _, t in rows]
    
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, max(6, len(names)*0.5)))
    y_pos = np.arange(len(names))
    ax.barh(y_pos, hours, color='skyblue')
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names)
    ax.set_xlabel('Hours in Voice')
    ax.set_title(f'Top {limit} Voice Leaderboard')
    plt.tight_layout()
    
    img_bytes = io.BytesIO()
    plt.savefig(img_bytes, format='PNG', bbox_inches='tight')
    img_bytes.seek(0)
    file = discord.File(img_bytes, 'leaderboard.png')
    await interaction.followup.send("📊 Voice Activity Chart:", file=file)
    plt.close()

@bot.event
async def on_guild_join(guild):
    # Sync slash commands on join
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    print(f"Synced {len(synced)} commands to {guild}")

bot.run(TOKEN)
