from redbot.core.bot import Red

from .pdc_webdashboard import WebDashboard

# Public integration API, also reachable via the cog package:
from .integration import (  # noqa: F401
    DashboardContext,
    DashboardIntegration,
    Field,
    PageSchema,
    PanelSchema,
    SubmitResult,
    WidgetData,
    dashboard_page,
    dashboard_panel,
    dashboard_widget,
)

__all__ = [
    "WebDashboard",
    "DashboardIntegration",
    "dashboard_widget",
    "dashboard_panel",
    "dashboard_page",
    "DashboardContext",
    "WidgetData",
    "PanelSchema",
    "PageSchema",
    "Field",
    "SubmitResult",
]


async def setup(bot: Red) -> None:
    await bot.add_cog(WebDashboard(bot))
