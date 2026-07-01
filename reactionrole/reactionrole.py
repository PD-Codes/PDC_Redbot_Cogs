import discord
from redbot.core import commands, Config
import uuid
from typing import Any, Dict, Optional
import json
import html

from .pdc_dashboard import (
    dashboard_widget, dashboard_panel, dashboard_list, WidgetData,
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

class ReactionRole(commands.Cog):
    """Simple ReactionRole Cog"""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=983472983472, force_registration=True)
        self.config.register_guild(
            language="en-US",  # per-guild language of this cog (de-DE | en-US)
            reactionroles={},
            panels={},
            templates={
                "set_success": "✅ ReactionRole erstellt | ID: `{id}` | Emoji: {emoji} | Rolle: {role}",
                "remove_success": "🗑️ ReactionRole `{id}` entfernt.",
            },
        )
        self._dashboard_attached = False

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

    @dashboard_widget("reactionrole_count", L("ReactionRoles", "ReactionRoles"), size="sm", permission="guild_member")
    async def reactionrole_count_widget(self, ctx):
        try:
            guild = getattr(ctx, "guild", None)
            data = await self.config.guild(guild).reactionroles()
            return WidgetData.kpi(value=len(data), label="ReactionRoles")
        except Exception:
            return WidgetData.kpi(value="–", label="ReactionRoles")

    # --- Guild panel: customize success messages ------------------------- #
    @dashboard_panel(
        "templates", L("ReactionRole-Nachrichten", "ReactionRole Messages"), mount="guild_settings", permission="guild_admin"
    )
    async def reactionrole_templates_panel(self, ctx):
        t = await self.config.guild(ctx.guild).templates()
        variables = [
            {"token": "{id}", "desc": "ID"},
            {"token": "{emoji}", "desc": "Emoji"},
            {"token": "{role}", "desc": "Role"},
        ]
        return PanelSchema(
            description=tr(ctx, "Antworten beim Erstellen/Entfernen von ReactionRoles.", "Responses when creating/removing ReactionRoles."),
            fields=[
                Field.textarea("set_success", "Created", value=t.get("set_success", ""),
                               max_length=500, variables=variables),
                Field.textarea("remove_success", "Removed", value=t.get("remove_success", ""),
                               max_length=500, variables=[{"token": "{id}", "desc": "ID"}]),
            ],
        )

    @reactionrole_templates_panel.on_submit
    async def _save_reactionrole_templates(self, ctx, data):
        cur = await self.config.guild(ctx.guild).templates()
        for k in ("set_success", "remove_success"):
            if k in data:
                cur[k] = str(data[k])[:500]
        await self.config.guild(ctx.guild).templates.set(cur)
        return SubmitResult.ok(tr(ctx, "Gespeichert.", "Saved."))

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

    # --- Guild panel: create ReactionRole directly ----------------------- #
    @dashboard_panel(
        "create_rr", L("ReactionRole anlegen", "Create ReactionRole"), mount="guild_settings", permission="guild_admin"
    )
    async def reactionrole_create_panel(self, ctx):
        ch_opts = [{"value": "", "label": "— Select channel —"}] + [
            {"value": str(c.id), "label": "#" + c.name} for c in ctx.guild.text_channels
        ]
        role_opts = [{"value": "", "label": "— Select role —"}] + [
            {"value": str(r.id), "label": r.name}
            for r in ctx.guild.roles
            if not r.is_default() and not r.managed
        ]
        return PanelSchema(
            description=tr(ctx, "Kanal, Nachrichten-ID, Emoji und Rolle angeben. Speichern legt die "
                        "ReactionRole an und setzt die Reaktion an der Nachricht.",
                        "Provide channel, message ID, emoji and role. Saving creates the "
                        "ReactionRole and adds the reaction to the message."),
            submit_label=tr(ctx, "ReactionRole anlegen", "Create ReactionRole"),
            fields=[
                Field.select("channel", "Channel", ch_opts, value=""),
                Field.text("message_id", "Message ID", value="", placeholder="123456789012345678"),
                Field.text("emoji", "Emoji", value="", placeholder="👍 oder <:name:id>"),
                Field.select("role", "Role to assign", role_opts, value=""),
            ],
        )

    @reactionrole_create_panel.on_submit
    async def _create_reactionrole(self, ctx, data):
        guild = ctx.guild
        ch_id = data.get("channel")
        role_id = data.get("role")
        emoji = str(data.get("emoji", "")).strip()
        msg_raw = str(data.get("message_id", "")).strip()
        if not ch_id or not role_id or not emoji or not msg_raw:
            return SubmitResult.fail(tr(ctx, "Bitte Kanal, Nachrichten-ID, Emoji und Rolle ausfüllen.", "Please fill in channel, message ID, emoji and role."))
        channel = guild.get_channel(int(ch_id))
        if channel is None:
            return SubmitResult.fail(tr(ctx, "Kanal nicht gefunden.", "Channel not found."), errors={"channel": tr(ctx, "Ungültig", "Invalid")})
        role = guild.get_role(int(role_id))
        if role is None:
            return SubmitResult.fail(tr(ctx, "Rolle nicht gefunden.", "Role not found."), errors={"role": tr(ctx, "Ungültig", "Invalid")})
        try:
            message_id = int(msg_raw)
        except ValueError:
            return SubmitResult.fail(tr(ctx, "Nachrichten-ID muss eine Zahl sein.", "Message ID must be a number."),
                                     errors={"message_id": tr(ctx, "Zahl erwartet", "Number expected")})
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return SubmitResult.fail(tr(ctx, "Nachricht nicht gefunden.", "Message not found."), errors={"message_id": tr(ctx, "Nicht gefunden", "Not found")})
        except discord.Forbidden:
            return SubmitResult.fail(tr(ctx, "Keine Rechte, die Nachricht zu lesen.", "No permission to read that message."))
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            return SubmitResult.fail(tr(ctx, "Ungültiges Emoji oder keine Rechte.", "Invalid emoji or missing permissions."), errors={"emoji": tr(ctx, "Ungültig", "Invalid")})
        rr_id = str(uuid.uuid4())[:8]
        async with self.config.guild(guild).reactionroles() as d:
            d[rr_id] = {
                "message_id": message_id,
                "channel_id": channel.id,
                "emoji": str(emoji),
                "role_id": role.id,
            }
        return SubmitResult.ok(tr(ctx, f"ReactionRole angelegt (ID {rr_id}).", f"ReactionRole created (ID {rr_id})."))

    # --- Guild list: view/delete existing ReactionRoles ------------------ #
    @dashboard_list(
        "rr_list", L("Bestehende ReactionRoles", "Existing ReactionRoles"), mount="guild_settings", permission="guild_admin",
        columns=[
            {"key": "channel", "label": "Channel"},
            {"key": "message", "label": "Message ID"},
            {"key": "emoji", "label": "Emoji"},
            {"key": "role", "label": "Role"},
        ],
    )
    async def reactionrole_list(self, ctx):
        data = await self.config.guild(ctx.guild).reactionroles()
        rows = []
        for rid, e in (data or {}).items():
            if not isinstance(e, dict):
                continue
            ch = ctx.guild.get_channel(e.get("channel_id"))
            role = ctx.guild.get_role(e.get("role_id"))
            rows.append({
                "id": rid,
                "cells": {
                    "channel": ("#" + ch.name) if ch else str(e.get("channel_id")),
                    "message": str(e.get("message_id")),
                    "emoji": str(e.get("emoji", "")),
                    "role": role.name if role else str(e.get("role_id")),
                },
            })
        return rows

    @reactionrole_list.on_delete
    async def _delete_reactionrole(self, ctx, item_id):
        entry = None
        async with self.config.guild(ctx.guild).reactionroles() as d:
            entry = d.pop(item_id, None)
        if entry is None:
            return SubmitResult.fail(tr(ctx, "Eintrag nicht gefunden.", "Entry not found."))
        # Clean up the reaction on the message (best effort).
        try:
            ch = ctx.guild.get_channel(entry.get("channel_id"))
            if ch:
                msg = await ch.fetch_message(entry.get("message_id"))
                await msg.clear_reaction(entry.get("emoji"))
        except Exception:
            pass
        return SubmitResult.ok(tr(ctx, "ReactionRole entfernt.", "ReactionRole removed."))

    @reactionrole_list.edit_form
    async def _edit_reactionrole_form(self, ctx, item_id):
        """Edit: only the assigned role is sensibly changeable.

        Channel/message/emoji define the reaction itself - changing those
        would amount to a new entry (use the create panel for that).
        """
        data = await self.config.guild(ctx.guild).reactionroles()
        entry = (data or {}).get(item_id) or {}
        role_options = [
            {"value": str(r.id), "label": r.name}
            for r in ctx.guild.roles if r.name != "@everyone"
        ]
        return PanelSchema(
            description=tr(ctx, "Zugewiesene Rolle ändern. Kanal/Nachricht/Emoji bleiben unverändert.", "Change the assigned role. Channel/message/emoji stay unchanged."),
            fields=[
                Field.select("role_id", "Assigned role", role_options,
                             value=str(entry.get("role_id") or "")),
            ],
        )

    @reactionrole_list.on_edit
    async def _edit_reactionrole(self, ctx, item_id, data):
        new_role = data.get("role_id")
        if not new_role or not str(new_role).isdigit():
            return SubmitResult.fail(tr(ctx, "Bitte eine gültige Rolle wählen.", "Please choose a valid role."))
        async with self.config.guild(ctx.guild).reactionroles() as d:
            entry = d.get(item_id)
            if not isinstance(entry, dict):
                return SubmitResult.fail(tr(ctx, "Eintrag nicht gefunden.", "Entry not found."))
            entry["role_id"] = int(new_role)
            d[item_id] = entry
        return SubmitResult.ok(tr(ctx, "Rolle aktualisiert.", "Role updated."))

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

    # -------------------------
    # SET
    # -------------------------
    @commands.hybrid_command(
        name="reactionrole-set",
        description="Add a reaction role: map an emoji on a message to a role.",
        extras={"i18n_desc": {
            "de-DE": "Reaktionsrolle hinzufügen: Emoji einer Nachricht einer Rolle zuordnen.",
            "en-US": "Add a reaction role: map an emoji on a message to a role.",
        }},
    )
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole_set(
        self,
        ctx: commands.Context,
        message_id: str,
        emoji: str,
        role: discord.Role
    ):
        """Add a reaction role to a message."""
        lang = await self.config.guild(ctx.guild).language()
        try:
            message_id = int(message_id)
        except ValueError:
            return await ctx.send(tr_lang(lang, "❌ Message-ID muss eine Zahl sein.", "❌ Message ID must be a number."))

        guild = ctx.guild
        channel = ctx.channel

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return await ctx.send(tr_lang(lang, "❌ Message nicht gefunden.", "❌ Message not found."))
        except discord.Forbidden:
            return await ctx.send(tr_lang(lang, "❌ Keine Rechte, um die Nachricht zu lesen.", "❌ No permission to read that message."))

        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            return await ctx.send(tr_lang(lang, "❌ Ungültiges Emoji oder keine Rechte.", "❌ Invalid emoji or missing permissions."))

        rr_id = str(uuid.uuid4())[:8]

        async with self.config.guild(guild).reactionroles() as data:
            data[rr_id] = {
                "message_id": message_id,
                "channel_id": channel.id,
                "emoji": str(emoji),
                "role_id": role.id
            }
        templates = await self.config.guild(guild).templates()
        await ctx.send(
            templates["set_success"].format(
                id=rr_id,
                emoji=emoji,
                role=role.mention,
                message_id=message_id,
                channel=channel.mention,
            )
        )

    # -------------------------
    # REMOVE
    # -------------------------
    @commands.hybrid_command(
        name="reactionrole-remove",
        description="Remove a reaction role by its ID.",
        extras={"i18n_desc": {
            "de-DE": "Entfernt eine Reaktionsrolle anhand ihrer ID.",
            "en-US": "Remove a reaction role by its ID.",
        }},
    )
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole_remove(self, ctx: commands.Context, rr_id: str):
        """Remove a reaction role from a message."""
        lang = await self.config.guild(ctx.guild).language()
        async with self.config.guild(ctx.guild).reactionroles() as data:
            if rr_id not in data:
                return await ctx.send(tr_lang(lang, "❌ Diese ReactionRole-ID existiert nicht.", "❌ That ReactionRole ID does not exist."))

            del data[rr_id]

        templates = await self.config.guild(ctx.guild).templates()
        await ctx.send(templates["remove_success"].format(id=rr_id))

    # -------------------------
    # GET
    # -------------------------
    @commands.hybrid_command(
        name="reactionrole-get",
        description="List all configured reaction roles in this server.",
        extras={"i18n_desc": {
            "de-DE": "Listet alle konfigurierten Reaktionsrollen dieses Servers auf.",
            "en-US": "List all configured reaction roles in this server.",
        }},
    )
    @commands.guild_only()
    async def reactionrole_get(self, ctx: commands.Context):
        """List the configured reaction roles."""
        lang = await self.config.guild(ctx.guild).language()
        data = await self.config.guild(ctx.guild).reactionroles()

        if not data:
            return await ctx.send(tr_lang(lang, "ℹ️ Keine ReactionRoles vorhanden.", "ℹ️ No ReactionRoles configured."))

        role_label = tr_lang(lang, "Rolle", "Role")
        deleted_label = tr_lang(lang, "❌ gelöscht", "❌ deleted")
        lines = []
        for rr_id, entry in data.items():
            role = ctx.guild.get_role(entry["role_id"])
            lines.append(
                f"**ID:** `{rr_id}` | "
                f"Emoji: {entry['emoji']} | "
                f"{role_label}: {role.name if role else deleted_label} | "
                f"MessageID: `{entry['message_id']}`"
            )

        await ctx.send("\n".join(lines))

    # -------------------------
    # EVENTS
    # -------------------------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)

        if member is None or member.bot:
            return

        data = await self.config.guild(guild).reactionroles()

        for entry in data.values():
            if (
                payload.message_id == entry["message_id"]
                and str(payload.emoji) == entry["emoji"]
            ):
                role = guild.get_role(entry["role_id"])
                if role:
                    await member.add_roles(role, reason="ReactionRole")
                break

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)

        if member is None or member.bot:
            return

        data = await self.config.guild(guild).reactionroles()

        for entry in data.values():
            if (
                payload.message_id == entry["message_id"]
                and str(payload.emoji) == entry["emoji"]
            ):
                role = guild.get_role(entry["role_id"])
                if role:
                    await member.remove_roles(role, reason="ReactionRole")
                break

    @commands.hybrid_command(
        name="reactionrole-sync",
        description="Re-apply reaction roles to users who already reacted.",
        extras={"i18n_desc": {
            "de-DE": "Wendet Reaktionsrollen erneut auf Nutzer an, die bereits reagiert haben.",
            "en-US": "Re-apply reaction roles to users who already reacted.",
        }},
    )
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole_sync(self, ctx: commands.Context):
        """Re-sync reaction roles with their messages."""
        guild = ctx.guild
        lang = await self.config.guild(guild).language()
        data = await self.config.guild(guild).reactionroles()

        if not data:
            return await ctx.send(tr_lang(lang, "ℹ️ Keine ReactionRoles zum Synchronisieren.", "ℹ️ No ReactionRoles to synchronize."))

        added = 0

        for rr_id, entry in data.items():
            channel = guild.get_channel(entry["channel_id"])
            if not channel:
                continue

            try:
                message = await channel.fetch_message(entry["message_id"])
            except (discord.NotFound, discord.Forbidden):
                continue

            role = guild.get_role(entry["role_id"])
            if not role:
                continue

            reaction = discord.utils.get(
                message.reactions,
                emoji=entry["emoji"]
            )

            if not reaction:
                continue

            async for user in reaction.users():
                if user.bot:
                    continue

                member = guild.get_member(user.id)
                if not member:
                    continue

                if role not in member.roles:
                    await member.add_roles(
                        role,
                        reason="ReactionRole manual sync"
                    )
                    added += 1

        await ctx.send(
            tr_lang(
                lang,
                f"🔄 Synchronisation abgeschlossen\n➕ Rollen neu gesetzt: **{added}**",
                f"🔄 Synchronization complete\n➕ Roles re-applied: **{added}**",
            )
        )

    async def _create_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        content: str,
        mappings: list[dict[str, Any]],
        panel_id: Optional[str] = None,
    ) -> str:
        message = await channel.send(content)
        valid_mappings = []
        for m in mappings:
            emoji = str(m["emoji"]).strip()
            role_id = int(m["role_id"])
            if not emoji or not guild.get_role(role_id):
                continue
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                continue
            rr_id = str(uuid.uuid4())[:8]
            valid_mappings.append(
                {
                    "rr_id": rr_id,
                    "emoji": emoji,
                    "role_id": role_id,
                }
            )

        if not valid_mappings:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            raise ValueError("No valid emoji/role mappings created.")

        if panel_id is None:
            panel_id = str(uuid.uuid4())[:8]

        async with self.config.guild(guild).reactionroles() as data:
            for mapping in valid_mappings:
                data[mapping["rr_id"]] = {
                    "message_id": message.id,
                    "channel_id": channel.id,
                    "emoji": mapping["emoji"],
                    "role_id": mapping["role_id"],
                    "panel_id": panel_id,
                }
        async with self.config.guild(guild).panels() as panels:
            panels[panel_id] = {
                "channel_id": channel.id,
                "message_id": message.id,
                "content": content,
            }
        return panel_id

    async def _delete_panel(self, guild: discord.Guild, panel_id: str) -> None:
        panels = await self.config.guild(guild).panels()
        panel = panels.get(panel_id)
        if panel:
            channel = guild.get_channel(int(panel.get("channel_id", 0)))
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(int(panel.get("message_id", 0)))
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        async with self.config.guild(guild).reactionroles() as data:
            to_del = [k for k, v in data.items() if v.get("panel_id") == panel_id]
            for key in to_del:
                del data[key]
        async with self.config.guild(guild).panels() as panels_mut:
            panels_mut.pop(panel_id, None)

    @_dashboard_page(name=None, description="ReactionRole Dashboard")
    async def dashboard_home(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        source = """
<div style="padding: 12px;">
  <h2>ReactionRole</h2>
  <p>Dashboard integration is active.</p>
  <p>Use the page <b>reactionrole</b> for guild-specific settings.</p>
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
        name="reactionrole",
        description="Configure reaction roles and text templates.",
        methods=("GET", "POST"),
        context_ids=["user_id", "guild_id"],
        hidden=False,
    )
    async def dashboard_reactionrole(
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
        if member is None or not (member.guild_permissions.manage_roles or member.guild_permissions.manage_guild):
            if user_id not in self.bot.owner_ids:
                return {"status": 1, "message": "Not allowed."}

        rr = await self.config.guild(guild).reactionroles()
        panels = await self.config.guild(guild).panels()
        templates = await self.config.guild(guild).templates()

        if method.upper() == "POST" and data:
            form = dict(data.get("form", {}))
            templates["set_success"] = str(form.get("set_success", templates["set_success"]))
            templates["remove_success"] = str(form.get("remove_success", templates["remove_success"]))
            await self.config.guild(guild).templates.set(templates)

            action = str(form.get("action", "")).strip().lower()
            if action in {"create", "update"}:
                target_panel_id = str(form.get("panel_id", "")).strip()
                channel_id_raw = str(form.get("channel_id", "")).strip()
                content = str(form.get("panel_content", "")).strip()
                channel = guild.get_channel(int(channel_id_raw)) if channel_id_raw.isdigit() else None
                if not isinstance(channel, discord.TextChannel):
                    return {
                        "status": 0,
                        "notifications": [{"message": "Please select a valid text channel.", "category": "warning"}],
                        "redirect_url": kwargs.get("request_url"),
                    }
                if not content:
                    return {
                        "status": 0,
                        "notifications": [{"message": "Panel markdown text is required.", "category": "warning"}],
                        "redirect_url": kwargs.get("request_url"),
                    }

                mappings: list[dict[str, Any]] = []
                for key, value in form.items():
                    if not key.startswith("map_emoji_"):
                        continue
                    idx = key.removeprefix("map_emoji_")
                    emoji = str(value).strip()
                    role_raw = str(form.get(f"map_role_{idx}", "")).strip()
                    if not emoji and not role_raw:
                        continue
                    if not role_raw.isdigit():
                        continue
                    mappings.append({"emoji": emoji, "role_id": int(role_raw)})

                if not mappings:
                    return {
                        "status": 0,
                        "notifications": [{"message": "Add at least one emoji -> role mapping.", "category": "warning"}],
                        "redirect_url": kwargs.get("request_url"),
                    }

                try:
                    if action == "update":
                        if not target_panel_id or target_panel_id not in panels:
                            raise ValueError("Select a valid existing panel to update.")
                        await self._delete_panel(guild, target_panel_id)
                        await self._create_panel(guild, channel, content, mappings, panel_id=target_panel_id)
                        msg = "ReactionRole panel updated."
                    else:
                        await self._create_panel(guild, channel, content, mappings)
                        msg = "ReactionRole panel created."
                except ValueError as exc:
                    return {
                        "status": 0,
                        "notifications": [{"message": str(exc), "category": "warning"}],
                        "redirect_url": kwargs.get("request_url"),
                    }
                return {
                    "status": 0,
                    "notifications": [{"message": msg, "category": "success"}],
                    "redirect_url": kwargs.get("request_url"),
                }

            if action == "delete":
                target_panel_id = str(form.get("panel_id", "")).strip()
                if not target_panel_id or target_panel_id not in panels:
                    return {
                        "status": 0,
                        "notifications": [{"message": "Select a valid panel to remove.", "category": "warning"}],
                        "redirect_url": kwargs.get("request_url"),
                    }
                await self._delete_panel(guild, target_panel_id)
                return {
                    "status": 0,
                    "notifications": [{"message": "ReactionRole panel removed.", "category": "success"}],
                    "redirect_url": kwargs.get("request_url"),
                }

            return {
                "status": 0,
                "notifications": [{"message": "ReactionRole dashboard settings saved.", "category": "success"}],
                "redirect_url": kwargs.get("request_url"),
            }

        # Backfill legacy entries into pseudo-panels if needed.
        panel_map: dict[str, dict[str, Any]] = {}
        for panel_id, p in panels.items():
            panel_map[panel_id] = {
                "panel_id": panel_id,
                "channel_id": p.get("channel_id"),
                "message_id": p.get("message_id"),
                "content": p.get("content", ""),
                "mappings": [],
            }
        for rr_id, entry in rr.items():
            p_id = str(entry.get("panel_id") or f"legacy-{entry['message_id']}")
            if p_id not in panel_map:
                panel_map[p_id] = {
                    "panel_id": p_id,
                    "channel_id": entry.get("channel_id"),
                    "message_id": entry.get("message_id"),
                    "content": "",
                    "mappings": [],
                }
            panel_map[p_id]["mappings"].append(
                {"rr_id": rr_id, "emoji": entry.get("emoji", ""), "role_id": entry.get("role_id", 0)}
            )

        panel_rows = []
        panel_options = ["<option value=''>-- select --</option>"]
        panel_data_for_js = []
        for p in panel_map.values():
            channel_obj = guild.get_channel(int(p["channel_id"])) if str(p.get("channel_id", "")).isdigit() else guild.get_channel(p["channel_id"]) if isinstance(p.get("channel_id"), int) else None
            mappings_text = ", ".join(
                [
                    f"{m['emoji']} -> {guild.get_role(int(m['role_id'])).mention if guild.get_role(int(m['role_id'])) else 'deleted-role'}"
                    for m in p["mappings"]
                ]
            ) or "-"
            panel_rows.append(
                f"<tr><td>{html.escape(str(p['panel_id']))}</td><td>{channel_obj.mention if channel_obj else 'deleted'}</td><td>{p['message_id']}</td><td>{html.escape(mappings_text)}</td></tr>"
            )
            ch_label = (
                f"#{channel_obj.name}"
                if isinstance(channel_obj, discord.TextChannel)
                else "deleted channel"
            )
            panel_options.append(
                f"<option value='{html.escape(str(p['panel_id']))}'>"
                f"{html.escape(ch_label)} — msg {html.escape(str(p.get('message_id', '')))}</option>"
            )
            panel_data_for_js.append(
                {
                    "panel_id": str(p["panel_id"]),
                    "channel_id": str(p["channel_id"]),
                    "content": p.get("content", ""),
                    "mappings": [
                        {"emoji": str(m["emoji"]), "role_id": str(m["role_id"])} for m in p["mappings"]
                    ],
                }
            )
        table = "".join(panel_rows) if panel_rows else "<tr><td colspan='4'><em>No entries</em></td></tr>"

        role_options = ["<option value=''>-- select role --</option>"] + [
            f"<option value='{r.id}'>{html.escape(r.name)}</option>" for r in guild.roles if not r.is_default()
        ]
        channel_options = ["<option value=''>-- select channel --</option>"] + [
            f"<option value='{c.id}'>{html.escape('#' + c.name)}</option>" for c in guild.text_channels
        ]
        panel_json = html.escape(json.dumps(panel_data_for_js))

        source = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
.pdc-dashboard * {{ font-family: 'Inter', sans-serif; box-sizing: border-box; }}
.pdc-dashboard .card {{ background: rgba(18, 23, 33, 0.6); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3); border-radius: 12px; padding: 24px; color: #e8eefc; transition: all 0.3s ease; }}
.pdc-dashboard .card:hover {{ box-shadow: 0 12px 40px 0 rgba(0, 0, 0, 0.4); border-color: rgba(255, 255, 255, 0.12); }}
.pdc-dashboard h2, .pdc-dashboard h3 {{ color: #ffffff; font-weight: 600; margin-top: 0; margin-bottom: 16px; letter-spacing: -0.02em; }}
.pdc-dashboard p {{ color: #a0aec0; font-size: 14px; line-height: 1.5; margin-top: 0; margin-bottom: 16px; }}
.pdc-dashboard code {{ background: rgba(255, 255, 255, 0.1); padding: 4px 8px; border-radius: 6px; font-size: 13px; color: #63b3ed; font-family: monospace; }}
.pdc-dashboard label {{ font-size: 13.5px; font-weight: 500; color: #cbd5e0; margin-bottom: 8px; display: inline-block; }}
.pdc-dashboard input, .pdc-dashboard textarea, .pdc-dashboard select {{ width: 100%; padding: 12px 16px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, 0.1); background: rgba(0, 0, 0, 0.25); color: #fff; font-size: 14px; transition: all 0.2s ease; margin-bottom: 16px; }}
.pdc-dashboard input:focus, .pdc-dashboard textarea:focus, .pdc-dashboard select:focus {{ outline: none; border-color: #4299e1; box-shadow: 0 0 0 3px rgba(66, 153, 225, 0.25); background: rgba(0, 0, 0, 0.35); }}
.pdc-dashboard button {{ padding: 12px 24px; border-radius: 8px; border: none; background: linear-gradient(135deg, #4299e1 0%, #3182ce 100%); color: #fff; font-weight: 600; cursor: pointer; transition: all 0.2s ease; box-shadow: 0 4px 6px rgba(50, 50, 93, 0.11), 0 1px 3px rgba(0, 0, 0, 0.08); font-size: 14px; margin-right: 8px; }}
.pdc-dashboard button:hover {{ transform: translateY(-1px); box-shadow: 0 7px 14px rgba(50, 50, 93, 0.15), 0 3px 6px rgba(0, 0, 0, 0.1); background: linear-gradient(135deg, #3182ce 0%, #2b6cb0 100%); }}
.pdc-dashboard button:active {{ transform: translateY(1px); }}
.pdc-dashboard table {{ width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 12px; margin-bottom: 24px; border-radius: 8px; overflow: hidden; border: 1px solid rgba(255,255,255,0.06); }}
.pdc-dashboard td, .pdc-dashboard th {{ padding: 14px 16px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.06); background: rgba(0,0,0,0.15); font-size: 13.5px; }}
.pdc-dashboard th {{ background: rgba(0,0,0,0.25); font-weight: 600; color: #a0aec0; text-transform: uppercase; font-size: 12px; letter-spacing: 0.05em; }}
.pdc-dashboard .row {{ display: grid; grid-template-columns: 1fr 1fr auto; gap: 12px; margin-bottom: 12px; align-items: center; }}
.pdc-dashboard .row input, .pdc-dashboard .row select {{ margin-bottom: 0; }}
.pdc-dashboard .row button {{ margin-bottom: 0; padding: 12px; background: rgba(255, 50, 50, 0.2); border: 1px solid rgba(255, 50, 50, 0.3); color: #ff6b6b; }}
.pdc-dashboard .row button:hover {{ background: rgba(255, 50, 50, 0.3); transform: none; box-shadow: none; }}
</style>
<div class="pdc-dashboard">
<div class="card">
<h2>ReactionRole Dashboard</h2>
<p><b>Variables:</b> <code>{'{id}'}</code> <code>{'{emoji}'}</code> <code>{'{role}'}</code> <code>{'{message_id}'}</code> <code>{'{channel}'}</code></p>
<form method="post" style="margin-bottom:14px;">
<label>Set success template</label><br><input name="set_success" value="{templates['set_success'].replace('"', '&quot;')}"><br><br>
<label>Remove success template</label><br><input name="remove_success" value="{templates['remove_success'].replace('"', '&quot;')}"><br><br>
<button type="submit">Save Template Texts</button>
</form>
<h3>Create / Edit Panel</h3>
<form method="post">
<label>Existing Panel (for edit/delete)</label><br>
<select id="panel_id" name="panel_id">{"".join(panel_options)}</select><br><br>
<label>Channel</label><br>
<select id="channel_id" name="channel_id">{"".join(channel_options)}</select><br><br>
<label>Message (Discord Markdown supported)</label><br>
<textarea id="panel_content" name="panel_content" rows="6" placeholder="**Welcome**&#10;React to get your role."></textarea><br><br>
<label>ReactionRole mappings (emoji + role, multiple with +)</label><br>
<div id="mapping_rows">
  <div class="row">
    <input type="text" name="map_emoji_1" placeholder="emoji, e.g. ✅ or <:name:id>">
    <select name="map_role_1">{"".join(role_options)}</select>
    <button type="button" onclick="removeRow(this)">-</button>
  </div>
</div>
<button type="button" onclick="addRow()">+ Add Mapping</button><br><br>
<button type="submit" name="action" value="create">Create New Panel</button>
<button type="submit" name="action" value="update">Update Selected Panel</button>
<button type="submit" name="action" value="delete" onclick="return confirm('Delete selected panel and message?')">Delete Selected Panel</button>
</form>
<h3>Current ReactionRoles</h3>
<table><thead><tr><th>Panel ID</th><th>Channel</th><th>Message</th><th>Mappings</th></tr></thead><tbody>{table}</tbody></table>
</div>
</div>
<script>
const panelData = JSON.parse("{panel_json}");
let mapIndex = 1;
function addRow(emoji="", roleId="") {{
  mapIndex += 1;
  const wrapper = document.getElementById("mapping_rows");
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = `
    <input type="text" name="map_emoji_${{mapIndex}}" placeholder="emoji, e.g. ✅ or <:name:id>" value="${{emoji}}">
    <select name="map_role_${{mapIndex}}">{"".join(role_options)}</select>
    <button type="button" onclick="removeRow(this)">-</button>
  `;
  wrapper.appendChild(row);
  if (roleId) {{
    row.querySelector(`select[name="map_role_${{mapIndex}}"]`).value = roleId;
  }}
}}
function removeRow(btn) {{
  const rows = document.querySelectorAll("#mapping_rows .row");
  if (rows.length <= 1) return;
  btn.closest(".row").remove();
}}
function resetRows() {{
  const wrapper = document.getElementById("mapping_rows");
  wrapper.innerHTML = `
  <div class="row">
    <input type="text" name="map_emoji_1" placeholder="emoji, e.g. ✅ or <:name:id>">
    <select name="map_role_1">{"".join(role_options)}</select>
    <button type="button" onclick="removeRow(this)">-</button>
  </div>`;
  mapIndex = 1;
}}
document.getElementById("panel_id").addEventListener("change", (e) => {{
  const selected = panelData.find(p => p.panel_id === e.target.value);
  if (!selected) return;
  document.getElementById("channel_id").value = selected.channel_id || "";
  document.getElementById("panel_content").value = selected.content || "";
  resetRows();
  if (selected.mappings && selected.mappings.length > 0) {{
    const first = selected.mappings[0];
    document.querySelector('input[name="map_emoji_1"]').value = first.emoji || "";
    document.querySelector('select[name="map_role_1"]').value = first.role_id || "";
    for (let i = 1; i < selected.mappings.length; i++) {{
      addRow(selected.mappings[i].emoji || "", selected.mappings[i].role_id || "");
    }}
  }}
}});
</script>
"""
        return {"status": 0, "web_content": {"source": source, "standalone": True}}
