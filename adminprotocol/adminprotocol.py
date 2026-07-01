import logging
import discord
from redbot.core import Config, commands
from typing import Any, Dict, List, Optional, Literal
from datetime import timedelta
import asyncio
import html
import json

from .pdc_dashboard import (
    dashboard_widget, dashboard_panel, WidgetData,
    PanelSchema, Field, SubmitResult,
    register_dashboard, unregister_dashboard,
    L, tr, tr_lang,
)

log = logging.getLogger("red.pdc.adminprotocol")

try:
    from pdc_dashboard.rpc.third_parties import dashboard_page as _dashboard_page  # type: ignore
except Exception:
    try:
        from dashboard.rpc.third_parties import dashboard_page as _dashboard_page  # type: ignore
    except Exception:
        def _dashboard_page(*args: Any, **kwargs: Any):  # type: ignore
            def decorator(func: Any) -> Any:
                func.__dashboard_decorator_params__ = (args, kwargs)
                return func
            return decorator

# Bilingual event names: (de, en). Resolve with tr(ctx, de, en) in the
# dashboard panel, or [0]/[1] for per-guild language elsewhere.
EVENTS = {
    "message_edit": ("Nachricht bearbeitet", "Message edited"),
    "user_ban": ("Benutzer gebannt", "User banned"),
    "user_timeout": ("Timeout (gegeben / entfernt)", "Timeout (given / removed)"),
    "channel_create": ("Kanal erstellt", "Channel created"),
    "thread_create": ("Thread erstellt", "Thread created"),
    "role_create": ("Rolle angelegt", "Role created"),
    "channel_delete": ("Kanal gelöscht", "Channel deleted"),
    "thread_delete": ("Thread gelöscht", "Thread deleted"),
    "message_delete": ("Nachricht gelöscht", "Message deleted"),
    "role_delete": ("Rolle gelöscht", "Role deleted"),
    "user_kick": ("Benutzer gekickt", "User kicked"),
    "voice_move": ("Benutzer verschoben (Sprachkanal)", "User moved (voice channel)"),
    "voice_disconnect": ("Sprachkanal-Verbindung getrennt", "Voice channel disconnected"),
    "user_join": ("Benutzer beigetreten", "User joined"),
    "nickname_change_other": ("Nickname verändert (Fremd)", "Nickname changed (other)"),
    "user_leave": ("Benutzer ausgetreten", "User left"),
    "nickname_change_self": ("Nickname geändert (Selbst)", "Nickname changed (self)"),
    "mod_command": ("Moderationsbefehl verwendet", "Moderation command used"),
    "role_add": ("Rolle vergeben", "Role added"),
    "role_remove": ("Rolle entfernt", "Role removed"),
    "invite_create": ("Server-Einladung erstellt", "Server invite created"),
    "user_unban": ("Benutzer entbannt", "User unbanned"),
    "channel_update": ("Kanal aktualisiert/modifiziert", "Channel updated/modified"),
    "thread_update": ("Thread aktualisiert/modifiziert", "Thread updated/modified"),
    "voice_join": ("Sprachkanal beigetreten", "Voice channel joined"),
    "voice_leave": ("Sprachkanal verlassen", "Voice channel left"),
    "voice_status": ("Sprachstatus geändert", "Voice status changed"),
    "voice_switch": ("Sprachkanal gewechselt", "Voice channel switched"),
}

# Grouping of events into categories (for clear tabs in the dashboard).
EVENT_CATEGORIES = {
    "messages": ("Nachrichten & Kanäle", [
        "message_edit", "message_delete",
        "channel_create", "channel_delete", "channel_update",
        "thread_create", "thread_delete", "thread_update",
    ]),
    "members": ("Mitglieder & Rollen", [
        "user_join", "user_leave",
        "nickname_change_other", "nickname_change_self",
        "role_create", "role_delete", "role_add", "role_remove",
    ]),
    "moderation": ("Moderation", [
        "user_ban", "user_unban", "user_kick", "user_timeout", "mod_command",
    ]),
    "voice": ("Sprachkanäle & Einladungen", [
        "voice_join", "voice_leave", "voice_switch", "voice_move",
        "voice_disconnect", "voice_status", "invite_create",
    ]),
}

def format_duration(seconds: float, lang: str = "en-US") -> str:
    if seconds <= 0:
        return tr_lang(lang, "Permanent / Sofort", "Permanent / Instant")
    parts = []
    days, remainder = divmod(int(seconds), 86400)
    if days > 0:
        parts.append(tr_lang(lang, f"{days} Tage", f"{days} days"))
    hours, remainder = divmod(remainder, 3600)
    if hours > 0:
        parts.append(tr_lang(lang, f"{hours} Stunden", f"{hours} hours"))
    minutes, secs = divmod(remainder, 60)
    if minutes > 0:
        parts.append(tr_lang(lang, f"{minutes} Minuten", f"{minutes} minutes"))
    if secs > 0 and not parts:
        parts.append(tr_lang(lang, f"{secs} Sekunden", f"{secs} seconds"))
    return " ".join(parts)

