# DashboardTemplate — Vorlage zum Migrieren ans PDC Web Dashboard

Dieses Cog ist eine **kommentierte Vorlage**. Es macht selbst nichts Produktives –
kopiere die Muster in deinen echten Cog. Vollständiger Code: `dashboardtemplate.py`.

## In 4 Schritten anbinden

### 1. Drop-in kopieren
Kopiere `pdc_dashboard.py` unverändert in deinen Cog-Ordner. Diese Datei hat **keine
harte Abhängigkeit**: Ist `pdc_webdashboard` nicht installiert, werden die Decorators zu
No-ops und dein Cog lädt normal weiter.

### 2. Importieren
```python
from .pdc_dashboard import (
    dashboard_widget, dashboard_panel,
    WidgetData, PanelSchema, Field, SubmitResult,
    register_dashboard, unregister_dashboard,
)
```

### 3. Beiträge dekorieren (Widget und/oder Panel)
```python
@dashboard_widget("status", "Status", size="sm", refresh=60, permission="guild_member")
async def status_widget(self, ctx):
    return WidgetData.kpi(value=ctx.guild.member_count, label="Mitglieder")

@dashboard_panel("settings", "Einstellungen", mount="guild_settings", permission="guild_admin")
async def settings_panel(self, ctx):
    cfg = await self.config.guild(ctx.guild).all()
    return PanelSchema(fields=[
        Field.switch("enabled", "Aktiv", value=cfg["enabled"]),
        Field.textarea("greeting", "Begrüßung", value=cfg["greeting"],
                       variables=[{"token": "{member}", "desc": "Mitglied"}]),
    ])

@settings_panel.on_submit
async def save_settings(self, ctx, data):
    await self.config.guild(ctx.guild).enabled.set(bool(data["enabled"]))
    await self.config.guild(ctx.guild).greeting.set(str(data["greeting"]))
    return SubmitResult.ok("Gespeichert.")
```

### 4. Bedingt registrieren
```python
async def cog_load(self):
    register_dashboard(self)     # No-op, wenn Dashboard nicht geladen
    # ... deine Logik ...

def cog_unload(self):
    unregister_dashboard(self)
    # ... dein Aufräumen ...
```

Fertig. Guild-Panels erscheinen auf der **Server-Detailseite** (aufklappbar),
globale Panels auf **`/settings` → „Modul-Einstellungen (global)"**.

## Feldtypen (`Field.*`)

| Builder | UI | Wert |
|---|---|---|
| `Field.switch(key, label, value=False)` | Schalter | `bool` |
| `Field.text(key, label, value="")` | Textzeile | `str` |
| `Field.textarea(key, label, value="", max_length=…, variables=[…])` | Mehrzeilig + Variablen-Buttons | `str` |
| `Field.number(key, label, value=0, min=…, max=…)` | Zahl | `int/float` |
| `Field.select(key, label, options, value=…)` | Dropdown | Wert der Option |

`options` ist `[{"value": "...", "label": "..."}]`.

**Kanäle/Rollen** haben (noch) keinen eigenen Picker – liefere sie als `Field.select`
mit Optionen aus `ctx.guild.text_channels` bzw. `ctx.guild.roles` und speichere die ID:
```python
ch_opts = [{"value": "", "label": "—"}] + [{"value": str(c.id), "label": "#"+c.name} for c in ctx.guild.text_channels]
Field.select("log_channel", "Log-Kanal", ch_opts, value=str(cfg["log_channel"] or ""))
# on_submit: await g.log_channel.set(int(v) if v else None)
```

`variables` (nur Textarea) zeigt Buttons, die das Token an der Cursor-Position einfügen.

## Guild- vs. globales Panel

- **Guild** (Standard): `@dashboard_panel(id, name, mount="guild_settings", permission="guild_admin")` – `ctx.guild` ist gesetzt, nutze `self.config.guild(ctx.guild)`.
- **Global** (z. B. API-Keys): `@dashboard_panel(id, name, scope="global", mount="bot_settings", permission="bot_owner")` – `ctx.guild` ist **None**, nutze `self.config.<key>()` bzw. `self.bot.get_shared_api_tokens(...)`.

## Permission-Stufen
`authenticated` · `guild_member` · `guild_mod` · `guild_admin` · `guild_owner` · `bot_owner`
(werden serverseitig aus Reds Rechten abgeleitet und bei jedem Aufruf erzwungen).

## Widget-Typen (`WidgetData.*`)
`kpi(value, label, trend=…, intent="positive|negative|neutral")` ·
`status(state="ok|warn|error", label, detail=…)` ·
`list(items=[{label, value}])` ·
`chart(series=[…], chart_type="line|bar|area|doughnut")` ·
`markdown(text)`

## Parallelbetrieb mit AAA3A
Beide Dashboards können gleichzeitig laufen. AAA3A nutzt eine eigene
`DashboardIntegration`/`@dashboard_page` – die Marker dieses Systems
(`__dashboard_widget__`/`__dashboard_panel__`) kollidieren nicht damit. Du kannst AAA3As
Mixin/Decorators also unverändert behalten und zusätzlich die PDC-Panels ergänzen. Es
muss **keines, eines oder beide** Dashboards geladen sein – `register_dashboard` und die
No-op-Decorators fangen alle Fälle ab.

### Mapping AAA3A → PDC
| AAA3A | PDC |
|---|---|
| `@dashboard_page` liefert HTML/Jinja je Cog | `@dashboard_panel` liefert `PanelSchema(fields=[…])` |
| eigene Seite pro Cog | eingebettetes Panel (guild) bzw. globales Panel (owner) |
| Formular-HTML + manuelles Parsen | `Field.*` + `@<panel>.on_submit(self, ctx, data)` |
| Rechte selbst prüfen | `permission=…` (serverseitig erzwungen) |
