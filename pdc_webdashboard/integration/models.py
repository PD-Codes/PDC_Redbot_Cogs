"""Declarative data models for the dashboard integration contract.

Third-party cogs return only these schemas - never raw HTML.
The frontend renders them with themeable shadcn-svelte components. This means
cog content cannot introduce an XSS attack surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union


# --------------------------------------------------------------------------- #
# Localization helpers (web UI language)
# --------------------------------------------------------------------------- #
# A dashboard text can be a plain str (same for everyone) or a per-locale dict
# {"de-DE": "...", "en-US": "..."}. The gateway resolves it against the language
# the user picked in the web UI (passed as ctx.locale).
LocalizedStr = Union[str, Dict[str, str]]


def resolve_locale(value: "LocalizedStr", locale: Optional[str] = None) -> str:
    """Resolve a localized string against `locale` (e.g. 'de-DE' / 'en-US').

    When no locale is chosen (or the locale is unknown), the default is ALWAYS
    en-US — never the first dict entry, which for ``L(de, en)`` would silently
    make German the default.
    """
    if not isinstance(value, dict):
        return value
    loc = str(locale or "en-US")
    if loc in value:
        return value[loc]
    lang = loc.split("-")[0].lower()
    for k, v in value.items():
        if str(k).split("-")[0].lower() == lang:
            return v
    # English fallback for unknown/unset locales.
    if "en-US" in value:
        return value["en-US"]
    for k, v in value.items():
        if str(k).split("-")[0].lower() == "en":
            return v
    return next(iter(value.values()), "")


def L(de: str, en: Optional[str] = None) -> "LocalizedStr":
    """Build a localized dashboard text. ``L("Profil", "Profile")``; passing a
    single argument keeps the same text for every language."""
    if en is None:
        return de
    return {"de-DE": de, "en-US": en}


def tr(ctx: Any, de: str, en: str) -> str:
    """Inside a handler: pick text by the web UI language (``ctx.locale``)."""
    loc = str(getattr(ctx, "locale", "") or "")
    return de if loc.lower().startswith("de") else en


def tr_lang(lang: Optional[str], de: str, en: str) -> str:
    """Pick OUTPUT text by a per-guild language setting ('de-DE' / 'en-US').

    For a cog's Discord output (responses, embeds, DMs). The cog stores a per-guild
    ``language`` and passes it here: ``tr_lang(lang, "Deutsch", "English")``."""
    return de if str(lang or "").lower().startswith("de") else en


# --------------------------------------------------------------------------- #
# Widgets (tiles on the central board)
# --------------------------------------------------------------------------- #
class WidgetKind(str, Enum):
    KPI = "kpi"          # single metric
    LIST = "list"        # list of entries
    CHART = "chart"      # mini chart
    STATUS = "status"    # status indicator (ok/warn/error)
    MARKDOWN = "markdown"  # safely rendered Markdown text


@dataclass
class WidgetData:
    """Data that a widget returns to the frontend."""

    kind: WidgetKind
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, locale=None) -> Dict[str, Any]:
        p = dict(self.payload)
        for k in ("label", "detail", "title"):
            if k in p:
                p[k] = resolve_locale(p[k], locale)
        return {"kind": self.kind.value, "payload": p}

    # --- convenience constructors ---------------------------------------- #
    @classmethod
    def kpi(
        cls,
        value: Union[int, float, str],
        label: str,
        *,
        trend: Optional[str] = None,
        icon: Optional[str] = None,
        intent: str = "neutral",  # neutral | positive | negative
    ) -> "WidgetData":
        return cls(
            WidgetKind.KPI,
            {"value": value, "label": label, "trend": trend, "icon": icon, "intent": intent},
        )

    @classmethod
    def list(cls, items: List[Dict[str, Any]], *, empty: Optional[str] = None) -> "WidgetData":
        return cls(WidgetKind.LIST, {"items": items, "empty": empty})

    @classmethod
    def chart(
        cls,
        series: List[Dict[str, Any]],
        *,
        chart_type: str = "line",  # line | bar | area | doughnut
        labels: Optional[List[str]] = None,
    ) -> "WidgetData":
        return cls(WidgetKind.CHART, {"type": chart_type, "labels": labels, "series": series})

    @classmethod
    def status(cls, state: str, label: str, *, detail: Optional[str] = None) -> "WidgetData":
        return cls(WidgetKind.STATUS, {"state": state, "label": label, "detail": detail})

    @classmethod
    def markdown(cls, text: str) -> "WidgetData":
        return cls(WidgetKind.MARKDOWN, {"text": text})


# --------------------------------------------------------------------------- #
# Panels (contextual forms embedded into existing pages)
# --------------------------------------------------------------------------- #
class FieldType(str, Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    NUMBER = "number"
    SWITCH = "switch"
    SELECT = "select"
    MULTISELECT = "multiselect"
    CHANNEL = "channel"
    ROLE = "role"
    USER = "user"
    COLOR = "color"


@dataclass
class Field:
    key: str
    type: FieldType
    label: str
    value: Any = None
    description: Optional[str] = None
    required: bool = False
    options: Optional[List[Dict[str, Any]]] = None  # for SELECT/MULTISELECT
    min: Optional[float] = None
    max: Optional[float] = None
    max_length: Optional[int] = None
    placeholder: Optional[str] = None
    # Optional variable buttons for TEXTAREA: [{"token": "{member}", "desc": "Mitglied"}]
    variables: Optional[List[Dict[str, Any]]] = None
    # For SELECT: changing the value immediately triggers a save + reload of the panel
    # (e.g. switch profile -> fields reload).
    reload_on_change: bool = False

    def to_dict(self, locale=None) -> Dict[str, Any]:
        # Resolve any localizable (L(...) dict) labels to a plain string for the
        # requested locale; resolve_locale leaves plain strings untouched.
        opts = self.options
        if opts:
            opts = [
                ({**o, "label": resolve_locale(o.get("label"), locale)} if isinstance(o, dict) and "label" in o else o)
                for o in opts
            ]
        vars_ = self.variables
        if vars_:
            vars_ = [
                ({**v, "desc": resolve_locale(v.get("desc"), locale)} if isinstance(v, dict) and "desc" in v else v)
                for v in vars_
            ]
        d = {
            "key": self.key,
            "type": self.type.value,
            "label": resolve_locale(self.label, locale),
            "value": self.value,
            "description": resolve_locale(self.description, locale),
            "required": self.required,
            "options": opts,
            "min": self.min,
            "max": self.max,
            "max_length": self.max_length,
            "placeholder": resolve_locale(self.placeholder, locale),
            "variables": vars_,
            "reload_on_change": self.reload_on_change or None,
        }
        return {k: v for k, v in d.items() if v is not None}

    # --- convenience builders -------------------------------------------- #
    @classmethod
    def text(cls, key, label, *, value="", **kw):
        return cls(key, FieldType.TEXT, label, value, **kw)

    @classmethod
    def textarea(cls, key, label, *, value="", **kw):
        return cls(key, FieldType.TEXTAREA, label, value, **kw)

    @classmethod
    def number(cls, key, label, *, value=0, **kw):
        return cls(key, FieldType.NUMBER, label, value, **kw)

    @classmethod
    def switch(cls, key, label, *, value=False, **kw):
        return cls(key, FieldType.SWITCH, label, value, **kw)

    @classmethod
    def select(cls, key, label, options, *, value=None, **kw):
        return cls(key, FieldType.SELECT, label, value, options=options, **kw)

    @classmethod
    def multiselect(cls, key, label, options, *, value=None, **kw):
        return cls(key, FieldType.MULTISELECT, label, value or [], options=options, **kw)

    @classmethod
    def channel(cls, key, label, *, value=None, **kw):
        return cls(key, FieldType.CHANNEL, label, value, **kw)

    @classmethod
    def role(cls, key, label, *, value=None, **kw):
        return cls(key, FieldType.ROLE, label, value, **kw)


@dataclass
class PanelSchema:
    fields: List[Field] = field(default_factory=list)
    description: Optional[str] = None
    submit_label: Optional[str] = None

    def to_dict(self, locale=None) -> Dict[str, Any]:
        return {
            "fields": [f.to_dict(locale) for f in self.fields],
            "description": resolve_locale(self.description, locale),
            "submit_label": resolve_locale(self.submit_label, locale),
        }


@dataclass
class SubmitResult:
    success: bool
    message: Optional[str] = None
    errors: Optional[Dict[str, str]] = None  # field-specific errors
    # If True, the frontend reloads the OTHER tabs of this module too (e.g. after
    # switching the active profile, so the edit panel and list reflect the change).
    reload: bool = False

    def to_dict(self, locale=None) -> Dict[str, Any]:
        return {
            "success": self.success,
            "message": resolve_locale(self.message, locale),
            "errors": self.errors,
            "reload": self.reload,
        }

    @classmethod
    def ok(cls, message: Optional[str] = None, *, reload: bool = False) -> "SubmitResult":
        return cls(True, message, reload=reload)

    @classmethod
    def fail(cls, message: str, errors: Optional[Dict[str, str]] = None) -> "SubmitResult":
        return cls(False, message, errors)


# --------------------------------------------------------------------------- #
# Pages (full standalone view - optional, component-tree schema)
# --------------------------------------------------------------------------- #
@dataclass
class Component:
    """A declarative UI building block for pages (no raw HTML)."""

    type: str  # heading | text | table | chart | panel_ref | divider | grid
    props: Dict[str, Any] = field(default_factory=dict)
    children: List["Component"] = field(default_factory=list)

    def to_dict(self, locale=None) -> Dict[str, Any]:
        props = dict(self.props)
        for k in ("title", "text", "label", "heading"):
            if k in props:
                props[k] = resolve_locale(props[k], locale)
        return {
            "type": self.type,
            "props": props,
            "children": [c.to_dict(locale) for c in self.children],
        }

    # --- convenience constructors (ergonomic page building) --------------- #
    @classmethod
    def heading(cls, text: "LocalizedStr", *, level: int = 2) -> "Component":
        return cls("heading", {"text": text, "level": level})

    @classmethod
    def text(cls, text: "LocalizedStr") -> "Component":
        return cls("text", {"text": text})

    @classmethod
    def divider(cls) -> "Component":
        return cls("divider", {})

    @classmethod
    def chart(
        cls,
        *,
        labels: List[str],
        series: List[Dict[str, Any]],
        chart_type: str = "line",
        title: Optional["LocalizedStr"] = None,
        height: Optional[int] = None,
    ) -> "Component":
        """A chart block. ``series`` = ``[{"label": str, "data": [num|None]}]``."""
        return cls("chart", {"title": title, "type": chart_type,
                             "labels": labels, "series": series, "height": height})

    @classmethod
    def table(cls, *, columns: List[Dict[str, Any]], rows: List[Dict[str, Any]],
              title: Optional["LocalizedStr"] = None) -> "Component":
        return cls("table", {"title": title, "columns": columns, "rows": rows})

    @classmethod
    def grid(cls, children: List["Component"], *, cols: int = 2) -> "Component":
        return cls("grid", {"cols": cols}, list(children))


@dataclass
class Control:
    """A server-driven page control. Its current value is sent back to the page
    handler as ``ctx.params[<id>]`` so the handler can return matching data
    (e.g. a region/type dropdown). Purely declarative - no client-side logic."""

    id: str
    label: "LocalizedStr"
    type: str = "select"  # select (extensible)
    options: List[Dict[str, Any]] = field(default_factory=list)  # [{value,label}]
    value: Optional[str] = None  # current / default value

    def to_dict(self, locale=None) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": resolve_locale(self.label, locale),
            "type": self.type,
            "value": self.value,
            "options": [
                {
                    "value": o.get("value"),
                    "label": resolve_locale(o.get("label", o.get("value")), locale),
                }
                for o in self.options
            ],
        }

    @classmethod
    def select(cls, id: str, label: "LocalizedStr", options: List[Dict[str, Any]],
               *, value: Optional[str] = None) -> "Control":
        return cls(id=id, label=label, type="select", options=options, value=value)


@dataclass
class PageSchema:
    components: List[Component] = field(default_factory=list)
    # Optional server-driven controls (e.g. dropdowns). Their values are posted
    # back and arrive in the handler as ``ctx.params``; the handler returns a new
    # PageSchema for the selection.
    controls: List["Control"] = field(default_factory=list)

    def to_dict(self, locale=None) -> Dict[str, Any]:
        return {
            "components": [c.to_dict(locale) for c in self.components],
            "controls": [
                (c.to_dict(locale) if hasattr(c, "to_dict") else c) for c in self.controls
            ],
        }
