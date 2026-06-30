"""ScheduledMsg — send messages once or on a recurring schedule.

Opt-in per guild (disabled by default). Bilingual output (DE/EN). Web dashboard
integration (enable toggle + language) via the resilient drop-in.

Schedule syntax (all times UTC):
  every <N>m | every <N>h | every <N>d     – interval
  daily <HH:MM>                            – every day at that time
  weekly <mon..sun> <HH:MM>                – every week on that weekday
  once <YYYY-MM-DD HH:MM>                  – a single time, then auto-removed

Add with a ``|`` separating schedule and message, e.g.
  [p]schedule add #news daily 09:00 | Good morning everyone!
"""
from __future__ import annotations

import asyncio
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

log = logging.getLogger("red.pdc.scheduledmsg")

_DOWS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def compute_next(spec: str, after: datetime.datetime) -> Optional[datetime.datetime]:
    """Return the next run datetime (UTC) for ``spec`` strictly after ``after``."""
    s = (spec or "").strip().lower()

    m = re.match(r"every\s+(\d+)\s*([mhd])$", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n <= 0:
            return None
        delta = {"m": datetime.timedelta(minutes=n), "h": datetime.timedelta(hours=n), "d": datetime.timedelta(days=n)}[unit]
        return after + delta

    m = re.match(r"daily\s+(\d{1,2}):(\d{2})$", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= after:
            cand += datetime.timedelta(days=1)
        return cand

    m = re.match(r"weekly\s+(mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2}):(\d{2})$", s)
    if m:
        target = _DOWS.index(m.group(1))
        hh, mm = int(m.group(2)), int(m.group(3))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        cand += datetime.timedelta(days=(target - cand.weekday()) % 7)
        if cand <= after:
            cand += datetime.timedelta(days=7)
        return cand

    m = re.match(r"once\s+(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})$", s)
    if m:
        try:
            cand = datetime.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
            )
        except ValueError:
            return None
        return cand if cand > after else None

    return None


class ScheduledMsg(commands.Cog):
    """Scheduled and recurring messages."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C4ED_3, force_registration=True)
        self.config.register_guild(enabled=False, jobs=[], language="en-US")
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
        now_dt = datetime.datetime.utcnow()
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
            changed = False
            remaining: List[dict] = []
            for job in jobs:
                if job.get("next_run", 0) > now_ts:
                    remaining.append(job)
                    continue
                # Due: send it.
                ch = guild.get_channel(int(job.get("channel", 0)))
                if ch is not None and ch.permissions_for(guild.me).send_messages:
                    try:
                        await ch.send(job.get("content", ""))
                    except discord.HTTPException:
                        pass
                job["last_run"] = now_ts
                changed = True
                if str(job.get("spec", "")).strip().lower().startswith("once"):
                    continue  # drop one-time jobs after firing
                nxt = compute_next(job.get("spec", ""), now_dt)
                if nxt is None:
                    continue  # invalid -> drop
                job["next_run"] = nxt.replace(tzinfo=datetime.timezone.utc).timestamp()
                remaining.append(job)
            if changed:
                await self.config.guild(guild).jobs.set(remaining)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="schedule", aliases=["sched"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def schedule(self, ctx: commands.Context) -> None:
        """Configure scheduled messages."""

    @schedule.command(name="enable")
    @app_commands.describe(on_off="Enable or disable scheduled messages")
    async def sched_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Geplante Nachrichten **{state}**.", f"Scheduled messages **{state}**."))

    @schedule.command(name="add")
    @app_commands.describe(channel="Target channel", spec_and_message="<schedule> | <message>")
    async def sched_add(self, ctx: commands.Context, channel: discord.TextChannel, *, spec_and_message: str) -> None:
        """Add a scheduled message: ``<schedule> | <message>``."""
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
        nxt = compute_next(spec, datetime.datetime.utcnow())
        if nxt is None:
            await ctx.send(self._t(
                lang,
                "Ungültiger Zeitplan. Gültig: `every 30m`, `daily 09:00`, `weekly mon 09:00`, `once 2025-12-24 18:00`.",
                "Invalid schedule. Valid: `every 30m`, `daily 09:00`, `weekly mon 09:00`, `once 2025-12-24 18:00`.",
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
            "next_run": nxt.replace(tzinfo=datetime.timezone.utc).timestamp(),
            "last_run": None,
        }
        async with self.config.guild(ctx.guild).jobs() as jobs:
            jobs.append(job)
        await ctx.send(self._t(
            lang,
            f"Geplant (ID `{job['id']}`) → {channel.mention}, nächste Ausführung <t:{int(job['next_run'])}:R>.",
            f"Scheduled (ID `{job['id']}`) → {channel.mention}, next run <t:{int(job['next_run'])}:R>.",
        ))

    @schedule.command(name="list")
    async def sched_list(self, ctx: commands.Context) -> None:
        """List scheduled messages."""
        lang = await self._lang(ctx.guild)
        jobs = await self.config.guild(ctx.guild).jobs()
        if not jobs:
            await ctx.send(self._t(lang, "Keine geplanten Nachrichten.", "No scheduled messages."))
            return
        lines = []
        for j in jobs:
            ch = ctx.guild.get_channel(int(j.get("channel", 0)))
            preview = (j.get("content", "") or "")[:40]
            lines.append(f"`{j.get('id')}` · {ch.mention if ch else '?'} · `{j.get('spec')}` · <t:{int(j.get('next_run', 0))}:R>\n   {preview}")
        await ctx.send(embed=discord.Embed(
            title=self._t(lang, "Geplante Nachrichten", "Scheduled messages"),
            description="\n".join(lines)[:4000],
            colour=await ctx.embed_colour(),
        ))

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

    @schedule.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def sched_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
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
        listing = "\n".join(f"• `{j.get('id')}` {j.get('spec')}" for j in jobs) or "—"
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Geplante/wiederkehrende Nachrichten. Per Befehl `schedule add` verwalten.\nAktuell:\n{listing}",
                f"Scheduled / recurring messages. Manage via `schedule add`.\nCurrent:\n{listing}",
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
