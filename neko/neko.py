"""Neko — anime images and reaction GIFs from nekos.best.

User commands fetch a random image/GIF per category. Robust external API
handling: timeout, retry with exponential backoff, TTL cache as fallback and
a small per-channel LRU so the same image is not repeated within a session.
Bilingual output (DE/EN, default en-US).
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

log = logging.getLogger("red.pdc.neko")

BASE_URL = "https://nekos.best/api/v2/"

IMAGE_CATEGORIES = [
    "husbando", "kitsune", "neko", "waifu"
]

GIF_CATEGORIES = [
    "angry", "baka", "bite", "blush", "bored", "cry", "cuddle", "dance", "facepalm",
    "feed", "handhold", "handshake", "happy", "highfive", "hug", "kick", "kiss",
    "laugh", "lurk", "nod", "nom", "nope", "pat", "peck", "poke", "pout", "punch",
    "run", "shoot", "shrug", "slap", "sleep", "smile", "smug", "stare", "think",
    "thumbsup", "tickle", "wave", "wink", "yawn", "yeet"
]

ALL_CATEGORIES = IMAGE_CATEGORIES + GIF_CATEGORIES

CACHE_TTL = 300  # seconds a cached response stays valid as fallback
RECENT_PER_CHANNEL = 15  # LRU size for de-duplication per channel
HTTP_RETRIES = 3
HTTP_TIMEOUT = 10  # seconds


class Neko(commands.Cog):
    """Show neko images and GIFs from nekos.best."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xD0DE20252, force_registration=True)
        self.config.register_guild(language="en-US")
        self._session: Optional[aiohttp.ClientSession] = None
        # category -> (timestamp, payload) — last good API response as fallback
        self._cache: Dict[str, Tuple[float, dict]] = {}
        # channel_id -> recently shown image URLs (avoid repeats in a session)
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

    # ------------------------------------------------------------------
    # HTTP helpers (timeout, retry + backoff, cache fallback)
    # ------------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
            )
        return self._session

    async def _get_json(self, url: str) -> Optional[dict]:
        """GET a JSON document with retries and exponential backoff on 429/5xx."""
        session = await self._get_session()
        delay = 1.0
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 429 or resp.status >= 500:
                        log.warning("nekos.best returned %s (attempt %s/%s)", resp.status, attempt, HTTP_RETRIES)
                    else:
                        log.warning("nekos.best returned %s for %s", resp.status, url)
                        return None
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("nekos.best request failed (attempt %s/%s)", attempt, HTTP_RETRIES)
            if attempt < HTTP_RETRIES:
                await asyncio.sleep(delay)
                delay *= 2
        return None

    async def _fetch_result(self, category: str, channel_id: Optional[int]) -> Optional[dict]:
        """Fetch one result for a category, avoiding recently shown URLs."""
        recent = self._recent.setdefault(channel_id or 0, deque(maxlen=RECENT_PER_CHANNEL))
        result = None
        for _ in range(4):  # a few tries to find an unseen image
            data = await self._get_json(BASE_URL + category)
            if data and data.get("results"):
                self._cache[category] = (time.time(), data)
                candidate = data["results"][0]
                result = candidate
                if candidate.get("url") not in recent:
                    break
            else:
                break
        if result is None:
            # Fall back to the last good cached response for this category.
            ts_data = self._cache.get(category)
            if ts_data and time.time() - ts_data[0] <= CACHE_TTL:
                results = ts_data[1].get("results") or []
                result = results[0] if results else None
                if result is not None:
                    log.info("Serving cached nekos.best result for %r", category)
        if result is not None and result.get("url"):
            recent.append(result["url"])
        return result

    @staticmethod
    def _error_embed(lang: str) -> discord.Embed:
        return discord.Embed(
            title=tr_lang(lang, "Fehler", "Error"),
            description=tr_lang(
                lang,
                "Die nekos.best-API ist gerade nicht erreichbar. Bitte versuche es später erneut.",
                "The nekos.best API is currently unreachable. Please try again later.",
            ),
            color=0xFF0000,
        )

    async def fetch_and_build_embed(self, category: str, lang: str = "en-US",
                                    channel_id: Optional[int] = None) -> discord.Embed:
        result = await self._fetch_result(category, channel_id)
        if not result or not result.get("url"):
            return self._error_embed(lang)

        artist = result.get("artist_name") or tr_lang(lang, "Unbekannt", "Unknown")
        source = result.get("source_url") or tr_lang(lang, "Keine Quelle", "No source")

        embed = discord.Embed(title=category.capitalize(), color=0xFF66CC)
        embed.set_image(url=result["url"])
        embed.set_footer(text=f"Artist: {artist} | Source: {source}")
        return embed

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
    @commands.hybrid_command(name="nekoset-language")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def nekoset_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language of the Neko cog for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(tr_lang(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------
    # Prefix command: !neko → only category "neko"
    # Prefix command: !neko <category> → any category
    # ------------------------------------------------------------------
    @commands.command(
        name="neko",
        extras={"i18n_desc": {
            "de-DE": "Zeigt ein Neko-Bild oder ein Bild/GIF aus der angegebenen Kategorie.",
            "en-US": "Show a neko or an image/GIF from the given category.",
        }},
    )
    async def neko_prefix(self, ctx, category: Optional[str] = None):
        """Show a neko or an image/GIF from the given category."""
        lang = await self._lang(ctx)

        # No parameter → always category "neko"
        if category is None:
            category = "neko"

        category = category.lower()
        if category not in ALL_CATEGORIES:
            return await ctx.send(
                tr_lang(
                    lang,
                    f"❌ Ungültige Kategorie!\nVerfügbar: `{', '.join(ALL_CATEGORIES)}`",
                    f"❌ Invalid category!\nAvailable: `{', '.join(ALL_CATEGORIES)}`",
                )
            )

        embed = await self.fetch_and_build_embed(category, lang, channel_id=ctx.channel.id)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Autocomplete function
    # ------------------------------------------------------------------
    async def neko_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.lower()
        suggestions = [
            app_commands.Choice(name=cat, value=cat)
            for cat in ALL_CATEGORIES
            if current in cat.lower()
        ]
        return suggestions[:25]

    # ------------------------------------------------------------------
    # Slash command: /neko → only category "neko"
    # ------------------------------------------------------------------
    @app_commands.command(
        name="neko",
        description="Show a neko image.",
        extras={"i18n_desc": {
            "de-DE": "Zeigt ein Neko-Bild oder ein Bild/GIF aus der angegebenen Kategorie.",
            "en-US": "Show a neko or an image/GIF from the given category.",
        }},
    )
    async def neko_slash(self, interaction: discord.Interaction):
        """Send a random anime image from the Nekos.best API."""
        await interaction.response.defer()
        lang = await self._lang(interaction)
        channel_id = interaction.channel_id
        embed = await self.fetch_and_build_embed("neko", lang, channel_id=channel_id)
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # Slash command: /neko-cat <category> → any category
    # ------------------------------------------------------------------
    @app_commands.command(
        name="neko-cat",
        description="Show an image or GIF from a category.",
        extras={"i18n_desc": {
            "de-DE": "Zeigt ein Bild oder GIF aus einer Kategorie.",
            "en-US": "Show an image or GIF from a category.",
        }},
    )
    @app_commands.describe(category="Choose a category")
    @app_commands.autocomplete(category=neko_autocomplete)
    async def neko_cat_slash(self, interaction: discord.Interaction, category: str):
        """Send a random category image from the Nekos.best API."""
        await interaction.response.defer()
        lang = await self._lang(interaction)
        category = category.lower()
        if category not in ALL_CATEGORIES:
            return await interaction.followup.send(
                tr_lang(
                    lang,
                    f"❌ Ungültige Kategorie!\nVerfügbar: `{', '.join(ALL_CATEGORIES)}`",
                    f"❌ Invalid category!\nAvailable: `{', '.join(ALL_CATEGORIES)}`",
                )
            )
        embed = await self.fetch_and_build_embed(category, lang, channel_id=interaction.channel_id)
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Neko(bot))
