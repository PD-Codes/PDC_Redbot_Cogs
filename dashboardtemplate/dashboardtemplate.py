"""
DashboardTemplate — reference/template cog for the PDC web dashboard integration.

This cog does nothing useful; it is an annotated template you use to show/copy
how to wire an existing cog into the web dashboard ("migrate" it). It
demonstrates:

  1) The drop-in import (no hard dependency, coexists with AAA3A).
  2) Conditional registration in cog_load / cog_unload.
  3) A widget (tile on the board).
  4) A guild panel with all practically usable field types + saving.
  5) A global panel (bot owner only), e.g. for API keys.

Migration in brief (from AAA3A's @dashboard_page):
  - AAA3A: one method returns raw HTML/Jinja per cog -> its own page.
  - PDC:   you return declarative schemas (PanelSchema/Field) -> rendered into a
           shared, themeable UI. One Field per setting, saved via
           @<panel>.on_submit. No HTML, no XSS surface.
Both can exist at the same time in the same cog (see README.md).
"""
from __future__ import annotations

from typing import Any

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

# ---- 1) Drop-in import -------------------------------------------------------
# `pdc_dashboard.py` lives in the same cog folder. When pdc_webdashboard is not
# installed these are no-ops -> the cog still loads normally.
from .pdc_dashboard import (
    dashboard_widget,
    dashboard_panel,
    dashboard_list,
    dashboard_page,
    WidgetData,
    PanelSchema,
    PageSchema,
    Component,
    Control,
    Field,
    SubmitResult,
    L,
    tr,
    tr_lang,
    register_dashboard,
    unregister_dashboard,
)

# ---- Localization helpers (three of them, for three different audiences) -----
#  • L("de", "en")          -> a NAME/description on a @dashboard_* decorator.
#                              The gateway resolves it to the WEB UI language
#                              (the DE/EN toggle), so tab titles follow the site.
#  • tr(ctx, "de", "en")    -> text returned from a handler (PanelSchema
#                              description, SubmitResult message, widget text).
#                              Also follows the WEB UI language via ctx.locale.
#  • tr_lang(lang, "de", "en") -> a cog's DISCORD output (command replies,
#                              embeds, DMs). Uses a PER-GUILD `language` setting
#                              (see the "language" field below), NOT the web UI.
# Field LABELS stay English (one source of truth); only NAMES/descriptions and
# Discord OUTPUT are localized.


