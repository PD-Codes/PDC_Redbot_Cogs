"""Drop-in hook for third-party cogs (reference implementation).

Copy this file as ``pdc_dashboard.py`` into your cog OR import it directly
(``from pdc_webdashboard.integration.dropin import ...``) when WebDashboard is already
installed as a cog in the same bot.

Properties:
* **No hard dependency** - works even when ``pdc_webdashboard`` is not installed
  (the decorators then become no-ops).
* **Opt-in at runtime** - ``register_dashboard`` only integrates when the
  ``WebDashboard`` cog is actually loaded; otherwise nothing happens.
* **AAA3A-compatible** - does not collide with AAA3A's ``DashboardIntegration`` /
  ``@dashboard_page``; both dashboards can run at the same time. Import under an
  alias if needed (see INTEGRATION.md).
"""
from __future__ import annotations

try:
    # Import from the submodules so that this works both as an internal import
    # (within the pdc_webdashboard package) and as a file copied into a
    # third-party cog.
    try:
        from .context import DashboardContext  # type: ignore  # noqa: F401
        from .decorators import (  # type: ignore  # noqa: F401
            dashboard_list,
            dashboard_page,
            dashboard_panel,
            dashboard_widget,
        )
        from .models import (  # type: ignore  # noqa: F401
            Component,
            Control,
            Field,
            L,
            PageSchema,
            PanelSchema,
            SubmitResult,
            WidgetData,
            resolve_locale,
            tr,
            tr_lang,
        )
    except ImportError:
        # as a copied file (no package context): absolute import
        from pdc_webdashboard.integration.context import DashboardContext  # type: ignore  # noqa: F401,E501
        from pdc_webdashboard.integration.decorators import (  # type: ignore  # noqa: F401
            dashboard_list,
            dashboard_page,
            dashboard_panel,
            dashboard_widget,
        )
        from pdc_webdashboard.integration.models import (  # type: ignore  # noqa: F401
            Component,
            Control,
            Field,
            L,
            PageSchema,
            PanelSchema,
            SubmitResult,
            WidgetData,
            resolve_locale,
            tr,
            tr_lang,
        )

    DASHBOARD_AVAILABLE = True
except Exception:  # pragma: no cover - pdc_webdashboard not installed
    DASHBOARD_AVAILABLE = False

    def _noop_decorator(*_args, **_kwargs):
        def deco(func):
            return func

        return deco

    # Decorators become no-ops; marked methods stay normal methods.
    def _noop_panel(*_args, **_kwargs):
        def deco(func):
            def on_submit(sub):
                return sub

            func.on_submit = on_submit
            return func

        return deco

    def _noop_list(*_args, **_kwargs):
        def deco(func):
            def _passthrough(sub):
                return sub

            func.on_delete = _passthrough
            func.edit_form = _passthrough
            func.on_edit = _passthrough
            return func

        return deco

    dashboard_widget = dashboard_page = _noop_decorator  # type: ignore
    dashboard_panel = _noop_panel  # type: ignore
    dashboard_list = _noop_list  # type: ignore

    # Standalone copies of the localization helpers so cogs can keep using
    # L(...)/tr(...) even without the dashboard installed. Default: en-US.
    def L(de, en=None):  # type: ignore  # noqa: N802
        """Build a localized text: L("Profil", "Profile"). One arg = same everywhere."""
        if en is None:
            return de
        return {"de-DE": de, "en-US": en}

    def resolve_locale(value, locale=None):  # type: ignore
        """Resolve a localized string; unknown/unset locales default to en-US."""
        if not isinstance(value, dict):
            return value
        loc = str(locale or "en-US")
        if loc in value:
            return value[loc]
        lang = loc.split("-")[0].lower()
        for k, v in value.items():
            if str(k).split("-")[0].lower() == lang:
                return v
        if "en-US" in value:
            return value["en-US"]
        for k, v in value.items():
            if str(k).split("-")[0].lower() == "en":
                return v
        return next(iter(value.values()), "")

    def tr(ctx, de, en):  # type: ignore
        """Pick text by the web UI language (ctx.locale); default English."""
        loc = str(getattr(ctx, "locale", "") or "")
        return de if loc.lower().startswith("de") else en

    def tr_lang(lang, de, en):  # type: ignore
        """Pick text by a per-guild language setting; default English."""
        return de if str(lang or "").lower().startswith("de") else en

    class _Stub:
        """Placeholder for when the data classes are used without a loaded dashboard."""

        def __init__(self, *_a, **_k):
            ...

        def to_dict(self):
            return {}

        @classmethod
        def _factory(cls, *_a, **_k):
            return cls()

        # common constructors
        kpi = list = chart = status = markdown = ok = fail = _factory  # type: ignore
        heading = text = divider = table = grid = select = _factory  # type: ignore
        switch = textarea = number = channel = role = multiselect = _factory  # type: ignore

    WidgetData = PanelSchema = PageSchema = Field = Component = SubmitResult = _Stub  # type: ignore
    Control = _Stub  # type: ignore
    DashboardContext = object  # type: ignore


def register_dashboard(cog) -> bool:
    """Call this in ``cog_load``. Integrates ONLY when WebDashboard is loaded.

    Returns ``True`` if registration happened, otherwise ``False``.
    """
    dashboard = cog.bot.get_cog("pdc_webdashboard") or cog.bot.get_cog("WebDashboard")
    if dashboard is None:
        return False
    dashboard.register_third_party(cog)
    return True


def unregister_dashboard(cog) -> None:
    """Call this in ``cog_unload`` (safe even if nothing was registered)."""
    dashboard = cog.bot.get_cog("pdc_webdashboard") or cog.bot.get_cog("WebDashboard")
    if dashboard is not None:
        try:
            dashboard.unregister_third_party(cog)
        except Exception:
            pass
