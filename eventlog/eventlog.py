"""EventLog — log server events to a channel.

Opt-in per guild (disabled by default). Each event type can be toggled. Bilingual
labels (DE/EN, default en-US). Web dashboard integration (enable, channel,
per-event switches) via the resilient drop-in.

Logged events: member join/leave (incl. kick detection), message edit/delete,
role changes, nickname changes, voice join/leave/move, channel create/delete/
update, thread create/delete, bans/unbans.

Where Discord does not tell us WHO performed an action (channel deleted, role
changed, kick, ban, ...) the acting moderator is resolved from the guild audit
log — gracefully skipped when the bot lacks the View Audit Log permission.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Deque, Dict, List, Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

from .pdc_dashboard import (
    Component,
    Control,
    Field,
    L,
    PageSchema,
    PanelSchema,
    SubmitResult,
    dashboard_page,
    dashboard_panel,
    register_dashboard,
    tr,
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
    # Newer event types. For guilds configured before these existed the stored
    # events dict lacks the keys; they then default to OFF (safe opt-in).
    "channels": ("Kanäle (erstellt/gelöscht/geändert)", "Channels (create/delete/update)"),
    "threads": ("Threads (erstellt/gelöscht)", "Threads (create/delete)"),
    "bans": ("Bans & Entbannungen", "Bans & unbans"),
}

# Maximum number of recent entries kept in memory per guild (viewer/recent cmd).
RECENT_BUFFER_SIZE = 300


def _fmt_content(text: Optional[str], limit: int = 1024) -> str:
    """Truncate message content for an embed field while keeping code blocks
    intact: if the cut leaves an unbalanced ``` fence, close it again."""
    if not text:
        return "—"
    if len(text) > limit:
        text = text[: limit - 6].rstrip()
        if text.count("```") % 2 == 1:
            text += "\n```"
        else:
            text += " …"
    return text