class DashboardTemplate(commands.Cog):
    """Template: shows widget + guild panel + global panel."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x7E5701E1, force_registration=True)

        # Per-guild settings (edited in the guild panel).
        self.config.register_guild(
            language="en-US",   # per-guild language of this cog (de-DE | en-US)
            enabled=False,
            greeting="Willkommen, {member}!",
            max_warns=3,
            mode="soft",
            log_channel=None,   # stores a channel ID (or None)
            staff_role=None,    # stores a role ID (or None)
            items={},           # example collection: {id: {"name", "note"}} – for the list
        )
        # Global settings (edited in the global panel, owner-only).
        self.config.register_global(
            api_key="",
            region="eu",
        )

    # ---- 2) Lifecycle: register conditionally -------------------------------
    async def cog_load(self) -> None:
        # IMPORTANT: register_dashboard as the first relevant line. Does nothing
        # if the dashboard is not loaded; otherwise integrates this cog.
        register_dashboard(self)
        # ... your own load logic here if needed ...

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        # ... your own cleanup here if needed ...

    # ---- 3) Widget: tile on the central board -------------------------------
    # Appears on the server detail page under the "Overview" tab.
    # size: sm | md | lg ; refresh: auto-refresh in seconds (optional).
    # permission: authenticated | guild_member | guild_mod | guild_admin |
    #             guild_owner | bot_owner
    @dashboard_widget("status", L("Vorlage-Status", "Template status"), size="sm", refresh=60, permission="guild_member")
    async def status_widget(self, ctx):
        try:
            enabled = await self.config.guild(ctx.guild).enabled()
            # Statt KPI gehen auch: WidgetData.list(...), .status(...), .chart(...), .markdown(...)
            return WidgetData.status(
                state="ok" if enabled else "warn",
                label="Aktiv" if enabled else "Inaktiv",
                detail="Beispiel-Widget",
            )
        except Exception:
            return WidgetData.status(state="error", label="Fehler")

    # ---- 4) Guild panel: all useful field types -----------------------------
    # mount="guild_settings" -> appears on the server detail page under
    # „Einstellungen" (collapsible). permission="guild_admin" recommended.
    # order=10 -> tab order within the module (smaller = further left).
    @dashboard_panel("settings", L("Vorlage-Einstellungen", "Template settings"), mount="guild_settings", permission="guild_admin", order=10)
    async def settings_panel(self, ctx):
        cfg = await self.config.guild(ctx.guild).all()

        # Channel/role selection: the frontend has no dedicated channel/role
        # picker (yet) -> provide it as a SELECT with options.
        channel_options = [{"value": "", "label": "— no channel —"}] + [
            {"value": str(c.id), "label": "#" + c.name} for c in ctx.guild.text_channels
        ]
        role_options = [{"value": "", "label": "— no role —"}] + [
            {"value": str(r.id), "label": r.name}
            for r in ctx.guild.roles
            if not r.is_default()
        ]

        return PanelSchema(
            description=tr(ctx, "Beispiel-Panel mit allen praktisch nutzbaren Feldtypen.",
                           "Example panel with all practically usable field types."),
            # submit_label omitted on purpose: the button then uses the dashboard's
            # own localized "Save" label and follows the website language.
            fields=[
                # Language of this module (per guild) – DE/EN toggle.
                # reload_on_change=True: on change it is saved immediately AND the panel
                # is reloaded (handy when other fields depend on the selection).
                Field.select(
                    "language", "Language (this module)",
                    [{"value": "de-DE", "label": "Deutsch"}, {"value": "en-US", "label": "English"}],
                    value=cfg["language"],
                    reload_on_change=True,
                ),
                # Switch (bool)
                Field.switch("enabled", "Module enabled", value=bool(cfg["enabled"])),
                # Multi-line text with variable buttons (insert tokens at the cursor)
                Field.textarea(
                    "greeting", "Greeting", value=cfg["greeting"], max_length=500,
                    variables=[
                        {"token": "{member}", "desc": "Member"},
                        {"token": "{server}", "desc": "Server"},
                    ],
                ),
                # Number with bounds
                Field.number("max_warns", "Max. warnings", value=cfg["max_warns"], min=0, max=10),
                # Selection (fixed options)
                Field.select(
                    "mode", "Mode",
                    [{"value": "soft", "label": "Soft"}, {"value": "hard", "label": "Hard"}],
                    value=cfg["mode"],
                ),
                # Channel selection (as a select with channel options)
                Field.select("log_channel", "Log channel", channel_options, value=str(cfg["log_channel"] or "")),
                # Role selection (as a select with role options)
                Field.select("staff_role", "Staff role", role_options, value=str(cfg["staff_role"] or "")),
                # Simple text field
                # Field.text("note", "Note", value=""),
            ],
        )

    # Save handler. `data` is a flat dict {field_key: value}.
    @settings_panel.on_submit
    async def save_settings(self, ctx, data):
        g = self.config.guild(ctx.guild)
        if "language" in data:
            await g.language.set("en-US" if data["language"] == "en-US" else "de-DE")
        if "enabled" in data:
            await g.enabled.set(bool(data["enabled"]))
        if "greeting" in data:
            await g.greeting.set(str(data["greeting"])[:500])
        if "max_warns" in data:
            try:
                await g.max_warns.set(max(0, min(10, int(data["max_warns"]))))
            except (TypeError, ValueError):
                return SubmitResult.fail(
                    tr(ctx, "Max. Verwarnungen muss eine Zahl sein.",
                       "Max. warnings must be a number."),
                    errors={"max_warns": tr(ctx, "Ungültige Zahl", "Invalid number")},
                )
        if "mode" in data:
            await g.mode.set("hard" if data["mode"] == "hard" else "soft")
        # Channel/role IDs: empty field -> None, otherwise int.
        if "log_channel" in data:
            v = data["log_channel"]
            await g.log_channel.set(int(v) if v else None)
        if "staff_role" in data:
            v = data["staff_role"]
            await g.staff_role.set(int(v) if v else None)
        # SubmitResult messages show in the WEB UI -> tr(ctx, ...).
        return SubmitResult.ok(tr(ctx, "Einstellungen gespeichert.", "Settings saved."))

    # ---- 5) Global panel: bot owner only (e.g. API keys) --------------------
    # scope="global" + mount="bot_settings" -> appears on /settings under
    # „Modul-Einstellungen (global)". permission="bot_owner".
    @dashboard_panel("api", L("Vorlage API & Global", "Template API & Global"), scope="global", mount="bot_settings", permission="bot_owner")
    async def global_panel(self, ctx):
        # ctx.guild is None here (global context) -> do NOT access ctx.guild.
        return PanelSchema(
            description=tr(ctx, "Globale Einstellungen dieses Moduls (Owner-only).",
                           "Global settings of this module (owner-only)."),
            fields=[
                Field.text("api_key", "API key", value=await self.config.api_key()),
                Field.select(
                    "region", "Region",
                    [{"value": "eu", "label": "EU"}, {"value": "us", "label": "US"}],
                    value=await self.config.region(),
                ),
            ],
        )

    @global_panel.on_submit
    async def save_global(self, ctx, data):
        if "api_key" in data:
            await self.config.api_key.set(str(data["api_key"]).strip())
        if "region" in data:
            await self.config.region.set("us" if data["region"] == "us" else "eu")
        return SubmitResult.ok(tr(ctx, "Global gespeichert.", "Saved globally."))

    # ---- 6) List: create / view / edit / delete -----------------------------
    # @dashboard_list renders a table with actions. The method returns rows
    # [{"id": ..., "cells": {column_key: value}}]. Optional: @<list>.on_delete /
    # @<list>.edit_form (returns a PanelSchema) / @<list>.on_edit (saves).
    @dashboard_list(
        "items", L("Vorlage-Liste", "Template list"), mount="guild_settings", permission="guild_admin", order=30,
        columns=[{"key": "name", "label": "Name"}, {"key": "note", "label": "Note"}],
        description=L("Beispiel-Liste: anlegen (Tab links), bearbeiten und löschen.",
                      "Example list: create (tab on the left), edit and delete."),
    )
    async def items_list(self, ctx):
        items = await self.config.guild(ctx.guild).items()
        return [
            {"id": str(k), "cells": {"name": str(v.get("name", k)), "note": str(v.get("note", ""))}}
            for k, v in (items or {}).items() if isinstance(v, dict)
        ]

    @items_list.edit_form
    async def items_edit_form(self, ctx, item_id):
        items = await self.config.guild(ctx.guild).items()
        entry = (items or {}).get(str(item_id)) or {}
        return PanelSchema(fields=[
            Field.text("name", "Name", value=str(entry.get("name", ""))),
            Field.text("note", "Note", value=str(entry.get("note", ""))),
        ])

    @items_list.on_edit
    async def items_edit(self, ctx, item_id, data):
        async with self.config.guild(ctx.guild).items() as items:
            entry = items.get(str(item_id)) if isinstance(items.get(str(item_id)), dict) else {}
            entry["name"] = str(data.get("name", "")).strip() or entry.get("name", "")
            entry["note"] = str(data.get("note", ""))
            items[str(item_id)] = entry
        return SubmitResult.ok(tr(ctx, "Eintrag aktualisiert.", "Entry updated."))

    @items_list.on_delete
    async def items_delete(self, ctx, item_id):
        async with self.config.guild(ctx.guild).items() as items:
            if str(item_id) in items:
                del items[str(item_id)]
            else:
                return SubmitResult.fail(tr(ctx, "Eintrag nicht gefunden.", "Entry not found."))
        return SubmitResult.ok(tr(ctx, "Eintrag gelöscht.", "Entry deleted."))

    # Create panel (order=25 -> tab to the left of the list at order=30).
    @dashboard_panel("item_add", L("Eintrag anlegen", "Add entry"), mount="guild_settings", permission="guild_admin", order=25)
    async def item_add_panel(self, ctx):
        return PanelSchema(
            description=tr(ctx, "Neuen Listen-Eintrag anlegen.", "Create a new list entry."),
            # A custom button label is fine too — localize it with tr(ctx, ...).
            submit_label=tr(ctx, "Anlegen", "Create"),
            fields=[
                Field.text("name", "Name", value="", placeholder="e.g. Rule 1"),
                Field.text("note", "Note", value=""),
            ],
        )

    @item_add_panel.on_submit
    async def item_add(self, ctx, data):
        import uuid
        name = str(data.get("name", "")).strip()
        if not name:
            return SubmitResult.fail(tr(ctx, "Bitte einen Namen angeben.", "Please provide a name."))
        async with self.config.guild(ctx.guild).items() as items:
            items[uuid.uuid4().hex[:8]] = {"name": name, "note": str(data.get("note", ""))}
        return SubmitResult.ok(tr(ctx, "Eintrag angelegt.", "Entry created."))

    # ---- 5) Full page (@dashboard_page) -------------------------------------
    # A standalone page (component tree, no raw HTML). scope="global" -> shows up
    # in the "Module (Cog) Sites" menu; scope="guild" -> button on the server page.
    # Server-driven controls: the chosen values arrive in ctx.params, and the
    # handler just returns a fresh PageSchema for that selection.
    @dashboard_page(
        "example",
        L("Vorlage-Seite", "Template page"),
        scope="global",
        permission="authenticated",
        icon="chart",
    )
    async def example_page(self, ctx):
        mode = (ctx.params or {}).get("mode") or "a"
        controls = [
            Control.select(
                "mode",
                L("Ansicht", "View"),
                [{"value": "a", "label": L("Reihe A", "Series A")},
                 {"value": "b", "label": L("Reihe B", "Series B")}],
                value=mode,
            )
        ]
        data = [3, 5, 4, 6] if mode == "a" else [6, 4, 5, 3]
        comps = [
            Component.heading(L("Beispiel-Seite", "Example page")),
            Component.text(tr(ctx, "Wähle oben eine Ansicht.", "Pick a view above.")),
            Component.chart(
                labels=["Mo", "Di", "Mi", "Do"],
                series=[{"label": mode.upper(), "data": data}],
                title=L("Trend", "Trend"),
                height=280,
            ),
        ]
        return PageSchema(components=comps, controls=controls)

    # ---- Owner command for a quick check ------------------------------------
    # Demonstrates tr_lang: this is DISCORD output, so it follows the cog's
    # PER-GUILD `language` setting (chosen in the "Template settings" panel) —
    # not the website language. Fetch the guild language, then wrap each string.
    @commands.is_owner()
    @commands.hybrid_command(
        name="dashboardtemplate",
        description="Show whether the WebDashboard cog is loaded (template self-check).",
        extras={"i18n_desc": {
            "de-DE": "Zeigt, ob das WebDashboard-Cog geladen ist (Vorlagen-Selbsttest).",
            "en-US": "Show whether the WebDashboard cog is loaded (template self-check).",
        }},
    )
    async def _status(self, ctx: commands.Context) -> None:
        """Show whether the WebDashboard cog is loaded (template self-check)."""
        lang = await self.config.guild(ctx.guild).language() if ctx.guild else "en-US"
        loaded = (self.bot.get_cog("pdc_webdashboard") or self.bot.get_cog("WebDashboard")) is not None
        await ctx.send(tr_lang(
            lang,
            f"WebDashboard geladen: {loaded}. Panels: settings (Gilde), api (global).",
            f"WebDashboard loaded: {loaded}. Panels: settings (guild), api (global).",
        ))
