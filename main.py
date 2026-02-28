import discord
from discord.ext import commands, tasks
import os
import io
import time
import random
from flask import Flask
from threading import Thread
import asyncpg
import asyncio
import logging
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('StreamBot')

# Environment variables
TOKEN = os.getenv('DISCORD_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')

# Validate environment variables
if not TOKEN:
    logger.error("DISCORD_TOKEN not found in environment variables!")
    exit(1)
if not DATABASE_URL:
    logger.error("DATABASE_URL not found in environment variables!")
    exit(1)

# Discord bot setup
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Flask keep-alive server
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and running!", 200

@app.route('/health')
def health():
    """Health check endpoint for Render.com"""
    if bot.is_ready():
        return {"status": "healthy", "bot": "ready"}, 200
    return {"status": "unhealthy", "bot": "not ready"}, 503

def run_flask():
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)

def keep_alive():
    """Start Flask server in background thread"""
    t = Thread(target=run_flask, daemon=True)
    t.start()
    logger.info(f"Flask server started on port {os.getenv('PORT', 10000)}")

# ================================================================
# ROLE HIERARCHY — Replace with your actual Role IDs from Discord
# How to get Role ID:
#   1. User Settings → Advanced → Enable Developer Mode
#   2. Server Settings → Roles → Right-click role → Copy Role ID
# ================================================================
ROLE_HIERARCHY = [
    (123456789012345678, None),   # VC Rookie        — everyone else
    (123456789012345679, 50),     # VC Raider        — top 50
    (123456789012345680, 40),     # VC Challenger    — top 40
    (123456789012345681, 30),     # VC Elite         — top 30
    (123456789012345682, 20),     # VC Legend        — top 20
    (123456789012345683, 10),     # VC Top Contender — top 10
    (123456789012345684, 5),      # VC Finalist      — top 5
    (123456789012345685, 3),      # VC Champ         — top 3
    (123456789012345686, 2),      # VC MVP           — top 2
    (123456789012345687, 1),      # Apex Speaker     — #1 only
]

# Set of all VC role IDs for quick membership checks
VC_ROLE_IDS = {role_id for role_id, _ in ROLE_HIERARCHY}

# Database connection pool
db_pool = None
join_times = {}

