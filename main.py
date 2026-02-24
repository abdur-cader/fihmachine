import nextcord
from nextcord.ext import commands, tasks
from nextcord import Interaction
from nextcord import ButtonStyle
from nextcord.ui import View, button, Button
import random
from variables import *
from vpcalc import calculate_vp
import httpx
import os
import json
import re
from datetime import datetime, timedelta, timezone, time as dt_time
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv()
elevenlabs = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
elevenlabs_priority = ElevenLabs(api_key=os.getenv("ELEVENLABS_PRIORITY_KEY")) if os.getenv("ELEVENLABS_PRIORITY_KEY") else None

async def _get_priority_key_remaining_chars() -> int | None:
    """Return remaining characters for ELEVENLABS_PRIORITY_KEY, or None if unavailable."""
    key = os.getenv("ELEVENLABS_PRIORITY_KEY")
    if not key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": key, "Content-Type": "application/json"},
                timeout=10,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            sub = data.get("subscription", {})
            limit = sub.get("character_limit", 0)
            used = sub.get("character_count", 0)
            return max(0, limit - used)
    except Exception:
        return None

# ---------------------------------------------------------------------------------
# Monthly cap for ELEVENLABS_API_KEY when used by this bot (shared key for other programs)
# ---------------------------------------------------------------------------------
BOT_REGULAR_KEY_MONTHLY_LIMIT = 10_000  # characters per calendar month
ELEVENLABS_BOT_USAGE_FILE = Path(__file__).resolve().parent / "elevenlabs_bot_usage.json"

