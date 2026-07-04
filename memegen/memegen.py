"""MemeGen — post memes on command or on a timer.

Fetches from meme-api.com (Reddit) with a direct Reddit JSON fallback when the
primary API is down. ``meme`` posts on demand; an optional interval auto-posts
into a channel. Configurable subreddit sources (validated on set) and NSFW
filtering. Robust external API handling: timeout, retry with exponential
backoff, TTL cache as fallback, per-channel de-duplication. Opt-in per guild,
bilingual (DE/EN, default en-US), web dashboard integration via the drop-in.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red

from .pdc_dashboard import (
    Field,
    L,
    PanelSchema,
    SubmitResult,
    dashboard_panel,
    register_dashboard,
    tr_lang,
    unregister_dashboard,
)

log = logging.getLogger("red.pdc.memegen")

_API = "https://meme-api.com/gimme"
_REDDIT_FALLBACK = "https://www.reddit.com/r/{sub}/hot.json?limit=50&raw_json=1"
_DEFAULT_SUB = "memes"
_UA = "PDC-Redbot-MemeGen/1.1 (Red-DiscordBot cog)"

_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")

CACHE_TTL = 600  # seconds a cached meme stays valid as fallback
CACHE_POOL_SIZE = 10  # distinct fallback memes kept per source key
RECENT_PER_CHANNEL = 20  # de-dup recently shown memes per channel
HTTP_RETRIES = 3
HTTP_TIMEOUT = 10  # seconds


class MemeGen(commands.Cog):
    """Post memes on command or on a timer."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x3E3E_60, force_registration=True)
        self.config.register_guild(
            enabled=True,
            language="en-US",
            channel=None,
            interval=0,  # minutes (0 = no auto-posting)
            subreddits=[],
            last_post=0.0,
            allow_nsfw=False,  # allow NSFW memes (only ever posted in NSFW channels)
        )
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        # source key -> small pool of recent good responses, newest last —
        # used as fallback when both live sources fail. A pool (not just the
        # single last response) lets repeated fallback serves still pick a
        # meme the channel hasn't already seen instead of reposting one item
        # forever.
        self._cache: Dict[str, Deque[Tuple[float, dict]]] = {}
        # channel_id -> recently posted meme URLs
        self._recent: Dict[int, Deque[str]] = {}

    async def cog_load(self) -> None:
        register_dashboard(self)
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._task:
            self._task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _t(lang: str, de: str, en: str) -> str:
        return de if str(lang).lower().startswith("de") else en

    async def _lang(self, guild) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    # ------------------------------------------------------------------ #
    # HTTP helpers (timeout, retry + backoff, fallback source, cache)
    # ------------------------------------------------------------------ #
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
                headers={"User-Agent": _UA},
            )
        return self._session

    async def _get_json(self, url: str):
        """GET a JSON document with retries and exponential backoff on 429/5xx."""
        session = await self._get_session()
        delay = 1.0
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 429 or resp.status >= 500:
                        log.warning("meme source returned %s for %s (attempt %s/%s)",
                                    resp.status, url, attempt, HTTP_RETRIES)
                    else:
                        log.warning("meme source returned %s for %s", resp.status, url)
                        return None
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("meme request failed for %s (attempt %s/%s)", url, attempt, HTTP_RETRIES)
            if attempt < HTTP_RETRIES:
                await asyncio.sleep(delay)
                delay *= 2
        return None

    async def _fetch_primary(self, subreddit: Optional[str], allow_nsfw: bool) -> Optional[dict]:
        """Fetch a meme from meme-api.com (primary source)."""
        url = _API + (f"/{subreddit}" if subreddit else "")
        for _ in range(3):  # a few tries to skip NSFW/spoiler results
            js = await self._get_json(url)
            if not js or not js.get("url"):
                return None
            if js.get("spoiler"):
                continue
            if js.get("nsfw") and not allow_nsfw:
                continue
            return js
        return None

    async def _fetch_fallback(self, subreddit: Optional[str], allow_nsfw: bool) -> Optional[dict]:
        """Fetch a meme directly from the Reddit JSON listing (fallback source)."""
        sub = subreddit or _DEFAULT_SUB
        js = await self._get_json(_REDDIT_FALLBACK.format(sub=sub))
        try:
            children = (js or {}).get("data", {}).get("children", [])
        except AttributeError:
            return None
        posts = []
        for child in children:
            d = child.get("data") or {}
            url = str(d.get("url_overridden_by_dest") or d.get("url") or "")
            if not url.lower().endswith(_IMAGE_EXT):
                continue
            if d.get("spoiler") or d.get("stickied"):
                continue
            if d.get("over_18") and not allow_nsfw:
                continue
            posts.append({
                "title": d.get("title") or "Meme",
                "url": url,
                "postLink": f"https://reddit.com{d.get('permalink', '')}",
                "subreddit": d.get("subreddit") or sub,
                "nsfw": bool(d.get("over_18")),
            })
        return random.choice(posts) if posts else None

    async def _fetch(self, subreddit: Optional[str], *, allow_nsfw: bool = False,
                     channel_id: Optional[int] = None) -> Optional[dict]:
        """Fetch a meme: primary API, then Reddit fallback, then TTL cache."""
        key = subreddit or "_default"
        recent = self._recent.setdefault(channel_id or 0, deque(maxlen=RECENT_PER_CHANNEL))

        js = None
        fresh = False
        for _ in range(3):  # de-dup: retry a few times for an unseen meme
            js = await self._fetch_primary(subreddit, allow_nsfw)
            if js is None:
                break
            if js.get("url") not in recent:
                fresh = True
                break
        if js is None:
            js = await self._fetch_fallback(subreddit, allow_nsfw)
            if js is not None:
                fresh = True
                log.info("MemeGen: primary API down, used Reddit fallback for %r", key)
        if js is None:
            js = self._serve_from_cache(key, recent)
        if js is not None:
            # Only extend the pool on a genuinely fresh fetch — a cache-served
            # meme must not be re-added, or a single stale entry keeps renewing
            # itself and every /meme call (while both live sources are down)
            # ends up posting the exact same meme with "independent" posts
            # collapsing into identical content.
            if fresh:
                pool = self._cache.setdefault(key, deque(maxlen=CACHE_POOL_SIZE))
                pool.append((time.time(), js))
            if js.get("url"):
                recent.append(js["url"])
        return js

    def _serve_from_cache(self, key: str, recent: Deque[str]) -> Optional[dict]:
        """Fall back to a pooled past response when both live sources fail.

        Prefers an entry the channel hasn't already seen so that repeated
        /meme calls during an outage don't just repost the same meme over
        and over; only repeats one if the whole (unexpired) pool is used up.
        """
        pool = self._cache.get(key)
        if not pool:
            return None
        now = time.time()
        candidates = [js for ts, js in pool if now - ts <= CACHE_TTL]
        if not candidates:
            return None
        unseen = [js for js in candidates if js.get("url") not in recent]
        chosen = random.choice(unseen) if unseen else candidates[-1]
        log.info("MemeGen: serving cached meme for %r (%d candidates, %d unseen)",
                  key, len(candidates), len(unseen))
        return chosen

    async def _validate_subreddit(self, name: str) -> bool:
        """Test-fetch a subreddit to confirm it exists and serves memes."""
        if not _SUBREDDIT_RE.match(name):
            return False
        js = await self._fetch_primary(name, True)
        if js is not None:
            return True
        js = await self._fetch_fallback(name, True)
        return js is not None

    def _embed(self, js: dict) -> discord.Embed:
        e = discord.Embed(
            title=(js.get("title") or "Meme")[:256],
            url=js.get("postLink"),
            colour=discord.Colour.blurple(),
        )
        e.set_image(url=js.get("url"))
        sub = js.get("subreddit")
        if sub:
            e.set_footer(text=f"r/{sub}")
        return e

    @staticmethod
    def _error_embed(lang: str) -> discord.Embed:
        return discord.Embed(
            title=tr_lang(lang, "Fehler", "Error"),
            description=tr_lang(
                lang,
                "Keine Meme-Quelle erreichbar. Bitte versuche es später erneut.",
                "No meme source is reachable right now. Please try again later.",
            ),
            colour=discord.Colour.red(),
        )

    @staticmethod
    def _channel_is_nsfw(channel) -> bool:
        checker = getattr(channel, "is_nsfw", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    # ------------------------------------------------------------------ #
    # Auto-post loop
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("MemeGen tick failed")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        now = time.time()
        guilds = await self.config.all_guilds()
        for gid, gconf in guilds.items():
            if not gconf.get("enabled"):
                continue
            interval = int(gconf.get("interval", 0) or 0)
            if interval <= 0 or not gconf.get("channel"):
                continue
            if now - float(gconf.get("last_post", 0)) < interval * 60:
                continue
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            channel = guild.get_channel(gconf.get("channel"))
            if channel is None or not channel.permissions_for(guild.me).send_messages:
                continue
            subs = gconf.get("subreddits") or []
            allow_nsfw = bool(gconf.get("allow_nsfw")) and self._channel_is_nsfw(channel)
            js = await self._fetch(
                random.choice(subs) if subs else None,
                allow_nsfw=allow_nsfw,
                channel_id=channel.id,
            )
            await self.config.guild(guild).last_post.set(now)
            if js:
                try:
                    await channel.send(embed=self._embed(js))
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_command(name="meme")
    @commands.guild_only()
    @app_commands.describe(subreddit="Optional subreddit (e.g. memes, dankmemes)")
    async def meme(self, ctx: commands.Context, subreddit: Optional[str] = None) -> None:
        """Post a random meme."""
        lang = await self._lang(ctx.guild)
        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send(self._t(lang, "Meme-Modul ist deaktiviert.", "Meme module is disabled."))
            return
        await ctx.typing()
        if subreddit is None:
            subs = await self.config.guild(ctx.guild).subreddits()
            subreddit = random.choice(subs) if subs else None
        else:
            subreddit = subreddit.lstrip("r/").strip().lower()
            if not _SUBREDDIT_RE.match(subreddit):
                await ctx.send(self._t(lang, "Ungültiger Subreddit-Name.", "Invalid subreddit name."))
                return
        allow_nsfw = bool(await self.config.guild(ctx.guild).allow_nsfw()) and self._channel_is_nsfw(ctx.channel)
        js = await self._fetch(subreddit, allow_nsfw=allow_nsfw, channel_id=ctx.channel.id)
        if not js:
            await ctx.send(embed=self._error_embed(lang))
            return
        await ctx.send(embed=self._embed(js))

    @commands.hybrid_group(name="memeset")
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def memeset(self, ctx: commands.Context) -> None:
        """Configure the meme module."""

    @memeset.command(name="enable")
    @app_commands.describe(on_off="Enable or disable memes")
    async def m_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Memes **{state}**.", f"Memes **{state}**."))

    @memeset.command(name="channel")
    @app_commands.describe(channel="Auto-post channel (omit to clear)")
    async def m_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Set/clear the auto-post channel."""
        lang = await self._lang(ctx.guild)
        if channel is None:
            await self.config.guild(ctx.guild).channel.clear()
            await ctx.send(self._t(lang, "Auto-Post-Kanal entfernt.", "Auto-post channel cleared."))
            return
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(self._t(lang, f"Auto-Post-Kanal: {channel.mention}", f"Auto-post channel: {channel.mention}"))

    @memeset.command(name="interval")
    @app_commands.describe(minutes="Auto-post interval in minutes (0 = off)")
    async def m_interval(self, ctx: commands.Context, minutes: int) -> None:
        """Set the auto-post interval (minutes; 0 = off)."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).interval.set(max(0, minutes))
        await ctx.send(self._t(lang, f"Intervall: {max(0, minutes)} Min", f"Interval: {max(0, minutes)} min"))

    @memeset.command(name="nsfw")
    @app_commands.describe(on_off="Allow NSFW memes (posted in NSFW channels only)")
    async def m_nsfw(self, ctx: commands.Context, on_off: bool) -> None:
        """Allow or block NSFW memes (they are only ever posted in NSFW channels)."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).allow_nsfw.set(on_off)
        if on_off:
            await ctx.send(self._t(
                lang,
                "NSFW-Memes **erlaubt** — sie werden nur in NSFW-Kanälen gepostet.",
                "NSFW memes **allowed** — they are only posted in NSFW channels.",
            ))
        else:
            await ctx.send(self._t(lang, "NSFW-Memes **blockiert**.", "NSFW memes **blocked**."))

    @memeset.command(name="subreddit")
    @app_commands.describe(name="Subreddit to toggle as a source")
    async def m_subreddit(self, ctx: commands.Context, name: str) -> None:
        """Toggle a subreddit as a meme source (validated with a test fetch)."""
        lang = await self._lang(ctx.guild)
        name = name.lstrip("r/").strip().lower()
        subs = await self.config.guild(ctx.guild).subreddits()
        if name in subs:
            async with self.config.guild(ctx.guild).subreddits() as subs_w:
                if name in subs_w:
                    subs_w.remove(name)
            await ctx.send(self._t(lang, f"r/{name} entfernt.", f"r/{name} removed."))
            return
        await ctx.typing()
        if not await self._validate_subreddit(name):
            await ctx.send(self._t(
                lang,
                f"r/{name} scheint nicht zu existieren oder liefert keine Bilder — nicht hinzugefügt.",
                f"r/{name} does not seem to exist or serves no images — not added.",
            ))
            return
        async with self.config.guild(ctx.guild).subreddits() as subs_w:
            if name not in subs_w:
                subs_w.append(name)
        await ctx.send(self._t(lang, f"r/{name} hinzugefügt (geprüft).", f"r/{name} added (validated)."))

    @memeset.command(name="list")
    async def m_list(self, ctx: commands.Context) -> None:
        """Show the current meme configuration."""
        lang = await self._lang(ctx.guild)
        conf = await self.config.guild(ctx.guild).all()
        ch = ctx.guild.get_channel(conf.get("channel") or 0)
        subs = conf.get("subreddits") or []
        embed = discord.Embed(
            title=self._t(lang, "Meme-Einstellungen", "Meme settings"),
            colour=await ctx.embed_colour(),
        )
        embed.add_field(
            name=self._t(lang, "Status", "Status"),
            value=self._t(lang, "aktiviert", "enabled") if conf.get("enabled") else self._t(lang, "deaktiviert", "disabled"),
        )
        embed.add_field(name=self._t(lang, "Auto-Post-Kanal", "Auto-post channel"), value=ch.mention if ch else "—")
        embed.add_field(
            name=self._t(lang, "Intervall", "Interval"),
            value=f"{conf.get('interval', 0)} min" if conf.get("interval") else "—",
        )
        embed.add_field(name="NSFW", value="✅" if conf.get("allow_nsfw") else "❌")
        embed.add_field(
            name="Subreddits",
            value=", ".join(f"r/{s}" for s in subs) if subs else self._t(lang, "(Standard)", "(default)"),
            inline=False,
        )
        await ctx.send(embed=embed)

    @memeset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def m_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("memegen", L("Memes", "Memes"), mount="guild_settings", permission="guild_admin", order=75)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        subs = await conf.subreddits()
        return PanelSchema(
            description=tr_lang(
                lang,
                "Memes von meme-api.com (Reddit-Fallback). Auto-Post optional über Kanal + Intervall.",
                "Memes from meme-api.com (Reddit fallback). Optional auto-posting via channel + interval.",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.channel("channel", L("Auto-Post-Kanal", "Auto-post channel"), value=str(await conf.channel() or "")),
                Field.number("interval", L("Intervall (Min, 0 = aus)", "Interval (min, 0 = off)"), value=int(await conf.interval())),
                Field.switch("allow_nsfw", L("NSFW erlauben (nur NSFW-Kanäle)", "Allow NSFW (NSFW channels only)"), value=bool(await conf.allow_nsfw())),
                Field.textarea("subreddits", L("Subreddits (eine pro Zeile)", "Subreddits (one per line)"), value="\n".join(subs)),
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
        # Input validation before anything is persisted.
        try:
            interval = int(data.get("interval", 0))
        except (TypeError, ValueError):
            return SubmitResult.fail(tr_lang(lang, "Intervall muss eine Zahl sein.", "Interval must be a number."))
        if interval < 0:
            return SubmitResult.fail(tr_lang(lang, "Intervall darf nicht negativ sein.", "Interval must not be negative."))
        raw = str(data.get("subreddits") or "")
        subs = [ln.strip().lstrip("r/").lower() for ln in raw.splitlines() if ln.strip()]
        invalid = [s for s in subs if not _SUBREDDIT_RE.match(s)]
        if invalid:
            return SubmitResult.fail(tr_lang(
                lang,
                "Ungültige Subreddit-Namen: " + ", ".join(invalid),
                "Invalid subreddit names: " + ", ".join(invalid),
            ))
        await conf.enabled.set(bool(data.get("enabled")))
        ch = str(data.get("channel") or "").strip()
        await (conf.channel.set(int(ch)) if ch.isdigit() else conf.channel.clear())
        await conf.interval.set(interval)
        await conf.allow_nsfw.set(bool(data.get("allow_nsfw")))
        await conf.subreddits.set(subs)
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))
