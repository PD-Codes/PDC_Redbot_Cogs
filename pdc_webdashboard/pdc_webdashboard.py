"""PDC Web Dashboard – companion cog.

Provides the RPC gateway and manages the integration registry into which
other cogs register their widgets, panels and pages.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
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
            # Security / gateway hardening
            cors_origins=[],      # allowed browser origins (empty = no CORS headers)
            request_log=False,    # optional RPC request logging for auditing
            token_rotate_h=0,     # automatic token rotation interval in hours (0 = off)
            token_rotated_at=0,   # unix timestamp of the last token (re)generation
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
            # Background monitor: automatic cog-update check + owner-DM alerts.
            monitor={"cog_update_interval_h": 0, "alerts_dm": False, "mem_threshold_mb": 0},
            monitor_last={"cogs": [], "checked_at": 0},
            monitor_alerted={"cogs": [], "mem": False},
        )
        self.registry = Registry()
        self.gateway: Optional[Gateway] = None
        self._monitor_task: Optional[asyncio.Task] = None

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
        # Background monitor (cog-update check + alerts).
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def cog_unload(self) -> None:
        from .gateway.logbuffer import uninstall as _uninstall_logbuffer
        _uninstall_logbuffer()
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
        await self._stop_gateway()

    async def _start_gateway(self) -> None:
        if self.gateway is not None:
            return
        token = await self.config.token()
        if not token:
            token = secrets.token_urlsafe(48)
            await self.config.token.set(token)
            await self.config.token_rotated_at.set(int(time.time()))
        host = await self.config.host()
        port = await self.config.port()
        cors_origins = await self.config.cors_origins()
        request_log = await self.config.request_log()
        self.gateway = Gateway(
            self.bot, self.registry, token=token, host=host, port=port,
            audit_sink=self._persist_audit,
            cors_origins=cors_origins, request_log=request_log,
        )
        try:
            await self.gateway.start()
        except Exception:
            log.exception("Gateway could not be started")
            self.gateway = None
            raise

    async def _stop_gateway(self) -> None:
        if self.gateway is not None:
            await self.gateway.stop()
            self.gateway = None

    # ------------------------------------------------------------------ #
    # Background monitor: cog-update check + owner-DM alerts
    # ------------------------------------------------------------------ #
    async def _monitor_loop(self) -> None:
        """Periodically check for cog updates (interval from config) and alert."""
        try:
            await self.bot.wait_until_red_ready()
        except Exception:
            pass
        while True:
            try:
                cfg = await self.config.monitor()
                h = int(cfg.get("cog_update_interval_h") or 0)
                if h > 0:
                    last = await self.config.monitor_last()
                    due = int(last.get("checked_at") or 0) + h * 3600
                    if time.time() >= due:
                        await self._run_monitor(cfg)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Monitor run failed", exc_info=True)
            try:
                # Automatic gateway-token rotation (optional, owner-configured).
                rot_h = int(await self.config.token_rotate_h() or 0)
                if rot_h > 0:
                    rotated_at = float(await self.config.token_rotated_at() or 0)
                    if time.time() >= rotated_at + rot_h * 3600:
                        await self._rotate_token(notify=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Automatic token rotation failed", exc_info=True)
            # Re-evaluate config/interval every 5 minutes (picks up changes quickly).
            await asyncio.sleep(300)

    async def _rotate_token(self, notify: bool = False) -> str:
        """Generate a new gateway token and apply it to the running gateway.

        The gateway keeps running (no downtime); only new requests must carry
        the new token. Optionally notifies the bot owners via DM.
        """
        token = secrets.token_urlsafe(48)
        await self.config.token.set(token)
        await self.config.token_rotated_at.set(int(time.time()))
        if self.gateway is not None:
            self.gateway.update_token(token)
        log.info("Gateway token rotated")
        if notify:
            await self._dm_owners(
                "🔑 **PDC Dashboard** — the gateway token was rotated automatically. "
                "Update the web app: fetch the new token with `[p]pdcdashboard token`."
            )
        return token

    async def _run_monitor(self, cfg: dict) -> None:
        cogs: list = []
        try:
            dl = self.bot.get_cog("Downloader")
            if dl is not None:
                # Reuse the gateway's helpers (lazy import avoids a circular import).
                from .gateway.methods import (
                    _cogs_with_updates,
                    _downloader_lock,
                    _installed_cogs,
                    _update_all_repos,
                )
                async with _downloader_lock:
                    await _update_all_repos(dl)
                    installed = await _installed_cogs(dl)
                    cogs = sorted(await _cogs_with_updates(dl, installed))
        except Exception:
            log.debug("Cog update check failed", exc_info=True)
        await self.config.monitor_last.set({"cogs": cogs, "checked_at": int(time.time())})
        if cfg.get("alerts_dm"):
            await self._maybe_alert(cogs, cfg)

    @staticmethod
    def _rss_mb() -> Optional[int]:
        try:
            import os

            import psutil  # Red ships psutil
            return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024))
        except Exception:
            return None

    async def _dm_owners(self, text: str) -> None:
        for oid in set(getattr(self.bot, "owner_ids", None) or []):
            try:
                user = self.bot.get_user(oid) or await self.bot.fetch_user(oid)
                if user is not None:
                    await user.send(text)
            except Exception:
                log.debug("Owner DM failed (%s)", oid, exc_info=True)

    async def _maybe_alert(self, cogs: list, cfg: dict) -> None:
        alerted = await self.config.monitor_alerted()
        # 1) Cog updates – only DM when the set of updatable cogs changed.
        if cogs and set(cogs) != set(alerted.get("cogs") or []):
            await self._dm_owners(
                f"🔔 **PDC Dashboard** — {len(cogs)} cog update(s) available: "
                + ", ".join(cogs)
                + "\nUpdate via the dashboard (Cogs) or `[p]cog update`."
            )
        alerted["cogs"] = cogs
        # 2) Memory threshold – DM once when crossing, reset when back below.
        thr = int(cfg.get("mem_threshold_mb") or 0)
        mem = self._rss_mb()
        if thr > 0 and mem is not None and mem >= thr:
            if not alerted.get("mem"):
                await self._dm_owners(f"⚠️ **PDC Dashboard** — high memory usage: {mem} MB (threshold {thr} MB).")
                alerted["mem"] = True
        elif alerted.get("mem"):
            alerted["mem"] = False
        await self.config.monitor_alerted.set(alerted)

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
            log.debug("Audit persistence failed", exc_info=True)

    def register_third_party(self, cog: Any) -> int:
        """Registers the dashboard contributions of a third-party cog."""
        return self.registry.register_cog(cog)

    def unregister_third_party(self, cog: Any) -> None:
        self.registry.unregister_cog(cog)

    # ------------------------------------------------------------------ #
    # Commands (bot owner only)
    # ------------------------------------------------------------------ #
    async def cog_check(self, ctx: commands.Context) -> bool:
        """Defence in depth: EVERY text/hybrid command of this cog is bot-owner only.

        Gateway token, bind address, CORS and logging are bot-level settings; no
        guild admin, mod or regular user may ever touch them. Each command also
        carries its own ``@commands.is_owner()`` check (parent-group checks are
        not reliably applied to hybrid subcommands invoked as slash commands).
        """
        return await self.bot.is_owner(ctx.author)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Same owner gate for app-command (slash) invocations of this cog."""
        return await self.bot.is_owner(interaction.user)

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

    @commands.is_owner()
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
        cors = await self.config.cors_origins()
        reqlog = await self.config.request_log()
        rotate_h = int(await self.config.token_rotate_h() or 0)
        contribs = len(self.registry.all())
        cogs = len({c.cog_name for c in self.registry.all()})
        lines = [
            _("Status: {state}").format(state=_("running") if running else _("stopped")),
            _("Address: http://{host}:{port}").format(host=host, port=port),
            _("Registered contributions: {n} (from {c} cogs)").format(n=contribs, c=cogs),
            _("CORS origins: {origins}").format(origins=", ".join(cors) if cors else "—"),
            _("Request logging: {state}").format(state=_("enabled") if reqlog else _("disabled")),
            _("Token auto-rotation: {value}").format(
                value=_("every {hours} h").format(hours=rotate_h) if rotate_h else _("disabled")
            ),
        ]
        await ctx.send(box("\n".join(lines)))

    @commands.is_owner()
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
            await ctx.send(_("Start failed: {error}").format(error=e))
            return
        await ctx.send(_("Gateway started."))

    @commands.is_owner()
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
        await ctx.send(_("Gateway stopped."))

    @commands.is_owner()
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
        if not 1 <= port <= 65535:
            await ctx.send(_("Value must be between {min} and {max}.").format(min=1, max=65535))
            return
        await self.config.host.set(host)
        await self.config.port.set(port)
        await ctx.send(_("Saved: {host}:{port}. Restart the gateway to apply.").format(host=host, port=port))

    @commands.is_owner()
    @dashboard_group.command(
        name="port", description="Set the gateway port (restart required).",
        extras={"i18n_desc": {
            "de-DE": "Setzt den Gateway-Port (Neustart erforderlich).",
            "en-US": "Set the gateway port (restart required).",
        }},
    )
    @app_commands.describe(port="Listen port (e.g. 6970)")
    async def dashboard_port(self, ctx: commands.Context, port: int) -> None:
        """Set the gateway port only (host stays unchanged; restart required)."""
        if not 1 <= port <= 65535:
            await ctx.send(_("Value must be between {min} and {max}.").format(min=1, max=65535))
            return
        host = await self.config.host()
        await self.config.port.set(port)
        await ctx.send(_("Saved: {host}:{port}. Restart the gateway to apply.").format(host=host, port=port))

    @commands.is_owner()
    @dashboard_group.command(
        name="cors", description="Manage allowed CORS origins (list/add/remove/clear).",
        extras={"i18n_desc": {
            "de-DE": "Verwaltet erlaubte CORS-Origins (list/add/remove/clear).",
            "en-US": "Manage allowed CORS origins (list/add/remove/clear).",
        }},
    )
    @app_commands.describe(
        action="list | add | remove | clear",
        origin="Origin, e.g. https://dashboard.example.com",
    )
    async def dashboard_cors(
        self, ctx: commands.Context, action: str = "list", origin: Optional[str] = None
    ) -> None:
        """Manage the allowed CORS origins of the gateway.

        Origins are full scheme://host[:port] values (no path). An empty list
        means the gateway sends no CORS headers at all (BFF-only usage).
        """
        action = (action or "list").lower()
        if action == "list":
            cors = await self.config.cors_origins()
            if cors:
                await ctx.send(_("CORS origins: {origins}").format(origins=", ".join(cors)))
            else:
                await ctx.send(_("No CORS origins configured."))
            return
        if action == "clear":
            await self.config.cors_origins.set([])
            if self.gateway is not None:
                self.gateway.update_cors_origins([])
            await ctx.send(_("CORS origins cleared."))
            return
        if action in ("add", "remove"):
            origin = (origin or "").strip().rstrip("/")
            if not origin or "://" not in origin or "/" in origin.split("://", 1)[1]:
                await ctx.send(_("Invalid origin. Use e.g. `https://dashboard.example.com`."))
                return
            async with self.config.cors_origins() as cors:
                if action == "add" and origin not in cors:
                    cors.append(origin)
                elif action == "remove" and origin in cors:
                    cors.remove(origin)
                current = list(cors)
            if self.gateway is not None:
                self.gateway.update_cors_origins(current)
            if action == "add":
                await ctx.send(_("Added CORS origin: {origin}").format(origin=origin))
            else:
                await ctx.send(_("Removed CORS origin: {origin}").format(origin=origin))
            return
        await ctx.send(_("Unknown action. Use: list, add, remove, clear."))

    @commands.is_owner()
    @dashboard_group.command(
        name="reqlog", description="Enable/disable RPC request logging (auditing).",
        extras={"i18n_desc": {
            "de-DE": "Aktiviert/deaktiviert das RPC-Request-Logging (Auditing).",
            "en-US": "Enable/disable RPC request logging (auditing).",
        }},
    )
    @app_commands.describe(enabled="true = log dashboard RPC calls, false = off")
    async def dashboard_reqlog(self, ctx: commands.Context, enabled: Optional[bool] = None) -> None:
        """Show or toggle the optional request logging of dashboard RPC calls.

        When enabled, every RPC call (method, user, guild, duration, outcome) is
        written to the bot log and kept in an in-memory ring buffer that can be
        queried via the ``requests.list`` RPC method (owner only).
        """
        if enabled is None:
            state = await self.config.request_log()
            await ctx.send(_("Request logging: {state}").format(
                state=_("enabled") if state else _("disabled")))
            return
        await self.config.request_log.set(bool(enabled))
        if self.gateway is not None:
            self.gateway.set_request_logging(bool(enabled))
        await ctx.send(_("Request logging: {state}").format(
            state=_("enabled") if enabled else _("disabled")))

    @commands.is_owner()
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
            await self.config.token_rotated_at.set(int(time.time()))
        try:
            await ctx.author.send(box(token))
            await ctx.send(_("Token sent via DM."))
        except discord.Forbidden:
            await ctx.send(_("I could not send you a DM. Please enable DMs."))

    @commands.is_owner()
    @dashboard_group.command(
        name="regen", description="Generate a new gateway token (the web app must be updated).",
        extras={"i18n_desc": {
            "de-DE": "Erzeugt ein neues Gateway-Token (die Web-App muss aktualisiert werden).",
            "en-US": "Generate a new gateway token (the web app must be updated).",
        }},
    )
    async def dashboard_regen(self, ctx: commands.Context) -> None:
        """Generate a new gateway token (the web app must be updated)."""
        await self._rotate_token(notify=False)
        await ctx.send(_("New token generated and applied. Retrieve it with `[p]pdcdashboard token`."))

    @commands.is_owner()
    @dashboard_group.command(
        name="tokenrotate", description="Set the automatic token rotation interval in hours (0 = off).",
        extras={"i18n_desc": {
            "de-DE": "Setzt das automatische Token-Rotationsintervall in Stunden (0 = aus).",
            "en-US": "Set the automatic token rotation interval in hours (0 = off).",
        }},
    )
    @app_commands.describe(hours="Rotation interval in hours (0 = off, max 720)")
    async def dashboard_tokenrotate(self, ctx: commands.Context, hours: int) -> None:
        """Configure automatic gateway-token rotation.

        When active, the token is regenerated every ``hours`` hours; the bot
        owners are notified via DM so the web app can be updated.
        """
        if not 0 <= hours <= 720:
            await ctx.send(_("Value must be between {min} and {max}.").format(min=0, max=720))
            return
        await self.config.token_rotate_h.set(hours)
        if hours:
            await ctx.send(_("Token auto-rotation: {value}").format(
                value=_("every {hours} h").format(hours=hours)))
        else:
            await ctx.send(_("Automatic token rotation disabled."))
