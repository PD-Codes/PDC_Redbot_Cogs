import discord
import aiohttp
from discord import app_commands
from redbot.core import commands, Config

from .pdc_dashboard import (
    register_dashboard, unregister_dashboard,
    dashboard_panel, PanelSchema, Field, SubmitResult,
    L, tr, tr_lang,
)


BASE_URL = "https://api.nekosapi.com/v4/images/random?limit=1&rating="

VALID_RATINGS = ["safe", "suggestive", "borderline", "explicit"]


class NekoAPI(commands.Cog):
    """Show NekoAPI images by rating."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xD0DE20253, force_registration=True)
        self.config.register_guild(language="en-US")

    async def cog_load(self) -> None:
        register_dashboard(self)

    def cog_unload(self) -> None:
        unregister_dashboard(self)

    async def _lang(self, ctx) -> str:
        guild = getattr(ctx, "guild", None)
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

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
        lang = str(data.get("language", "en-US")).strip() or "en-US"
        await self.config.guild(ctx.guild).language.set(lang)
        return SubmitResult.ok(tr(ctx, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------
    # Helper: API Request + Embed Builder
    # ------------------------------------------------------------------
    async def fetch_image(self, rating: str):
        async with aiohttp.ClientSession() as session:
            async with session.get(BASE_URL + rating) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                if not data:
                    return None

                return data[0]  # Object contains id, url, rating

    async def build_embed(self, info: dict):
        embed = discord.Embed(
            title=f"NekoAPI – {info['rating']}",
            color=0xFF66CC
        )
        embed.set_image(url=info["url"])
        embed.set_footer(text=f"ID: {info['id']} | Rating: {info['rating']}")
        return embed

    # ------------------------------------------------------------------
    # Prefix Command
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

        if rating not in VALID_RATINGS:
            return await ctx.send(
                tr_lang(
                    lang,
                    f"❌ Ungültiges Rating!\nErlaubt: {', '.join(VALID_RATINGS)}",
                    f"❌ Invalid rating!\nAllowed: {', '.join(VALID_RATINGS)}",
                )
            )

        # NSFW check for explicit
        if rating == "explicit" and not ctx.channel.is_nsfw():
            return await ctx.send(
                tr_lang(
                    lang,
                    "❌ `explicit` ist nur in NSFW-Channels erlaubt.",
                    "❌ `explicit` is only allowed in NSFW channels.",
                )
            )

        info = await self.fetch_image(rating)
        if not info:
            return await ctx.send(
                tr_lang(lang, "❌ Fehler beim Abrufen der API.", "❌ Error fetching from the API.")
            )

        embed = await self.build_embed(info)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Slash: /nekoapi (fix safe)
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

        info = await self.fetch_image("safe")
        if not info:
            return await interaction.followup.send(
                tr_lang(lang, "❌ Fehler beim Abrufen der API.", "❌ Error fetching from the API.")
            )

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

        if rating not in VALID_RATINGS:
            return await interaction.followup.send(
                tr_lang(
                    lang,
                    f"❌ Ungültiges Rating! Erlaubt: {', '.join(VALID_RATINGS)}",
                    f"❌ Invalid rating! Allowed: {', '.join(VALID_RATINGS)}",
                )
            )

        # NSFW check for explicit
        if rating == "explicit" and not interaction.channel.is_nsfw():
            return await interaction.followup.send(
                tr_lang(
                    lang,
                    "❌ `explicit` ist nur in NSFW-Channels erlaubt.",
                    "❌ `explicit` is only allowed in NSFW channels.",
                )
            )

        info = await self.fetch_image(rating)
        if not info:
            return await interaction.followup.send(
                tr_lang(lang, "❌ Fehler beim Abrufen der API.", "❌ Error fetching from the API.")
            )

        embed = await self.build_embed(info)
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(NekoAPI(bot))
