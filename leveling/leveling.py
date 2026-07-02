"""Leveling cog — MEE6-style XP, levels, role rewards and a leaderboard.

Opt-in per guild (disabled by default). Bilingual output (DE/EN) following a
per-guild language setting (default: en-US). Awards XP for messages (with a
cooldown) and for voice activity (background loop). Integrates with the PDC
web dashboard (settings panel + leaderboard page) via the resilient drop-in.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import time
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red

try:
    from redbot.core.utils.views import SimpleMenu
except Exception:  # pragma: no cover - older Red versions
    SimpleMenu = None  # type: ignore

from .pdc_dashboard import (
    Component,
    Field,
    L,
    PageSchema,
    PanelSchema,
    SubmitResult,
    dashboard_page,
    dashboard_panel,
    register_dashboard,
    tr_lang,
    unregister_dashboard,
)

log = logging.getLogger("red.pdc.leveling")  # module logger

ANNOUNCE_MODES = ("off", "current", "channel")
VOICE_TICK_SECONDS = 60  # voice XP is granted once per minute


def xp_for_level(level: int) -> int:
    """XP needed to advance from ``level`` to ``level + 1`` (MEE6 curve)."""
    return 5 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    """Cumulative XP required to *reach* ``level`` from zero."""
    total = 0
    for lvl in range(level):
        total += xp_for_level(lvl)
    return total


def level_from_xp(total_xp: int) -> int:
    """Level reached with ``total_xp`` cumulative XP."""
    level = 0
    remaining = int(total_xp)
    while remaining >= xp_for_level(level):
        remaining -= xp_for_level(level)
        level += 1
    return level


class _ConfirmView(discord.ui.View):
    """Small yes/no confirmation view restricted to the invoking user."""

    def __init__(self, author_id: int, lang: str) -> None:
        super().__init__(timeout=30)
        self.author_id = author_id
        self.value: Optional[bool] = None
        self.confirm.label = tr_lang(lang, "Bestätigen", "Confirm")
        self.cancel.label = tr_lang(lang, "Abbrechen", "Cancel")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.value = False
        self.stop()
        await interaction.response.defer()


class Leveling(commands.Cog):
    """MEE6-style leveling: XP for messages and voice, role rewards, leaderboard."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=741398265023487156, force_registration=True)
        self.config.register_guild(
            enabled=False,
            language="en-US",
            # -- message XP -------------------------------------------------- #
            xp_min=15,
            xp_max=25,
            cooldown=60,  # seconds between counted messages per member
            # -- voice XP ---------------------------------------------------- #
            voice_xp=5,  # XP per minute in voice; 0 disables voice XP
            voice_ignore_muted=True,
            voice_ignore_deafened=True,
            voice_ignore_alone=True,
            # -- level-up announcements -------------------------------------- #
            announce_mode="current",  # off | current | channel
            announce_channel=None,
            announce_message="🎉 {member}, you reached level **{level}**!",
            # -- role rewards ------------------------------------------------- #
            role_rewards={},  # {"<level>": role_id}
            stack_rewards=True,  # True = keep lower reward roles, False = replace
            # -- exclusions ---------------------------------------------------- #
            no_xp_channels=[],
            no_xp_roles=[],
        )
        self.config.register_member(xp=0, level=0)
        self._cooldowns: Dict[Tuple[int, int], float] = {}  # (guild_id, user_id) -> monotonic ts
        self._xp_locks: Dict[int, asyncio.Lock] = {}  # per-guild lock for XP writes
        self._voice_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        register_dashboard(self)
        self._voice_task = asyncio.create_task(self._voice_loop())

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._voice_task:
            self._voice_task.cancel()

    # ------------------------------------------------------------------ #
    # Red data APIs
    # ------------------------------------------------------------------ #
    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Delete the stored XP/level of ``user_id`` in every guild."""
        all_members = await self.config.all_members()
        for guild_id, members in all_members.items():
            if user_id in members:
                await self.config.member_from_ids(guild_id, user_id).clear()

    async def red_get_data_for_user(self, *, user_id: int) -> Dict[str, io.BytesIO]:
        """Return the stored XP/level of ``user_id`` per guild."""
        data = {}
        all_members = await self.config.all_members()
        for guild_id, members in all_members.items():
            if user_id in members:
                data[str(guild_id)] = members[user_id]
        if not data:
            return {}
        return {"user_data.json": io.BytesIO(json.dumps(data, indent=2).encode("utf-8"))}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _lang(self, guild: Optional[discord.Guild]) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    @staticmethod
    def _t(lang: str, de: str, en: str) -> str:
        return de if str(lang).lower().startswith("de") else en

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._xp_locks.get(guild_id)
        if lock is None:
            lock = self._xp_locks[guild_id] = asyncio.Lock()
        return lock

    async def _send_pages(self, ctx: commands.Context, pages: List[discord.Embed]) -> None:
        """Send one or more embed pages, with a paginated menu when possible."""
        if not pages:
            return
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
            return
        if SimpleMenu is not None:
            await SimpleMenu(pages).start(ctx)
        else:  # fallback: send the first few pages directly
            for page in pages[:3]:
                await ctx.send(embed=page)

    async def _add_xp(
        self,
        member: discord.Member,
        amount: int,
        gconf: dict,
        message_channel: Optional[discord.abc.Messageable] = None,
    ) -> None:
        """Add XP to a member, update the level and handle level-up effects."""
        async with self._lock(member.guild.id):
            mconf = self.config.member(member)
            xp = max(0, int(await mconf.xp()) + int(amount))
            old_level = int(await mconf.level())
            new_level = level_from_xp(xp)
            await mconf.xp.set(xp)
            if new_level != old_level:
                await mconf.level.set(new_level)
        if new_level > old_level:
            await self._handle_level_up(member, new_level, gconf, message_channel)
        elif new_level < old_level:
            # XP removal can lower the level; keep reward roles consistent.
            await self._apply_role_rewards(member, new_level, gconf)

    async def _handle_level_up(
        self,
        member: discord.Member,
        level: int,
        gconf: dict,
        message_channel: Optional[discord.abc.Messageable],
    ) -> None:
        """Announce a level-up and apply role rewards."""
        await self._apply_role_rewards(member, level, gconf)

        mode = str(gconf.get("announce_mode") or "current")
        channel: Optional[discord.abc.Messageable] = None
        if mode == "current":
            channel = message_channel
            if channel is None and gconf.get("announce_channel"):
                # Voice level-ups have no message channel; fall back if configured.
                channel = member.guild.get_channel(gconf["announce_channel"])
        elif mode == "channel" and gconf.get("announce_channel"):
            channel = member.guild.get_channel(gconf["announce_channel"])
        if channel is None:
            return
        perms = getattr(channel, "permissions_for", None)
        if perms is not None and not channel.permissions_for(member.guild.me).send_messages:
            return
        template = gconf.get("announce_message") or "🎉 {member}, you reached level **{level}**!"
        text = template.replace("{member}", member.mention).replace("{level}", str(level))
        try:
            await channel.send(text)
        except discord.HTTPException:
            pass

    async def _apply_role_rewards(self, member: discord.Member, level: int, gconf: dict) -> None:
        """Sync reward roles for ``member`` at ``level`` (stack or replace mode)."""
        rewards = gconf.get("role_rewards") or {}
        if not rewards:
            return
        guild = member.guild
        me = guild.me
        if not me.guild_permissions.manage_roles:
            return

        earned: List[Tuple[int, discord.Role]] = []  # (level, role) already reached
        all_reward_roles: List[discord.Role] = []
        for lvl_str, role_id in rewards.items():
            try:
                lvl = int(lvl_str)
            except (TypeError, ValueError):
                continue
            role = guild.get_role(int(role_id))
            if role is None or role >= me.top_role:  # hierarchy safety check
                continue
            all_reward_roles.append(role)
            if lvl <= level:
                earned.append((lvl, role))

        if gconf.get("stack_rewards", True):
            wanted = {r.id for _lvl, r in earned}
        else:
            # Replace mode: only the highest earned reward is kept.
            wanted = {max(earned, key=lambda e: e[0])[1].id} if earned else set()

        try:
            to_add = [r for r in all_reward_roles if r.id in wanted and r not in member.roles]
            to_remove = [r for r in all_reward_roles if r.id not in wanted and r in member.roles]
            if to_add:
                await member.add_roles(*to_add, reason="Leveling role reward")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Leveling role reward sync")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    async def _rank_position(self, guild: discord.Guild, member_id: int) -> Tuple[int, int]:
        """Return (rank, total ranked members) for ``member_id`` in ``guild``."""
        members = await self.config.all_members(guild)
        ranked = sorted(members.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
        total = len(ranked)
        for idx, (mid, _mconf) in enumerate(ranked, start=1):
            if mid == member_id:
                return idx, total
        return total + 1, total

    # ------------------------------------------------------------------ #
    # XP sources: messages + voice loop
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return
        gconf = await self.config.guild(message.guild).all()
        if not gconf.get("enabled"):
            return
        if message.channel.id in (gconf.get("no_xp_channels") or []):
            return
        no_xp_roles = set(gconf.get("no_xp_roles") or [])
        if no_xp_roles and any(r.id in no_xp_roles for r in message.author.roles):
            return
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        cooldown = max(0, int(gconf.get("cooldown", 60) or 0))
        last = self._cooldowns.get(key)
        if last is not None and now - last < cooldown:
            return
        self._cooldowns[key] = now
        xp_min = int(gconf.get("xp_min", 15) or 0)
        xp_max = max(xp_min, int(gconf.get("xp_max", 25) or 0))
        amount = random.randint(xp_min, xp_max) if xp_max > xp_min else xp_min
        if amount <= 0:
            return
        await self._add_xp(message.author, amount, gconf, message.channel)

    async def _voice_loop(self) -> None:
        """Grant voice XP once per minute to eligible members."""
        await self.bot.wait_until_red_ready()
        while True:
            await asyncio.sleep(VOICE_TICK_SECONDS)
            try:
                await self._voice_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Voice XP tick failed")

    async def _voice_tick(self) -> None:
        for guild in self.bot.guilds:
            try:
                if await self.bot.cog_disabled_in_guild(self, guild):
                    continue
                gconf = await self.config.guild(guild).all()
                if not gconf.get("enabled"):
                    continue
                amount = int(gconf.get("voice_xp", 5) or 0)
                if amount <= 0:
                    continue
                await self._voice_tick_guild(guild, gconf, amount)
            except Exception:
                log.exception("Voice XP tick failed for guild %s", guild.id)

    async def _voice_tick_guild(self, guild: discord.Guild, gconf: dict, amount: int) -> None:
        no_xp_channels = set(gconf.get("no_xp_channels") or [])
        no_xp_roles = set(gconf.get("no_xp_roles") or [])
        afk_channel_id = guild.afk_channel.id if guild.afk_channel else None
        for channel in guild.voice_channels:
            if channel.id in no_xp_channels or channel.id == afk_channel_id:
                continue
            eligible: List[discord.Member] = []
            for member in channel.members:
                if member.bot:
                    continue
                voice = member.voice
                if voice is None:
                    continue
                if gconf.get("voice_ignore_muted", True) and (voice.self_mute or voice.mute):
                    continue
                if gconf.get("voice_ignore_deafened", True) and (voice.self_deaf or voice.deaf):
                    continue
                if no_xp_roles and any(r.id in no_xp_roles for r in member.roles):
                    continue
                eligible.append(member)
            humans = sum(1 for m in channel.members if not m.bot)
            if gconf.get("voice_ignore_alone", True) and humans < 2:
                continue
            for member in eligible:
                await self._add_xp(member, amount, gconf, None)

    # ------------------------------------------------------------------ #
    # User commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="level", aliases=["rank", "xp"])
    @commands.guild_only()
    async def level(self, ctx: commands.Context) -> None:
        """Leveling commands."""

    @level.command(name="show", aliases=["card", "me"])
    @app_commands.describe(member="Member to look up (default: you)")
    async def level_show(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """Show a rank card with XP, level, progress and rank position."""
        lang = await self._lang(ctx.guild)
        member = member or ctx.author
        if member.bot:
            await ctx.send(self._t(lang, "Bots sammeln keine XP.", "Bots do not earn XP."))
            return
        mconf = await self.config.member(member).all()
        xp = int(mconf.get("xp", 0))
        level = level_from_xp(xp)
        floor = total_xp_for_level(level)
        needed = xp_for_level(level)
        progress = xp - floor
        rank, total = await self._rank_position(ctx.guild, member.id)

        # Simple text progress bar for the embed.
        filled = int(round(10 * progress / needed)) if needed else 0
        bar = "▰" * filled + "▱" * (10 - filled)

        e = discord.Embed(
            title=self._t(lang, f"Rang von {member.display_name}", f"Rank of {member.display_name}"),
            colour=await ctx.embed_colour(),
        )
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name=self._t(lang, "Level", "Level"), value=str(level), inline=True)
        e.add_field(name=self._t(lang, "Rang", "Rank"), value=f"#{rank}/{total}", inline=True)
        e.add_field(name="XP", value=f"{xp:,}", inline=True)
        e.add_field(
            name=self._t(lang, "Fortschritt", "Progress"),
            value=f"{bar} {progress:,}/{needed:,} XP",
            inline=False,
        )
        await ctx.send(embed=e)

    @level.command(name="leaderboard", aliases=["lb", "top"])
    async def level_leaderboard(self, ctx: commands.Context) -> None:
        """Show the server leaderboard (paginated)."""
        lang = await self._lang(ctx.guild)
        members = await self.config.all_members(ctx.guild)
        ranked = sorted(members.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
        entries = []
        for idx, (mid, mconf) in enumerate(ranked, start=1):
            xp = int(mconf.get("xp", 0))
            if xp <= 0:
                continue
            m = ctx.guild.get_member(mid)
            name = m.display_name if m else f"<{mid}>"
            entries.append(f"**#{idx}** {name} — Level {level_from_xp(xp)} · {xp:,} XP")
        if not entries:
            await ctx.send(self._t(lang, "Noch keine XP gesammelt.", "No XP earned yet."))
            return

        per_page = 10
        pages: List[discord.Embed] = []
        colour = await ctx.embed_colour()
        title = self._t(lang, "🏆 Rangliste", "🏆 Leaderboard")
        for i in range(0, len(entries), per_page):
            chunk = entries[i:i + per_page]
            e = discord.Embed(title=title, description="\n".join(chunk), colour=colour)
            e.set_footer(text=self._t(
                lang,
                f"Seite {i // per_page + 1}/{(len(entries) - 1) // per_page + 1} · {len(entries)} Einträge",
                f"Page {i // per_page + 1}/{(len(entries) - 1) // per_page + 1} · {len(entries)} entries",
            ))
            pages.append(e)
        await self._send_pages(ctx, pages)

    # ------------------------------------------------------------------ #
    # Admin configuration
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="levelset", aliases=["lvlset"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def levelset(self, ctx: commands.Context) -> None:
        """Configure the leveling module."""

    @levelset.command(name="enable")
    @app_commands.describe(on_off="Enable or disable leveling")
    async def levelset_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Leveling-Modul **{state}**.", f"Leveling module **{state}**."))

    @levelset.command(name="xp")
    @app_commands.describe(minimum="Minimum XP per message", maximum="Maximum XP per message")
    async def levelset_xp(self, ctx: commands.Context, minimum: int, maximum: int) -> None:
        """Set the XP range awarded per counted message."""
        lang = await self._lang(ctx.guild)
        if not (0 <= minimum <= maximum <= 1000):
            await ctx.send(self._t(lang, "Es muss 0 ≤ min ≤ max ≤ 1000 gelten.", "Requires 0 ≤ min ≤ max ≤ 1000."))
            return
        await self.config.guild(ctx.guild).xp_min.set(minimum)
        await self.config.guild(ctx.guild).xp_max.set(maximum)
        await ctx.send(self._t(
            lang,
            f"XP pro Nachricht: **{minimum}–{maximum}**.",
            f"XP per message: **{minimum}–{maximum}**.",
        ))

    @levelset.command(name="cooldown")
    @app_commands.describe(seconds="Seconds between counted messages (0 = no cooldown)")
    async def levelset_cooldown(self, ctx: commands.Context, seconds: int) -> None:
        """Set the per-member message XP cooldown."""
        lang = await self._lang(ctx.guild)
        if not 0 <= seconds <= 3600:
            await ctx.send(self._t(lang, "Sekunden müssen 0–3600 sein.", "Seconds must be 0–3600."))
            return
        await self.config.guild(ctx.guild).cooldown.set(seconds)
        await ctx.send(self._t(lang, f"Cooldown: **{seconds}s**.", f"Cooldown: **{seconds}s**."))

    @levelset.command(name="voicexp")
    @app_commands.describe(amount="XP per minute in voice (0 disables voice XP)")
    async def levelset_voicexp(self, ctx: commands.Context, amount: int) -> None:
        """Set the XP awarded per minute in voice channels."""
        lang = await self._lang(ctx.guild)
        if not 0 <= amount <= 1000:
            await ctx.send(self._t(lang, "Betrag muss 0–1000 sein.", "Amount must be 0–1000."))
            return
        await self.config.guild(ctx.guild).voice_xp.set(amount)
        if amount == 0:
            await ctx.send(self._t(lang, "Voice-XP deaktiviert.", "Voice XP disabled."))
        else:
            await ctx.send(self._t(lang, f"Voice-XP: **{amount}/min**.", f"Voice XP: **{amount}/min**."))

    @levelset.command(name="voiceignore")
    @app_commands.describe(
        muted="Ignore muted members", deafened="Ignore deafened members", alone="Ignore members who are alone"
    )
    async def levelset_voiceignore(
        self, ctx: commands.Context, muted: bool, deafened: bool, alone: bool
    ) -> None:
        """Configure which voice states are excluded from voice XP."""
        lang = await self._lang(ctx.guild)
        conf = self.config.guild(ctx.guild)
        await conf.voice_ignore_muted.set(muted)
        await conf.voice_ignore_deafened.set(deafened)
        await conf.voice_ignore_alone.set(alone)
        await ctx.send(self._t(
            lang,
            f"Voice-XP ignoriert: stumm={muted}, taub={deafened}, allein={alone}.",
            f"Voice XP ignores: muted={muted}, deafened={deafened}, alone={alone}.",
        ))

    @levelset.command(name="announce")
    @app_commands.describe(
        mode="off, current (same channel) or channel (fixed channel)",
        channel="Fixed announcement channel (for mode 'channel')",
    )
    async def levelset_announce(
        self, ctx: commands.Context, mode: str, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set the level-up announcement mode."""
        lang = await self._lang(ctx.guild)
        mode = mode.strip().lower()
        if mode not in ANNOUNCE_MODES:
            await ctx.send(self._t(
                lang,
                "Modus muss `off`, `current` oder `channel` sein.",
                "Mode must be `off`, `current` or `channel`.",
            ))
            return
        if mode == "channel" and channel is None:
            await ctx.send(self._t(
                lang,
                "Für Modus `channel` bitte einen Kanal angeben.",
                "Mode `channel` requires a channel.",
            ))
            return
        conf = self.config.guild(ctx.guild)
        await conf.announce_mode.set(mode)
        if channel is not None:
            await conf.announce_channel.set(channel.id)
        where = channel.mention if channel else mode
        await ctx.send(self._t(lang, f"Ankündigungen: **{where}**.", f"Announcements: **{where}**."))

    @levelset.command(name="message")
    @app_commands.describe(text="Message template — {member} and {level} are replaced")
    async def levelset_message(self, ctx: commands.Context, *, text: str) -> None:
        """Set the level-up message template."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).announce_message.set(text)
        await ctx.send(self._t(lang, "Nachricht gespeichert.", "Message saved."))

    @levelset.command(name="stack")
    @app_commands.describe(on_off="True = stack reward roles, False = keep only the highest")
    async def levelset_stack(self, ctx: commands.Context, on_off: bool) -> None:
        """Choose whether reward roles stack or replace each other."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).stack_rewards.set(on_off)
        await ctx.send(self._t(
            lang,
            "Belohnungsrollen werden **gestapelt**." if on_off else "Nur die **höchste** Belohnungsrolle bleibt.",
            "Reward roles are **stacked**." if on_off else "Only the **highest** reward role is kept.",
        ))

    @levelset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def levelset_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    @levelset.command(name="settings")
    async def levelset_settings(self, ctx: commands.Context) -> None:
        """Show the current leveling configuration."""
        lang = await self._lang(ctx.guild)
        gconf = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(gconf["announce_channel"]) if gconf.get("announce_channel") else None
        rewards = gconf.get("role_rewards") or {}
        reward_lines = []
        for lvl_str in sorted(rewards, key=lambda s: int(s)):
            role = ctx.guild.get_role(int(rewards[lvl_str]))
            reward_lines.append(f"Level {lvl_str}: {role.mention if role else rewards[lvl_str]}")
        no_xp_ch = ", ".join(f"<#{cid}>" for cid in gconf.get("no_xp_channels") or []) or "—"
        no_xp_r = ", ".join(f"<@&{rid}>" for rid in gconf.get("no_xp_roles") or []) or "—"
        e = discord.Embed(title=self._t(lang, "Leveling-Einstellungen", "Leveling settings"),
                          colour=await ctx.embed_colour())
        e.add_field(name=self._t(lang, "Aktiviert", "Enabled"), value=str(gconf["enabled"]), inline=True)
        e.add_field(name=self._t(lang, "XP/Nachricht", "XP/message"),
                    value=f"{gconf['xp_min']}–{gconf['xp_max']} ({gconf['cooldown']}s)", inline=True)
        e.add_field(name="Voice-XP", value=f"{gconf['voice_xp']}/min", inline=True)
        e.add_field(name=self._t(lang, "Ankündigung", "Announcement"),
                    value=f"{gconf['announce_mode']}" + (f" → {channel.mention}" if channel else ""), inline=True)
        e.add_field(name=self._t(lang, "Rollen stapeln", "Stack roles"), value=str(gconf["stack_rewards"]), inline=True)
        e.add_field(name=self._t(lang, "Sprache", "Language"), value=gconf["language"], inline=True)
        e.add_field(name=self._t(lang, "Belohnungen", "Rewards"),
                    value="\n".join(reward_lines) or "—", inline=False)
        e.add_field(name=self._t(lang, "Keine-XP-Kanäle", "No-XP channels"), value=no_xp_ch, inline=False)
        e.add_field(name=self._t(lang, "Keine-XP-Rollen", "No-XP roles"), value=no_xp_r, inline=False)
        await ctx.send(embed=e)

    # --- role rewards --------------------------------------------------- #
    @levelset.group(name="reward")
    async def levelset_reward(self, ctx: commands.Context) -> None:
        """Manage level → role rewards."""

    @levelset_reward.command(name="add")
    @app_commands.describe(level="Level at which the role is granted", role="Role to grant")
    async def reward_add(self, ctx: commands.Context, level: int, role: discord.Role) -> None:
        """Add or update a role reward for a level."""
        lang = await self._lang(ctx.guild)
        if not 1 <= level <= 1000:
            await ctx.send(self._t(lang, "Level muss 1–1000 sein.", "Level must be 1–1000."))
            return
        if role >= ctx.guild.me.top_role:
            await ctx.send(self._t(
                lang,
                "Diese Rolle liegt über meiner höchsten Rolle — ich kann sie nicht vergeben.",
                "That role is above my top role — I cannot assign it.",
            ))
            return
        async with self.config.guild(ctx.guild).role_rewards() as rewards:
            rewards[str(level)] = role.id
        await ctx.send(self._t(
            lang,
            f"Belohnung: Level **{level}** → {role.mention}",
            f"Reward: level **{level}** → {role.mention}",
        ))

    @levelset_reward.command(name="remove")
    @app_commands.describe(level="Level whose reward should be removed")
    async def reward_remove(self, ctx: commands.Context, level: int) -> None:
        """Remove the role reward for a level."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).role_rewards() as rewards:
            removed = rewards.pop(str(level), None)
        if removed is None:
            await ctx.send(self._t(lang, "Für dieses Level gibt es keine Belohnung.", "No reward for that level."))
        else:
            await ctx.send(self._t(lang, f"Belohnung für Level **{level}** entfernt.",
                                   f"Reward for level **{level}** removed."))

    @levelset_reward.command(name="list")
    async def reward_list(self, ctx: commands.Context) -> None:
        """List all role rewards."""
        lang = await self._lang(ctx.guild)
        rewards = await self.config.guild(ctx.guild).role_rewards()
        if not rewards:
            await ctx.send(self._t(lang, "Keine Belohnungen konfiguriert.", "No rewards configured."))
            return
        lines = []
        for lvl_str in sorted(rewards, key=lambda s: int(s)):
            role = ctx.guild.get_role(int(rewards[lvl_str]))
            lines.append(f"Level **{lvl_str}** → {role.mention if role else rewards[lvl_str]}")
        await ctx.send(embed=discord.Embed(
            title=self._t(lang, "Rollen-Belohnungen", "Role rewards"),
            description="\n".join(lines),
            colour=await ctx.embed_colour(),
        ))

    # --- no-XP lists ----------------------------------------------------- #
    @levelset.group(name="noxp")
    async def levelset_noxp(self, ctx: commands.Context) -> None:
        """Manage no-XP channels and roles."""

    @levelset_noxp.command(name="channel")
    @app_commands.describe(channel="Channel to toggle on the no-XP list")
    async def noxp_channel(self, ctx: commands.Context, channel: discord.abc.GuildChannel) -> None:
        """Toggle a channel on the no-XP list."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).no_xp_channels() as channels:
            if channel.id in channels:
                channels.remove(channel.id)
                added = False
            else:
                channels.append(channel.id)
                added = True
        if added:
            await ctx.send(self._t(lang, f"{channel.mention} gibt keine XP mehr.",
                                   f"{channel.mention} no longer grants XP."))
        else:
            await ctx.send(self._t(lang, f"{channel.mention} gibt wieder XP.",
                                   f"{channel.mention} grants XP again."))

    @levelset_noxp.command(name="role")
    @app_commands.describe(role="Role to toggle on the no-XP list")
    async def noxp_role(self, ctx: commands.Context, role: discord.Role) -> None:
        """Toggle a role on the no-XP list."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).no_xp_roles() as roles:
            if role.id in roles:
                roles.remove(role.id)
                added = False
            else:
                roles.append(role.id)
                added = True
        if added:
            await ctx.send(self._t(lang, f"{role.mention} sammelt keine XP mehr.",
                                   f"{role.mention} no longer earns XP."))
        else:
            await ctx.send(self._t(lang, f"{role.mention} sammelt wieder XP.",
                                   f"{role.mention} earns XP again."))

    # --- XP management ---------------------------------------------------- #
    @levelset.command(name="addxp")
    @app_commands.describe(member="Member to grant XP to", amount="Amount of XP to add")
    async def levelset_addxp(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        """Add XP to a member."""
        lang = await self._lang(ctx.guild)
        if member.bot or amount <= 0:
            await ctx.send(self._t(lang, "Ungültige Eingabe.", "Invalid input."))
            return
        gconf = await self.config.guild(ctx.guild).all()
        await self._add_xp(member, amount, gconf, ctx.channel)
        xp = await self.config.member(member).xp()
        await ctx.send(self._t(
            lang,
            f"**{amount:,}** XP zu {member.display_name} hinzugefügt (jetzt {xp:,} XP, Level {level_from_xp(xp)}).",
            f"Added **{amount:,}** XP to {member.display_name} (now {xp:,} XP, level {level_from_xp(xp)}).",
        ))

    @levelset.command(name="removexp")
    @app_commands.describe(member="Member to remove XP from", amount="Amount of XP to remove")
    async def levelset_removexp(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        """Remove XP from a member."""
        lang = await self._lang(ctx.guild)
        if member.bot or amount <= 0:
            await ctx.send(self._t(lang, "Ungültige Eingabe.", "Invalid input."))
            return
        gconf = await self.config.guild(ctx.guild).all()
        await self._add_xp(member, -amount, gconf, None)
        xp = await self.config.member(member).xp()
        await ctx.send(self._t(
            lang,
            f"**{amount:,}** XP von {member.display_name} entfernt (jetzt {xp:,} XP, Level {level_from_xp(xp)}).",
            f"Removed **{amount:,}** XP from {member.display_name} (now {xp:,} XP, level {level_from_xp(xp)}).",
        ))

    @levelset.command(name="setlevel")
    @app_commands.describe(member="Member to set the level for", level="Target level")
    async def levelset_setlevel(self, ctx: commands.Context, member: discord.Member, level: int) -> None:
        """Set a member's level directly (XP is set to the level floor)."""
        lang = await self._lang(ctx.guild)
        if member.bot or not 0 <= level <= 1000:
            await ctx.send(self._t(lang, "Level muss 0–1000 sein.", "Level must be 0–1000."))
            return
        target_xp = total_xp_for_level(level)
        async with self._lock(ctx.guild.id):
            await self.config.member(member).xp.set(target_xp)
            await self.config.member(member).level.set(level)
        gconf = await self.config.guild(ctx.guild).all()
        await self._apply_role_rewards(member, level, gconf)
        await ctx.send(self._t(
            lang,
            f"{member.display_name} ist jetzt Level **{level}** ({target_xp:,} XP).",
            f"{member.display_name} is now level **{level}** ({target_xp:,} XP).",
        ))

    @levelset.command(name="reset")
    @app_commands.describe(member="Member to reset (leave empty to reset the whole server)")
    async def levelset_reset(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """Reset a member's XP, or the whole server's XP (with confirmation)."""
        lang = await self._lang(ctx.guild)
        if member is not None:
            question = self._t(
                lang,
                f"XP von **{member.display_name}** wirklich zurücksetzen?",
                f"Really reset the XP of **{member.display_name}**?",
            )
        else:
            question = self._t(
                lang,
                "Wirklich **alle** XP dieses Servers zurücksetzen?",
                "Really reset **all** XP for this server?",
            )
        view = _ConfirmView(ctx.author.id, lang)
        msg = await ctx.send(question, view=view)
        await view.wait()
        if not view.value:
            await msg.edit(content=self._t(lang, "Abgebrochen.", "Cancelled."), view=None)
            return
        if member is not None:
            await self.config.member(member).clear()
            await msg.edit(content=self._t(
                lang,
                f"XP von {member.display_name} zurückgesetzt.",
                f"XP of {member.display_name} has been reset.",
            ), view=None)
        else:
            await self.config.clear_all_members(ctx.guild)
            await msg.edit(content=self._t(
                lang,
                "Alle XP dieses Servers wurden zurückgesetzt.",
                "All XP for this server has been reset.",
            ), view=None)

    # ------------------------------------------------------------------ #
    # Dashboard panel: guild settings
    # ------------------------------------------------------------------ #
    @dashboard_panel("leveling", L("Leveling", "Leveling"), mount="guild_settings", permission="guild_admin", order=60)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        return PanelSchema(
            description=tr_lang(lang, "XP, Level und Rollen-Belohnungen für diesen Server.",
                                "XP, levels and role rewards for this server."),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.number("xp_min", L("XP pro Nachricht (min)", "XP per message (min)"),
                             value=int(await conf.xp_min()), min=0, max=1000),
                Field.number("xp_max", L("XP pro Nachricht (max)", "XP per message (max)"),
                             value=int(await conf.xp_max()), min=0, max=1000),
                Field.number("cooldown", L("Cooldown (Sekunden)", "Cooldown (seconds)"),
                             value=int(await conf.cooldown()), min=0, max=3600),
                Field.number("voice_xp", L("Voice-XP pro Minute (0 = aus)", "Voice XP per minute (0 = off)"),
                             value=int(await conf.voice_xp()), min=0, max=1000),
                Field.switch("voice_ignore_muted", L("Stumme ignorieren", "Ignore muted"),
                             value=bool(await conf.voice_ignore_muted())),
                Field.switch("voice_ignore_deafened", L("Taube ignorieren", "Ignore deafened"),
                             value=bool(await conf.voice_ignore_deafened())),
                Field.switch("voice_ignore_alone", L("Alleinige ignorieren", "Ignore members who are alone"),
                             value=bool(await conf.voice_ignore_alone())),
                Field.select(
                    "announce_mode", L("Level-Up-Ankündigung", "Level-up announcement"),
                    [
                        {"value": "off", "label": L("Aus", "Off")},
                        {"value": "current", "label": L("Im selben Kanal", "Same channel")},
                        {"value": "channel", "label": L("Fester Kanal", "Fixed channel")},
                    ],
                    value=str(await conf.announce_mode() or "current"),
                ),
                Field.channel("announce_channel", L("Fester Ankündigungs-Kanal", "Fixed announcement channel"),
                              value=str(await conf.announce_channel() or "")),
                Field.textarea(
                    "announce_message",
                    L("Nachricht — {member} und {level} werden ersetzt",
                      "Message — {member} and {level} are replaced"),
                    value=str(await conf.announce_message() or ""),
                ),
                Field.switch("stack_rewards", L("Belohnungsrollen stapeln", "Stack reward roles"),
                             value=bool(await conf.stack_rewards())),
                Field.select(
                    "language", L("Sprache", "Language"),
                    [{"value": "de-DE", "label": "Deutsch"}, {"value": "en-US", "label": "English"}],
                    value=str(lang), reload_on_change=True,
                ),
            ],
        )

    @settings_panel.on_submit
    async def _save_settings(self, ctx, data):
        conf = self.config.guild(ctx.guild)
        lang = str(data.get("language", "en-US")).strip()
        if lang not in ("de-DE", "en-US"):
            lang = "en-US"

        # --- validation ------------------------------------------------- #
        errors = {}

        def _int(key, default):
            try:
                return int(data.get(key, default))
            except (TypeError, ValueError):
                return -1

        xp_min = _int("xp_min", 15)
        xp_max = _int("xp_max", 25)
        if not (0 <= xp_min <= 1000):
            errors["xp_min"] = tr_lang(lang, "Wert muss 0–1000 sein.", "Value must be 0–1000.")
        if not (0 <= xp_max <= 1000) or xp_max < max(xp_min, 0):
            errors["xp_max"] = tr_lang(lang, "Max muss ≥ Min und ≤ 1000 sein.", "Max must be ≥ min and ≤ 1000.")
        cooldown = _int("cooldown", 60)
        if not (0 <= cooldown <= 3600):
            errors["cooldown"] = tr_lang(lang, "Sekunden müssen 0–3600 sein.", "Seconds must be 0–3600.")
        voice_xp = _int("voice_xp", 5)
        if not (0 <= voice_xp <= 1000):
            errors["voice_xp"] = tr_lang(lang, "Wert muss 0–1000 sein.", "Value must be 0–1000.")
        announce_mode = str(data.get("announce_mode", "current")).strip()
        if announce_mode not in ANNOUNCE_MODES:
            errors["announce_mode"] = tr_lang(lang, "Ungültiger Modus.", "Invalid mode.")
        if errors:
            return SubmitResult.fail(tr_lang(lang, "Bitte Eingaben prüfen.", "Please check your input."), errors)

        # --- save --------------------------------------------------------- #
        await conf.enabled.set(bool(data.get("enabled")))
        await conf.xp_min.set(xp_min)
        await conf.xp_max.set(xp_max)
        await conf.cooldown.set(cooldown)
        await conf.voice_xp.set(voice_xp)
        await conf.voice_ignore_muted.set(bool(data.get("voice_ignore_muted")))
        await conf.voice_ignore_deafened.set(bool(data.get("voice_ignore_deafened")))
        await conf.voice_ignore_alone.set(bool(data.get("voice_ignore_alone")))
        await conf.announce_mode.set(announce_mode)
        ch = str(data.get("announce_channel") or "").strip()
        await (conf.announce_channel.set(int(ch)) if ch.isdigit() else conf.announce_channel.clear())
        msg = str(data.get("announce_message", "")).strip()
        if msg:
            await conf.announce_message.set(msg)
        await conf.stack_rewards.set(bool(data.get("stack_rewards")))
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard page: leaderboard (guild scope)
    # ------------------------------------------------------------------ #
    @dashboard_page(
        "leaderboard",
        L("Rangliste", "Leaderboard"),
        scope="guild",
        permission="guild_member",
        icon="trophy",
    )
    async def leaderboard_page(self, ctx):
        members = await self.config.all_members(ctx.guild)
        ranked = sorted(members.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
        rows = []
        for idx, (mid, mconf) in enumerate(ranked, start=1):
            xp = int(mconf.get("xp", 0))
            if xp <= 0:
                continue
            m = ctx.guild.get_member(mid)
            rows.append({
                "rank": f"#{idx}",
                "member": m.display_name if m else str(mid),
                "level": str(level_from_xp(xp)),
                "xp": f"{xp:,}",
            })

        comps = [
            Component.heading(L("Rangliste", "Leaderboard")),
            Component.text(L(
                f"{len(rows)} Mitglieder mit XP.",
                f"{len(rows)} members with XP.",
            )),
        ]
        if rows:
            comps.append(Component.table(
                columns=[
                    {"key": "rank", "label": L("Rang", "Rank")},
                    {"key": "member", "label": L("Mitglied", "Member")},
                    {"key": "level", "label": L("Level", "Level")},
                    {"key": "xp", "label": "XP"},
                ],
                rows=rows[:200],
                title=L("Top-Mitglieder", "Top members"),
            ))
        else:
            comps.append(Component.text(L("Noch keine XP gesammelt.", "No XP earned yet.")))
        return PageSchema(components=comps)
