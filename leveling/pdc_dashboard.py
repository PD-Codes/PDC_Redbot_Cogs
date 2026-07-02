"""Drop-in hook for the PDC web dashboard integration (no hard dependency).

This file can be copied unchanged into every cog. It also works when the
``pdc_webdashboard`` cog is not installed (the decorators then become no-ops) and can
be used alongside the AAA3A dashboard.
"""
from __future__ import annotations

try:
    from pdc_webdashboard.integration.context import DashboardContext  # noqa: F401
    from pdc_webdashboard.integration.decorators import (  # noqa: F401
        dashboard_page,
        dashboard_panel,
        dashboard_widget,
    )
    from pdc_webdashboard.integration.models import (  # noqa: F401
        Component,
        Control,
        Field,
        L,
        PageSchema,
        PanelSchema,
        SubmitResult,
        WidgetData,
        tr,
        tr_lang,
    )

    DASHBOARD_AVAILABLE = True

    # Guarantee `.on_submit` exists even if the *installed* pdc_webdashboard is older
    # than this drop-in (older builds did not attach the helper). Keeps the cog
    # loadable regardless of the running pdc_webdashboard version.
    _real_dashboard_panel = dashboard_panel  # type: ignore[has-type]

    def dashboard_panel(*_a, **_k):  # type: ignore[no-redef]
        _deco = _real_dashboard_panel(*_a, **_k)

        def _wrap(func):
            func = _deco(func)
            if not hasattr(func, "on_submit"):
                def on_submit(_sub):
                    return _sub
                func.on_submit = on_submit  # type: ignore[attr-defined]
            return func

        return _wrap
except Exception:  # pdc_webdashboard not installed
    DASHBOARD_AVAILABLE = False

    def _noop_decorator(*_args, **_kwargs):
        def deco(func):
            return func

        return deco

    def _noop_panel(*_args, **_kwargs):
        def deco(func):
            def on_submit(sub):
                return sub

            func.on_submit = on_submit
            return func

        return deco

    dashboard_widget = dashboard_page = _noop_decorator  # type: ignore
    dashboard_panel = _noop_panel  # type: ignore

    class _Stub:
        def __init__(self, *_a, **_k):
            ...

        def to_dict(self):
            return {}

        @classmethod
        def _factory(cls, *_a, **_k):
            return cls()

        kpi = list = chart = status = markdown = ok = fail = _factory  # type: ignore

    WidgetData = PanelSchema = PageSchema = Field = Component = Control = SubmitResult = _Stub  # type: ignore
    DashboardContext = object  # type: ignore

    def L(de, en=None):
        # Without the dashboard the labels are never rendered; keep English default.
        return en if en is not None else de

    def tr(ctx, de, en):
        loc = str(getattr(ctx, "locale", "") or "")
        return de if loc.lower().startswith("de") else en

    def tr_lang(lang, de, en):
        # Honour the per-guild language even without the dashboard (default: English).
        return de if str(lang or "").lower().startswith("de") else en


def register_dashboard(cog) -> bool:
    """Call in ``cog_load``. Only integrates if WebDashboard is loaded."""
    dashboard = cog.bot.get_cog("pdc_webdashboard") or cog.bot.get_cog("WebDashboard")
    if dashboard is None:
        return False
    dashboard.register_third_party(cog)
    return True


def unregister_dashboard(cog) -> None:
    """Call in ``cog_unload`` (always safe)."""
    dashboard = cog.bot.get_cog("pdc_webdashboard") or cog.bot.get_cog("WebDashboard")
    if dashboard is not None:
        try:
            dashboard.unregister_third_party(cog)
        except Exception:
            pass
