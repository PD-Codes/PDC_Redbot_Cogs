"""SocialFeed — watch RSS/Atom feeds and post new items to a channel.

Add any feed URL (a YouTube channel feed, a blog, a subreddit's ``.rss`` …) and
the bot posts new entries. Manage feeds from the **web dashboard** (a table with
add/edit/delete) or via commands. Opt-in per guild, bilingual (DE/EN).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import List, Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red

from .pdc_dashboard import (
    Field,
    L,
    PanelSchema,
    SubmitResult,
    dashboard_list,
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

POLL_INTERVAL = 300  # seconds


class SocialFeed(commands.Cog):
    """Watch RSS/Atom feeds and post new items."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x50C1A1_FE, force_registration=True)
        self.config.register_guild(enabled=False, language="en-US", feeds={})
        # feed: id -> {url, channel, name, last}
        self._task: Optional[asyncio.Task] = None

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
    # Feed polling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _entry_key(entry) -> str:
        return str(entry.get("id") or entry.get("link") or entry.get("title") or "")

    async def _parse(self, url: str):
        if feedparser is None:
            return None
        try:
            return await self.bot.loop.run_in_executor(None, feedparser.parse, url)
        except Exception:
            log.debug("feed parse failed for %s", url, exc_info=True)
            return None

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("SocialFeed tick failed")
            await asyncio.sleep(POLL_INTERVAL)

    async def _tick(self) -> None:
        if feedparser is None:
            return
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
            updated = dict(feeds)
            dirty = False
            for fid, feed in feeds.items():
                parsed = await self._parse(feed.get("url", ""))
                if parsed is None or not getattr(parsed, "entries", None):
                    continue
                entries = list(parsed.entries)
                last = feed.get("last")
                newest_key = self._entry_key(entries[0])
                if last is None:
                    updated[fid] = {**feed, "last": newest_key}
                    dirty = True
                    continue
                # Collect entries until we reach the last-seen one (those are new).
                new_items = []
                for e in entries:
                    if self._entry_key(e) == last:
                        break
                    new_items.append(e)
                if not new_items:
                    continue
                channel = guild.get_channel(feed.get("channel"))
                if channel is not None and channel.permissions_for(guild.me).send_messages:
                    name = feed.get("name") or (getattr(parsed, "feed", {}) or {}).get("title") or "Feed"
                    for e in reversed(new_items[:5]):  # oldest first, cap burst
                        title = (e.get("title") or "").strip()
                        link = (e.get("link") or "").strip()
                        try:
                            await channel.send(f"📢 **{name}**: {title}\n{link}")
                        except discord.HTTPException:
                            pass
                updated[fid] = {**feed, "last": newest_key}
                dirty = True
            if dirty:
                await self.config.guild(guild).feeds.set(updated)

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
        """Add a feed."""
        lang = await self._lang(ctx.guild)
        if feedparser is None:
            await ctx.send(self._t(lang, "`feedparser` ist nicht installiert.", "`feedparser` is not installed."))
            return
        parsed = await self._parse(url)
        if parsed is None or getattr(parsed, "bozo", 1) and not getattr(parsed, "entries", None):
            await ctx.send(self._t(lang, "Konnte den Feed nicht lesen.", "Couldn't read that feed."))
            return
        fid = uuid.uuid4().hex[:8]
        last = self._entry_key(parsed.entries[0]) if getattr(parsed, "entries", None) else None
        async with self.config.guild(ctx.guild).feeds() as feeds:
            feeds[fid] = {"url": url.strip(), "channel": channel.id, "name": (name or "").strip() or None, "last": last}
        await ctx.send(self._t(lang, f"Feed hinzugefügt (ID `{fid}`) → {channel.mention}.", f"Feed added (ID `{fid}`) → {channel.mention}."))

    @feeds.command(name="remove")
    @app_commands.describe(feed_id="Feed ID (from 'feeds list')")
    async def f_remove(self, ctx: commands.Context, feed_id: str) -> None:
        """Remove a feed."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).feeds() as feeds:
            existed = feeds.pop(feed_id, None) is not None
        await ctx.send(self._t(lang, "Entfernt." if existed else "Nicht gefunden.", "Removed." if existed else "Not found."))

    @feeds.command(name="list")
    async def f_list(self, ctx: commands.Context) -> None:
        """List configured feeds."""
        lang = await self._lang(ctx.guild)
        feeds = await self.config.guild(ctx.guild).feeds()
        if not feeds:
            await ctx.send(self._t(lang, "Keine Feeds.", "No feeds."))
            return
        lines = []
        for fid, f in feeds.items():
            ch = ctx.guild.get_channel(f.get("channel"))
            lines.append(f"`{fid}` · {ch.mention if ch else '?'} · {f.get('name') or f.get('url')}")
        await ctx.send(embed=discord.Embed(
            title=self._t(lang, "Feeds", "Feeds"),
            description="\n".join(lines)[:4000],
            colour=await ctx.embed_colour(),
        ))

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
        lang = str(data.get("language", "en-US")).strip() or "en-US"
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard: feeds table (add/edit/delete)
    # ------------------------------------------------------------------ #
    @dashboard_list(
        "feeds", L("Feeds", "Feeds"), mount="guild_settings", permission="guild_admin", order=67,
        columns=[{"key": "name", "label": "Name"}, {"key": "channel", "label": "Channel"}, {"key": "url", "label": "URL"}],
        description=L("Feed-URLs. Neue im Tab 'Feed hinzufügen'.", "Feed URLs. Add new ones in the 'Add feed' tab."),
    )
    async def feeds_list(self, ctx):
        feeds = await self.config.guild(ctx.guild).feeds()
        rows = []
        for fid, f in feeds.items():
            ch = ctx.guild.get_channel(f.get("channel"))
            rows.append({"id": fid, "cells": {"name": f.get("name") or "—", "channel": f"#{ch.name}" if ch else "?", "url": (f.get("url") or "")[:60]}})
        return rows

    @feeds_list.edit_form
    async def feeds_edit_form(self, ctx, item_id):
        feeds = await self.config.guild(ctx.guild).feeds()
        f = feeds.get(item_id) or {}
        return PanelSchema(fields=[
            Field.text("name", L("Name", "Name"), value=str(f.get("name") or "")),
            Field.text("url", L("Feed-URL", "Feed URL"), value=str(f.get("url") or "")),
            Field.channel("channel", L("Kanal", "Channel"), value=str(f.get("channel") or "")),
        ])

    @feeds_list.on_edit
    async def feeds_edit(self, ctx, item_id, data):
        lang = await self.config.guild(ctx.guild).language()
        ch = str(data.get("channel") or "").strip()
        async with self.config.guild(ctx.guild).feeds() as feeds:
            f = feeds.get(item_id) or {}
            f["name"] = str(data.get("name") or "").strip() or None
            f["url"] = str(data.get("url") or "").strip() or f.get("url")
            if ch.isdigit():
                f["channel"] = int(ch)
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
            description=tr_lang(lang, "Neuen RSS/Atom-Feed hinzufügen.", "Add a new RSS/Atom feed."),
            fields=[
                Field.text("name", L("Name (optional)", "Name (optional)"), value=""),
                Field.text("url", L("Feed-URL", "Feed URL"), value=""),
                Field.channel("channel", L("Kanal", "Channel"), value=""),
            ],
        )

    @feed_add_panel.on_submit
    async def _feed_add(self, ctx, data):
        lang = await self.config.guild(ctx.guild).language()
        url = str(data.get("url") or "").strip()
        ch = str(data.get("channel") or "").strip()
        if not url or not ch.isdigit():
            return SubmitResult.fail(tr_lang(lang, "URL und Kanal erforderlich.", "URL and channel required."))
        last = None
        parsed = await self._parse(url)
        if parsed is not None and getattr(parsed, "entries", None):
            last = self._entry_key(parsed.entries[0])
        fid = uuid.uuid4().hex[:8]
        async with self.config.guild(ctx.guild).feeds() as feeds:
            feeds[fid] = {"url": url, "channel": int(ch), "name": (str(data.get("name") or "").strip() or None), "last": last}
        return SubmitResult.ok(tr_lang(lang, "Feed hinzugefügt.", "Feed added."), reload=True)
