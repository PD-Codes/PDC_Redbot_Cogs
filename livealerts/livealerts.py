"""LiveAlerts — Twitch live notifications and YouTube upload alerts.

Twitch uses the Helix API with app credentials from Red's shared API keys
(``[p]set api twitch client_id,... client_secret,...``); the app access token
is cached and refreshed automatically and live status is fetched in batches of
up to 100 logins per request. YouTube needs no API key — new uploads are
detected via the public channel RSS feed. Per-guild subscriptions with target
channel, message template ({streamer}, {title}, {url}, {game}) and optional
role ping. Bilingual (DE/EN, default en-US), works with or without the PDC
web dashboard.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

import aiohttp
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

log = logging.getLogger("red.pdc.livealerts")

DEFAULT_POLL_INTERVAL = 120  # seconds
MIN_POLL_INTERVAL = 60  # seconds
JITTER_MAX = 15  # seconds of random jitter added to each cycle
BACKOFF_MAX = 900  # cap for error backoff in seconds
SEEN_CAP = 50  # persisted de-dup video IDs per YouTube subscription
TWITCH_BATCH = 100  # Helix allows up to 100 logins per streams request

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAMS_URL = "https://api.twitch.tv/helix/streams"
YT_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

ATOM_NS = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"

DEFAULT_TWITCH_TEMPLATE = "🔴 **{streamer}** is now live: {title}\n{url}"
DEFAULT_YT_TEMPLATE = "📺 **{streamer}** uploaded a new video: {title}\n{url}"

API_KEY_HINT_CMD = "[p]set api twitch client_id,<client_id> client_secret,<client_secret>"


def _fill_template(template: str, *, streamer: str, title: str, url: str, game: str) -> str:
    """Safely substitute the supported placeholders in a message template."""
    out = str(template or "")
    for key, val in (("{streamer}", streamer), ("{title}", title), ("{url}", url), ("{game}", game)):
        out = out.replace(key, str(val or ""))
    return out[:1900]


class LiveAlerts(commands.Cog):
    """Twitch live & YouTube upload notifications."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x11FE_A1E7_75, force_registration=True)
        # subs: id -> {type: "twitch"|"youtube", key: login/channel_id, name,
        #              channel, message, role, offline ("leave"|"edit"|"delete"),
        #              live_id, live_message (channel_id/message_id pair),
        #              seen (video ids), last_status, last_error}
        self.config.register_guild(
            enabled=False,
            language="en-US",
            interval=DEFAULT_POLL_INTERVAL,
            subs={},
        )
        self.session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._tick_lock = asyncio.Lock()
        # In-memory Twitch app token cache: (token, expires_at_epoch)
        self._twitch_token: Optional[str] = None
        self._twitch_token_expiry: float = 0.0
        self._error_streak = 0  # consecutive failed ticks, drives backoff

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        register_dashboard(self)
        self._task = asyncio.create_task(self._loop())

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._task:
            self._task.cancel()
        if self.session and not self.session.closed:
            asyncio.create_task(self.session.close())

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        """This cog stores no personal user data — nothing to delete."""
        return

    @staticmethod
    def _t(lang: str, de: str, en: str) -> str:
        return tr_lang(lang, de, en)

    async def _lang(self, guild) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    # ------------------------------------------------------------------ #
    # Twitch Helix helpers (app access token flow with caching)
    # ------------------------------------------------------------------ #
    async def _twitch_creds(self) -> Optional[Tuple[str, str]]:
        """Return (client_id, client_secret) from Red's shared API keys, or None."""
        tokens = await self.bot.get_shared_api_tokens("twitch")
        client_id = tokens.get("client_id")
        client_secret = tokens.get("client_secret")
        if not client_id or not client_secret:
            return None
        return client_id, client_secret

    async def _get_twitch_token(self, force: bool = False) -> Optional[str]:
        """Return a cached app access token, refreshing it when expired."""
        if not force and self._twitch_token and time.time() < self._twitch_token_expiry - 60:
            return self._twitch_token
        creds = await self._twitch_creds()
        if creds is None or self.session is None:
            return None
        client_id, client_secret = creds
        try:
            async with self.session.post(
                TWITCH_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("LiveAlerts: Twitch token request failed (HTTP %s)", resp.status)
                    return None
                data = await resp.json()
        except Exception:
            log.warning("LiveAlerts: Twitch token request errored", exc_info=True)
            return None
        token = data.get("access_token")
        if not token:
            return None
        self._twitch_token = token
        self._twitch_token_expiry = time.time() + float(data.get("expires_in") or 3600)
        return token

    async def _twitch_streams(self, logins: List[str]) -> Optional[Dict[str, dict]]:
        """Fetch live streams for the given logins (batched, max 100 each).

        Returns a mapping of lowercase login -> stream payload for channels
        that are currently live, or None on hard failure (no creds / API down).
        """
        creds = await self._twitch_creds()
        if creds is None or self.session is None:
            return None
        token = await self._get_twitch_token()
        if token is None:
            return None
        client_id = creds[0]
        live: Dict[str, dict] = {}
        for i in range(0, len(logins), TWITCH_BATCH):
            batch = logins[i:i + TWITCH_BATCH]
            params = [("user_login", login) for login in batch]
            params.append(("first", str(TWITCH_BATCH)))
            for attempt in (1, 2):
                try:
                    async with self.session.get(
                        TWITCH_STREAMS_URL,
                        params=params,
                        headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"},
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 401 and attempt == 1:
                            # Token expired/revoked: refresh once and retry.
                            token = await self._get_twitch_token(force=True)
                            if token is None:
                                return None
                            continue
                        if resp.status != 200:
                            log.warning("LiveAlerts: Helix streams failed (HTTP %s)", resp.status)
                            return None
                        data = await resp.json()
                except Exception:
                    log.warning("LiveAlerts: Helix streams request errored", exc_info=True)
                    return None
                for stream in data.get("data") or []:
                    live[str(stream.get("user_login") or "").lower()] = stream
                break
        return live

    # ------------------------------------------------------------------ #
    # YouTube RSS helpers (no API key required)
    # ------------------------------------------------------------------ #
    async def _youtube_entries(self, channel_id: str) -> Optional[List[dict]]:
        """Fetch and parse the upload RSS feed of a YouTube channel.

        Returns a list of {video_id, title, url, author} (newest first),
        or None on fetch/parse failure.
        """
        if self.session is None:
            return None
        url = YT_FEED_URL.format(cid=channel_id)
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
        except Exception:
            log.debug("LiveAlerts: YouTube feed fetch failed for %s", channel_id, exc_info=True)
            return None
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return None
        entries: List[dict] = []
        for entry in root.findall(f"{ATOM_NS}entry"):
            vid = entry.findtext(f"{YT_NS}videoId") or ""
            title = entry.findtext(f"{ATOM_NS}title") or ""
            author = entry.findtext(f"{ATOM_NS}author/{ATOM_NS}name") or ""
            link_el = entry.find(f"{ATOM_NS}link")
            link = link_el.get("href") if link_el is not None else f"https://www.youtube.com/watch?v={vid}"
            if vid:
                entries.append({"video_id": vid, "title": title, "url": link, "author": author})
        return entries

    # ------------------------------------------------------------------ #
    # Polling loop with jitter and error backoff
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                async with self._tick_lock:
                    await self._tick()
                self._error_streak = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._error_streak += 1
                log.exception("LiveAlerts tick failed")
            delay = await self._sleep_time()
            await asyncio.sleep(delay)

    async def _sleep_time(self) -> float:
        """Compute the next sleep: smallest configured guild interval + jitter,
        multiplied by an exponential backoff when previous ticks errored."""
        interval = DEFAULT_POLL_INTERVAL
        try:
            guilds = await self.config.all_guilds()
            intervals = [
                max(MIN_POLL_INTERVAL, int(g.get("interval") or DEFAULT_POLL_INTERVAL))
                for g in guilds.values()
                if g.get("enabled")
            ]
            if intervals:
                interval = min(intervals)
        except Exception:
            pass
        delay = interval + random.uniform(0, JITTER_MAX)
        if self._error_streak:
            delay = min(BACKOFF_MAX, delay * (2 ** min(self._error_streak, 4)))
        return delay

    async def _tick(self) -> None:
        guilds = await self.config.all_guilds()
        now = time.time()

        # Collect all Twitch logins across guilds for one batched lookup.
        twitch_logins: List[str] = []
        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            for sub in (gconf.get("subs") or {}).values():
                if sub.get("type") == "twitch":
                    login = str(sub.get("key") or "").lower()
                    if login and login not in twitch_logins:
                        twitch_logins.append(login)

        live_map: Optional[Dict[str, dict]] = None
        if twitch_logins:
            live_map = await self._twitch_streams(twitch_logins)
            # None means no credentials or API failure — Twitch subs are
            # skipped gracefully this tick (see per-sub status below).

        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            # Per-guild interval is enforced per subscription via last_fetch.
            interval = max(MIN_POLL_INTERVAL, int(gconf.get("interval") or DEFAULT_POLL_INTERVAL))
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            subs = gconf.get("subs") or {}
            if not subs:
                continue
            updated = dict(subs)
            dirty = False
            for sid, sub in subs.items():
                sub = dict(sub)
                if now - float(sub.get("last_fetch") or 0) < interval:
                    continue
                if sub.get("type") == "twitch":
                    updated[sid] = await self._process_twitch(guild, sub, live_map)
                elif sub.get("type") == "youtube":
                    updated[sid] = await self._process_youtube(guild, sub)
                else:
                    continue
                dirty = True
            if dirty:
                await self.config.guild(guild).subs.set(updated)

    # ------------------------------------------------------------------ #
    # Per-subscription processing
    # ------------------------------------------------------------------ #
    def _sub_channel(self, guild: discord.Guild, sub: dict) -> Optional[discord.TextChannel]:
        channel = guild.get_channel(int(sub.get("channel") or 0))
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            return None
        return channel

    def _ping_prefix(self, guild: discord.Guild, sub: dict) -> str:
        role_id = sub.get("role")
        if role_id:
            role = guild.get_role(int(role_id))
            if role is not None:
                return f"{role.mention} "
        return ""

    async def _process_twitch(self, guild: discord.Guild, sub: dict, live_map: Optional[Dict[str, dict]]) -> dict:
        sub["last_fetch"] = time.time()
        if live_map is None:
            # No credentials or Helix unavailable — report and skip.
            sub["last_status"] = "no_api"
            sub["last_error"] = f"Twitch API keys missing or unreachable — set them with {API_KEY_HINT_CMD}"
            return sub
        sub["last_status"] = "ok"
        sub["last_error"] = None
        login = str(sub.get("key") or "").lower()
        stream = live_map.get(login)

        if stream is not None:
            stream_id = str(stream.get("id") or "")
            if stream_id and stream_id != str(sub.get("live_id") or ""):
                # New stream (de-duped by stream id): announce it.
                await self._announce_twitch(guild, sub, stream)
                sub["live_id"] = stream_id
        else:
            if sub.get("live_id"):
                # Channel went offline: apply the configured offline behaviour.
                await self._handle_offline(guild, sub)
                sub["live_id"] = None
                sub["live_message"] = None
        return sub

    async def _announce_twitch(self, guild: discord.Guild, sub: dict, stream: dict) -> None:
        channel = self._sub_channel(guild, sub)
        if channel is None:
            return
        streamer = str(stream.get("user_name") or sub.get("key") or "")
        title = str(stream.get("title") or "")
        game = str(stream.get("game_name") or "")
        url = f"https://twitch.tv/{stream.get('user_login') or sub.get('key')}"
        viewers = int(stream.get("viewer_count") or 0)
        thumb = str(stream.get("thumbnail_url") or "").replace("{width}", "640").replace("{height}", "360")
        content = self._ping_prefix(guild, sub) + _fill_template(
            sub.get("message") or DEFAULT_TWITCH_TEMPLATE,
            streamer=streamer, title=title, url=url, game=game,
        )
        embed = discord.Embed(title=title[:250] or streamer, url=url, colour=discord.Colour.purple())
        embed.set_author(name=f"🔴 {streamer} — LIVE")
        if game:
            embed.add_field(name="Game", value=game[:100], inline=True)
        embed.add_field(name="Viewers", value=str(viewers), inline=True)
        if thumb:
            # Cache-bust the thumbnail so Discord shows a fresh preview.
            embed.set_image(url=f"{thumb}?t={int(time.time())}")
        try:
            msg = await channel.send(content=content, embed=embed)
            sub["live_message"] = {"channel": channel.id, "message": msg.id}
        except discord.HTTPException:
            log.warning("LiveAlerts: sending Twitch alert to #%s failed", getattr(channel, "name", "?"), exc_info=True)

    async def _handle_offline(self, guild: discord.Guild, sub: dict) -> None:
        mode = str(sub.get("offline") or "leave")
        ref = sub.get("live_message") or {}
        if mode == "leave" or not ref:
            return
        channel = guild.get_channel(int(ref.get("channel") or 0))
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(int(ref.get("message") or 0))
        except (discord.HTTPException, ValueError):
            return
        try:
            if mode == "delete":
                await msg.delete()
            elif mode == "edit":
                lang = await self._lang(guild)
                embed = msg.embeds[0] if msg.embeds else discord.Embed()
                embed.set_author(name=self._t(lang, "⚫ Stream beendet", "⚫ Stream ended"))
                embed.colour = discord.Colour.dark_grey()
                await msg.edit(embed=embed)
        except discord.HTTPException:
            log.debug("LiveAlerts: offline handling failed", exc_info=True)

    async def _process_youtube(self, guild: discord.Guild, sub: dict) -> dict:
        sub["last_fetch"] = time.time()
        entries = await self._youtube_entries(str(sub.get("key") or ""))
        if entries is None:
            sub["last_status"] = "error"
            sub["last_error"] = "feed fetch/parse failed"
            return sub
        sub["last_status"] = "ok"
        sub["last_error"] = None

        seen = list(sub.get("seen") or [])
        seen_set = set(seen)
        first_run = not seen and not sub.get("seeded")

        new_items = [] if first_run else [e for e in entries if e["video_id"] not in seen_set]
        channel = self._sub_channel(guild, sub)
        if new_items and channel is not None:
            for e in reversed(new_items[:5]):  # oldest first, cap burst
                streamer = sub.get("name") or e.get("author") or sub.get("key")
                content = self._ping_prefix(guild, sub) + _fill_template(
                    sub.get("message") or DEFAULT_YT_TEMPLATE,
                    streamer=str(streamer), title=e["title"], url=e["url"], game="",
                )
                try:
                    await channel.send(content=content)
                except discord.HTTPException:
                    log.warning("LiveAlerts: sending YouTube alert failed", exc_info=True)

        for e in entries:
            if e["video_id"] not in seen_set:
                seen.append(e["video_id"])
                seen_set.add(e["video_id"])
        sub["seen"] = seen[-SEEN_CAP:]
        sub["seeded"] = True
        return sub

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="livealerts", aliases=["la"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def livealerts(self, ctx: commands.Context) -> None:
        """Configure Twitch live & YouTube upload notifications."""

    @livealerts.command(name="enable")
    @app_commands.describe(on_off="Enable or disable live alerts")
    async def la_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        await ctx.send(self._t(
            lang,
            f"Live-Benachrichtigungen **{'aktiviert' if on_off else 'deaktiviert'}**.",
            f"Live alerts **{'enabled' if on_off else 'disabled'}**.",
        ))

    @livealerts.command(name="addtwitch")
    @app_commands.describe(login="Twitch login name (from the channel URL)", channel="Channel to post into", message="Optional template ({streamer}, {title}, {url}, {game})")
    async def la_addtwitch(self, ctx: commands.Context, login: str, channel: discord.TextChannel, *, message: Optional[str] = None) -> None:
        """Subscribe to a Twitch channel's live status."""
        lang = await self._lang(ctx.guild)
        login = login.strip().lstrip("@").lower()
        creds = await self._twitch_creds()
        sid = uuid.uuid4().hex[:8]
        async with self.config.guild(ctx.guild).subs() as subs:
            subs[sid] = {
                "type": "twitch", "key": login, "name": login,
                "channel": channel.id, "message": (message or "").strip() or None,
                "role": None, "offline": "leave",
                "live_id": None, "live_message": None,
                "last_fetch": 0, "last_status": None, "last_error": None,
            }
        note = ""
        if creds is None:
            note = self._t(
                lang,
                f"\n⚠️ Keine Twitch-API-Keys gesetzt — setze sie mit `{API_KEY_HINT_CMD}` (ersetze `[p]` durch dein Prefix).",
                f"\n⚠️ No Twitch API keys set — set them with `{API_KEY_HINT_CMD}` (replace `[p]` with your prefix).",
            )
        await ctx.send(self._t(
            lang,
            f"Twitch-Abo `{login}` hinzugefügt (ID `{sid}`) → {channel.mention}.{note}",
            f"Twitch subscription `{login}` added (ID `{sid}`) → {channel.mention}.{note}",
        ))

    @livealerts.command(name="addyoutube")
    @app_commands.describe(channel_id="YouTube channel ID (starts with UC…)", channel="Channel to post into", message="Optional template ({streamer}, {title}, {url})")
    async def la_addyoutube(self, ctx: commands.Context, channel_id: str, channel: discord.TextChannel, *, message: Optional[str] = None) -> None:
        """Subscribe to a YouTube channel's uploads (via RSS, no API key)."""
        lang = await self._lang(ctx.guild)
        channel_id = channel_id.strip()
        await ctx.typing()
        entries = await self._youtube_entries(channel_id)
        if entries is None:
            await ctx.send(self._t(
                lang,
                "Konnte den YouTube-Feed nicht lesen — Kanal-ID geprüft? (Sie beginnt mit `UC…`.)",
                "Couldn't read that YouTube feed — is the channel ID correct? (It starts with `UC…`.)",
            ))
            return
        name = entries[0].get("author") if entries else channel_id
        sid = uuid.uuid4().hex[:8]
        async with self.config.guild(ctx.guild).subs() as subs:
            subs[sid] = {
                "type": "youtube", "key": channel_id, "name": name,
                "channel": channel.id, "message": (message or "").strip() or None,
                "role": None, "offline": "leave",
                "seen": [e["video_id"] for e in entries][:SEEN_CAP], "seeded": True,
                "last_fetch": time.time(), "last_status": "ok", "last_error": None,
            }
        await ctx.send(self._t(
            lang,
            f"YouTube-Abo **{name}** hinzugefügt (ID `{sid}`) → {channel.mention}.",
            f"YouTube subscription **{name}** added (ID `{sid}`) → {channel.mention}.",
        ))

    @livealerts.command(name="remove")
    @app_commands.describe(sub_id="Subscription ID (from 'livealerts list')")
    async def la_remove(self, ctx: commands.Context, sub_id: str) -> None:
        """Remove a subscription."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).subs() as subs:
            existed = subs.pop(sub_id, None) is not None
        await ctx.send(self._t(lang, "Entfernt." if existed else "Nicht gefunden.", "Removed." if existed else "Not found."))

    @livealerts.command(name="list")
    async def la_list(self, ctx: commands.Context) -> None:
        """List configured subscriptions (paginated)."""
        lang = await self._lang(ctx.guild)
        subs = await self.config.guild(ctx.guild).subs()
        if not subs:
            await ctx.send(self._t(lang, "Keine Abos.", "No subscriptions."))
            return
        lines = []
        for sid, s in subs.items():
            ch = ctx.guild.get_channel(int(s.get("channel") or 0))
            icon = "🟣" if s.get("type") == "twitch" else "🔴"
            status = s.get("last_status") or "—"
            lines.append(f"`{sid}` · {icon} {s.get('type')} · **{s.get('name') or s.get('key')}** · {ch.mention if ch else '?'} · `{status}`")
        pages: List[discord.Embed] = []
        colour = await ctx.embed_colour()
        chunks = [lines[i:i + 10] for i in range(0, len(lines), 10)]
        for idx, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(title="LiveAlerts", description="\n".join(chunk)[:4000], colour=colour)
            if len(chunks) > 1:
                embed.set_footer(text=self._t(lang, f"Seite {idx}/{len(chunks)}", f"Page {idx}/{len(chunks)}"))
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS, timeout=120)

    @livealerts.command(name="message")
    @app_commands.describe(sub_id="Subscription ID", template="Template ({streamer}, {title}, {url}, {game}); omit to reset")
    async def la_message(self, ctx: commands.Context, sub_id: str, *, template: Optional[str] = None) -> None:
        """Set/reset the message template of a subscription."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).subs() as subs:
            sub = subs.get(sub_id)
            if sub is None:
                await ctx.send(self._t(lang, "Abo nicht gefunden.", "Subscription not found."))
                return
            sub["message"] = (template or "").strip() or None
            subs[sub_id] = sub
        await ctx.send(self._t(
            lang,
            "Vorlage gespeichert." if template else "Vorlage zurückgesetzt.",
            "Template saved." if template else "Template reset.",
        ))

    @livealerts.command(name="ping")
    @app_commands.describe(sub_id="Subscription ID", role="Role to ping; omit to remove the ping")
    async def la_ping(self, ctx: commands.Context, sub_id: str, role: Optional[discord.Role] = None) -> None:
        """Set/remove the ping role of a subscription."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).subs() as subs:
            sub = subs.get(sub_id)
            if sub is None:
                await ctx.send(self._t(lang, "Abo nicht gefunden.", "Subscription not found."))
                return
            sub["role"] = role.id if role else None
            subs[sub_id] = sub
        await ctx.send(self._t(
            lang,
            f"Ping-Rolle: {role.mention if role else 'entfernt'}.",
            f"Ping role: {role.mention if role else 'removed'}.",
        ))

    @livealerts.command(name="offline")
    @app_commands.describe(sub_id="Subscription ID (Twitch)", mode="leave, edit or delete")
    async def la_offline(self, ctx: commands.Context, sub_id: str, mode: str) -> None:
        """Set the offline behaviour of a Twitch subscription (leave/edit/delete)."""
        lang = await self._lang(ctx.guild)
        mode = mode.lower().strip()
        if mode not in ("leave", "edit", "delete"):
            await ctx.send(self._t(lang, "Modus muss `leave`, `edit` oder `delete` sein.", "Mode must be `leave`, `edit` or `delete`."))
            return
        async with self.config.guild(ctx.guild).subs() as subs:
            sub = subs.get(sub_id)
            if sub is None:
                await ctx.send(self._t(lang, "Abo nicht gefunden.", "Subscription not found."))
                return
            sub["offline"] = mode
            subs[sub_id] = sub
        await ctx.send(self._t(lang, f"Offline-Verhalten: `{mode}`.", f"Offline behaviour: `{mode}`."))

    @livealerts.command(name="interval")
    @app_commands.describe(seconds="Poll interval in seconds (min 60)")
    async def la_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set the poll interval for this server (seconds, minimum 60)."""
        lang = await self._lang(ctx.guild)
        if seconds < MIN_POLL_INTERVAL:
            await ctx.send(self._t(lang, f"Minimum ist {MIN_POLL_INTERVAL} Sekunden.", f"The minimum is {MIN_POLL_INTERVAL} seconds."))
            return
        await self.config.guild(ctx.guild).interval.set(int(seconds))
        await ctx.send(self._t(lang, f"Intervall: {seconds}s", f"Interval: {seconds}s"))

    @livealerts.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def la_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard: settings panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("livealerts", L("Live-Alerts", "Live alerts"), mount="guild_settings", permission="guild_admin", order=70)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        note = ""
        if await self._twitch_creds() is None:
            note = tr_lang(
                lang,
                f"\n\n⚠️ Keine Twitch-API-Keys — Bot-Owner: `{API_KEY_HINT_CMD}`.",
                f"\n\n⚠️ No Twitch API keys — bot owner: `{API_KEY_HINT_CMD}`.",
            )
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Twitch-Live- & YouTube-Upload-Benachrichtigungen. Abos im Tab 'Abos' verwalten.{note}",
                f"Twitch live & YouTube upload notifications. Manage subscriptions in the 'Subscriptions' tab.{note}",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.number("interval", L("Intervall (Sek., min 60)", "Interval (sec, min 60)"), value=int(await conf.interval())),
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
        lang = str(data.get("language", "en-US")).strip()
        if lang not in ("de-DE", "en-US"):
            lang = "en-US"
        try:
            interval = int(data.get("interval") or DEFAULT_POLL_INTERVAL)
        except (TypeError, ValueError):
            return SubmitResult.fail(tr_lang(lang, "Intervall muss eine Zahl sein.", "Interval must be a number."))
        if interval < MIN_POLL_INTERVAL:
            return SubmitResult.fail(tr_lang(lang, f"Intervall-Minimum: {MIN_POLL_INTERVAL}s.", f"Interval minimum: {MIN_POLL_INTERVAL}s."))
        await conf.enabled.set(bool(data.get("enabled")))
        await conf.interval.set(interval)
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard: subscriptions table (add/edit/delete)
    # ------------------------------------------------------------------ #
    @dashboard_list(
        "livealerts_subs", L("Live-Abos", "Subscriptions"), mount="guild_settings", permission="guild_admin", order=71,
        columns=[
            {"key": "type", "label": "Type"},
            {"key": "name", "label": "Name"},
            {"key": "channel", "label": "Channel"},
            {"key": "role", "label": "Ping"},
            {"key": "status", "label": "Status"},
        ],
        description=L("Twitch/YouTube-Abos. Neue im Tab 'Abo hinzufügen'.", "Twitch/YouTube subscriptions. Add new ones in the 'Add subscription' tab."),
    )
    async def subs_list(self, ctx):
        subs = await self.config.guild(ctx.guild).subs()
        rows = []
        for sid, s in subs.items():
            ch = ctx.guild.get_channel(int(s.get("channel") or 0))
            role = ctx.guild.get_role(int(s.get("role") or 0)) if s.get("role") else None
            rows.append({
                "id": sid,
                "cells": {
                    "type": str(s.get("type") or "?"),
                    "name": str(s.get("name") or s.get("key") or "—"),
                    "channel": f"#{ch.name}" if ch else "?",
                    "role": f"@{role.name}" if role else "—",
                    "status": str(s.get("last_status") or "—"),
                },
            })
        return rows

    @subs_list.edit_form
    async def subs_edit_form(self, ctx, item_id):
        subs = await self.config.guild(ctx.guild).subs()
        s = subs.get(item_id) or {}
        return PanelSchema(fields=[
            Field.text("name", L("Anzeigename", "Display name"), value=str(s.get("name") or "")),
            Field.channel("channel", L("Kanal", "Channel"), value=str(s.get("channel") or "")),
            Field.text("message", L("Vorlage ({streamer}, {title}, {url}, {game})", "Template ({streamer}, {title}, {url}, {game})"), value=str(s.get("message") or "")),
            Field.text("role", L("Ping-Rollen-ID (leer = keine)", "Ping role ID (empty = none)"), value=str(s.get("role") or "")),
            Field.select("offline", L("Offline-Verhalten (Twitch)", "Offline behaviour (Twitch)"), [
                {"value": "leave", "label": "leave"},
                {"value": "edit", "label": "edit"},
                {"value": "delete", "label": "delete"},
            ], value=str(s.get("offline") or "leave")),
        ])

    @subs_list.on_edit
    async def subs_edit(self, ctx, item_id, data):
        lang = await self.config.guild(ctx.guild).language()
        ch = str(data.get("channel") or "").strip()
        role = str(data.get("role") or "").strip()
        if role and not role.isdigit():
            return SubmitResult.fail(tr_lang(lang, "Rollen-ID muss eine Zahl sein.", "Role ID must be a number."))
        async with self.config.guild(ctx.guild).subs() as subs:
            s = subs.get(item_id) or {}
            s["name"] = str(data.get("name") or "").strip() or s.get("key")
            if ch.isdigit():
                s["channel"] = int(ch)
            s["message"] = str(data.get("message") or "").strip() or None
            s["role"] = int(role) if role else None
            mode = str(data.get("offline") or "leave")
            s["offline"] = mode if mode in ("leave", "edit", "delete") else "leave"
            subs[item_id] = s
        return SubmitResult.ok(tr_lang(lang, "Abo gespeichert.", "Subscription saved."))

    @subs_list.on_delete
    async def subs_delete(self, ctx, item_id):
        lang = await self.config.guild(ctx.guild).language()
        async with self.config.guild(ctx.guild).subs() as subs:
            subs.pop(item_id, None)
        return SubmitResult.ok(tr_lang(lang, "Abo gelöscht.", "Subscription deleted."))

    @dashboard_panel("livealerts_add", L("Abo hinzufügen", "Add subscription"), mount="guild_settings", permission="guild_admin", order=72)
    async def sub_add_panel(self, ctx):
        lang = await self.config.guild(ctx.guild).language()
        return PanelSchema(
            description=tr_lang(
                lang,
                "Twitch: Login-Name (aus der Kanal-URL). YouTube: Kanal-ID (beginnt mit UC…).",
                "Twitch: login name (from the channel URL). YouTube: channel ID (starts with UC…).",
            ),
            fields=[
                Field.select("type", L("Typ", "Type"), [
                    {"value": "twitch", "label": "Twitch"},
                    {"value": "youtube", "label": "YouTube"},
                ], value="twitch"),
                Field.text("key", L("Login / Kanal-ID", "Login / channel ID"), value=""),
                Field.channel("channel", L("Kanal", "Channel"), value=""),
                Field.text("message", L("Vorlage (optional)", "Template (optional)"), value=""),
            ],
        )

    @sub_add_panel.on_submit
    async def _sub_add(self, ctx, data):
        lang = await self.config.guild(ctx.guild).language()
        sub_type = str(data.get("type") or "").strip()
        key = str(data.get("key") or "").strip()
        ch = str(data.get("channel") or "").strip()
        if sub_type not in ("twitch", "youtube") or not key or not ch.isdigit():
            return SubmitResult.fail(tr_lang(lang, "Typ, Login/ID und Kanal erforderlich.", "Type, login/ID and channel required."))
        sid = uuid.uuid4().hex[:8]
        sub = {
            "type": sub_type, "key": key.lower() if sub_type == "twitch" else key,
            "name": key, "channel": int(ch),
            "message": str(data.get("message") or "").strip() or None,
            "role": None, "offline": "leave",
            "last_fetch": 0, "last_status": None, "last_error": None,
        }
        if sub_type == "youtube":
            entries = await self._youtube_entries(key)
            if entries is None:
                return SubmitResult.fail(tr_lang(lang, "YouTube-Feed nicht lesbar (Kanal-ID prüfen).", "YouTube feed unreadable (check the channel ID)."))
            sub["name"] = entries[0].get("author") if entries else key
            sub["seen"] = [e["video_id"] for e in entries][:SEEN_CAP]
            sub["seeded"] = True
            sub["last_fetch"] = time.time()
            sub["last_status"] = "ok"
        else:
            sub["live_id"] = None
            sub["live_message"] = None
        async with self.config.guild(ctx.guild).subs() as subs:
            subs[sid] = sub
        return SubmitResult.ok(tr_lang(lang, "Abo hinzugefügt.", "Subscription added."), reload=True)

    # ------------------------------------------------------------------ #
    # Dashboard: status page (guild scope)
    # ------------------------------------------------------------------ #
    @dashboard_page("status", L("Live-Alert-Status", "Live alert status"), scope="guild", permission="guild_admin", icon="chart")
    async def status_page(self, ctx):
        conf = self.config.guild(ctx.guild)
        subs = await conf.subs()
        enabled = await conf.enabled()
        has_creds = await self._twitch_creds() is not None
        rows = []
        for sid, s in subs.items():
            ch = ctx.guild.get_channel(int(s.get("channel") or 0))
            last_fetch = float(s.get("last_fetch") or 0)
            fetched = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_fetch)) if last_fetch else "—"
            live = "LIVE" if s.get("live_id") else ("—" if s.get("type") == "twitch" else "")
            rows.append([
                sid, str(s.get("type")), str(s.get("name") or s.get("key")),
                f"#{ch.name}" if ch else "?", str(s.get("last_status") or "—"), live, fetched,
            ])
        comps = [
            Component.heading(L("Live-Alert-Status", "Live alert status")),
            Component.text(L("Modul aktiviert." if enabled else "Modul deaktiviert.",
                             "Module enabled." if enabled else "Module disabled.")),
        ]
        if not has_creds:
            comps.append(Component.text(L(
                f"⚠️ Keine Twitch-API-Keys gesetzt ({API_KEY_HINT_CMD}). YouTube funktioniert trotzdem.",
                f"⚠️ No Twitch API keys set ({API_KEY_HINT_CMD}). YouTube still works.",
            )))
        if rows:
            comps.append(Component.table(
                columns=["ID", L("Typ", "Type"), "Name", "Channel", "Status", "Live", L("Letzter Abruf", "Last fetch")],
                rows=rows,
                title=L("Abos", "Subscriptions"),
            ))
        else:
            comps.append(Component.text(L("Keine Abos konfiguriert.", "No subscriptions configured.")))
        return PageSchema(components=comps)
