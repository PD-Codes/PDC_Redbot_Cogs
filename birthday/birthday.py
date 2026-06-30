"""Birthday cog — announce member birthdays + optional birthday role.

Opt-in per guild (disabled by default). Bilingual output (DE/EN) following a
per-guild language setting. Integrates with the PDC web dashboard (enable toggle
+ channel + language) via the resilient drop-in.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
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

log = logging.getLogger("red.pdc.birthday")

_DATE_RE = re.compile(r"^\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*\.?\s*$")  # DD.MM / DD-MM / DD/MM
_ISO_RE = re.compile(r"^\s*\d{4}-(\d{2})-(\d{2})\s*$")  # YYYY-MM-DD


def _parse_date(text: str) -> Optional[str]:
    """Return a normalised ``MM-DD`` string or None."""
    m = _ISO_RE.match(text or "")
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
    else:
        m = _DATE_RE.match(text or "")
        if not m:
            return None
        dd, mm = int(m.group(1)), int(m.group(2))
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    return f"{mm:02d}-{dd:02d}"


class Birthday(commands.Cog):
    """Birthday announcements with an optional birthday role."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xB17_4DA1, force_registration=True)
        self.config.register_guild(
            enabled=False,
            channel=None,
            role=None,
            message="🎉 {mention} has a birthday today — happy birthday!",
            hour=9,  # UTC hour at which to announce
            language="en-US",
            last_run=None,  # "YYYY-MM-DD" guard against double announcing
        )
        self.config.register_member(birthday=None)  # "MM-DD"
        self._task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        register_dashboard(self)
        self._task = asyncio.create_task(self._loop())

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._task:
            self._task.cancel()

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

    # ------------------------------------------------------------------ #
    # Background announcement loop
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Birthday tick failed")
            await asyncio.sleep(900)  # every 15 minutes

    async def _tick(self) -> None:
        now = datetime.datetime.utcnow()
        today = now.strftime("%m-%d")
        today_full = now.strftime("%Y-%m-%d")
        guilds = await self.config.all_guilds()
        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            members = await self.config.all_members(guild)
            today_ids = {mid for mid, mconf in members.items() if mconf.get("birthday") == today}

            # Keep the birthday role in sync (self-healing across restarts).
            role_id = gconf.get("role")
            role = guild.get_role(role_id) if role_id else None
            if role is not None and guild.me.guild_permissions.manage_roles:
                try:
                    for m in list(role.members):
                        if m.id not in today_ids:
                            await m.remove_roles(role, reason="Birthday over")
                    for mid in today_ids:
                        m = guild.get_member(mid)
                        if m is not None and role not in m.roles:
                            await m.add_roles(role, reason="Birthday")
                except discord.Forbidden:
                    pass

            # Announce once per day, at the configured UTC hour.
            if now.hour != int(gconf.get("hour", 9) or 9):
                continue
            if gconf.get("last_run") == today_full:
                continue
            await self.config.guild(guild).last_run.set(today_full)
            if not today_ids:
                continue
            channel = guild.get_channel(gconf.get("channel")) if gconf.get("channel") else None
            if channel is None or not channel.permissions_for(guild.me).send_messages:
                continue
            template = gconf.get("message") or "🎉 {mention} — happy birthday!"
            for mid in today_ids:
                m = guild.get_member(mid)
                if m is None:
                    continue
                text = template.replace("{mention}", m.mention).replace("{name}", m.display_name)
                try:
                    await channel.send(text)
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------------------ #
    # User commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="birthday", aliases=["bday"])
    @commands.guild_only()
    async def birthday(self, ctx: commands.Context) -> None:
        """Birthday commands."""

    @birthday.command(name="set")
    @app_commands.describe(date="Your birthday, e.g. 24.12 or 12-24")
    async def birthday_set(self, ctx: commands.Context, *, date: str) -> None:
        """Set your birthday (day + month)."""
        lang = await self._lang(ctx.guild)
        parsed = _parse_date(date)
        if parsed is None:
            await ctx.send(self._t(lang, "Ungültiges Datum. Beispiel: `24.12`", "Invalid date. Example: `24.12`"))
            return
        await self.config.member(ctx.author).birthday.set(parsed)
        mm, dd = parsed.split("-")
        await ctx.send(self._t(lang, f"Geburtstag gesetzt: **{dd}.{mm}**", f"Birthday set: **{dd}.{mm}**"))

    @birthday.command(name="remove")
    async def birthday_remove(self, ctx: commands.Context) -> None:
        """Remove your stored birthday."""
        lang = await self._lang(ctx.guild)
        await self.config.member(ctx.author).birthday.clear()
        await ctx.send(self._t(lang, "Geburtstag entfernt.", "Birthday removed."))

    @birthday.command(name="list")
    async def birthday_list(self, ctx: commands.Context) -> None:
        """Show upcoming birthdays."""
        lang = await self._lang(ctx.guild)
        members = await self.config.all_members(ctx.guild)
        entries = []
        for mid, mconf in members.items():
            bd = mconf.get("birthday")
            m = ctx.guild.get_member(mid)
            if bd and m is not None:
                mm, dd = bd.split("-")
                entries.append((bd, f"**{dd}.{mm}** — {m.display_name}"))
        if not entries:
            await ctx.send(self._t(lang, "Noch keine Geburtstage gespeichert.", "No birthdays stored yet."))
            return
        today = datetime.datetime.utcnow().strftime("%m-%d")
        entries.sort(key=lambda e: ((e[0] < today), e[0]))  # upcoming first
        lines = "\n".join(e[1] for e in entries[:25])
        title = self._t(lang, "🎂 Geburtstage", "🎂 Birthdays")
        await ctx.send(embed=discord.Embed(title=title, description=lines, colour=await ctx.embed_colour()))

    # ------------------------------------------------------------------ #
    # Admin configuration
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="birthdayset", aliases=["bdayset"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def birthdayset(self, ctx: commands.Context) -> None:
        """Configure the birthday module."""

    @birthdayset.command(name="enable")
    @app_commands.describe(on_off="Enable or disable birthday announcements")
    async def birthdayset_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Birthday-Modul **{state}**.", f"Birthday module **{state}**."))

    @birthdayset.command(name="channel")
    @app_commands.describe(channel="Announcement channel")
    async def birthdayset_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the announcement channel."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(self._t(lang, f"Kanal: {channel.mention}", f"Channel: {channel.mention}"))

    @birthdayset.command(name="role")
    @app_commands.describe(role="Birthday role (assigned on the day), or leave empty to clear")
    async def birthdayset_role(self, ctx: commands.Context, role: Optional[discord.Role] = None) -> None:
        """Set (or clear) the birthday role."""
        lang = await self._lang(ctx.guild)
        if role is None:
            await self.config.guild(ctx.guild).role.clear()
            await ctx.send(self._t(lang, "Geburtstagsrolle entfernt.", "Birthday role cleared."))
            return
        await self.config.guild(ctx.guild).role.set(role.id)
        await ctx.send(self._t(lang, f"Rolle: {role.mention}", f"Role: {role.mention}"))

    @birthdayset.command(name="hour")
    @app_commands.describe(hour="UTC hour (0-23) at which to announce")
    async def birthdayset_hour(self, ctx: commands.Context, hour: int) -> None:
        """Set the announcement hour (UTC)."""
        lang = await self._lang(ctx.guild)
        if not 0 <= hour <= 23:
            await ctx.send(self._t(lang, "Stunde muss 0–23 sein.", "Hour must be 0–23."))
            return
        await self.config.guild(ctx.guild).hour.set(hour)
        await ctx.send(self._t(lang, f"Ankündigung um **{hour:02d}:00 UTC**.", f"Announce at **{hour:02d}:00 UTC**."))

    @birthdayset.command(name="message")
    @app_commands.describe(text="Message template — {mention} and {name} are replaced")
    async def birthdayset_message(self, ctx: commands.Context, *, text: str) -> None:
        """Set the announcement message template."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).message.set(text)
        await ctx.send(self._t(lang, "Nachricht gespeichert.", "Message saved."))

    @birthdayset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def birthdayset_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("birthday", L("Geburtstage", "Birthdays"), mount="guild_settings", permission="guild_admin", order=50)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        return PanelSchema(
            description=tr_lang(lang, "Geburtstags-Ankündigungen für diesen Server.", "Birthday announcements for this server."),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.channel("channel", L("Ankündigungs-Kanal", "Announcement channel"), value=str(await conf.channel() or "")),
                Field.role("role", L("Geburtstagsrolle (optional)", "Birthday role (optional)"), value=str(await conf.role() or "")),
                Field.number("hour", L("Ankündigung um (Stunde, UTC)", "Announce at (hour, UTC)"), value=int(await conf.hour())),
                Field.textarea(
                    "message",
                    L("Nachricht — {mention} und {name} werden ersetzt", "Message — {mention} and {name} are replaced"),
                    value=str(await conf.message() or ""),
                ),
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
        role = str(data.get("role") or "").strip()
        await (conf.role.set(int(role)) if role.isdigit() else conf.role.clear())
        try:
            hour = int(data.get("hour", 9))
        except (TypeError, ValueError):
            hour = 9
        await conf.hour.set(max(0, min(23, hour)))
        msg = str(data.get("message", "")).strip()
        if msg:
            await conf.message.set(msg)
        lang = str(data.get("language", "en-US")).strip() or "en-US"
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))
