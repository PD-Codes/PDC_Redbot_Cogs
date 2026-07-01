"""PDC Web Dashboard – companion cog.

Provides the RPC gateway and manages the integration registry into which
other cogs register their widgets, panels and pages.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import box

from .gateway import Gateway
from .integration.base import DashboardIntegration
from .integration.registry import Registry

log = logging.getLogger("red.pdc.pdc_webdashboard")
_ = Translator("WebDashboard", __file__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6970


@cog_i18n(_)
class WebDashboard(commands.Cog, name="pdc_webdashboard"):
    """Custom, modular web dashboard system for Red.

    Cogs hook in via integration (widgets + contextual panels) instead of
    creating their own separate page.
    """

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xD8B0A12D, force_registration=True)
        self.config.register_global(
            token=None,
            host=DEFAULT_HOST,
            port=DEFAULT_PORT,
            autostart=True,
            # Branding / UI
            ui={
                "title": "PDC Dashboard",
                "icon": None,
                "description": "",
                "support_url": "",
                "color": "indigo",
                "theme": "dark",
            },
            # Overview / security
            locked=False,
            session_epoch=0,
            custom_pages=[],  # [{slug, title, html, nav}]
            audit_log=[],     # [{action, user, guild, detail, time}] – last 1000
        )
        self.registry = Registry()
        self.gateway: Optional[Gateway] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def cog_load(self) -> None:
        # Capture recent log records in memory for the dashboard Log-Viewer.
        from .gateway.logbuffer import install as _install_logbuffer
        _install_logbuffer()
        # Collect already-loaded third-party cogs – regardless of whether they use
        # the DashboardIntegration mixin or merely decorated methods and would have
        # registered later. This way any load order works.
        from .integration.decorators import iter_contributions
        for cog in self.bot.cogs.values():
            if isinstance(cog, DashboardIntegration) or iter_contributions(cog):
                self.registry.register_cog(cog)
        if await self.config.autostart():
            await self._start_gateway()

    async def cog_unload(self) -> None:
        from .gateway.logbuffer import uninstall as _uninstall_logbuffer
        _uninstall_logbuffer()
        await self._stop_gateway()

    async def _start_gateway(self) -> None:
        if self.gateway is not None:
            return
        token = await self.config.token()
        if not token:
            token = secrets.token_urlsafe(48)
            await self.config.token.set(token)
        host = await self.config.host()
        port = await self.config.port()
        self.gateway = Gateway(
            self.bot, self.registry, token=token, host=host, port=port,
            audit_sink=self._persist_audit,
        )
        try:
            await self.gateway.start()
        except Exception:
            log.exception("Gateway konnte nicht gestartet werden")
            self.gateway = None
            raise

    async def _stop_gateway(self) -> None:
        if self.gateway is not None:
            await self.gateway.stop()
            self.gateway = None

    # ------------------------------------------------------------------ #
    # Public integration API (used by DashboardIntegration)
    # ------------------------------------------------------------------ #
    async def _persist_audit(self, entry: dict) -> None:
        """Audit sink: appends each logged operation to the audit log (capped)."""
        try:
            async with self.config.audit_log() as logs:
                logs.append(entry)
                del logs[:-1000]  # keep only the last 1000 entries
        except Exception:
            log.debug("Audit-Persistierung fehlgeschlagen", exc_info=True)

    def register_third_party(self, cog: Any) -> int:
        """Registers the dashboard contributions of a third-party cog."""
        return self.registry.register_cog(cog)

    def unregister_third_party(self, cog: Any) -> None:
        self.registry.unregister_cog(cog)

    # ------------------------------------------------------------------ #
    # Commands (bot owner only)
    # ------------------------------------------------------------------ #
    @commands.is_owner()
    @commands.hybrid_group(
        name="pdcdashboard", aliases=["pdcdash"], description="Manage the PDC web dashboard.",
        extras={"i18n_desc": {
            "de-DE": "Verwaltet das PDC-Web-Dashboard.",
            "en-US": "Manage the PDC web dashboard.",
        }},
    )
    async def dashboard_group(self, ctx: commands.Context) -> None:
        """Manage the PDC web dashboard.

        Note: Uses its own command name so it can run alongside AAA3A's `[p]dashboard`.
        """

    @dashboard_group.command(
        name="status", description="Show the current status of the gateway.",
        extras={"i18n_desc": {
            "de-DE": "Zeigt den aktuellen Status des Gateways.",
            "en-US": "Show the current status of the gateway.",
        }},
    )
    async def dashboard_status(self, ctx: commands.Context) -> None:
        """Show the current status of the gateway."""
        running = self.gateway is not None
        host = await self.config.host()
        port = await self.config.port()
        contribs = len(self.registry.all())
        cogs = len({c.cog_name for c in self.registry.all()})
        lines = [
            _("Status: {state}").format(state=_("läuft") if running else _("gestoppt")),
            _("Adresse: http://{host}:{port}").format(host=host, port=port),
            _("Registrierte Beiträge: {n} (aus {c} Cogs)").format(n=contribs, c=cogs),
        ]
        await ctx.send(box("\n".join(lines)))

    @dashboard_group.command(
        name="start", description="Start the gateway.",
        extras={"i18n_desc": {
            "de-DE": "Startet das Gateway.",
            "en-US": "Start the gateway.",
        }},
    )
    async def dashboard_start(self, ctx: commands.Context) -> None:
        """Start the gateway."""
        try:
            await self._start_gateway()
        except Exception as e:
            await ctx.send(_("Start fehlgeschlagen: {error}").format(error=e))
            return
        await ctx.send(_("Gateway gestartet."))

    @dashboard_group.command(
        name="stop", description="Stop the gateway.",
        extras={"i18n_desc": {
            "de-DE": "Stoppt das Gateway.",
            "en-US": "Stop the gateway.",
        }},
    )
    async def dashboard_stop(self, ctx: commands.Context) -> None:
        """Stop the gateway."""
        await self._stop_gateway()
        await ctx.send(_("Gateway gestoppt."))

    @dashboard_group.command(
        name="bind", description="Set the gateway host and port (restart required).",
        extras={"i18n_desc": {
            "de-DE": "Setzt Host und Port des Gateways (Neustart erforderlich).",
            "en-US": "Set the gateway host and port (restart required).",
        }},
    )
    @app_commands.describe(host="Listen address (e.g. 127.0.0.1)", port="Listen port (e.g. 6970)")
    async def dashboard_bind(self, ctx: commands.Context, host: str, port: int) -> None:
        """Set the host and port (restart required).

        Note: For security reasons the gateway should listen only on 127.0.0.1
        and be exposed externally through a reverse proxy or tunnel.
        """
        await self.config.host.set(host)
        await self.config.port.set(port)
        await ctx.send(_("Gespeichert: {host}:{port}. Bitte neu starten.").format(host=host, port=port))

    @dashboard_group.command(
        name="token", description="Send the gateway token via DM (for configuring the web app).",
        extras={"i18n_desc": {
            "de-DE": "Sendet das Gateway-Token per DM (zur Einrichtung der Web-App).",
            "en-US": "Send the gateway token via DM (for configuring the web app).",
        }},
    )
    async def dashboard_token(self, ctx: commands.Context) -> None:
        """Send the gateway token via DM (for configuring the web app)."""
        token = await self.config.token()
        if not token:
            token = secrets.token_urlsafe(48)
            await self.config.token.set(token)
        try:
            await ctx.author.send(box(token))
            await ctx.send(_("Token per DM gesendet."))
        except discord.Forbidden:
            await ctx.send(_("Ich konnte dir keine DM senden. Bitte DMs aktivieren."))

    @dashboard_group.command(
        name="regen", description="Generate a new gateway token (the web app must be updated).",
        extras={"i18n_desc": {
            "de-DE": "Erzeugt ein neues Gateway-Token (die Web-App muss aktualisiert werden).",
            "en-US": "Generate a new gateway token (the web app must be updated).",
        }},
    )
    async def dashboard_regen(self, ctx: commands.Context) -> None:
        """Generate a new gateway token (the web app must be updated)."""
        token = secrets.token_urlsafe(48)
        await self.config.token.set(token)
        await self._stop_gateway()
        await self._start_gateway()
        await ctx.send(_("Neues Token erzeugt und Gateway neu gestartet. Hole es mit `[p]pdcdashboard token`."))
