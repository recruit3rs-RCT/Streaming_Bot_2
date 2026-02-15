import discord
from discord.ext import commands, tasks
import sqlite3
import asyncio
import time
import logging
from datetime import datetime, timezone, timedelta

DAILY_CAP_SECONDS = 3 * 60 * 60
GRACE_SECONDS = 30

log = logging.getLogger("voice_leaderboard")
logging.basicConfig(level=logging.INFO)


class VoiceLeaderboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_lock = asyncio.Lock()
        self.rank_lock = asyncio.Lock()
        self.invalid_since = {}
        self.rank_cache = {}

        self.conn = sqlite3.connect(
            "voice.db",
            check_same_thread=False
        )
        self._init_db()

        self.rank_sync.start()
        self.bot.loop.create_task(self.restore_sessions())

        # Replace role IDs
        self.RANK_ROLE_MAP = [
            (1, 111111111111111111),
            (2, 222222222222222222),
            (3, 333333333333333333),
            (5, 444444444444444444),
            (10, 555555555555555555),
            (20, 666666666666666666),
            (30, 777777777777777777),
            (40, 888888888888888888),
            (50, 999999999999999999),
            (9999999, 101010101010101010),
        ]

    def _init_db(self):
        cur = self.conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            guild_id INTEGER,
            user_id INTEGER,
            total_seconds INTEGER DEFAULT 0,
            daily_seconds INTEGER DEFAULT 0,
            last_day INTEGER,
            active_start REAL,
            PRIMARY KEY (guild_id, user_id)
        )
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_total
        ON users(guild_id, total_seconds DESC)
        """)
        self.conn.commit()

    def current_day(self):
        return int(datetime.now(timezone.utc).strftime("%Y%m%d"))

    async def ensure_user(self, guild_id, user_id):
        async with self.db_lock:
            cur = self.conn.cursor()
            cur.execute("""
            INSERT OR IGNORE INTO users (guild_id, user_id, last_day)
            VALUES (?, ?, ?)
            """, (guild_id, user_id, self.current_day()))
            self.conn.commit()

    async def start_session(self, guild_id, user_id):
        async with self.db_lock:
            cur = self.conn.cursor()
            cur.execute("""
            UPDATE users
            SET active_start=?
            WHERE guild_id=? AND user_id=?
            """, (time.time(), guild_id, user_id))
            self.conn.commit()

    async def end_session(self, guild_id, user_id):
        async with self.db_lock:
            cur = self.conn.cursor()
            cur.execute("""
            SELECT active_start FROM users
            WHERE guild_id=? AND user_id=?
            """, (guild_id, user_id))
            row = cur.fetchone()

            if not row or not row[0]:
                return

            start = row[0]
            end = time.time()

            await self._split_add_locked(cur, guild_id, user_id, start, end)

            cur.execute("""
            UPDATE users SET active_start=NULL
            WHERE guild_id=? AND user_id=?
            """, (guild_id, user_id))
            self.conn.commit()

        guild = self.bot.get_guild(guild_id)
        await self.update_rank_roles(guild)

    async def _split_add_locked(self, cur, guild_id, user_id, start, end):
        cur.execute("""
        SELECT total_seconds, daily_seconds, last_day
        FROM users WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        total, daily, last_day = cur.fetchone()

        while start < end:
            start_dt = datetime.fromtimestamp(start, timezone.utc)
            next_midnight = datetime(
                start_dt.year,
                start_dt.month,
                start_dt.day,
                tzinfo=timezone.utc
            ) + timedelta(days=1)

            segment_end = min(end, next_midnight.timestamp())
            seconds = int(segment_end - start)

            today = int(start_dt.strftime("%Y%m%d"))
            if last_day != today:
                daily = 0
                last_day = today

            remaining = DAILY_CAP_SECONDS - daily
            allowed = max(0, min(seconds, remaining))

            total += allowed
            daily += allowed

            start = segment_end

        cur.execute("""
        UPDATE users
        SET total_seconds=?, daily_seconds=?, last_day=?
        WHERE guild_id=? AND user_id=?
        """, (total, daily, last_day, guild_id, user_id))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return

        guild_id = member.guild.id
        user_id = member.id

        await self.ensure_user(guild_id, user_id)

        valid = (
            after.channel
            and after.self_stream
            and not after.self_deaf
        )

        if valid:
            async with self.db_lock:
                cur = self.conn.cursor()
                cur.execute("""
                SELECT active_start FROM users
                WHERE guild_id=? AND user_id=?
                """, (guild_id, user_id))
                active = cur.fetchone()[0]

            if not active:
                await self.start_session(guild_id, user_id)

            self.invalid_since.pop((guild_id, user_id), None)

        else:
            key = (guild_id, user_id)
            if key not in self.invalid_since:
                self.invalid_since[key] = time.time()
            elif time.time() - self.invalid_since[key] > GRACE_SECONDS:
                await self.end_session(guild_id, user_id)
                self.invalid_since.pop(key, None)

    async def restore_sessions(self):
        await self.bot.wait_until_ready()

        async with self.db_lock:
            cur = self.conn.cursor()
            cur.execute("""
            SELECT guild_id, user_id, active_start
            FROM users WHERE active_start IS NOT NULL
            """)
            rows = cur.fetchall()

        for guild_id, user_id, start in rows:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            member = guild.get_member(user_id)
            if (
                member
                and member.voice
                and member.voice.self_stream
                and not member.voice.self_deaf
            ):
                await self.end_session(guild_id, user_id)
                await self.start_session(guild_id, user_id)
            else:
                async with self.db_lock:
                    cur = self.conn.cursor()
                    cur.execute("""
                    UPDATE users SET active_start=NULL
                    WHERE guild_id=? AND user_id=?
                    """, (guild_id, user_id))
                    self.conn.commit()

    async def update_rank_roles(self, guild):
        if not guild:
            return

        async with self.rank_lock:
            async with self.db_lock:
                cur = self.conn.cursor()
                cur.execute("""
                SELECT user_id
                FROM users
                WHERE guild_id=?
                ORDER BY total_seconds DESC
                """, (guild.id,))
                rows = cur.fetchall()

            new_ranks = {uid: i + 1 for i, (uid,) in enumerate(rows)}
            old_ranks = self.rank_cache.get(guild.id, {})
            self.rank_cache[guild.id] = new_ranks

            changed = {
                uid for uid in set(new_ranks) | set(old_ranks)
                if new_ranks.get(uid) != old_ranks.get(uid)
            }

            role_lookup = {rid: guild.get_role(rid) for _, rid in self.RANK_ROLE_MAP}

            for user_id in changed:
                member = guild.get_member(user_id)
                if not member or member.bot:
                    continue

                rank = new_ranks.get(user_id)
                if not rank:
                    continue

                target_role_id = None
                for threshold, rid in self.RANK_ROLE_MAP:
                    if rank <= threshold:
                        target_role_id = rid
                        break

                target_role = role_lookup.get(target_role_id)

                current_roles = [
                    role_lookup[rid]
                    for _, rid in self.RANK_ROLE_MAP
                    if role_lookup.get(rid) in member.roles
                ]

                try:
                    if current_roles:
                        await member.remove_roles(*current_roles)

                    if (
                        target_role
                        and guild.me.top_role > target_roleA
                        and target_role not in member.roles
                    ):
                        await member.add_roles(target_role)

                except Exception as e:
                    log.error(f"Role update failed for {user_id}: {e}")

    @tasks.loop(minutes=5)
    async def rank_sync(self):
        for guild in self.bot.guilds:
            await self.update_rank_roles(guild)

    def format_time(self, seconds):
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h}h {m:02}m {s:02}s"

    @commands.command()
    async def leaderboard(self, ctx):
        async with self.db_lock:
            cur = self.conn.cursor()
            cur.execute("""
            SELECT user_id, total_seconds
            FROM users
            WHERE guild_id=?
            ORDER BY total_seconds DESC
            LIMIT 10
            """, (ctx.guild.id,))
            rows = cur.fetchall()

        embed = discord.Embed(
            title="? Voice Leaderboard",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.set_thumbnail(
            url=ctx.guild.icon.url if ctx.guild.icon else None
        )

        medals = {1: "?", 2: "?", 3: "?"}

        for pos, (uid, seconds) in enumerate(rows, start=1):
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            badge = medals.get(pos, f"`#{pos}`")
            value = self.format_time(seconds)

            if ctx.author.id == uid:
                name = f"**{name}** ?"

            embed.add_field(
                name=f"{badge} {name}",
                value=value,
                inline=False
            )

        embed.set_footer(text="Rankings auto-update every 5 minutes")
        await ctx.send(embed=embed)

    def cog_unload(self):
        self.rank_sync.cancel()
        self.conn.close()


async def setup(bot):
    await bot.add_cog(VoiceLeaderboard(bot))