async def init_db():
    """Initialize database connection pool with proper error handling"""
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=60,
            ssl='require'
        )

        async with db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS voice_time (
                    guild_id BIGINT,
                    user_id BIGINT,
                    total_seconds INTEGER DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
            ''')

            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_guild_total
                ON voice_time(guild_id, total_seconds DESC)
            ''')

        logger.info("✅ Database initialized successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return False

@bot.event
async def on_ready():
    """Bot startup event"""
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    db_ready = await init_db()
    if not db_ready:
        logger.error("Failed to initialize database. Bot may not function properly.")
        return

    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands globally")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

    if not save_streaming_time.is_running():
        save_streaming_time.start()
    if not auto_update_vc_roles.is_running():
        auto_update_vc_roles.start()

    logger.info("🔄 Background tasks started (30s auto-save, 5min role update)")
    logger.info("✅ Stream LB Bot ready - tracking all streaming activity!")

@bot.event
async def on_close():
    """Cleanup on bot shutdown — save all active sessions"""
    global db_pool

    logger.info("Shutting down bot...")

    for key in list(join_times.keys()):
        try:
            guild_id, user_id = key
            session_time = int(asyncio.get_event_loop().time() - join_times[key])

            if session_time >= 5 and db_pool:
                async with db_pool.acquire() as conn:
                    await conn.execute('''
                        INSERT INTO voice_time (guild_id, user_id, total_seconds)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (guild_id, user_id)
                        DO UPDATE SET total_seconds = voice_time.total_seconds + $3
                    ''', guild_id, user_id, session_time)
                logger.info(f"💾 Final save {session_time}s for user {user_id}")
        except Exception as e:
            logger.error(f"Error saving final session: {e}")

    if db_pool:
        await db_pool.close()
        logger.info("🔒 Database pool closed")

@bot.event
async def on_voice_state_update(member, before, after):
    """Track streaming time based ONLY on self_stream status"""
    if member.bot or not member.guild:
        return

    guild_id = member.guild.id
    user_id = member.id
    key = (guild_id, user_id)

    is_streaming_now = after.channel is not None and after.self_stream
    was_streaming_before = before.channel is not None and before.self_stream

    # START tracking
    if is_streaming_now and not was_streaming_before and key not in join_times:
        join_times[key] = asyncio.get_event_loop().time()
        channel_name = after.channel.name if after.channel else "Unknown"
        logger.info(f"▶️ Started tracking {member.name} (streaming in {channel_name})")

    # STOP tracking
    elif was_streaming_before and not is_streaming_now and key in join_times:
        session_time = int(asyncio.get_event_loop().time() - join_times[key])

        if session_time >= 5:
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute('''
                        INSERT INTO voice_time (guild_id, user_id, total_seconds)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (guild_id, user_id)
                        DO UPDATE SET total_seconds = voice_time.total_seconds + $3,
                                     last_updated = NOW()
                    ''', guild_id, user_id, session_time)
                logger.info(f"✅ Saved {session_time}s for {member.name}")
            except Exception as e:
                logger.error(f"❌ Database save error for {member.name}: {e}")
        else:
            logger.info(f"⏭️ Skipped {session_time}s for {member.name} (too short)")

        del join_times[key]
        logger.info(f"⏹️ Stopped tracking {member.name} (stopped streaming)")

@tasks.loop(seconds=30)
async def save_streaming_time():
    """Save streaming time every 30 seconds for all active streamers"""
    if not db_pool:
        return

    keys_snapshot = list(join_times.keys())

    for key in keys_snapshot:
        try:
            guild_id, user_id = key
            join_time = join_times.get(key)

            if join_time is None:
                continue

            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            member = guild.get_member(user_id)
            if not member:
                continue

            is_streaming = member.voice and member.voice.self_stream

            # Stopped streaming — save final time
            if not is_streaming:
                session_time = int(asyncio.get_event_loop().time() - join_time)
                if session_time >= 5:
                    async with db_pool.acquire() as conn:
                        await conn.execute('''
                            INSERT INTO voice_time (guild_id, user_id, total_seconds)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (guild_id, user_id)
                            DO UPDATE SET total_seconds = voice_time.total_seconds + $3,
                                         last_updated = NOW()
                        ''', guild_id, user_id, session_time)
                    logger.info(f"💾 Final save {session_time}s for {member.name} (stopped streaming)")

                join_times.pop(key, None)
                continue

            # Still streaming — periodic save
            elapsed = int(asyncio.get_event_loop().time() - join_time)
            if elapsed >= 30:
                async with db_pool.acquire() as conn:
                    await conn.execute('''
                        INSERT INTO voice_time (guild_id, user_id, total_seconds)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (guild_id, user_id)
                        DO UPDATE SET total_seconds = voice_time.total_seconds + $3,
                                     last_updated = NOW()
                    ''', guild_id, user_id, elapsed)

                join_times[key] = asyncio.get_event_loop().time()
                logger.info(f"💾 Auto-saved {elapsed}s for {member.name}")

        except Exception as e:
            logger.error(f"Error in save_streaming_time for {key}: {e}")

@save_streaming_time.before_loop
async def before_save_task():
    await bot.wait_until_ready()
    logger.info("Save task initialized")

@save_streaming_time.error
async def save_task_error(error):
    logger.error(f"❌ save_streaming_time error: {error}")
    await asyncio.sleep(60)

@tasks.loop(minutes=5)
async def auto_update_vc_roles():
    """Auto-update VC rank roles every 5 minutes"""
    if not db_pool:
        return

    for guild in bot.guilds:
        try:
            await update_guild_vc_roles(guild)
            logger.info(f"✅ Auto-updated VC roles for {guild.name}")

            if len(bot.guilds) > 1:
                await asyncio.sleep(5)

        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = int(e.response.headers.get('Retry-After', 60))
                logger.warning(f"⚠️ Rate limited for {guild.name}, waiting {retry_after}s...")
                await asyncio.sleep(retry_after)
            else:
                logger.error(f"❌ HTTP Error for {guild.name}: {e}")
        except Exception as e:
            logger.error(f"❌ Error updating {guild.name}: {e}")

@auto_update_vc_roles.before_loop
async def before_role_update():
    await bot.wait_until_ready()
    logger.info("Role update task initialized")

@auto_update_vc_roles.error
async def role_update_error(error):
    logger.error(f"❌ auto_update_vc_roles error: {error}")
    await asyncio.sleep(300)

async def update_guild_vc_roles(guild):
    """Update VC roles for a specific guild using Role IDs"""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT user_id, total_seconds FROM voice_time WHERE guild_id=$1 ORDER BY total_seconds DESC',
                guild.id
            )
    except Exception as e:
        logger.error(f"❌ Database error for {guild.name}: {e}")
        return

    if not rows:
        logger.info(f"⚠️ No streaming data for {guild.name}")
        return

    # Fetch all VC roles by ID
    guild_vc_roles = {}
    missing_roles = []
    for role_id, _ in ROLE_HIERARCHY:
        role = guild.get_role(role_id)
        if role:
            guild_vc_roles[role_id] = role
        else:
            missing_roles.append(role_id)

    if missing_roles:
        logger.warning(f"⚠️ {guild.name} missing role IDs: {', '.join(str(r) for r in missing_roles)}")
        return

    updated_count = 0

    for rank, row in enumerate(rows, 1):
        member = guild.get_member(row['user_id'])
        if not member:
            continue

        # Determine target role based on rank
        target_role = None
        for role_id, threshold in reversed(ROLE_HIERARCHY):
            if threshold is None or rank <= threshold:
                target_role = guild_vc_roles[role_id]
                break

        # Get member's current VC roles by ID
        current_vc_roles = [r for r in member.roles if r.id in VC_ROLE_IDS]

        # Skip if already correct
        if len(current_vc_roles) == 1 and current_vc_roles[0] == target_role:
            continue

        try:
            if current_vc_roles:
                await member.remove_roles(*current_vc_roles, reason="VC rank update")

            if target_role:
                await member.add_roles(target_role, reason=f"Rank #{rank}")
                updated_count += 1

            await asyncio.sleep(0.5)

        except discord.Forbidden:
            logger.warning(f"⚠️ No permission to update {member.name} in {guild.name}")
        except Exception as e:
            logger.error(f"❌ Error updating {member.name}: {e}")

    if updated_count > 0:
        logger.info(f"🔄 Updated {updated_count} members in {guild.name}")

