"""StatChannels — keep channel names updated with live server stats.

Opt-in per guild (disabled by default). Bilingual output (DE/EN, default
en-US). Integrates with the PDC web dashboard via the resilient drop-in.

Templates support these placeholders:
  {members} {humans} {bots} {online} {online_pct} {boosts} {roles} {channels}

Channel renames are heavily rate limited by Discord (about 2 per 10 min per
channel). The cog therefore only renames when the rendered name actually
changed, spreads updates out, backs off per channel on 429 responses and the
per-guild refresh interval cannot go below 10 minutes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

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

PLACEHOLDERS = [
    "{members}", "{humans}", "{bots}", "{online}", "{online_pct}",
    "{boosts}", "{roles}", "{channels}",
]
_PLACEHOLDER_KEYS = {p.strip("{}") for p in PLACEHOLDERS}
_TOKEN_RE = re.compile(r"\{([a-zA-Z_]+)\}")

MIN_INTERVAL = 600  # seconds (Discord rename rate limits: ~2 per 10 min per channel)
RENAME_SPACING = 2.0  # seconds between individual channel renames
RATELIMIT_BACKOFF = 900  # seconds to skip a channel after a 429


class StatChannels(commands.Cog):
    """Live counter / stat voice channels."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x57A7_C44, force_registration=True)
        self.config.register_guild(
            enabled=False,
            channels={},  # {channel_id(str): "template"}
            language="en-US",
            interval=MIN_INTERVAL,  # per-guild refresh interval in seconds (min 600)
        )
        self._task: Optional[asyncio.Task] = None
        self._tick_lock = asyncio.Lock()  # parallel-safe (startup/reload)
        self._last_run: Dict[int, float] = {}  # guild_id -> last refresh ts
        self._backoff_until: Dict[int, float] = {}  # channel_id -> skip-until ts

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
        online_pct = round(online * 100 / humans) if humans else 0
        return {
            "members": guild.member_count or len(guild.members),
            "humans": humans,
            "bots": bots,
            "online": online,
            "online_pct": online_pct,
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

    @staticmethod
    def _unknown_placeholders(template: str) -> List[str]:
        """Return placeholder tokens in the template that are not supported."""
        return [t for t in _TOKEN_RE.findall(template) if t not in _PLACEHOLDER_KEYS]

    # ------------------------------------------------------------------ #
    # Background update loop
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                async with self._tick_lock:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("StatChannels tick failed")
            await asyncio.sleep(60)  # scheduler resolution; renames gated per guild

    async def _tick(self) -> None:
        now = time.time()
        guilds = await self.config.all_guilds()
        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            channels = gconf.get("channels") or {}
            if not channels:
                continue
            interval = max(MIN_INTERVAL, int(gconf.get("interval") or MIN_INTERVAL))
            if now - self._last_run.get(gid, 0.0) < interval:
                continue
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            self._last_run[gid] = now
            await self._refresh_guild(guild, channels)

    async def _refresh_guild(self, guild: discord.Guild, channels: dict) -> None:
        """Rename all stat channels of a guild, spread out and 429-aware."""
        stats = self._stats(guild)
        now = time.time()
        for cid, template in channels.items():
            ch = guild.get_channel(int(cid))
            if ch is None:
                continue
            if now < self._backoff_until.get(ch.id, 0.0):
                continue  # channel is rate limited — try again next cycle
            new_name = self._render(template, stats)
            if ch.name == new_name:
                continue  # only rename when the rendered name actually changed
            if not ch.permissions_for(guild.me).manage_channels:
                continue
            try:
                # discord.py silently queues renames hit by the 2/10min bucket;
                # a timeout keeps one stuck channel from blocking the loop.
                await asyncio.wait_for(
                    ch.edit(name=new_name, reason="StatChannels update"), timeout=10
                )
            except asyncio.TimeoutError:
                self._backoff_until[ch.id] = time.time() + RATELIMIT_BACKOFF
                log.warning("StatChannels: renaming %s timed out — backing off %ss", ch.id, RATELIMIT_BACKOFF)
            except discord.HTTPException as exc:
                if getattr(exc, "status", None) == 429:
                    self._backoff_until[ch.id] = time.time() + RATELIMIT_BACKOFF
                    log.warning("StatChannels: rate limited renaming %s — backing off %ss", ch.id, RATELIMIT_BACKOFF)
                else:
                    log.warning("StatChannels: renaming %s failed", ch.id, exc_info=True)
            # Spread renames so many stat channels don't burst the API.
            await asyncio.sleep(RENAME_SPACING)

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
        """Add/update a stat channel and its template (placeholders are validated)."""
        lang = await self._lang(ctx.guild)
        unknown = self._unknown_placeholders(template)
        if unknown:
            bad = ", ".join("{" + t + "}" for t in unknown)
            valid = " ".join(PLACEHOLDERS)
            await ctx.send(self._t(
                lang,
                f"Unbekannte Platzhalter: {bad}\nGültig: {valid}",
                f"Unknown placeholders: {bad}\nValid: {valid}",
            ))
            return
        async with self.config.guild(ctx.guild).channels() as chans:
            chans[str(channel.id)] = template
        await ctx.send(
            self._t(lang, f"Hinzugefügt: {channel.mention} → `{template}`", f"Added: {channel.mention} → `{template}`")
        )
        # Update immediately (best effort; rename may be rate limited).
        # A timeout keeps the command/interaction from hanging when discord.py
        # queues the rename behind the 2/10min bucket.
        new_name = self._render(template, self._stats(ctx.guild))
        if channel.name != new_name:
            try:
                await asyncio.wait_for(
                    channel.edit(name=new_name, reason="StatChannels"), timeout=10
                )
            except asyncio.TimeoutError:
                self._backoff_until[channel.id] = time.time() + RATELIMIT_BACKOFF
                await ctx.send(self._t(
                    lang,
                    "Discord bremst Umbenennungen gerade — der Name wird beim nächsten Durchlauf gesetzt.",
                    "Discord is rate limiting renames — the name will be applied on the next cycle.",
                ))
            except discord.HTTPException as exc:
                if getattr(exc, "status", None) == 429:
                    self._backoff_until[channel.id] = time.time() + RATELIMIT_BACKOFF
                    await ctx.send(self._t(
                        lang,
                        "Discord bremst Umbenennungen gerade — der Name wird beim nächsten Durchlauf gesetzt.",
                        "Discord is rate limiting renames — the name will be applied on the next cycle.",
                    ))

    @statchannels.command(name="remove")
    @app_commands.describe(channel="Channel to stop updating")
    async def sc_remove(self, ctx: commands.Context, channel: discord.abc.GuildChannel) -> None:
        """Remove a stat channel."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).channels() as chans:
            chans.pop(str(channel.id), None)
        await ctx.send(self._t(lang, "Entfernt.", "Removed."))

    @statchannels.command(name="interval")
    @app_commands.describe(minutes="Refresh interval in minutes (minimum 10)")
    async def sc_interval(self, ctx: commands.Context, minutes: int) -> None:
        """Set the refresh interval for this server (minutes, minimum 10)."""
        lang = await self._lang(ctx.guild)
        if minutes < 10:
            await ctx.send(self._t(
                lang,
                "Minimum sind 10 Minuten (Discord-Rate-Limit für Umbenennungen).",
                "The minimum is 10 minutes (Discord rename rate limit).",
            ))
            return
        await self.config.guild(ctx.guild).interval.set(minutes * 60)
        await ctx.send(self._t(lang, f"Intervall: {minutes} Min", f"Interval: {minutes} min"))

    @statchannels.command(name="list")
    async def sc_list(self, ctx: commands.Context) -> None:
        """List configured stat channels + available placeholders (paginated)."""
        lang = await self._lang(ctx.guild)
        chans = await self.config.guild(ctx.guild).channels()
        interval = max(MIN_INTERVAL, int(await self.config.guild(ctx.guild).interval() or MIN_INTERVAL))
        lines = []
        for cid, template in chans.items():
            ch = ctx.guild.get_channel(int(cid))
            lines.append(f"{ch.mention if ch else cid}: `{template}`")
        if not lines:
            lines = [self._t(lang, "—", "—")]
        ph = " ".join(PLACEHOLDERS)
        colour = await ctx.embed_colour()
        per_page = 15
        chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
        pages: List[discord.Embed] = []
        for idx, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(
                title=self._t(lang, "Stat-Channels", "Stat channels"),
                description="\n".join(chunk)[:4000],
                colour=colour,
            )
            embed.add_field(name=self._t(lang, "Platzhalter", "Placeholders"), value=ph, inline=False)
            embed.add_field(
                name=self._t(lang, "Intervall", "Interval"),
                value=f"{interval // 60} min",
                inline=False,
            )
            if len(chunks) > 1:
                embed.set_footer(text=self._t(lang, f"Seite {idx}/{len(chunks)}", f"Page {idx}/{len(chunks)}"))
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS, timeout=120)

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
        interval_min = max(MIN_INTERVAL, int(await conf.interval() or MIN_INTERVAL)) // 60
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Live-Statistik-Kanäle. Kanäle per Befehl `statchannels add` verwalten.\nAktuell:\n{listing}",
                f"Live stat channels. Manage channels via `statchannels add`.\nCurrent:\n{listing}",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.number("interval", L("Intervall (Min, min 10)", "Interval (min, minimum 10)"), value=interval_min),
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
        try:
            interval_min = int(data.get("interval") or 10)
        except (TypeError, ValueError):
            return SubmitResult.fail(tr_lang(lang, "Intervall muss eine Zahl sein.", "Interval must be a number."))
        if interval_min < 10:
            return SubmitResult.fail(tr_lang(lang, "Intervall-Minimum: 10 Minuten.", "Interval minimum: 10 minutes."))
        await conf.enabled.set(bool(data.get("enabled")))
        await conf.interval.set(interval_min * 60)
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))
