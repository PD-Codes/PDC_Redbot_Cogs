# Eigene Cogs ins PDC Web Dashboard integrieren

Diese Anleitung zeigt, wie ein beliebiger Red-Cog Inhalte (Widgets, Panels, Seiten)
zum PDC Web Dashboard beiträgt – **optional** (nur wenn das Dashboard geladen ist) und
**parallel zu AAA3As Dashboard** nutzbar.

## Grundprinzipien

1. **Keine harte Abhängigkeit.** Der Cog funktioniert auch ohne `pdc_webdashboard`. Die
   Decorators werden dann zu No-ops.
2. **Opt-in zur Laufzeit.** Die Integration passiert nur, wenn der `WebDashboard`-Cog
   tatsächlich geladen ist (`bot.get_cog("WebDashboard")`).
3. **Koexistenz mit AAA3A.** Marker und Klassennamen kollidieren nicht; beide Dashboards
   dürfen gleichzeitig laufen.
4. **Nur deklarative Schemas, kein rohes HTML.** Dadurch keine XSS-Fläche.

## Schritt 1 – Drop-in-Helfer einbinden

Du hast zwei Möglichkeiten:

**A) Direkt importieren** (wenn `pdc_webdashboard` als Cog im selben Bot installiert ist):

```python
from pdc_webdashboard.integration import (
    dashboard_widget, dashboard_panel, dashboard_page,
    WidgetData, PanelSchema, PageSchema, Field, SubmitResult,
    register_dashboard, unregister_dashboard,
)
```

**B) Komplett entkoppelt** (empfohlen, wenn der Cog auch ohne `pdc_webdashboard` lauffähig
sein soll): Kopiere `pdc_webdashboard/integration/dropin.py` als `pdc_dashboard.py` in deinen
Cog und importiere von dort:

```python
from .pdc_dashboard import (
    dashboard_widget, dashboard_panel, WidgetData, PanelSchema, Field, SubmitResult,
    register_dashboard, unregister_dashboard, DASHBOARD_AVAILABLE,
)
```

## Schritt 2 – Beiträge dekorieren

```python
class MyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        self.config.register_guild(welcome={"enabled": False, "message": "", "channel": None})

    # --- Widget: Kachel auf dem zentralen Board -------------------------- #
    @dashboard_widget("member_count", "Mitglieder", size="sm", refresh=60,
                      permission="guild_member")
    async def member_count(self, ctx):
        return WidgetData.kpi(value=ctx.guild.member_count, label="Mitglieder", icon="users")

    # --- Panel: kontextuelles Formular (eingebettet, keine Extra-Seite) -- #
    @dashboard_panel("welcome", "Willkommensnachricht", mount="guild_settings",
                     permission="guild_admin")
    async def welcome_panel(self, ctx):
        cfg = await self.config.guild(ctx.guild).welcome()
        return PanelSchema(fields=[
            Field.switch("enabled", "Aktiviert", value=cfg["enabled"]),
            Field.textarea("message", "Nachricht", value=cfg["message"], max_length=2000),
            Field.channel("channel", "Kanal", value=cfg["channel"]),
        ])

    @welcome_panel.on_submit
    async def save_welcome(self, ctx, data):
        await self.config.guild(ctx.guild).welcome.set(data)
        return SubmitResult.ok("Gespeichert.")
```

`ctx` ist ein `DashboardContext` mit `bot`, `user`, `guild`, `member`, `locale`. Der
Zugriff ist beim Aufruf bereits **serverseitig autorisiert** (gemäß `permission`).

### Permission-Stufen

`authenticated` · `guild_member` · `guild_mod` · `guild_admin` · `guild_owner` · `bot_owner`

## Schritt 3 – Bedingt registrieren (das „Extra")

```python
    async def cog_load(self):
        # ... deine bestehende Logik ...
        register_dashboard(self)     # integriert NUR, wenn WebDashboard geladen ist

    def cog_unload(self):
        unregister_dashboard(self)   # sicher, auch wenn nichts registriert war
        # ... deine bestehende Logik ...
```

Das war's. Ist `WebDashboard` nicht geladen, passiert schlicht nichts.

> Hinweis: Selbst wenn du `register_dashboard` weglässt, erkennt der `WebDashboard`-Cog
> beim Laden alle bereits geladenen Cogs mit dekorierten Methoden automatisch. Der
> explizite Aufruf deckt zusätzlich den Fall ab, dass dein Cog **nach** dem Dashboard
> geladen wird.

## Parallelbetrieb mit AAA3A

