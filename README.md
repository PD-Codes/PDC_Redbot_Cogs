## PDC_Redbot_Cogs

This are my Cogs for Redbot. This Cogs will be used for my private Discord and can be used from you too. Please aware that the Cogs are 95% in german!

As you can read from my grammar, you see the reason why :D

![Screenshot: PDC cogs overview](assets/readme-cogs-overview.png)

> 📖 **Full documentation:** [PDC_Redbot_Cogs Wiki](https://github.com/PD-Codes/PDC_Redbot_Cogs/wiki) (English & Deutsch)

> 🎮 **Game-spezifische Cogs** (WoW, Trivia, …) liegen im separaten Repo **[PDC_Redbot_Game_Cogs](https://github.com/PD-Codes/PDC_Redbot_Game_Cogs)**.


## Status Information

| Status | Description |
|---|---|
| Alpha | Alpha Release. Most Commands cannot work |
| Beta | Beta Release. Most Commands should work |
| Info | Not for Production! |
| Release | All Commands should work |
| Stopped | Stopped work on it for different reasons |
| … / On Work | Currently working on it. |

## PDC Web Dashboard (eigenes, modulares Web-Panel)

Neben den AAA3A-kompatiblen Cogs gibt es ein **eigenes** Web-Dashboard. Die Web-App
liegt im separaten Repo **https://github.com/PD-Codes/PDC_Redbot_Webapp**; die Bot-Seite
besteht aus diesen Cogs hier:

| Cog | Zweck |
|---|---|
| `pdc_webdashboard` | Companion-Cog: RPC-Gateway, Auth, Branding, Custom Pages, Audit-Log. Mit `[p]pdcdashboard` verwalten. |
| `pdc_webdashboard_stats` | Sammelt Server-Statistiken (Nachrichten/Voice/Status/Einladungen/Aktivität) für die `/stats`-Seite. |
| `dashboardtemplate` | **Kopier-Vorlage** mit allen Feature-Beispielen (Widget, Panels, Liste mit Anlegen/Bearbeiten/Löschen, globales Panel). |
| `dashboardexample` | Minimal-Beispiel der Integration. |

Eigenen Cog anbinden: siehe `pdc_webdashboard/INTEGRATION.md` + den Drop-in `pdc_dashboard.py`
(1:1 kopierbar, funktioniert auch ohne installiertes Dashboard und parallel zu AAA3A).
Jeder Cog erscheint als **ein Modul mit Tabs** auf der Server-Detailseite.

![Screenshot: PDC Web Dashboard – Cog als Modul mit Tabs](assets/readme-dashboard-module.png)

## About Cogs

| Cog | Status / Version | Description | Commands | Author |
|---|---|---|---|---|
| AdminUtils | Beta 0.2.0 | Commands for Admins and Moderators. | `kick`, `ban`, `timeout`, `purge`, `purgefast`, `messagemove`, `move-memberall`, `move-member`, `copy-channelrole`, `copy-role` | pd-codes |
| eventmessages | Release 0.0.1 | Notifications for join, leave, kick, ban, timeout. | `em-enabled`, `em-channel`, `em-status` | pd-codes |
| GuildTools | Beta 0.1.1 | Some tools for Guilds | `whois`, `setblizzard`, `set-wow-defaults`, `get-absence`, `list-absence`, `add-absence`, `export-userlist`, `export-poll`, `get-readytimes`, `set-readytimes` | pd-codes |
| neko | Release 0.0.1 | Connects to Nekos.best API | `neko`, `neko-cat` | pd-codes |
| nekoapi | Release 0.0.1 | Connects to Nekosapi.com (incl. NSFW ratings) | `nekoapi`, `nekoapi-rating` | pd-codes |
| reactionrole | Release 0.0.1 | Feature-rich Reaction Roles cog with Dashboard support. | `reactionrole-set`, `reactionrole-remove`, `reactionrole-get`, `reactionrole-sync` | pd-codes |
| adminprotocol | Release 0.0.1 | Detailed admin & activity logging into configurable channels (fully web-configured). | *Listeners only (no commands)* | pd-codes |
| channeljoinnotification | Release 0.0.1 | DMs users with a customizable text when they join configured voice channels. | `/join-notification` | pd-codes |
| birthday | Alpha 0.1.0 | Birthday announcements + optional birthday role (self-healing). Opt-in per guild, DE/EN, dashboard panel. | `birthday set/remove/list`, `birthdayset enable/channel/role/hour/message/language` | pd-codes |
| statchannels | Alpha 0.1.0 | Live counter / stat voice channels (`{members}`, `{humans}`, `{bots}`, `{online}`, `{boosts}`, `{roles}`, `{channels}`). Opt-in, DE/EN, dashboard. | `statchannels enable/add/remove/list/language` | pd-codes |
| scheduledmsg | Alpha 0.1.0 | Scheduled & recurring messages (`every`/`daily`/`weekly`/`once`). Opt-in per guild, DE/EN, dashboard. | `schedule add/list/remove/enable/language` | pd-codes |
| eventlog | Alpha 0.1.0 | Server event logging (joins/leaves, msg edit/delete, role/nick/voice) to a channel with per-type toggles. Opt-in, DE/EN, dashboard. | `eventlog enable/channel/event/status/language` | pd-codes |
| giveaway | Alpha 0.1.0 | Button-based giveaways (persistent), embed card, auto draw, reroll. DE/EN, dashboard. | `giveaway start/end/reroll/list`, `giveawayset enable/language` | pd-codes |
| socialfeed | Alpha 0.1.0 | Watch RSS/Atom feeds (YouTube/blogs/Reddit) and post new items. Feeds managed in a dashboard table. DE/EN. | `feeds add/remove/list/enable/language` | pd-codes |
| memegen | Alpha 0.1.0 | Post memes (meme-api.com) on command or on a timer; configurable subreddits. Opt-in, DE/EN, dashboard. | `meme`, `memeset enable/channel/interval/subreddit/language` | pd-codes |
| pdc_webdashboard | Release 1.0.0 | Companion cog: RPC gateway, auth, branding, custom pages, audit log + the cog-integration framework. | `pdcdashboard` (status/start/stop/bind/token/regen) | pd-codes |
| pdc_webdashboard_stats | Release 1.0.0 | Collects server statistics (messages/voice/status/invites/activity, heatmaps, peaks) for the dashboard `/stats` page. | *Listeners only (no commands)* | pd-codes |
| dashboardtemplate | Template | Annotated reference cog for the PDC dashboard integration (incl. the `L`/`tr`/`tr_lang` i18n helpers). | `dashboardtemplate` | pd-codes |
| dashboardexample | Example | Minimal example of dashboard integration (widget + panel). | `dashboardexample` | pd-codes |

> Most cogs support **German & English**: dashboard module texts follow the website language toggle, and each cog has a per-server **language** setting (in its dashboard module) for its Discord output.

## 🌐 Web Dashboard Integration

Several cogs in this repository feature **native integration with AAA3A's Red-Web-Dashboard**! 
Instead of configuring everything strictly via Discord commands, you can manage them seamlessly through your browser:

- **AdminUtils** (Templates & Settings)
- **eventmessages** (Channel routing & Custom Event Texts)
- **reactionrole** (Easily add and map reaction roles visually)

**Modern UI Details:** 
These dashboard pages have been styled with a custom, premium *glassmorphism* aesthetic that provides a highly modern, sleek experience while remaining 100% compatible with the AAA3A Argon Dashboard native layout!

I am Using the Original Dashboard from AAA3A with some customizations for me