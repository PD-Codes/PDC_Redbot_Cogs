import discord
import aiohttp
from discord import app_commands
from redbot.core import commands, Config

from .pdc_dashboard import (
    register_dashboard, unregister_dashboard,
    dashboard_panel, PanelSchema, Field, SubmitResult,
    L, tr, tr_lang,
)

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


class Neko(commands.Cog):
    """Show neko images and GIFs from nekos.best."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xD0DE20252, force_registration=True)
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
    async def fetch_and_build_embed(self, category: str, lang: str = "en-US"):
        url = BASE_URL + category

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return discord.Embed(
                        title=tr_lang(lang, "Fehler", "Error"),
                        description=tr_lang(lang, "Konnte keine Daten abrufen.", "Could not fetch data."),
                        color=0xFF0000
                    )
                data = await resp.json()

        result = data["results"][0]
        img = result["url"]
        artist = result.get("artist_name", tr_lang(lang, "Unbekannt", "Unknown"))
        source = result.get("source_url", tr_lang(lang, "Keine Quelle", "No source"))

        embed = discord.Embed(
            title=f"{category.capitalize()}",
            color=0xFF66CC
        )
        embed.set_image(url=img)
        embed.set_footer(text=f"Artist: {artist} | Source: {source}")

        return embed

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
    async def neko_prefix(self, ctx, category: str = None):
        """Show a neko or an image/GIF from the given category."""

        lang = await self._lang(ctx)

        # No parameter → always category "neko"
        if category is None:
            embed = await self.fetch_and_build_embed("neko", lang)
            return await ctx.send(embed=embed)

        category = category.lower()

        if category not in ALL_CATEGORIES:
            return await ctx.send(
                tr_lang(
                    lang,
                    f"❌ Ungültige Kategorie!\nVerfügbar: `{', '.join(ALL_CATEGORIES)}`",
                    f"❌ Invalid category!\nAvailable: `{', '.join(ALL_CATEGORIES)}`",
                )
            )

        embed = await self.fetch_and_build_embed(category, lang)
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
        embed = await self.fetch_and_build_embed("neko", lang)
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
        embed = await self.fetch_and_build_embed(category, lang)
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Neko(bot))