class AdminProtocol(commands.Cog):
    """Logs administrative actions and user activity on the server."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8912389124, force_registration=True)
        
        default_events = {
            ev: {
                "enabled": False,
                "channel": None,
                "ignored_channels": [],
                "ignored_users": [],
                "ignored_roles": []
            }
            for ev in EVENTS
        }
        self.config.register_guild(language="en-US", events=default_events)
        self._dashboard_attached = False
        self._selected_event = {}  # (guild_id, user_id, category) -> event_id

    async def cog_load(self) -> None:
        dashboard = self.bot.get_cog("pdc_webdashboard") or self.bot.get_cog("WebDashboard") or self.bot.get_cog("Dashboard")
        if dashboard is not None:
            try:
                dashboard.rpc.third_parties_handler.add_third_party(self, overwrite=True)
                self._dashboard_attached = True
            except Exception:
                self._dashboard_attached = False
        register_dashboard(self)

    @dashboard_widget("adminprotocol_enabled_events", L("Aktive Log-Events", "Active Log Events"), size="sm", permission="guild_member")
    async def adminprotocol_enabled_events_widget(self, ctx):
        try:
            events = await self.config.guild(ctx.guild).events()
            count = sum(1 for ev in events.values() if isinstance(ev, dict) and ev.get("enabled"))
            return WidgetData.kpi(value=count, label="Aktive Log-Events")
        except Exception:
            return WidgetData.kpi(value="–", label="Aktive Log-Events")

    # --- Guild panels: log events by category (clear tabs) ---- #
    async def _ap_events_schema(self, ctx, category: str, keys: list, with_language: bool = False):
        guild_id = ctx.guild.id
        user_id = ctx.user.id
        self._selected_event.setdefault(guild_id, {}).setdefault(user_id, {})
        sel_ev = self._selected_event[guild_id][user_id].get(category, "0")

        def _ev_label(ev):
            name = EVENTS.get(ev, ev)
            if isinstance(name, (tuple, list)):
                return tr(ctx, name[0], name[1])
            return name

        # Build dropdown choices
        event_choices = [{"value": "0", "label": tr(ctx, "-- Ereignis wählen --", "-- Select event --")}]
        for ev in keys:
            event_choices.append({"value": ev, "label": _ev_label(ev)})

        # Ensure selection is still valid
        choice_vals = {v["value"] for v in event_choices}
        if sel_ev not in choice_vals:
            sel_ev = "0"
            self._selected_event[guild_id][user_id][category] = "0"

        fields = []
        fields.append(
            Field.select("event_id", tr(ctx, "Ereignis", "Event"), event_choices, value=sel_ev, reload_on_change=True)
        )

        if sel_ev != "0":
            events = await self.config.guild(ctx.guild).events()
            if not isinstance(events, dict):
                events = {}
            cfg = events.get(sel_ev, {}) if isinstance(events.get(sel_ev), dict) else {}

            channel_options = [{"value": "", "label": tr(ctx, "— kein Kanal —", "— no channel —")}] + [
                {"value": str(c.id), "label": "#" + c.name} for c in ctx.guild.text_channels
            ]
            role_opts = [
                {"value": str(r.id), "label": r.name}
                for r in ctx.guild.roles if r.name != "@everyone"
            ]

            fields.extend([
                Field.switch("enabled", tr(ctx, "Aktiviert", "Enabled"), value=bool(cfg.get("enabled", False))),
                Field.select("channel", tr(ctx, "Log-Kanal", "Log channel"), channel_options, value=str(cfg.get("channel") or "")),
                Field.multiselect("ignored_channels", tr(ctx, "Ignorierte Kanäle", "Ignored channels"), channel_options[1:], value=[str(x) for x in cfg.get("ignored_channels", [])]),
                Field.multiselect("ignored_roles", tr(ctx, "Ignorierte Rollen", "Ignored roles"), role_opts, value=[str(x) for x in cfg.get("ignored_roles", [])]),
                Field.text("ignored_users", tr(ctx, "Ignorierte User-IDs (mit Komma getrennt)", "Ignored user IDs (comma-separated)"), value=", ".join(str(x) for x in cfg.get("ignored_users", [])), placeholder=tr(ctx, "z. B. 123, 456", "e.g. 123, 456"))
            ])

        return PanelSchema(description=tr(ctx, "Pro Ereignis aktivieren, Ziel-Kanal und Ausnahmen wählen.", "Per event: enable, choose the target channel and exceptions."), fields=fields)

    async def _ap_events_save(self, ctx, category: str, keys: list, data: dict, with_language: bool = False):
        guild_id = ctx.guild.id
        user_id = ctx.user.id
        self._selected_event.setdefault(guild_id, {}).setdefault(user_id, {})
        current_sel = self._selected_event[guild_id][user_id].get(category, "0")

        # Tolerate a stray language field (the language selector now lives in its
        # own dedicated panel, but keep this harmless for safety).
        if "language" in data:
            await self.config.guild(ctx.guild).language.set("en-US" if data["language"] == "en-US" else "de-DE")

        submitted_ev = str(data.get("event_id", "0")).strip()
        if submitted_ev != current_sel:
            # User switched dropdown selection
            self._selected_event[guild_id][user_id][category] = submitted_ev
            return SubmitResult.ok()

        if submitted_ev == "0":
            return SubmitResult.fail(tr(ctx, "Bitte wähle ein Ereignis aus.", "Please select an event."))

        events = await self.config.guild(ctx.guild).events()
        if not isinstance(events, dict):
            events = {}

        cfg = events.get(submitted_ev, {}) if isinstance(events.get(submitted_ev), dict) else {}

        # Save values
        cfg["enabled"] = bool(data.get("enabled", False))

        ch = data.get("channel")
        cfg["channel"] = int(ch) if ch else None

        def _ids(raw):
            if isinstance(raw, list):
                return [int(x) for x in raw if str(x).strip().isdigit()]
            return [int(x.strip()) for x in str(raw or "").split(",") if x.strip().isdigit()]

        cfg["ignored_channels"] = _ids(data.get("ignored_channels", []))
        cfg["ignored_roles"] = _ids(data.get("ignored_roles", []))
        cfg["ignored_users"] = _ids(data.get("ignored_users", ""))

        events[submitted_ev] = cfg
        await self.config.guild(ctx.guild).events.set(events)
        _name = EVENTS.get(submitted_ev, submitted_ev)
        ev_de = _name[0] if isinstance(_name, (tuple, list)) else _name
        ev_en = _name[1] if isinstance(_name, (tuple, list)) else _name
        return SubmitResult.ok(tr(ctx, f"Einstellungen für '{ev_de}' gespeichert.", f"Settings for '{ev_en}' saved."))

    @dashboard_panel("events_messages", L("Nachrichten & Kanäle", "Messages & Channels"), mount="guild_settings", permission="guild_admin", order=1)
    async def ap_panel_messages(self, ctx):
        return await self._ap_events_schema(ctx, "messages", EVENT_CATEGORIES["messages"][1], with_language=False)

    @ap_panel_messages.on_submit
    async def _ap_save_messages(self, ctx, data):
        return await self._ap_events_save(ctx, "messages", EVENT_CATEGORIES["messages"][1], data, with_language=False)

    @dashboard_panel("events_members", L("Mitglieder & Rollen", "Members & Roles"), mount="guild_settings", permission="guild_admin", order=2)
    async def ap_panel_members(self, ctx):
        return await self._ap_events_schema(ctx, "members", EVENT_CATEGORIES["members"][1])

    @ap_panel_members.on_submit
    async def _ap_save_members(self, ctx, data):
        return await self._ap_events_save(ctx, "members", EVENT_CATEGORIES["members"][1], data)

    @dashboard_panel("events_moderation", L("Moderation", "Moderation"), mount="guild_settings", permission="guild_admin", order=3)
    async def ap_panel_moderation(self, ctx):
        return await self._ap_events_schema(ctx, "moderation", EVENT_CATEGORIES["moderation"][1])

    @ap_panel_moderation.on_submit
    async def _ap_save_moderation(self, ctx, data):
        return await self._ap_events_save(ctx, "moderation", EVENT_CATEGORIES["moderation"][1], data)

    @dashboard_panel("events_voice", L("Sprachkanäle & Einladungen", "Voice Channels & Invites"), mount="guild_settings", permission="guild_admin", order=4)
    async def ap_panel_voice(self, ctx):
        return await self._ap_events_schema(ctx, "voice", EVENT_CATEGORIES["voice"][1])

    @ap_panel_voice.on_submit
    async def _ap_save_voice(self, ctx, data):
        return await self._ap_events_save(ctx, "voice", EVENT_CATEGORIES["voice"][1], data)

    # --- Dedicated language tab ---- #
    @dashboard_panel("ap_language", L("Sprache", "Language"), mount="guild_settings", permission="guild_admin", order=99)
    async def ap_language_panel(self, ctx):
        return PanelSchema(
            description=tr(ctx, "Sprache der Bot-Ausgaben für diesen Server.", "Output language for this server."),
            fields=[
                Field.select("language", L("Sprache", "Language"),
                    [{"value": "de-DE", "label": "Deutsch"}, {"value": "en-US", "label": "English"}],
                    value=str(await self.config.guild(ctx.guild).language()), reload_on_change=True),
            ],
        )

    @ap_language_panel.on_submit
    async def _ap_language_save(self, ctx, data):
        if "language" in data:
            await self.config.guild(ctx.guild).language.set("en-US" if data.get("language") == "en-US" else "de-DE")
        return SubmitResult.ok(tr(ctx, "Gespeichert.", "Saved."))

    async def cog_unload(self) -> None:
        unregister_dashboard(self)
        dashboard = self.bot.get_cog("pdc_webdashboard") or self.bot.get_cog("WebDashboard") or self.bot.get_cog("Dashboard")
        if dashboard is not None:
            try:
                dashboard.rpc.third_parties_handler.remove_third_party(self)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog: commands.Cog) -> None:
        if self._dashboard_attached:
            return
        try:
            dashboard_cog.rpc.third_parties_handler.add_third_party(self, overwrite=True)
            self._dashboard_attached = True
        except Exception:
            self._dashboard_attached = False

    @commands.Cog.listener()
    async def on_cog_add(self, cog: commands.Cog) -> None:
        if self._dashboard_attached:
            return
        if cog.qualified_name not in {"Dashboard", "WebDashboard", "pdc_webdashboard"}:
            return
        try:
            cog.rpc.third_parties_handler.add_third_party(self, overwrite=True)
            self._dashboard_attached = True
        except Exception:
            self._dashboard_attached = False

    # ------------------------------------------------------------
    # Helpers & Ignores
    # ------------------------------------------------------------

    async def _is_ignored(
        self,
        guild: discord.Guild,
        event_name: str,
        *,
        channel: Optional[discord.abc.GuildChannel] = None,
        member: Optional[discord.Member] = None,
        role: Optional[discord.Role] = None,
        actor: Optional[discord.Member | discord.User] = None,
    ) -> bool:
        event_conf = await self.config.guild(guild).events.get_raw(event_name)
        
        ignored_channels = event_conf.get("ignored_channels", [])
        ignored_users = event_conf.get("ignored_users", [])
        ignored_roles = event_conf.get("ignored_roles", [])
        
        # 1. Channel check
        if channel and channel.id in ignored_channels:
            return True
            
        # 2. Member check (subject)
        if member:
            if member.id in ignored_users:
                return True
            if hasattr(member, "roles"):
                for r in member.roles:
                    if r.id in ignored_roles:
                        return True
                        
        # 3. Role check
        if role and role.id in ignored_roles:
            return True
            
        # 4. Actor check (moderator/initiator)
        if actor:
            if actor.id in ignored_users:
                return True
            if hasattr(actor, "roles"):
                for r in actor.roles:
                    if r.id in ignored_roles:
                        return True

        return False

    async def _post_embed(
        self,
        guild: discord.Guild,
        event_name: str,
        embed: discord.Embed,
        *,
        channel: Optional[discord.abc.GuildChannel] = None,
        member: Optional[discord.Member] = None,
        role: Optional[discord.Role] = None,
        actor: Optional[discord.Member | discord.User] = None,
    ):
        event_conf = await self.config.guild(guild).events.get_raw(event_name)
        if not event_conf.get("enabled", False):
            return
            
        dest_channel_id = event_conf.get("channel")
        if not dest_channel_id:
            return
            
        if await self._is_ignored(guild, event_name, channel=channel, member=member, role=role, actor=actor):
            return
            
        dest_channel = guild.get_channel(dest_channel_id)
        if dest_channel:
            try:
                await dest_channel.send(embed=embed)
            except discord.Forbidden:
                log.debug(f"Missing permissions to log to channel {dest_channel_id} for event {event_name}")
            except Exception as e:
                log.error(f"Error sending log embed: {e}")

    async def _get_audit_log_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: Optional[int] = None,
        max_age_seconds: float = 15.0
    ) -> Optional[discord.AuditLogEntry]:
        try:
            if not guild.me.guild_permissions.view_audit_log:
                return None
            now = discord.utils.utcnow()
            async for entry in guild.audit_logs(action=action, limit=5):
                if target_id is not None and entry.target and entry.target.id != target_id:
                    continue
                age = (now - entry.created_at).total_seconds()
                if age <= max_age_seconds:
                    return entry
        except Exception as e:
            log.debug(f"Failed to fetch audit log for action {action}: {e}")
        return None

    def _is_mod_command(self, command_name: str, cog_name: Optional[str]) -> bool:
        if cog_name in {"Mod", "Admin", "AdminUtils"}:
            return True
        if command_name in {"kick", "ban", "timeout", "warn", "mute", "unmute", "unban", "purge", "purgefast", "messagemove"}:
            return True
        return False

    # ------------------------------------------------------------
    # Discord Event Listeners
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot:
            return
        if before.content == after.content:
            return

        lang = await self.config.guild(after.guild).language()
        embed = discord.Embed(
            color=0xf1c40f,  # Orange
            description=tr_lang(lang, f"📝 **Nachricht gesendet von** {after.author.mention} in {after.channel.mention} **bearbeitet.** [Zur Nachricht springen]({after.jump_url})", f"📝 **Message sent by** {after.author.mention} in {after.channel.mention} **edited.** [Jump to message]({after.jump_url})")
        )
        embed.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        embed.add_field(name=tr_lang(lang, "Alt", "Old"), value=f">>> {before.content[:1010]}" if before.content else tr_lang(lang, "> *kein Text*", "> *no text*"), inline=False)
        embed.add_field(name=tr_lang(lang, "Neu", "New"), value=f">>> {after.content[:1010]}" if after.content else tr_lang(lang, "> *kein Text*", "> *no text*"), inline=False)
        embed.set_footer(text=f"ID: {after.author.id}")
        embed.timestamp = discord.utils.utcnow()

        await self._post_embed(after.guild, "message_edit", embed, channel=after.channel, member=after.author)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        channel = guild.get_channel(payload.channel_id)
        if not channel:
            return

        lang = await self.config.guild(guild).language()
        message = payload.cached_message
        if message:
            if message.author.bot:
                return
            author = message.author
            content = message.content[:1010] if message.content else tr_lang(lang, "*Kein Textinhalt oder nur Medien*", "*No text content or media only*")
        else:
            author = None
            content = tr_lang(lang, "*Inhalt nicht im Bot-Cache vorhanden (ältere Nachricht)*", "*Content not present in bot cache (older message)*")

        # Lookup who deleted
        moderator = None
        if author:
            # We look up audit log for message deletion targeting message author
            entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.message_delete, target_id=author.id)
            if entry and entry.extra and entry.extra.channel and entry.extra.channel.id == channel.id:
                moderator = entry.user

        # If no moderator found, assume author did it
        deleter_str = moderator.mention if moderator else (author.mention if author else tr_lang(lang, "Unbekannt (Selbst/Mod)", "Unknown (Self/Mod)"))

        embed = discord.Embed(
            color=0xe74c3c,  # Rot
            description=tr_lang(lang, f"🗑️ **Nachricht gesendet von** {author.mention if author else 'Unbekannt'} in {channel.mention} **gelöscht.**", f"🗑️ **Message sent by** {author.mention if author else 'Unknown'} in {channel.mention} **deleted.**")
        )
        if author:
            embed.set_author(name=str(author), icon_url=author.display_avatar.url)
        embed.add_field(name=tr_lang(lang, "Nachrichteninhalt", "Message content"), value=f">>> {content}", inline=False)
        embed.add_field(name=tr_lang(lang, "Verfasst von", "Authored by"), value=author.mention if author else tr_lang(lang, "Unbekannt", "Unknown"), inline=True)
        embed.add_field(name=tr_lang(lang, "Gelöscht von", "Deleted by"), value=deleter_str, inline=True)
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "message_delete", embed, channel=channel, member=author, actor=moderator)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member):
        lang = await self.config.guild(guild).language()
        entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.ban, target_id=user.id)
        moderator = entry.user if entry else None
        reason = entry.reason if entry and entry.reason else tr_lang(lang, "Keine Begründung angegeben", "No reason provided")

        embed = discord.Embed(
            color=0xe74c3c,
            title=tr_lang(lang, "🔨 Benutzer gebannt", "🔨 User banned"),
            description=tr_lang(lang,
                        f"👤 **Benutzer:** {user.mention} ({user.id})\n"
                        f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unbekannt'}\n"
                        f"📝 **Begründung:** {reason}\n"
                        f"⏱️ **Dauer:** Unbegrenzt / Permanent",
                        f"👤 **User:** {user.mention} ({user.id})\n"
                        f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unknown'}\n"
                        f"📝 **Reason:** {reason}\n"
                        f"⏱️ **Duration:** Unlimited / Permanent")
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url if user.display_avatar else None)
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "user_ban", embed, member=user, actor=moderator)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        lang = await self.config.guild(guild).language()
        entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.unban, target_id=user.id)
        moderator = entry.user if entry else None

        embed = discord.Embed(
            color=0x2ecc71,
            title=tr_lang(lang, "🔓 Benutzer entbannt", "🔓 User unbanned"),
            description=tr_lang(lang,
                        f"👤 **Benutzer:** {user.mention} ({user.id})\n"
                        f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unbekannt'}",
                        f"👤 **User:** {user.mention} ({user.id})\n"
                        f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unknown'}")
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url if user.display_avatar else None)
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "user_unban", embed, member=user, actor=moderator)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        lang = await self.config.guild(guild).language()

        # Check if member was kicked
        entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.kick, target_id=member.id)

        if entry:
            # Kick event
            moderator = entry.user
            reason = entry.reason if entry.reason else tr_lang(lang, "Keine Begründung angegeben", "No reason provided")

            embed = discord.Embed(
                color=0xe74c3c,
                title=tr_lang(lang, "👢 Benutzer gekickt", "👢 User kicked"),
                description=tr_lang(lang,
                            f"👤 **Benutzer:** {member.mention} ({member.id})\n"
                            f"🛡️ **Moderator:** {moderator.mention}\n"
                            f"📝 **Begründung:** {reason}",
                            f"👤 **User:** {member.mention} ({member.id})\n"
                            f"🛡️ **Moderator:** {moderator.mention}\n"
                            f"📝 **Reason:** {reason}")
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="adminprotocol")
            
            await self._post_embed(guild, "user_kick", embed, member=member, actor=moderator)
        else:
            # Leave event
            embed = discord.Embed(
                color=0xe74c3c,
                description=tr_lang(lang, f"👋 **{member.mention}** ({str(member)}) hat den Server verlassen.", f"👋 **{member.mention}** ({str(member)}) left the server.")
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text=f"ID: {member.id}")
            
            await self._post_embed(guild, "user_leave", embed, member=member)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        lang = await self.config.guild(guild).language()
        created_at = member.created_at
        now = discord.utils.utcnow()
        age_delta = now - created_at

        # Format account age nicely
        days = age_delta.days
        if days > 365:
            years = round(days / 365.25, 1)
            age_str = tr_lang(lang, f"{years} Jahre alt", f"{years} years old")
        elif days > 30:
            months = round(days / 30.4, 1)
            age_str = tr_lang(lang, f"{months} Monate alt", f"{months} months old")
        else:
            age_str = tr_lang(lang, f"{days} Tage alt", f"{days} days old")

        embed = discord.Embed(
            color=0x2ecc71,
            description=tr_lang(lang,
                        f"📥 {member.mention} **trat dem Server bei.**\n\n"
                        f"🧭 **Alter des Kontos:**\n"
                        f"`{created_at.strftime('%d/%m/%Y %H:%M')}`\n"
                        f"*{age_str}*",
                        f"📥 {member.mention} **joined the server.**\n\n"
                        f"🧭 **Account age:**\n"
                        f"`{created_at.strftime('%d/%m/%Y %H:%M')}`\n"
                        f"*{age_str}*")
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.timestamp = now
        embed.set_footer(text=f"ID: {member.id}")

        await self._post_embed(guild, "user_join", embed, member=member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild
        lang = await self.config.guild(guild).language()

        # 1. Timeout (gegeben / entfernt)
        if before.timed_out_until != after.timed_out_until:
            entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.member_update, target_id=after.id)
            moderator = entry.user if entry else None
            reason = entry.reason if entry and entry.reason else tr_lang(lang, "Keine Begründung angegeben", "No reason provided")

            if after.timed_out_until and after.timed_out_until > discord.utils.utcnow():
                # Given
                duration_sec = (after.timed_out_until - discord.utils.utcnow()).total_seconds()
                duration_str = format_duration(duration_sec, lang)

                embed = discord.Embed(
                    color=0xf1c40f,
                    title=tr_lang(lang, "⏱️ Timeout gegeben", "⏱️ Timeout given"),
                    description=tr_lang(lang,
                                f"👤 **Benutzer:** {after.mention} ({after.id})\n"
                                f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unbekannt'}\n"
                                f"📝 **Begründung:** {reason}\n"
                                f"⏱️ **Dauer:** {duration_str}",
                                f"👤 **User:** {after.mention} ({after.id})\n"
                                f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unknown'}\n"
                                f"📝 **Reason:** {reason}\n"
                                f"⏱️ **Duration:** {duration_str}")
                )
                embed.set_author(name=str(after), icon_url=after.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                embed.set_footer(text="adminprotocol")
                await self._post_embed(guild, "user_timeout", embed, member=after, actor=moderator)
            else:
                # Removed
                embed = discord.Embed(
                    color=0x2ecc71,
                    title=tr_lang(lang, "⏱️ Timeout entfernt", "⏱️ Timeout removed"),
                    description=tr_lang(lang,
                                f"👤 **Benutzer:** {after.mention} ({after.id})\n"
                                f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unbekannt'}",
                                f"👤 **User:** {after.mention} ({after.id})\n"
                                f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unknown'}")
                )
                embed.set_author(name=str(after), icon_url=after.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                embed.set_footer(text="adminprotocol")
                await self._post_embed(guild, "user_timeout", embed, member=after, actor=moderator)

        # 2. Nickname changed
        if before.nick != after.nick:
            entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.member_update, target_id=after.id)
            # Check if updated by someone else
            if entry and entry.user and entry.user.id != after.id:
                # Nickname changed (by someone else)
                embed = discord.Embed(
                    color=0xf1c40f,
                    title=tr_lang(lang, "🏷️ Nickname verändert (Fremd)", "🏷️ Nickname changed (By other)"),
                    description=tr_lang(lang,
                                f"👤 **Benutzer:** {after.mention} ({after.id})\n"
                                f"🛡️ **Geändert von:** {entry.user.mention}\n"
                                f"➖ **Alter Nickname:** `{before.nick or 'Keiner'}`\n"
                                f"➕ **Neuer Nickname:** `{after.nick or 'Keiner'}`",
                                f"👤 **User:** {after.mention} ({after.id})\n"
                                f"🛡️ **Changed by:** {entry.user.mention}\n"
                                f"➖ **Old nickname:** `{before.nick or 'None'}`\n"
                                f"➕ **New nickname:** `{after.nick or 'None'}`")
                )
                embed.set_author(name=str(after), icon_url=after.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                embed.set_footer(text="adminprotocol")
                await self._post_embed(guild, "nickname_change_other", embed, member=after, actor=entry.user)
            else:
                # Nickname changed (by self)
                embed = discord.Embed(
                    color=0xf1c40f,
                    title=tr_lang(lang, "🏷️ Nickname geändert (Selbst)", "🏷️ Nickname changed (Self)"),
                    description=tr_lang(lang,
                                f"👤 **Benutzer:** {after.mention} ({after.id})\n"
                                f"➖ **Alter Nickname:** `{before.nick or 'Keiner'}`\n"
                                f"➕ **Neuer Nickname:** `{after.nick or 'Keiner'}`",
                                f"👤 **User:** {after.mention} ({after.id})\n"
                                f"➖ **Old nickname:** `{before.nick or 'None'}`\n"
                                f"➕ **New nickname:** `{after.nick or 'None'}`")
                )
                embed.set_author(name=str(after), icon_url=after.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                embed.set_footer(text="adminprotocol")
                await self._post_embed(guild, "nickname_change_self", embed, member=after)

        # 3. Roles added
        added_roles = set(after.roles) - set(before.roles)
        for role in added_roles:
            entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.member_role_update, target_id=after.id)
            moderator = entry.user if entry else None
            
            embed = discord.Embed(
                color=0x2ecc71,
                title=tr_lang(lang, "🛡️ Rolle vergeben", "🛡️ Role added"),
                description=tr_lang(lang,
                            f"👤 **Benutzer:** {after.mention} ({after.id})\n"
                            f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unbekannt'}\n"
                            f"🏷️ **Rolle:** {role.mention} ({role.name})",
                            f"👤 **User:** {after.mention} ({after.id})\n"
                            f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unknown'}\n"
                            f"🏷️ **Role:** {role.mention} ({role.name})")
            )
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="adminprotocol")
            await self._post_embed(guild, "role_add", embed, member=after, role=role, actor=moderator)

        # 4. Roles removed
        removed_roles = set(before.roles) - set(after.roles)
        for role in removed_roles:
            entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.member_role_update, target_id=after.id)
            moderator = entry.user if entry else None
            
            embed = discord.Embed(
                color=0xe74c3c,
                title=tr_lang(lang, "🛡️ Rolle entfernt", "🛡️ Role removed"),
                description=tr_lang(lang,
                            f"👤 **Benutzer:** {after.mention} ({after.id})\n"
                            f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unbekannt'}\n"
                            f"🏷️ **Rolle:** {role.mention} ({role.name})",
                            f"👤 **User:** {after.mention} ({after.id})\n"
                            f"🛡️ **Moderator:** {moderator.mention if moderator else 'Unknown'}\n"
                            f"🏷️ **Role:** {role.mention} ({role.name})")
            )
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="adminprotocol")
            await self._post_embed(guild, "role_remove", embed, member=after, role=role, actor=moderator)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        guild = channel.guild
        lang = await self.config.guild(guild).language()

        embed = discord.Embed(
            color=0x2ecc71,
            title=tr_lang(lang, "🆕 Kanal erstellt", "🆕 Channel created"),
            description=tr_lang(lang,
                        f"🌐 **Kanal:** {channel.mention if hasattr(channel, 'mention') else f'#{channel.name}'}\n"
                        f"🏷️ **Typ:** `{channel.type.name}`\n"
                        f"🆔 **ID:** `{channel.id}`",
                        f"🌐 **Channel:** {channel.mention if hasattr(channel, 'mention') else f'#{channel.name}'}\n"
                        f"🏷️ **Type:** `{channel.type.name}`\n"
                        f"🆔 **ID:** `{channel.id}`")
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "channel_create", embed, channel=channel)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        guild = channel.guild
        lang = await self.config.guild(guild).language()

        embed = discord.Embed(
            color=0xe74c3c,
            title=tr_lang(lang, "🗑️ Kanal gelöscht", "🗑️ Channel deleted"),
            description=tr_lang(lang,
                        f"🌐 **Kanalname:** `#{channel.name}`\n"
                        f"🏷️ **Typ:** `{channel.type.name}`\n"
                        f"🆔 **ID:** `{channel.id}`",
                        f"🌐 **Channel name:** `#{channel.name}`\n"
                        f"🏷️ **Type:** `{channel.type.name}`\n"
                        f"🆔 **ID:** `{channel.id}`")
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "channel_delete", embed, channel=channel)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        guild = after.guild
        lang = await self.config.guild(guild).language()
        entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.channel_update, target_id=after.id)
        moderator = entry.user if entry else None

        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` ➔ `{after.name}`")
        if hasattr(before, "topic") and hasattr(after, "topic") and before.topic != after.topic:
            changes.append(tr_lang(lang, f"Thema: `{before.topic or 'Kein Thema'}` ➔ `{after.topic or 'Kein Thema'}`", f"Topic: `{before.topic or 'No topic'}` ➔ `{after.topic or 'No topic'}`"))
        if hasattr(before, "nsfw") and hasattr(after, "nsfw") and before.nsfw != after.nsfw:
            changes.append(f"NSFW: `{before.nsfw}` ➔ `{after.nsfw}`")

        if not changes:
            return # Skip if nothing significant changed (e.g. overrides or positional shifts)

        embed = discord.Embed(
            color=0xf1c40f,
            title=tr_lang(lang, "⚙️ Kanal aktualisiert/modifiziert", "⚙️ Channel updated/modified"),
            description=tr_lang(lang,
                        f"🌐 **Kanal:** {after.mention if hasattr(after, 'mention') else f'#{after.name}'} ({after.id})\n"
                        f"🛡️ **Modifiziert von:** {moderator.mention if moderator else 'Unbekannt'}\n\n"
                        f"📋 **Änderungen:**\n",
                        f"🌐 **Channel:** {after.mention if hasattr(after, 'mention') else f'#{after.name}'} ({after.id})\n"
                        f"🛡️ **Modified by:** {moderator.mention if moderator else 'Unknown'}\n\n"
                        f"📋 **Changes:**\n") + "\n".join(changes)
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "channel_update", embed, channel=after, actor=moderator)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        guild = thread.guild
        lang = await self.config.guild(guild).language()

        embed = discord.Embed(
            color=0x2ecc71,
            title=tr_lang(lang, "🧵 Thread erstellt", "🧵 Thread created"),
            description=tr_lang(lang,
                        f"🌐 **Thread:** {thread.mention} ({thread.name})\n"
                        f"📁 **Kanal:** {thread.parent.mention if thread.parent else 'Unbekannt'}\n"
                        f"🆔 **ID:** `{thread.id}`",
                        f"🌐 **Thread:** {thread.mention} ({thread.name})\n"
                        f"📁 **Channel:** {thread.parent.mention if thread.parent else 'Unknown'}\n"
                        f"🆔 **ID:** `{thread.id}`")
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "thread_create", embed, channel=thread.parent)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        guild = thread.guild
        lang = await self.config.guild(guild).language()

        embed = discord.Embed(
            color=0xe74c3c,
            title=tr_lang(lang, "🧵 Thread gelöscht", "🧵 Thread deleted"),
            description=tr_lang(lang,
                        f"🌐 **Threadname:** `{thread.name}`\n"
                        f"📁 **Kanal:** {thread.parent.mention if thread.parent else 'Unbekannt'}\n"
                        f"🆔 **ID:** `{thread.id}`",
                        f"🌐 **Thread name:** `{thread.name}`\n"
                        f"📁 **Channel:** {thread.parent.mention if thread.parent else 'Unknown'}\n"
                        f"🆔 **ID:** `{thread.id}`")
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "thread_delete", embed, channel=thread.parent)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        guild = after.guild
        lang = await self.config.guild(guild).language()
        entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.thread_update, target_id=after.id)
        moderator = entry.user if entry else None

        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` ➔ `{after.name}`")
        if before.archived != after.archived:
            changes.append(tr_lang(lang, f"Archiviert: `{before.archived}` ➔ `{after.archived}`", f"Archived: `{before.archived}` ➔ `{after.archived}`"))

        if not changes:
            return

        embed = discord.Embed(
            color=0xf1c40f,
            title=tr_lang(lang, "⚙️ Thread aktualisiert/modifiziert", "⚙️ Thread updated/modified"),
            description=tr_lang(lang,
                        f"🌐 **Thread:** {after.mention} ({after.id})\n"
                        f"📁 **Kanal:** {after.parent.mention if after.parent else 'Unbekannt'}\n"
                        f"🛡️ **Modifiziert von:** {moderator.mention if moderator else 'Unbekannt'}\n\n"
                        f"📋 **Änderungen:**\n",
                        f"🌐 **Thread:** {after.mention} ({after.id})\n"
                        f"📁 **Channel:** {after.parent.mention if after.parent else 'Unknown'}\n"
                        f"🛡️ **Modified by:** {moderator.mention if moderator else 'Unknown'}\n\n"
                        f"📋 **Changes:**\n") + "\n".join(changes)
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "thread_update", embed, channel=after.parent, actor=moderator)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        guild = role.guild
        lang = await self.config.guild(guild).language()

        embed = discord.Embed(
            color=0x2ecc71,
            title=tr_lang(lang, "🎨 Rolle angelegt", "🎨 Role created"),
            description=tr_lang(lang,
                        f"🏷️ **Rolle:** {role.mention} (`{role.name}`)\n"
                        f"🆔 **ID:** `{role.id}`",
                        f"🏷️ **Role:** {role.mention} (`{role.name}`)\n"
                        f"🆔 **ID:** `{role.id}`")
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "role_create", embed, role=role)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        guild = role.guild
        lang = await self.config.guild(guild).language()

        embed = discord.Embed(
            color=0xe74c3c,
            title=tr_lang(lang, "🗑️ Rolle gelöscht", "🗑️ Role deleted"),
            description=tr_lang(lang,
                        f"🏷️ **Rollenname:** `{role.name}`\n"
                        f"🆔 **ID:** `{role.id}`",
                        f"🏷️ **Role name:** `{role.name}`\n"
                        f"🆔 **ID:** `{role.id}`")
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "role_delete", embed, role=role)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild = invite.guild
        if not guild:
            return

        lang = await self.config.guild(guild).language()
        expire_str = format_duration(invite.max_age, lang) if invite.max_age > 0 else tr_lang(lang, "Niemals", "Never")

        embed = discord.Embed(
            color=0x2ecc71,
            title=tr_lang(lang, "🎟️ Server-Einladung erstellt", "🎟️ Server invite created"),
            description=tr_lang(lang,
                        f"👤 **Ersteller:** {invite.inviter.mention if invite.inviter else 'Unbekannt'}\n"
                        f"🌐 **Kanal:** {invite.channel.mention if hasattr(invite.channel, 'mention') else f'#{invite.channel.name}'}\n"
                        f"🔗 **Link:** {invite.url}\n"
                        f"⏱️ **Ablauf:** {expire_str}",
                        f"👤 **Creator:** {invite.inviter.mention if invite.inviter else 'Unknown'}\n"
                        f"🌐 **Channel:** {invite.channel.mention if hasattr(invite.channel, 'mention') else f'#{invite.channel.name}'}\n"
                        f"🔗 **Link:** {invite.url}\n"
                        f"⏱️ **Expires:** {expire_str}")
        )
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="adminprotocol")

        await self._post_embed(guild, "invite_create", embed, channel=invite.channel, actor=invite.inviter)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        lang = await self.config.guild(guild).language()

        # 1. Joined voice channel
        if before.channel is None and after.channel is not None:
            embed = discord.Embed(
                color=0x3498db,  # Blau
                description=tr_lang(lang, f"🔊 {member.mention} **ist dem Sprachkanal** {after.channel.mention} **beigetreten.**", f"🔊 {member.mention} **joined the voice channel** {after.channel.mention}**.**")
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text=f"ID: {member.id}")
            await self._post_embed(guild, "voice_join", embed, channel=after.channel, member=member)

        # 2. Left voice channel
        elif before.channel is not None and after.channel is None:
            # Check if disconnected by a moderator
            entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.member_disconnect, target_id=member.id)
            if entry:
                # Disconnected by other
                embed = discord.Embed(
                    color=0xe74c3c,
                    title=tr_lang(lang, "🔇 Sprachkanal-Verbindung getrennt", "🔇 Voice channel disconnected"),
                    description=tr_lang(lang,
                                f"👤 **Benutzer:** {member.mention} ({member.id})\n"
                                f"🛡️ **Getrennt von:** {entry.user.mention}\n"
                                f"🌐 **Kanal:** {before.channel.name}",
                                f"👤 **User:** {member.mention} ({member.id})\n"
                                f"🛡️ **Disconnected by:** {entry.user.mention}\n"
                                f"🌐 **Channel:** {before.channel.name}")
                )
                embed.set_author(name=str(member), icon_url=member.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                embed.set_footer(text="adminprotocol")
                await self._post_embed(guild, "voice_disconnect", embed, channel=before.channel, member=member, actor=entry.user)
            else:
                # Self leave
                embed = discord.Embed(
                    color=0xe74c3c,
                    description=tr_lang(lang, f"🔊 {member.mention} **hat den Sprachkanal** {before.channel.mention} **verlassen.**", f"🔊 {member.mention} **left the voice channel** {before.channel.mention}**.**")
                )
                embed.set_author(name=str(member), icon_url=member.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                embed.set_footer(text=f"ID: {member.id}")
                await self._post_embed(guild, "voice_leave", embed, channel=before.channel, member=member)

        # 3. Switched voice channels / moved by moderator
        elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
            # Check if moved by moderator
            entry = await self._get_audit_log_entry(guild, discord.AuditLogAction.member_move)
            # In member_move audit logs, target is often None or the user. We check target_id.
            if entry and entry.target and entry.target.id == member.id:
                # Moderator moved
                embed = discord.Embed(
                    color=0xf1c40f,
                    title=tr_lang(lang, "🔀 Benutzer in Sprachkanal verschoben", "🔀 User moved to voice channel"),
                    description=tr_lang(lang,
                                f"👤 **Benutzer:** {member.mention} ({member.id})\n"
                                f"🛡️ **Verschoben von:** {entry.user.mention}\n"
                                f"📥 **Von:** {before.channel.name}\n"
                                f"📤 **Zu:** {after.channel.name}",
                                f"👤 **User:** {member.mention} ({member.id})\n"
                                f"🛡️ **Moved by:** {entry.user.mention}\n"
                                f"📥 **From:** {before.channel.name}\n"
                                f"📤 **To:** {after.channel.name}")
                )
                embed.set_author(name=str(member), icon_url=member.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                embed.set_footer(text="adminprotocol")
                await self._post_embed(guild, "voice_move", embed, channel=after.channel, member=member, actor=entry.user)
            else:
                # Self switch
                embed = discord.Embed(
                    color=0xf1c40f,
                    description=tr_lang(lang,
                                f"🔊 {member.mention} **hat den Sprachkanal gewechselt.**\n"
                                f"📥 **Herkunft:** {before.channel.mention}\n"
                                f"📤 **Ziel:** {after.channel.mention}",
                                f"🔊 {member.mention} **switched voice channel.**\n"
                                f"📥 **From:** {before.channel.mention}\n"
                                f"📤 **To:** {after.channel.mention}")
                )
                embed.set_author(name=str(member), icon_url=member.display_avatar.url)
                embed.timestamp = discord.utils.utcnow()
                embed.set_footer(text=f"ID: {member.id}")
                await self._post_embed(guild, "voice_switch", embed, channel=after.channel, member=member)

        # 4. Voice status changed (mute/deafen)
        status_changes = []
        if before.self_mute != after.self_mute:
            status_changes.append(tr_lang(lang, f"Mute (Selbst): `{'Aktiv' if after.self_mute else 'Inaktiv'}`", f"Mute (Self): `{'Active' if after.self_mute else 'Inactive'}`"))
        if before.self_deaf != after.self_deaf:
            status_changes.append(tr_lang(lang, f"Deafen (Selbst): `{'Aktiv' if after.self_deaf else 'Inaktiv'}`", f"Deafen (Self): `{'Active' if after.self_deaf else 'Inactive'}`"))
        if before.mute != after.mute:
            status_changes.append(tr_lang(lang, f"Mute (Server): `{'Aktiv' if after.mute else 'Inaktiv'}`", f"Mute (Server): `{'Active' if after.mute else 'Inactive'}`"))
        if before.deaf != after.deaf:
            status_changes.append(tr_lang(lang, f"Deafen (Server): `{'Aktiv' if after.deaf else 'Inaktiv'}`", f"Deafen (Server): `{'Active' if after.deaf else 'Inactive'}`"))
        if before.self_stream != after.self_stream:
            status_changes.append(tr_lang(lang, f"Stream: `{'Start' if after.self_stream else 'Stopp'}`", f"Stream: `{'Start' if after.self_stream else 'Stop'}`"))
        if before.self_video != after.self_video:
            status_changes.append(tr_lang(lang, f"Kamera: `{'An' if after.self_video else 'Aus'}`", f"Camera: `{'On' if after.self_video else 'Off'}`"))

        if status_changes:
            current_channel = after.channel or before.channel
            embed = discord.Embed(
                color=0x3498db,
                title=tr_lang(lang, "🎙️ Sprachstatus geändert", "🎙️ Voice status changed"),
                description=tr_lang(lang,
                            f"👤 **Benutzer:** {member.mention} ({member.id})\n"
                            f"🌐 **Sprachkanal:** {current_channel.name if current_channel else 'Unbekannt'}\n\n"
                            f"📋 **Änderungen:**\n",
                            f"👤 **User:** {member.mention} ({member.id})\n"
                            f"🌐 **Voice channel:** {current_channel.name if current_channel else 'Unknown'}\n\n"
                            f"📋 **Changes:**\n") + "\n".join(status_changes)
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="adminprotocol")
            await self._post_embed(guild, "voice_status", embed, channel=current_channel, member=member)

    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context):
        if not ctx.guild or ctx.command is None:
            return
        cog_name = ctx.cog.qualified_name if ctx.cog else None
        if self._is_mod_command(ctx.command.name, cog_name):
            lang = await self.config.guild(ctx.guild).language()
            embed = discord.Embed(
                title=tr_lang(lang, "🛡️ Moderationsbefehl verwendet", "🛡️ Moderation command used"),
                color=0xf1c40f,
                description=tr_lang(lang,
                            f"👤 **Benutzer:** {ctx.author.mention} ({ctx.author.id})\n"
                            f"💬 **Befehl:** `{ctx.message.content}`\n"
                            f"🌐 **Kanal:** {ctx.channel.mention}",
                            f"👤 **User:** {ctx.author.mention} ({ctx.author.id})\n"
                            f"💬 **Command:** `{ctx.message.content}`\n"
                            f"🌐 **Channel:** {ctx.channel.mention}")
            )
            embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="adminprotocol")
            
            await self._post_embed(ctx.guild, "mod_command", embed, channel=ctx.channel, actor=ctx.author)

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command: discord.app_commands.Command | discord.app_commands.ContextMenu):
        if not interaction.guild or command is None:
            return
        cog_name = command.binding.qualified_name if hasattr(command, "binding") and command.binding else None
        
        cmd_repr = f"/{command.name}"
        if interaction.data:
            options = interaction.data.get("options", [])
            if options:
                opts_str = " ".join([f"{o.get('name')}:{o.get('value')}" for o in options])
                cmd_repr += f" {opts_str}"
                
        if self._is_mod_command(command.name, cog_name):
            lang = await self.config.guild(interaction.guild).language()
            embed = discord.Embed(
                title=tr_lang(lang, "🛡️ Moderationsbefehl verwendet", "🛡️ Moderation command used"),
                color=0xf1c40f,
                description=tr_lang(lang,
                            f"👤 **Benutzer:** {interaction.user.mention} ({interaction.user.id})\n"
                            f"💬 **Befehl:** `{cmd_repr}`\n"
                            f"🌐 **Kanal:** {interaction.channel.mention if interaction.channel else 'Unbekannt'}",
                            f"👤 **User:** {interaction.user.mention} ({interaction.user.id})\n"
                            f"💬 **Command:** `{cmd_repr}`\n"
                            f"🌐 **Channel:** {interaction.channel.mention if interaction.channel else 'Unknown'}")
            )
            embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="adminprotocol")
            
            await self._post_embed(interaction.guild, "mod_command", embed, channel=interaction.channel, actor=interaction.user)

    # ------------------------------------------------------------
    # Standalone Dashboard Page
    # ------------------------------------------------------------

    @_dashboard_page(name=None, description="AdminProtocol Dashboard")
    async def dashboard_home(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        source = """
