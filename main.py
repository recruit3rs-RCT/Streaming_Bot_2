import discord
from discord.ext import commands, tasks
import discord.app_commands
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

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("StreamBot")

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "10000"))

MAX_RANKED_USERS = int(os.getenv("MAX_RANKED_USERS", "60"))
ROLE_EDIT_DELAY = float(os.getenv("ROLE_EDIT_DELAY", "1.0"))

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN missing")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL missing")

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot alive", 200

@app.route("/health")
def health():
    return ({"status": "healthy", "bot_ready": bot.is_ready()}, 200 if bot.is_ready() else 503)

def run_flask():
    app.run(host="0.0.0.0", port=PORT, threaded=True)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()
    logger.info(f"Flask server started on port {PORT}")

ROLE_HIERARCHY = [
    (1477355642271305749, None),   # VC Rookie
    (1477355709522903081, 50),     # VC Raider
    (1477355810433794088, 40),     # VC Challenger
    (1477355846970114118, 30),     # VC Elite
    (1477355913961537577, 20),     # VC Legend
    (1477356047319695411, 10),     # VC Top Contender
    (1477356099484127354, 5),      # VC Finalist
    (1477356210528321670, 3),      # VC Champ
    (1477356266945777828, 2),      # VC MVP
    (1477356310059290767, 1),      # Apex Speaker
]
VC_ROLE_IDS = {rid for rid, _ in ROLE_HIERARCHY}

db_pool = None
join_times = {}  # (guild_id, user_id) -> monotonic start time

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=60,
        ssl="require",
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_time (
                guild_id BIGINT,
                user_id BIGINT,
                total_seconds INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_guild_total
            ON voice_time(guild_id, total_seconds DESC)
            """
        )
    logger.info("✅ Database initialized")

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if db_pool is None:
        await init_db()

    # IMPORTANT: no auto tree.sync here (can contribute to rate limits if reconnects happen). [web:92]
    if not save_streaming_time.is_running():
        save_streaming_time.start()
    if not auto_update_vc_roles.is_running():
        auto_update_vc_roles.start()

    logger.info("✅ Bot ready; tasks running")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or not member.guild:
        return

    key = (member.guild.id, member.id)

    is_streaming_now = after.channel is not None and after.self_stream
    was_streaming_before = before.channel is not None and before.self_stream

    if is_streaming_now and not was_streaming_before and key not in join_times:
        join_times[key] = asyncio.get_running_loop().time()
        ch = after.channel.name if after.channel else "Unknown"
        logger.info(f"▶️ Start track: {member} in {ch}")

    elif was_streaming_before and not is_streaming_now and key in join_times:
        start = join_times.pop(key)
        secs = int(asyncio.get_running_loop().time() - start)
        if secs < 5:
            return

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO voice_time (guild_id, user_id, total_seconds)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET total_seconds = voice_time.total_seconds + $3,
                              last_updated = NOW()
                """,
                member.guild.id,
                member.id,
                secs,
            )
        logger.info(f"💾 Saved {secs}s for {member}")

@tasks.loop(seconds=30)
async def save_streaming_time():
    if db_pool is None:
        return

    now = asyncio.get_running_loop().time()
    for (guild_id, user_id) in list(join_times.keys()):
        start = join_times.get((guild_id, user_id))
        if start is None:
            continue

        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        member = guild.get_member(user_id)
        if not member:
            continue

        is_streaming = bool(member.voice and member.voice.self_stream)
        if not is_streaming:
            secs = int(now - start)
            join_times.pop((guild_id, user_id), None)
            if secs >= 5:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO voice_time (guild_id, user_id, total_seconds)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (guild_id, user_id)
                        DO UPDATE SET total_seconds = voice_time.total_seconds + $3,
                                      last_updated = NOW()
                        """,
                        guild_id, user_id, secs
                    )
            continue

        elapsed = int(now - start)
        if elapsed >= 30:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO voice_time (guild_id, user_id, total_seconds)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET total_seconds = voice_time.total_seconds + $3,
                                  last_updated = NOW()
                    """,
                    guild_id, user_id, elapsed
                )
            join_times[(guild_id, user_id)] = asyncio.get_running_loop().time()

@save_streaming_time.before_loop
async def before_save_loop():
    await bot.wait_until_ready()

@tasks.loop(minutes=15)
async def auto_update_vc_roles():
    await bot.wait_until_ready()
    for guild in bot.guilds:
        await update_guild_vc_roles(guild)
        if len(bot.guilds) > 1:
            await asyncio.sleep(5)

@auto_update_vc_roles.before_loop
async def before_role_loop():
    await bot.wait_until_ready()

