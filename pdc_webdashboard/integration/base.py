"""``DashboardIntegration`` - mixin that third-party cogs inherit from.

Usage in a cog::

    from redbot.core import commands
    # adjust the path to your own installation if needed:
    from pdc_webdashboard.integration import (
        DashboardIntegration, dashboard_widget, dashboard_panel,
        WidgetData, PanelSchema, Field, SubmitResult,
    )

    class MyCog(DashboardIntegration, commands.Cog):
        def __init__(self, bot):
            self.bot = bot

        @dashboard_widget("hello", "Hallo")
        async def hello_widget(self, ctx):
            return WidgetData.kpi(value=42, label="Antwort")

The mixin registers the cog with the ``WebDashboard`` cog as soon as it becomes
available - even if the dashboard is loaded after the third-party cog.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("red.pdc.pdc_webdashboard.integration")


class DashboardIntegration:
    """Mixin for third-party cogs that integrate into the PDC Web Dashboard."""

    bot: Any  # provided by the cog class

    async def cog_load(self) -> None:  # type: ignore[override]
        # run the subclass's own cog_load logic first
        parent_load = getattr(super(), "cog_load", None)
        if parent_load is not None:
            await parent_load()
        self._register_with_dashboard()

    def cog_unload(self) -> None:  # type: ignore[override]
        dashboard = self.bot.get_cog("pdc_webdashboard") or self.bot.get_cog("WebDashboard")
        if dashboard is not None:
            try:
                dashboard.unregister_third_party(self)
            except Exception:
                log.exception("Failed to unregister from WebDashboard")
        parent_unload = getattr(super(), "cog_unload", None)
        if parent_unload is not None:
            parent_unload()

    def _register_with_dashboard(self) -> None:
        dashboard = self.bot.get_cog("pdc_webdashboard") or self.bot.get_cog("WebDashboard")
        if dashboard is None:
            # Dashboard not loaded yet - it will pick us up once it loads.
            log.debug("WebDashboard not loaded yet; registration will be picked up later.")
            return
        try:
            dashboard.register_third_party(self)
        except Exception:
            log.exception("Failed to register with WebDashboard")