<div style="padding: 12px;">
  <h2>AdminProtocol</h2>
  <p>Dashboard integration is active.</p>
  <p>Use the page <b>adminprotocol</b> for guild-specific settings.</p>
</div>
"""
        return {
            "status": 0,
            "web_content": {
                "source": source,
                "standalone": True,
            },
        }

    @_dashboard_page(
        name="adminprotocol",
        description="Verwalte das AdminProtocol und logge administrative Events.",
        methods=("GET", "POST"),
        context_ids=["user_id", "guild_id"],
        hidden=False,
    )
    async def dashboard_adminprotocol(
        self,
        user_id: Optional[int] = None,
        guild_id: Optional[int] = None,
        method: str = "GET",
        data: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if user_id is None or guild_id is None:
            return {"status": 0, "error_code": 400, "message": "Fehlender Kontext: user_id/guild_id."}
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"status": 1, "message": "Server nicht gefunden."}
        member = guild.get_member(user_id)
        if member is None or not member.guild_permissions.manage_guild:
            if user_id not in self.bot.owner_ids:
                return {"status": 1, "message": "Nicht berechtigt."}

        events = await self.config.guild(guild).events()

        # Generate CSRF token if Form class is provided by dashboard
        Form = kwargs.get("Form")
        csrf_token_html = ""
        if Form is not None:
            try:
                class DummyForm(Form):
                    pass
                csrf_token_html = DummyForm().hidden_tag()
            except Exception as e:
                log.debug(f"Failed to generate CSRF token: {e}")

        # Handle POST configuration save
        if method.upper() == "POST" and data:
            form = dict(data.get("form", {}))
            
            target_event = form.get("save_event")
            if target_event and target_event in EVENTS:
                ev = target_event
                enabled = str(form.get(f"enabled_{ev}", "off")).lower() in ("on", "true", "1", "yes")
                
                ch_raw = form.get(f"channel_{ev}", "")
                channel_id = int(ch_raw) if str(ch_raw).isdigit() else None
                
                ignored_ch_raw = form.get(f"ignored_channels_{ev}", "")
                ignored_channels = [int(x) for x in ignored_ch_raw.split(",") if x.strip().isdigit()]
                
                ignored_ro_raw = form.get(f"ignored_roles_{ev}", "")
                ignored_roles = [int(x) for x in ignored_ro_raw.split(",") if x.strip().isdigit()]
                
                ignored_us_raw = form.get(f"ignored_users_{ev}", "")
                ignored_users = [int(x) for x in ignored_us_raw.split(",") if x.strip().isdigit()]
                
                # Update only this specific event
                event_data = {
                    "enabled": enabled,
                    "channel": channel_id,
                    "ignored_channels": ignored_channels,
                    "ignored_users": ignored_users,
                    "ignored_roles": ignored_roles
                }
                await self.config.guild(guild).events.set_raw(ev, value=event_data)
                
                return {
                    "status": 0,
                    "notifications": [{"message": f"Einstellungen für '{EVENTS[ev][0]}' erfolgreich gespeichert.", "category": "success"}],
                    "redirect_url": kwargs.get("request_url"),
                }

        # Build dropdown options
        text_channels = [c for c in guild.text_channels]
        # Include all channel types (text, voice, stage, threads) for ignore lists
        all_channels = [c for c in guild.channels if isinstance(c, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread))]
        roles = [r for r in guild.roles if not r.is_default()]
        # Get all members in the guild (excluding bots)
        guild_members = [m for m in guild.members if not m.bot]
        guild_members = sorted(guild_members, key=lambda m: m.display_name.lower())

        all_channel_options = "".join([f'<option value="{c.id}">#{html.escape(c.name)} ({c.type.name} - {c.id})</option>' for c in all_channels])
        role_options = "".join([f'<option value="{r.id}">{html.escape(r.name)} ({r.id})</option>' for r in roles])
        user_options = "".join([f'<option value="{m.id}">{html.escape(m.display_name)} ({m.id})</option>' for m in guild_members])

        # Prepare JSON values of channels, roles and users to map IDs to names in JavaScript
        channel_map = {str(c.id): f"#{c.name} ({c.type.name})" for c in all_channels}
        role_map = {str(r.id): r.name for r in roles}
        user_map = {str(m.id): m.display_name for m in guild_members}
        
        channel_map_json = json.dumps(channel_map).replace("</", "<\\/")
        role_map_json = json.dumps(role_map).replace("</", "<\\/")
        user_map_json = json.dumps(user_map).replace("</", "<\\/")
        initial_data_json = json.dumps({
            ev: {
                "ignored_channels": [str(x) for x in events[ev].get("ignored_channels", [])],
                "ignored_roles": [str(x) for x in events[ev].get("ignored_roles", [])],
                "ignored_users": [str(x) for x in events[ev].get("ignored_users", [])]
            }
            for ev in EVENTS
        }).replace("</", "<\\/")

        # Categorize events into groups
        categories = {
            "messages": ["message_edit", "message_delete", "channel_create", "channel_delete", "channel_update", "thread_create", "thread_delete", "thread_update"],
            "members": ["user_join", "user_leave", "nickname_change_other", "nickname_change_self", "role_create", "role_delete", "role_add", "role_remove"],
            "moderation": ["user_ban", "user_unban", "user_kick", "user_timeout", "mod_command"],
            "voice": ["voice_join", "voice_leave", "voice_status", "voice_switch", "voice_move", "voice_disconnect", "invite_create"]
        }

        # Build Accordion items for each event card
        rows = []
        for cat, ev_list in categories.items():
            for ev in ev_list:
                ev_name = EVENTS[ev][0]
                ev_data = events.get(ev, {})
                enabled_checked = "checked" if ev_data.get("enabled", False) else ""
                
                # Channel dropdown options with selected pre-selected channel
                current_ch_id = ev_data.get("channel")
                ch_select_options = [f'<option value="">-- Deaktiviert --</option>']
                for ch in text_channels:
                    selected = "selected" if ch.id == current_ch_id else ""
                    ch_select_options.append(f'<option value="{ch.id}" {selected}>#{html.escape(ch.name)} ({ch.id})</option>')
                ch_dropdown = "".join(ch_select_options)

                rows.append(f"""