@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    """Force sync slash commands (Administrator only)"""
    try:
        synced = await bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"✅ Synced {len(synced)} commands to this server!")

        synced_global = await bot.tree.sync()
        await ctx.send(f"✅ Also synced {len(synced_global)} commands globally (may take up to 1 hour)")
    except Exception as e:
        await ctx.send(f"❌ Sync failed: {e}")

@sync.error
async def sync_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need Administrator permission to use this command!")

@bot.tree.command(name="leaderboard", description="Show streaming time leaderboard (limit: 1-20)")
async def leaderboard_slash(interaction: discord.Interaction, limit: int = 10):
    limit = max(1, min(limit, 20))
    await interaction.response.defer()

    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT user_id, total_seconds FROM voice_time WHERE guild_id=$1 ORDER BY total_seconds DESC LIMIT $2',
                interaction.guild.id, limit
            )
    except Exception as e:
        logger.error(f"Database error in leaderboard: {e}")
        return await interaction.followup.send("❌ Database error occurred. Please try again later.")

    if not rows:
        return await interaction.followup.send("📊 No streaming stats yet! Start streaming to get tracked.")

    embed = discord.Embed(
        title=f"🎤 Top {len(rows)} Streamers",
        color=0x5865F2,
        description=""
    )

    leaderboard_text = ""
    for i, row in enumerate(rows, 1):
        user = interaction.guild.get_member(row['user_id'])
        name = user.display_name if user else f"User {row['user_id']}"
        hours = row['total_seconds'] / 3600
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"**{i}.**"
        leaderboard_text += f"{medal} {name} — **{hours:.2f}** hours\n"

    embed.description = leaderboard_text
    embed.set_footer(text=f"Total tracked streamers: {len(rows)} | Refreshes every 30s")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="stats", description="View your personal streaming statistics")
async def stats_slash(interaction: discord.Interaction, user: discord.Member = None):
    await interaction.response.defer()

    target_user = user or interaction.user

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT total_seconds, last_updated FROM voice_time WHERE guild_id=$1 AND user_id=$2',
                interaction.guild.id, target_user.id
            )

            rank_row = await conn.fetchrow('''
                SELECT COUNT(*) + 1 as rank
                FROM voice_time
                WHERE guild_id=$1 AND total_seconds > (
                    SELECT COALESCE(
                        (SELECT total_seconds FROM voice_time WHERE guild_id=$1 AND user_id=$2),
                        0
                    )
                )
            ''', interaction.guild.id, target_user.id)

    except Exception as e:
        logger.error(f"Database error in stats: {e}")
        return await interaction.followup.send("❌ Database error occurred.")

    if not row:
        return await interaction.followup.send(f"📊 {target_user.display_name} has no streaming data yet!")

    total_hours = row['total_seconds'] / 3600
    rank = rank_row['rank'] if rank_row else "N/A"

    embed = discord.Embed(
        title=f"🎤 {target_user.display_name}'s Streaming Stats",
        color=0x5865F2
    )
    embed.add_field(name="Total Time", value=f"**{total_hours:.2f}** hours", inline=True)
    embed.add_field(name="Server Rank", value=f"**#{rank}**", inline=True)
    embed.add_field(
        name="Last Updated",
        value=f"{row['last_updated'].strftime('%Y-%m-%d %H:%M')} UTC" if row['last_updated'] else "Unknown",
        inline=True
    )
    embed.set_thumbnail(url=target_user.display_avatar.url)
    embed.set_footer(text="Tracking all streaming activity in voice channels")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="chart", description="Generate streaming time bar chart (limit: 1-15)")
