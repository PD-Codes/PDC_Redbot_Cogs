"""EventLog — log server events to a channel.

Opt-in per guild (disabled by default). Each event type can be toggled. Bilingual
labels (DE/EN). Web dashboard integration (enable, channel, per-event switches)
via the resilient drop-in.

Logged events: member join/leave, message edit/delete, role changes, nickname
changes, voice join/leave/move.
"""
from __future__ import annotations

import logging
from typing import Optional

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

log = logging.getLogger("red.pdc.eventlog")

# event key -> (DE label, EN label)
EVENTS = {
    "joins": ("Beitritte", "Joins"),
    "leaves": ("Austritte", "Leaves"),
    "msg_edit": ("Nachricht bearbeitet", "Message edited"),
    "msg_delete": ("Nachricht gelöscht", "Message deleted"),
    "roles": ("Rollenänderungen", "Role changes"),
    "nicknames": ("Namensänderungen", "Nickname changes"),
    "voice": ("Sprachkanäle", "Voice activity"),
}


class EventLog(commands.Cog):
    """Server event logging."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xE7E_106, force_registration=True)
        self.config.register_guild(
            enabled=False,
            channel=None,
            events={k: True for k in EVENTS},
            language="en-US",
        )

    async def cog_load(self) -> None:
        register_dashboard(self)

    def cog_unload(self) -> None:
        unregister_dashboard(self)

    @staticmethod
    def _t(lang: str, de: str, en: str) -> str:
        return de if str(lang).lower().startswith("de") else en

    # ------------------------------------------------------------------ #
    # Core dispatch
    # ------------------------------------------------------------------ #
    async def _log(self, guild: Optional[discord.Guild], event: str, embed: discord.Embed) -> None:
        if guild is None:
            return
        conf = self.config.guild(guild)
        if not await conf.enabled():
            return
        events = await conf.events()
        if not events.get(event, False):
            return
        cid = await conf.channel()
        if not cid:
            return
        ch = guild.get_channel(cid)
        if ch is None or not ch.permissions_for(guild.me).send_messages:
            return
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ #
    # Listeners
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        e = discord.Embed(colour=discord.Colour.green(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.description = f"📥 {member.mention} joined • {member.id}"
        await self._log(member.guild, "joins", e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        e = discord.Embed(colour=discord.Colour.red(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.description = f"📤 {member.mention} left • {member.id}"
        await self._log(member.guild, "leaves", e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot or before.content == after.content:
            return
        e = discord.Embed(colour=discord.Colour.gold(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        e.description = f"✏️ Edited in {after.channel.mention} [jump]({after.jump_url})"
        e.add_field(name="Before", value=(before.content or "—")[:1024], inline=False)
        e.add_field(name="After", value=(after.content or "—")[:1024], inline=False)
        await self._log(after.guild, "msg_edit", e)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        e = discord.Embed(colour=discord.Colour.dark_red(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        e.description = f"🗑️ Deleted in {message.channel.mention}"
        if message.content:
            e.add_field(name="Content", value=message.content[:1024], inline=False)
        await self._log(message.guild, "msg_delete", e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick != after.nick:
            e = discord.Embed(colour=discord.Colour.blurple(), timestamp=discord.utils.utcnow())
            e.set_author(name=str(after), icon_url=after.display_avatar.url)
            e.description = f"📝 Nickname: `{before.nick or before.name}` → `{after.nick or after.name}`"
            await self._log(after.guild, "nicknames", e)
        if set(before.roles) != set(after.roles):
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            parts = []
            if added:
                parts.append("➕ " + ", ".join(r.mention for r in added))
            if removed:
                parts.append("➖ " + ", ".join(r.mention for r in removed))
            if parts:
                e = discord.Embed(colour=discord.Colour.blurple(), timestamp=discord.utils.utcnow())
                e.set_author(name=str(after), icon_url=after.display_avatar.url)
                e.description = f"🎭 Roles for {after.mention}\n" + "\n".join(parts)
                await self._log(after.guild, "roles", e)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        if member.bot or before.channel == after.channel:
            return
        if before.channel is None and after.channel is not None:
            desc = f"🔊 {member.mention} joined voice {after.channel.mention}"
        elif before.channel is not None and after.channel is None:
            desc = f"🔇 {member.mention} left voice {before.channel.mention}"
        else:
            desc = f"🔀 {member.mention} moved {before.channel.mention} → {after.channel.mention}"
        e = discord.Embed(colour=discord.Colour.teal(), timestamp=discord.utils.utcnow(), description=desc)
        await self._log(member.guild, "voice", e)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="eventlog", aliases=["elog"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def eventlog(self, ctx: commands.Context) -> None:
        """Configure event logging."""

    @eventlog.command(name="enable")
    @app_commands.describe(on_off="Enable or disable event logging")
    async def el_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the module for this server."""
        lang = await self.config.guild(ctx.guild).language()
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = self._t(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(self._t(lang, f"Event-Logging **{state}**.", f"Event logging **{state}**."))

    @eventlog.command(name="channel")
    @app_commands.describe(channel="Log channel")
    async def el_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the log channel."""
        lang = await self.config.guild(ctx.guild).language()
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(self._t(lang, f"Log-Kanal: {channel.mention}", f"Log channel: {channel.mention}"))

    @eventlog.command(name="event")
    @app_commands.describe(event="Event type", on_off="Enable or disable this event")
    async def el_event(self, ctx: commands.Context, event: str, on_off: bool) -> None:
        """Toggle a single event type (joins, leaves, msg_edit, msg_delete, roles, nicknames, voice)."""
        lang = await self.config.guild(ctx.guild).language()
        event = event.lower()
        if event not in EVENTS:
            await ctx.send(self._t(lang, f"Unbekannt. Gültig: {', '.join(EVENTS)}", f"Unknown. Valid: {', '.join(EVENTS)}"))
            return
        async with self.config.guild(ctx.guild).events() as ev:
            ev[event] = on_off
        await ctx.send(self._t(lang, "Gespeichert.", "Saved."))

    @eventlog.command(name="status")
    async def el_status(self, ctx: commands.Context) -> None:
        """Show the current configuration."""
        lang = await self.config.guild(ctx.guild).language()
        conf = self.config.guild(ctx.guild)
        events = await conf.events()
        cid = await conf.channel()
        ch = ctx.guild.get_channel(cid) if cid else None
        lines = [f"{'✅' if events.get(k) else '❌'} {self._t(lang, de, en)}" for k, (de, en) in EVENTS.items()]
        e = discord.Embed(
            title=self._t(lang, "Event-Logging", "Event logging"),
            description=self._t(lang, "Aktiv: ", "Active: ")
            + ("✅" if await conf.enabled() else "❌")
            + f"\n{self._t(lang, 'Kanal', 'Channel')}: {ch.mention if ch else '—'}\n\n"
            + "\n".join(lines),
            colour=await ctx.embed_colour(),
        )
        await ctx.send(embed=e)

    @eventlog.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def el_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(self._t(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("eventlog", L("Event-Logging", "Event logging"), mount="guild_settings", permission="guild_admin", order=80)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        events = await conf.events()
        channels = [{"value": str(c.id), "label": f"#{c.name}"} for c in ctx.guild.text_channels]
        fields = [
            Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
            Field.select("channel", L("Log-Kanal", "Log channel"), channels, value=str(await conf.channel() or "")),
        ]
        for key, (de, en) in EVENTS.items():
            fields.append(Field.switch(f"ev_{key}", L(de, en), value=bool(events.get(key))))
        fields.append(
            Field.select(
                "language", L("Sprache", "Language"),
                [{"value": "de-DE", "label": "Deutsch"}, {"value": "en-US", "label": "English"}],
                value=str(lang), reload_on_change=True,
            )
        )
        return PanelSchema(
            description=tr_lang(lang, "Server-Ereignisse in einen Kanal loggen.", "Log server events to a channel."),
            fields=fields,
        )

    @settings_panel.on_submit
    async def _save_settings(self, ctx, data):
        conf = self.config.guild(ctx.guild)
        await conf.enabled.set(bool(data.get("enabled")))
        ch = str(data.get("channel") or "").strip()
        await (conf.channel.set(int(ch)) if ch.isdigit() else conf.channel.clear())
        async with conf.events() as ev:
            for key in EVENTS:
                if f"ev_{key}" in data:
                    ev[key] = bool(data.get(f"ev_{key}"))
        lang = str(data.get("language", "en-US")).strip() or "en-US"
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))