<form method="post">
    {csrf_token_html}
    <input type="hidden" name="save_event" value="{ev}">
    <div class="event-card" data-tab="{cat}">
        <div class="event-header" onclick="toggleAccordion(this)">
            <div class="title-wrap">
                <span class="indicator {'active' if ev_data.get('enabled') and ev_data.get('channel') else ''}"></span>
                <h4>{html.escape(ev_name)} <code>({ev})</code></h4>
            </div>
            <span class="chevron">&#9662;</span>
        </div>
        <div class="event-content" style="display: none;">
            <div class="form-grid">
                <div class="form-sec">
                    <label class="switch-label">
                        <input type="checkbox" name="enabled_{ev}" {enabled_checked}>
                        <span class="switch-custom"></span>
                        Aktiviert
                    </label>
                    <div style="margin-top:12px;">
                        <label>Log-Kanal (Dropdown)</label>
                        <select name="channel_{ev}">{ch_dropdown}</select>
                        <small style="color:#a0aec0;font-size:11.5px;display:block;margin-top:2px;">* Keine Auswahl deaktiviert die Funktion unabhängig des Status.</small>
                    </div>
                </div>
                <div class="form-sec">
                    <label>Ignorierte Kanäle (Text & Voice)</label>
                    <select class="ch-ignore-select" onchange="addTag(this, 'channel', '{ev}')">
                        <option value="">-- Kanal hinzufügen --</option>
                        {all_channel_options}
                    </select>
                    <div id="tags_channel_{ev}" class="tags-container"></div>
                    <input type="hidden" id="ignored_channels_{ev}" name="ignored_channels_{ev}" value="">
                </div>
                <div class="form-sec">
                    <label>Ignorierte Rollen</label>
                    <select class="role-ignore-select" onchange="addTag(this, 'role', '{ev}')">
                        <option value="">-- Rolle hinzufügen --</option>
                        {role_options}
                    </select>
                    <div id="tags_role_{ev}" class="tags-container"></div>
                    <input type="hidden" id="ignored_roles_{ev}" name="ignored_roles_{ev}" value="">
                </div>
                <div class="form-sec">
                    <label>Ignorierte Benutzer</label>
                    <select class="user-ignore-select" onchange="addTag(this, 'user', '{ev}')">
                        <option value="">-- Benutzer hinzufügen --</option>
                        {user_options}
                    </select>
                    <div id="tags_user_{ev}" class="tags-container"></div>
                    <input type="hidden" id="ignored_users_{ev}" name="ignored_users_{ev}" value="">
                </div>
            </div>
            <button type="submit" class="btn-primary" style="margin-top: 16px; padding: 10px 24px !important; font-size: 13.5px !important; border-radius: 8px !important; width: auto !important; display: block !important;">Einstellungen speichern</button>
        </div>
    </div>
