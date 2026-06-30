"""MemeGen — post memes on command or on a timer.

Fetches from meme-api.com (Reddit). ``meme`` posts on demand; an optional
interval auto-posts into a channel. Configurable subreddit sources. Opt-in per
guild, bilingual (DE/EN), web dashboard integration via the resilient drop-in.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import List, Optional

import aiohttp
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

log = logging.getLogger("red.pdc.memegen")

_API = "https://meme-api.com/gimme"


class MemeGen(commands.Cog):
    """Post memes on command or on a timer."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x3E3E_60, force_registration=True)
        self.config.register_guild(
            enabled=True,
            language="en-US",
            channel=None,
            interval=0,  # minutes (0 = no auto-posting)
            subreddits=[],
            last_post=0.0,
        )
        self._task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        register_dashboard(self)
        self._task = asyncio.create_task(self._loop())

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._task:
            self._task.cancel()

    @staticmethod
    def _t(lang: str, de: str, en: str) -> str:
        return de if str(lang).lower().startswith("de") else en

    async def _lang(self, guild) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    # ------------------------------------------------------------------ #
    # Fetching
    # ------------------------------------------------------------------ #
    async def _fetch(self, subreddit: Optional[str]) -> Optional[dict]:
        url = _API + (f"/{subreddit}" if subreddit else "")
        for _ in range(3):  # retry to skip NSFW/spoiler results
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status != 200:
                            return None
                        js = await r.json()
            except Exception:
                return None
            if not js or js.get("nsfw") or js.get("spoiler") or not js.get("url"):
                continue
            return js
        return None

    def _embed(self, js: dict) -> discord.Embed:
        e = discord.Embed(title=(js.get("title") or "Meme")[:256], url=js.get("postLink"), colour=discord.Colour.blurple())
        e.set_image(url=js.get("url"))
        sub = js.get("subreddit")
        if sub:
            e.set_footer(text=f"r/{sub}")
        return e

    # ------------------------------------------------------------------ #
    # Auto-post loop
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("MemeGen tick failed")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        now = time.time()
        guilds = await self.config.all_guilds()
        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            interval = int(gconf.get("interval", 0) or 0)
            if interval <= 0 or not gconf.get("channel"):
                continue
            if now - float(gconf.get("last_post", 0)) < interval * 60:
                continue
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            channel = guild.get_channel(gconf.get("channel"))
            if channel is None or not channel.permissions_for(guild.me).send_messages:
                continue
            subs = gconf.get("subreddits") or []
            js = await self._fetch(random.choice(subs) if subs else None)
            await self.config.guild(guild).last_post.set(now)
            if js:
                try:
                    await channel.send(embed=self._embed(js))
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_command(name="meme")
    @commands.guild_only()
    @app_commands.describe(subreddit="Optional subreddit (e.g. memes, dankmemes)")
    async def meme(self, ctx: commands.Context, subreddit: Optional[str] = None) -> None:
        """Post a random meme."""
        lang = await self._lang(ctx.guild)
        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send(self._t(lang, "Meme-Modul ist deaktiviert.", "Meme module is disabled."))
            return
        await ctx.typing()
        if subreddit is None:
            subs = await self.config.guild(ctx.guild).subreddits()
            subreddit = random.choice(subs) if subs else None
        js = await self._fetch(subreddit)
        if not js:
            await ctx.send(self._t(lang, "Kein Meme bekommen, versuch's nochmal.", "Couldn't get a meme, try again."))
            return
        await ctx.send(embed=self._embed(js))

    @commands.hybrid_group(name="memeset")
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def memeset(self, ctx: commands.Context) -> None:
        """Configure the meme module."""

    @memeset.command(name="enable")
    @app_commands.describe(on_off="Enable or disable memes")
    async def m_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Memes **{state}**.", f"Memes **{state}**."))

    @memeset.command(name="channel")
    @app_commands.describe(channel="Auto-post channel (omit to clear)")
    async def m_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Set/clear the auto-post channel."""
        lang = await self._lang(ctx.guild)
        if channel is None:
            await self.config.guild(ctx.guild).channel.clear()
            await ctx.send(self._t(lang, "Auto-Post-Kanal entfernt.", "Auto-post channel cleared."))
            return
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(self._t(lang, f"Auto-Post-Kanal: {channel.mention}", f"Auto-post channel: {channel.mention}"))

    @memeset.command(name="interval")
    @app_commands.describe(minutes="Auto-post interval in minutes (0 = off)")
    async def m_interval(self, ctx: commands.Context, minutes: int) -> None:
        """Set the auto-post interval (minutes; 0 = off)."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).interval.set(max(0, minutes))
        await ctx.send(self._t(lang, f"Intervall: {max(0, minutes)} Min", f"Interval: {max(0, minutes)} min"))

    @memeset.command(name="subreddit")
    @app_commands.describe(name="Subreddit to toggle as a source")
    async def m_subreddit(self, ctx: commands.Context, name: str) -> None:
        """Toggle a subreddit as a meme source."""
        lang = await self._lang(ctx.guild)
        name = name.lstrip("r/").strip().lower()
        async with self.config.guild(ctx.guild).subreddits() as subs:
            if name in subs:
                subs.remove(name)
                msg = self._t(lang, f"r/{name} entfernt.", f"r/{name} removed.")
            else:
                subs.append(name)
                msg = self._t(lang, f"r/{name} hinzugefügt.", f"r/{name} added.")
        await ctx.send(msg)

    @memeset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def m_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("memegen", L("Memes", "Memes"), mount="guild_settings", permission="guild_admin", order=75)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        subs = await conf.subreddits()
        return PanelSchema(
            description=tr_lang(
                lang,
                "Memes von meme-api.com. Auto-Post optional über Kanal + Intervall.",
                "Memes from meme-api.com. Optional auto-posting via channel + interval.",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.channel("channel", L("Auto-Post-Kanal", "Auto-post channel"), value=str(await conf.channel() or "")),
                Field.number("interval", L("Intervall (Min, 0 = aus)", "Interval (min, 0 = off)"), value=int(await conf.interval())),
                Field.textarea("subreddits", L("Subreddits (eine pro Zeile)", "Subreddits (one per line)"), value="\n".join(subs)),
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
        ch = str(data.get("channel") or "").strip()
        await (conf.channel.set(int(ch)) if ch.isdigit() else conf.channel.clear())
        try:
            interval = int(data.get("interval", 0))
        except (TypeError, ValueError):
            interval = 0
        await conf.interval.set(max(0, interval))
        raw = str(data.get("subreddits") or "")
        subs = [ln.strip().lstrip("r/").lower() for ln in raw.splitlines() if ln.strip()]
        await conf.subreddits.set(subs)
        lang = str(data.get("language", "en-US")).strip() or "en-US"
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))