class EventLog(commands.Cog):
    """Server event logging."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xE7E_106, force_registration=True)
        self.config.register_guild(
            enabled=False,
            channel=None,
            events={k: True for k in EVENTS},
            language="en-US",
            ignore_bots=False,
            retention_days=0,  # 0 = no retention note
        )
        # In-memory ring buffer of recent log entries per guild (not persisted).
        self._recent: Dict[int, Deque[dict]] = {}

    async def cog_load(self) -> None:
        register_dashboard(self)

    def cog_unload(self) -> None:
        unregister_dashboard(self)

    @staticmethod
    def _t(lang: str, de: str, en: str) -> str:
        return de if str(lang or "").lower().startswith("de") else en

    # ------------------------------------------------------------------ #
    # Audit-log helper
    # ------------------------------------------------------------------ #
    async def _audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: Optional[int] = None,
        *,
        delay: float = 1.5,
        max_age: float = 25.0,
    ) -> Optional[discord.AuditLogEntry]:
        """Fetch the most recent matching audit-log entry for an event.

        Returns None when the bot lacks View Audit Log or nothing matches.
        A short delay compensates for audit entries arriving slightly after
        the gateway event.
        """
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        try:
            if delay:
                await asyncio.sleep(delay)
            now = discord.utils.utcnow()
            async for entry in guild.audit_logs(action=action, limit=6):
                if target_id is not None:
                    tgt = getattr(entry, "target", None)
                    if tgt is not None and getattr(tgt, "id", None) != target_id:
                        continue
                if (now - entry.created_at).total_seconds() <= max_age:
                    return entry
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.debug("Audit log lookup failed for %s: %s", action, exc)
        return None

    @staticmethod
    def _actor_line(lang: str, actor: Optional[discord.abc.User]) -> str:
        """Bilingual 'by <moderator>' line for embeds."""
        if actor is None:
            return ""
        who = f"{actor.mention} ({actor})"
        return ("\n🛡️ " + ("Durch: " if str(lang).lower().startswith("de") else "By: ") + who)

    # ------------------------------------------------------------------ #
    # Core dispatch
    # ------------------------------------------------------------------ #
    def _remember(self, guild: discord.Guild, event: str, summary: str) -> None:
        buf = self._recent.setdefault(guild.id, deque(maxlen=RECENT_BUFFER_SIZE))
        buf.append(
            {
                "ts": discord.utils.utcnow(),
                "event": event,
                "text": summary[:300],
            }
        )

    async def _log(
        self,
        guild: Optional[discord.Guild],
        event: str,
        embed: discord.Embed,
        summary: Optional[str] = None,
    ) -> None:
        if guild is None:
            return
        conf = self.config.guild(guild)
        if not await conf.enabled():
            return
        events = await conf.events()
        if not events.get(event, False):
            return
        self._remember(guild, event, summary or (embed.description or embed.title or event))
        cid = await conf.channel()
        if not cid:
            return
        ch = guild.get_channel(cid)
        if ch is None or not ch.permissions_for(guild.me).send_messages:
            return
        retention = await conf.retention_days()
        if retention and not embed.footer.text:
            lang = await conf.language()
            embed.set_footer(
                text=self._t(lang, f"Aufbewahrung: {retention} Tage", f"Retention: {retention} days")
            )
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _ignore_bot(self, guild: discord.Guild, user: discord.abc.User) -> bool:
        """True when bot actions should be skipped for this guild."""
        if not getattr(user, "bot", False):
            return False
        return bool(await self.config.guild(guild).ignore_bots())

    # ------------------------------------------------------------------ #
    # Listeners: members
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if await self._ignore_bot(member.guild, member):
            return
        e = discord.Embed(colour=discord.Colour.green(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.description = f"📥 {member.mention} joined • {member.id}"
        await self._log(member.guild, "joins", e, f"{member} joined")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        if await self._ignore_bot(guild, member):
            return
        lang = await self.config.guild(guild).language()
        # Audit-log check: was this actually a kick?
        entry = await self._audit_entry(guild, discord.AuditLogAction.kick, target_id=member.id)
        e = discord.Embed(colour=discord.Colour.red(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        if entry is not None:
            reason = entry.reason or self._t(lang, "Kein Grund angegeben", "No reason provided")
            e.description = (
                f"👢 {member.mention} "
                + self._t(lang, "wurde gekickt", "was kicked")
                + f" • {member.id}"
                + self._actor_line(lang, entry.user)
                + f"\n📝 {reason}"
            )
            await self._log(guild, "leaves", e, f"{member} kicked by {entry.user}")
        else:
            e.description = f"📤 {member.mention} left • {member.id}"
            await self._log(guild, "leaves", e, f"{member} left")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.abc.User) -> None:
        lang = await self.config.guild(guild).language()
        entry = await self._audit_entry(guild, discord.AuditLogAction.ban, target_id=user.id)
        actor = entry.user if entry else None
        reason = (entry.reason if entry and entry.reason else None) or self._t(
            lang, "Kein Grund angegeben", "No reason provided"
        )
        e = discord.Embed(colour=discord.Colour.dark_red(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(user), icon_url=user.display_avatar.url if user.display_avatar else None)
        e.description = (
            f"🔨 {user.mention} "
            + self._t(lang, "wurde gebannt", "was banned")
            + f" • {user.id}"
            + self._actor_line(lang, actor)
            + f"\n📝 {reason}"
        )
        await self._log(guild, "bans", e, f"{user} banned by {actor or 'unknown'}")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.abc.User) -> None:
        lang = await self.config.guild(guild).language()
        entry = await self._audit_entry(guild, discord.AuditLogAction.unban, target_id=user.id)
        actor = entry.user if entry else None
        e = discord.Embed(colour=discord.Colour.green(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(user), icon_url=user.display_avatar.url if user.display_avatar else None)
        e.description = (
            f"🔓 {user.mention} "
            + self._t(lang, "wurde entbannt", "was unbanned")
            + f" • {user.id}"
            + self._actor_line(lang, actor)
        )
        await self._log(guild, "bans", e, f"{user} unbanned by {actor or 'unknown'}")

    # ------------------------------------------------------------------ #
    # Listeners: messages
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot or before.content == after.content:
            return
        e = discord.Embed(colour=discord.Colour.gold(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        e.description = f"✏️ Edited in {after.channel.mention} [jump]({after.jump_url})"
        e.add_field(name="Before", value=_fmt_content(before.content), inline=False)
        e.add_field(name="After", value=_fmt_content(after.content), inline=False)
        await self._log(after.guild, "msg_edit", e, f"{after.author} edited a message in #{after.channel}")

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        e = discord.Embed(colour=discord.Colour.dark_red(), timestamp=discord.utils.utcnow())
        e.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        e.description = f"🗑️ Deleted in {message.channel.mention}"
        if message.content:
            e.add_field(name="Content", value=_fmt_content(message.content), inline=False)
        await self._log(message.guild, "msg_delete", e, f"Message by {message.author} deleted in #{message.channel}")

    # ------------------------------------------------------------------ #
    # Listeners: member updates (roles / nicknames)
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        guild = after.guild
        lang = await self.config.guild(guild).language()
        if before.nick != after.nick:
            entry = await self._audit_entry(guild, discord.AuditLogAction.member_update, target_id=after.id)
            actor = entry.user if entry and entry.user and entry.user.id != after.id else None
            e = discord.Embed(colour=discord.Colour.blurple(), timestamp=discord.utils.utcnow())
            e.set_author(name=str(after), icon_url=after.display_avatar.url)
            e.description = (
                f"📝 Nickname: `{before.nick or before.name}` → `{after.nick or after.name}`"
                + self._actor_line(lang, actor)
            )
            await self._log(guild, "nicknames", e, f"{after} nickname changed")
        if set(before.roles) != set(after.roles):
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            parts: List[str] = []
            if added:
                parts.append("➕ " + ", ".join(r.mention for r in added))
            if removed:
                parts.append("➖ " + ", ".join(r.mention for r in removed))
            if parts:
                entry = await self._audit_entry(
                    guild, discord.AuditLogAction.member_role_update, target_id=after.id
                )
                actor = entry.user if entry else None
                e = discord.Embed(colour=discord.Colour.blurple(), timestamp=discord.utils.utcnow())
                e.set_author(name=str(after), icon_url=after.display_avatar.url)
                e.description = (
                    f"🎭 Roles for {after.mention}\n" + "\n".join(parts) + self._actor_line(lang, actor)
                )
                await self._log(guild, "roles", e, f"{after} roles changed by {actor or 'unknown'}")

    # ------------------------------------------------------------------ #
    # Listeners: voice
    # ------------------------------------------------------------------ #
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
        await self._log(member.guild, "voice", e, f"{member} voice update")

    # ------------------------------------------------------------------ #
    # Listeners: channels & threads (with audit-log actor)
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        lang = await self.config.guild(guild).language()
        entry = await self._audit_entry(guild, discord.AuditLogAction.channel_create, target_id=channel.id)
        actor = entry.user if entry else None
        e = discord.Embed(colour=discord.Colour.green(), timestamp=discord.utils.utcnow())
        e.description = (
            "🆕 "
            + self._t(lang, "Kanal erstellt", "Channel created")
            + f": {getattr(channel, 'mention', '#' + channel.name)} (`{channel.type.name}` • {channel.id})"
            + self._actor_line(lang, actor)
        )
        await self._log(guild, "channels", e, f"Channel #{channel.name} created by {actor or 'unknown'}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        lang = await self.config.guild(guild).language()
        # BUGFIX: resolve WHICH moderator deleted the channel via the audit log.
        entry = await self._audit_entry(guild, discord.AuditLogAction.channel_delete, target_id=channel.id)
        actor = entry.user if entry else None
        e = discord.Embed(colour=discord.Colour.dark_red(), timestamp=discord.utils.utcnow())
        e.description = (
            "🗑️ "
            + self._t(lang, "Kanal gelöscht", "Channel deleted")
            + f": `#{channel.name}` (`{channel.type.name}` • {channel.id})"
            + self._actor_line(lang, actor)
        )
        if actor is None:
            e.description += "\n" + self._t(
                lang,
                "ℹ️ Moderator unbekannt (kein Audit-Log-Zugriff oder Eintrag).",
                "ℹ️ Moderator unknown (no audit-log access or entry).",
            )
        await self._log(guild, "channels", e, f"Channel #{channel.name} deleted by {actor or 'unknown'}")

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        guild = after.guild
        changes: List[str] = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append(f"Topic: `{getattr(before, 'topic', None) or '—'}` → `{getattr(after, 'topic', None) or '—'}`")
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            changes.append(f"NSFW: `{getattr(before, 'nsfw', None)}` → `{getattr(after, 'nsfw', None)}`")
        if not changes:
            return  # ignore positional shifts / permission syncs
        lang = await self.config.guild(guild).language()
        entry = await self._audit_entry(guild, discord.AuditLogAction.channel_update, target_id=after.id)
        actor = entry.user if entry else None
        e = discord.Embed(colour=discord.Colour.gold(), timestamp=discord.utils.utcnow())
        e.description = (
            "⚙️ "
            + self._t(lang, "Kanal geändert", "Channel updated")
            + f": {getattr(after, 'mention', '#' + after.name)}\n"
            + "\n".join(changes)
            + self._actor_line(lang, actor)
        )
        await self._log(guild, "channels", e, f"Channel #{after.name} updated by {actor or 'unknown'}")

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        guild = thread.guild
        lang = await self.config.guild(guild).language()
        e = discord.Embed(colour=discord.Colour.green(), timestamp=discord.utils.utcnow())
        owner = f" • <@{thread.owner_id}>" if thread.owner_id else ""
        e.description = (
            "🧵 "
            + self._t(lang, "Thread erstellt", "Thread created")
            + f": {thread.mention}"
            + (f" in {thread.parent.mention}" if thread.parent else "")
            + owner
        )
        await self._log(guild, "threads", e, f"Thread {thread.name} created")

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        guild = thread.guild
        lang = await self.config.guild(guild).language()
        entry = await self._audit_entry(guild, discord.AuditLogAction.thread_delete, target_id=thread.id)
        actor = entry.user if entry else None
        e = discord.Embed(colour=discord.Colour.dark_red(), timestamp=discord.utils.utcnow())
        e.description = (
            "🧵 "
            + self._t(lang, "Thread gelöscht", "Thread deleted")
            + f": `{thread.name}`"
            + (f" in {thread.parent.mention}" if thread.parent else "")
            + self._actor_line(lang, actor)
        )
        await self._log(guild, "threads", e, f"Thread {thread.name} deleted by {actor or 'unknown'}")

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
        """Toggle a single event type (joins, leaves, msg_edit, msg_delete, roles, nicknames, voice, channels, threads, bans)."""
        lang = await self.config.guild(ctx.guild).language()
        event = event.lower()
        if event not in EVENTS:
            await ctx.send(self._t(lang, f"Unbekannt. Gültig: {', '.join(EVENTS)}", f"Unknown. Valid: {', '.join(EVENTS)}"))
            return
        async with self.config.guild(ctx.guild).events() as ev:
            ev[event] = on_off
        await ctx.send(self._t(lang, "Gespeichert.", "Saved."))

    @eventlog.command(name="ignorebots")
    @app_commands.describe(on_off="Ignore actions performed by bots")
    async def el_ignorebots(self, ctx: commands.Context, on_off: bool) -> None:
        """Ignore bot joins/leaves and other bot actions."""
        lang = await self.config.guild(ctx.guild).language()
        await self.config.guild(ctx.guild).ignore_bots.set(on_off)
        await ctx.send(
            self._t(
                lang,
                f"Bot-Aktionen werden {'ignoriert' if on_off else 'geloggt'}.",
                f"Bot actions are now {'ignored' if on_off else 'logged'}.",
            )
        )

    @eventlog.command(name="retention")
    @app_commands.describe(days="Retention note in days (0 disables the note)")
    async def el_retention(self, ctx: commands.Context, days: commands.Range[int, 0, 365]) -> None:
        """Set an informational retention note added to log embeds.

        This does not delete anything by itself — it documents your guideline
        for how long log entries should be kept.
        """
        lang = await self.config.guild(ctx.guild).language()
        await self.config.guild(ctx.guild).retention_days.set(int(days))
        if days:
            await ctx.send(self._t(lang, f"Aufbewahrungshinweis: {days} Tage.", f"Retention note: {days} days."))
        else:
            await ctx.send(self._t(lang, "Aufbewahrungshinweis deaktiviert.", "Retention note disabled."))

    @eventlog.command(name="status")
    async def el_status(self, ctx: commands.Context) -> None:
        """Show the current configuration."""
        lang = await self.config.guild(ctx.guild).language()
        conf = self.config.guild(ctx.guild)
        events = await conf.events()
        cid = await conf.channel()
        ch = ctx.guild.get_channel(cid) if cid else None
        retention = await conf.retention_days()
        lines = [f"{'✅' if events.get(k) else '❌'} {self._t(lang, de, en)} (`{k}`)" for k, (de, en) in EVENTS.items()]
        e = discord.Embed(
            title=self._t(lang, "Event-Logging", "Event logging"),
            description=self._t(lang, "Aktiv: ", "Active: ")
            + ("✅" if await conf.enabled() else "❌")
            + f"\n{self._t(lang, 'Kanal', 'Channel')}: {ch.mention if ch else '—'}"
            + f"\n{self._t(lang, 'Bots ignorieren', 'Ignore bots')}: "
            + ("✅" if await conf.ignore_bots() else "❌")
            + f"\n{self._t(lang, 'Aufbewahrung', 'Retention')}: "
            + (self._t(lang, f"{retention} Tage", f"{retention} days") if retention else "—")
            + "\n\n"
            + "\n".join(lines),
            colour=await ctx.embed_colour(),
        )
        await ctx.send(embed=e)

    @eventlog.command(name="recent")
    @app_commands.describe(event="Optional event type filter", count="How many entries (max 100)")
    async def el_recent(
        self,
        ctx: commands.Context,
        event: Optional[str] = None,
        count: commands.Range[int, 1, 100] = 50,
    ) -> None:
        """Show recent log entries from the in-memory buffer (paginated)."""
        lang = await self.config.guild(ctx.guild).language()
        entries = list(self._recent.get(ctx.guild.id, []))
        if event:
            event = event.lower()
            if event not in EVENTS:
                await ctx.send(
                    self._t(lang, f"Unbekannt. Gültig: {', '.join(EVENTS)}", f"Unknown. Valid: {', '.join(EVENTS)}")
                )
                return
            entries = [x for x in entries if x["event"] == event]
        entries = entries[-int(count):][::-1]
        if not entries:
            await ctx.send(self._t(lang, "Keine Einträge im Puffer.", "No entries in the buffer."))
            return
        pages: List[discord.Embed] = []
        per_page = 10
        for i in range(0, len(entries), per_page):
            chunk = entries[i : i + per_page]
            lines = []
            for x in chunk:
                label = EVENTS.get(x["event"], (x["event"], x["event"]))
                lines.append(
                    f"<t:{int(x['ts'].timestamp())}:t> **{self._t(lang, label[0], label[1])}** — {x['text']}"
                )
            emb = discord.Embed(
                title=self._t(lang, "Letzte Ereignisse", "Recent events"),
                description="\n".join(lines),
                colour=await ctx.embed_colour(),
            )
            emb.set_footer(text=f"{i // per_page + 1}/{(len(entries) - 1) // per_page + 1}")
            pages.append(emb)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await menu(ctx, pages, DEFAULT_CONTROLS)

    @eventlog.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def el_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server (default: en-US)."""
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
            Field.switch("ignore_bots", L("Bot-Aktionen ignorieren", "Ignore bot actions"), value=bool(await conf.ignore_bots())),
            Field.number("retention_days", L("Aufbewahrungshinweis (Tage, 0 = aus)", "Retention note (days, 0 = off)"), value=int(await conf.retention_days())),
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
        # Validate that the submitted channel actually belongs to this guild.
        ch = str(data.get("channel") or "").strip()
        if ch.isdigit() and ctx.guild.get_channel(int(ch)) is not None:
            await conf.channel.set(int(ch))
        else:
            await conf.channel.clear()
        await conf.ignore_bots.set(bool(data.get("ignore_bots")))
        try:
            retention = max(0, min(365, int(data.get("retention_days") or 0)))
        except (TypeError, ValueError):
            retention = 0
        await conf.retention_days.set(retention)
        async with conf.events() as ev:
            for key in EVENTS:
                if f"ev_{key}" in data:
                    ev[key] = bool(data.get(f"ev_{key}"))
        lang = str(data.get("language", "en-US")).strip()
        if lang not in ("de-DE", "en-US"):
            lang = "en-US"
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard page (guild scope): recent event viewer
    # ------------------------------------------------------------------ #
    @dashboard_page(
        "viewer",
        L("Event-Log", "Event log"),
        scope="guild",
        permission="guild_mod",
        icon="list",
    )
    async def viewer_page(self, ctx):
        params = getattr(ctx, "params", None) or {}
        selected = str(params.get("event") or "all")
        valid = {"all", *EVENTS.keys()}
        if selected not in valid:
            selected = "all"
        entries = list(self._recent.get(ctx.guild.id, []))
        if selected != "all":
            entries = [x for x in entries if x["event"] == selected]
        entries = entries[-100:][::-1]

        options = [{"value": "all", "label": L("Alle Ereignisse", "All events")}]
        for key, (de, en) in EVENTS.items():
            options.append({"value": key, "label": L(de, en)})
        controls = [Control.select("event", L("Ereignis", "Event"), options, value=selected)]

        rows = []
        for x in entries:
            label = EVENTS.get(x["event"], (x["event"], x["event"]))
            rows.append(
                {
                    "time": x["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                    "event": tr(ctx, label[0], label[1]),
                    "text": x["text"],
                }
            )
        comps = [
            Component.heading(L("Event-Log", "Event log")),
            Component.text(
                tr(
                    ctx,
                    "Letzte Ereignisse aus dem Arbeitsspeicher-Puffer (max. 300, nicht persistent).",
                    "Recent events from the in-memory buffer (max. 300, not persisted).",
                )
            ),
            Component.table(
                columns=[
                    {"key": "time", "label": tr(ctx, "Zeit", "Time")},
                    {"key": "event", "label": tr(ctx, "Ereignis", "Event")},
                    {"key": "text", "label": tr(ctx, "Beschreibung", "Description")},
                ],
                rows=rows,
                title=tr(ctx, "Letzte Ereignisse", "Recent events"),
            ),
        ]
        return PageSchema(components=comps, controls=controls)