async def update_guild_vc_roles(guild: discord.Guild):
    if db_pool is None:
        return

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, total_seconds
            FROM voice_time
            WHERE guild_id=$1
            ORDER BY total_seconds DESC
            LIMIT $2
            """,
            guild.id,
            MAX_RANKED_USERS,
        )

    if not rows:
        logger.info(f"⚠️ No data for {guild.name}")
        return

    role_cache = {}
    missing = []
    for rid, _ in ROLE_HIERARCHY:
        r = guild.get_role(rid)
        if r:
            role_cache[rid] = r
        else:
            missing.append(rid)
    if missing:
        logger.warning(f"{guild.name} missing role IDs: {missing}")
        return

    updated = 0
    for rank, row in enumerate(rows, start=1):
        member = guild.get_member(row["user_id"])
        if not member:
            continue

        target = None
        for rid, threshold in reversed(ROLE_HIERARCHY):
            if threshold is None or rank <= threshold:
                target = role_cache[rid]
                break

        current = [r for r in member.roles if r.id in VC_ROLE_IDS]
        if len(current) == 1 and target and current[0].id == target.id:
            continue

        try:
            if current:
                await member.remove_roles(*current, reason="VC rank update")
                await asyncio.sleep(ROLE_EDIT_DELAY)
            if target:
                await member.add_roles(target, reason=f"Rank #{rank}")
                updated += 1
                await asyncio.sleep(ROLE_EDIT_DELAY)
        except discord.Forbidden:
            logger.warning(f"No permission to update roles for {member} in {guild.name}")
        except discord.HTTPException as e:
            logger.error(f"HTTP error updating {member}: {e}")
            await asyncio.sleep(max(ROLE_EDIT_DELAY * 3, 3))

    logger.info(f"🔄 {guild.name}: updated {updated} members")

# -------------------- Sync command (multi-server friendly) --------------------
@bot.command()
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def sync(ctx: commands.Context, spec: str | None = None):
    """
    !sync      -> global sync
    !sync ~    -> sync to current guild only (fast)
    !sync *    -> copy global to current guild and sync (fast; use for testing) [web:89]
    """
    try:
        if spec == "~":
            synced = await ctx.bot.tree.sync(guild=ctx.guild)
            await ctx.send(f"✅ Synced {len(synced)} commands to this server.")
        elif spec == "*":
            ctx.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await ctx.bot.tree.sync(guild=ctx.guild)
            await ctx.send(f"✅ Copied + synced {len(synced)} commands to this server.")
        else:
            synced = await ctx.bot.tree.sync()
            await ctx.send(f"✅ Synced {len(synced)} commands globally (may take time).")
    except Exception as e:
        await ctx.send(f"❌ Sync failed: {e}")

@sync.error
async def sync_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Administrator only.")

# -------------------- Slash commands --------------------
@bot.tree.command(name="leaderboard", description="Show streaming time leaderboard (limit: 1-20)")
async def leaderboard_slash(interaction: discord.Interaction, limit: int = 10):
    limit = max(1, min(limit, 20))
    await interaction.response.defer()

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, total_seconds
            FROM voice_time
            WHERE guild_id=$1
            ORDER BY total_seconds DESC
            LIMIT $2
            """,
            interaction.guild.id,
            limit,
        )

    if not rows:
        return await interaction.followup.send("No streaming stats yet.")

    embed = discord.Embed(title=f"🎤 Top {len(rows)} Streamers", color=0x5865F2)
    lines = []
    for i, row in enumerate(rows, start=1):
        m = interaction.guild.get_member(row["user_id"])
        name = m.display_name if m else f"User {row['user_id']}"
        hours = row["total_seconds"] / 3600
        lines.append(f"**{i}.** {name} — **{hours:.2f}** hours")
    embed.description = "\n".join(lines)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="stats", description="View streaming stats for you or a user")
async def stats_slash(interaction: discord.Interaction, user: discord.Member | None = None):
    await interaction.response.defer()
    target = user or interaction.user

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_seconds, last_updated FROM voice_time WHERE guild_id=$1 AND user_id=$2",
            interaction.guild.id,
            target.id,
        )

    if not row:
        return await interaction.followup.send(f"{target.display_name} has no streaming data yet.")

    embed = discord.Embed(title=f"🎤 {target.display_name} stats", color=0x5865F2)
    embed.add_field(name="Total", value=f"{row['total_seconds']/3600:.2f} hours", inline=True)
    embed.add_field(name="Last updated", value=str(row["last_updated"])[:19], inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="chart", description="Streaming time bar chart (limit: 1-15)")
async def chart_slash(interaction: discord.Interaction, limit: int = 10):
    limit = max(1, min(limit, 15))
    await interaction.response.defer()

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, total_seconds
            FROM voice_time
            WHERE guild_id=$1
            ORDER BY total_seconds DESC
            LIMIT $2
            """,
            interaction.guild.id,
            limit,
        )

    if not rows:
        return await interaction.followup.send("No data for chart.")

    names, hrs = [], []
    for r in rows:
        m = interaction.guild.get_member(r["user_id"])
        names.append((m.name if m else f"User{r['user_id']}")[:20])
        hrs.append(r["total_seconds"] / 3600)

    hrs = np.array(hrs)
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(12, max(6, len(names) * 0.5)))
    y = np.arange(len(names))
    ax.barh(y, hrs, color="#5865F2")
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.set_xlabel("Hours streaming")
    ax.set_title(f"{interaction.guild.name} top {len(names)}")
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="PNG", dpi=130, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)

    await interaction.followup.send(file=discord.File(buf, filename="stream_chart.png"))

@bot.tree.command(name="updateroles", description="Manually update VC rank roles (Admin only)")
@discord.app_commands.checks.has_permissions(administrator=True)
async def updateroles_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await update_guild_vc_roles(interaction.guild)
    await interaction.followup.send("✅ Done.", ephemeral=True)

@bot.tree.command(name="resetstats", description="Reset a user's stats (Admin only)")
@discord.app_commands.checks.has_permissions(administrator=True)
async def resetstats_slash(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM voice_time WHERE guild_id=$1 AND user_id=$2",
            interaction.guild.id,
            user.id,
        )
    await interaction.followup.send("✅ Reset.", ephemeral=True)

# -------------------- Entrypoint --------------------
if __name__ == "__main__":
    keep_alive()
    time.sleep(random.uniform(2, 6))
    bot.run(TOKEN, log_handler=None)
