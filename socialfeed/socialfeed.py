"""SocialFeed — watch RSS/Atom feeds and post new items to a channel.

Add any feed URL (a YouTube channel feed, a blog, a subreddit's ``.rss`` …) and
the bot posts new entries as embeds (with entry image when available). Feed
URLs are validated on add, duplicates are detected via persisted entry hashes,
and every feed has its own poll interval. Manage feeds from the web dashboard
or via commands. Opt-in per guild, bilingual (DE/EN, default en-US).
"""
from __future__ import annotations

import asyncio
import hashlib
import html as html_mod
import ipaddress
import logging
import re
import socket
import time
import uuid
from typing import List, Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

from .pdc_dashboard import (
    Component,
    Field,
    L,
    PageSchema,
    PanelSchema,
    SubmitResult,
    dashboard_list,
    dashboard_page,
    dashboard_panel,
    register_dashboard,
    tr_lang,
    unregister_dashboard,
)

try:
    import feedparser  # type: ignore
except Exception:  # pragma: no cover
    feedparser = None  # type: ignore

log = logging.getLogger("red.pdc.socialfeed")

DEFAULT_POLL_INTERVAL = 300  # seconds
MIN_POLL_INTERVAL = 60  # seconds
LOOP_STEP = 30  # scheduler resolution in seconds
SEEN_CAP = 400  # persisted de-dup hashes per feed
BURST_CAP = 5  # max entries posted per feed per tick

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(value: str, limit: int = 500) -> str:
    """Strip HTML tags and decode HTML entities from feed-provided text."""
    text = _TAG_RE.sub("", str(value or ""))
    text = html_mod.unescape(text).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


