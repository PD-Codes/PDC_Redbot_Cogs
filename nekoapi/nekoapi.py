"""NekoAPI — anime images by content rating from api.nekosapi.com.

Ratings other than ``safe`` are only allowed in NSFW channels. Invalid ratings
are rejected explicitly (no silent fallback). Robust external API handling:
timeout, retry with exponential backoff, TTL cache as fallback and a small
per-channel LRU to avoid repeating images. Bilingual (DE/EN, default en-US).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from redbot.core import Config, commands

from .pdc_dashboard import (
    register_dashboard, unregister_dashboard,
    dashboard_panel, PanelSchema, Field, SubmitResult,
    L, tr, tr_lang,
)

log = logging.getLogger("red.pdc.nekoapi")

BASE_URL = "https://api.nekosapi.com/v4/images/random?limit=1&rating="

VALID_RATINGS = ["safe", "suggestive", "borderline", "explicit"]
NSFW_RATINGS = {"suggestive", "borderline", "explicit"}

CACHE_TTL = 300  # seconds a cached response stays valid as fallback
RECENT_PER_CHANNEL = 15
HTTP_RETRIES = 3
HTTP_TIMEOUT = 10  # seconds


class NekoAPI(commands.Cog):
    """Show NekoAPI images by rating."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xD0DE20253, force_registration=True)
        self.config.register_guild(language="en-US")
        self._session: Optional[aiohttp.ClientSession] = None
        # rating -> (timestamp, item) — last good result as fallback
        self._cache: Dict[str, Tuple[float, dict]] = {}
        # channel_id -> recently shown image URLs
        self._recent: Dict[int, Deque[str]] = {}

    async def cog_load(self) -> None:
        register_dashboard(self)

    async def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._session and not self._session.closed:
            await self._session.close()

    async def _lang(self, ctx) -> str:
        guild = getattr(ctx, "guild", None)
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    @staticmethod
    def _channel_is_nsfw(channel) -> bool:
        """True if suggestive/borderline/explicit content may be posted here."""
        checker = getattr(channel, "is_nsfw", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        # DMs without an is_nsfw() implementation: treat as not NSFW-safe.
        return False

    # ------------------------------------------------------------------
    # Dashboard: per-guild output language
    # ------------------------------------------------------------------
    @dashboard_panel(
        "language", L("Sprache", "Language"),
        mount="guild_settings", permission="guild_admin", order=99,
    )
    async def settings_panel(self, ctx):
        return PanelSchema(
            description=tr(
                ctx,
                "Sprache der Bot-Ausgaben für diesen Server.",
                "Output language for this server.",
            ),
            fields=[
                Field.select(
                    "language", L("Sprache", "Language"),
                    [
                        {"value": "de-DE", "label": "Deutsch"},
                        {"value": "en-US", "label": "English"},
                    ],
                    value=str(await self.config.guild(ctx.guild).language()),
                    reload_on_change=True,
                )
            ],
        )

    @settings_panel.on_submit
    async def _save_settings(self, ctx, data):
        lang = str(data.get("language", "en-US")).strip()
        if lang not in ("de-DE", "en-US"):
            lang = "en-US"
        await self.config.guild(ctx.guild).language.set(lang)
        return SubmitResult.ok(tr(ctx, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------
    # Admin: per-guild output language via command
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="nekoapiset-language")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def nekoapiset_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language of the NekoAPI cog for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(tr_lang(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------
    # HTTP helpers (timeout, retry + backoff, cache fallback)
    # ------------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
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
                        log.warning("nekosapi returned %s (attempt %s/%s)", resp.status, attempt, HTTP_RETRIES)
                    else:
                        log.warning("nekosapi returned %s for %s", resp.status, url)
                        return None
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("nekosapi request failed (attempt %s/%s)", attempt, HTTP_RETRIES)
            if attempt < HTTP_RETRIES:
                await asyncio.sleep(delay)
                delay *= 2
        return None

    async def fetch_image(self, rating: str, channel_id: Optional[int] = None) -> Optional[dict]:
        """Fetch one image object for a rating, avoiding recently shown URLs."""
        recent = self._recent.setdefault(channel_id or 0, deque(maxlen=RECENT_PER_CHANNEL))
        item = None
        for _ in range(4):  # a few tries to find an unseen image
            data = await self._get_json(BASE_URL + rating)
            if isinstance(data, dict):
                # Some API versions wrap the result: {"items": [...]}
                data = data.get("items") or data.get("results") or []
            if data:
                candidate = data[0]
                self._cache[rating] = (time.time(), candidate)
                item = candidate
                if candidate.get("url") not in recent:
                    break
            else:
                break
        if item is None:
            cached = self._cache.get(rating)
            if cached and time.time() - cached[0] <= CACHE_TTL:
                item = cached[1]
                log.info("Serving cached nekosapi result for rating %r", rating)
        if item is not None and item.get("url"):
            recent.append(item["url"])
        return item

    @staticmethod
    def _error_embed(lang: str) -> discord.Embed:
        return discord.Embed(
            title=tr_lang(lang, "Fehler", "Error"),
            description=tr_lang(
                lang,
                "Die NekoAPI ist gerade nicht erreichbar. Bitte versuche es später erneut.",
                "The NekoAPI is currently unreachable. Please try again later.",
            ),
            color=0xFF0000,
        )

    async def build_embed(self, info: dict) -> discord.Embed:
        embed = discord.Embed(
            title=f"NekoAPI – {info.get('rating', '?')}",
            color=0xFF66CC,
        )
        embed.set_image(url=info["url"])
        embed.set_footer(text=f"ID: {info.get('id', '?')} | Rating: {info.get('rating', '?')}")
        return embed

    def _rating_gate(self, lang: str, rating: str, channel) -> Optional[str]:
        """Validate rating + channel. Returns an error message or None if OK."""
        if rating not in VALID_RATINGS:
            return tr_lang(
                lang,
                f"❌ Ungültiges Rating!\nErlaubt: {', '.join(VALID_RATINGS)}",
                f"❌ Invalid rating!\nAllowed: {', '.join(VALID_RATINGS)}",
            )
        if rating in NSFW_RATINGS and not self._channel_is_nsfw(channel):
            return tr_lang(
                lang,
                f"❌ `{rating}` ist nur in NSFW-Kanälen erlaubt.",
                f"❌ `{rating}` is only allowed in NSFW channels.",
            )
        return None

    # ------------------------------------------------------------------
    # Prefix command
    # ------------------------------------------------------------------
    @commands.command(
        name="nekoapi",
        extras={"i18n_desc": {
            "de-DE": "Zeigt ein Bild nach Rating (Standard = safe).",
            "en-US": "Show an image by rating (default = safe).",
        }},
    )
    async def nekoapi_prefix(self, ctx, rating: str = "safe"):
        """Show an image by rating (default = safe)."""
        lang = await self._lang(ctx)
        rating = rating.lower()

        error = self._rating_gate(lang, rating, ctx.channel)
        if error:
            return await ctx.send(error)

        info = await self.fetch_image(rating, channel_id=ctx.channel.id)
        if not info or not info.get("url"):
            return await ctx.send(embed=self._error_embed(lang))

        embed = await self.build_embed(info)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Slash: /nekoapi (fixed rating "safe")
    # ------------------------------------------------------------------
    @app_commands.command(
        name="nekoapi",
        description="Show a random image (rating = safe).",
        extras={"i18n_desc": {
            "de-DE": "Zeigt ein Bild nach Rating (Standard = safe).",
            "en-US": "Show an image by rating (default = safe).",
        }},
    )
    async def nekoapi_slash_safe(self, interaction: discord.Interaction):
        """Send a random safe-for-work anime image from the Nekosapi service."""
        await interaction.response.defer()
        lang = await self._lang(interaction)

        info = await self.fetch_image("safe", channel_id=interaction.channel_id)
        if not info or not info.get("url"):
            return await interaction.followup.send(embed=self._error_embed(lang))

        embed = await self.build_embed(info)
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # Autocomplete for rating
    # ------------------------------------------------------------------
    async def rating_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.lower()
        return [
            app_commands.Choice(name=r, value=r)
            for r in VALID_RATINGS
            if current in r
        ]

    # ------------------------------------------------------------------
    # Slash: /nekoapi-rating <rating>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="nekoapi-rating",
        description="Show an image with the selected rating.",
        extras={"i18n_desc": {
            "de-DE": "Zeigt ein Bild mit dem gewählten Rating.",
            "en-US": "Show an image with the selected rating.",
        }},
    )
    @app_commands.describe(rating="Choose a rating")
    @app_commands.autocomplete(rating=rating_autocomplete)
    async def nekoapi_slash_rating(self, interaction: discord.Interaction, rating: str):
        """Send a random anime image filtered by content rating from the Nekosapi service."""
        rating = rating.lower()

        await interaction.response.defer()
        lang = await self._lang(interaction)

        error = self._rating_gate(lang, rating, interaction.channel)
        if error:
            return await interaction.followup.send(error)

        info = await self.fetch_image(rating, channel_id=interaction.channel_id)
        if not info or not info.get("url"):
            return await interaction.followup.send(embed=self._error_embed(lang))

        embed = await self.build_embed(info)
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(NekoAPI(bot))
