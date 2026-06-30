"""StatChannels — keep channel names updated with live server stats.

Opt-in per guild (disabled by default). Bilingual output (DE/EN). Integrates with
the PDC web dashboard (enable toggle + language) via the resilient drop-in.

Templates support these placeholders:
  {members} {humans} {bots} {online} {boosts} {roles} {channels}

Channel renames are heavily rate limited by Discord (about 2 per 10 min per
channel), so names are refreshed every ~10 minutes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red

from .pdc_dashboard import (
    Field,
    L,
    PanelSchema,
    SubmitResult,
    dashboard_panel,
    register_dashboard,
    tr_lang,
    unregister_dashboard,
)

log = logging.getLogger("red.pdc.statchannels")

PLACEHOLDERS = ["{members}", "{humans}", "{bots}", "{online}", "{boosts}", "{roles}", "{channels}"]


class StatChannels(commands.Cog):
    """Live counter / stat voice channels."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x57A7_C44, force_registration=True)
        self.config.register_guild(
            enabled=False,
            channels={},  # {channel_id(str): "template"}
            language="en-US",
        )
        self._task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        register_dashboard(self)
        self._task = asyncio.create_task(self._loop())

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._task:
            self._task.cancel()

    async def _lang(self, guild: Optional[discord.Guild]) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    @staticmethod
    def _t(lang: str, de: str, en: str) -> str:
        return de if str(lang).lower().startswith("de") else en

    # ------------------------------------------------------------------ #
    # Stats + rendering
    # ------------------------------------------------------------------ #
    @staticmethod
    def _stats(guild: discord.Guild) -> dict:
        humans = sum(1 for m in guild.members if not m.bot)
        bots = sum(1 for m in guild.members if m.bot)
        online = sum(
            1 for m in guild.members if m.status is not discord.Status.offline and not m.bot
        )
        return {
            "members": guild.member_count or len(guild.members),
            "humans": humans,
            "bots": bots,
            "online": online,
            "boosts": guild.premium_subscription_count or 0,
            "roles": len(guild.roles),
            "channels": len(guild.channels),
        }

    @classmethod
    def _render(cls, template: str, stats: dict) -> str:
        out = template
        for key, val in stats.items():
            out = out.replace("{" + key + "}", str(val))
        return out[:100]  # Discord channel name limit

    # ------------------------------------------------------------------ #
    # Background update loop
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("StatChannels tick failed")
            await asyncio.sleep(600)  # every 10 minutes (rename rate limits)

    async def _tick(self) -> None:
        guilds = await self.config.all_guilds()
        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            channels = gconf.get("channels") or {}
            if not channels:
                continue
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            stats = self._stats(guild)
            for cid, template in channels.items():
                ch = guild.get_channel(int(cid))
                if ch is None:
                    continue
                new_name = self._render(template, stats)
                if ch.name == new_name:
                    continue
                if not ch.permissions_for(guild.me).manage_channels:
                    continue
                try:
                    await ch.edit(name=new_name, reason="StatChannels update")
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="statchannels", aliases=["statchan"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def statchannels(self, ctx: commands.Context) -> None:
        """Configure live stat channels."""

    @statchannels.command(name="enable")
    @app_commands.describe(on_off="Enable or disable stat-channel updates")
    async def sc_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Stat-Channels **{state}**.", f"Stat channels **{state}**."))

    @statchannels.command(name="add")
    @app_commands.describe(channel="Channel to rename", template="Name template, e.g. 'Members: {members}'")
    async def sc_add(self, ctx: commands.Context, channel: discord.abc.GuildChannel, *, template: str) -> None:
        """Add/update a stat channel and its template."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).channels() as chans:
            chans[str(channel.id)] = template
        await ctx.send(
            self._t(lang, f"Hinzugefügt: {channel.mention} → `{template}`", f"Added: {channel.mention} → `{template}`")
        )
        # Update immediately.
        try:
            await channel.edit(name=self._render(template, self._stats(ctx.guild)), reason="StatChannels")
        except discord.HTTPException:
            pass

    @statchannels.command(name="remove")
    @app_commands.describe(channel="Channel to stop updating")
    async def sc_remove(self, ctx: commands.Context, channel: discord.abc.GuildChannel) -> None:
        """Remove a stat channel."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).channels() as chans:
            chans.pop(str(channel.id), None)
        await ctx.send(self._t(lang, "Entfernt.", "Removed."))

    @statchannels.command(name="list")
    async def sc_list(self, ctx: commands.Context) -> None:
        """List configured stat channels + available placeholders."""
        lang = await self._lang(ctx.guild)
        chans = await self.config.guild(ctx.guild).channels()
        lines = []
        for cid, template in chans.items():
            ch = ctx.guild.get_channel(int(cid))
            lines.append(f"{ch.mention if ch else cid}: `{template}`")
        body = "\n".join(lines) if lines else self._t(lang, "—", "—")
        ph = " ".join(PLACEHOLDERS)
        embed = discord.Embed(
            title=self._t(lang, "Stat-Channels", "Stat channels"),
            description=body,
            colour=await ctx.embed_colour(),
        )
        embed.add_field(name=self._t(lang, "Platzhalter", "Placeholders"), value=ph, inline=False)
        await ctx.send(embed=embed)

    @statchannels.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def sc_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("statchannels", L("Stat-Channels", "Stat channels"), mount="guild_settings", permission="guild_admin", order=60)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        chans = await conf.channels()
        listing = "\n".join(f"• {ctx.guild.get_channel(int(c))}: {t}" for c, t in chans.items()) or "—"
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Live-Statistik-Kanäle. Kanäle per Befehl `statchannels add` verwalten.\nAktuell:\n{listing}",
                f"Live stat channels. Manage channels via `statchannels add`.\nCurrent:\n{listing}",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
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
        await conf.enabled.set(bool(data.get("enabled")))
        lang = str(data.get("language", "en-US")).strip() or "en-US"
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))
