"""Giveaway — button-based giveaways with embed cards, auto draw and reroll.

Entry is a single click on a **persistent** button (survives bot restarts).
Bilingual output (DE/EN, default en-US). Optional entry requirements (required
role, minimum account age, minimum server membership) are checked on entry.
Winners are announced in the channel and additionally notified via DM (with the
channel mention as fallback when DMs are closed). Ended giveaways are cleaned up
after a configurable number of days. Web dashboard integration (settings panel +
overview page) via the resilient drop-in.
"""
from __future__ import annotations

import asyncio
import datetime
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

try:
    from redbot.core.utils.views import SimpleMenu
except Exception:  # pragma: no cover - older Red versions
    SimpleMenu = None  # type: ignore

from .pdc_dashboard import (
    Component,
    Field,
    L,
    PageSchema,
    PanelSchema,
    SubmitResult,
    dashboard_page,
    dashboard_panel,
    register_dashboard,
    tr_lang,
    unregister_dashboard,
)

log = logging.getLogger("red.pdc.giveaway")  # module logger

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
        self.config.register_guild(
            enabled=True,
            language="en-US",
            giveaways=[],
            # -- new keys (backward compatible defaults) --------------------- #
            dm_winners=True,  # DM winners (channel mention remains as fallback)
            cleanup_days=2,  # prune ended giveaways after N days
            req_role=None,  # default entry requirement: role ID
            min_account_days=0,  # default entry requirement: account age in days
            min_join_days=0,  # default entry requirement: days in this server
        )
        # giveaway: {id, channel, message, prize, winners, end, host, entrants[],
        #            ended, won[], req_role, min_account_days, min_join_days}
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

    async def _send_pages(self, ctx: commands.Context, pages: List[discord.Embed]) -> None:
        """Send one or more embed pages, with a paginated menu when possible."""
        if not pages:
            return
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
            return
        if SimpleMenu is not None:
            await SimpleMenu(pages).start(ctx)
        else:  # fallback: send the first few pages directly
            for page in pages[:3]:
                await ctx.send(embed=page)

    # ------------------------------------------------------------------ #
    # Embeds
    # ------------------------------------------------------------------ #
    def _requirements_text(self, guild, gw, lang: str) -> str:
        """Human-readable entry requirements for the giveaway embed."""
        parts = []
        role_id = gw.get("req_role")
        role = guild.get_role(int(role_id)) if role_id else None
        if role is not None:
            parts.append(self._t(lang, f"Rolle {role.mention}", f"Role {role.mention}"))
        if int(gw.get("min_account_days") or 0) > 0:
            n = int(gw["min_account_days"])
            parts.append(self._t(lang, f"Account ≥ {n} Tage", f"Account ≥ {n} days"))
        if int(gw.get("min_join_days") or 0) > 0:
            n = int(gw["min_join_days"])
            parts.append(self._t(lang, f"Mitglied ≥ {n} Tage", f"Member ≥ {n} days"))
        return " · ".join(parts)

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
            req = self._requirements_text(guild, gw, lang)
            if req:
                e.add_field(name=self._t(lang, "Voraussetzungen", "Requirements"), value=req, inline=False)
        e.set_footer(text=self._t(lang, f"Veranstaltet von {host}", f"Hosted by {host}") if host else "Giveaway")
        return e

    # ------------------------------------------------------------------ #
    # Entry handler (persistent button)
    # ------------------------------------------------------------------ #
    def _eligibility_error(self, member: discord.Member, gw: dict, lang: str) -> Optional[str]:
        """Return a rejection message when ``member`` may not enter, else None."""
        guild = member.guild
        role_id = gw.get("req_role")
        if role_id:
            role = guild.get_role(int(role_id))
            if role is not None and role not in member.roles:
                return self._t(
                    lang,
                    f"Du benötigst die Rolle **{role.name}**, um teilzunehmen.",
                    f"You need the **{role.name}** role to enter.",
                )
        now = datetime.datetime.now(datetime.timezone.utc)
        min_account = int(gw.get("min_account_days") or 0)
        if min_account > 0 and member.created_at is not None:
            age = (now - member.created_at).days
            if age < min_account:
                return self._t(
                    lang,
                    f"Dein Account muss mindestens **{min_account}** Tage alt sein (aktuell {age}).",
                    f"Your account must be at least **{min_account}** days old (currently {age}).",
                )
        min_join = int(gw.get("min_join_days") or 0)
        if min_join > 0 and member.joined_at is not None:
            days = (now - member.joined_at).days
            if days < min_join:
                return self._t(
                    lang,
                    f"Du musst mindestens **{min_join}** Tage auf dem Server sein (aktuell {days}).",
                    f"You must be a member for at least **{min_join}** days (currently {days}).",
                )
        return None

    async def _handle_enter(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or interaction.message is None:
            return
        lang = await self._lang(guild)
        joined = None
        reject: Optional[str] = None
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
                # Withdrawing is always allowed, regardless of requirements.
                entrants.remove(uid)
                joined = False
            else:
                member = guild.get_member(uid) or interaction.user
                if isinstance(member, discord.Member):
                    reject = self._eligibility_error(member, gw, lang)
                if reject is None:
                    entrants.append(uid)
                    joined = True
        if reject is not None:
            await interaction.response.send_message(reject, ephemeral=True)
            return
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

    async def _dm_winners(self, guild, gw, lang: str) -> None:
        """DM each winner; failures fall back to the channel mention silently."""
        channel_id, message_id = gw.get("channel"), gw.get("message")
        jump = f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}" if channel_id and message_id else ""
        for wid in gw.get("won", []):
            member = guild.get_member(wid)
            if member is None:
                continue
            try:
                await member.send(self._t(
                    lang,
                    f"🎉 Du hast **{gw.get('prize', '')}** auf **{guild.name}** gewonnen!\n{jump}",
                    f"🎉 You won **{gw.get('prize', '')}** on **{guild.name}**!\n{jump}",
                ))
            except (discord.Forbidden, discord.HTTPException):
                # DMs closed -> the channel announcement mention is the fallback.
                pass

    async def _finish(self, guild, gw, lang: str, *, dm: bool = True) -> None:
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
            if dm:
                await self._dm_winners(guild, gw, lang)

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
            gconf = await self.config.guild(guild).all()
            lang = str(gconf.get("language") or "en-US")
            dm = bool(gconf.get("dm_winners", True))
            cleanup_days = max(1, int(gconf.get("cleanup_days") or 2))
            async with self.config.guild(guild).giveaways() as gws:
                due = [g for g in gws if not g.get("ended") and g.get("end", 0) <= now]
                for gw in due:
                    await self._finish(guild, gw, lang, dm=dm)
                # Prune giveaways ended more than `cleanup_days` days ago.
                cutoff = now - cleanup_days * 86400
                gws[:] = [g for g in gws if not (g.get("ended") and g.get("end", 0) < cutoff)]

    # ------------------------------------------------------------------ #
    # Commands (running giveaways: moderators and up)
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="giveaway", aliases=["gw"])
    @commands.mod_or_permissions(manage_messages=True)
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
        """Start a giveaway (entry requirements come from ``giveawayset``)."""
        lang = await self._lang(ctx.guild)
        gconf = await self.config.guild(ctx.guild).all()
        if not gconf.get("enabled", True):
            await ctx.send(self._t(lang, "Giveaway-Modul ist deaktiviert.", "Giveaway module is disabled."))
            return
        secs = parse_duration(duration)
        if secs is None:
            await ctx.send(self._t(lang, "Ungültige Dauer. Beispiel: `1h`, `30m`, `2d`.", "Invalid duration. Example: `1h`, `30m`, `2d`."))
            return
        if not 1 <= winners <= 50:
            await ctx.send(self._t(lang, "Gewinner müssen 1–50 sein.", "Winners must be 1–50."))
            return
        if not channel.permissions_for(ctx.guild.me).send_messages:
            await ctx.send(self._t(lang, "Keine Senderechte in dem Kanal.", "I can't send messages in that channel."))
            return
        gw = {
            "id": uuid.uuid4().hex[:8],
            "channel": channel.id,
            "message": None,
            "prize": prize.strip()[:240],
            "winners": winners,
            "end": time.time() + secs,
            "host": ctx.author.id,
            "entrants": [],
            "ended": False,
            "won": [],
            # Snapshot the guild's current entry requirements so later config
            # changes do not retroactively affect running giveaways.
            "req_role": gconf.get("req_role"),
            "min_account_days": int(gconf.get("min_account_days") or 0),
            "min_join_days": int(gconf.get("min_join_days") or 0),
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
        dm = bool(await self.config.guild(ctx.guild).dm_winners())
        done = False
        async with self.config.guild(ctx.guild).giveaways() as gws:
            gw = next((g for g in gws if g.get("id") == giveaway_id and not g.get("ended")), None)
            if gw is not None:
                await self._finish(ctx.guild, gw, lang, dm=dm)
                done = True
        await ctx.send(self._t(lang, "Beendet & ausgelost." if done else "Nicht gefunden.", "Ended & drawn." if done else "Not found."))

    @giveaway.command(name="reroll")
    @app_commands.describe(giveaway_id="The giveaway ID", count="How many winners to draw (default: original count)")
    async def gw_reroll(self, ctx: commands.Context, giveaway_id: str, count: Optional[int] = None) -> None:
        """Reroll the winners of an ended giveaway (optionally N winners)."""
        lang = await self._lang(ctx.guild)
        if count is not None and not 1 <= count <= 50:
            await ctx.send(self._t(lang, "Anzahl muss 1–50 sein.", "Count must be 1–50."))
            return
        dm = bool(await self.config.guild(ctx.guild).dm_winners())
        winners = None
        drawn_gw = None
        async with self.config.guild(ctx.guild).giveaways() as gws:
            gw = next((g for g in gws if g.get("id") == giveaway_id and g.get("ended")), None)
            if gw is not None:
                gw["won"] = self._draw(gw.get("entrants", []), count or gw.get("winners", 1))
                winners = gw["won"]
                drawn_gw = dict(gw)
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
            if dm and drawn_gw is not None:
                await self._dm_winners(ctx.guild, drawn_gw, lang)
        else:
            await ctx.send(self._t(lang, "Keine Teilnehmer.", "No entries."))

    @giveaway.command(name="list")
    async def gw_list(self, ctx: commands.Context) -> None:
        """List running giveaways (paginated)."""
        lang = await self._lang(ctx.guild)
        gws = [g for g in await self.config.guild(ctx.guild).giveaways() if not g.get("ended")]
        if not gws:
            await ctx.send(self._t(lang, "Keine laufenden Giveaways.", "No running giveaways."))
            return
        lines = []
        for g in gws:
            ch = ctx.guild.get_channel(g.get("channel"))
            lines.append(f"`{g.get('id')}` · {ch.mention if ch else '?'} · **{g.get('prize')}** · {len(g.get('entrants', []))} 👤 · <t:{int(g.get('end', 0))}:R>")
        per_page = 10
        pages: List[discord.Embed] = []
        colour = await ctx.embed_colour()
        title = self._t(lang, "Laufende Giveaways", "Running giveaways")
        for i in range(0, len(lines), per_page):
            e = discord.Embed(title=title, description="\n".join(lines[i:i + per_page])[:4000], colour=colour)
            e.set_footer(text=self._t(
                lang,
                f"Seite {i // per_page + 1}/{(len(lines) - 1) // per_page + 1}",
                f"Page {i // per_page + 1}/{(len(lines) - 1) // per_page + 1}",
            ))
            pages.append(e)
        await self._send_pages(ctx, pages)

    @giveaway.command(name="entries")
    @app_commands.describe(giveaway_id="The giveaway ID (from 'giveaway list')")
    async def gw_entries(self, ctx: commands.Context, giveaway_id: str) -> None:
        """Show the entrants of a giveaway (paginated)."""
        lang = await self._lang(ctx.guild)
        gws = await self.config.guild(ctx.guild).giveaways()
        gw = next((g for g in gws if g.get("id") == giveaway_id), None)
        if gw is None:
            await ctx.send(self._t(lang, "Nicht gefunden.", "Not found."))
            return
        entrants = gw.get("entrants", [])
        if not entrants:
            await ctx.send(self._t(lang, "Keine Teilnehmer.", "No entries."))
            return
        lines = []
        for uid in entrants:
            m = ctx.guild.get_member(uid)
            lines.append(f"• {m.display_name if m else f'<@{uid}>'}")
        per_page = 20
        pages: List[discord.Embed] = []
        colour = await ctx.embed_colour()
        title = self._t(lang, f"Teilnehmer — {gw.get('prize', '')}", f"Entries — {gw.get('prize', '')}")
        for i in range(0, len(lines), per_page):
            e = discord.Embed(title=title, description="\n".join(lines[i:i + per_page])[:4000], colour=colour)
            e.set_footer(text=self._t(
                lang,
                f"Seite {i // per_page + 1}/{(len(lines) - 1) // per_page + 1} · {len(lines)} Teilnehmer",
                f"Page {i // per_page + 1}/{(len(lines) - 1) // per_page + 1} · {len(lines)} entrants",
            ))
            pages.append(e)
        await self._send_pages(ctx, pages)

    # ------------------------------------------------------------------ #
    # Configuration (admin)
    # ------------------------------------------------------------------ #
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

    @giveawayset.command(name="dmwinners")
    @app_commands.describe(on_off="DM winners in addition to the channel announcement")
    async def gws_dmwinners(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle DM notifications for winners."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).dm_winners.set(on_off)
        await ctx.send(self._t(
            lang,
            "Gewinner werden per DM benachrichtigt." if on_off else "Keine DM-Benachrichtigung mehr.",
            "Winners will be notified via DM." if on_off else "DM notifications disabled.",
        ))

    @giveawayset.command(name="cleanup")
    @app_commands.describe(days="Days to keep ended giveaways before auto-cleanup (1-30)")
    async def gws_cleanup(self, ctx: commands.Context, days: int) -> None:
        """Set after how many days ended giveaways are removed."""
        lang = await self._lang(ctx.guild)
        if not 1 <= days <= 30:
            await ctx.send(self._t(lang, "Tage müssen 1–30 sein.", "Days must be 1–30."))
            return
        await self.config.guild(ctx.guild).cleanup_days.set(days)
        await ctx.send(self._t(
            lang,
            f"Beendete Giveaways werden nach **{days}** Tag(en) entfernt.",
            f"Ended giveaways are removed after **{days}** day(s).",
        ))

    @giveawayset.command(name="reqrole")
    @app_commands.describe(role="Required role for entry (leave empty to clear)")
    async def gws_reqrole(self, ctx: commands.Context, role: Optional[discord.Role] = None) -> None:
        """Set (or clear) the role required to enter new giveaways."""
        lang = await self._lang(ctx.guild)
        if role is None:
            await self.config.guild(ctx.guild).req_role.clear()
            await ctx.send(self._t(lang, "Rollen-Voraussetzung entfernt.", "Role requirement cleared."))
            return
        await self.config.guild(ctx.guild).req_role.set(role.id)
        await ctx.send(self._t(lang, f"Teilnahme erfordert: {role.mention}", f"Entry requires: {role.mention}"))

    @giveawayset.command(name="minaccountage")
    @app_commands.describe(days="Minimum account age in days (0 = off)")
    async def gws_minaccountage(self, ctx: commands.Context, days: int) -> None:
        """Set the minimum Discord account age (days) for entry."""
        lang = await self._lang(ctx.guild)
        if not 0 <= days <= 3650:
            await ctx.send(self._t(lang, "Tage müssen 0–3650 sein.", "Days must be 0–3650."))
            return
        await self.config.guild(ctx.guild).min_account_days.set(days)
        await ctx.send(self._t(
            lang,
            f"Mindest-Accountalter: **{days}** Tag(e) (0 = aus).",
            f"Minimum account age: **{days}** day(s) (0 = off).",
        ))

    @giveawayset.command(name="minmember")
    @app_commands.describe(days="Minimum days in this server (0 = off)")
    async def gws_minmember(self, ctx: commands.Context, days: int) -> None:
        """Set the minimum server membership duration (days) for entry."""
        lang = await self._lang(ctx.guild)
        if not 0 <= days <= 3650:
            await ctx.send(self._t(lang, "Tage müssen 0–3650 sein.", "Days must be 0–3650."))
            return
        await self.config.guild(ctx.guild).min_join_days.set(days)
        await ctx.send(self._t(
            lang,
            f"Mindest-Mitgliedschaft: **{days}** Tag(e) (0 = aus).",
            f"Minimum server membership: **{days}** day(s) (0 = off).",
        ))

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
            for g in running[:15]
        ) or "—"
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Giveaways per Befehl `giveaway start` starten.\nLaufend:\n{listing}",
                f"Start giveaways with `giveaway start`.\nRunning:\n{listing}",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.switch("dm_winners", L("Gewinner per DM benachrichtigen", "DM winners"), value=bool(await conf.dm_winners())),
                Field.number("cleanup_days", L("Beendete nach (Tagen) aufräumen", "Clean up ended after (days)"), value=int(await conf.cleanup_days() or 2), min=1, max=30),
                Field.role("req_role", L("Erforderliche Rolle (optional)", "Required role (optional)"), value=str(await conf.req_role() or "")),
                Field.number("min_account_days", L("Mindest-Accountalter (Tage, 0 = aus)", "Min. account age (days, 0 = off)"), value=int(await conf.min_account_days() or 0), min=0, max=3650),
                Field.number("min_join_days", L("Mindest-Mitgliedschaft (Tage, 0 = aus)", "Min. server membership (days, 0 = off)"), value=int(await conf.min_join_days() or 0), min=0, max=3650),
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

        errors = {}

        def _int_in(key, lo, hi, default):
            try:
                v = int(data.get(key, default))
            except (TypeError, ValueError):
                v = lo - 1
            if not lo <= v <= hi:
                errors[key] = tr_lang(lang, f"Wert muss {lo}–{hi} sein.", f"Value must be {lo}–{hi}.")
            return v

        cleanup_days = _int_in("cleanup_days", 1, 30, 2)
        min_account = _int_in("min_account_days", 0, 3650, 0)
        min_join = _int_in("min_join_days", 0, 3650, 0)
        if errors:
            return SubmitResult.fail(tr_lang(lang, "Bitte Eingaben prüfen.", "Please check your input."), errors)

        await conf.enabled.set(bool(data.get("enabled")))
        await conf.dm_winners.set(bool(data.get("dm_winners")))
        await conf.cleanup_days.set(cleanup_days)
        role = str(data.get("req_role") or "").strip()
        await (conf.req_role.set(int(role)) if role.isdigit() else conf.req_role.clear())
        await conf.min_account_days.set(min_account)
        await conf.min_join_days.set(min_join)
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard page: giveaway overview (guild scope)
    # ------------------------------------------------------------------ #
    @dashboard_page(
        "overview",
        L("Giveaway-Übersicht", "Giveaway overview"),
        scope="guild",
        permission="guild_mod",
        icon="gift",
    )
    async def overview_page(self, ctx):
        gws = await self.config.guild(ctx.guild).giveaways()
        rows = []
        for g in sorted(gws, key=lambda x: (bool(x.get("ended")), -float(x.get("end", 0)))):
            ch = ctx.guild.get_channel(g.get("channel"))
            end_str = datetime.datetime.fromtimestamp(
                float(g.get("end", 0)), datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            winners = ", ".join(
                (ctx.guild.get_member(w).display_name if ctx.guild.get_member(w) else str(w))
                for w in g.get("won", [])
            ) or "—"
            rows.append({
                "id": str(g.get("id", "")),
                "prize": str(g.get("prize", ""))[:60],
                "channel": f"#{ch.name}" if ch else "?",
                "entries": str(len(g.get("entrants", []))),
                "end": end_str,
                "status": "ended" if g.get("ended") else "running",
                "winners": winners[:80],
            })
        running = sum(1 for r in rows if r["status"] == "running")
        comps = [
            Component.heading(L("Giveaway-Übersicht", "Giveaway overview")),
            Component.text(L(
                f"{running} laufend · {len(rows) - running} beendet (Auto-Aufräumen aktiv)",
                f"{running} running · {len(rows) - running} ended (auto-cleanup active)",
            )),
        ]
        if rows:
            comps.append(Component.table(
                columns=[
                    {"key": "id", "label": "ID"},
                    {"key": "prize", "label": L("Preis", "Prize")},
                    {"key": "channel", "label": L("Kanal", "Channel")},
                    {"key": "entries", "label": L("Teilnahmen", "Entries")},
                    {"key": "end", "label": L("Ende", "End")},
                    {"key": "status", "label": L("Status", "Status")},
                    {"key": "winners", "label": L("Gewinner", "Winners")},
                ],
                rows=rows[:200],
            ))
        else:
            comps.append(Component.text(L("Keine Giveaways vorhanden.", "No giveaways yet.")))
        return PageSchema(components=comps)
