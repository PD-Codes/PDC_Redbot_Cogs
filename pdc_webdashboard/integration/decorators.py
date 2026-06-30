"""Decorators that third-party cogs use to mark their dashboard contributions.

See ARCHITECTURE.md §3 for examples. The decorators only attach metadata to the
method; collecting them is done by the registry when the cog is registered.
"""
from __future__ import annotations

import functools
from typing import Any, Callable, List, Optional

WIDGET_ATTR = "__dashboard_widget__"
PANEL_ATTR = "__dashboard_panel__"
PAGE_ATTR = "__dashboard_page__"
LIST_ATTR = "__dashboard_list__"


class _ContributionMeta:
    """Shared metadata base for all contribution types."""

    def __init__(
        self,
        kind: str,
        identifier: str,
        name: str,
        *,
        permission: str = "authenticated",
        description: Optional[str] = None,
        icon: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        self.kind = kind
        self.identifier = identifier
        self.name = name
        self.permission = permission
        self.description = description
        self.icon = icon
        self.extra = extra or {}
        # set for panels
        self.submit_handler: Optional[Callable] = None
        # set for lists
        self.delete_handler: Optional[Callable] = None
        self.edit_handler: Optional[Callable] = None
        self.edit_form_handler: Optional[Callable] = None

    def manifest(self, locale: Optional[str] = None) -> dict:
        # name/description may be localized (str or {locale: str}); resolve against
        # the web UI language so the tab title + module description follow the toggle.
        from .models import resolve_locale
        return {
            "kind": self.kind,
            "identifier": self.identifier,
            "name": resolve_locale(self.name, locale),
            "permission": self.permission,
            "description": resolve_locale(self.description, locale) if self.description is not None else None,
            "icon": self.icon,
            **self.extra,
        }


def dashboard_widget(
    identifier: str,
    name: str,
    *,
    size: str = "md",            # sm | md | lg
    refresh: Optional[int] = None,  # auto-refresh in seconds
    permission: str = "authenticated",
    scope: str = "guild",        # guild | global
    description: Optional[str] = None,
    icon: Optional[str] = None,
) -> Callable:
    """Registers a method as a board widget."""

    def decorator(func: Callable) -> Callable:
        meta = _ContributionMeta(
            "widget", identifier, name,
            permission=permission, description=description, icon=icon,
            extra={"size": size, "refresh": refresh, "scope": scope},
        )
        setattr(func, WIDGET_ATTR, meta)
        return func

    return decorator


def dashboard_panel(
    identifier: str,
    name: str,
    *,
    mount: str = "guild_settings",  # embedding location in the UI
    permission: str = "guild_admin",
    scope: str = "guild",
    description: Optional[str] = None,
    icon: Optional[str] = None,
    order: int = 100,  # tab order within the module (smaller = further left)
) -> Callable:
    """Registers a method as a contextual panel (form).

    The associated save handler is set via ``@<panel>.on_submit``.
    """

    def decorator(func: Callable) -> Callable:
        meta = _ContributionMeta(
            "panel", identifier, name,
            permission=permission, description=description, icon=icon,
            extra={"mount": mount, "scope": scope, "order": order},
        )
        setattr(func, PANEL_ATTR, meta)

        def on_submit(submit_func: Callable) -> Callable:
            meta.submit_handler = submit_func
            setattr(submit_func, PANEL_ATTR + "_submit", identifier)
            return submit_func

        func.on_submit = on_submit  # type: ignore[attr-defined]
        return func

    return decorator


def dashboard_list(
    identifier: str,
    name: str,
    *,
    columns: Optional[List[dict]] = None,  # [{"key": "...", "label": "..."}]
    mount: str = "guild_settings",
    permission: str = "guild_admin",
    scope: str = "guild",
    description: Optional[str] = None,
    icon: Optional[str] = None,
    order: int = 100,  # tab order within the module (smaller = further left)
) -> Callable:
    """Registers a method as a managed list (table with delete).

    The method returns rows ``[{"id": "...", "cells": {col_key: value, ...}}]``.
    The delete handler is set via ``@<list>.on_delete`` and receives ``(ctx, id)``.
    """

    def decorator(func: Callable) -> Callable:
        meta = _ContributionMeta(
            "list", identifier, name,
            permission=permission, description=description, icon=icon,
            extra={"mount": mount, "scope": scope, "columns": columns or [], "order": order},
        )
        setattr(func, LIST_ATTR, meta)

        def on_delete(delete_func: Callable) -> Callable:
            meta.delete_handler = delete_func
            return delete_func

        def edit_form(form_func: Callable) -> Callable:
            """Returns the edit form for a row: ``(ctx, id) -> PanelSchema``."""
            meta.edit_form_handler = form_func
            return form_func

        def on_edit(edit_func: Callable) -> Callable:
            """Saves the changes of a row: ``(ctx, id, data) -> SubmitResult``."""
            meta.edit_handler = edit_func
            return edit_func

        func.on_delete = on_delete  # type: ignore[attr-defined]
        func.edit_form = edit_form  # type: ignore[attr-defined]
        func.on_edit = on_edit  # type: ignore[attr-defined]
        return func

    return decorator


def dashboard_page(
    identifier: str,
    name: str,
    *,
    permission: str = "authenticated",
    scope: str = "guild",
    description: Optional[str] = None,
    icon: Optional[str] = None,
    nav: bool = True,  # show in the side navigation?
) -> Callable:
    """Registers a method as a full standalone page (component-tree schema)."""

    def decorator(func: Callable) -> Callable:
        meta = _ContributionMeta(
            "page", identifier, name,
            permission=permission, description=description, icon=icon,
            extra={"scope": scope, "nav": nav},
        )
        setattr(func, PAGE_ATTR, meta)
        return func

    return decorator


def iter_contributions(cog: Any) -> List[tuple]:
    """Returns ``(attr, meta, bound_method)`` for all decorated methods of a cog."""
    found = []
    for attr_name in dir(cog):
        try:
            member = getattr(cog, attr_name)
        except Exception:
            continue
        if not callable(member):
            continue
        func = getattr(member, "__func__", member)
        for marker in (WIDGET_ATTR, PANEL_ATTR, PAGE_ATTR, LIST_ATTR):
            meta = getattr(func, marker, None)
            if meta is not None:
                found.append((attr_name, meta, member))
                break
    return found
