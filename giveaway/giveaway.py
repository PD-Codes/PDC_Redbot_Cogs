"""Giveaway — button-based giveaways with embed cards, auto draw and reroll.

Entry is a single click on a **persistent** button (survives bot restarts).
Bilingual output (DE/EN). Web dashboard integration (enable + language + a live
list of running giveaways) via the resilient drop-in.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
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
    dashboard_panel,
    register_dashboard,
    tr_lang,
    unregister_dashboard,
)

log = logging.getLogger("red.pdc.giveaway")

_ENTER_ID = "pdc_giveaway_enter"
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> Optional[int]:
    """Parse e.g. ``1h``, ``30m``, ``2d12h`` -> seconds (None if invalid)."""
    total = 0
    found = False
    for num, unit in re.findall(r"(\d+)\s*([smhd])", (text or "").lower()):
        found = True
        total += int(num) * _UNITS[unit]
    return total if found and total > 0 else None


class GiveawayView(discord.ui.View):
    """Persistent view holding the single Enter button."""

    def __init__(self, cog: "Giveaway") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🎉 Enter", style=discord.ButtonStyle.primary, custom_id=_ENTER_ID)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._handle_enter(interaction)


class Giveaway(commands.Cog):
    """Button-based giveaways with embed cards, auto draw and reroll."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x617_E_A1, force_registration=True)
        self.config.register_guild(enabled=True, language="en-US", giveaways=[])
        # giveaway: {id, channel, message, prize, winners, end, host, entrants[], ended, won[]}
        self._view: Optional[GiveawayView] = None
        self._task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        register_dashboard(self)
        self._view = GiveawayView(self)
        self.bot.add_view(self._view)  # persistent: handles clicks after restarts
        self._task = asyncio.create_task(self._loop())

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._task:
            self._task.cancel()
        if self._view:
            self._view.stop()

    @staticmethod
    def _t(lang: str, de: str, en: str) -> str:
        return de if str(lang).lower().startswith("de") else en

    async def _lang(self, guild: Optional[discord.Guild]) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    # ------------------------------------------------------------------ #
    # Embeds
    # ------------------------------------------------------------------ #
    def _embed(self, guild, gw, lang: str) -> discord.Embed:
        host = guild.get_member(gw.get("host"))
        ended = gw.get("ended")
        colour = discord.Colour.dark_grey() if ended else discord.Colour.gold()
        e = discord.Embed(title=f"🎉 {gw.get('prize', '')}", colour=colour)
        if ended:
            won = gw.get("won", [])
            if won:
                names = ", ".join(f"<@{w}>" for w in won)
                e.description = self._t(lang, f"**Gewinner:** {names}", f"**Winner(s):** {names}")
            else:
                e.description = self._t(lang, "Keine gültigen Teilnahmen.", "No valid entries.")
        else:
            e.description = self._t(
                lang,
                f"Klick auf **🎉 Teilnehmen**!\nGewinner: **{gw.get('winners', 1)}** · Endet <t:{int(gw.get('end', 0))}:R>",
                f"Click **🎉 Enter**!\nWinners: **{gw.get('winners', 1)}** · Ends <t:{int(gw.get('end', 0))}:R>",
            )
        e.set_footer(text=self._t(lang, f"Veranstaltet von {host}", f"Hosted by {host}") if host else "Giveaway")
        return e

    # ------------------------------------------------------------------ #
    # Entry handler (persistent button)
    # ------------------------------------------------------------------ #
    async def _handle_enter(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or interaction.message is None:
            return
        lang = await self._lang(guild)
        joined = None
        async with self.config.guild(guild).giveaways() as gws:
            gw = next((g for g in gws if g.get("message") == interaction.message.id and not g.get("ended")), None)
            if gw is None:
                await interaction.response.send_message(
                    self._t(lang, "Dieses Giveaway ist beendet.", "This giveaway has ended."), ephemeral=True
                )
                return
            uid = interaction.user.id
            entrants = gw.setdefault("entrants", [])
            if uid in entrants:
                entrants.remove(uid)
                joined = False
            else:
                entrants.append(uid)
                joined = True
        msg = (
            self._t(lang, "Du bist dabei! 🎉", "You're in! 🎉")
            if joined
            else self._t(lang, "Teilnahme zurückgezogen.", "Entry withdrawn.")
        )
        await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------------ #
    # Drawing / loop
    # ------------------------------------------------------------------ #
    @staticmethod
    def _draw(entrants: List[int], count: int) -> List[int]:
        if not entrants:
            return []
        return random.sample(entrants, min(max(1, count), len(entrants)))

    async def _finish(self, guild, gw, lang: str) -> None:
        gw["ended"] = True
        gw["won"] = self._draw(gw.get("entrants", []), gw.get("winners", 1))
        channel = guild.get_channel(gw.get("channel"))
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(gw.get("message"))
            await msg.edit(embed=self._embed(guild, gw, lang), view=None)
        except discord.HTTPException:
            pass
        if gw["won"]:
            mentions = ", ".join(f"<@{w}>" for w in gw["won"])
            try:
                await channel.send(
                    self._t(lang, f"🎉 Glückwunsch {mentions} — du gewinnst **{gw.get('prize','')}**!",
                            f"🎉 Congratulations {mentions} — you won **{gw.get('prize','')}**!")
                )
            except discord.HTTPException:
                pass

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Giveaway tick failed")
            await asyncio.sleep(20)

    async def _tick(self) -> None:
        now = time.time()
        for guild in self.bot.guilds:
            lang = await self.config.guild(guild).language()
            async with self.config.guild(guild).giveaways() as gws:
                due = [g for g in gws if not g.get("ended") and g.get("end", 0) <= now]
                for gw in due:
                    await self._finish(guild, gw, lang)
                # Prune giveaways ended more than 2 days ago.
                cutoff = now - 172800
                gws[:] = [g for g in gws if not (g.get("ended") and g.get("end", 0) < cutoff)]

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="giveaway", aliases=["gw"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def giveaway(self, ctx: commands.Context) -> None:
        """Run and manage giveaways."""

    @giveaway.command(name="start")
    @app_commands.describe(
        channel="Channel to post the giveaway in",
        duration="Duration, e.g. 1h, 30m, 2d12h",
        winners="Number of winners",
        prize="What is being given away",
    )
    async def gw_start(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        duration: str,
        winners: int,
        *,
        prize: str,
    ) -> None:
        """Start a giveaway."""
        lang = await self._lang(ctx.guild)
        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send(self._t(lang, "Giveaway-Modul ist deaktiviert.", "Giveaway module is disabled."))
            return
        secs = parse_duration(duration)
        if secs is None:
            await ctx.send(self._t(lang, "Ungültige Dauer. Beispiel: `1h`, `30m`, `2d`.", "Invalid duration. Example: `1h`, `30m`, `2d`."))
            return
        if not channel.permissions_for(ctx.guild.me).send_messages:
            await ctx.send(self._t(lang, "Keine Senderechte in dem Kanal.", "I can't send messages in that channel."))
            return
        gw = {
            "id": uuid.uuid4().hex[:8],
            "channel": channel.id,
            "message": None,
            "prize": prize.strip()[:240],
            "winners": max(1, winners),
            "end": time.time() + secs,
            "host": ctx.author.id,
            "entrants": [],
            "ended": False,
            "won": [],
        }
        msg = await channel.send(embed=self._embed(ctx.guild, gw, lang), view=self._view or GiveawayView(self))
        gw["message"] = msg.id
        async with self.config.guild(ctx.guild).giveaways() as gws:
            gws.append(gw)
        await ctx.send(self._t(lang, f"Giveaway gestartet in {channel.mention} (ID `{gw['id']}`).", f"Giveaway started in {channel.mention} (ID `{gw['id']}`)."))

    @giveaway.command(name="end")
    @app_commands.describe(giveaway_id="The giveaway ID (from 'giveaway list')")
    async def gw_end(self, ctx: commands.Context, giveaway_id: str) -> None:
        """End a running giveaway early and draw now."""
        lang = await self._lang(ctx.guild)
        done = False
        async with self.config.guild(ctx.guild).giveaways() as gws:
            gw = next((g for g in gws if g.get("id") == giveaway_id and not g.get("ended")), None)
            if gw is not None:
                await self._finish(ctx.guild, gw, lang)
                done = True
        await ctx.send(self._t(lang, "Beendet & ausgelost." if done else "Nicht gefunden.", "Ended & drawn." if done else "Not found."))

    @giveaway.command(name="reroll")
    @app_commands.describe(giveaway_id="The giveaway ID")
    async def gw_reroll(self, ctx: commands.Context, giveaway_id: str) -> None:
        """Reroll the winners of an ended giveaway."""
        lang = await self._lang(ctx.guild)
        winners = None
        async with self.config.guild(ctx.guild).giveaways() as gws:
            gw = next((g for g in gws if g.get("id") == giveaway_id and g.get("ended")), None)
            if gw is not None:
                gw["won"] = self._draw(gw.get("entrants", []), gw.get("winners", 1))
                winners = gw["won"]
                channel = ctx.guild.get_channel(gw.get("channel"))
                if channel is not None:
                    try:
                        m = await channel.fetch_message(gw.get("message"))
                        await m.edit(embed=self._embed(ctx.guild, gw, lang))
                    except discord.HTTPException:
                        pass
        if winners is None:
            await ctx.send(self._t(lang, "Nicht gefunden (oder läuft noch).", "Not found (or still running)."))
        elif winners:
            await ctx.send(self._t(lang, f"Neue Gewinner: {', '.join(f'<@{w}>' for w in winners)}", f"New winner(s): {', '.join(f'<@{w}>' for w in winners)}"))
        else:
            await ctx.send(self._t(lang, "Keine Teilnehmer.", "No entries."))

    @giveaway.command(name="list")
    async def gw_list(self, ctx: commands.Context) -> None:
        """List running giveaways."""
        lang = await self._lang(ctx.guild)
        gws = [g for g in await self.config.guild(ctx.guild).giveaways() if not g.get("ended")]
        if not gws:
            await ctx.send(self._t(lang, "Keine laufenden Giveaways.", "No running giveaways."))
            return
        lines = []
        for g in gws:
            ch = ctx.guild.get_channel(g.get("channel"))
            lines.append(f"`{g.get('id')}` · {ch.mention if ch else '?'} · **{g.get('prize')}** · {len(g.get('entrants', []))} 👤 · <t:{int(g.get('end', 0))}:R>")
        await ctx.send(embed=discord.Embed(
            title=self._t(lang, "Laufende Giveaways", "Running giveaways"),
            description="\n".join(lines)[:4000],
            colour=await ctx.embed_colour(),
        ))

    @commands.hybrid_group(name="giveawayset", aliases=["gwset"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def giveawayset(self, ctx: commands.Context) -> None:
        """Configure the giveaway module."""

    @giveawayset.command(name="enable")
    @app_commands.describe(on_off="Enable or disable giveaways")
    async def gws_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Giveaways **{state}**.", f"Giveaways **{state}**."))

    @giveawayset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def gws_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("giveaway", L("Giveaways", "Giveaways"), mount="guild_settings", permission="guild_admin", order=55)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        running = [g for g in await conf.giveaways() if not g.get("ended")]
        listing = "\n".join(
            f"• `{g.get('id')}` {g.get('prize')} — {len(g.get('entrants', []))} 👤"
            for g in running
        ) or "—"
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Giveaways per Befehl `giveaway start` starten.\nLaufend:\n{listing}",
                f"Start giveaways with `giveaway start`.\nRunning:\n{listing}",
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