class SocialFeed(commands.Cog):
    """Watch RSS/Atom feeds and post new items."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x50C1A1_FE, force_registration=True)
        # feeds: id -> {url, channel, name, last, interval, seen, last_fetch,
        #               last_status, last_error, error_count, entry_count}
        # (new keys are optional — read with .get() for backward compatibility)
        self.config.register_guild(enabled=False, language="en-US", feeds={})
        self._task: Optional[asyncio.Task] = None
        self._tick_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        register_dashboard(self)
        self._task = asyncio.create_task(self._loop())

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._task:
            self._task.cancel()

    @staticmethod
    def _t(lang: str, de: str, en: str) -> str:
        return de if str(lang).lower().startswith("de") else en

    async def _lang(self, guild) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    # ------------------------------------------------------------------ #
    # Feed parsing helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _entry_key(entry) -> str:
        return str(entry.get("id") or entry.get("link") or entry.get("title") or "")

    @classmethod
    def _entry_hash(cls, entry) -> str:
        return hashlib.sha1(cls._entry_key(entry).encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _resolve_and_check_host(host: str, port: int) -> bool:
        """Blocking helper: resolve a host and verify all IPs are public."""
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError):
            return False
        if not infos:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return False
        return True

    async def _is_safe_url(self, url: str) -> bool:
        """Validate a feed URL against SSRF (scheme + resolved IP ranges)."""
        try:
            parsed = urlparse(str(url or "").strip())
        except ValueError:
            return False
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        port = parsed.port or (443 if scheme == "https" else 80)
        return await asyncio.to_thread(self._resolve_and_check_host, host, port)

    async def _parse(self, url: str):
        """Parse a feed URL off-thread. Returns the parsed feed or None."""
        if feedparser is None:
            return None
        # Re-validate before every fetch (DNS may have changed since add).
        if not await self._is_safe_url(url):
            log.warning("SocialFeed: blocked unsafe feed URL %s", url)
            return None
        try:
            return await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, feedparser.parse, url), timeout=30
            )
        except Exception:
            log.debug("feed parse failed for %s", url, exc_info=True)
            return None

    @staticmethod
    def _feed_ok(parsed) -> bool:
        """A feed is usable if it has entries or at least a parsed feed title."""
        if parsed is None:
            return False
        if getattr(parsed, "entries", None):
            return True
        feed_meta = getattr(parsed, "feed", {}) or {}
        return bool(feed_meta.get("title")) and not getattr(parsed, "bozo", 0)

    @staticmethod
    def _entry_image(entry) -> Optional[str]:
        """Extract an image URL from media/enclosure metadata of an entry."""
        for media_key in ("media_content", "media_thumbnail"):
            for media in entry.get(media_key) or []:
                url = str(media.get("url") or "")
                mtype = str(media.get("type") or "")
                if url and (mtype.startswith("image/") or media_key == "media_thumbnail" or not mtype):
                    return url
        for enc in entry.get("enclosures") or []:
            url = str(enc.get("href") or enc.get("url") or "")
            if url and str(enc.get("type") or "").startswith("image/"):
                return url
        for link in entry.get("links") or []:
            if str(link.get("rel") or "") == "enclosure" and str(link.get("type") or "").startswith("image/"):
                url = str(link.get("href") or "")
                if url:
                    return url
        return None

    def _entry_embed(self, feed_name: str, entry) -> discord.Embed:
        title = _clean_text(entry.get("title") or "", 250) or "—"
        link = str(entry.get("link") or "").strip()
        summary = _clean_text(entry.get("summary") or entry.get("description") or "", 400)
        embed = discord.Embed(
            title=title,
            url=link or None,
            description=summary or None,
            colour=discord.Colour.blurple(),
        )
        embed.set_author(name=f"📢 {feed_name[:250]}")
        image = self._entry_image(entry)
        if image:
            embed.set_image(url=image)
        return embed

    # ------------------------------------------------------------------ #
    # Polling loop (per-feed interval scheduling)
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                async with self._tick_lock:  # parallel-safe (reload/startup)
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("SocialFeed tick failed")
            await asyncio.sleep(LOOP_STEP)

    async def _tick(self) -> None:
        if feedparser is None:
            return
        now = time.time()
        guilds = await self.config.all_guilds()
        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            feeds = gconf.get("feeds") or {}
            if not feeds:
                continue
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            # Poll (network I/O) outside of any config lock, then merge the
            # results per feed ID so concurrent edits/removals are not lost.
            polled: dict = {}
            for fid, feed in feeds.items():
                interval = max(MIN_POLL_INTERVAL, int(feed.get("interval") or DEFAULT_POLL_INTERVAL))
                if now - float(feed.get("last_fetch") or 0) < interval:
                    continue
                polled[fid] = await self._poll_feed(guild, dict(feed))
            if polled:
                async with self.config.guild(guild).feeds() as current:
                    for fid, result in polled.items():
                        existing = current.get(fid)
                        if existing is None:
                            continue  # feed removed meanwhile
                        if existing.get("url") != result.get("url"):
                            continue  # URL edited meanwhile; poll state is stale
                        # Only merge poll-state keys; keep user-editable fields.
                        for key in ("last_fetch", "last_status", "last_error",
                                    "error_count", "entry_count", "seen", "last"):
                            if key in result:
                                existing[key] = result[key]
                        current[fid] = existing

    async def _poll_feed(self, guild: discord.Guild, feed: dict) -> dict:
        """Poll a single feed, post new entries, return the updated feed dict."""
        feed["last_fetch"] = time.time()
        parsed = await self._parse(feed.get("url", ""))
        if parsed is None or not getattr(parsed, "entries", None):
            feed["last_status"] = "error"
            feed["error_count"] = int(feed.get("error_count") or 0) + 1
            feed["last_error"] = "fetch/parse failed or feed has no entries"
            log.warning("SocialFeed: polling %s failed", feed.get("url"))
            return feed

        entries = list(parsed.entries)
        feed["last_status"] = "ok"
        feed["entry_count"] = len(entries)

        seen = list(feed.get("seen") or [])
        seen_set = set(seen)
        first_run = not seen and feed.get("last") is None

        new_items = []
        if not first_run:
            legacy_last = feed.get("last")
            for e in entries:
                key_hash = self._entry_hash(e)
                if key_hash in seen_set:
                    continue
                # Legacy marker support: stop at the previously newest entry.
                if legacy_last is not None and not seen and self._entry_key(e) == legacy_last:
                    break
                new_items.append(e)

        channel = guild.get_channel(feed.get("channel"))
        if new_items and channel is not None and channel.permissions_for(guild.me).send_messages:
            name = feed.get("name") or _clean_text((getattr(parsed, "feed", {}) or {}).get("title") or "", 100) or "Feed"
            for e in reversed(new_items[:BURST_CAP]):  # oldest first, cap burst
                try:
                    await channel.send(embed=self._entry_embed(name, e))
                except discord.HTTPException:
                    log.warning("SocialFeed: sending entry to #%s failed", getattr(channel, "name", "?"), exc_info=True)

        # Persist de-dup hashes (all current entries), capped.
        for e in entries:
            h = self._entry_hash(e)
            if h not in seen_set:
                seen.append(h)
                seen_set.add(h)
        feed["seen"] = seen[-SEEN_CAP:]
        feed["last"] = self._entry_key(entries[0])  # keep legacy marker updated
        return feed

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="feeds", aliases=["socialfeed"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def feeds(self, ctx: commands.Context) -> None:
        """Configure social media / RSS feeds."""

    @feeds.command(name="enable")
    @app_commands.describe(on_off="Enable or disable feed watching")
    async def f_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Feeds **{state}**.", f"Feeds **{state}**."))

    @feeds.command(name="add")
    @app_commands.describe(channel="Channel to post into", url="Feed URL (RSS/Atom)", name="Optional display name")
    async def f_add(self, ctx: commands.Context, channel: discord.TextChannel, url: str, *, name: Optional[str] = None) -> None:
        """Add a feed (the URL is validated with a test fetch + parse)."""
        lang = await self._lang(ctx.guild)
        if feedparser is None:
            await ctx.send(self._t(lang, "`feedparser` ist nicht installiert.", "`feedparser` is not installed."))
            return
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            await ctx.send(self._t(lang, "Die Feed-URL muss mit http(s):// beginnen.", "The feed URL must start with http(s)://."))
            return
        if not await self._is_safe_url(url):
            await ctx.send(self._t(
                lang,
                "Diese URL ist nicht erlaubt (nicht auflösbar oder zeigt auf ein internes Netzwerk).",
                "This URL is not allowed (unresolvable or pointing to an internal network).",
            ))
            return
        await ctx.typing()
        parsed = await self._parse(url)
        if not self._feed_ok(parsed):
            await ctx.send(self._t(
                lang,
                "Konnte den Feed nicht lesen — URL geprüft? (Abruf + Parse-Test fehlgeschlagen)",
                "Couldn't read that feed — is the URL correct? (fetch + parse test failed)",
            ))
            return
        entries = list(getattr(parsed, "entries", []) or [])
        fid = uuid.uuid4().hex[:8]
        async with self.config.guild(ctx.guild).feeds() as feeds:
            feeds[fid] = {
                "url": url,
                "channel": channel.id,
                "name": (name or "").strip() or None,
                "last": self._entry_key(entries[0]) if entries else None,
                "interval": DEFAULT_POLL_INTERVAL,
                "seen": [self._entry_hash(e) for e in entries][:SEEN_CAP],
                "last_fetch": time.time(),
                "last_status": "ok",
                "last_error": None,
                "error_count": 0,
                "entry_count": len(entries),
            }
        await ctx.send(self._t(lang, f"Feed hinzugefügt (ID `{fid}`) → {channel.mention}.", f"Feed added (ID `{fid}`) → {channel.mention}."))

    @feeds.command(name="remove")
    @app_commands.describe(feed_id="Feed ID (from 'feeds list')")
    async def f_remove(self, ctx: commands.Context, feed_id: str) -> None:
        """Remove a feed."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).feeds() as feeds:
            existed = feeds.pop(feed_id, None) is not None
        await ctx.send(self._t(lang, "Entfernt." if existed else "Nicht gefunden.", "Removed." if existed else "Not found."))

    @feeds.command(name="interval")
    @app_commands.describe(feed_id="Feed ID (from 'feeds list')", seconds="Poll interval in seconds (min 60)")
    async def f_interval(self, ctx: commands.Context, feed_id: str, seconds: int) -> None:
        """Set the poll interval for one feed (seconds, minimum 60)."""
        lang = await self._lang(ctx.guild)
        if seconds < MIN_POLL_INTERVAL:
            await ctx.send(self._t(
                lang,
                f"Minimum ist {MIN_POLL_INTERVAL} Sekunden.",
                f"The minimum is {MIN_POLL_INTERVAL} seconds.",
            ))
            return
        async with self.config.guild(ctx.guild).feeds() as feeds:
            feed = feeds.get(feed_id)
            if feed is None:
                await ctx.send(self._t(lang, "Feed nicht gefunden.", "Feed not found."))
                return
            feed["interval"] = int(seconds)
            feeds[feed_id] = feed
        await ctx.send(self._t(lang, f"Intervall für `{feed_id}`: {seconds}s", f"Interval for `{feed_id}`: {seconds}s"))

    @feeds.command(name="list")
    async def f_list(self, ctx: commands.Context) -> None:
        """List configured feeds (paginated)."""
        lang = await self._lang(ctx.guild)
        feeds = await self.config.guild(ctx.guild).feeds()
        if not feeds:
            await ctx.send(self._t(lang, "Keine Feeds.", "No feeds."))
            return
        lines = []
        for fid, f in feeds.items():
            ch = ctx.guild.get_channel(f.get("channel"))
            interval = int(f.get("interval") or DEFAULT_POLL_INTERVAL)
            lines.append(f"`{fid}` · {ch.mention if ch else '?'} · {interval}s · {f.get('name') or f.get('url')}")
        pages: List[discord.Embed] = []
        per_page = 10
        colour = await ctx.embed_colour()
        chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
        for idx, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(
                title=self._t(lang, "Feeds", "Feeds"),
                description="\n".join(chunk)[:4000],
                colour=colour,
            )
            if len(chunks) > 1:
                embed.set_footer(text=self._t(lang, f"Seite {idx}/{len(chunks)}", f"Page {idx}/{len(chunks)}"))
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS, timeout=120)

    @feeds.command(name="stats")
    @app_commands.describe(feed_id="Optional feed ID; omit for all feeds")
    async def f_stats(self, ctx: commands.Context, feed_id: Optional[str] = None) -> None:
        """Show feed statistics (last fetch, entry count, errors)."""
        lang = await self._lang(ctx.guild)
        feeds = await self.config.guild(ctx.guild).feeds()
        if feed_id is not None:
            feeds = {feed_id: feeds[feed_id]} if feed_id in feeds else {}
        if not feeds:
            await ctx.send(self._t(lang, "Keine Feeds gefunden.", "No feeds found."))
            return
        pages: List[discord.Embed] = []
        colour = await ctx.embed_colour()
        items = list(feeds.items())
        per_page = 5
        chunks = [items[i:i + per_page] for i in range(0, len(items), per_page)]
        for idx, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(title=self._t(lang, "Feed-Statistiken", "Feed statistics"), colour=colour)
            for fid, f in chunk:
                last_fetch = float(f.get("last_fetch") or 0)
                fetched = f"<t:{int(last_fetch)}:R>" if last_fetch else "—"
                status = f.get("last_status") or "—"
                err = f.get("last_error")
                value = self._t(
                    lang,
                    f"Status: `{status}` · Letzter Abruf: {fetched}\n"
                    f"Einträge: {f.get('entry_count') or 0} · Fehler: {f.get('error_count') or 0}"
                    + (f"\nLetzter Fehler: {err}" if err else ""),
                    f"Status: `{status}` · Last fetch: {fetched}\n"
                    f"Entries: {f.get('entry_count') or 0} · Errors: {f.get('error_count') or 0}"
                    + (f"\nLast error: {err}" if err else ""),
                )
                embed.add_field(name=f"`{fid}` · {f.get('name') or f.get('url')}"[:256], value=value[:1024], inline=False)
            if len(chunks) > 1:
                embed.set_footer(text=self._t(lang, f"Seite {idx}/{len(chunks)}", f"Page {idx}/{len(chunks)}"))
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS, timeout=120)

    @feeds.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def f_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard: settings panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("socialfeed", L("Social-Feeds", "Social feeds"), mount="guild_settings", permission="guild_admin", order=65)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        note = ""
        if feedparser is None:
            note = tr_lang(lang, "\n\n⚠️ `feedparser` nicht installiert.", "\n\n⚠️ `feedparser` not installed.")
        return PanelSchema(
            description=tr_lang(
                lang,
                f"RSS/Atom-Feeds beobachten und Neues posten. Feeds im Tab 'Feeds' verwalten.{note}",
                f"Watch RSS/Atom feeds and post new items. Manage feeds in the 'Feeds' tab.{note}",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.select(
                    "language", L("Sprache", "Language"),
                    [{"value": "de-DE", "label": "Deutsch"}, {"value": "en-US", "label": "English"}],
                    value=str(lang), reload_on_change=True,
                ),
            ],
        )

    @settings_panel.on_submit
    async def _save_settings(self, ctx, data):
        conf = self.config.guild(ctx.guild)
        await conf.enabled.set(bool(data.get("enabled")))
        lang = str(data.get("language", "en-US")).strip()
        if lang not in ("de-DE", "en-US"):
            lang = "en-US"
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard: feeds table (add/edit/delete)
    # ------------------------------------------------------------------ #
    @dashboard_list(
        "feeds", L("Feeds", "Feeds"), mount="guild_settings", permission="guild_admin", order=67,
        columns=[
            {"key": "name", "label": "Name"},
            {"key": "channel", "label": "Channel"},
            {"key": "interval", "label": "Interval"},
            {"key": "url", "label": "URL"},
        ],
        description=L("Feed-URLs. Neue im Tab 'Feed hinzufügen'.", "Feed URLs. Add new ones in the 'Add feed' tab."),
    )
    async def feeds_list(self, ctx):
        feeds = await self.config.guild(ctx.guild).feeds()
        rows = []
        for fid, f in feeds.items():
            ch = ctx.guild.get_channel(f.get("channel"))
            rows.append({
                "id": fid,
                "cells": {
                    "name": f.get("name") or "—",
                    "channel": f"#{ch.name}" if ch else "?",
                    "interval": f"{int(f.get('interval') or DEFAULT_POLL_INTERVAL)}s",
                    "url": (f.get("url") or "")[:60],
                },
            })
        return rows

    @feeds_list.edit_form
    async def feeds_edit_form(self, ctx, item_id):
        feeds = await self.config.guild(ctx.guild).feeds()
        f = feeds.get(item_id) or {}
        return PanelSchema(fields=[
            Field.text("name", L("Name", "Name"), value=str(f.get("name") or "")),
            Field.text("url", L("Feed-URL", "Feed URL"), value=str(f.get("url") or "")),
            Field.channel("channel", L("Kanal", "Channel"), value=str(f.get("channel") or "")),
            Field.number("interval", L("Intervall (Sek., min 60)", "Interval (sec, min 60)"),
                         value=int(f.get("interval") or DEFAULT_POLL_INTERVAL)),
        ])

    @feeds_list.on_edit
    async def feeds_edit(self, ctx, item_id, data):
        lang = await self.config.guild(ctx.guild).language()
        ch = str(data.get("channel") or "").strip()
        url = str(data.get("url") or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            return SubmitResult.fail(tr_lang(lang, "URL muss mit http(s):// beginnen.", "URL must start with http(s)://."))
        if url and not await self._is_safe_url(url):
            return SubmitResult.fail(tr_lang(
                lang,
                "Diese URL ist nicht erlaubt (nicht auflösbar oder internes Netzwerk).",
                "This URL is not allowed (unresolvable or internal network).",
            ))
        try:
            interval = int(data.get("interval") or DEFAULT_POLL_INTERVAL)
        except (TypeError, ValueError):
            return SubmitResult.fail(tr_lang(lang, "Intervall muss eine Zahl sein.", "Interval must be a number."))
        if interval < MIN_POLL_INTERVAL:
            return SubmitResult.fail(tr_lang(
                lang, f"Intervall-Minimum: {MIN_POLL_INTERVAL}s.", f"Interval minimum: {MIN_POLL_INTERVAL}s."
            ))
        async with self.config.guild(ctx.guild).feeds() as feeds:
            f = feeds.get(item_id) or {}
            f["name"] = str(data.get("name") or "").strip() or None
            if url and url != f.get("url"):
                f["url"] = url
                f["seen"] = []  # new URL: reset de-dup history
                f["last"] = None
            if ch.isdigit():
                f["channel"] = int(ch)
            f["interval"] = interval
            feeds[item_id] = f
        return SubmitResult.ok(tr_lang(lang, "Feed gespeichert.", "Feed saved."))

    @feeds_list.on_delete
    async def feeds_delete(self, ctx, item_id):
        lang = await self.config.guild(ctx.guild).language()
        async with self.config.guild(ctx.guild).feeds() as feeds:
            feeds.pop(item_id, None)
        return SubmitResult.ok(tr_lang(lang, "Feed gelöscht.", "Feed deleted."))

    @dashboard_panel("feed_add", L("Feed hinzufügen", "Add feed"), mount="guild_settings", permission="guild_admin", order=66)
    async def feed_add_panel(self, ctx):
        lang = await self.config.guild(ctx.guild).language()
        return PanelSchema(
            description=tr_lang(lang, "Neuen RSS/Atom-Feed hinzufügen (URL wird geprüft).", "Add a new RSS/Atom feed (the URL is validated)."),
            fields=[
                Field.text("name", L("Name (optional)", "Name (optional)"), value=""),
                Field.text("url", L("Feed-URL", "Feed URL"), value=""),
                Field.channel("channel", L("Kanal", "Channel"), value=""),
                Field.number("interval", L("Intervall (Sek., min 60)", "Interval (sec, min 60)"), value=DEFAULT_POLL_INTERVAL),
            ],
        )

    @feed_add_panel.on_submit
    async def _feed_add(self, ctx, data):
        lang = await self.config.guild(ctx.guild).language()
        url = str(data.get("url") or "").strip()
        ch = str(data.get("channel") or "").strip()
        if not url or not ch.isdigit():
            return SubmitResult.fail(tr_lang(lang, "URL und Kanal erforderlich.", "URL and channel required."))
        if not url.lower().startswith(("http://", "https://")):
            return SubmitResult.fail(tr_lang(lang, "URL muss mit http(s):// beginnen.", "URL must start with http(s)://."))
        if not await self._is_safe_url(url):
            return SubmitResult.fail(tr_lang(
                lang,
                "Diese URL ist nicht erlaubt (nicht auflösbar oder internes Netzwerk).",
                "This URL is not allowed (unresolvable or internal network).",
            ))
        try:
            interval = int(data.get("interval") or DEFAULT_POLL_INTERVAL)
        except (TypeError, ValueError):
            return SubmitResult.fail(tr_lang(lang, "Intervall muss eine Zahl sein.", "Interval must be a number."))
        if interval < MIN_POLL_INTERVAL:
            return SubmitResult.fail(tr_lang(
                lang, f"Intervall-Minimum: {MIN_POLL_INTERVAL}s.", f"Interval minimum: {MIN_POLL_INTERVAL}s."
            ))
        parsed = await self._parse(url)
        if not self._feed_ok(parsed):
            return SubmitResult.fail(tr_lang(
                lang,
                "Feed nicht lesbar (Abruf/Parse-Test fehlgeschlagen).",
                "Feed unreadable (fetch/parse test failed).",
            ))
        entries = list(getattr(parsed, "entries", []) or [])
        fid = uuid.uuid4().hex[:8]
        async with self.config.guild(ctx.guild).feeds() as feeds:
            feeds[fid] = {
                "url": url,
                "channel": int(ch),
                "name": (str(data.get("name") or "").strip() or None),
                "last": self._entry_key(entries[0]) if entries else None,
                "interval": interval,
                "seen": [self._entry_hash(e) for e in entries][:SEEN_CAP],
                "last_fetch": time.time(),
                "last_status": "ok",
                "last_error": None,
                "error_count": 0,
                "entry_count": len(entries),
            }
        return SubmitResult.ok(tr_lang(lang, "Feed hinzugefügt.", "Feed added."), reload=True)

    # ------------------------------------------------------------------ #
    # Dashboard: status page (guild scope)
    # ------------------------------------------------------------------ #
    @dashboard_page(
        "status", L("Feed-Status", "Feed status"),
        scope="guild", permission="guild_admin", icon="chart",
    )
    async def status_page(self, ctx):
        feeds = await self.config.guild(ctx.guild).feeds()
        enabled = await self.config.guild(ctx.guild).enabled()
        rows = []
        for fid, f in feeds.items():
            ch = ctx.guild.get_channel(f.get("channel"))
            last_fetch = float(f.get("last_fetch") or 0)
            fetched = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_fetch)) if last_fetch else "—"
            rows.append([
                fid,
                f.get("name") or (f.get("url") or "")[:40],
                f"#{ch.name}" if ch else "?",
                f"{int(f.get('interval') or DEFAULT_POLL_INTERVAL)}s",
                str(f.get("last_status") or "—"),
                fetched,
                str(f.get("entry_count") or 0),
                str(f.get("error_count") or 0),
            ])
        comps = [
            Component.heading(L("Social-Feed Status", "Social feed status")),
            Component.text(
                L("Modul aktiviert." if enabled else "Modul deaktiviert.",
                  "Module enabled." if enabled else "Module disabled.")
            ),
        ]
        if rows:
            comps.append(Component.table(
                columns=["ID", "Feed", "Channel", L("Intervall", "Interval"), "Status",
                         L("Letzter Abruf", "Last fetch"), L("Einträge", "Entries"), L("Fehler", "Errors")],
                rows=rows,
                title=L("Feeds", "Feeds"),
            ))
        else:
            comps.append(Component.text(L("Keine Feeds konfiguriert.", "No feeds configured.")))
        return PageSchema(components=comps)
