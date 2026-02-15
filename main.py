import os
import asyncio
import discord
from discord.ext import commands

# ========== CONFIG ==========
TOKEN = "MTQ3MTkyODE5MTI0ODYyOTg4Mw.G4qEKB.40rCmQUN8nGKjOBmRLT2fII0oxUd4ijXAvUCo0"
COG_DIR = "./cogs"
# ============================


# Simple colored console output
class colors:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    RESET = '\033[0m'


# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


@bot.event
async def on_ready():
    print(f"{colors.GREEN}Logged in as {bot.user} (ID: {bot.user.id}){colors.RESET}")
    print(f"{colors.YELLOW}------{colors.RESET}")


async def load_cogs():
    successful_cogs = []
    failed_cogs = []

    print(f"{colors.CYAN}📦 Only loading files ending with 'cog.py' for faster startup{colors.RESET}")

    for root, _, files in os.walk(COG_DIR):
        for filename in files:
            if filename.endswith('cog.py') and not filename.startswith('__'):
                rel_path = os.path.relpath(root, COG_DIR)

                # Convert Windows path to proper Python module path
                if rel_path == '.':
                    module_name = filename[:-3]
                else:
                    module_parts = rel_path.replace('\\', '.').replace('/', '.')
                    module_name = f'{module_parts}.{filename[:-3]}'

                try:
                    full_name = f'cogs.{module_name}'
                    await bot.load_extension(full_name)

                    simple_name = module_name.split('.')[-1]
                    successful_cogs.append(simple_name)

                    print(f"{colors.GREEN}✅ {simple_name}{colors.RESET}")

                except Exception as e:
                    failed_cogs.append((filename, root, str(e)))
                    print(f"{colors.RED}❌ {filename} - {str(e)}...{colors.RESET}")

    print()
    print(f"{colors.GREEN}Loaded: {len(successful_cogs)} cogs{colors.RESET}")
    print(f"{colors.RED}Failed: {len(failed_cogs)} cogs{colors.RESET}")

    if failed_cogs:
        print(f"\n{colors.YELLOW}Failed Cogs Details:{colors.RESET}")
        for filename, root, error in failed_cogs:
            print(f" - {filename} ({root}) -> {error}")


async def main():
    async with bot:
        await load_cogs()
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

