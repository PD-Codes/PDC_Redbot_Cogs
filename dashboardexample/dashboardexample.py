"""Example cog: shows integration into the PDC web dashboard.

Demonstrates a widget (KPI), a panel (form with saving) and conditional
registration. Also works without WebDashboard installed.
"""
from __future__ import annotations

from redbot.core import Config, commands
from redbot.core.bot import Red

from .pdc_dashboard import (
    DASHBOARD_AVAILABLE,
    Field,
    L,
    PanelSchema,
    SubmitResult,
    WidgetData,
    dashboard_panel,
    dashboard_widget,
    register_dashboard,
    tr,
    tr_lang,
    unregister_dashboard,
)

# Three localization helpers:
#   L("de","en")            -> decorator NAME/description, follows the WEB language.
#   tr(ctx,"de","en")       -> handler text (descriptions, SubmitResult, widget),
#                              follows the WEB language (ctx.locale).
#   tr_lang(lang,"de","en") -> DISCORD output, follows the per-guild `language`.


class DashboardExample(commands.Cog):
    """Small example cog for the web dashboard integration."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xDA5B0A4D, force_registration=True)
        self.config.register_guild(
            language="en-US",  # per-guild language for this cog's Discord output
            greeting={"enabled": False, "message": "Willkommen!", "channel": None},
        )

    # ------------------------------------------------------------------ #
    # Lifecycle – the "extra": only integrate when the dashboard is present
    # ------------------------------------------------------------------ #
    async def cog_load(self) -> None:
        register_dashboard(self)  # No-op if WebDashboard is not loaded

    def cog_unload(self) -> None:
        unregister_dashboard(self)

    # ------------------------------------------------------------------ #
    # Widget – appears as a tile on the central board
    # ------------------------------------------------------------------ #
    @dashboard_widget(
        "member_count", L("Mitglieder", "Members"), size="sm", refresh=60, permission="guild_member"
    )
    async def member_count_widget(self, ctx):
        guild = ctx.guild
        # Widget text is shown in the web -> tr(ctx, ...) follows the website language.
        label = tr(ctx, "Mitglieder", "Members")
        if guild is None:
            return WidgetData.kpi(value="–", label=label)
        return WidgetData.kpi(value=guild.member_count, label=label, icon="users")

    # ------------------------------------------------------------------ #
    # Panel – contextual form (embedded, not its own page)
    # ------------------------------------------------------------------ #
    @dashboard_panel(
        "greeting", L("Begrüßung", "Greeting"), mount="guild_settings", permission="guild_admin"
    )
    async def greeting_panel(self, ctx):
        cfg = await self.config.guild(ctx.guild).greeting()
        return PanelSchema(
            description=tr(ctx, "Begrüßungsnachricht für neue Mitglieder.",
                           "Greeting message for new members."),
            fields=[
                Field.switch("enabled", "Enabled", value=cfg["enabled"]),
                Field.textarea("message", "Message", value=cfg["message"], max_length=1000),
                Field.channel("channel", "Channel", value=cfg["channel"]),
            ],
        )

    @greeting_panel.on_submit
    async def save_greeting(self, ctx, data):
        await self.config.guild(ctx.guild).greeting.set(
            {
                "enabled": bool(data.get("enabled")),
                "message": str(data.get("message", ""))[:1000],
                "channel": data.get("channel"),
            }
        )
        return SubmitResult.ok(tr(ctx, "Begrüßung gespeichert.", "Greeting saved."))

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

    # ------------------------------------------------------------------ #
    # Owner command for a quick check
    # ------------------------------------------------------------------ #
    @commands.is_owner()
    @commands.hybrid_command(
        name="dashboardexample",
        description="Show whether the WebDashboard integration is available.",
        extras={"i18n_desc": {
            "de-DE": "Zeigt, ob die WebDashboard-Integration verfügbar ist.",
            "en-US": "Show whether the WebDashboard integration is available.",
        }},
    )
    async def _status(self, ctx: commands.Context) -> None:
        """Show whether the WebDashboard integration is available (example check)."""
        # Discord output -> tr_lang with the per-guild language setting.
        lang = await self.config.guild(ctx.guild).language() if ctx.guild else "en-US"
        loaded = (self.bot.get_cog("pdc_webdashboard") or self.bot.get_cog("WebDashboard")) is not None
        await ctx.send(tr_lang(
            lang,
            f"WebDashboard-Integration: {'verfügbar' if DASHBOARD_AVAILABLE else 'nicht installiert'}; "
            f"Cog geladen: {loaded}.",
            f"WebDashboard integration: {'available' if DASHBOARD_AVAILABLE else 'not installed'}; "
            f"cog loaded: {loaded}.",
        ))