async def chart_slash(interaction: discord.Interaction, limit: int = 10):
    limit = max(1, min(limit, 15))
    await interaction.response.defer()

    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT user_id, total_seconds FROM voice_time WHERE guild_id=$1 ORDER BY total_seconds DESC LIMIT $2',
                interaction.guild.id, limit
            )
    except Exception as e:
        logger.error(f"Database error in chart: {e}")
        return await interaction.followup.send("❌ Database error occurred.")

    if not rows:
        return await interaction.followup.send("📊 No streaming data available for chart!")

    usernames = []
    for row in rows:
        user = interaction.guild.get_member(row['user_id'])
        if user:
            name = user.name[:20] + "..." if len(user.name) > 20 else user.name
            usernames.append(name)
        else:
            usernames.append(f"User{row['user_id']}")

    hours = np.array([row['total_seconds'] / 3600 for row in rows])

    plt.rcParams['font.family'] = 'DejaVu Sans'
    plt.rcParams['axes.unicode_minus'] = False
    plt.style.use('dark_background')

    fig, ax = plt.subplots(figsize=(14, max(7, len(usernames) * 0.6)))
    y_pos = np.arange(len(usernames))
    bars = ax.barh(y_pos, hours, color='#5865F2', edgecolor='white', linewidth=0.5)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(usernames, fontsize=11, family='monospace')
    ax.set_xlabel('Hours Streaming', fontsize=13, fontweight='bold')
    ax.set_title(f'🎥 {interaction.guild.name} - Top {limit} Streamers',
                 fontsize=16, fontweight='bold', pad=20)
    ax.grid(axis='x', alpha=0.25, linestyle='--')
    ax.set_facecolor('#2C2F33')
    fig.patch.set_facecolor('#23272A')

    max_hours = max(hours) if len(hours) > 0 else 1
    for i, bar in enumerate(bars):
        width = bar.get_width()
        if width > 0:
            ax.text(width + max_hours * 0.015, bar.get_y() + bar.get_height() / 2,
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
        logger.error(f"Manual role update error: {e}")
        await interaction.followup.send(f"❌ Error: {str(e)[:200]}")

@updateroles_slash.error
async def updateroles_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

@bot.tree.command(name="resetstats", description="Reset streaming stats for a user (Admin only)")
@discord.app_commands.checks.has_permissions(administrator=True)
async def resetstats_slash(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    try:
        async with db_pool.acquire() as conn:
            result = await conn.execute(
                'DELETE FROM voice_time WHERE guild_id=$1 AND user_id=$2',
                interaction.guild.id, user.id
            )
        if result == "DELETE 1":
            await interaction.followup.send(f"✅ Reset streaming stats for {user.display_name}")
        else:
            await interaction.followup.send(f"⚠️ {user.display_name} had no stats to reset")
    except Exception as e:
        logger.error(f"Reset stats error: {e}")
        await interaction.followup.send(f"❌ Error resetting stats: {e}")

@resetstats_slash.error
async def resetstats_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

# ================================================================
# ENTRY POINT — Exponential backoff on rate limits
# ================================================================
if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("Starting Discord Streaming Tracker Bot")
    logger.info("=" * 50)

    keep_alive()

    # Random startup jitter — prevents synchronized reconnects on Render
    startup_delay = random.uniform(2, 8)
    logger.info(f"Waiting {startup_delay:.1f}s before connecting...")
    time.sleep(startup_delay)

    max_retries = 5
    base_delay = 30  # seconds

    for attempt in range(max_retries):
        try:
            logger.info(f"Connection attempt {attempt + 1}/{max_retries}")
            bot.run(TOKEN, log_handler=None, reconnect=True)
            break  # Clean exit — stop retrying

        except discord.errors.HTTPException as e:
            if e.status == 429:
                wait_time = base_delay * (2 ** attempt) + random.uniform(0, 10)
                logger.error(f"❌ Rate limited by Discord (429). Waiting {wait_time:.0f}s before retry...")
                time.sleep(wait_time)
            else:
                logger.error(f"❌ HTTP error {e.status}: {e}")
                time.sleep(base_delay)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break

        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = base_delay * (2 ** attempt) + random.uniform(0, 10)
                logger.error(f"❌ Fatal error: {e} — retrying in {wait_time:.0f}s...")
                time.sleep(wait_time)
            else:
                logger.error(f"❌ Max retries reached. Last error: {e}")
