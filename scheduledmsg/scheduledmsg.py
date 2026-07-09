"""ScheduledMsg — send messages once or on a recurring schedule.

Opt-in per guild (disabled by default). Bilingual output (DE/EN, default en-US).
Web dashboard integration (settings panel + job overview page) via the resilient
drop-in. Core job management is available to moderators; module configuration
(enable, timezone, catch-up policy, language) is admin-only.

Schedule syntax (times are interpreted in the guild's configured timezone,
default UTC):
  every <N>m | every <N>h | every <N>d     - interval
  daily <HH:MM>                            - every day at that time
  weekly <mon..sun> <HH:MM>                - every week on that weekday
  monthly <1-31> <HH:MM>                   - every month on that day
                                             (clamped to the month's last day)
  once <YYYY-MM-DD HH:MM>                  - a single time, then auto-removed

Message placeholders: {server}, {member_count}, {channel}, {date}, {time}.

Add with a ``|`` separating schedule and message, e.g.
  [p]schedule add #news daily 09:00 | Good morning everyone!
"""
from __future__ import annotations

import asyncio
import calendar
import datetime
import logging
import re
import time
import uuid
from typing import List, Optional

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

log = logging.getLogger("red.pdc.scheduledmsg")  # module logger

_DOWS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Seconds of tolerance: a job that is due within this window after its planned
# time is always sent. Anything older counts as "missed while the bot was down"
# and is handled according to the guild's catch-up policy.
_CATCHUP_GRACE = 300


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


def compute_next(spec: str, after: datetime.datetime, tz=None) -> Optional[datetime.datetime]:
    """Return the next run (aware, UTC) for ``spec`` strictly after ``after``.

    ``after`` must be an aware UTC datetime. daily/weekly/monthly/once times
    are interpreted as wall-clock times in ``tz`` (default UTC).
    """
    s = (spec or "").strip().lower()
    tz = tz or datetime.timezone.utc
    if after.tzinfo is None:
        after = after.replace(tzinfo=datetime.timezone.utc)
    local_after = after.astimezone(tz)

    m = re.match(r"every\s+(\d+)\s*([mhd])$", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n <= 0:
            return None
        delta = {"m": datetime.timedelta(minutes=n), "h": datetime.timedelta(hours=n), "d": datetime.timedelta(days=n)}[unit]
        return (after + delta).astimezone(datetime.timezone.utc)

    m = re.match(r"daily\s+(\d{1,2}):(\d{2})$", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        cand = local_after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= local_after:
            cand += datetime.timedelta(days=1)
        return cand.astimezone(datetime.timezone.utc)

    m = re.match(r"weekly\s+(mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2}):(\d{2})$", s)
    if m:
        target = _DOWS.index(m.group(1))
        hh, mm = int(m.group(2)), int(m.group(3))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        cand = local_after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        cand += datetime.timedelta(days=(target - cand.weekday()) % 7)
        if cand <= local_after:
            cand += datetime.timedelta(days=7)
        return cand.astimezone(datetime.timezone.utc)

    m = re.match(r"monthly\s+(\d{1,2})\s+(\d{1,2}):(\d{2})$", s)
    if m:
        day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= day <= 31 and 0 <= hh <= 23 and 0 <= mm <= 59):
            return None

        def _cand(year: int, month: int) -> datetime.datetime:
            # Clamp to the last day of shorter months (e.g. "monthly 31").
            last = calendar.monthrange(year, month)[1]
            return local_after.replace(
                year=year, month=month, day=min(day, last),
                hour=hh, minute=mm, second=0, microsecond=0,
            )

        cand = _cand(local_after.year, local_after.month)
        if cand <= local_after:
            y, mo = (local_after.year + 1, 1) if local_after.month == 12 else (local_after.year, local_after.month + 1)
            cand = _cand(y, mo)
        return cand.astimezone(datetime.timezone.utc)

    m = re.match(r"once\s+(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})$", s)
    if m:
        try:
            cand = datetime.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)),
                tzinfo=tz,
            )
        except ValueError:
            return None
        cand = cand.astimezone(datetime.timezone.utc)
        return cand if cand > after else None

    return None


