import discord
from discord.ext import commands, tasks
import os
import io
from flask import Flask
from threading import Thread
import asyncpg
import asyncio
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

TOKEN = os.getenv('DISCORD_TOKEN')
STREAM_ROLE_NAME = "Streaming stat"
DATABASE_URL = os.getenv('DATABASE_URL')

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

def keep_alive():
    """Start Flask server in background thread"""
    t = Thread(target=run_flask, daemon=True)
    t.start()

# Role hierarchy (order matters - higher index = higher rank)
ROLE_HIERARCHY = [
    ("VC Rookie", None),
    ("VC Raider", 50),
    ("VC Challenger", 40),
    ("VC Elite", 30),
    ("VC Legend", 20),
    ("VC Top Contender", 10),
    ("VC Finalist", 5),
    ("VC Champ", 3),
    ("VC MVP", 2),
    ("Apex Speaker", 1)
]

# Database connection pool
db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS voice_time (
                guild_id BIGINT,
                user_id BIGINT,
                total_seconds INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')
    print("✅ Database initialized")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await init_db()
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands globally")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    
    save_streaming_time.start()
    auto_update_vc_roles.start()
    print("🔄 Auto-save (30s) and VC role update (5min) started")
    print("✅ Stream LB Bot ready with dual-condition tracking (streaming + role)")

join_times = {}

@bot.event
async def on_voice_state_update(member, before, after):
    """Track streaming time ONLY when: 1) Actually streaming AND 2) Has 'Streaming stat' role"""
    if member.bot or not member.guild:
        return
    
    guild_id = member.guild.id
    user_id = member.id
    key = (guild_id, user_id)
    
    # Get the streaming stat role
    stream_role = discord.utils.get(member.guild.roles, name=STREAM_ROLE_NAME)
    if not stream_role:
        return
    
    # Check both conditions NOW
    is_streaming_now = after.channel is not None and after.self_stream
    has_role_now = stream_role in member.roles
    
    # Check both conditions BEFORE
    was_streaming_before = before.channel is not None and before.self_stream
    had_role_before = stream_role in member.roles
    
    # Both conditions to START tracking: streaming + has role
    should_track_now = is_streaming_now and has_role_now
    was_tracking_before = was_streaming_before and had_role_before
    
    # START tracking: Both conditions met now, wasn't tracking before
    if should_track_now and not was_tracking_before and key not in join_times:
        join_times[key] = asyncio.get_event_loop().time()
        print(f"▶️ Started tracking {member.name} (streaming + has role in {after.channel.name})")
    
    # STOP tracking: Either condition lost (stopped streaming OR role removed)
    elif was_tracking_before and not should_track_now and key in join_times:
        session_time = int(asyncio.get_event_loop().time() - join_times[key])
        
        # Only save if more than 5 seconds (avoid false triggers)
        if session_time >= 5:
            print(f"💾 Saving {session_time}s for {member.name}...")
            
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute('''
                        INSERT INTO voice_time (guild_id, user_id, total_seconds) 
                        VALUES ($1, $2, $3)
                        ON CONFLICT (guild_id, user_id) 
                        DO UPDATE SET total_seconds = voice_time.total_seconds + $3
                    ''', guild_id, user_id, session_time)
                print(f"✅ Successfully saved {session_time}s for {member.name}")
            except Exception as e:
                print(f"❌ Database save error for {member.name}: {e}")
        else:
            print(f"⏭️ Skipped {session_time}s for {member.name} (too short)")
        
        del join_times[key]
        reason = "stopped streaming" if not is_streaming_now else "role removed"
        print(f"⏹️ Stopped tracking {member.name} ({reason})")

@tasks.loop(seconds=30)
async def save_streaming_time():
    """Save streaming time every 30 seconds - ONLY for users actively streaming WITH role"""
    for key in list(join_times.keys()):
        guild_id, user_id = key
        if key not in join_times:
            continue
            
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        
        member = guild.get_member(user_id)
        if not member:
            continue
        
        # Get the streaming stat role
        stream_role = discord.utils.get(guild.roles, name=STREAM_ROLE_NAME)
        if not stream_role:
            continue
        
        # Check BOTH conditions: streaming + has role
        is_streaming = member.voice and member.voice.self_stream
        has_role = stream_role in member.roles
        
        # If either condition is lost, save final time and stop tracking
        if not is_streaming or not has_role:
            session_time = int(asyncio.get_event_loop().time() - join_times[key])
            if session_time >= 5:
                async with db_pool.acquire() as conn:
                    await conn.execute('''
                        INSERT INTO voice_time (guild_id, user_id, total_seconds) 
                        VALUES ($1, $2, $3)
                        ON CONFLICT (guild_id, user_id) 
                        DO UPDATE SET total_seconds = voice_time.total_seconds + $3
                    ''', guild_id, user_id, session_time)
                reason = "not streaming" if not is_streaming else "role removed"
                print(f"💾 Final save {session_time}s for {member.name} ({reason})")
            del join_times[key]
            continue
        
        # Both conditions still met - save periodic progress
        elapsed = int(asyncio.get_event_loop().time() - join_times[key])
        if elapsed >= 30:
            async with db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO voice_time (guild_id, user_id, total_seconds) 
                    VALUES ($1, $2, $3)
                    ON CONFLICT (guild_id, user_id) 
                    DO UPDATE SET total_seconds = voice_time.total_seconds + $3
                ''', guild_id, user_id, elapsed)
            join_times[key] = asyncio.get_event_loop().time()
            print(f"💾 Auto-saved {elapsed}s for {member.name}")

@tasks.loop(minutes=5)
async def auto_update_vc_roles():
    """Auto-update VC rank roles every 5 minutes"""
    await bot.wait_until_ready()
    
    for guild in bot.guilds:
        try:
            await update_guild_vc_roles(guild)
            print(f"✅ Auto-updated VC roles for {guild.name}")
            
            if len(bot.guilds) > 1:
                await asyncio.sleep(2)
                
        except discord.HTTPException as e:
            if e.status == 429:
                print(f"⚠️ Rate limited for {guild.name}, waiting 60s...")
                await asyncio.sleep(60)
            else:
                print(f"❌ HTTP Error for {guild.name}: {e}")
        except Exception as e:
            print(f"❌ Error updating {guild.name}: {e}")

async def update_guild_vc_roles(guild):
    """Update VC roles for a specific guild"""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT user_id, total_seconds FROM voice_time WHERE guild_id=$1 ORDER BY total_seconds DESC',
                guild.id
            )
    except Exception as e:
        print(f"❌ Database error for {guild.name}: {e}")
        return
    
    if not rows:
        print(f"⚠️ No streaming data for {guild.name}")
        return
    
    guild_roles = {role.name: role for role in guild.roles}
    vc_role_names = [name for name, _ in ROLE_HIERARCHY]
    
    missing_roles = [name for name in vc_role_names if name not in guild_roles]
    if missing_roles:
        print(f"⚠️ {guild.name} missing roles: {', '.join(missing_roles)}")
        return
    
    updated_count = 0
    
    for rank, row in enumerate(rows, 1):
        member = guild.get_member(row['user_id'])
        if not member:
            continue
        
        target_role = None
        for role_name, threshold in reversed(ROLE_HIERARCHY):
            if threshold is None or rank <= threshold:
                target_role = guild_roles[role_name]
                break
        
        current_vc_roles = [r for r in member.roles if r.name in vc_role_names]
        
        if len(current_vc_roles) == 1 and current_vc_roles[0] == target_role:
            continue
        
        try:
            if current_vc_roles:
                await member.remove_roles(*current_vc_roles, reason="VC rank update")
            
            if target_role:
                await member.add_roles(target_role, reason=f"Rank #{rank}")
                updated_count += 1
                
        except discord.Forbidden:
            print(f"⚠️ No permission to update {member.name} in {guild.name}")
        except Exception as e:
            print(f"❌ Error updating {member.name}: {e}")
    
    if updated_count > 0:
        print(f"🔄 Updated {updated_count} members in {guild.name}")

@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    """Force sync slash commands (Administrator only)"""
    try:
        synced = await bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"✅ Synced {len(synced)} commands to this server!")
        
        synced_global = await bot.tree.sync()
        await ctx.send(f"✅ Also synced {len(synced_global)} commands globally (may take 1 hour)")
    except Exception as e:
        await ctx.send(f"❌ Sync failed: {e}")

@sync.error
async def sync_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need Administrator permission to use this command!")

@bot.tree.command(name="leaderboard", description="Show streaming time leaderboard (limit: 1-20)")
async def leaderboard_slash(interaction: discord.Interaction, limit: int = 10):
    if limit > 20 or limit < 1:
        limit = 10
    await interaction.response.defer()
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT user_id, total_seconds FROM voice_time WHERE guild_id=$1 ORDER BY total_seconds DESC LIMIT $2',
            interaction.guild.id, limit
        )
    
    if not rows:
        return await interaction.followup.send("No streaming stats yet! Start streaming to track time.")
    
    embed = discord.Embed(title=f"🎤 Top {limit} Streamers", color=0x5865F2)
    
    leaderboard_text = ""
    for i, row in enumerate(rows, 1):
        user = interaction.guild.get_member(row['user_id'])
        name = user.display_name if user else f"ID {row['user_id']}"
        hours = row['total_seconds'] / 3600
        leaderboard_text += f"**{i}.** {name} — **{hours:.2f}** *hours*\n"
    
    embed.description = leaderboard_text
    embed.set_footer(text=f"Total streamers: {len(rows)}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="stats", description="Streaming stats with time periods")
async def stats_slash(interaction: discord.Interaction, limit: int = 10):
    if limit > 20 or limit < 1:
        limit = 10
    await interaction.response.defer()
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT user_id, total_seconds FROM voice_time WHERE guild_id=$1 ORDER BY total_seconds DESC LIMIT $2',
            interaction.guild.id, limit
        )
    
    if not rows:
        return await interaction.followup.send("No streaming data!")
    
    total_secs = sum([row['total_seconds'] for row in rows])
    
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
    for i, row in enumerate(rows[:5], 1):
        user = interaction.guild.get_member(row['user_id'])
        name = user.display_name if user else f"ID{row['user_id']}"
        hours = row['total_seconds'] / 3600
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
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT user_id, total_seconds FROM voice_time WHERE guild_id=$1 ORDER BY total_seconds DESC LIMIT $2',
            interaction.guild.id, limit
        )
    
    if not rows:
        return await interaction.followup.send("No streaming data for chart!")
    
    usernames = []
    for row in rows:
        user = interaction.guild.get_member(row['user_id'])
        if user:
            usernames.append(user.name)
        else:
            usernames.append(f"ID{row['user_id']}")
    
    hours = np.array([row['total_seconds'] / 3600 for row in rows])
    
    plt.rcParams['font.family'] = 'DejaVu Sans'
    plt.rcParams['axes.unicode_minus'] = False
    
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(14, max(7, len(usernames)*0.6)))
    y_pos = np.arange(len(usernames))
    bars = ax.barh(y_pos, hours, color='#5865F2', edgecolor='white', linewidth=0.5)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(usernames, fontsize=11, family='monospace')
    ax.set_xlabel('Hours Streaming', fontsize=13, fontweight='bold')
    ax.set_title(f'🎥 {interaction.guild.name} - Top {limit} Streaming Chart', 
                 fontsize=16, fontweight='bold', pad=20)
    ax.grid(axis='x', alpha=0.25, linestyle='--')
    ax.set_facecolor('#2C2F33')
    fig.patch.set_facecolor('#23272A')
    
    for i, bar in enumerate(bars):
        width = bar.get_width()
        if width > 0:
            ax.text(width + max(hours)*0.015, bar.get_y() + bar.get_height()/2, 
                    f'{hours[i]:.2f}h', ha='left', va='center', 
                    color='white', fontsize=10, fontweight='bold')
    
    plt.tight_layout()
    
    img_bytes = io.BytesIO()
    plt.savefig(img_bytes, format='PNG', bbox_inches='tight', dpi=130)
    img_bytes.seek(0)
    file = discord.File(img_bytes, 'streaming_chart.png')
    await interaction.followup.send("📊 **Streaming Time Chart**", file=file)
    plt.close(fig)

@bot.tree.command(name="updateroles", description="Manually update VC rank roles (Admin only)")
@discord.app_commands.checks.has_permissions(administrator=True)
async def updateroles_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    
    try:
        await update_guild_vc_roles(interaction.guild)
        await interaction.followup.send("✅ VC roles updated successfully!")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)[:100]}")

@updateroles_slash.error
async def updateroles_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ Admin only!", ephemeral=True)

# CRITICAL: Start Flask BEFORE Discord bot
if __name__ == "__main__":
    keep_alive()  # ← Start Flask first!
    bot.run(TOKEN)