</form>
""")

        content = "".join(rows)

        source = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600&display=swap');
.ap-dashboard * {{ font-family: 'Outfit', sans-serif; box-sizing: border-box; }}
.ap-dashboard {{ background: #0b0f19 !important; color: #e8eefc !important; padding: 24px !important; border-radius: 16px !important; border: 1px solid rgba(255, 255, 255, 0.08) !important; }}
.ap-dashboard h2 {{ color: #ffffff !important; font-weight: 600 !important; margin-top: 0 !important; margin-bottom: 8px !important; letter-spacing: -0.02em !important; font-size: 26px !important; }}
.ap-dashboard p {{ color: #9aa5b1 !important; font-size: 14.5px !important; line-height: 1.5 !important; margin-top: 0 !important; margin-bottom: 24px !important; }}
.ap-dashboard .card {{ background: #111625 !important; border: 1px solid rgba(255, 255, 255, 0.08) !important; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5) !important; border-radius: 16px !important; padding: 24px !important; color: #e8eefc !important; }}
.ap-dashboard .tab-container {{ display: flex !important; gap: 10px !important; margin-bottom: 20px !important; border-bottom: 1px solid rgba(255, 255, 255, 0.08) !important; padding-bottom: 12px !important; overflow-x: auto !important; }}
.ap-dashboard .tab-btn {{ padding: 10px 22px !important; background: #1c2338 !important; border: 1px solid rgba(255, 255, 255, 0.1) !important; border-radius: 30px !important; color: #cbd5e1 !important; font-weight: 600 !important; cursor: pointer !important; transition: all 0.25s ease !important; font-size: 13.5px !important; white-space: nowrap !important; }}
.ap-dashboard .tab-btn:hover {{ background: #283250 !important; color: #ffffff !important; border-color: rgba(255, 255, 255, 0.2) !important; }}
.ap-dashboard .tab-btn.active {{ background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%) !important; color: #ffffff !important; border-color: transparent !important; box-shadow: 0 4px 14px rgba(99, 102, 241, 0.4) !important; }}
.ap-dashboard .event-card {{ border: 1px solid rgba(255, 255, 255, 0.08) !important; background: #171f35 !important; border-radius: 12px !important; margin-bottom: 14px !important; overflow: hidden !important; transition: all 0.2s ease !important; }}
.ap-dashboard .event-card:hover {{ border-color: rgba(255, 255, 255, 0.18) !important; background: #1e2844 !important; }}
.ap-dashboard .event-header {{ padding: 18px 22px !important; display: flex !important; justify-content: space-between !important; align-items: center !important; cursor: pointer !important; user-select: none !important; }}
.ap-dashboard .event-header h4 {{ margin: 0 !important; font-weight: 600 !important; font-size: 15.5px !important; color: #ffffff !important; display: flex !important; align-items: center !important; gap: 10px !important; }}
.ap-dashboard .event-header h4 code {{ background: #0b0f19 !important; padding: 3px 8px !important; border-radius: 6px !important; font-size: 12px !important; font-family: monospace !important; color: #63b3ed !important; border: 1px solid rgba(255, 255, 255, 0.06) !important; }}
.ap-dashboard .event-header .chevron {{ color: #a0aec0 !important; transition: transform 0.2s ease !important; font-size: 15px !important; }}
.ap-dashboard .event-header .title-wrap {{ display: flex !important; align-items: center !important; gap: 12px !important; }}
.ap-dashboard .event-header .indicator {{ width: 10px; height: 10px; border-radius: 50%; background: #4a5568 !important; display: inline-block !important; transition: background 0.3s ease !important; }}
.ap-dashboard .event-header .indicator.active {{ background: #48bb78 !important; box-shadow: 0 0 10px #48bb78 !important; }}
.ap-dashboard .event-content {{ padding: 22px !important; background: #0c0f1a !important; border-top: 1px solid rgba(255, 255, 255, 0.08) !important; }}
.ap-dashboard .form-grid {{ display: grid !important; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)) !important; gap: 20px !important; }}
.ap-dashboard label {{ font-size: 13px !important; font-weight: 600 !important; color: #cbd5e1 !important; margin-bottom: 8px !important; display: block !important; }}
.ap-dashboard input, .ap-dashboard select {{ width: 100% !important; padding: 11px 15px !important; border-radius: 8px !important; border: 1px solid rgba(255, 255, 255, 0.12) !important; background: #111625 !important; color: #ffffff !important; font-size: 13.5px !important; transition: all 0.2s ease !important; }}
.ap-dashboard input:focus, .ap-dashboard select:focus {{ outline: none !important; border-color: #6366f1 !important; box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.35) !important; background: #171f35 !important; }}
.ap-dashboard .btn-sec {{ padding: 11px 18px !important; border-radius: 8px !important; border: 1px solid rgba(255, 255, 255, 0.18) !important; background: #1c2338 !important; color: #ffffff !important; cursor: pointer !important; font-size: 13.5px !important; transition: all 0.2s ease !important; font-weight: 600 !important; white-space: nowrap !important; }}
.ap-dashboard .btn-sec:hover {{ background: #283250 !important; border-color: rgba(255, 255, 255, 0.3) !important; }}
.ap-dashboard .btn-primary {{ padding: 12px 36px !important; border-radius: 30px !important; border: none !important; background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%) !important; color: #ffffff !important; font-weight: 600 !important; cursor: pointer !important; transition: all 0.25s ease !important; box-shadow: 0 4px 14px rgba(99, 102, 241, 0.4) !important; font-size: 14.5px !important; margin-top: 14px !important; }}
.ap-dashboard .btn-primary:hover {{ transform: translateY(-1px) !important; box-shadow: 0 6px 18px rgba(99, 102, 241, 0.5) !important; background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%) !important; }}
.ap-dashboard .tags-container {{ display: flex !important; flex-wrap: wrap !important; gap: 8px !important; margin-top: 10px !important; min-height: 20px !important; }}
.ap-dashboard .tag {{ display: inline-flex !important; align-items: center !important; gap: 8px !important; padding: 6px 12px !important; background: #6366f1 !important; border: none !important; border-radius: 8px !important; font-size: 13.5px !important; color: #ffffff !important; font-weight: 500 !important; box-shadow: 0 2px 6px rgba(99, 102, 241, 0.2) !important; }}
.ap-dashboard .tag .remove {{ cursor: pointer !important; color: rgba(255, 255, 255, 0.7) !important; font-weight: bold !important; font-size: 14px !important; transition: color 0.15s ease !important; }}
.ap-dashboard .tag .remove:hover {{ color: #ffffff !important; }}
.ap-dashboard .switch-label {{ display: inline-flex !important; align-items: center !important; gap: 10px !important; cursor: pointer !important; font-size: 14px !important; font-weight: 600 !important; color: #ffffff !important; }}
.ap-dashboard .switch-label input {{ display: none !important; }}
.ap-dashboard .switch-custom {{ width: 38px !important; height: 22px !important; background: #3e4859 !important; border-radius: 20px !important; position: relative !important; transition: all 0.3s ease !important; display: inline-block !important; }}
.ap-dashboard .switch-custom::after {{ content: '' !important; width: 16px !important; height: 16px !important; background: #fff !important; border-radius: 50% !important; position: absolute !important; top: 3px !important; left: 3px !important; transition: all 0.25s cubic-bezier(0.5, 1.6, 0.4, 0.7) !important; }}
.ap-dashboard .switch-label input:checked + .switch-custom {{ background: #48bb78 !important; }}
.ap-dashboard .switch-label input:checked + .switch-custom::after {{ left: 19px !important; }}
</style>
<div class="ap-dashboard">
<div class="card">
    <h2>AdminProtocol - Guild Dashboard</h2>
    <p>Konfiguriere hier die automatischen Logs für verschiedene Server-Aktivitäten. Für jede Funktion können spezifische Ignorier-Listen gepflegt werden.</p>
    
    <div class="tab-container">
        <button type="button" class="tab-btn" onclick="switchTab('messages', this)">Nachrichten & Kanäle</button>
        <button type="button" class="tab-btn" onclick="switchTab('members', this)">Mitglieder & Rollen</button>
        <button type="button" class="tab-btn" onclick="switchTab('moderation', this)">Moderation</button>
        <button type="button" class="tab-btn" onclick="switchTab('voice', this)">Sprachkanäle & Einladungen</button>
    </div>

    <div id="events_list">
        {content}
    </div>
</div>
</div>

<script>
const channelNames = {channel_map_json};
const roleNames = {role_map_json};
const userNames = {user_map_json};
const initData = {initial_data_json};

// Switch tabs
function switchTab(tabName, btnElement) {{
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    if (btnElement) {{
        btnElement.classList.add('active');
    }} else {{
        const defaultBtn = document.querySelector(`.tab-btn[onclick*="'${{tabName}}'"]`);
        if (defaultBtn) defaultBtn.classList.add('active');
    }}
    document.querySelectorAll('.event-card').forEach(card => {{
        const parentForm = card.closest('form');
        if (parentForm) {{
            if (card.dataset.tab === tabName) {{
                parentForm.style.display = 'block';
            }} else {{
                parentForm.style.display = 'none';
            }}
        }}
    }});
}}

// Accordion toggle
function toggleAccordion(header) {{
    const card = header.closest('.event-card');
    const content = card.querySelector('.event-content');
    const chevron = header.querySelector('.chevron');
    if (content.style.display === 'none') {{
        content.style.display = 'block';
        chevron.style.transform = 'rotate(180deg)';
    }} else {{
        content.style.display = 'none';
        chevron.style.transform = 'rotate(0deg)';
    }}
}}

// Tag system logic
const tagData = {{}};

function initTags() {{
    Object.keys(initData).forEach(ev => {{
        tagData[ev] = {{
            channel: [...(initData[ev]?.ignored_channels || [])],
            role: [...(initData[ev]?.ignored_roles || [])],
            user: [...(initData[ev]?.ignored_users || [])]
        }};
        renderTags(ev, 'channel');
        renderTags(ev, 'role');
        renderTags(ev, 'user');
    }});
}}

function renderTags(ev, type) {{
    const container = document.getElementById(`tags_${{type}}_${{ev}}`);
    const hiddenInput = document.getElementById(`ignored_${{type}}s_${{ev}}`);
    if (!container || !hiddenInput) return;

    container.innerHTML = "";
    const list = tagData[ev][type];
    hiddenInput.value = list.join(",");

    list.forEach(id => {{
        let name = id;
        if (type === 'channel') name = channelNames[id] || `Kanal #${{id}}`;
        if (type === 'role') name = roleNames[id] || `Rolle #${{id}}`;
        if (type === 'user') name = userNames[id] || `Benutzer #${{id}}`;

        const tag = document.createElement("span");
        tag.className = "tag";
        tag.innerHTML = `${{name}} <span class="remove" onclick="removeTag('${{ev}}', '${{type}}', '${{id}}')">&times;</span>`;
        container.appendChild(tag);
    }});
}}

function addTag(select, type, ev) {{
    const id = select.value;
    if (!id) return;
    select.value = ""; // reset dropdown

    if (!tagData[ev]) {{
        tagData[ev] = {{ channel: [], role: [], user: [] }};
    }}
    if (!tagData[ev][type]) {{
        tagData[ev][type] = [];
    }}
    if (!tagData[ev][type].includes(id)) {{
        tagData[ev][type].push(id);
        renderTags(ev, type);
    }}
}}

function removeTag(ev, type, id) {{
    if (tagData[ev] && tagData[ev][type]) {{
        tagData[ev][type] = tagData[ev][type].filter(item => item !== id);
        renderTags(ev, type);
    }}
}}

// Start initialization immediately
switchTab('messages', null);
initTags();
</script>
"""
        return {"status": 0, "web_content": {"source": source, "standalone": True}}
