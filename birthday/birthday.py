"""Birthday cog — announce member birthdays + optional birthday role.

Opt-in per guild (disabled by default). Bilingual output (DE/EN) following a
per-guild language setting (default: en-US). Announcements run at a configurable
hour in the guild's configured IANA timezone (default: UTC). Integrates with the
PDC web dashboard (settings panel + birthday calendar page) via the resilient
drop-in.
"""
from __future__ import annotations

import asyncio
import calendar
import csv
import datetime
import io
import json
import logging
import re
from typing import Dict, List, Optional, Set

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

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

log = logging.getLogger("red.pdc.birthday")  # module logger

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
    if not (1 <= mm <= 12):
        return None
    max_day = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mm - 1]
    if not (1 <= dd <= max_day):
        return None
    return f"{mm:02d}-{dd:02d}"


def _csv_safe(value) -> str:
    """Neutralise spreadsheet formula injection in exported cells."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _role_problem(guild: discord.Guild, role: discord.Role, lang: str) -> Optional[str]:
    """Return an error text when `role` must not be used as birthday role."""
    t = Birthday._t
    if role.is_default():
        return t(lang, "@everyone kann nicht verwendet werden.", "@everyone cannot be used.")
    if role.managed:
        return t(lang, "Verwaltete Rollen (Bots/Integrationen/Booster) sind nicht erlaubt.",
                 "Managed roles (bots/integrations/boosters) are not allowed.")
    if role.permissions.administrator:
        return t(lang, "Rollen mit Administrator-Recht sind nicht erlaubt.",
                 "Roles with the Administrator permission are not allowed.")
    me = guild.me
    if me is None or role >= me.top_role:
        return t(lang, "Die Rolle liegt nicht unter der höchsten Rolle des Bots.",
                 "The role is not below the bot's highest role.")
    return None


def _tzinfo(name: Optional[str]):
    """Resolve an IANA timezone name to a tzinfo (fallback: UTC)."""
    if ZoneInfo is not None and name:
        try:
            return ZoneInfo(str(name))
        except Exception:
            pass
    return datetime.timezone.utc


def _valid_tz(name: str) -> bool:
    if ZoneInfo is None:
        return str(name).upper() == "UTC"
    try:
        ZoneInfo(str(name))
        return True
    except Exception:
        return False


def _keys_for_date(d: datetime.date, feb29_mode: str) -> Set[str]:
    """Birthday keys (``MM-DD``) that count as *today* on date ``d``.

    In non-leap years a Feb-29 birthday is celebrated on Feb 28 or Mar 1,
    depending on ``feb29_mode`` ("feb28" | "mar1").
    """
    keys = {d.strftime("%m-%d")}
    if not calendar.isleap(d.year):
        if feb29_mode == "feb28" and (d.month, d.day) == (2, 28):
            keys.add("02-29")
        elif feb29_mode != "feb28" and (d.month, d.day) == (3, 1):
            keys.add("02-29")
    return keys


def _days_until(birthday: str, today: datetime.date, feb29_mode: str) -> int:
    """Days from ``today`` until the next occurrence of ``birthday`` (MM-DD)."""
    try:
        mm, dd = (int(x) for x in birthday.split("-"))
    except (ValueError, AttributeError):
        return 9999
    for year in (today.year, today.year + 1):
        try:
            cand = datetime.date(year, mm, dd)
        except ValueError:
            # Feb 29 in a non-leap year -> mapped replacement date.
            if (mm, dd) == (2, 29):
                cand = datetime.date(year, 2, 28) if feb29_mode == "feb28" else datetime.date(year, 3, 1)
            else:
                return 9999
        if cand >= today:
            return (cand - today).days
    return 9999


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
            hour=9,  # guild-local hour at which to announce
            language="en-US",
            last_run=None,  # "YYYY-MM-DD" guard against double announcing
            # -- new keys (backward compatible defaults) --------------------- #
            timezone="UTC",  # IANA timezone name used for all date/hour logic
            feb29_mode="mar1",  # celebrate Feb-29 birthdays on "feb28" or "mar1"
            reminder_days=0,  # 0 = off; N = announce upcoming birthdays N days ahead
            last_reminder=None,  # "YYYY-MM-DD" guard for the reminder
            # Members the bot itself gave the birthday role to; only these are
            # ever un-assigned again: {"role": role_id, "members": [ids]}
            role_holders={},
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
    # Red end-user data API
    # ------------------------------------------------------------------ #
    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        all_members = await self.config.all_members()
        for gid, members in all_members.items():
            if user_id in members:
                await self.config.member_from_ids(gid, user_id).clear()
        all_guilds = await self.config.all_guilds()
        for gid, gconf in all_guilds.items():
            holders = gconf.get("role_holders") or {}
            if user_id in (holders.get("members") or []):
                holders["members"] = [m for m in holders["members"] if m != user_id]
                await self.config.guild_from_id(gid).role_holders.set(holders)

    async def red_get_data_for_user(self, *, user_id: int) -> Dict[str, io.BytesIO]:
        all_members = await self.config.all_members()
        data = {
            str(gid): {"birthday": members[user_id].get("birthday")}
            for gid, members in all_members.items()
            if user_id in members and members[user_id].get("birthday")
        }
        if not data:
            return {}
        return {"birthday.json": io.BytesIO(json.dumps(data, indent=2).encode("utf-8"))}

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
        guilds = await self.config.all_guilds()
        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            if await self.bot.cog_disabled_in_guild(self, guild):
                continue
            try:
                await self._tick_guild(guild, gconf)
            except Exception:
                log.exception("Birthday tick failed for guild %s", gid)

    async def _tick_guild(self, guild: discord.Guild, gconf: dict) -> None:
        tz = _tzinfo(gconf.get("timezone") or "UTC")
        now_local = datetime.datetime.now(tz)
        today = now_local.date()
        feb29_mode = str(gconf.get("feb29_mode") or "mar1")
        today_keys = _keys_for_date(today, feb29_mode)

        members = await self.config.all_members(guild)
        today_ids = {mid for mid, mconf in members.items() if mconf.get("birthday") in today_keys}

        await self._sync_role(guild, gconf, today_ids)

        # Announce once per day, at (or - catching up after downtime - after)
        # the configured guild-local hour. 0 is a valid hour.
        hour = gconf.get("hour")
        hour = 9 if hour is None else int(hour)
        if now_local.hour < hour:
            return
        today_full = today.isoformat()
        lang = str(gconf.get("language") or "en-US")
        channel = guild.get_channel(gconf.get("channel")) if gconf.get("channel") else None
        can_send = channel is not None and channel.permissions_for(guild.me).send_messages

        if gconf.get("last_run") != today_full:
            await self.config.guild(guild).last_run.set(today_full)
            if today_ids and can_send:
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

        # Optional advance reminder N days ahead.
        reminder_days = int(gconf.get("reminder_days") or 0)
        if reminder_days > 0 and gconf.get("last_reminder") != today_full and can_send:
            await self.config.guild(guild).last_reminder.set(today_full)
            target = today + datetime.timedelta(days=reminder_days)
            target_keys = _keys_for_date(target, feb29_mode)
            upcoming = []
            for mid, mconf in members.items():
                if mconf.get("birthday") in target_keys:
                    m = guild.get_member(mid)
                    if m is not None:
                        upcoming.append(m.display_name)
            if upcoming:
                names = ", ".join(sorted(upcoming))
                date_str = target.strftime("%d.%m.")
                try:
                    await channel.send(self._t(
                        lang,
                        f"🎂 In **{reminder_days}** Tag(en) ({date_str}) haben Geburtstag: {names}",
                        f"🎂 Birthday(s) in **{reminder_days}** day(s) ({date_str}): {names}",
                    ))
                except discord.HTTPException:
                    pass

    async def _sync_role(self, guild: discord.Guild, gconf: dict, today_ids: Set[int]) -> None:
        """Keep the birthday role in sync (self-healing across restarts).

        Only members the bot gave the role to itself (tracked in
        ``role_holders``) are ever un-assigned; manually assigned holders of
        the same role are left untouched."""
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return
        role_id = gconf.get("role")
        role = guild.get_role(role_id) if role_id else None
        stored = gconf.get("role_holders") or {}
        holders: Set[int] = set(stored.get("members") or [])
        changed = False
        if stored.get("role") != role_id:
            # Role was changed/cleared: take the old role back from our holders.
            old = guild.get_role(stored.get("role")) if stored.get("role") else None
            if old is not None and old < me.top_role:
                for mid in holders:
                    m = guild.get_member(mid)
                    if m is not None and old in m.roles:
                        try:
                            await m.remove_roles(old, reason="Birthday role changed")
                        except discord.HTTPException:
                            pass
            holders = set()
            changed = True
        if role is not None and _role_problem(guild, role, "en-US") is None:
            try:
                for mid in list(holders):
                    if mid in today_ids:
                        continue
                    m = guild.get_member(mid)
                    if m is not None and role in m.roles:
                        await m.remove_roles(role, reason="Birthday over")
                    holders.discard(mid)
                    changed = True
                for mid in today_ids:
                    m = guild.get_member(mid)
                    if m is not None and role not in m.roles:
                        await m.add_roles(role, reason="Birthday")
                        holders.add(mid)
                        changed = True
            except discord.HTTPException:
                pass
        if changed:
            await self.config.guild(guild).role_holders.set(
                {"role": role_id, "members": sorted(holders)}
            )

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
        """Show all birthdays, sorted by how soon they occur (paginated)."""
        lang = await self._lang(ctx.guild)
        gconf = await self.config.guild(ctx.guild).all()
        tz = _tzinfo(gconf.get("timezone") or "UTC")
        today = datetime.datetime.now(tz).date()
        feb29_mode = str(gconf.get("feb29_mode") or "mar1")

        members = await self.config.all_members(ctx.guild)
        entries = []
        for mid, mconf in members.items():
            bd = mconf.get("birthday")
            m = ctx.guild.get_member(mid)
            if bd and m is not None:
                days = _days_until(bd, today, feb29_mode)
                mm, dd = bd.split("-")
                when = self._t(lang, "heute 🎉", "today 🎉") if days == 0 else self._t(
                    lang, f"in {days} Tag(en)", f"in {days} day(s)"
                )
                entries.append((days, bd, f"**{dd}.{mm}** — {m.display_name} · {when}"))
        if not entries:
            await ctx.send(self._t(lang, "Noch keine Geburtstage gespeichert.", "No birthdays stored yet."))
            return
        entries.sort(key=lambda e: (e[0], e[1]))

        per_page = 15
        pages: List[discord.Embed] = []
        colour = await ctx.embed_colour()
        title = self._t(lang, "🎂 Geburtstage", "🎂 Birthdays")
        for i in range(0, len(entries), per_page):
            chunk = entries[i:i + per_page]
            e = discord.Embed(title=title, description="\n".join(x[2] for x in chunk), colour=colour)
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
    @commands.hybrid_group(name="birthdayset", aliases=["bdayset"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def birthdayset(self, ctx: commands.Context) -> None:
        """Configure the birthday module."""

    @birthdayset.command(name="enable")
    @app_commands.describe(on_off="Enable or disable birthday announcements")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Birthday-Modul **{state}**.", f"Birthday module **{state}**."))

    @birthdayset.command(name="channel")
    @app_commands.describe(channel="Announcement channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the announcement channel."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(self._t(lang, f"Kanal: {channel.mention}", f"Channel: {channel.mention}"))

    @birthdayset.command(name="role")
    @app_commands.describe(role="Birthday role (assigned on the day), or leave empty to clear")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_role(self, ctx: commands.Context, role: Optional[discord.Role] = None) -> None:
        """Set (or clear) the birthday role."""
        lang = await self._lang(ctx.guild)
        if role is None:
            await self.config.guild(ctx.guild).role.clear()
            await ctx.send(self._t(lang, "Geburtstagsrolle entfernt.", "Birthday role cleared."))
            return
        problem = _role_problem(ctx.guild, role, lang)
        if problem is None and ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            problem = self._t(lang, "Die Rolle liegt nicht unter deiner höchsten Rolle.",
                              "The role is not below your highest role.")
        if problem:
            await ctx.send(problem, allowed_mentions=discord.AllowedMentions.none())
            return
        await self.config.guild(ctx.guild).role.set(role.id)
        msg = self._t(lang, f"Rolle: {role.mention}", f"Role: {role.mention}")
        if role.members:
            msg += "\n" + self._t(
                lang,
                f"⚠️ Die Rolle hat bereits **{len(role.members)}** Mitglied(er). Diese behalten sie; "
                "der Bot entfernt die Rolle nur bei Mitgliedern, denen er sie selbst gegeben hat.",
                f"⚠️ The role already has **{len(role.members)}** member(s). They keep it; "
                "the bot only removes the role from members it assigned it to itself.",
            )
        await ctx.send(msg, allowed_mentions=discord.AllowedMentions.none())

    @birthdayset.command(name="hour")
    @app_commands.describe(hour="Guild-local hour (0-23) at which to announce")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_hour(self, ctx: commands.Context, hour: int) -> None:
        """Set the announcement hour (in the guild's timezone)."""
        lang = await self._lang(ctx.guild)
        if not 0 <= hour <= 23:
            await ctx.send(self._t(lang, "Stunde muss 0–23 sein.", "Hour must be 0–23."))
            return
        await self.config.guild(ctx.guild).hour.set(hour)
        tz_name = await self.config.guild(ctx.guild).timezone()
        await ctx.send(self._t(
            lang,
            f"Ankündigung um **{hour:02d}:00** ({tz_name}).",
            f"Announce at **{hour:02d}:00** ({tz_name}).",
        ))

    @birthdayset.command(name="timezone", aliases=["tz"])
    @app_commands.describe(timezone="IANA timezone name, e.g. Europe/Berlin (default: UTC)")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_timezone(self, ctx: commands.Context, timezone: str) -> None:
        """Set the guild timezone (IANA name) used for announcements."""
        lang = await self._lang(ctx.guild)
        timezone = timezone.strip()
        if not _valid_tz(timezone):
            await ctx.send(self._t(
                lang,
                "Unbekannte Zeitzone. Beispiel: `Europe/Berlin`, `America/New_York`, `UTC`.",
                "Unknown timezone. Example: `Europe/Berlin`, `America/New_York`, `UTC`.",
            ))
            return
        await self.config.guild(ctx.guild).timezone.set(timezone)
        now = datetime.datetime.now(_tzinfo(timezone)).strftime("%H:%M")
        await ctx.send(self._t(
            lang,
            f"Zeitzone: **{timezone}** (aktuell {now}).",
            f"Timezone: **{timezone}** (currently {now}).",
        ))

    @birthdayset.command(name="feb29")
    @app_commands.describe(mode="When to celebrate Feb-29 birthdays in non-leap years: feb28 or mar1")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_feb29(self, ctx: commands.Context, mode: str) -> None:
        """Set how Feb-29 birthdays are handled in non-leap years."""
        lang = await self._lang(ctx.guild)
        mode = mode.strip().lower()
        if mode not in ("feb28", "mar1"):
            await ctx.send(self._t(lang, "Modus muss `feb28` oder `mar1` sein.", "Mode must be `feb28` or `mar1`."))
            return
        await self.config.guild(ctx.guild).feb29_mode.set(mode)
        human = "28.02." if mode == "feb28" else "01.03."
        await ctx.send(self._t(
            lang,
            f"29.02.-Geburtstage werden in Nicht-Schaltjahren am **{human}** gefeiert.",
            f"Feb-29 birthdays are celebrated on **{human}** in non-leap years.",
        ))

    @birthdayset.command(name="reminder")
    @app_commands.describe(days="Days of advance notice (0 disables the reminder)")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_reminder(self, ctx: commands.Context, days: int) -> None:
        """Announce upcoming birthdays N days in advance (0 = off)."""
        lang = await self._lang(ctx.guild)
        if not 0 <= days <= 60:
            await ctx.send(self._t(lang, "Tage müssen 0–60 sein.", "Days must be 0–60."))
            return
        await self.config.guild(ctx.guild).reminder_days.set(days)
        if days == 0:
            await ctx.send(self._t(lang, "Vorab-Erinnerung deaktiviert.", "Advance reminder disabled."))
        else:
            await ctx.send(self._t(
                lang,
                f"Vorab-Erinnerung **{days}** Tag(e) vorher.",
                f"Advance reminder **{days}** day(s) ahead.",
            ))

    @birthdayset.command(name="message")
    @app_commands.describe(text="Message template — {mention} and {name} are replaced")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_message(self, ctx: commands.Context, *, text: str) -> None:
        """Set the announcement message template."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).message.set(text)
        await ctx.send(self._t(lang, "Nachricht gespeichert.", "Message saved."))

    @birthdayset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    @birthdayset.command(name="export")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_export(self, ctx: commands.Context) -> None:
        """Export all stored birthdays as a CSV file."""
        lang = await self._lang(ctx.guild)
        members = await self.config.all_members(ctx.guild)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\n")
        w.writerow(["UserID", "Username", "Birthday"])
        count = 0
        for mid, mconf in sorted(members.items()):
            bd = mconf.get("birthday")
            if not bd:
                continue
            m = ctx.guild.get_member(mid)
            w.writerow([str(mid), _csv_safe(m.name if m else ""), _csv_safe(bd)])
            count += 1
        if count == 0:
            await ctx.send(self._t(lang, "Keine Geburtstage gespeichert.", "No birthdays stored."))
            return
        buf.seek(0)
        file = discord.File(
            io.BytesIO(buf.getvalue().encode("utf-8-sig")),
            filename=f"birthdays_{ctx.guild.id}.csv",
        )
        await ctx.send(
            self._t(lang, f"Export mit **{count}** Einträgen.", f"Export with **{count}** entries."),
            file=file,
        )

    @birthdayset.command(name="import")
    @app_commands.describe(file="CSV file: UserID;Birthday (MM-DD or DD.MM.) per line")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthdayset_import(self, ctx: commands.Context, file: discord.Attachment) -> None:
        """Import birthdays from a CSV file (UserID;Birthday)."""
        lang = await self._lang(ctx.guild)
        try:
            raw = (await file.read()).decode("utf-8-sig", errors="replace")
        except discord.HTTPException:
            await ctx.send(self._t(lang, "Datei konnte nicht gelesen werden.", "Could not read the file."))
            return
        imported = skipped = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in re.split(r"[;,]", line)]
            if len(parts) < 2 or not parts[0].isdigit():
                continue  # header or malformed row
            # Accept both 2-column (UserID;Birthday) and export format (UserID;Username;Birthday).
            date_field = parts[-1] if len(parts) >= 3 else parts[1]
            parsed = _parse_date(date_field)
            member = ctx.guild.get_member(int(parts[0]))
            if parsed is None or member is None:
                skipped += 1
                continue
            await self.config.member(member).birthday.set(parsed)
            imported += 1
        await ctx.send(self._t(
            lang,
            f"Import abgeschlossen: **{imported}** übernommen, **{skipped}** übersprungen.",
            f"Import finished: **{imported}** imported, **{skipped}** skipped.",
        ))

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
                Field.number("hour", L("Ankündigung um (Stunde, lokal)", "Announce at (hour, local)"), value=int(await conf.hour()), min=0, max=23),
                Field.text("timezone", L("Zeitzone (IANA)", "Timezone (IANA)"), value=str(await conf.timezone() or "UTC"), placeholder="Europe/Berlin"),
                Field.select(
                    "feb29_mode", L("29.02. in Nicht-Schaltjahren", "Feb-29 in non-leap years"),
                    [
                        {"value": "feb28", "label": L("Am 28.02. feiern", "Celebrate on Feb 28")},
                        {"value": "mar1", "label": L("Am 01.03. feiern", "Celebrate on Mar 1")},
                    ],
                    value=str(await conf.feb29_mode() or "mar1"),
                ),
                Field.number("reminder_days", L("Vorab-Erinnerung (Tage, 0 = aus)", "Advance reminder (days, 0 = off)"), value=int(await conf.reminder_days() or 0), min=0, max=60),
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
        lang = str(data.get("language", "en-US")).strip()
        if lang not in ("de-DE", "en-US"):
            lang = "en-US"

        # --- validation ------------------------------------------------- #
        errors = {}
        try:
            hour = int(data.get("hour", 9))
        except (TypeError, ValueError):
            hour = -1
        if not 0 <= hour <= 23:
            errors["hour"] = tr_lang(lang, "Stunde muss 0–23 sein.", "Hour must be 0–23.")
        tz_name = str(data.get("timezone", "UTC")).strip() or "UTC"
        if not _valid_tz(tz_name):
            errors["timezone"] = tr_lang(lang, "Unbekannte IANA-Zeitzone.", "Unknown IANA timezone.")
        try:
            reminder_days = int(data.get("reminder_days", 0))
        except (TypeError, ValueError):
            reminder_days = -1
        if not 0 <= reminder_days <= 60:
            errors["reminder_days"] = tr_lang(lang, "Tage müssen 0–60 sein.", "Days must be 0–60.")
        feb29_mode = str(data.get("feb29_mode", "mar1")).strip()
        if feb29_mode not in ("feb28", "mar1"):
            errors["feb29_mode"] = tr_lang(lang, "Ungültiger Modus.", "Invalid mode.")
        if errors:
            return SubmitResult.fail(tr_lang(lang, "Bitte Eingaben prüfen.", "Please check your input."), errors)

        # --- save --------------------------------------------------------- #
        await conf.enabled.set(bool(data.get("enabled")))
        ch = str(data.get("channel") or "").strip()
        await (conf.channel.set(int(ch)) if ch.isdigit() else conf.channel.clear())
        role = str(data.get("role") or "").strip()
        role_obj = ctx.guild.get_role(int(role)) if role.isdigit() else None
        role_warning = ""
        if role_obj is not None:
            problem = _role_problem(ctx.guild, role_obj, lang)
            if problem:
                return SubmitResult.fail(problem, {"role": problem})
            if role_obj.members:
                role_warning = " " + tr_lang(
                    lang,
                    f"Hinweis: Die Rolle hat bereits {len(role_obj.members)} Mitglied(er).",
                    f"Note: the role already has {len(role_obj.members)} member(s).",
                )
        await (conf.role.set(role_obj.id) if role_obj is not None else conf.role.clear())
        await conf.hour.set(hour)
        await conf.timezone.set(tz_name)
        await conf.feb29_mode.set(feb29_mode)
        await conf.reminder_days.set(reminder_days)
        msg = str(data.get("message", "")).strip()
        if msg:
            await conf.message.set(msg)
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved.") + role_warning)

    # ------------------------------------------------------------------ #
    # Dashboard page: birthday calendar (guild scope)
    # ------------------------------------------------------------------ #
    @dashboard_page(
        "calendar",
        L("Geburtstags-Kalender", "Birthday calendar"),
        scope="guild",
        permission="guild_member",
        icon="calendar",
    )
    async def calendar_page(self, ctx):
        gconf = await self.config.guild(ctx.guild).all()
        tz = _tzinfo(gconf.get("timezone") or "UTC")
        today = datetime.datetime.now(tz).date()
        feb29_mode = str(gconf.get("feb29_mode") or "mar1")

        members = await self.config.all_members(ctx.guild)
        rows = []
        for mid, mconf in members.items():
            bd = mconf.get("birthday")
            m = ctx.guild.get_member(mid)
            if not bd or m is None:
                continue
            days = _days_until(bd, today, feb29_mode)
            mm, dd = bd.split("-")
            rows.append({"_days": days, "member": m.display_name, "date": f"{dd}.{mm}.", "days": str(days)})
        rows.sort(key=lambda r: (r["_days"], r["date"]))
        for r in rows:
            r.pop("_days", None)

        comps = [
            Component.heading(L("Geburtstags-Kalender", "Birthday calendar")),
            Component.text(L(
                f"{len(rows)} gespeicherte Geburtstage · Zeitzone: {gconf.get('timezone') or 'UTC'}",
                f"{len(rows)} stored birthdays · timezone: {gconf.get('timezone') or 'UTC'}",
            )),
        ]
        if rows:
            comps.append(Component.table(
                columns=[
                    {"key": "date", "label": L("Datum", "Date")},
                    {"key": "member", "label": L("Mitglied", "Member")},
                    {"key": "days", "label": L("In Tagen", "In days")},
                ],
                rows=rows[:200],
                title=L("Nächste Geburtstage", "Upcoming birthdays"),
            ))
        else:
            comps.append(Component.text(L("Noch keine Geburtstage gespeichert.", "No birthdays stored yet.")))
        return PageSchema(components=comps)