class ScheduledMsg(commands.Cog):
    """Scheduled and recurring messages."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C4ED_3, force_registration=True)
        self.config.register_guild(
            enabled=False,
            jobs=[],
            language="en-US",
            # -- new keys (backward compatible defaults) --------------------- #
            timezone="UTC",  # IANA timezone for daily/weekly/monthly/once specs
            catchup="skip",  # missed-run policy: "skip" | "send_once"
        )
        # job: {id, channel, content, spec, next_run(epoch), last_run(epoch|None)}
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

    async def _tz(self, guild: discord.Guild):
        return _tzinfo(await self.config.guild(guild).timezone())

    @staticmethod
    def _render(content: str, guild: discord.Guild, channel, tz, lang: str) -> str:
        """Replace supported placeholders in a job's message."""
        now = datetime.datetime.now(tz)
        date_fmt = "%d.%m.%Y" if str(lang).lower().startswith("de") else "%Y-%m-%d"
        return (
            (content or "")
            .replace("{server}", guild.name)
            .replace("{member_count}", str(guild.member_count))
            .replace("{channel}", getattr(channel, "mention", "#?"))
            .replace("{date}", now.strftime(date_fmt))
            .replace("{time}", now.strftime("%H:%M"))
        )

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
    # Scheduler loop
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ScheduledMsg tick failed")
            await asyncio.sleep(30)

    async def _tick(self) -> None:
        now_ts = time.time()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        guilds = await self.config.all_guilds()
        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            jobs = gconf.get("jobs") or []
            if not jobs:
                continue
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            tz = _tzinfo(gconf.get("timezone"))
            catchup = str(gconf.get("catchup") or "skip")
            lang = str(gconf.get("language") or "en-US")
            # Sends (network I/O) happen outside of any config lock; the
            # results are merged per job afterwards so concurrent edits from
            # commands/dashboard are not lost.
            def _job_key(j: dict) -> str:
                jid = str(j.get("id") or "")
                if jid:
                    return jid
                return f"{j.get('channel')}|{j.get('spec')}|{j.get('content')}"

            changed = False
            updates: dict = {}  # key -> {"spec", "last_run"?, "next_run"?}
            drops: set = set()
            for job in jobs:
                if job.get("next_run", 0) > now_ts:
                    continue
                # Due. Decide whether to send: on-time jobs always fire; jobs
                # missed while the bot was down follow the catch-up policy
                # ("send_once" fires exactly once, "skip" only reschedules).
                missed_by = now_ts - float(job.get("next_run", 0) or 0)
                should_send = missed_by <= _CATCHUP_GRACE or catchup == "send_once"
                if should_send:
                    ch = guild.get_channel(int(job.get("channel", 0)))
                    if ch is not None and ch.permissions_for(guild.me).send_messages:
                        try:
                            await ch.send(self._render(job.get("content", ""), guild, ch, tz, lang))
                        except discord.HTTPException:
                            pass
                changed = True
                key = _job_key(job)
                spec = str(job.get("spec", ""))
                if spec.strip().lower().startswith("once"):
                    drops.add(key)  # drop one-time jobs after their slot
                    continue
                nxt = compute_next(spec, now_utc, tz)
                if nxt is None:
                    drops.add(key)  # invalid -> drop
                    continue
                upd = {"spec": spec, "next_run": nxt.timestamp()}
                if should_send:
                    upd["last_run"] = now_ts
                updates[key] = upd
            if changed:
                async with self.config.guild(guild).jobs() as current:
                    merged: List[dict] = []
                    for job in current:
                        key = _job_key(job)
                        if key in drops:
                            continue
                        upd = updates.get(key)
                        # Only apply if the spec was not edited concurrently.
                        if upd is not None and str(job.get("spec", "")) == upd["spec"]:
                            job["next_run"] = upd["next_run"]
                            if "last_run" in upd:
                                job["last_run"] = upd["last_run"]
                        merged.append(job)
                    current[:] = merged

    # ------------------------------------------------------------------ #
    # Commands (core management: moderators and up)
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="schedule", aliases=["sched"])
    @commands.mod_or_permissions(manage_messages=True)
    @commands.guild_only()
    async def schedule(self, ctx: commands.Context) -> None:
        """Manage scheduled messages."""

    @schedule.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(on_off="Enable or disable scheduled messages")
    async def sched_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server (admin)."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Geplante Nachrichten **{state}**.", f"Scheduled messages **{state}**."))

    @schedule.command(name="add")
    @app_commands.describe(channel="Target channel", spec_and_message="<schedule> | <message>")
    async def sched_add(self, ctx: commands.Context, channel: discord.TextChannel, *, spec_and_message: str) -> None:
        """Add a scheduled message: ``<schedule> | <message>``.

        Placeholders: {server}, {member_count}, {channel}, {date}, {time}.
        """
        lang = await self._lang(ctx.guild)
        if "|" not in spec_and_message:
            await ctx.send(self._t(
                lang,
                "Format: `<zeitplan> | <nachricht>` – z. B. `daily 09:00 | Guten Morgen!`",
                "Format: `<schedule> | <message>` – e.g. `daily 09:00 | Good morning!`",
            ))
            return
        spec, content = spec_and_message.split("|", 1)
        spec, content = spec.strip(), content.strip()
        tz = await self._tz(ctx.guild)
        nxt = compute_next(spec, datetime.datetime.now(datetime.timezone.utc), tz)
        if nxt is None:
            await ctx.send(self._t(
                lang,
                "Ungültiger Zeitplan. Gültig: `every 30m`, `daily 09:00`, `weekly mon 09:00`, `monthly 1 09:00`, `once 2025-12-24 18:00`.",
                "Invalid schedule. Valid: `every 30m`, `daily 09:00`, `weekly mon 09:00`, `monthly 1 09:00`, `once 2025-12-24 18:00`.",
            ))
            return
        if not content:
            await ctx.send(self._t(lang, "Nachricht fehlt.", "Message is empty."))
            return
        job = {
            "id": uuid.uuid4().hex[:8],
            "channel": channel.id,
            "content": content,
            "spec": spec,
            "next_run": nxt.timestamp(),
            "last_run": None,
        }
        async with self.config.guild(ctx.guild).jobs() as jobs:
            jobs.append(job)
        await ctx.send(self._t(
            lang,
            f"Geplant (ID `{job['id']}`) → {channel.mention}, nächste Ausführung <t:{int(job['next_run'])}:R>.",
            f"Scheduled (ID `{job['id']}`) → {channel.mention}, next run <t:{int(job['next_run'])}:R>.",
        ))

    @schedule.command(name="edit")
    @app_commands.describe(
        job_id="The job ID (from 'schedule list')",
        field="What to change: message, spec or channel",
        value="The new value",
    )
    async def sched_edit(self, ctx: commands.Context, job_id: str, field: str, *, value: str) -> None:
        """Edit an existing job: change its message, schedule or channel."""
        lang = await self._lang(ctx.guild)
        field = field.strip().lower()
        if field not in ("message", "spec", "channel"):
            await ctx.send(self._t(
                lang,
                "Feld muss `message`, `spec` oder `channel` sein.",
                "Field must be `message`, `spec` or `channel`.",
            ))
            return
        value = value.strip()
        new_channel: Optional[discord.TextChannel] = None
        new_next: Optional[float] = None
        if field == "channel":
            try:
                new_channel = await commands.TextChannelConverter().convert(ctx, value)
            except commands.BadArgument:
                await ctx.send(self._t(lang, "Kanal nicht gefunden.", "Channel not found."))
                return
        elif field == "spec":
            tz = await self._tz(ctx.guild)
            nxt = compute_next(value, datetime.datetime.now(datetime.timezone.utc), tz)
            if nxt is None:
                await ctx.send(self._t(lang, "Ungültiger Zeitplan.", "Invalid schedule."))
                return
            new_next = nxt.timestamp()
        elif not value:
            await ctx.send(self._t(lang, "Nachricht fehlt.", "Message is empty."))
            return

        updated = None
        async with self.config.guild(ctx.guild).jobs() as jobs:
            job = next((j for j in jobs if j.get("id") == job_id), None)
            if job is not None:
                if field == "message":
                    job["content"] = value
                elif field == "spec":
                    job["spec"] = value
                    job["next_run"] = new_next
                elif field == "channel" and new_channel is not None:
                    job["channel"] = new_channel.id
                updated = dict(job)
        if updated is None:
            await ctx.send(self._t(lang, "Job nicht gefunden.", "Job not found."))
            return
        await ctx.send(self._t(
            lang,
            f"Job `{job_id}` aktualisiert · `{updated.get('spec')}` · nächste Ausführung <t:{int(updated.get('next_run', 0))}:R>.",
            f"Job `{job_id}` updated · `{updated.get('spec')}` · next run <t:{int(updated.get('next_run', 0))}:R>.",
        ))

    @schedule.command(name="list")
    async def sched_list(self, ctx: commands.Context) -> None:
        """List scheduled messages (paginated)."""
        lang = await self._lang(ctx.guild)
        jobs = await self.config.guild(ctx.guild).jobs()
        if not jobs:
            await ctx.send(self._t(lang, "Keine geplanten Nachrichten.", "No scheduled messages."))
            return
        lines = []
        for j in jobs:
            ch = ctx.guild.get_channel(int(j.get("channel", 0)))
            preview = (j.get("content", "") or "")[:40]
            lines.append(
                f"`{j.get('id')}` · {ch.mention if ch else '?'} · `{j.get('spec')}` · <t:{int(j.get('next_run', 0))}:R>\n   {preview}"
            )
        per_page = 10
        pages: List[discord.Embed] = []
        colour = await ctx.embed_colour()
        title = self._t(lang, "Geplante Nachrichten", "Scheduled messages")
        for i in range(0, len(lines), per_page):
            e = discord.Embed(title=title, description="\n".join(lines[i:i + per_page])[:4000], colour=colour)
            e.set_footer(text=self._t(
                lang,
                f"Seite {i // per_page + 1}/{(len(lines) - 1) // per_page + 1} · {len(lines)} Jobs",
                f"Page {i // per_page + 1}/{(len(lines) - 1) // per_page + 1} · {len(lines)} jobs",
            ))
            pages.append(e)
        await self._send_pages(ctx, pages)

    @schedule.command(name="remove")
    @app_commands.describe(job_id="The job ID (from 'schedule list')")
    async def sched_remove(self, ctx: commands.Context, job_id: str) -> None:
        """Remove a scheduled message by ID."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).jobs() as jobs:
            before = len(jobs)
            jobs[:] = [j for j in jobs if j.get("id") != job_id]
            removed = before - len(jobs)
        await ctx.send(
            self._t(lang, "Entfernt." if removed else "Nicht gefunden.", "Removed." if removed else "Not found.")
        )

    # -- module configuration (admin) ----------------------------------- #
    @schedule.command(name="timezone", aliases=["tz"])
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(timezone="IANA timezone name, e.g. Europe/Berlin (default: UTC)")
    async def sched_timezone(self, ctx: commands.Context, timezone: str) -> None:
        """Set the guild timezone used for schedule times (admin)."""
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
            f"Zeitzone: **{timezone}** (aktuell {now}). Neue Zeitpläne nutzen diese Zone.",
            f"Timezone: **{timezone}** (currently {now}). New schedule times use this zone.",
        ))

    @schedule.command(name="catchup")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(policy="What to do with runs missed while the bot was down: skip or send_once")
    async def sched_catchup(self, ctx: commands.Context, policy: str) -> None:
        """Set the catch-up policy for missed runs (admin)."""
        lang = await self._lang(ctx.guild)
        policy = policy.strip().lower().replace("-", "_")
        if policy not in ("skip", "send_once"):
            await ctx.send(self._t(
                lang,
                "Richtlinie muss `skip` oder `send_once` sein.",
                "Policy must be `skip` or `send_once`.",
            ))
            return
        await self.config.guild(ctx.guild).catchup.set(policy)
        await ctx.send(self._t(
            lang,
            "Verpasste Ausführungen werden **übersprungen**." if policy == "skip"
            else "Verpasste Ausführungen werden **einmalig nachgeholt**.",
            "Missed runs are **skipped**." if policy == "skip"
            else "Missed runs are **sent once** on recovery.",
        ))

    @schedule.command(name="language")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def sched_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server (admin)."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("scheduledmsg", L("Geplante Nachrichten", "Scheduled messages"), mount="guild_settings", permission="guild_admin", order=70)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        jobs = await conf.jobs()
        listing = "\n".join(f"• `{j.get('id')}` {j.get('spec')}" for j in jobs[:15]) or "—"
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Geplante/wiederkehrende Nachrichten. Per Befehl `schedule add` verwalten.\nAktuell:\n{listing}",
                f"Scheduled / recurring messages. Manage via `schedule add`.\nCurrent:\n{listing}",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.text("timezone", L("Zeitzone (IANA)", "Timezone (IANA)"), value=str(await conf.timezone() or "UTC"), placeholder="Europe/Berlin"),
                Field.select(
                    "catchup", L("Verpasste Ausführungen", "Missed runs"),
                    [
                        {"value": "skip", "label": L("Überspringen", "Skip")},
                        {"value": "send_once", "label": L("Einmalig nachholen", "Send once")},
                    ],
                    value=str(await conf.catchup() or "skip"),
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

        errors = {}
        tz_name = str(data.get("timezone", "UTC")).strip() or "UTC"
        if not _valid_tz(tz_name):
            errors["timezone"] = tr_lang(lang, "Unbekannte IANA-Zeitzone.", "Unknown IANA timezone.")
        catchup = str(data.get("catchup", "skip")).strip()
        if catchup not in ("skip", "send_once"):
            errors["catchup"] = tr_lang(lang, "Ungültige Richtlinie.", "Invalid policy.")
        if errors:
            return SubmitResult.fail(tr_lang(lang, "Bitte Eingaben prüfen.", "Please check your input."), errors)

        await conf.enabled.set(bool(data.get("enabled")))
        await conf.timezone.set(tz_name)
        await conf.catchup.set(catchup)
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard page: job overview (guild scope)
    # ------------------------------------------------------------------ #
    @dashboard_page(
        "overview",
        L("Geplante Nachrichten", "Scheduled messages"),
        scope="guild",
        permission="guild_mod",
        icon="clock",
    )
    async def overview_page(self, ctx):
        gconf = await self.config.guild(ctx.guild).all()
        tz = _tzinfo(gconf.get("timezone"))
        jobs = gconf.get("jobs") or []
        rows = []
        for j in sorted(jobs, key=lambda x: x.get("next_run", 0)):
            ch = ctx.guild.get_channel(int(j.get("channel", 0)))
            nxt = j.get("next_run")
            nxt_str = (
                datetime.datetime.fromtimestamp(float(nxt), tz).strftime("%Y-%m-%d %H:%M")
                if nxt else "—"
            )
            rows.append({
                "id": str(j.get("id", "")),
                "channel": f"#{ch.name}" if ch else "?",
                "spec": str(j.get("spec", "")),
                "next": nxt_str,
                "message": (j.get("content", "") or "")[:60],
            })
        comps = [
            Component.heading(L("Geplante Nachrichten", "Scheduled messages")),
            Component.text(L(
                f"{len(rows)} Job(s) · Zeitzone: {gconf.get('timezone') or 'UTC'} · Verpasste Ausführungen: {gconf.get('catchup') or 'skip'}",
                f"{len(rows)} job(s) · timezone: {gconf.get('timezone') or 'UTC'} · missed runs: {gconf.get('catchup') or 'skip'}",
            )),
        ]
        if rows:
            comps.append(Component.table(
                columns=[
                    {"key": "id", "label": "ID"},
                    {"key": "channel", "label": L("Kanal", "Channel")},
                    {"key": "spec", "label": L("Zeitplan", "Schedule")},
                    {"key": "next", "label": L("Nächste Ausführung", "Next run")},
                    {"key": "message", "label": L("Nachricht", "Message")},
                ],
                rows=rows[:200],
            ))
        else:
            comps.append(Component.text(L("Keine geplanten Nachrichten.", "No scheduled messages.")))
        return PageSchema(components=comps)
