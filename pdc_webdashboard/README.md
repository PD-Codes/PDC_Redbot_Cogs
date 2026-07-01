# WebDashboard (Companion-Cog)

> 🇩🇪 Deutsch zuerst — 🇬🇧 **English version below.**

Eigenes, modulares und sicheres Web-Dashboard-System für **Red-DiscordBot**. Dieser Cog
ist die **Bot-Seite**: er stellt ein RPC-Gateway bereit und sammelt die Beiträge anderer
Cogs (Widgets, Panels, Seiten). Das **Frontend** ist die separate SvelteKit-App
`PDC_Redbot_Webapp`.

- Architektur: [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- Eigene Cogs anbinden: [`INTEGRATION.md`](./INTEGRATION.md) und das Vorlage-Cog
  `dashboardtemplate` (kommentiertes Beispiel + Migrations-README).

## Was es kann

- Zentrales **Widget-Board** und **kontextuelle Panels** je Server (statt isolierter
  Extra-Seiten je Cog).
- **Öffentliche Befehlsübersicht** (ohne Login), inkl. Slash- und Prefix-Commands.
- **Server-Verwaltung:** Übersicht, Bot-Einstellungen pro Gilde (Prefixe, Nickname,
  Admin-/Mod-Rollen, Embeds).
- **Cog-Verwaltung (Owner):** Laden/Entladen, Downloader (Repos & Cogs), Slash-Sync und
  Aktivieren/Deaktivieren von App-Commands.
- **Globale Einstellungen, Branding, Custom Pages (WYSIWYG)**.
- **Discord-OAuth2-Login**, Rechte serverseitig aus Reds Rechtesystem abgeleitet.
- **i18n:** Deutsch & Englisch (Cog-Strings via Red-Translator, Web-App via DE/EN-Schalter).
- Läuft **parallel** zum AAA3A-Dashboard; Cogs binden sich **optional** an (nur wenn der
  WebDashboard-Cog geladen ist).

## Installation

```
[p]repo add pdc-cogs https://github.com/PD-Codes/PDC_Redbot_Cogs
[p]cog install pdc-cogs pdc_webdashboard
[p]load pdc_webdashboard
```

## Einrichtung & Verbindung mit der Web-App

### 1. Gateway prüfen / binden
Beim Laden startet das Gateway automatisch auf `127.0.0.1:6970` (nur localhost).
```
[p]pdcdashboard status
[p]pdcdashboard bind 127.0.0.1 6970   # Adresse/Port ändern (Neustart nötig)
[p]pdcdashboard stop  /  start
```
> **Sicherheit:** Lass das Gateway auf `127.0.0.1` und mach es nur über einen
> Reverse-Proxy/Tunnel (TLS) erreichbar – nicht direkt an `0.0.0.0` binden.

### 2. Token abrufen
```
[p]pdcdashboard token   # sendet das Token per DM (nur der BFF/SvelteKit-Server kennt es)
[p]pdcdashboard regen   # neues Token erzeugen (Web-App muss aktualisiert werden)
```

### 3. Web-App `.env`
```dotenv
GATEWAY_URL=http://127.0.0.1:6970
GATEWAY_TOKEN=<Token aus [p]pdcdashboard token>
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
DISCORD_REDIRECT_URI=https://deine-domain/auth/callback
SESSION_SECRET=<openssl rand -hex 32>
```

## Owner-Commands

| Command | Funktion |
|---|---|
| `[p]pdcdashboard status` | Status, Adresse, Anzahl registrierter Beiträge |
| `[p]pdcdashboard start` / `stop` | Gateway starten/stoppen |
| `[p]pdcdashboard bind <host> <port>` | Adresse setzen (Neustart nötig) |
| `[p]pdcdashboard token` | Token per DM |
| `[p]pdcdashboard regen` | Neues Token + Neustart |

> Eigener Befehlsname `pdcdashboard`, damit er **parallel zu AAA3As `[p]dashboard`** läuft.

## Wie Cogs sich anbinden (Kurzfassung)

Cogs kopieren `pdc_dashboard.py` (Drop-in), dekorieren Methoden mit `@dashboard_widget` /
`@dashboard_panel` und rufen in `cog_load` `register_dashboard(self)` auf. Ohne geladenes
WebDashboard passiert nichts; parallel zu AAA3A nutzbar. Vollständig: `INTEGRATION.md` und
das Cog `dashboardtemplate`.

## Konnektivität & Troubleshooting

- **Web-App erreicht das Gateway nicht?** `[p]pdcdashboard status` prüfen; `GATEWAY_URL`/
  Port und `GATEWAY_TOKEN` müssen passen.
- **Web-App in Docker, Bot auf dem Host:** Gateway lauscht auf `127.0.0.1` und ist aus dem
  Container nicht erreichbar → entweder beide ins selbe Docker-Netz oder bewusst
  `0.0.0.0` binden (+ Firewall) und `GATEWAY_URL=http://host.docker.internal:6970`.
- **Keine Server/Widgets sichtbar trotz Berechtigung?** Aktiviere den **Server Members
  Intent** im Discord Developer Portal – sonst kennt Red die Mitglieder nicht und die
  Rechteauflösung liefert nur `authenticated`.
- **Health-Check:** `GET http://127.0.0.1:6970/api/health` liefert ohne Token
  `{"status":"ok"}`. Alle anderen Endpunkte verlangen das Token.

## Sicherheit (Kurzfassung)

- Gateway nur localhost, Token-Auth (konstant-Zeit) zwischen BFF und Cog.
- Discord-OAuth2 im BFF; Berechtigungen werden serverseitig erzwungen.
- Cogs liefern nur deklarative Schemas (kein rohes HTML) → keine XSS-Fläche.
- Schreibende Aktionen werden auditiert (Log).

---

# 🇬🇧 WebDashboard (companion cog) — English

A custom, modular and secure web dashboard system for **Red-DiscordBot**. This cog is the
**bot side**: it exposes an RPC gateway and collects contributions from other cogs
(widgets, panels, pages). The **frontend** is the separate SvelteKit app
`PDC_Redbot_Webapp`.

- Architecture: [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- Integrate your own cogs: [`INTEGRATION.md`](./INTEGRATION.md) and the template cog
  `dashboardtemplate` (commented example + migration README).

## Features

- Central **widget board** plus **contextual panels** per server (instead of isolated
  extra pages per cog).
- **Public command overview** (no login), incl. slash and prefix commands.
- **Server management:** overview, per-guild bot settings (prefixes, nickname, admin/mod
  roles, embeds).
- **Cog management (owner):** load/unload, Downloader (repos & cogs), slash sync and
  enabling/disabling app commands.
- **Global settings, branding, custom pages (WYSIWYG)**.
- **Discord OAuth2 login**, permissions derived server-side from Red's permission system.
- **i18n:** German & English (cog strings via Red translator, web app via a DE/EN switch).
- Runs **alongside** the AAA3A dashboard; cogs integrate **optionally** (only when the
  WebDashboard cog is loaded).

## Install

```
[p]repo add pdc-cogs https://github.com/PD-Codes/PDC_Redbot_Cogs
[p]cog install pdc-cogs pdc_webdashboard
[p]load pdc_webdashboard
```

## Setup & connecting the web app

### 1. Check / bind the gateway
On load the gateway starts on `127.0.0.1:6970` (localhost only).
```
[p]pdcdashboard status
[p]pdcdashboard bind 127.0.0.1 6970   # change address/port (restart needed)
[p]pdcdashboard stop  /  start
```
> **Security:** keep the gateway on `127.0.0.1` and expose it only via a reverse
> proxy/tunnel (TLS) — don't bind directly to `0.0.0.0`.

### 2. Get the token
```
[p]pdcdashboard token   # DMs the token (only the BFF/SvelteKit server should know it)
[p]pdcdashboard regen   # generate a new token (update the web app afterwards)
```

### 3. Web app `.env`
```dotenv
GATEWAY_URL=http://127.0.0.1:6970
GATEWAY_TOKEN=<token from [p]pdcdashboard token>
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
DISCORD_REDIRECT_URI=https://your-domain/auth/callback
SESSION_SECRET=<openssl rand -hex 32>
```

## Owner commands

| Command | Purpose |
|---|---|
| `[p]pdcdashboard status` | status, address, number of registered contributions |
| `[p]pdcdashboard start` / `stop` | start/stop the gateway |
| `[p]pdcdashboard bind <host> <port>` | set the address (restart needed) |
| `[p]pdcdashboard token` | token via DM |
| `[p]pdcdashboard regen` | new token + restart |

> Custom command name `pdcdashboard` so it runs **alongside AAA3A's `[p]dashboard`**.

## How cogs integrate (short)

Cogs copy `pdc_dashboard.py` (drop-in), decorate methods with `@dashboard_widget` /
`@dashboard_panel`, and call `register_dashboard(self)` in `cog_load`. Nothing happens
without the WebDashboard cog loaded; works alongside AAA3A. Full guide: `INTEGRATION.md`
and the `dashboardtemplate` cog.

## Connectivity & troubleshooting

- **Web app can't reach the gateway?** Check `[p]pdcdashboard status`; `GATEWAY_URL`/port
  and `GATEWAY_TOKEN` must match.
- **Web app in Docker, bot on the host:** the gateway listens on `127.0.0.1` and isn't
  reachable from the container → either put both on the same Docker network, or bind to
  `0.0.0.0` on purpose (+ firewall) and use `GATEWAY_URL=http://host.docker.internal:6970`.
- **No servers/widgets despite permission?** Enable the **Server Members Intent** in the
  Discord Developer Portal, otherwise Red doesn't know the members and permission
  resolution only yields `authenticated`.
- **Health check:** `GET http://127.0.0.1:6970/api/health` returns `{"status":"ok"}`
  without a token. All other endpoints require the token.

## Security (short)

- Gateway is localhost-only, token auth (constant-time) between BFF and cog.
- Discord OAuth2 in the BFF; permissions are enforced server-side.
- Cogs return only declarative schemas (no raw HTML) → no XSS surface.
- Write actions are audited (log).
