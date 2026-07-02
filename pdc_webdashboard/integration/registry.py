"""Central collection point for all dashboard contributions of the registered cogs."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .decorators import iter_contributions

log = logging.getLogger("red.pdc.pdc_webdashboard.registry")


@dataclass
class Contribution:
    cog_name: str
    cog: Any
    kind: str            # widget | panel | page
    identifier: str
    meta: Any            # _ContributionMeta
    handler: Callable    # bound method (returns data/schema/rows)
    submit: Optional[Callable] = None  # panels only
    delete: Optional[Callable] = None  # lists only
    edit: Optional[Callable] = None        # lists only (save)
    edit_form: Optional[Callable] = None   # lists only (form)

    @property
    def key(self) -> str:
        return f"{self.cog_name}:{self.identifier}"

    def manifest(self, locale: Optional[str] = None) -> Dict[str, Any]:
        m = self.meta.manifest(locale)
        m["cog"] = self.cog_name
        m["key"] = self.key
        if self.kind == "list":
            m["deletable"] = self.delete is not None
            m["editable"] = self.edit is not None and self.edit_form is not None
        return m


@dataclass
class Registry:
    _contribs: Dict[str, Contribution] = field(default_factory=dict)

    # --- registration ----------------------------------------------------- #
    def register_cog(self, cog: Any) -> int:
        """Scans a cog for decorated methods and registers them."""
        cog_name = type(cog).__name__
        count = 0
        for _attr, meta, bound in iter_contributions(cog):
            submit = None
            if meta.kind == "panel" and meta.submit_handler is not None:
                # resolve the bound submit handler on the cog
                submit = getattr(cog, meta.submit_handler.__name__, None)
            delete = edit = edit_form = None
            if meta.kind == "list":
                if meta.delete_handler is not None:
                    delete = getattr(cog, meta.delete_handler.__name__, None)
                if meta.edit_handler is not None:
                    edit = getattr(cog, meta.edit_handler.__name__, None)
                if meta.edit_form_handler is not None:
                    edit_form = getattr(cog, meta.edit_form_handler.__name__, None)
            contrib = Contribution(
                cog_name=cog_name,
                cog=cog,
                kind=meta.kind,
                identifier=meta.identifier,
                meta=meta,
                handler=bound,
                submit=submit,
                delete=delete,
                edit=edit,
                edit_form=edit_form,
            )
            self._contribs[contrib.key] = contrib
            count += 1
        log.info("Registered: %d contributions from cog %s", count, cog_name)
        return count

    def unregister_cog(self, cog: Any) -> None:
        cog_name = type(cog).__name__
        for key in [k for k, c in self._contribs.items() if c.cog_name == cog_name]:
            del self._contribs[key]
        log.info("Contributions from cog %s removed", cog_name)

    # --- query ------------------------------------------------------------ #
    def get(self, key: str) -> Optional[Contribution]:
        return self._contribs.get(key)

    def all(self) -> List[Contribution]:
        return list(self._contribs.values())

    def by_kind(self, kind: str) -> List[Contribution]:
        return [c for c in self._contribs.values() if c.kind == kind]

    def manifest(self) -> List[Dict[str, Any]]:
        return [c.manifest() for c in self._contribs.values()]
