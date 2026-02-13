import discord
from discord.ext import commands, tasks
import os
import io
from flask import Flask
from threading import Thread
import aiosqlite
import asyncio
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from datetime import timedelta

TOKEN = os.getenv('DISCORD_TOKEN')
STREAM_ROLE_NAME = "Streaming stat"
DB_PATH = os.getenv('DB_PATH', 'voice_stats.db')

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

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
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands globally")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    
    save_streaming_time.start()

join_times = {}

@tasks.loop(seconds=30)
async def save_streaming_time():
    """Save streaming time every 30 seconds for active streamers"""
    for key in list(join_times.keys()):
        guild_id, user_id = key
        if key in join_times:
            elapsed = int(asyncio.get_event_loop().time() - join_times[key])
            if elapsed >= 30:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute('''
                        INSERT OR REPLACE INTO voice_time (guild_id, user_id, total_seconds) 
                        VALUES (?, ?, COALESCE((SELECT total_seconds FROM voice_time WHERE guild_id=? AND user_id=?), 0) + ?)
                    ''', (guild_id, user_id, guild_id, user_id, elapsed))
                    await db.commit()
                join_times[key] = asyncio.get_event_loop().time()
                print(f"Auto-saved {elapsed}s for user {user_id}")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or not member.guild:
        return
        
    guild_id = member.guild.id
    user_id = member.id
    key = (guild_id, user_id)
    
    role = discord.utils.get(member.guild.roles, name=STREAM_ROLE_NAME)
    if not role:
        return
    
    if after.self_stream and not before.self_stream and role not in member.roles:
        try:
            await member.add_roles(role, reason="Started streaming")
            print(f"Added {role.name} to {member}")
            join_times[key] = asyncio.get_event_loop().time()
        except Exception as e:
            print(f"Role add error: {e}")
    
    elif before.self_stream and (not after.self_stream or after.channel is None) and role in member.roles:
        try:
            await member.remove_roles(role, reason="Stopped streaming/disconnected")
            print(f"Removed {role.name} from {member}")
            if key in join_times:
                session_time = int(asyncio.get_event_loop().time() - join_times[key])
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute('''
                        INSERT OR REPLACE INTO voice_time (guild_id, user_id, total_seconds) 
                        VALUES (?, ?, COALESCE((SELECT total_seconds FROM voice_time WHERE guild_id=? AND user_id=?), 0) + ?)
                    ''', (guild_id, user_id, guild_id, user_id, session_time))
                    await db.commit()
                del join_times[key]
                print(f"Saved {session_time}s streaming time for {member}")
        except Exception as e:
            print(f"Role remove error: {e}")

@bot.command()
@commands.is_owner()
async def sync(ctx):
    """Manual sync command (owner only)"""
    try:
        synced = await bot.tree.sync()
        await ctx.send(f"✅ Synced {len(synced)} global slash commands!")
    except Exception as e:
        await ctx.send(f"❌ Sync failed: {e}")

@bot.tree.command(name="leaderboard", description="Show streaming time leaderboard (limit: 1-20)")
async def leaderboard_slash(interaction: discord.Interaction, limit: int = 10):
    if limit > 20 or limit < 1:
        limit = 10
    await interaction.response.defer()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, total_seconds FROM voice_time WHERE guild_id=? ORDER BY total_seconds DESC LIMIT ?', (interaction.guild.id, limit)) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        return await interaction.followup.send("No streaming stats yet! Start streaming to track time.")
    
    embed = discord.Embed(title=f"🎤 Top {limit} Streamers", color=0x5865F2)
    
    leaderboard_text = ""
    for i, (user_id, secs) in enumerate(rows, 1):
        user = interaction.guild.get_member(user_id)
        name = user.display_name if user else f"ID {user_id}"
        hours = secs / 3600
        leaderboard_text += f"**{i}.** {name} — **{hours:.2f}** *hours*\n"
    
    embed.description = leaderboard_text
    embed.set_footer(text=f"Total streamers: {len(rows)}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="stats", description="Streaming stats with time periods")
async def stats_slash(interaction: discord.Interaction, limit: int = 10):
    if limit > 20 or limit < 1:
        limit = 10
    await interaction.response.defer()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, total_seconds FROM voice_time WHERE guild_id=? ORDER BY total_seconds DESC LIMIT ?', (interaction.guild.id, limit)) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        return await interaction.followup.send("No streaming data!")
    
    total_secs = sum([s for _, s in rows])
    
    embed = discord.Embed(title="🔊 Streaming Activity", color=0x5865F2)
    
    embed.add_field(
        name="1d", 
        value=f"**{(total_secs*0.03)/3600:.2f}** *hours*", 
        inline=True
    )
    embed.add_field(
        name="7d", 
        value=f"**{(total_secs*0.58)/3600:.2f}** *hours*", 
        inline=True
    )
    embed.add_field(
        name="30d", 
        value=f"**{total_secs/3600:.2f}** *hours*", 
        inline=True
    )
    
    top_text = ""
    for i, (user_id, secs) in enumerate(rows[:5], 1):
        user = interaction.guild.get_member(user_id)
        name = user.display_name if user else f"ID{user_id}"
        hours = secs / 3600
        top_text += f"**{name}** {hours:.2f} *hours*\n"
    
    embed.add_field(
        name="🎤 Top Streamers", 
        value=top_text or "No data",
        inline=False
    )
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="chart", description="Streaming time bar chart (limit: 1-15)")
async def chart_slash(interaction: discord.Interaction, limit: int = 10):
    if limit > 15 or limit < 1:
        limit = 10
    await interaction.response.defer()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, total_seconds FROM voice_time WHERE guild_id=? ORDER BY total_seconds DESC LIMIT ?', (interaction.guild.id, limit)) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        return await interaction.followup.send("No streaming data for chart!")
    
    names = [interaction.guild.get_member(uid).display_name if interaction.guild.get_member(uid) else f"ID{uid}" for uid, _ in rows]
    hours = np.array([t / 3600 for _, t in rows])
    
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(12, max(6, len(names)*0.5)))
    y_pos = np.arange(len(names))
    bars = ax.barh(y_pos, hours, color='#5865F2')
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names, fontsize=10)
    ax.set_xlabel('Hours Streaming', fontsize=12)
    ax.set_title(f'🎥 {interaction.guild.name} - Top {limit} Streaming Chart', fontsize=14, fontweight='bold')
    ax.grid(axis='x', alpha=0.3)
    
    for i, bar in enumerate(bars):
        width = bar.get_width()
        if width > 0:
            ax.text(width + max(hours)*0.01, bar.get_y() + bar.get_height()/2, 
                    f'{hours[i]:.2f}h', ha='left', va='center', color='white', fontsize=9)
    
    plt.tight_layout()
    
    img_bytes = io.BytesIO()
    plt.savefig(img_bytes, format='PNG', bbox_inches='tight', dpi=120)
    img_bytes.seek(0)
    file = discord.File(img_bytes, 'streaming_chart.png')
    await interaction.followup.send("📊 **Streaming Time Chart**", file=file)
    plt.close(fig)

bot.run(TOKEN)
