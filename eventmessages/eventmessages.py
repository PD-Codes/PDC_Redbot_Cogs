import discord
from discord import app_commands
from redbot.core import Config, commands
from typing import Any, Dict, Optional

from .pdc_dashboard import (
    dashboard_widget, dashboard_panel, WidgetData,
    PanelSchema, Field, SubmitResult,
    register_dashboard, unregister_dashboard,
    L, tr, tr_lang,
)

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

EVENTS = [
    "join",
    "leave",
    "kick",
    "ban",
    "unban",
    "timeout",
    "timeout_end"
]

class EventMessages(commands.Cog):
    """Sendet automatisch Eventnachrichten (Join, Leave, Ban, Timeout etc.)."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=981273598123)

        default_templates = {
            "join": "🎉 **{display_name}** ist dem Server beigetreten!",
            "leave": "👋 **{display_name}** (`{username}`) hat den Server verlassen.",
            "kick": "👢 **{display_name}** (`{username}`) wurde gekickt. Moderator: {moderator} | Grund: {reason}",
            "ban": "⛔ **{display_name}** (`{username}`) wurde gebannt. Moderator: {moderator} | Grund: {reason}",
            "unban": "🔓 **{display_name}** (`{username}`) wurde entbannt. Moderator: {moderator}",
            "timeout": "⛔ **{display_name}** (`{username}`) erhielt Timeout. Moderator: {moderator} | Grund: {reason} | Bis: {duration}",
            "timeout_end": "⏱️ Timeout für **{display_name}** (`{username}`) ist abgelaufen.",
        }
        default_guild = {
            "language": "en-US",
            "events": {
                ev: {
                    "enabled": False,
                    "channel": None
                }
                for ev in EVENTS
            },
            "templates": default_templates,
        }

        self.config.register_guild(**default_guild)
        self._dashboard_attached = False
        self._selected_event = {}  # (guild_id, user_id) -> event_id

    def _get_dashboard_cog(self) -> Optional[commands.Cog]:
        return self.bot.get_cog("pdc_webdashboard") or self.bot.get_cog("WebDashboard") or self.bot.get_cog("Dashboard")

    def _attach_to_dashboard(self, dashboard_cog: commands.Cog) -> bool:
        try:
            dashboard_cog.rpc.third_parties_handler.add_third_party(self, overwrite=True)  # type: ignore[attr-defined]
            return True
        except Exception:
            try:
                dashboard_cog.rpc.third_parties_handler.add_third_party(self)  # type: ignore[attr-defined]
                return True
            except Exception:
                return False

    async def cog_load(self) -> None:
        register_dashboard(self)
        dashboard_cog = self._get_dashboard_cog()
        if dashboard_cog is not None:
            self._dashboard_attached = self._attach_to_dashboard(dashboard_cog)

    async def cog_unload(self) -> None:
        unregister_dashboard(self)
        dashboard_cog = self._get_dashboard_cog()
        if dashboard_cog is not None:
            try:
                dashboard_cog.rpc.third_parties_handler.remove_third_party(self)
            except Exception:
                pass
        self._dashboard_attached = False

    @commands.Cog.listener()
    async def on_cog_add(self, cog: commands.Cog) -> None:
        if self._dashboard_attached:
            return
        if cog.qualified_name not in {"Dashboard", "WebDashboard", "pdc_webdashboard"}:
            return
        self._dashboard_attached = self._attach_to_dashboard(cog)

    @dashboard_widget("eventmessages_enabled", L("Aktive Events", "Active Events"), size="sm", permission="guild_member")
    async def eventmessages_enabled_widget(self, ctx):
        try:
            guild = getattr(ctx, "guild", None)
            events = await self.config.guild(guild).events()
            count = sum(1 for ev in EVENTS if events.get(ev, {}).get("enabled"))
            return WidgetData.kpi(value=count, label="Aktive Events")
        except Exception:
            return WidgetData.kpi(value="–", label="Aktive Events")

    # --- Guild panel: enable per event, channel & message ------------ #
    @dashboard_panel(
        "events", L("Event-Nachrichten", "Event Messages"), mount="guild_settings", permission="guild_admin"
    )
    async def eventmessages_panel(self, ctx):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        labels = {
            "join": "Join", "leave": "Leave", "kick": "Kick", "ban": "Ban",
            "unban": "Unban", "timeout": "Timeout", "timeout_end": "Timeout end",
        }
        event_choices = [{"value": "0", "label": "-- Select event --"}]
        for ev in EVENTS:
            event_choices.append({"value": ev, "label": labels.get(ev, ev)})

        selection = self._selected_event.get((guild_id, user_id), "0")

        # Ensure selection is still valid
        choice_vals = {v["value"] for v in event_choices}
        if selection not in choice_vals:
            selection = "0"
            self._selected_event[(guild_id, user_id)] = "0"

        fields = [
            Field.select("event_id", "Event", event_choices, value=selection, reload_on_change=True)
        ]

        if selection != "0":
            events = await self.config.guild(ctx.guild).events()
            templates = await self.config.guild(ctx.guild).templates()
            if not isinstance(events, dict):
                events = {}
            if not isinstance(templates, dict):
                templates = {}

            cfg = events.get(selection, {}) if isinstance(events.get(selection), dict) else {}
            name = labels.get(selection, selection)

            variables = [
                {"token": "{display_name}", "desc": "Display name"},
                {"token": "{username}", "desc": "Username"},
                {"token": "{moderator}", "desc": "Moderator"},
                {"token": "{reason}", "desc": "Reason"},
                {"token": "{duration}", "desc": "Duration"},
            ]
            channel_options = [{"value": "", "label": "— no channel —"}] + [
                {"value": str(c.id), "label": "#" + c.name} for c in ctx.guild.text_channels
            ]

            fields.extend([
                Field.switch("enabled", "Enabled", value=bool(cfg.get("enabled", False))),
                Field.select("channel", "Log channel", channel_options, value=str(cfg.get("channel") or "")),
                Field.textarea("tmpl", "Message template", value=templates.get(selection, ""), max_length=1000, variables=variables)
            ])

        return PanelSchema(description=tr(ctx, "Pro Event: aktivieren, Kanal wählen und Nachricht festlegen.", "Per event: enable, choose a channel and set the message."), fields=fields)

    @eventmessages_panel.on_submit
    async def _save_eventmessages(self, ctx, data):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        event_id = str(data.get("event_id", "0")).strip()
        prev_sel = self._selected_event.get((guild_id, user_id), "0")

        if event_id != prev_sel:
            # User switched dropdown selection
            self._selected_event[(guild_id, user_id)] = event_id
            return SubmitResult.ok()

        if event_id == "0":
            return SubmitResult.fail("Bitte wähle ein Ereignis aus.")

        labels = {
            "join": "Beitritt", "leave": "Verlassen", "kick": "Kick", "ban": "Ban",
            "unban": "Entbann", "timeout": "Timeout", "timeout_end": "Timeout-Ende",
        }

        events = await self.config.guild(ctx.guild).events()
        templates = await self.config.guild(ctx.guild).templates()
        if not isinstance(events, dict):
            events = {}
        if not isinstance(templates, dict):
            templates = {}

        cfg = events.get(event_id, {}) if isinstance(events.get(event_id), dict) else {}

        cfg["enabled"] = bool(data.get("enabled", False))
        ch = data.get("channel")
        cfg["channel"] = int(ch) if ch else None
        events[event_id] = cfg

        if "tmpl" in data:
            templates[event_id] = str(data["tmpl"])[:1000]

        await self.config.guild(ctx.guild).events.set(events)
        await self.config.guild(ctx.guild).templates.set(templates)
        return SubmitResult.ok(f"Einstellungen für '{labels.get(event_id, event_id)}' gespeichert.")

    @dashboard_panel("language", L("Sprache", "Language"), mount="guild_settings", permission="guild_admin", order=99)
    async def language_panel(self, ctx):
        return PanelSchema(
            description=tr(ctx, "Sprache der Bot-Ausgaben für diesen Server.", "Output language for this server."),
            fields=[
                Field.select("language", L("Sprache", "Language"),
                    [{"value": "de-DE", "label": "Deutsch"}, {"value": "en-US", "label": "English"}],
                    value=str(await self.config.guild(ctx.guild).language()), reload_on_change=True),
            ],
        )

    @language_panel.on_submit
    async def _language_save(self, ctx, data):
        if "language" in data:
            await self.config.guild(ctx.guild).language.set("en-US" if data.get("language") == "en-US" else "de-DE")
        return SubmitResult.ok(tr(ctx, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------
    # Autocomplete
    # ------------------------------------------------------------

    async def event_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for event names."""
        suggestions = [
            app_commands.Choice(name=ev, value=ev)
            for ev in EVENTS
            if current.lower() in ev.lower()
        ]
        return suggestions[:25]


    # ------------------------------------------------------------
    # Slash: Enabled
    # ------------------------------------------------------------

    @app_commands.command(
        name="em-enabled",
        description="Enable or disable an event.",
        extras={"i18n_desc": {
            "de-DE": "Aktiviert oder deaktiviert ein Event.",
            "en-US": "Enable or disable an event.",
        }},
    )
    @app_commands.describe(
        event="Which event?",
        value="true/false"
    )
    @app_commands.autocomplete(event=event_autocomplete)
    async def em_enabled(
        self,
        interaction: discord.Interaction,
        event: str,                # <-- now required
        value: bool                # <-- now required
    ):
        """Enable or disable event messages for this server."""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        lang = await self.config.guild(guild).language() if guild else "en-US"

        # Validation
        if event not in EVENTS:
            await interaction.followup.send(
                tr_lang(lang, f"Ungültiges Event. Verwendet werden kann: `{', '.join(EVENTS)}`", f"Invalid event. Allowed: `{', '.join(EVENTS)}`"),
                ephemeral=True
            )
            return

        # Set
        await self.config.guild(guild).events.set_raw(
            event, "enabled", value=value
        )

        await interaction.followup.send(
            tr_lang(lang, f"Event **{event}** wurde auf **{value}** gesetzt.", f"Event **{event}** was set to **{value}**."),
            ephemeral=True
        )


    # ------------------------------------------------------------
    # Slash: Channel setzen
    # ------------------------------------------------------------

    @app_commands.command(
        name="em-channel",
        description="Sets the channel for an event.",
        extras={"i18n_desc": {
            "de-DE": "Legt den Kanal für ein Event fest.",
            "en-US": "Sets the channel for an event.",
        }},
    )
    @app_commands.describe(
        event="Which event?",
        channel="Channel for notifications"
    )
    @app_commands.autocomplete(event=event_autocomplete)
    async def em_channel(
        self,
        interaction: discord.Interaction,
        event: str,                    # <-- now required
        channel: discord.TextChannel   # <-- required
    ):
        """Set the channel where event messages are posted."""
        await interaction.response.defer(ephemeral=True)
        lang = await self.config.guild(interaction.guild).language() if interaction.guild else "en-US"

        if event not in EVENTS:
            await interaction.followup.send(
                tr_lang(lang, f"Ungültiges Event. Erlaubt: `{', '.join(EVENTS)}`", f"Invalid event. Allowed: `{', '.join(EVENTS)}`"),
                ephemeral=True
            )
            return

        await self.config.guild(interaction.guild).events.set_raw(
            event, "channel", value=channel.id
        )

        await interaction.followup.send(
            tr_lang(lang, f"Channel für **{event}** gesetzt auf {channel.mention}.", f"Channel for **{event}** set to {channel.mention}."),
            ephemeral=True
        )


    # ------------------------------------------------------------
    # Slash: Status anzeigen
    # ------------------------------------------------------------

    @app_commands.command(
        name="em-status",
        description="Shows the status of all events.",
        extras={"i18n_desc": {
            "de-DE": "Zeigt den Status aller Events an.",
            "en-US": "Shows the status of all events.",
        }},
    )
    async def em_status(self, interaction: discord.Interaction):
        """Show the current event-message configuration."""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        lang = await self.config.guild(guild).language() if guild else "en-US"
        data = await self.config.guild(guild).events()

        msg = tr_lang(lang, "**Eventstatus:**\n\n", "**Event status:**\n\n")
        for ev in EVENTS:
            ch_id = data[ev]["channel"]
            ch = f"<#{ch_id}>" if ch_id else "—"
            msg += tr_lang(
                lang,
                f"**Event:** `{ev}`\n"
                f"→ Enabled: **{data[ev]['enabled']}**\n"
                f"→ Channel: {ch}\n\n",
                f"**Event:** `{ev}`\n"
                f"→ Enabled: **{data[ev]['enabled']}**\n"
                f"→ Channel: {ch}\n\n",
            )

        await interaction.followup.send(msg, ephemeral=True)

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    async def _post(self, guild: discord.Guild, event: str, message: str):
        data = await self.config.guild(guild).events()
        if not data[event]["enabled"]:
            return

        ch_id = data[event]["channel"]
        if not ch_id:
            return

        channel = guild.get_channel(ch_id)
        if channel:
            await channel.send(message)

    async def _render_template(self, guild: discord.Guild, event: str, **kwargs: str) -> str:
        templates = await self.config.guild(guild).templates()
        template = templates.get(event, "{display_name}")
        safe = {
            "display_name": kwargs.get("display_name", ""),
            "username": kwargs.get("username", ""),
            "moderator": kwargs.get("moderator", "Unbekannt"),
            "reason": kwargs.get("reason", "Kein Grund angegeben"),
            "duration": kwargs.get("duration", "-"),
        }
        try:
            return template.format(**safe)
        except Exception:
            return template

    # ------------------------------------------------------------
    # Events
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        msg = await self._render_template(
            member.guild, "join", display_name=member.display_name, username=str(member)
        )
        await self._post(member.guild, "join", msg)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild

        # Check audit log: was the user kicked?
        entry = None
        async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
            # Discord audit logs are slightly delayed, so we check 5 entries
            if log.target.id == member.id:
                entry = log
                break

        if entry:
            # → This was a kick
            moderator = entry.user.mention
            reason = entry.reason or "Kein Grund angegeben"

            await self._post(
                guild,
                "kick",
                await self._render_template(
                    guild,
                    "kick",
                    display_name=member.display_name,
                    username=str(member),
                    moderator=moderator,
                    reason=reason,
                ),
            )
            return

        # If not a kick → normal leave
        await self._post(
            guild,
            "leave",
            await self._render_template(
                guild, "leave", display_name=member.display_name, username=str(member)
            ),
        )


    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        entry = None
        async for log in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
            entry = log

        reason = entry.reason or "Kein Grund angegeben" if entry else "Unbekannt"
        moderator = entry.user.mention if entry else "Unbekannt"

        await self._post(
            guild,
            "ban",
            await self._render_template(
                guild,
                "ban",
                display_name=getattr(user, "display_name", str(user)),
                username=str(user),
                moderator=moderator,
                reason=reason,
            ),
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        entry = None
        async for log in guild.audit_logs(limit=1, action=discord.AuditLogAction.unban):
            entry = log

        moderator = entry.user.mention if entry else "Unbekannt"

        await self._post(
            guild,
            "unban",
            await self._render_template(
                guild,
                "unban",
                display_name=getattr(user, "display_name", str(user)),
                username=str(user),
                moderator=moderator,
            ),
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Timeout set?
        if before.timed_out_until != after.timed_out_until:
            # Timeout END
            if before.timed_out_until and after.timed_out_until is None:
                await self._post(
                    after.guild,
                    "timeout_end",
                    await self._render_template(
                        after.guild,
                        "timeout_end",
                        display_name=after.display_name,
                        username=str(after),
                    ),
                )
                return

            # Timeout START
            if after.timed_out_until:
                # Fetch audit log
                entry = None
                async for log in after.guild.audit_logs(limit=1, action=discord.AuditLogAction.member_update):
                    entry = log

                moderator = entry.user.mention if entry else "Unbekannt"
                reason = entry.reason or "Kein Grund angegeben" if entry else "Unbekannt"

                duration = discord.utils.format_dt(after.timed_out_until, style="R")

                await self._post(
                    after.guild,
                    "timeout",
                    await self._render_template(
                        after.guild,
                        "timeout",
                        display_name=after.display_name,
                        username=str(after),
                        moderator=moderator,
                        reason=reason,
                        duration=duration,
                    ),
                )

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog: commands.Cog) -> None:
        if self._dashboard_attached:
            return
        try:
            dashboard_cog.rpc.third_parties_handler.add_third_party(self, overwrite=True)  # type: ignore[attr-defined]
            self._dashboard_attached = True
        except TypeError:
            try:
                dashboard_cog.rpc.third_parties_handler.add_third_party(self)  # type: ignore[attr-defined]
                self._dashboard_attached = True
            except Exception:
                self._dashboard_attached = False
        except Exception:
            self._dashboard_attached = False

    @_dashboard_page(name=None, description="EventMessages Dashboard")
    async def dashboard_home(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        source = """
<div style="padding: 12px;">
  <h2>EventMessages</h2>
  <p>Dashboard integration is active.</p>
  <p>Use the page <b>eventmessages</b> for guild-specific settings.</p>
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
        name="eventmessages",
        description="Configure event messages, templates and variables.",
        methods=("GET", "POST"),
        context_ids=["user_id", "guild_id"],
        hidden=False,
    )
    async def dashboard_eventmessages(
        self,
        user_id: Optional[int] = None,
        guild_id: Optional[int] = None,
        method: str = "GET",
        data: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        _ = kwargs
        if user_id is None or guild_id is None:
            return {"status": 0, "error_code": 400, "message": "Missing context user_id/guild_id."}
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"status": 1, "message": "Guild not found."}
        member = guild.get_member(user_id)
        if member is None or not member.guild_permissions.manage_guild:
            if user_id not in self.bot.owner_ids:
                return {"status": 1, "message": "Not allowed."}

        events = await self.config.guild(guild).events()
        templates = await self.config.guild(guild).templates()

        if method.upper() == "POST" and data:
            form = dict(data.get("form", {}))
            for ev in EVENTS:
                templates[ev] = str(form.get(f"tpl_{ev}", templates.get(ev, "")))
                events[ev]["enabled"] = str(form.get(f"enabled_{ev}", "off")).lower() in ("on", "true", "1", "yes")
                ch_raw = form.get(f"channel_{ev}", "")
                events[ev]["channel"] = int(ch_raw) if str(ch_raw).isdigit() else None
            await self.config.guild(guild).events.set(events)
            await self.config.guild(guild).templates.set(templates)
            return {
                "status": 0,
                "notifications": [{"message": "EventMessages dashboard settings saved.", "category": "success"}],
                "redirect_url": kwargs.get("request_url"),
            }

        channel_options = "".join(
            [f"<option value=''>-- none --</option>"]
            + [f"<option value='{c.id}'>#{c.name} ({c.id})</option>" for c in guild.text_channels]
        )
        rows = []
        for ev in EVENTS:
            enabled_checked = "checked" if events[ev]["enabled"] else ""
            channel_id = events[ev]["channel"] or ""
            tpl_val = str(templates.get(ev, "")).replace("<", "&lt;").replace(">", "&gt;")
            rows.append(
                f"""
<div class='card'>
<h3>{ev}</h3>
<label><input type='checkbox' name='enabled_{ev}' {enabled_checked}> enabled</label><br>
<label>channel id</label><br><input name='channel_{ev}' value='{channel_id}'><br>
<label>template</label><br><textarea name='tpl_{ev}' rows='3'>{tpl_val}</textarea>
</div>
"""
            )
        content = "".join(rows)
        source = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
* {{ font-family: 'Inter', sans-serif; box-sizing: border-box; }}
.pdc-dashboard .card {{ background: rgba(18, 23, 33, 0.6); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3); border-radius: 12px; padding: 24px; color: #e8eefc; transition: all 0.3s ease; }}
.pdc-dashboard .card:hover {{ box-shadow: 0 12px 40px 0 rgba(0, 0, 0, 0.4); border-color: rgba(255, 255, 255, 0.12); }}
.pdc-dashboard h2, .pdc-dashboard h3 {{ color: #ffffff; font-weight: 600; margin-top: 0; margin-bottom: 16px; letter-spacing: -0.02em; }}
.pdc-dashboard p {{ color: #a0aec0; font-size: 14px; line-height: 1.5; margin-top: 0; margin-bottom: 16px; }}
.pdc-dashboard code {{ background: rgba(255, 255, 255, 0.1); padding: 4px 8px; border-radius: 6px; font-size: 13px; color: #63b3ed; font-family: monospace; }}
.pdc-dashboard label {{ font-size: 13.5px; font-weight: 500; color: #cbd5e0; margin-bottom: 8px; display: inline-block; }}
.pdc-dashboard input, .pdc-dashboard textarea, .pdc-dashboard select {{ width: 100%; padding: 12px 16px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, 0.1); background: rgba(0, 0, 0, 0.25); color: #fff; font-size: 14px; transition: all 0.2s ease; margin-bottom: 16px; }}
.pdc-dashboard input:focus, .pdc-dashboard textarea:focus, .pdc-dashboard select:focus {{ outline: none; border-color: #4299e1; box-shadow: 0 0 0 3px rgba(66, 153, 225, 0.25); background: rgba(0, 0, 0, 0.35); }}
.pdc-dashboard button {{ padding: 12px 24px; border-radius: 8px; border: none; background: linear-gradient(135deg, #4299e1 0%, #3182ce 100%); color: #fff; font-weight: 600; cursor: pointer; transition: all 0.2s ease; box-shadow: 0 4px 6px rgba(50, 50, 93, 0.11), 0 1px 3px rgba(0, 0, 0, 0.08); font-size: 14px; }}
.pdc-dashboard button:hover {{ transform: translateY(-1px); box-shadow: 0 7px 14px rgba(50, 50, 93, 0.15), 0 3px 6px rgba(0, 0, 0, 0.1); background: linear-gradient(135deg, #3182ce 0%, #2b6cb0 100%); }}
.pdc-dashboard button:active {{ transform: translateY(1px); }}
.pdc-dashboard .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-bottom: 16px; }}
</style>
<div class='pdc-dashboard'>
<div class='card'>
<h2>EventMessages Dashboard</h2>
<p><b>Variables:</b> <code>{{display_name}}</code> <code>{{username}}</code> <code>{{moderator}}</code> <code>{{reason}}</code> <code>{{duration}}</code></p>
<p>Use channel ID values from your server channels.</p>
<form method='post'><div class='grid'>{content}</div><br><button type='submit'>Save all</button></form>
</div>
</div>
"""
        return {"status": 0, "web_content": {"source": source, "standalone": True}}

    

