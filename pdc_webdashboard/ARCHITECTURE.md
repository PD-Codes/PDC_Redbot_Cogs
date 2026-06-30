# PDC Web Dashboard — Architektur

Ein eigenes, vollständig modulares, modernes und sicheres Web-Dashboard-System für
[Red-DiscordBot](https://github.com/cog-creators/red-discordbot). Inspiriert von den
Funktionen des [AAA3A Red-Web-Dashboard](https://github.com/AAA3A-AAA3A/Red-Web-Dashboard),
aber mit einem grundlegend anderen Integrationsmodell: Cogs binden sich **integriert**
(Widgets + kontextuelle Panels) in ein gemeinsames Dashboard ein, statt jeweils eine
eigene, isolierte „Extra-Seite" zu bekommen.

## 1. Überblick

Das System besteht aus zwei getrennt deploybaren Teilen:

```
┌──────────────────────────────┐         ┌─────────────────────────────────┐
│  Red-DiscordBot (Python)     │         │  Web-App (Node / SvelteKit)     │
│                              │         │                                 │
│  ┌────────────────────────┐  │  JSON-  │  ┌───────────────────────────┐  │
│  │ pdc_webdashboard (Cog)     │  │  RPC    │  │ SvelteKit Server (BFF)    │  │
│  │  ├─ RPC-Gateway        │◄─┼─2.0/────┼─►│  ├─ Discord OAuth2        │  │
│  │  │   (aiohttp REST+WS) │  │  WS+REST│  │  ├─ Session/Cookies       │  │
│  │  ├─ Integration-Registry│ │  (Token)│  │  └─ RPC-Client            │  │
│  │  ├─ Core-Provider      │  │         │  └───────────┬───────────────┘  │
│  │  └─ Permission-Mapper  │  │         │              │ HTTP/WS           │
│  └────────────────────────┘  │         │  ┌───────────▼───────────────┐  │
│  ┌────────────────────────┐  │         │  │ SPA (Svelte + Tailwind +  │  │
│  │ Dritt-Cogs             │  │         │  │ shadcn-svelte)            │  │
│  │  (DashboardIntegration)│  │         │  │  ├─ Widget-Grid (Board)   │  │
│  └────────────────────────┘  │         │  │  ├─ Kontext-Panels        │  │
└──────────────────────────────┘         │  │  └─ Live-Logs / Charts    │  │
                                          │  └───────────────────────────┘  │
                                          └─────────────────────────────────┘
```

- **Companion-Cog `pdc_webdashboard`** (in diesem Repo, `PDC_Redbot_Cogs`): läuft im Bot-Prozess
  und stellt ein **RPC-Gateway** bereit (aiohttp, JSON-RPC 2.0 über WebSocket + ein paar
  REST-Endpunkte). Es bündelt Bot-/Guild-Daten, Cog-Verwaltung und – zentral – eine
  **Integrations-Registry**, in der sich andere Cogs mit Widgets, Panels und Seiten
  registrieren.
- **Web-App `PDC_Redbot_Webapp`** (separates Projekt, `W:\_Git\PDC_Redbot_Webapp`):
  SvelteKit-Anwendung. Der SvelteKit-Server fungiert als **Backend-for-Frontend (BFF)**:
  er übernimmt Discord-OAuth2, hält die Session und ist der **einzige** Client, der das
  geteilte Gateway-Secret kennt. Das SPA-Frontend spricht ausschließlich mit dem
  SvelteKit-Server, nie direkt mit dem Bot.

## 2. Warum dieses Modell (vs. AAA3A)

| Aspekt | AAA3A Red-Web-Dashboard | PDC Web Dashboard |
|---|---|---|
| Transport | Reds eingebautes RPC (`--rpc`) + Flask | Eigenes aiohttp-Gateway, JSON-RPC 2.0 über WS, Token-Auth |
| Frontend | Server-rendered (Flask/Jinja, AppSeed-Template) | SvelteKit SPA + Tailwind + shadcn-svelte |
| Cog-Integration | `@dashboard_page` → eigene Seite je Cog | Widgets **und** kontextuelle Panels → integriert in ein gemeinsames Board |
| Live-Daten | Polling | WebSocket (Live-Logs, Stats-Streams) |
| i18n | Englisch (primär) | de-DE + en-US, durchgängig (Cog + Frontend) |

## 3. Integrations-Contract (das Herzstück)

Dritt-Cogs erben von `DashboardIntegration` und dekorieren Methoden. Drei Beitragstypen:

### 3.1 Widget — `@dashboard_widget`
Eine Kachel auf dem zentralen **Dashboard-Board**. Liefert strukturierte Daten
(JSON-Schema-basiert), die das Frontend in eine generische, themenbare Karte rendert
(Zahl/KPI, Liste, Mini-Chart, Status). Kein rohes HTML nötig → sicher by default.

```python
@dashboard_widget(
    identifier="guild_member_count",
    name=_("Mitglieder"),
    size="sm",                 # sm | md | lg
    refresh=30,                # Auto-Refresh in Sekunden (optional)
    permission="guild_admin",  # siehe §5
)
async def member_count(self, ctx: DashboardContext) -> WidgetData:
    guild = ctx.guild
    return WidgetData.kpi(value=guild.member_count, label=_("Mitglieder"),
                          trend="+3", icon="users")
```

### 3.2 Panel — `@dashboard_panel`
Eine **kontextuelle Komponente**, die in eine bestehende Seite eingebettet wird (z. B. in
die Guild-Settings-Ansicht), statt einer eigenen Top-Level-Seite. Definiert ein
**Formular-Schema** (Felder, Typen, Validierung), das das Frontend mit shadcn-svelte
rendert. Speichern läuft über einen `on_submit`-Handler.

```python
@dashboard_panel(
    identifier="welcome",
    name=_("Willkommensnachricht"),
    mount="guild_settings",     # wohin im UI eingebettet wird
    permission="guild_admin",
)
async def welcome_panel(self, ctx: DashboardContext) -> PanelSchema:
    cfg = await self.config.guild(ctx.guild).welcome()
    return PanelSchema(fields=[
        Field.switch("enabled", _("Aktiviert"), value=cfg["enabled"]),
        Field.textarea("message", _("Nachricht"), value=cfg["message"],
                       max_length=2000),
        Field.channel("channel", _("Kanal"), value=cfg["channel"]),
    ])

@welcome_panel.on_submit
async def save_welcome(self, ctx: DashboardContext, data: dict) -> SubmitResult:
    await self.config.guild(ctx.guild).welcome.set(data)
    return SubmitResult.ok(_("Gespeichert."))
```

### 3.3 Page — `@dashboard_page` (optional, Rückwärtskompatibilität)
Für Fälle, in denen ein Cog doch eine vollwertige eigene Ansicht braucht. Liefert ein
**Komponentenbaum-Schema** (kein rohes HTML) — gleiche Sicherheitsgarantien.

### 3.4 Datenfluss
1. Beim Cog-Load registriert sich der Dritt-Cog bei `pdc_webdashboard` über
   `bot.get_cog("WebDashboard").register_third_party(self)`.
2. Das Gateway sammelt alle Beiträge in der **Registry**.
3. Die Web-App fragt `GET /api/manifest` ab → Liste aller Widgets/Panels/Pages
   (Metadaten, Schemas, benötigte Permissions), gefiltert nach den Rechten des
   eingeloggten Users.
4. Beim Rendern ruft das Frontend pro Widget/Panel den zugehörigen RPC-Call auf
   (`widget.data`, `panel.schema`, `panel.submit`).

## 4. RPC-Gateway-Protokoll

- **Transport:** aiohttp. WebSocket unter `/rpc` (JSON-RPC 2.0, bidirektional → Server
  kann pushen: Live-Logs, Stats). REST unter `/api/*` für einfache, cachebare GETs.
- **Bindung:** standardmäßig `127.0.0.1:<port>` (nur localhost). Für Remote-Setups hinter
  einem Reverse-Proxy/Tunnel konfigurierbar.
- **Auth (Gateway ↔ BFF):** geteiltes Secret (`X-Dashboard-Token` Header bzw.
  `connection_init`-Frame beim WS). Konstant-Zeit-Vergleich. Das Secret kennt **nur** der
  SvelteKit-Server.
- **Auth-Kontext (User):** der BFF reicht den verifizierten Discord-User (ID, Guilds) als
  signierten Kontext mit. Das Gateway leitet daraus die effektiven Red-Rechte ab (§5) und
  erzwingt sie serverseitig bei **jedem** Call — das Frontend-Filtering ist nur UX.

### Beispiel-Methoden
| Methode | Typ | Zweck |
|---|---|---|
| `core.botinfo` | RPC | Name, Avatar, Uptime, Latenz, Versionen |
| `core.guilds` | RPC | Guilds des Users (mit Rechte-Flags) |
| `manifest.get` | RPC | Alle sichtbaren Widgets/Panels/Pages |
| `widget.data` | RPC | Daten eines Widgets |
| `panel.schema` / `panel.submit` | RPC | Panel-Formular laden/speichern |
| `cogs.list` / `cogs.install` / `cogs.load` | RPC | Cog-Verwaltung |
| `logs.stream` | WS-Sub | Live-Logs (z. B. Cog-Download/-Install) |
| `stats.subscribe` | WS-Sub | Live-Statistiken für Graphen |

## 5. Sicherheit & Berechtigungen

- **Mehrschichtig:** (1) Gateway-Secret (BFF ↔ Cog), (2) Discord-OAuth2 (User ↔ BFF),
  (3) serverseitige Red-Permission-Erzwingung pro Call.
- **Permission-Stufen** (gemappt auf Red):
  - `bot_owner` → `bot.is_owner(user)`
  - `guild_owner` → `guild.owner_id == user.id`
  - `guild_admin` → Reds Admin-Rolle / `manage_guild`
  - `guild_mod` → Reds Mod-Rolle
  - `guild_member` → Mitglied der Guild
  - `authenticated` → eingeloggt
- **Härtung:** localhost-Bindung per Default, CSRF-Schutz im BFF, `SameSite=Lax`-Cookies,
  HttpOnly-Session, Rate-Limiting am Gateway, Input-Validierung über Schemas, kein rohes
  HTML aus Cogs (nur deklarative Schemas → keine XSS-Fläche), Audit-Log für schreibende
  Aktionen.
- **Keine Geheimnisse im Frontend:** Discord-Client-Secret und Gateway-Token liegen nur
  serverseitig (SvelteKit `$env/static/private`).

## 6. Internationalisierung (i18n)

- **Cog:** Reds `Translator`/`cog_i18n` mit `locales/` (`de-DE.po`, `en-US.po`).
  Übersetzbare Strings in Widget-/Panel-Namen und Meldungen.
- **Frontend:** Sprachpakete `de` + `en` (Messages als JSON), Sprachumschaltung in der UI,
  Persistenz pro User. Vom Cog gelieferte, bereits übersetzte Labels haben Vorrang.

## 7. Verzeichnisstruktur

```
PDC_Redbot_Cogs/pdc_webdashboard/        # Companion-Cog (dieses Repo)
  __init__.py
  info.json
  pdc_webdashboard.py                # Hauptcog (Lifecycle, Config, Commands)
  gateway/                       # aiohttp RPC-Gateway
    __init__.py
    server.py                    # App, Routen, Auth-Middleware
    rpc.py                       # JSON-RPC-2.0-Dispatcher (WS)
    methods.py                   # Core-RPC-Methoden
  integration/                   # Integrations-Contract für Dritt-Cogs
    __init__.py
    base.py                      # DashboardIntegration-Mixin
    decorators.py                # @dashboard_widget/@dashboard_panel/@dashboard_page
    models.py                    # WidgetData, PanelSchema, Field, SubmitResult, ...
    registry.py                  # Sammelstelle für Beiträge
    context.py                   # DashboardContext
  permissions.py                 # Red-Permission-Mapping
  locales/                       # i18n (de-DE, en-US)

PDC_Redbot_Webapp/               # Web-App (separates Projekt)
  src/
    lib/server/                  # RPC-Client, Auth, Session (nur serverseitig)
    lib/components/              # Widget-Grid, Panels, shadcn-svelte
    routes/                      # SPA-Routen + OAuth2-Endpunkte
  ...
```

## 8. Roadmap / Status

- [x] Architektur definiert
- [ ] Companion-Cog: Gateway + Registry + Integration-Contract (in Arbeit)
- [ ] SvelteKit-Web-App: Scaffold, OAuth2, RPC-Client, Widget-Board (in Arbeit)
- [ ] Beispiel-Anbindung eines bestehenden Cogs
- [ ] Cog-Verwaltung mit Live-Logs
- [ ] Charts/Stats-Streaming
- [ ] Härtung & Tests
