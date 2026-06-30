"""Public integration API of the PDC Web Dashboard.

Third-party cogs import from here::

    from pdc_webdashboard.integration import (
        DashboardIntegration, dashboard_widget, dashboard_panel, dashboard_page,
        DashboardContext, WidgetData, PanelSchema, PageSchema,
        Field, Component, SubmitResult,
        register_dashboard, unregister_dashboard, DASHBOARD_AVAILABLE,
    )
"""
from .base import DashboardIntegration
from .context import DashboardContext
from .decorators import (
    dashboard_list,
    dashboard_page,
    dashboard_panel,
    dashboard_widget,
    iter_contributions,
)
from .models import (
    Component,
    Field,
    FieldType,
    L,
    LocalizedStr,
    PageSchema,
    PanelSchema,
    SubmitResult,
    WidgetData,
    WidgetKind,
    resolve_locale,
    tr,
    tr_lang,
)
from .registry import Contribution, Registry
from .dropin import (
    DASHBOARD_AVAILABLE,
    register_dashboard,
    unregister_dashboard,
)

__all__ = [
    "DashboardIntegration",
    "DashboardContext",
    "dashboard_widget",
    "dashboard_panel",
    "dashboard_page",
    "dashboard_list",
    "iter_contributions",
    "WidgetData",
    "WidgetKind",
    "PanelSchema",
    "PageSchema",
    "Field",
    "FieldType",
    "Component",
    "SubmitResult",
    "L",
    "LocalizedStr",
    "tr",
    "tr_lang",
    "resolve_locale",
    "Registry",
    "Contribution",
    "register_dashboard",
    "unregister_dashboard",
    "DASHBOARD_AVAILABLE",
]
