import discord
from discord import app_commands
from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from datetime import datetime, timezone
import io
import csv
import asyncio
import os
import re

try:
    import aiohttp
except ImportError:
    aiohttp = None

from .pdc_dashboard import (
    dashboard_widget, dashboard_panel, WidgetData, PanelSchema, Field, SubmitResult,
    register_dashboard, unregister_dashboard,
    L, tr, tr_lang,
)

ONLINE_STATES = {discord.Status.online, discord.Status.idle, discord.Status.dnd}
DATE_FORMATS = ["%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y"]

def _parse_date(s: str):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None

def _out_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")

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
    __version__ = "1.3.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xD0DE2025, force_registration=True)
        self.config.register_guild(
            last_seen={},
            wow_default_region="eu",
            wow_default_realm="",
            language="en-US"
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

    async def cog_load(self) -> None:
        register_dashboard(self)

    def cog_unload(self) -> None:
        unregister_dashboard(self)

    @dashboard_widget("tracked_members", L("Erfasste Mitglieder", "Tracked Members"), size="sm", permission="guild_member")
    async def tracked_members_widget(self, ctx):
        try:
            data = await self.config.guild(ctx.guild).last_seen()
            return WidgetData.kpi(value=int(len(data)), label="Erfasste Mitglieder")
        except Exception:
            return WidgetData.kpi(value="–", label="Erfasste Mitglieder")

    async def _lang(self, guild) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    @dashboard_panel(
        "language", L("Sprache", "Language"),
        mount="guild_settings", permission="guild_admin", order=99,
    )
    async def settings_panel(self, ctx):
        return PanelSchema(
            description=tr(
                ctx,
                "Sprache der Bot-Ausgaben für diesen Server.",
                "Output language for this server.",
            ),
            fields=[
                Field.select(
                    "language", L("Sprache", "Language"),
                    [
                        {"value": "de-DE", "label": "Deutsch"},
                        {"value": "en-US", "label": "English"},
                    ],
                    value=str(await self.config.guild(ctx.guild).language()),
                    reload_on_change=True,
                )
            ],
        )

    @settings_panel.on_submit
    async def _save_settings(self, ctx, data):
        lang = str(data.get("language", "en-US")).strip() or "en-US"
        await self.config.guild(ctx.guild).language.set(lang)
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
        data = await self.config.guild(after.guild).last_seen()
        data[str(after.id)] = now_iso
        await self.config.guild(after.guild).last_seen.set(data)

    # ---------- /export-userlist ----------
    @app_commands.command(name="export-userlist", description="Export all users to a CSV.", extras={"i18n_desc": {"de-DE": "Alle Benutzer als CSV exportieren.", "en-US": "Export all users to a CSV."}})
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def export_userlist(self, interaction: discord.Interaction):
        """Export the server's member list."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lang = await self._lang(guild)
        if guild is None:
            return await interaction.followup.send(tr_lang(lang, "Dieser Befehl muss in einer Guild ausgeführt werden.", "This command must be used in a server."), ephemeral=True)
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
        last_seen_map = await self.config.guild(guild).last_seen()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\n")
        w.writerow(["UserID", "Username", "Name_Auf_Server", "Rolle(n)", "Mitglied_Seit", "Zuletzt_Online"])
        for m in members:
            w.writerow([
                str(m.id),
                m.name,
                m.display_name,
                ", ".join([r.name for r in m.roles if r.name != "@everyone"]) or "",
                m.joined_at.astimezone(timezone.utc).isoformat() if m.joined_at else "",
                last_seen_map.get(str(m.id), "unbekannt"),
            ])
        buf.seek(0)
        file = discord.File(io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename=f"user_export_{guild.id}.csv")
        await interaction.followup.send(tr_lang(lang, "Hier ist dein Export (nur für dich sichtbar).", "Here is your export (only visible to you)."), file=file, ephemeral=True)

    # ---------- Abwesenheiten ----------
    @app_commands.command(name="add-absence", description="Add an absence (DD-MM-YYYY / DD.MM.YYYY / DD/MM/YYYY).", extras={"i18n_desc": {"de-DE": "Abwesenheit eintragen (TT-MM-JJJJ / TT.MM.JJJJ / TT/MM/JJJJ).", "en-US": "Add an absence (DD-MM-YYYY / DD.MM.YYYY / DD/MM/YYYY)."}})
    @app_commands.describe(von="Start date", bis="End date")
    @app_commands.guild_only()
    async def add_absence(self, interaction: discord.Interaction, von: str, bis: str):
        """Add an absence entry for a member."""
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

        data_dir = cog_data_path(raw_name=self.__class__.__name__)
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / f"absences_{guild.id}.txt"

        line = ";".join([
            str(interaction.user.id),
            interaction.user.name,
            interaction.user.display_name,
            _out_date(start),
            _out_date(end),
        ]) + "\n"

        async with self._abs_lock:
            new_file = not path.exists()
            def _write():
                with open(path, "a", encoding="utf-8") as f:
                    if new_file:
                        f.write("UserID;Username;Name auf Server;Von;Bis\n")
                    f.write(line)
            await asyncio.to_thread(_write)

        await interaction.response.send_message(
            tr_lang(
                lang,
                f"✅ Neue Abwesenheit gespeichert für **{interaction.user.mention}**\n"
                f"• Von: **{_out_date(start)}**\n"
                f"• Bis: **{_out_date(end)}**",
                f"✅ New absence saved for **{interaction.user.mention}**\n"
                f"• From: **{_out_date(start)}**\n"
                f"• To: **{_out_date(end)}**",
            ),
        )

    @app_commands.command(name="list-absence", description="Show your absences (ephemeral).", extras={"i18n_desc": {"de-DE": "Deine Abwesenheiten anzeigen (ephemer).", "en-US": "Show your absences (ephemeral)."}})
    @app_commands.guild_only()
    async def list_absence(self, interaction: discord.Interaction):
        """List recorded absences."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lang = await self._lang(guild)
        if guild is None:
            return await interaction.followup.send(tr_lang(lang, "Dieser Befehl muss in einer Guild ausgeführt werden.", "This command must be used in a server."), ephemeral=True)
        data_dir = cog_data_path(raw_name=self.__class__.__name__)
        path = data_dir / f"absences_{guild.id}.txt"
        if not path.exists():
            return await interaction.followup.send(tr_lang(lang, "Keine Abwesenheiten gefunden.", "No absences found."), ephemeral=True)

        uid = str(interaction.user.id)
        async with self._abs_lock:
            def _read_rows():
                out = []
                with open(path, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        if i == 0:  # Header
                            continue
                        parts = line.rstrip("\n").split(";")
                        if len(parts) >= 5 and parts[0] == uid:
                            out.append(parts)
                return out
            rows = await asyncio.to_thread(_read_rows)

        if not rows:
            return await interaction.followup.send(tr_lang(lang, "Du hast keine Abwesenheiten hinterlegt.", "You have no absences on file."), ephemeral=True)

        desc = "\n".join(
            tr_lang(lang, f"• **{r[3]}** → **{r[4]}** (als *{r[2]}*)", f"• **{r[3]}** → **{r[4]}** (as *{r[2]}*)")
            for r in rows
        )
        embed = discord.Embed(title=tr_lang(lang, "Deine Abwesenheiten", "Your absences"), description=desc, color=discord.Color.blurple())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="get-absence", description="CSV with all absences (mods only).", extras={"i18n_desc": {"de-DE": "CSV mit allen Abwesenheiten (nur Mods).", "en-US": "CSV with all absences (mods only)."}})
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def get_absence(self, interaction: discord.Interaction):
        """Show the absence entry for a member."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lang = await self._lang(guild)
        if guild is None:
            return await interaction.followup.send(tr_lang(lang, "Dieser Befehl muss in einer Guild ausgeführt werden.", "This command must be used in a server."), ephemeral=True)
        data_dir = cog_data_path(raw_name=self.__class__.__name__)
        path = data_dir / f"absences_{guild.id}.txt"
        if not path.exists():
            return await interaction.followup.send(tr_lang(lang, "Keine Abwesenheiten gefunden.", "No absences found."), ephemeral=True)

        async with self._abs_lock:
            def _read_all():
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            content = await asyncio.to_thread(_read_all)

        out_bytes = ("\ufeff" + content).encode("utf-8")
        file = discord.File(io.BytesIO(out_bytes), filename=f"absences_{guild.id}.csv")
        await interaction.followup.send(tr_lang(lang, "Hier ist die Abwesenheitsliste (nur für dich sichtbar).", "Here is the absence list (only visible to you)."), file=file, ephemeral=True)

    # ---------- Blizzard API: ENV-first Credentials ----------
    @commands.hybrid_command(name="setblizzard", description="Owner-only: set the Blizzard API client ID/secret (ENV fallback).", extras={"i18n_desc": {"de-DE": "Nur Owner: Blizzard-API Client-ID/Secret setzen (ENV-Fallback).", "en-US": "Owner-only: set the Blizzard API client ID/secret (ENV fallback)."}})
    @commands.is_owner()
    @app_commands.describe(client_id="Blizzard API client ID", client_secret="Blizzard API client secret")
    async def set_blizzard_credentials(self, ctx: commands.Context, client_id: str, client_secret: str):
        """Owner-only: Set the Blizzard API client ID/secret (fallback when ENV is not used)."""
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
            raise RuntimeError("aiohttp nicht installiert.")

        # 1) ENV first
        env_id = os.getenv("BLIZZARD_CLIENT_ID") or ""
        env_secret = os.getenv("BLIZZARD_CLIENT_SECRET") or ""
        use_env = bool(env_id and env_secret)

        # 2) Fallback: Config
        if not use_env:
            env_id = await self.config.blizz_client_id()
            env_secret = await self.config.blizz_client_secret()
            if not (env_id and env_secret):
                raise RuntimeError("Blizzard API Credentials fehlen. Setze ENV oder nutze `[p]setblizzard <id> <secret>`.")

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
                    raise RuntimeError(f"Token-Request fehlgeschlagen ({r.status}): {text}")
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

    async def _get_profile(self, region: str, realm: str, charname: str, locale: str = "de_DE"):
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
                    raise RuntimeError(f"Profil-Request fehlgeschlagen ({r.status}).")
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
        """Show information about a member."""
        await interaction.response.defer(ephemeral=True)
        lang = await self._lang(interaction.guild)
        gconf = self.config.guild(interaction.guild)
        region = (await gconf.wow_default_region()) or "eu"
        def_realm = (await gconf.wow_default_realm()) or ""
        realm_use = realm.strip() if realm else def_realm
        if not realm_use:
            return await interaction.followup.send(tr_lang(lang, "Bitte Realm angeben oder `/set-wow-defaults` setzen.", "Please provide a realm or set `/set-wow-defaults`."), ephemeral=True)

        locale = "en_US" if lang.startswith("en") else "de_DE"
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