def _get_bot_regular_usage() -> tuple[str, int]:
    """Return (current_month_yyyy_mm, characters_used_this_month)."""
    now = datetime.now(timezone.utc)
    month_key = now.strftime("%Y-%m")
    if not ELEVENLABS_BOT_USAGE_FILE.exists():
        return month_key, 0
    try:
        with open(ELEVENLABS_BOT_USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("month") != month_key:
            return month_key, 0
        return month_key, int(data.get("characters_used", 0))
    except Exception:
        return month_key, 0

def _record_bot_regular_usage(chars: int) -> None:
    """Add chars to this month's usage for the regular key."""
    month_key, used = _get_bot_regular_usage()
    used += chars
    try:
        with open(ELEVENLABS_BOT_USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump({"month": month_key, "characters_used": used}, f, indent=2)
    except Exception:
        pass

# ---------------------------------------------------------------------------------
# Persistent trigger settings (message-based triggers: per-channel or server-wide)
# ---------------------------------------------------------------------------------
TRIGGER_NAMES = ("dad", "sus", "gyros", "eat_shit", "shut_up")
SHUT_UP_USER_ID = 129801271870881793
TRIGGER_SETTINGS_FILE = Path(__file__).resolve().parent / "trigger_settings.json"

def load_trigger_settings():
    """Load (disabled_channels, disabled_guilds). Each is { trigger: [id, ...] }."""
    empty_c = {t: [] for t in TRIGGER_NAMES}
    empty_g = {t: [] for t in TRIGGER_NAMES}
    if not TRIGGER_SETTINGS_FILE.exists():
        return empty_c, empty_g
    try:
        with open(TRIGGER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "channels" in data and "guilds" in data:
            return (
                {t: list(data["channels"].get(t, [])) for t in TRIGGER_NAMES},
                {t: list(data["guilds"].get(t, [])) for t in TRIGGER_NAMES},
            )
        # Old format: top-level keys are trigger names -> channel ids
        return {t: list(data.get(t, [])) for t in TRIGGER_NAMES}, empty_g
    except Exception:
        return empty_c, empty_g

def save_trigger_settings():
    with open(TRIGGER_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"channels": trigger_disabled_channels, "guilds": trigger_disabled_guilds}, f, indent=2)

trigger_disabled_channels, trigger_disabled_guilds = load_trigger_settings()

def is_trigger_enabled(channel_id: int, guild_id: int, trigger_name: str) -> bool:
    if guild_id in trigger_disabled_guilds.get(trigger_name, []):
        return False
    return channel_id not in trigger_disabled_channels.get(trigger_name, [])

def set_trigger_enabled(channel_id: int, guild_id: int, trigger_name: str, enabled: bool, scope: str):
    if scope == "server_wide":
        guilds = trigger_disabled_guilds.setdefault(trigger_name, [])
        if enabled:
            if guild_id in guilds:
                guilds.remove(guild_id)
        else:
            if guild_id not in guilds:
                guilds.append(guild_id)
    else:
        channels = trigger_disabled_channels.setdefault(trigger_name, [])
        if enabled:
            if channel_id in channels:
                channels.remove(channel_id)
        else:
            if channel_id not in channels:
                channels.append(channel_id)
    save_trigger_settings()

# ---------------------------------------------------------------------------------
# Time-me-out: daily self-timeout at a given local time (persistent)
# ---------------------------------------------------------------------------------
TIMEOUT_SCHEDULES_FILE = Path(__file__).resolve().parent / "timeout_schedules.json"

def load_timeout_schedules():
    if not TIMEOUT_SCHEDULES_FILE.exists():
        return []
    try:
        with open(TIMEOUT_SCHEDULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("schedules", [])
    except Exception:
        return []

def save_timeout_schedules(schedules: list):
    with open(TIMEOUT_SCHEDULES_FILE, "w", encoding="utf-8") as f:
        json.dump({"schedules": schedules}, f, indent=2)

def get_timeout_schedule(user_id: int, guild_id: int):
    schedules = load_timeout_schedules()
    for s in schedules:
        if s["user_id"] == user_id and s["guild_id"] == guild_id:
            return s
    return None

def get_timeout_schedules_for_user(user_id: int, guild_id: int):
    """Return all timeout schedules for a given user in a given guild."""
    schedules = load_timeout_schedules()
    return [s for s in schedules if s["user_id"] == user_id and s["guild_id"] == guild_id]

def set_timeout_schedule(user_id: int, guild_id: int, channel_id: int, duration_minutes: int, hour: int, minute: int, gmt_offset: int):
    schedules = load_timeout_schedules()
    schedules = [s for s in schedules if not (s["user_id"] == user_id and s["guild_id"] == guild_id)]
    schedules.append({
        "user_id": user_id,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "duration_minutes": duration_minutes,
        "hour": hour,
        "minute": minute,
        "gmt_offset": gmt_offset,
        "last_apply_date": None,
    })
    save_timeout_schedules(schedules)

def remove_timeout_schedule(user_id: int, guild_id: int):
    schedules = load_timeout_schedules()
    schedules = [s for s in schedules if not (s["user_id"] == user_id and s["guild_id"] == guild_id)]
    save_timeout_schedules(schedules)

def parse_time_24h(s: str):
    """Parse 'HH:MM' or 'H:MM', return (hour, minute) or None."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return (h, mi)
    return None

def next_occurrence_utc(hour: int, minute: int, gmt_offset: int) -> datetime:
    """Next occurrence of hour:minute in GMT+offset, as UTC datetime."""
    tz = timezone(timedelta(hours=gmt_offset))
    now_in_tz = datetime.now(tz)
    today = now_in_tz.date()
    target_in_tz = datetime.combine(today, dt_time(hour, minute), tzinfo=tz)
    if target_in_tz <= now_in_tz:
        target_in_tz = datetime.combine(today + timedelta(days=1), dt_time(hour, minute), tzinfo=tz)
    return target_in_tz.astimezone(timezone.utc)


bot = commands.Bot(
    command_prefix=".",
    intents=nextcord.Intents.all(),
    activity=nextcord.Streaming(
        name='how to cook fish',
        url='https://www.youtube.com/watch?v=qOO4ZEj8tlw'
    )
)

ONLINE_CHANNEL_ID = 1250442534375788586
TIMEOUT_LOG_CHANNEL_ID = 1250442534375788586  # same channel for timeout scheduler logs
MIMIC_LOG_CHANNEL_ID = 1475448201715908628

# Channel ID -> last deleted message info
snipes: dict[int, dict] = {}

# ---------------------------------------------------------------------------------
# Rock Paper Scissors game views
# ---------------------------------------------------------------------------------


class RPSInviteView(View):
    def __init__(self, starter: nextcord.Member, opponent: nextcord.Member, best_of: int):
        super().__init__(timeout=300)
        self.starter = starter
        self.opponent = opponent
        self.best_of = best_of

    async def interaction_check(self, interaction: Interaction) -> bool:
        # Only invited opponent can interact with this view.
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("You are not the invited player for this game.", ephemeral=True)
            return False
        return True

    @button(label="Accept", style=ButtonStyle.green)
    async def accept(self, _: Button, interaction: Interaction):
        # Only called if interaction_check passed.
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"{self.opponent.mention} accepted the RPS invite from {self.starter.mention}.",
            view=self,
        )

        # Start actual game
        view = RPSGameView(self.starter, self.opponent, self.best_of)
        wins_needed = view.wins_needed
        await interaction.channel.send(
            f"Rock, Paper, Scissors game started between {self.starter.mention} and {self.opponent.mention}!"
            f"\nFirst to **{wins_needed}** win{'s' if wins_needed > 1 else ''} (best of {self.best_of})."
            f"\nBoth players, pick your move using the buttons below.",
            view=view,
        )

    @button(label="Deny", style=ButtonStyle.red)
    async def deny(self, _: Button, interaction: Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"{self.opponent.mention} denied the RPS invite from {self.starter.mention} cause they were scared.",
            view=self,
        )


class RPSGameView(View):
    def __init__(self, player1: nextcord.Member, player2: nextcord.Member, best_of: int):
        super().__init__(timeout=None)
        self.player1 = player1
        self.player2 = player2
        self.best_of = max(1, min(10, best_of))
        self.choices: dict[int, str] = {}  # user_id -> "rock"/"paper"/"scissors"
        self.scores: dict[int, int] = {player1.id: 0, player2.id: 0}
        self.round_number: int = 1
        self.wins_needed: int = self.best_of // 2 + 1

    async def interaction_check(self, interaction: Interaction) -> bool:
        # Only the two players can interact with the game.
        if interaction.user.id not in (self.player1.id, self.player2.id):
            await interaction.response.send_message("You are not part of this RPS game.", ephemeral=True)
            return False
        return True

    def _other_player(self, user_id: int) -> nextcord.Member:
        return self.player2 if user_id == self.player1.id else self.player1

    def _choice_emoji(self, choice: str) -> str:
        mapping = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
        return mapping.get(choice, choice)

    async def _handle_bail(self, interaction: Interaction):
        bailer = interaction.user
        winner = self._other_player(bailer.id)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"{bailer.mention} bailed!\n{winner.mention} wins cause {bailer.mention} got scared. <a:tomato:1471738692308566141> <a:tomato:1471738692308566141> <a:tomato:1471738692308566141>",
            view=self,
        )

    async def _resolve_round(self, interaction: Interaction):
        # Both players have chosen; resolve this round.
        user_ids = list(self.choices.keys())
        u1, u2 = user_ids[0], user_ids[1]
        c1, c2 = self.choices[u1], self.choices[u2]

        p1 = self.player1 if self.player1.id == u1 else self.player2
        p2 = self.player1 if self.player1.id == u2 else self.player2

        beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}

        round_result_lines = [
            f"Round **{self.round_number}** results:",
            f"{p1.mention} chose {self._choice_emoji(c1)}",
            f"{p2.mention} chose {self._choice_emoji(c2)}",
        ]

        winner_id: int | None
        if c1 == c2:
            winner_id = None
            round_result_lines.append("It's a tie!")
        elif beats[c1] == c2:
            winner_id = u1
        else:
            winner_id = u2

        if winner_id is not None:
            self.scores[winner_id] += 1
            winner_member = self.player1 if self.player1.id == winner_id else self.player2
            round_result_lines.append(f"{winner_member.mention} wins this round!")

        # Prepare for next round
        self.round_number += 1
        self.choices.clear()

        s1 = self.scores[self.player1.id]
        s2 = self.scores[self.player2.id]
        score_line = f"Score: {self.player1.mention} **{s1}** - **{s2}** {self.player2.mention}"

        game_over = s1 >= self.wins_needed or s2 >= self.wins_needed
        if game_over:
            for child in self.children:
                child.disabled = True
            overall_winner = self.player1 if s1 > s2 else self.player2
            round_result_lines.append(f"\n{overall_winner.mention} wins the series (best of {self.best_of})!")

        content = "\n".join(round_result_lines + [score_line])
        await interaction.response.edit_message(content=content, view=self)

    async def _handle_choice(self, interaction: Interaction, choice: str):
        user = interaction.user
        self.choices[user.id] = choice

        # If only one player has chosen so far, just acknowledge.
        if len(self.choices) == 1:
            await interaction.response.send_message(
                f"You picked {self._choice_emoji(choice)}. Waiting for the other player...",
                ephemeral=True,
            )
            return

        # Second player's choice completes the round.
        await self._resolve_round(interaction)

    @button(label="Rock", style=ButtonStyle.blurple)
    async def rock(self, _: Button, interaction: Interaction):
        await self._handle_choice(interaction, "rock")

    @button(label="Paper", style=ButtonStyle.blurple)
    async def paper(self, _: Button, interaction: Interaction):
        await self._handle_choice(interaction, "paper")

    @button(label="Scissors", style=ButtonStyle.blurple)
    async def scissors(self, _: Button, interaction: Interaction):
        await self._handle_choice(interaction, "scissors")

    @button(label="Bail", style=ButtonStyle.red)
    async def bail(self, _: Button, interaction: Interaction):
        await self._handle_bail(interaction)


# ---------------------------------------------------------------------------------
# Coinflip game view (two-player heads/tails prediction)
# ---------------------------------------------------------------------------------


class CoinflipView(View):
    def __init__(self, starter: nextcord.Member, opponent: nextcord.Member):
        super().__init__(timeout=120)
        self.starter = starter
        self.opponent = opponent
        self.choices: dict[int, str] = {}  # user_id -> "Heads"/"Tails"

    async def interaction_check(self, interaction: Interaction) -> bool:
        # Only the two players can interact with this view.
        if interaction.user.id not in (self.starter.id, self.opponent.id):
            await interaction.response.send_message("You are not part of this coinflip.", ephemeral=True)
            return False
        return True

    async def _handle_choice(self, interaction: Interaction, choice: str):
        user = interaction.user
        self.choices[user.id] = choice

        # First chooser: acknowledge privately.
        if len(self.choices) == 1:
            await interaction.response.send_message(f"You chose **{choice}**. Waiting for the other player...", ephemeral=True)
            return

        # Second chooser: resolve the flip.
        result = random.choice(["Heads", "Tails"])

        for child in self.children:
            child.disabled = True

        s_choice = self.choices.get(self.starter.id)
        o_choice = self.choices.get(self.opponent.id)

        lines = [
            f"🪙 Coinflip between {self.starter.mention} and {self.opponent.mention}",
            f"{self.starter.mention} picked **{s_choice}**",
            f"{self.opponent.mention} picked **{o_choice}**",
            f"\nCoin landed on **{result}**.",
        ]

        winners = []
        if s_choice == result:
            winners.append(self.starter)
        if o_choice == result:
            winners.append(self.opponent)

        if len(winners) == 2:
            lines.append("Both guessed correctly!")
        elif len(winners) == 1:
            lines.append(f"{winners[0].mention} wins!")
        else:
            lines.append("Nobody guessed correctly.")

        await interaction.response.edit_message(content="\n".join(lines), view=self)

    @button(label="Heads", style=ButtonStyle.blurple)
    async def heads(self, _: Button, interaction: Interaction):
        await self._handle_choice(interaction, "Heads")

    @button(label="Tails", style=ButtonStyle.blurple)
    async def tails(self, _: Button, interaction: Interaction):
        await self._handle_choice(interaction, "Tails")

@bot.event
async def on_ready():
    print("Bot is online.")
    timeout_scheduler_task.start()
    channel = bot.get_channel(ONLINE_CHANNEL_ID)
    if channel:
        await channel.send("online")

######################################################################################################
############################################ BOT COMMANDS ############################################

@bot.command(aliases=["hi", "hey"])
async def hello(ctx):
    await ctx.send(f"hi {ctx.author.mention} bye {ctx.author.mention}")

@bot.command()
async def bye(ctx):
    await ctx.send(f"ok get lost")



@bot.command()
async def sendmsg(ctx, channel_id: int, *, message: str):
    channel = bot.get_channel(channel_id)
    if channel is None:
        for guild in bot.guilds:
            channel = guild.get_channel(channel_id)
            if channel:
                break
    if channel is None:
        await ctx.send("Channel not found. (Use a channel ID from a server the bot is in.)")
        return
    try:
        await channel.send(message)
        await ctx.send("Message sent.")
    except nextcord.Forbidden:
        await ctx.send("I don't have permission to send messages in that channel.")
    except Exception as e:
        await ctx.send(f"Failed to send: {e}")

@bot.slash_command(name="greet", description="I'll greet you")
async def greet(interaction: Interaction):
    await interaction.response.send_message("no")


@bot.slash_command(name="vpcalculator", description="Suggests VP bundles to purchase based on the item you want to buy and your current balance.")
async def vp(interaction: nextcord.Interaction, itemprice: float, currentbalance: float):
    details, total = calculate_vp(itemprice, currentbalance) 
    
    embed = nextcord.Embed(
        title=f"Total: €{total}",
        description=details if details else "You already have enough u stoopid",
        color=nextcord.Color.from_rgb(43, 45, 49)
    )
    embed.set_footer(text=f" Item Price: {itemprice} VP | Current Balance: {currentbalance} VP",
                     icon_url="https://cdn.discordapp.com/emojis/834771348739326043.gif?size=128&quality=lossless")
    
    await interaction.send(embed=embed)

@bot.slash_command(name="random", description="only if youre bored")
async def rndm(interaction: Interaction):
    await interaction.response.send_message(random.choice(randomsg))

@bot.slash_command(name="coinflip", description="Flip a coin (50/50). Optionally challenge another user.")
async def coinflip(
    interaction: Interaction,
    opponent: nextcord.Member = nextcord.SlashOption(
        required=False,
        description="Optional: challenge another user; both pick Heads/Tails before the flip",
    ),
):
    # Solo flip: instant result.
    if opponent is None:
        result = random.choice(["Heads", "Tails"])
        await interaction.response.send_message(f"🪙 {result}")
        return

    if opponent.bot:
        await interaction.response.send_message("You can't challenge a bot for coinflip.", ephemeral=True)
        return

    if opponent.id == interaction.user.id:
        await interaction.response.send_message("You can't challenge yourself for coinflip.", ephemeral=True)
        return

    view = CoinflipView(interaction.user, opponent)
    await interaction.response.send_message(
        f"🪙 Coinflip prediction game!\n"
        f"{interaction.user.mention} vs {opponent.mention}\n"
        f"Both players, pick **Heads** or **Tails** using the buttons below. The coin will flip after both have chosen.",
        view=view,
    )

@bot.slash_command(name="rps", description="Challenge someone to Rock, Paper, Scissors")
async def rps(
    interaction: Interaction,
    opponent: nextcord.Member = nextcord.SlashOption(required=True, description="Who do you want to play against?"),
    best_of: int = nextcord.SlashOption(required=False, default=1, description="Best of how many rounds? (1-10)"),
):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    if opponent.bot:
        await interaction.response.send_message("You can't challenge a bot.", ephemeral=True)
        return

    if opponent.id == interaction.user.id:
        await interaction.response.send_message("You can't play RPS against yourself.", ephemeral=True)
        return

    if best_of < 1 or best_of > 10:
        await interaction.response.send_message("Best of must be between 1 and 10.", ephemeral=True)
        return

    view = RPSInviteView(interaction.user, opponent, best_of)
    await interaction.response.send_message(
        f"{opponent.mention}, {interaction.user.mention} challenged you to Rock, Paper, Scissors!"
        f"\nBest of **{best_of}**. Do you accept?",
        view=view,
    )

@bot.slash_command(name="snipe", description="Show the most recently deleted message in this channel")
async def snipe(interaction: Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    channel_id = interaction.channel_id
    data = snipes.get(channel_id)
    if not data:
        await interaction.response.send_message("There's nothing to snipe in this channel.", ephemeral=True)
        return

    author = data["author"]
    content = data["content"]
    created_at = data["created_at"]
    deleted_at = data["deleted_at"]

    embed = nextcord.Embed(
        title="Sniped message",
        description=content or "*[no content]*",
        color=nextcord.Color.from_rgb(43, 45, 49),
    )
    embed.set_author(name=str(author), icon_url=getattr(author.display_avatar, "url", nextcord.Embed.Empty))
    if isinstance(created_at, datetime):
        embed.add_field(
            name="Sent at",
            value=f"<t:{int(created_at.timestamp())}:F> (<t:{int(created_at.timestamp())}:R>)",
            inline=False,
        )
    if isinstance(deleted_at, datetime):
        embed.add_field(
            name="Deleted at",
            value=f"<t:{int(deleted_at.timestamp())}:F> (<t:{int(deleted_at.timestamp())}:R>)",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)

@bot.slash_command(name="eightball", description="Ask a question and you shall be answered")
async def eightball(interaction: Interaction, question: str):
    response = random.choice(ebresponse)
    await interaction.response.send_message(f"`\"{question}\"`\n\n{response}")

@bot.slash_command(name="mimic", description="mimic someone")
async def mimic(interaction: Interaction, user: nextcord.Member, message: str):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return
    channel = interaction.channel
    if not isinstance(channel, nextcord.TextChannel):
        await interaction.response.send_message("This channel doesn't support webhooks.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    # Log who invoked mimic, who was mimicked, and the message
    mimic_log = f"[mimic] **({interaction.user})**\n```{user.display_name}: {message}```"
    print(mimic_log)
    log_channel = bot.get_channel(MIMIC_LOG_CHANNEL_ID)
    if log_channel:
        try:
            await log_channel.send(mimic_log)
        except Exception:
            pass
    webhook = None
    try:
        webhook = await channel.create_webhook(name="Mimic")
        avatar_url = str(user.display_avatar.url)
        await webhook.send(content=message, username=user.display_name, avatar_url=avatar_url)
        await interaction.followup.send("Mimic sent.", ephemeral=True)
    except nextcord.Forbidden:
        await interaction.followup.send("I need **Manage Webhooks** permission in this channel.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed: {e}", ephemeral=True)
    finally:
        if webhook:
            try:
                await webhook.delete()
            except Exception:
                pass


# -------------------- Message trigger toggles: /enable, /disable (feature + scope) --------------------
# nextcord SlashOption choices: dict of display_name -> value (no SlashOptionChoice in this nextcord version)
trigger_choices = {
    "Dad jokes (I'm...)": "dad",
    "Sus / wordlist (video reply)": "sus",
    "Gyros (imo/imho/opinion gif)": "gyros",
    "Eat shit (peepoChocolate)": "eat_shit",
    "Shut up (20% reply to specific user)": "shut_up",
}
scope_choices = {"This channel": "this_channel", "Server-wide": "server_wide"}
TRIGGER_LABELS = {v: k for k, v in trigger_choices.items()}

def _trigger_label(value: str) -> str:
    return TRIGGER_LABELS.get(value, value)

@bot.slash_command(name="enable", description="Enable a message trigger in this channel or server-wide")
async def enable_trigger(
    interaction: Interaction,
    feature: str = nextcord.SlashOption(choices=trigger_choices, required=True, description="Which trigger to enable"),
    scope: str = nextcord.SlashOption(choices=scope_choices, required=True, description="This channel or server-wide"),
):
    await interaction.response.defer(ephemeral=True)
    if interaction.guild_id is None and scope == "server_wide":
        await interaction.followup.send("Server-wide only works in a server.", ephemeral=True)
        return
    guild_id = interaction.guild_id or 0
    set_trigger_enabled(interaction.channel_id, guild_id, feature, True, scope)
    label = _trigger_label(feature)
    where = "server-wide" if scope == "server_wide" else "in this channel"
    await interaction.followup.send(f"**{label}** enabled {where} ✅", ephemeral=True)

@bot.slash_command(name="disable", description="Disable a message trigger in this channel or server-wide")
async def disable_trigger(
    interaction: Interaction,
    feature: str = nextcord.SlashOption(choices=trigger_choices, required=True, description="Which trigger to disable"),
    scope: str = nextcord.SlashOption(choices=scope_choices, required=True, description="This channel or server-wide"),
):
    await interaction.response.defer(ephemeral=True)
    if interaction.guild_id is None and scope == "server_wide":
        await interaction.followup.send("Server-wide only works in a server.", ephemeral=True)
        return
    guild_id = interaction.guild_id or 0
    set_trigger_enabled(interaction.channel_id, guild_id, feature, False, scope)
    label = _trigger_label(feature)
    where = "server-wide" if scope == "server_wide" else "in this channel"
    await interaction.followup.send(f"**{label}** disabled {where} ✅", ephemeral=True)

@bot.slash_command(name="triggers", description="Show trigger status for this channel and server")
async def triggers_status(interaction: Interaction):
    channel_id = interaction.channel_id
    guild_id = getattr(interaction.guild, "id", None) or 0
    lines = []
    for t in TRIGGER_NAMES:
        label = _trigger_label(t)
        ch_on = is_trigger_enabled(channel_id, guild_id, t)
        guild_disabled = guild_id and guild_id in trigger_disabled_guilds.get(t, [])
        if guild_disabled:
            lines.append(f"• **{label}**: off (server-wide)")
        elif ch_on:
            lines.append(f"• **{label}**: on (this channel)")
        else:
            lines.append(f"• **{label}**: off (this channel)")
    await interaction.response.send_message("Trigger status:\n" + "\n".join(lines))

# -------------------- Time-me-out: daily self-timeout at local time --------------------
# GMT offset dropdown: GMT-12 through GMT+12 (value = offset hours as string)
gmt_offset_choices = {f"GMT{n:+d}" if n != 0 else "GMT+0": str(n) for n in range(-12, 13)}

@bot.slash_command(name="timeout", description="Schedule a daily timeout for yourself at a set time (your local time)")
async def timeout_schedule(
    interaction: Interaction,
    time_24h: str = nextcord.SlashOption(required=True, description="Time in 24h format, e.g. 14:30"),
    timezone: str = nextcord.SlashOption(choices=gmt_offset_choices, required=True, description="Your timezone (GMT offset)"),
    duration_hours: int = nextcord.SlashOption(required=True, description="Duration hours (use 0 if only minutes)"),
    duration_minutes: int = nextcord.SlashOption(required=True, description="Duration minutes (use 0 if only hours)"),
):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return
    if duration_hours == 0 and duration_minutes == 0:
        await interaction.response.send_message("Duration must be at least 1 hour or 1 minute (both can't be 0).", ephemeral=True)
        return
    parsed = parse_time_24h(time_24h)
    if not parsed:
        await interaction.response.send_message("Invalid time. Use 24h format, e.g. `14:30` or `9:00`.", ephemeral=True)
        return
    hour, minute = parsed
    gmt_offset = int(timezone)
    total_minutes = duration_hours * 60 + duration_minutes
    if total_minutes > 40320:  # 28 days max for Discord timeout
        await interaction.response.send_message("Duration cannot exceed 28 days.", ephemeral=True)
        return
    await interaction.response.defer()
    user_id = interaction.user.id
    guild_id = interaction.guild_id
    channel_id = interaction.channel_id
    set_timeout_schedule(user_id, guild_id, channel_id, total_minutes, hour, minute, gmt_offset)
    next_utc = next_occurrence_utc(hour, minute, gmt_offset)
    unix_ts = int(next_utc.timestamp())
    time_str = f"{hour:02d}:{minute:02d}"
    tz_label = f"GMT{gmt_offset:+d}" if gmt_offset != 0 else "GMT+0"
    await interaction.followup.send(
        f"You will be timed out **every day** at **{time_str}** ({tz_label}) for **{duration_hours}h {duration_minutes}min**.\n"
        f"Next run: <t:{unix_ts}:F>\n"
        f"Use `/timeout_cancel` to stop.",
    )

@bot.slash_command(name="timeout_cancel", description="Stop your daily timeout schedule in this server")
async def timeout_cancel(interaction: Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return
    user_id = interaction.user.id
    guild_id = interaction.guild_id
    if not get_timeout_schedule(user_id, guild_id):
        await interaction.response.send_message("You don't have a time-me-out schedule in this server.", ephemeral=True)
        return
    remove_timeout_schedule(user_id, guild_id)
    await interaction.response.send_message("Daily time-me-out disabled for you in this server.", ephemeral=True)

@bot.slash_command(name="timeouts", description="Show all your daily timeout schedules in this server")
async def timeout_list(interaction: Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return
    user_id = interaction.user.id
    guild_id = interaction.guild_id
    schedules = get_timeout_schedules_for_user(user_id, guild_id)
    if not schedules:
        await interaction.response.send_message("You don't have any time-me-out schedules in this server.", ephemeral=True)
        return

    lines = []
    for s in schedules:
        hour, minute = s["hour"], s["minute"]
        gmt_offset = s.get("gmt_offset", 0)
        tz_label = f"GMT{gmt_offset:+d}" if gmt_offset != 0 else "GMT+0"

        total_minutes = s["duration_minutes"]
        dur_h = total_minutes // 60
        dur_m = total_minutes % 60
        if dur_h and dur_m:
            dur_str = f"{dur_h}h {dur_m}min"
        elif dur_h:
            dur_str = f"{dur_h}h"
        else:
            dur_str = f"{dur_m}min"

        next_utc = next_occurrence_utc(hour, minute, gmt_offset)
        unix_ts = int(next_utc.timestamp())

        channel_id = s.get("channel_id")
        channel_label = f"<#{channel_id}>" if channel_id else "(channel unknown)"

        lines.append(
            f"- Time: **{hour:02d}:{minute:02d}** ({tz_label}), "
            f"Duration: **{dur_str}**, Channel: {channel_label}, "
            f"Next run: <t:{unix_ts}:F>"
        )

    await interaction.response.send_message(
        "Your daily time-me-out schedules in this server:\n" + "\n".join(lines),
        ephemeral=True,
    )

@bot.slash_command(name="timeouts_user", description="Show another member's daily timeout schedules in this server")
async def timeout_list_user(
    interaction: Interaction,
    member: nextcord.Member = nextcord.SlashOption(required=True, description="Member to inspect"),
    ephemeral: bool = nextcord.SlashOption(required=False, default=False, description="Show this only to you?"),
):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    user_id = member.id
    schedules = get_timeout_schedules_for_user(user_id, guild_id)
    if not schedules:
        await interaction.response.send_message(
            f"{member.mention} doesn't have any time-me-out schedules in this server.",
            ephemeral=ephemeral,
        )
        return

    lines = []
    for s in schedules:
        hour, minute = s["hour"], s["minute"]
        gmt_offset = s.get("gmt_offset", 0)
        tz_label = f"GMT{gmt_offset:+d}" if gmt_offset != 0 else "GMT+0"

        total_minutes = s["duration_minutes"]
        dur_h = total_minutes // 60
        dur_m = total_minutes % 60
        if dur_h and dur_m:
            dur_str = f"{dur_h}h {dur_m}min"
        elif dur_h:
            dur_str = f"{dur_h}h"
        else:
            dur_str = f"{dur_m}min"

        next_utc = next_occurrence_utc(hour, minute, gmt_offset)
        unix_ts = int(next_utc.timestamp())

        channel_id = s.get("channel_id")
        channel_label = f"<#{channel_id}>" if channel_id else "(channel unknown)"

        lines.append(
            f"- Time: **{hour:02d}:{minute:02d}** ({tz_label}), "
            f"Duration: **{dur_str}**, Channel: {channel_label}, "
            f"Next run: <t:{unix_ts}:F>"
        )

    await interaction.response.send_message(
        f"Daily time-me-out schedules for {member.mention} in this server:\n" + "\n".join(lines),
        ephemeral=ephemeral,
    )

async def _timeout_log(message: str, guild_id: int):
    """Send timeout scheduler log to the log channel and print to console."""
    print(message)
    ch = bot.get_channel(TIMEOUT_LOG_CHANNEL_ID)
    if ch:
        try:
            await ch.send(f"[timeout-scheduler] {message}\n**Server ID:** `{guild_id}`")
        except Exception:
            pass

@tasks.loop(seconds=10)
async def timeout_scheduler_task():
    """Apply daily time-me-out at scheduled times (user's local time)."""
    schedules = load_timeout_schedules()
    if not schedules:
        return
    now_utc = datetime.now(timezone.utc)
    for s in schedules:
        guild_id = s["guild_id"]
        if "gmt_offset" in s:
            tz = timezone(timedelta(hours=s["gmt_offset"]))
        else:
            try:
                tz = ZoneInfo(s.get("timezone", "UTC"))
            except Exception:
                tz = timezone.utc
        guild = bot.get_guild(guild_id)
        if not guild:
            await _timeout_log(f"Guild not found (not in cache). User: {s['user_id']}.", guild_id)
            continue
        member = guild.get_member(s["user_id"])
        if not member:
            try:
                member = await guild.fetch_member(s["user_id"])
            except Exception as e:
                await _timeout_log(f"Could not fetch member {s['user_id']}: {e}", guild_id)
                continue
        if not member:
            continue

        # Check if a previous timeout for this schedule has just ended and notify once.
        last_end_at_str = s.get("last_timeout_end_at")
        last_end_notified = s.get("last_timeout_end_notified", True if not last_end_at_str else False)

        # Prefer Discord's own timeout end time (restart-safe), fall back to stored value.
        discord_end = getattr(member, "communication_disabled_until", None)
        if discord_end is None:
            discord_end = getattr(member, "timed_out_until", None)

        if isinstance(discord_end, datetime):
            last_end_at = discord_end.astimezone(timezone.utc)
        elif last_end_at_str:
            try:
                last_end_at = datetime.fromisoformat(last_end_at_str)
            except ValueError:
                last_end_at = None
        else:
            last_end_at = None

        if last_end_at and not last_end_notified and now_utc >= last_end_at:
            announce_ch = guild.get_channel(s.get("channel_id")) if s.get("channel_id") else guild.system_channel
            if announce_ch:
                try:
                    await announce_ch.send(f"{member.mention} your timeout is over <a:5x30:1338567476962656318>")
                except Exception:
                    pass
            s["last_timeout_end_notified"] = True
            updated = [x for x in load_timeout_schedules() if not (x["user_id"] == s["user_id"] and x["guild_id"] == s["guild_id"])]
            updated.append(s)
            save_timeout_schedules(updated)

        now_in_tz = now_utc.astimezone(tz)
        h, mi = s["hour"], s["minute"]
        scheduled_today = datetime.combine(now_in_tz.date(), dt_time(h, mi), tzinfo=tz)
        today_str = scheduled_today.date().isoformat()

        # Not yet time for today's timeout in this timezone.
        if now_in_tz < scheduled_today:
            continue

        # Already handled today's schedule.
        if s.get("last_apply_date") == today_str:
            continue

        # Compute this day's timeout window in local time.
        total_minutes = s["duration_minutes"]
        full_duration = timedelta(minutes=total_minutes)
        end_today_local = scheduled_today + full_duration

        # If we're past the full timeout window for today, skip applying it
        # (missed for this day) and mark as applied so the next run is tomorrow.
        if now_in_tz >= end_today_local:
            s["last_apply_date"] = today_str
            updated = [x for x in load_timeout_schedules() if not (x["user_id"] == s["user_id"] and x["guild_id"] == s["guild_id"])]
            updated.append(s)
            save_timeout_schedules(updated)
            continue

        # We're within today's timeout window but after the scheduled start:
        # apply only the remaining duration for this day.
        remaining_duration = end_today_local - now_in_tz
        try:
            await member.timeout(remaining_duration, reason="Scheduled time-me-out")
        except nextcord.Forbidden:
            await _timeout_log(f"Missing permission (need Moderate Members) to timeout user {s['user_id']}.", guild_id)
            continue
        except Exception as e:
            await _timeout_log(f"Failed to timeout user {s['user_id']}: {e}", guild_id)
            continue

        s["last_apply_date"] = today_str
        # Record when this timeout will end (UTC) for restart-safe notifications.
        discord_end = getattr(member, "communication_disabled_until", None)
        if discord_end is None:
            discord_end = getattr(member, "timed_out_until", None)
        if isinstance(discord_end, datetime):
            s["last_timeout_end_at"] = discord_end.astimezone(timezone.utc).isoformat()
        else:
            s["last_timeout_end_at"] = (now_utc + remaining_duration).isoformat()
        s["last_timeout_end_notified"] = False
        updated = [x for x in load_timeout_schedules() if not (x["user_id"] == s["user_id"] and x["guild_id"] == s["guild_id"])]
        updated.append(s)
        save_timeout_schedules(updated)
        await _timeout_log(f"Applied timeout for user {s['user_id']}.", guild_id)
        # Public message in the respective channel: "[user] has been timed out for [x duration]"
        announce_ch = guild.get_channel(s.get("channel_id")) if s.get("channel_id") else guild.system_channel
        if announce_ch:
            # Announce the actual remaining duration that is being applied.
            remaining_minutes = int(remaining_duration.total_seconds() // 60)
            if remaining_minutes >= 60 and remaining_minutes % 60 == 0:
                dur_str = f"{remaining_minutes // 60}h"
            elif remaining_minutes >= 60:
                dur_str = f"{remaining_minutes // 60}h {remaining_minutes % 60}min"
            else:
                dur_str = f"{remaining_minutes}min"
            try:
                await announce_ch.send(f"{member.mention} has been timed out for **{dur_str}**.")
            except Exception:
                pass

# Voice choices for TTS (display name -> ElevenLabs voice_id)
generate_voice_choices = {
    "Jake (larry voice)": "nPczCjzI2devNBz1zQrb",
    "Piggsy (what x is this)": "85LOUMcMhNruPi5cBPC0",
}
# Language choices for TTS (ISO 639-1). Must be supported by eleven_multilingual_v2.
generate_voice_language_choices = {
    "English": "en",
    "Japanese": "ja",
    "Indonesian": "id",
    "Korean": "ko",
    "Chinese": "zh",
}

@bot.slash_command(name="generate_voice", description="Generate speech audio from your text (restricted).")
async def generate_voice(
    interaction: Interaction,
    text: str = nextcord.SlashOption(required=True, description="Text to convert to speech"),
    voice: str = nextcord.SlashOption(choices=generate_voice_choices, required=True, description="Voice to use"),
    language: str = nextcord.SlashOption(choices=generate_voice_language_choices, required=False, default="en", description="Language for speech (default: English)"),
):
    await interaction.response.defer()

    voice_id = voice if voice in generate_voice_choices.values() else "nPczCjzI2devNBz1zQrb"
    language_code = language if language else "en"
    is_custom_clone = voice_id == "85LOUMcMhNruPi5cBPC0"  # Piggsy / IVC cloned voice
    if is_custom_clone:
        voice_settings = {
            "stability": 0.50,
            "similarity_boost": 0.75,
            "style_exaggeration": 0.0,
            "speaking_rate": 0.95,
        }
    else:
        voice_settings = {
            "stability": 0.42,
            "similarity_boost": 0.75,
            "style_exaggeration": 0.10,
            "speaking_rate": 1.10,
        }
    kwargs = {
        "text": text,
        "voice_id": voice_id,
        "model_id": "eleven_multilingual_v2",
        "output_format": "mp3_44100_128",
        "language_code": language_code,
        "voice_settings": voice_settings,
    }
    if is_custom_clone:
        kwargs["use_pvc_as_ivc"] = True

    text_len = len(text)
    month_key, bot_used = _get_bot_regular_usage()
    regular_cap_ok = (bot_used + text_len) <= BOT_REGULAR_KEY_MONTHLY_LIMIT

    client = elevenlabs
    if not is_custom_clone and elevenlabs_priority:
        remaining = await _get_priority_key_remaining_chars()
        estimated_chars = text_len * 2
        if remaining is not None and remaining >= estimated_chars:
            client = elevenlabs_priority
    if client is elevenlabs and not regular_cap_ok:
        await interaction.followup.send(
            f"This bot's monthly character limit ({BOT_REGULAR_KEY_MONTHLY_LIMIT:,} characters) for the API key has been reached. Try again next month or use the other voice.",
            ephemeral=True,
        )
        return

    try:
        audio = client.text_to_speech.convert(**kwargs)
    except Exception as e:
        await interaction.followup.send(f"Failed to generate audio: {e}", ephemeral=True)
        return

    voice_display = {v: k for k, v in generate_voice_choices.items()}
    print(f"[generate_voice] Voice used: {voice_display.get(voice_id, voice_id)}")

    if client is elevenlabs:
        _record_bot_regular_usage(text_len)

    filename = f"voice_{interaction.user.id}.mp3"

    # save to disk
    with open(filename, "wb") as f:
        for chunk in audio:
            f.write(chunk)

    try:
        # send only the file
        await interaction.followup.send(file=nextcord.File(filename))
    finally:
        # cleanup local file
        if os.path.exists(filename):
            os.remove(filename)

######################################################################################################
######################################################################################################

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    channel_id = message.channel.id
    guild_id = message.guild.id if message.guild else 0
    words = message.content.split(" ")

    # Dad jokes (I'm...) — only one reply per message (if/elif)
    if is_trigger_enabled(channel_id, guild_id, "dad"):
        low = message.content.lower()
        if 'i am ' in low:
            await message.channel.send('Hi ' + message.content[low.index('i am ')+5:] + ', I\'m Dad')
        elif 'i\'m ' in low:
            await message.channel.send('Hi ' + message.content[low.index('i\'m ')+4:] + ', I\'m Dad')
        elif 'i"m ' in low:
            await message.channel.send('Hi ' + message.content[low.index('i"m ')+4:] + ', I\'m Dad')
        elif 'im ' in low:
            idx = low.index('im ')
            if idx == 0 or message.content[idx - 1] == ' ':
                await message.channel.send('Hi ' + message.content[idx+3:] + ', I\'m Dad')

    # Sus / wordlist
    if is_trigger_enabled(channel_id, guild_id, "sus"):
        for word in words:
            if word in wordlist:
                await message.reply('https://cdn.discordapp.com/attachments/852873744912482345/1006523187183501382/SomeOrdinaryGamers_Is_Very_Sus....mp4')
                break

    # Gyros (imo/imho/opinion)
    if is_trigger_enabled(channel_id, guild_id, "gyros"):
        for word in words:
            if word.lower() in gyros_trigger:
                await message.reply('https://media.discordapp.net/attachments/877394207571083341/976824012539826176/sadsadddd-1.gif')
                break

    # Eat shit
    if is_trigger_enabled(channel_id, guild_id, "eat_shit") and 'eat shit' in message.content.lower():
        await message.channel.send('<:peepoChocolate:1250442571701026867>')

    # Shut up: 20% chance to reply "shut up" when specific user sends a message
    if message.author.id == SHUT_UP_USER_ID and is_trigger_enabled(channel_id, guild_id, "shut_up") and random.random() < 0.20:
        await message.reply("shut up")

    await bot.process_commands(message)

@bot.event
async def on_message_delete(message: nextcord.Message):
    # Ignore own messages and system messages
    if message.author == bot.user or message.author.bot:
        return
    if not message.guild or not isinstance(message.channel, nextcord.TextChannel):
        return

    snipes[message.channel.id] = {
        "author": message.author,
        "content": message.content,
        "created_at": message.created_at if isinstance(message.created_at, datetime) else None,
        "deleted_at": datetime.now(timezone.utc),
    }

TOKEN = os.getenv("PANKOMACHINE_TOKEN")
print("Loaded token:", repr(TOKEN))
bot.run(TOKEN)