Du kannst beide Dashboards gleichzeitig bedienen. AAA3As Integration nutzt eine eigene
`DashboardIntegration`-Klasse und `@dashboard_page`. Um Namenskollisionen zu vermeiden,
importiere die PDC-Variante bei Bedarf unter Alias:

```python
# AAA3A
from dashboard.rpc.thirdparties import dashboard_page as aaa3a_page  # Beispiel
# PDC
from pdc_webdashboard.integration import dashboard_widget, dashboard_panel
```

- Die Marker-Attribute sind verschieden (`__dashboard_widget__`/`__dashboard_panel__`/
  `__dashboard_page__` bei PDC), daher stören sich die Scanner nicht.
- PDC registriert über `register_dashboard(self)` / den Auto-Scan und ist damit
  unabhängig von AAA3As Mixin-Vererbung. Du kannst AAA3As Mixin also normal weiter erben.
- Keines der beiden Systeme schaltet das andere ab.

## Eigene Seiten (`@dashboard_page`)

Ein Cog kann **vollständige Unterseiten** im Dashboard registrieren – als
Komponentenbaum (`Component`), ohne rohes HTML. Wo die Seite erscheint, steuert
`scope`:

- `scope="global"` → eigener Menüpunkt unter **„Module (Cog) Seiten"** (Hauptmenü,
  server-unabhängig, z. B. WoW-Token).
- `scope="guild"`  → als Button auf der **Server-Detailseite** (neben
  „Bot-Einstellungen"); der Handler bekommt dann `ctx.guild`.

```python
from .pdc_dashboard import (
    dashboard_page, PageSchema, Component, Control, L, tr,
)

class MyCog(commands.Cog):

    @dashboard_page(
        "overview",                     # identifier (Key = "<CogName>:overview")
        L("Übersicht", "Overview"),     # Menü-/Seitentitel (folgt der UI-Sprache)
        scope="global",                 # global | guild
        permission="authenticated",     # authenticated | guild_member | guild_admin | bot_owner
        icon="chart",                   # optional
        nav=True,                       # im Menü anzeigen?
    )
    async def my_page(self, ctx):
        # Aktuelle Auswahl der Controls kommt in ctx.params an (server-driven).
        region = (ctx.params or {}).get("region") or "eu"

        controls = [
            Control.select(
                "region", L("Region", "Region"),
                [{"value": "eu", "label": "EU"}, {"value": "us", "label": "US"}],
                value=region,
            )
        ]
        comps = [
            Component.heading(L(f"Region {region.upper()}", f"Region {region.upper()}")),
            Component.text(tr(ctx, "Beschreibung …", "Description …")),
            Component.chart(
                labels=["Mo", "Di", "Mi"],
                series=[{"label": "Wert", "data": [1, 3, 2]}],
                title="Trend", height=280,
            ),
        ]
        return PageSchema(components=comps, controls=controls)
```

### Server-driven Controls (Dropdowns)

`PageSchema(controls=[...])` deklariert Steuerelemente. Ändert der Nutzer eine
Auswahl, ruft die Web-App **denselben Handler erneut** auf – die gewählten Werte
stehen in `ctx.params` (z. B. `ctx.params["region"]`). Der Handler gibt einfach ein
neues `PageSchema` für die Auswahl zurück. Rein deklarativ, kein Client-JS, keine
XSS-Fläche.

`Control.select(id, label, options, *, value=None)` – `options` ist eine Liste aus
`{"value": ..., "label": ...}`; Labels dürfen `L("de", "en")` sein.

### Komponenten (`Component`)

Bequeme Konstruktoren: `Component.heading(text, level=2)`, `Component.text(text)`,
`Component.divider()`, `Component.chart(labels=, series=, chart_type=..., title=, height=)`,
`Component.table(columns=, rows=, title=)`, `Component.grid(children, cols=2)`.

`chart_type` unterstützt `"line"`, `"area"`, `"bar"` und `"doughnut"` (bzw. `"donut"`).
Beim Donut wird die **erste** Serie (`series[0].data`) mit `labels` als Segmente gezeichnet.

`series` = `[{"label": str, "data": [zahl|None]}]`. Texte (`title`/`text`/`label`/
`heading`) dürfen `L("de", "en")` sein und folgen der UI-Sprache.

**Panel einbetten:** `Component("panel_ref", {"key": "<CogName>:<panel_id>", "title": ...})`
rendert ein bestehendes Panel (Formular) direkt in der Seite. Auf guild-scoped Seiten
speichert es in den Kontext-Server.

Siehe `ARCHITECTURE.md §3.3` und das Beispiel im Cog `dashboardtemplate`
(`example_page`).
