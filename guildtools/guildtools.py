"""GuildTools — member export, absences and /whois for WoW guilds (ENV-first).

Bilingual output (DE/EN, default en-US). Absences are stored in the Red config
(a legacy text file from older versions is migrated automatically) and are
interpreted in the guild's configured IANA timezone (default: UTC). Web
dashboard integration (settings panel, tracked-members widget, absence
overview page) via the resilient drop-in.
"""
import discord
from discord import app_commands
from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from datetime import datetime, timezone, date, timedelta
from typing import List, Optional
import io
import csv
import asyncio
import os
import re
import uuid

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

from .pdc_dashboard import (
    dashboard_widget, dashboard_panel, dashboard_page,
    WidgetData, PanelSchema, PageSchema, Component, Control, Field, SubmitResult,
    register_dashboard, unregister_dashboard,
    L, tr, tr_lang,
)

ONLINE_STATES = {discord.Status.online, discord.Status.idle, discord.Status.dnd}  # counted as online
DATE_FORMATS = ["%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"]

def _parse_date(s: str):
    """Parse a user-supplied date string (DD-MM-YYYY / DD.MM.YYYY / DD/MM/YYYY / ISO)."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None

def _out_date(dt) -> str:
    return dt.strftime("%d.%m.%Y")

def _iso_date(dt) -> str:
    return dt.strftime("%Y-%m-%d")

def _from_iso(s: str) -> Optional[date]:
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None

def _tzinfo(name: Optional[str]):
    """Resolve an IANA timezone name to a tzinfo (fallback: UTC)."""
    if ZoneInfo is not None and name:
        try:
            return ZoneInfo(str(name))
        except Exception:
            pass
    return timezone.utc

def _valid_tz(name: str) -> bool:
    if ZoneInfo is None:
        return str(name).upper() == "UTC"
    try:
        ZoneInfo(str(name))
        return True
    except Exception:
        return False

def _slugify_realm(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[’'`]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")

def _slugify_char(s: str) -> str:
    s = s.strip().lower()
    s = (s.replace("ä","a").replace("ö","o").replace("ü","u").replace("ß","ss"))
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")

class GuildTools(commands.Cog):
    """Cog: Tools for WoW guilds - export, absences & /whois (ENV-first)."""

    __author__ = "pd-codes"
    __version__ = "1.4.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xD0DE2025, force_registration=True)
        self.config.register_guild(
            last_seen={},
            wow_default_region="eu",
            wow_default_realm="",
            language="en-US",
            # -- new keys (backward compatible defaults) --------------------- #
            timezone="UTC",  # IANA timezone used for absence date logic
            absences=[],  # [{id, user_id, username, display_name, start, end, created_at}]
            absences_migrated=False,  # legacy text file imported into config?
        )
        self.config.register_global(
            blizz_client_id="",
            blizz_client_secret="",
            blizz_token="",
            blizz_token_expires_at=0
        )
        self._abs_lock = asyncio.Lock()
        # In-memory token cache (process-local)
        self._token_mem = ""
        self._token_mem_exp = 0
        # Presence tracking buffer: guild_id -> {member_id(str): iso timestamp}
        self._presence_buffer: dict = {}
        self._presence_flush_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        register_dashboard(self)
        self._presence_flush_task = asyncio.create_task(self._presence_flush_loop())

    async def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._presence_flush_task:
            self._presence_flush_task.cancel()
            self._presence_flush_task = None
        try:
            await self._flush_presence_buffer()
        except Exception:
            pass

    async def _presence_flush_loop(self) -> None:
        """Flush buffered presence timestamps to the config every 60 seconds."""
        try:
            while True:
                await asyncio.sleep(60)
                try:
                    await self._flush_presence_buffer()
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def _flush_presence_buffer(self) -> None:
        if not self._presence_buffer:
            return
        buffered, self._presence_buffer = self._presence_buffer, {}
        for gid, entries in buffered.items():
            # Merge into the stored dict instead of overwriting it wholesale.
            async with self.config.guild_from_id(gid).last_seen() as data:
                data.update(entries)

    @dashboard_widget("tracked_members", L("Erfasste Mitglieder", "Tracked Members"), size="sm", permission="guild_member")
    async def tracked_members_widget(self, ctx):
        try:
            data = await self.config.guild(ctx.guild).last_seen()
            return WidgetData.kpi(value=int(len(data)), label=L("Erfasste Mitglieder", "Tracked members"))
        except Exception:
            return WidgetData.kpi(value="–", label=L("Erfasste Mitglieder", "Tracked members"))

    async def _lang(self, guild) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    async def _tz(self, guild):
        if guild is None:
            return timezone.utc
        return _tzinfo(await self.config.guild(guild).timezone())

    # ---------- Dashboard settings panel ----------
    @dashboard_panel(
        "settings", L("Einstellungen", "Settings"),
        mount="guild_settings", permission="guild_admin", order=99,
    )
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        return PanelSchema(
            description=tr(
                ctx,
                "Sprache und Zeitzone der Bot-Ausgaben für diesen Server.",
                "Output language and timezone for this server.",
            ),
            fields=[
                Field.select(
                    "language", L("Sprache", "Language"),
                    [
                        {"value": "de-DE", "label": "Deutsch"},
                        {"value": "en-US", "label": "English"},
                    ],
                    value=str(await conf.language()),
                    reload_on_change=True,
                ),
                Field.text(
                    "timezone", L("Zeitzone (IANA)", "Timezone (IANA)"),
                    value=str(await conf.timezone() or "UTC"), placeholder="Europe/Berlin",
                ),
            ],
        )

    @settings_panel.on_submit
    async def _save_settings(self, ctx, data):
        lang = str(data.get("language", "en-US")).strip()
        if lang not in ("de-DE", "en-US"):
            lang = "en-US"
        tz_name = str(data.get("timezone", "UTC")).strip() or "UTC"
        if not _valid_tz(tz_name):
            return SubmitResult.fail(
                tr(ctx, "Bitte Eingaben prüfen.", "Please check your input."),
                {"timezone": tr(ctx, "Unbekannte IANA-Zeitzone.", "Unknown IANA timezone.")},
            )
        await self.config.guild(ctx.guild).language.set(lang)
        await self.config.guild(ctx.guild).timezone.set(tz_name)
        return SubmitResult.ok(tr(ctx, "Gespeichert.", "Saved."))

    # ---------- Presence Tracking ----------
    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if not after.guild:
            return
        intents = getattr(self.bot, "intents", None)
        if not intents or not intents.presences:
            return
        became_online = after.status in ONLINE_STATES and before.status != after.status
        became_offline = after.status is discord.Status.offline and before.status != after.status
        if not (became_online or became_offline):
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        # Buffer in memory; a background task flushes periodically to avoid
        # a full config write on every presence change.
        self._presence_buffer.setdefault(after.guild.id, {})[str(after.id)] = now_iso

    # ---------- Guild-level configuration (admin) ----------
    @commands.hybrid_group(name="guildtoolsset", aliases=["gtset"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def guildtoolsset(self, ctx: commands.Context) -> None:
        """Configure the GuildTools module."""

    @guildtoolsset.command(name="timezone", aliases=["tz"])
    @app_commands.describe(timezone_name="IANA timezone name, e.g. Europe/Berlin (default: UTC)")
    async def gts_timezone(self, ctx: commands.Context, timezone_name: str) -> None:
        """Set the guild timezone (IANA name) used for absence dates."""
        lang = await self._lang(ctx.guild)
        timezone_name = timezone_name.strip()
        if not _valid_tz(timezone_name):
            await ctx.send(tr_lang(
                lang,
                "Unbekannte Zeitzone. Beispiel: `Europe/Berlin`, `America/New_York`, `UTC`.",
                "Unknown timezone. Example: `Europe/Berlin`, `America/New_York`, `UTC`.",
            ))
            return
        await self.config.guild(ctx.guild).timezone.set(timezone_name)
        now = datetime.now(_tzinfo(timezone_name)).strftime("%H:%M")
        await ctx.send(tr_lang(
            lang,
            f"Zeitzone: **{timezone_name}** (aktuell {now}).",
            f"Timezone: **{timezone_name}** (currently {now}).",
        ))

    @guildtoolsset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def gts_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(tr_lang(language, "Sprache: Deutsch", "Language: English"))

    # ---------- /export-userlist ----------
    @app_commands.command(name="export-userlist", description="Export members to a CSV (with optional filters).", extras={"i18n_desc": {"de-DE": "Mitglieder als CSV exportieren (mit optionalen Filtern).", "en-US": "Export members to a CSV (with optional filters)."}})
    @app_commands.describe(
        role="Only include members with this role",
        joined_after="Only members who joined after this date (DD.MM.YYYY)",
        joined_before="Only members who joined before this date (DD.MM.YYYY)",
        status="Filter by presence status (requires presence intent)",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="all", value="all"),
        app_commands.Choice(name="online", value="online"),
        app_commands.Choice(name="offline", value="offline"),
    ])
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def export_userlist(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
        joined_after: Optional[str] = None,
        joined_before: Optional[str] = None,
        status: Optional[app_commands.Choice[str]] = None,
    ):
        """Export the server's member list with optional filters."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lang = await self._lang(guild)
        if guild is None:
            return await interaction.followup.send(tr_lang(lang, "Dieser Befehl muss in einer Guild ausgeführt werden.", "This command must be used in a server."), ephemeral=True)

        # Validate date filters up front.
        after_dt = before_dt = None
        if joined_after:
            after_dt = _parse_date(joined_after)
            if after_dt is None:
                return await interaction.followup.send(tr_lang(lang, "❌ Ungültiges Datum bei `joined_after`.", "❌ Invalid `joined_after` date."), ephemeral=True)
        if joined_before:
            before_dt = _parse_date(joined_before)
            if before_dt is None:
                return await interaction.followup.send(tr_lang(lang, "❌ Ungültiges Datum bei `joined_before`.", "❌ Invalid `joined_before` date."), ephemeral=True)
        status_val = status.value if status else "all"

        members = []
        try:
            async for m in guild.fetch_members(limit=None):
                members.append(m)
        except discord.Forbidden:
            return await interaction.followup.send(
                tr_lang(
                    lang,
                    "Mir fehlen Berechtigungen, um Mitglieder zu lesen. Bitte gib mir **Mitglieder anzeigen** (View Guild Members).",
                    "I'm missing permissions to read members. Please grant me **View Guild Members**.",
                ),
                ephemeral=True
            )

        tz = await self._tz(guild)

        def _keep(m: discord.Member) -> bool:
            if role is not None and role not in m.roles:
                return False
            if (after_dt or before_dt) and m.joined_at is None:
                return False
            if after_dt is not None and m.joined_at is not None:
                if m.joined_at.astimezone(tz).date() < after_dt.date():
                    return False
            if before_dt is not None and m.joined_at is not None:
                if m.joined_at.astimezone(tz).date() > before_dt.date():
                    return False
            if status_val != "all":
                cached = guild.get_member(m.id)
                st = cached.status if cached is not None else discord.Status.offline
                is_online = st in ONLINE_STATES
                if status_val == "online" and not is_online:
                    return False
                if status_val == "offline" and is_online:
                    return False
            return True

        members = [m for m in members if _keep(m)]
        if not members:
            return await interaction.followup.send(tr_lang(lang, "Keine Mitglieder entsprechen den Filtern.", "No members match the filters."), ephemeral=True)

        last_seen_map = await self.config.guild(guild).last_seen()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\n")
        w.writerow(["UserID", "Username", "DisplayName", "Roles", "JoinedAt", "LastSeen"])
        for m in members:
            w.writerow([
                str(m.id),
                m.name,
                m.display_name,
                ", ".join([r.name for r in m.roles if r.name != "@everyone"]) or "",
                m.joined_at.astimezone(timezone.utc).isoformat() if m.joined_at else "",
                last_seen_map.get(str(m.id), "unknown"),
            ])
        buf.seek(0)
        file = discord.File(io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename=f"user_export_{guild.id}.csv")
        await interaction.followup.send(
            tr_lang(
                lang,
                f"Hier ist dein Export (**{len(members)}** Mitglieder, nur für dich sichtbar).",
                f"Here is your export (**{len(members)}** members, only visible to you).",
            ),
            file=file, ephemeral=True,
        )

    # ---------- Absences (Config-backed, legacy file migrated) ----------
    async def _migrate_absences(self, guild: discord.Guild) -> None:
        """One-time import of the legacy ``absences_<gid>.txt`` into the config."""
        conf = self.config.guild(guild)
        if await conf.absences_migrated():
            return
        path = cog_data_path(raw_name=self.__class__.__name__) / f"absences_{guild.id}.txt"
        records = []
        if path.exists():
            def _read_rows():
                out = []
                with open(path, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        if i == 0:  # header
                            continue
                        parts = line.rstrip("\n").split(";")
                        if len(parts) >= 5:
                            out.append(parts)
                return out
            for parts in await asyncio.to_thread(_read_rows):
                try:
                    start = datetime.strptime(parts[3].strip(), "%d.%m.%Y")
                    end = datetime.strptime(parts[4].strip(), "%d.%m.%Y")
                    records.append({
                        "id": uuid.uuid4().hex[:8],
                        "user_id": int(parts[0]),
                        "username": parts[1],
                        "display_name": parts[2],
                        "start": _iso_date(start),
                        "end": _iso_date(end),
                        "created_at": "",
                    })
                except (ValueError, IndexError):
                    continue
        if records:
            async with conf.absences() as absences:
                absences.extend(records)
        await conf.absences_migrated.set(True)

    async def _get_absences(self, guild: discord.Guild) -> List[dict]:
        """Return all absence records (after ensuring legacy migration)."""
        async with self._abs_lock:
            await self._migrate_absences(guild)
        return await self.config.guild(guild).absences()

    @staticmethod
    def _overlaps(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
        return not (a_end < b_start or a_start > b_end)

    @app_commands.command(name="add-absence", description="Add an absence (DD-MM-YYYY / DD.MM.YYYY / DD/MM/YYYY).", extras={"i18n_desc": {"de-DE": "Abwesenheit eintragen (TT-MM-JJJJ / TT.MM.JJJJ / TT/MM/JJJJ).", "en-US": "Add an absence (DD-MM-YYYY / DD.MM.YYYY / DD/MM/YYYY)."}})
    @app_commands.describe(von="Start date", bis="End date")
    @app_commands.guild_only()
    async def add_absence(self, interaction: discord.Interaction, von: str, bis: str):
        """Add an absence entry for the invoking member (with overlap warning)."""
        lang = await self._lang(interaction.guild)
        start, end = _parse_date(von), _parse_date(bis)
        if not start:
            return await interaction.response.send_message(tr_lang(lang, "❌ Ungültiges **von**-Datum.", "❌ Invalid **start** date."), ephemeral=True)
        if not end:
            return await interaction.response.send_message(tr_lang(lang, "❌ Ungültiges **bis**-Datum.", "❌ Invalid **end** date."), ephemeral=True)
        if end < start:
            return await interaction.response.send_message(tr_lang(lang, "❌ **bis** darf nicht vor **von** liegen.", "❌ **end** must not be before **start**."), ephemeral=True)
        if (end - start).days > 365:
            return await interaction.response.send_message(tr_lang(lang, "❌ Abwesenheiten dürfen max. 365 Tage umfassen.", "❌ Absences may span at most 365 days."), ephemeral=True)

        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(tr_lang(lang, "Dieser Befehl muss in einer Guild ausgeführt werden.", "This command must be used in a server."), ephemeral=True)

        existing = await self._get_absences(guild)
        new_start, new_end = start.date(), end.date()

        # Conflict detection: warn about overlaps with the member's own entries.
        conflicts = []
        for rec in existing:
            if int(rec.get("user_id", 0)) != interaction.user.id:
                continue
            ex_start, ex_end = _from_iso(rec.get("start")), _from_iso(rec.get("end"))
            if ex_start and ex_end and self._overlaps(new_start, new_end, ex_start, ex_end):
                conflicts.append(f"{_out_date(datetime.combine(ex_start, datetime.min.time()))} → {_out_date(datetime.combine(ex_end, datetime.min.time()))}")

        record = {
            "id": uuid.uuid4().hex[:8],
            "user_id": interaction.user.id,
            "username": interaction.user.name,
            "display_name": interaction.user.display_name,
            "start": _iso_date(start),
            "end": _iso_date(end),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        async with self.config.guild(guild).absences() as absences:
            absences.append(record)

        msg = tr_lang(
            lang,
            f"✅ Neue Abwesenheit gespeichert für **{interaction.user.mention}**\n"
            f"• Von: **{_out_date(start)}**\n"
            f"• Bis: **{_out_date(end)}**",
            f"✅ New absence saved for **{interaction.user.mention}**\n"
            f"• From: **{_out_date(start)}**\n"
            f"• To: **{_out_date(end)}**",
        )
        if conflicts:
            msg += tr_lang(
                lang,
                "\n⚠️ **Achtung:** Überschneidung mit bestehenden Einträgen:\n" + "\n".join(f"• {c}" for c in conflicts[:5]),
                "\n⚠️ **Warning:** overlaps with existing entries:\n" + "\n".join(f"• {c}" for c in conflicts[:5]),
            )
        await interaction.response.send_message(msg)

    @app_commands.command(name="list-absence", description="Show your absences (ephemeral).", extras={"i18n_desc": {"de-DE": "Deine Abwesenheiten anzeigen (ephemer).", "en-US": "Show your absences (ephemeral)."}})
    @app_commands.describe(history="Also include past absences")
    @app_commands.guild_only()
    async def list_absence(self, interaction: discord.Interaction, history: Optional[bool] = False):
        """List the invoker's absences (active/upcoming; ``history`` adds past ones)."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lang = await self._lang(guild)
        if guild is None:
            return await interaction.followup.send(tr_lang(lang, "Dieser Befehl muss in einer Guild ausgeführt werden.", "This command must be used in a server."), ephemeral=True)

        tz = await self._tz(guild)
        today = datetime.now(tz).date()
        records = [r for r in await self._get_absences(guild) if int(r.get("user_id", 0)) == interaction.user.id]
        rows = []
        for r in sorted(records, key=lambda x: str(x.get("start", ""))):
            r_start, r_end = _from_iso(r.get("start")), _from_iso(r.get("end"))
            if r_start is None or r_end is None:
                continue
            if not history and r_end < today:
                continue
            if r_end < today:
                state = tr_lang(lang, "vergangen", "past")
            elif r_start > today:
                state = tr_lang(lang, "geplant", "upcoming")
            else:
                state = tr_lang(lang, "aktiv", "active")
            rows.append(f"• **{r_start.strftime('%d.%m.%Y')}** → **{r_end.strftime('%d.%m.%Y')}** ({state})")

        if not rows:
            return await interaction.followup.send(
                tr_lang(lang, "Du hast keine (aktuellen) Abwesenheiten hinterlegt.", "You have no (current) absences on file."),
                ephemeral=True,
            )

        # Pagination: chunk into multiple embeds (Discord allows up to 10 per message).
        per_page = 15
        embeds = []
        for i in range(0, len(rows), per_page):
            embeds.append(discord.Embed(
                title=tr_lang(lang, "Deine Abwesenheiten", "Your absences"),
                description="\n".join(rows[i:i + per_page]),
                color=discord.Color.blurple(),
            ))
        await interaction.followup.send(embeds=embeds[:10], ephemeral=True)

    @app_commands.command(name="get-absence", description="CSV with all absences (mods only).", extras={"i18n_desc": {"de-DE": "CSV mit allen Abwesenheiten (nur Mods).", "en-US": "CSV with all absences (mods only)."}})
    @app_commands.describe(include_past="Also include past absences (full history)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def get_absence(self, interaction: discord.Interaction, include_past: Optional[bool] = False):
        """Export the guild's absence list as CSV (mods and up)."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lang = await self._lang(guild)
        if guild is None:
            return await interaction.followup.send(tr_lang(lang, "Dieser Befehl muss in einer Guild ausgeführt werden.", "This command must be used in a server."), ephemeral=True)

        tz = await self._tz(guild)
        today = datetime.now(tz).date()
        records = await self._get_absences(guild)
        rows = []
        for r in sorted(records, key=lambda x: str(x.get("start", ""))):
            r_end = _from_iso(r.get("end"))
            if not include_past and r_end is not None and r_end < today:
                continue
            rows.append(r)
        if not rows:
            return await interaction.followup.send(tr_lang(lang, "Keine Abwesenheiten gefunden.", "No absences found."), ephemeral=True)

        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\n")
        w.writerow(["UserID", "Username", "DisplayName", "From", "To", "CreatedAt"])
        for r in rows:
            w.writerow([
                str(r.get("user_id", "")),
                r.get("username", ""),
                r.get("display_name", ""),
                r.get("start", ""),
                r.get("end", ""),
                r.get("created_at", ""),
            ])
        buf.seek(0)
        file = discord.File(io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename=f"absences_{guild.id}.csv")
        await interaction.followup.send(
            tr_lang(
                lang,
                f"Hier ist die Abwesenheitsliste (**{len(rows)}** Einträge, nur für dich sichtbar).",
                f"Here is the absence list (**{len(rows)}** entries, only visible to you).",
            ),
            file=file, ephemeral=True,
        )

    # ---------- Dashboard page: absence overview (guild scope) ----------
    @dashboard_page(
        "absences",
        L("Abwesenheiten", "Absences"),
        scope="guild",
        permission="guild_mod",
        icon="calendar",
    )
    async def absences_page(self, ctx):
        view = (ctx.params or {}).get("view") or "current"
        tz = await self._tz(ctx.guild)
        today = datetime.now(tz).date()
        records = await self._get_absences(ctx.guild)

        rows = []
        for r in sorted(records, key=lambda x: str(x.get("start", ""))):
            r_start, r_end = _from_iso(r.get("start")), _from_iso(r.get("end"))
            if r_start is None or r_end is None:
                continue
            if r_end < today:
                state = "past"
            elif r_start > today:
                state = "upcoming"
            else:
                state = "active"
            if view == "current" and state == "past":
                continue
            if view in ("active", "upcoming", "past") and state != view:
                continue
            member = ctx.guild.get_member(int(r.get("user_id", 0)))
            rows.append({
                "member": member.display_name if member else str(r.get("display_name") or r.get("user_id")),
                "start": r_start.strftime("%d.%m.%Y"),
                "end": r_end.strftime("%d.%m.%Y"),
                "state": state,
            })

        controls = [
            Control.select(
                "view", L("Ansicht", "View"),
                [
                    {"value": "current", "label": L("Aktiv + geplant", "Active + upcoming")},
                    {"value": "active", "label": L("Aktiv", "Active")},
                    {"value": "upcoming", "label": L("Geplant", "Upcoming")},
                    {"value": "past", "label": L("Vergangen", "Past")},
                    {"value": "all", "label": L("Alle", "All")},
                ],
                value=view,
            )
        ]
        comps = [
            Component.heading(L("Abwesenheiten", "Absences")),
            Component.text(L(
                f"{len(rows)} Einträge · Zeitzone: {await self.config.guild(ctx.guild).timezone() or 'UTC'}",
                f"{len(rows)} entries · timezone: {await self.config.guild(ctx.guild).timezone() or 'UTC'}",
            )),
        ]
        if rows:
            comps.append(Component.table(
                columns=[
                    {"key": "member", "label": L("Mitglied", "Member")},
                    {"key": "start", "label": L("Von", "From")},
                    {"key": "end", "label": L("Bis", "To")},
                    {"key": "state", "label": L("Status", "Status")},
                ],
                rows=rows[:200],
            ))
        else:
            comps.append(Component.text(L("Keine Einträge für diese Ansicht.", "No entries for this view.")))
        return PageSchema(components=comps, controls=controls)

    # ---------- Blizzard API: ENV-first Credentials ----------
    @commands.hybrid_command(name="setblizzard", description="Owner-only: set the Blizzard API client ID/secret (ENV fallback).", extras={"i18n_desc": {"de-DE": "Nur Owner: Blizzard-API Client-ID/Secret setzen (ENV-Fallback).", "en-US": "Owner-only: set the Blizzard API client ID/secret (ENV fallback)."}})
    @commands.is_owner()
    @app_commands.describe(client_id="Blizzard API client ID", client_secret="Blizzard API client secret")
    async def set_blizzard_credentials(self, ctx: commands.Context, client_id: str, client_secret: str):
        """Owner-only: Set the Blizzard API client ID/secret (fallback when ENV is not used)."""
        # Refuse prefix invocation in a guild: the secret would be visible in chat.
        if ctx.interaction is None and ctx.guild is not None:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            await ctx.send(tr_lang(
                await self._lang(ctx.guild),
                "⚠️ Aus Sicherheitsgründen bitte den Slash-Command `/setblizzard` oder eine DM verwenden "
                "(das Secret wäre sonst im Chat sichtbar).",
                "⚠️ For security reasons please use the `/setblizzard` slash command or a DM "
                "(the secret would otherwise be visible in chat).",
            ))
            return
        await self.config.blizz_client_id.set(client_id)
        await self.config.blizz_client_secret.set(client_secret)
        await self.config.blizz_token.set("")
        await self.config.blizz_token_expires_at.set(0)
        # Clear in-memory cache as well
        self._token_mem = ""
        self._token_mem_exp = 0
        await ctx.send(tr_lang(await self._lang(ctx.guild), "Blizzard-Zugangsdaten gespeichert.", "Blizzard credentials saved."), ephemeral=True)

    @commands.hybrid_command(name="clearblizzard", description="Owner-only: remove the Blizzard API credentials from the config.", extras={"i18n_desc": {"de-DE": "Nur Owner: Blizzard-API-Zugangsdaten aus der Config entfernen.", "en-US": "Owner-only: remove the Blizzard API credentials from the config."}})
    @commands.is_owner()
    async def clear_blizzard_credentials(self, ctx: commands.Context):
        """Owner-only: Remove the Blizzard API credentials from the config."""
        await self.config.blizz_client_id.set("")
        await self.config.blizz_client_secret.set("")
        await self.config.blizz_token.set("")
        await self.config.blizz_token_expires_at.set(0)
        self._token_mem = ""
        self._token_mem_exp = 0
        await ctx.send(tr_lang(await self._lang(ctx.guild), "Blizzard-Zugangsdaten entfernt.", "Blizzard credentials cleared."), ephemeral=True)

    @app_commands.command(name="set-wow-defaults", description="Set the default region/realm for /whois.", extras={"i18n_desc": {"de-DE": "Standard-Region/-Realm für /whois festlegen.", "en-US": "Set the default region/realm for /whois."}})
    @app_commands.describe(region="eu/us/kr/tw", realm="Realm name (e.g. 'Blackmoore')")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def set_wow_defaults(self, interaction: discord.Interaction, region: str, realm: str):
        """Set the default World of Warcraft region and realm for this server."""
        lang = await self._lang(interaction.guild)
        region = region.lower()
        if region not in {"eu", "us", "kr", "tw"}:
            return await interaction.response.send_message(tr_lang(lang, "Region muss **eu/us/kr/tw** sein.", "Region must be **eu/us/kr/tw**."), ephemeral=True)
        await self.config.guild(interaction.guild).wow_default_region.set(region)
        await self.config.guild(interaction.guild).wow_default_realm.set(realm.strip())
        await interaction.response.send_message(tr_lang(lang, f"✅ Defaults gesetzt: Region **{region}**, Realm **{realm.strip()}**", f"✅ Defaults set: region **{region}**, realm **{realm.strip()}**"), ephemeral=True)

    async def _get_token(self) -> str:
        """ENV-first token acquisition. If ENV is used, token is kept only in memory; otherwise also in Config."""
        if aiohttp is None:
            raise RuntimeError("aiohttp is not installed.")

        # 1) ENV first
        env_id = os.getenv("BLIZZARD_CLIENT_ID") or ""
        env_secret = os.getenv("BLIZZARD_CLIENT_SECRET") or ""
        use_env = bool(env_id and env_secret)

        # 2) Fallback: Config
        if not use_env:
            env_id = await self.config.blizz_client_id()
            env_secret = await self.config.blizz_client_secret()
            if not (env_id and env_secret):
                raise RuntimeError("Blizzard API credentials missing. Set ENV or use `[p]setblizzard <id> <secret>`.")

        now = int(datetime.now(timezone.utc).timestamp())
        # In-memory cache is usually sufficient
        if self._token_mem and now < self._token_mem_exp - 60:
            return self._token_mem

        # When config credentials are used, also check for a still-valid token in the config
        if not use_env:
            cfg_token = await self.config.blizz_token()
            cfg_exp = await self.config.blizz_token_expires_at()
            if cfg_token and now < cfg_exp - 60:
                self._token_mem = cfg_token
                self._token_mem_exp = cfg_exp
                return cfg_token

        # Fetch a new token
        token_url = "https://oauth.battle.net/token"
        data = {"grant_type": "client_credentials"}

        async with aiohttp.ClientSession() as sess:
            async with sess.post(token_url, data=data, auth=aiohttp.BasicAuth(env_id, env_secret)) as r:
                if r.status != 200:
                    text = await r.text()
                    raise RuntimeError(f"Token request failed ({r.status}): {text}")
                js = await r.json()

        token = js.get("access_token", "")
        expires_in = int(js.get("expires_in", 0))
        exp = now + max(0, expires_in)

        # Always cache in memory ...
        self._token_mem = token
        self._token_mem_exp = exp
        # ... and only persist additionally when using config credentials
        if not use_env:
            await self.config.blizz_token.set(token)
            await self.config.blizz_token_expires_at.set(exp)

        return token

    async def _get_profile(self, region: str, realm: str, charname: str, locale: str = "en_US"):
        token = await self._get_token()
        realm_slug = _slugify_realm(realm)
        char_slug = _slugify_char(charname)
        base = f"https://{region}.api.blizzard.com"
        ns = f"profile-classic-{region}"
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession(headers=headers) as sess:
            params = {"namespace": ns, "locale": locale}
            prof_url = f"{base}/profile/wow/character/{realm_slug}/{char_slug}"
            async with sess.get(prof_url, params=params) as r:
                if r.status == 404:
                    return None
                if r.status != 200:
                    raise RuntimeError(f"Profile request failed ({r.status}).")
                prof = await r.json()
            equip_url = f"{base}/profile/wow/character/{realm_slug}/{char_slug}/equipment"
            ilvl = None
            async with sess.get(equip_url, params=params) as r2:
                if r2.status == 200:
                    eq = await r2.json()
                    ilvl = eq.get("equipped_item_level") or eq.get("average_item_level")
        prof["_equipped_ilvl"] = ilvl
        return prof

    @app_commands.command(name="whois", description="Show WoW character info (level, class, guild, iLvl if available).", extras={"i18n_desc": {"de-DE": "WoW-Charakterinfo anzeigen (Level, Klasse, Gilde, iLvl falls verfügbar).", "en-US": "Show WoW character info (level, class, guild, iLvl if available)."}})
    @app_commands.describe(charname="Character name", realm="Optional realm (otherwise the guild default)")
    @app_commands.guild_only()
    async def whois(self, interaction: discord.Interaction, charname: str, realm: str | None = None):
        """Show information about a WoW character."""
        await interaction.response.defer(ephemeral=True)
        lang = await self._lang(interaction.guild)
        gconf = self.config.guild(interaction.guild)
        region = (await gconf.wow_default_region()) or "eu"
        def_realm = (await gconf.wow_default_realm()) or ""
        realm_use = realm.strip() if realm else def_realm
        if not realm_use:
            return await interaction.followup.send(tr_lang(lang, "Bitte Realm angeben oder `/set-wow-defaults` setzen.", "Please provide a realm or set `/set-wow-defaults`."), ephemeral=True)

        locale = "de_DE" if lang.startswith("de") else "en_US"
        try:
            prof = await self._get_profile(region, realm_use, charname, locale=locale)
        except Exception as e:
            return await interaction.followup.send(tr_lang(lang, f"❌ Fehler bei der Blizzard API: {e}", f"❌ Blizzard API error: {e}"), ephemeral=True)

        if not prof:
            return await interaction.followup.send(tr_lang(lang, "❌ Charakter nicht gefunden (Name/Realm/Region prüfen).", "❌ Character not found (check name/realm/region)."), ephemeral=True)

        unknown = tr_lang(lang, "Unbekannt", "Unknown")
        name = prof.get("name", charname)
        realm_name = prof.get("realm", {}).get("name", realm_use)
        level = prof.get("level", "?")
        char_class = prof.get("character_class", {}).get("name", unknown)
        race = prof.get("race", {}).get("name", unknown)
        guild_name = prof.get("guild", {}).get("name", "—")
        ilvl = prof.get("_equipped_ilvl")
        faction = prof.get("faction", {}).get("name", "")
        last_login = prof.get("last_login_timestamp")
        last_login_str = ""
        if isinstance(last_login, int):
            dt = datetime.fromtimestamp(last_login/1000, tz=timezone.utc)
            last_login_str = dt.strftime("%d.%m.%Y %H:%M UTC")

        embed = discord.Embed(title=f"{name} @ {realm_name}", color=discord.Color.gold())
        embed.add_field(name=tr_lang(lang, "Level / Klasse", "Level / Class"), value=f"{level} / {char_class}", inline=True)
        embed.add_field(name=tr_lang(lang, "Rasse / Fraktion", "Race / Faction"), value=f"{race} / {faction or '—'}", inline=True)
        embed.add_field(name=tr_lang(lang, "Gilde", "Guild"), value=guild_name or "—", inline=True)
        if ilvl:
            embed.add_field(name=tr_lang(lang, "Ø Itemlevel", "Avg. item level"), value=str(ilvl), inline=True)
        if last_login_str:
            embed.add_field(name=tr_lang(lang, "Zuletzt eingeloggt", "Last login"), value=last_login_str, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot: Red):
    await bot.add_cog(GuildTools(bot))
