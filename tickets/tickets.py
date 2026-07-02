"""Tickets — Ticket-Tool-style support tickets with panels and transcripts.

Admins post ticket panels (an embed with an **Open ticket** button) into a
channel; multiple panels with their own subject are supported. Clicking the
button opens a private thread or a text channel (configurable) that is visible
to the opener and the configured support roles. Support staff can claim,
close (with confirmation), reopen and delete tickets. On close a plain-text
and a simple HTML transcript are generated, stored in the cog data folder and
posted to a configurable log channel. All buttons are persistent (custom_id
based) and survive bot restarts. Bilingual output (DE/EN, default en-US) and
web dashboard integration (ticket overview + transcript viewer + settings
panel) via the resilient drop-in.
"""
from __future__ import annotations

import asyncio
import datetime
import html as html_mod
import logging
import time
import uuid
from pathlib import Path
from typing import List, Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

try:
    from redbot.core.utils.views import SimpleMenu
except Exception:  # pragma: no cover - older Red versions
    SimpleMenu = None  # type: ignore

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
    tr_lang,
    unregister_dashboard,
)

log = logging.getLogger("red.pdc.tickets")  # module logger

_OPEN_ID = "pdc_ticket_open"
_CLOSE_ID = "pdc_ticket_close"
_CLAIM_ID = "pdc_ticket_claim"

_DEFAULT_WELCOME = (
    "Hello {user}, thanks for opening a ticket about **{subject}**!\n"
    "Please describe your issue — the support team will be with you shortly."
)


class PanelView(discord.ui.View):
    """Persistent view holding the single Open-ticket button of every panel."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🎫 Open ticket", style=discord.ButtonStyle.primary, custom_id=_OPEN_ID)
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._handle_open(interaction)


class TicketView(discord.ui.View):
    """Persistent view with the Close/Claim buttons inside a ticket."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🔒 Close", style=discord.ButtonStyle.danger, custom_id=_CLOSE_ID)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._handle_close_button(interaction)

    @discord.ui.button(label="🙋 Claim", style=discord.ButtonStyle.secondary, custom_id=_CLAIM_ID)
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._handle_claim_button(interaction)


class ConfirmCloseView(discord.ui.View):
    """Short-lived (non-persistent) confirmation before closing a ticket."""

    def __init__(self, cog: "Tickets", lang: str, user_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id
        self.confirm.label = tr_lang(lang, "✅ Schließen bestätigen", "✅ Confirm close")
        self.cancel.label = tr_lang(lang, "Abbrechen", "Cancel")

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.defer()
            return
        self.stop()
        await self.cog._do_close(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.defer()
            return
        self.stop()
        lang = await self.cog._lang(interaction.guild)
        await interaction.response.edit_message(
            content=tr_lang(lang, "Abgebrochen.", "Cancelled."), view=None
        )


class Tickets(commands.Cog):
    """Ticket-Tool-style support tickets with panels, transcripts and claiming."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x71C4E7_5, force_registration=True)
        self.config.register_guild(
            language="en-US",
            enabled=True,
            panels=[],  # {id, channel, message, subject, button_label}
            support_roles=[],  # role IDs
            ticket_category=None,  # category channel ID (for channel mode / thread parent fallback)
            use_threads=True,  # private threads (True) or text channels (False)
            max_open=3,  # max open tickets per user
            welcome_message=_DEFAULT_WELCOME,  # supports {user}, {subject}, {ticket}
            log_channel=None,  # transcripts are posted here on close
            retention_days=30,  # prune closed/deleted records after N days
            next_ticket=1,  # incrementing ticket number
            tickets=[],  # ticket records, see _handle_open()
        )
        self._panel_view: Optional[PanelView] = None
        self._ticket_view: Optional[TicketView] = None
        self._task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        register_dashboard(self)
        # Persistent views: re-registered on every load so the buttons keep
        # working after bot restarts.
        self._panel_view = PanelView(self)
        self._ticket_view = TicketView(self)
        self.bot.add_view(self._panel_view)
        self.bot.add_view(self._ticket_view)
        self._task = asyncio.create_task(self._prune_loop())

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        if self._task:
            self._task.cancel()
        if self._panel_view:
            self._panel_view.stop()
        if self._ticket_view:
            self._ticket_view.stop()

    # ------------------------------------------------------------------ #
    # End user data statements
    # ------------------------------------------------------------------ #
    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Anonymize the user's IDs in ticket records of all guilds.

        Stored transcript files are message logs of closed tickets and may
        still contain the user's messages; they are removed automatically
        after the configured retention period.
        """
        all_guilds = await self.config.all_guilds()
        for gid in all_guilds:
            async with self.config.guild_from_id(gid).tickets() as tickets:
                for t in tickets:
                    if t.get("opener") == user_id:
                        t["opener"] = None
                    if t.get("claimer") == user_id:
                        t["claimer"] = None

    async def red_get_data_for_user(self, *, user_id: int) -> dict:
        """Return the ticket records referencing the user."""
        found = []
        all_guilds = await self.config.all_guilds()
        for gid, gdata in all_guilds.items():
            for t in gdata.get("tickets", []):
                if user_id in (t.get("opener"), t.get("claimer")):
                    found.append({"guild": gid, **t})
        return {"tickets": found}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _lang(self, guild: Optional[discord.Guild]) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    def _is_support(self, member: discord.Member, support_roles: List[int]) -> bool:
        """Support = has one of the support roles or manage_guild permission."""
        if member.guild_permissions.manage_guild:
            return True
        ids = {r.id for r in member.roles}
        return any(rid in ids for rid in support_roles)

    def _transcript_dir(self) -> Path:
        path = cog_data_path(self) / "transcripts"
        path.mkdir(parents=True, exist_ok=True)
        return path

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

    def _ticket_lines(self, guild: discord.Guild, tickets: List[dict], lang: str) -> List[str]:
        lines = []
        for t in tickets:
            opener = guild.get_member(t.get("opener") or 0)
            opener_name = opener.display_name if opener else (f"<@{t['opener']}>" if t.get("opener") else "?")
            ch = guild.get_channel_or_thread(t.get("channel") or 0) if hasattr(guild, "get_channel_or_thread") else guild.get_channel(t.get("channel") or 0)
            where = ch.mention if ch else "—"
            claimer = guild.get_member(t.get("claimer") or 0)
            claim = f" · 🙋 {claimer.display_name}" if claimer else ""
            lines.append(
                f"`#{t.get('id'):04d}` · {where} · **{t.get('subject', '?')}** · "
                f"{opener_name} · {t.get('status')}{claim} · <t:{int(t.get('created', 0))}:R>"
            )
        return lines

    # ------------------------------------------------------------------ #
    # Ticket opening (persistent panel button)
    # ------------------------------------------------------------------ #
    async def _handle_open(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or interaction.message is None:
            return
        gconf = await self.config.guild(guild).all()
        lang = str(gconf.get("language") or "en-US")
        if not gconf.get("enabled", True):
            await interaction.response.send_message(
                tr_lang(lang, "Das Ticketsystem ist deaktiviert.", "The ticket system is disabled."),
                ephemeral=True,
            )
            return
        panel = next(
            (p for p in gconf.get("panels", []) if p.get("message") == interaction.message.id), None
        )
        if panel is None:
            await interaction.response.send_message(
                tr_lang(lang, "Dieses Panel existiert nicht mehr.", "This panel no longer exists."),
                ephemeral=True,
            )
            return
        # Enforce the per-user open ticket limit.
        max_open = max(1, int(gconf.get("max_open") or 3))
        open_count = sum(
            1
            for t in gconf.get("tickets", [])
            if t.get("opener") == interaction.user.id and t.get("status") == "open"
        )
        if open_count >= max_open:
            await interaction.response.send_message(
                tr_lang(
                    lang,
                    f"Du hast bereits **{open_count}** offene Tickets (Maximum: {max_open}).",
                    f"You already have **{open_count}** open tickets (maximum: {max_open}).",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            ticket = await self._create_ticket(interaction, panel, gconf, lang)
        except discord.Forbidden:
            await interaction.followup.send(
                tr_lang(
                    lang,
                    "Mir fehlen Berechtigungen, um das Ticket zu erstellen.",
                    "I'm missing permissions to create the ticket.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception("Ticket creation failed in guild %s", guild.id)
            await interaction.followup.send(
                tr_lang(lang, "Ticket konnte nicht erstellt werden.", "Could not create the ticket."),
                ephemeral=True,
            )
            return
        ch = guild.get_thread(ticket["channel"]) or guild.get_channel(ticket["channel"])
        await interaction.followup.send(
            tr_lang(
                lang,
                f"Ticket `#{ticket['id']:04d}` erstellt: {ch.mention if ch else ''}",
                f"Ticket `#{ticket['id']:04d}` created: {ch.mention if ch else ''}",
            ),
            ephemeral=True,
        )

    async def _create_ticket(self, interaction: discord.Interaction, panel: dict, gconf: dict, lang: str) -> dict:
        """Create the thread/channel, post the welcome message, store the record."""
        guild = interaction.guild
        opener = interaction.user
        use_threads = bool(gconf.get("use_threads", True))
        support_roles = [guild.get_role(r) for r in gconf.get("support_roles", [])]
        support_roles = [r for r in support_roles if r is not None]

        # Allocate the next ticket number.
        number = int(await self.config.guild(guild).next_ticket())
        await self.config.guild(guild).next_ticket.set(number + 1)
        name = f"ticket-{number:04d}"

        if use_threads:
            parent = interaction.channel
            thread = await parent.create_thread(
                name=name,
                type=discord.ChannelType.private_thread,
                invitable=False,
                reason=f"Ticket #{number:04d} by {opener}",
            )
            await thread.add_user(opener)
            target = thread
        else:
            category = guild.get_channel(gconf.get("ticket_category") or 0)
            if not isinstance(category, discord.CategoryChannel):
                category = None
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
                opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
            }
            for role in support_roles:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)
            target = await guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket #{number:04d} by {opener}",
            )

        subject = str(panel.get("subject") or "Support")
        welcome = str(gconf.get("welcome_message") or _DEFAULT_WELCOME)
        try:
            text = welcome.format(user=opener.mention, subject=subject, ticket=f"#{number:04d}")
        except (KeyError, IndexError, ValueError):
            # Broken template placeholders should never block ticket creation.
            text = _DEFAULT_WELCOME.format(user=opener.mention, subject=subject, ticket=f"#{number:04d}")
        embed = discord.Embed(
            title=f"🎫 Ticket #{number:04d} — {subject}",
            description=text,
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(
            text=tr_lang(lang, "Zum Schließen den Button nutzen.", "Use the button to close this ticket.")
        )
        # Mentioning the support roles adds them to private threads.
        mentions = " ".join(r.mention for r in support_roles)
        await target.send(
            content=mentions or None,
            embed=embed,
            view=self._ticket_view or TicketView(self),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )

        ticket = {
            "id": number,
            "opener": opener.id,
            "channel": target.id,
            "panel_id": panel.get("id"),
            "subject": subject,
            "status": "open",
            "claimer": None,
            "created": time.time(),
            "closed": None,
            "thread": use_threads,
            "transcript": None,  # transcript file basename (without extension)
        }
        async with self.config.guild(guild).tickets() as tickets:
            tickets.append(ticket)
        return ticket

    # ------------------------------------------------------------------ #
    # Claim / close / reopen / delete
    # ------------------------------------------------------------------ #
    async def _find_ticket(self, guild: discord.Guild, channel_id: int) -> Optional[dict]:
        tickets = await self.config.guild(guild).tickets()
        return next((t for t in tickets if t.get("channel") == channel_id), None)

    async def _handle_claim_button(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or interaction.channel is None:
            return
        lang = await self._lang(guild)
        support_roles = await self.config.guild(guild).support_roles()
        member = guild.get_member(interaction.user.id)
        if member is None or not self._is_support(member, support_roles):
            await interaction.response.send_message(
                tr_lang(lang, "Nur Support kann Tickets übernehmen.", "Only support staff can claim tickets."),
                ephemeral=True,
            )
            return
        claimed = False
        async with self.config.guild(guild).tickets() as tickets:
            t = next((x for x in tickets if x.get("channel") == interaction.channel.id), None)
            if t is not None and t.get("status") == "open" and not t.get("claimer"):
                t["claimer"] = member.id
                claimed = True
        if claimed:
            await interaction.response.send_message(
                tr_lang(
                    lang,
                    f"🙋 {member.mention} hat dieses Ticket übernommen.",
                    f"🙋 {member.mention} claimed this ticket.",
                )
            )
        else:
            await interaction.response.send_message(
                tr_lang(lang, "Ticket ist bereits übernommen oder geschlossen.", "Ticket is already claimed or closed."),
                ephemeral=True,
            )

    async def _handle_close_button(self, interaction: discord.Interaction) -> None:
        """Ask for confirmation before closing (opener or support only)."""
        guild = interaction.guild
        if guild is None or interaction.channel is None:
            return
        lang = await self._lang(guild)
        t = await self._find_ticket(guild, interaction.channel.id)
        if t is None or t.get("status") != "open":
            await interaction.response.send_message(
                tr_lang(lang, "Dieses Ticket ist nicht offen.", "This ticket is not open."), ephemeral=True
            )
            return
        support_roles = await self.config.guild(guild).support_roles()
        member = guild.get_member(interaction.user.id)
        allowed = interaction.user.id == t.get("opener") or (
            member is not None and self._is_support(member, support_roles)
        )
        if not allowed:
            await interaction.response.send_message(
                tr_lang(
                    lang,
                    "Nur der Ersteller oder Support kann das Ticket schließen.",
                    "Only the opener or support staff can close this ticket.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            tr_lang(lang, "Ticket wirklich schließen?", "Really close this ticket?"),
            view=ConfirmCloseView(self, lang, interaction.user.id),
            ephemeral=True,
        )

    async def _do_close(self, interaction: discord.Interaction) -> None:
        """Actually close the ticket after the confirmation click."""
        guild = interaction.guild
        channel = interaction.channel
        lang = await self._lang(guild)
        await interaction.response.edit_message(
            content=tr_lang(lang, "Schließe Ticket…", "Closing ticket…"), view=None
        )
        t = await self._find_ticket(guild, channel.id)
        if t is None or t.get("status") != "open":
            return
        await self._close_ticket(guild, channel, t, closer=interaction.user, lang=lang)

    async def _close_ticket(
        self,
        guild: discord.Guild,
        channel,
        ticket: dict,
        *,
        closer,
        lang: str,
    ) -> None:
        """Generate the transcript, store/post it and lock the ticket."""
        basename = f"ticket_{guild.id}_{ticket['id']:04d}_{uuid.uuid4().hex[:6]}"
        try:
            txt_path, html_path = await self._write_transcript(guild, channel, ticket, basename, lang)
        except Exception:
            log.exception("Transcript generation failed for ticket %s", ticket.get("id"))
            txt_path = html_path = None

        async with self.config.guild(guild).tickets() as tickets:
            t = next((x for x in tickets if x.get("id") == ticket.get("id")), None)
            if t is None or t.get("status") != "open":
                return
            t["status"] = "closed"
            t["closed"] = time.time()
            if txt_path is not None:
                t["transcript"] = basename

        # Post the transcript files to the log channel (if configured).
        log_channel = guild.get_channel(await self.config.guild(guild).log_channel() or 0)
        if log_channel is not None and txt_path is not None:
            opener = guild.get_member(ticket.get("opener") or 0)
            try:
                await log_channel.send(
                    tr_lang(
                        lang,
                        f"📑 Transkript Ticket `#{ticket['id']:04d}` — **{ticket.get('subject')}** "
                        f"(Ersteller: {opener or ticket.get('opener')}, geschlossen von {closer})",
                        f"📑 Transcript ticket `#{ticket['id']:04d}` — **{ticket.get('subject')}** "
                        f"(opener: {opener or ticket.get('opener')}, closed by {closer})",
                    ),
                    files=[discord.File(str(txt_path)), discord.File(str(html_path))],
                )
            except discord.HTTPException:
                log.warning("Could not post transcript to log channel in guild %s", guild.id)

        # Announce and lock the ticket.
        try:
            await channel.send(
                tr_lang(
                    lang,
                    f"🔒 Ticket geschlossen von {closer.mention}.",
                    f"🔒 Ticket closed by {closer.mention}.",
                )
            )
        except discord.HTTPException:
            pass
        try:
            if isinstance(channel, discord.Thread):
                await channel.edit(archived=True, locked=True)
            else:
                opener = guild.get_member(ticket.get("opener") or 0)
                if opener is not None:
                    await channel.set_permissions(opener, view_channel=True, send_messages=False)
        except discord.HTTPException:
            pass

    async def _write_transcript(self, guild, channel, ticket, basename: str, lang: str):
        """Collect the channel history and write .txt + .html transcript files."""
        messages = []
        async for msg in channel.history(limit=500, oldest_first=True):
            attachments = " ".join(a.url for a in msg.attachments)
            content = msg.content or ""
            if msg.embeds and not content:
                content = "[embed]"
            messages.append(
                (
                    msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    f"{msg.author} ({msg.author.id})",
                    (content + (" " + attachments if attachments else "")).strip(),
                )
            )

        head = f"Ticket #{ticket['id']:04d} — {ticket.get('subject')} — {guild.name}"
        txt_lines = [head, "=" * len(head), ""]
        txt_lines += [f"[{ts}] {author}: {content}" for ts, author, content in messages]
        txt_data = "\n".join(txt_lines) + "\n"

        rows = "\n".join(
            f"<div class='msg'><span class='ts'>{html_mod.escape(ts)}</span> "
            f"<span class='author'>{html_mod.escape(author)}</span>"
            f"<div class='content'>{html_mod.escape(content)}</div></div>"
            for ts, author, content in messages
        )
        html_data = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{html_mod.escape(head)}</title>"
            "<style>body{font-family:sans-serif;background:#2b2d31;color:#dbdee1;padding:20px}"
            ".msg{margin-bottom:10px}.ts{color:#949ba4;font-size:12px}"
            ".author{font-weight:bold;color:#f2f3f5}.content{white-space:pre-wrap}</style></head>"
            f"<body><h2>{html_mod.escape(head)}</h2>{rows}</body></html>\n"
        )

        directory = self._transcript_dir()
        txt_path = directory / f"{basename}.txt"
        html_path = directory / f"{basename}.html"

        def _write() -> None:
            # File IO runs in a thread so the event loop is never blocked.
            txt_path.write_text(txt_data, encoding="utf-8")
            html_path.write_text(html_data, encoding="utf-8")

        await asyncio.to_thread(_write)
        return txt_path, html_path

    # ------------------------------------------------------------------ #
    # Prune loop (record + transcript retention)
    # ------------------------------------------------------------------ #
    async def _prune_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._prune_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ticket prune tick failed")
            await asyncio.sleep(3600)

    async def _prune_once(self) -> None:
        now = time.time()
        for guild in self.bot.guilds:
            retention = max(1, int(await self.config.guild(guild).retention_days() or 30))
            cutoff = now - retention * 86400
            stale_files: List[str] = []
            async with self.config.guild(guild).tickets() as tickets:
                keep = []
                for t in tickets:
                    if t.get("status") in ("closed", "deleted") and (t.get("closed") or 0) < cutoff:
                        if t.get("transcript"):
                            stale_files.append(t["transcript"])
                        continue
                    keep.append(t)
                tickets[:] = keep
            if stale_files:
                directory = self._transcript_dir()

                def _remove(files=stale_files, directory=directory) -> None:
                    for base in files:
                        for suffix in (".txt", ".html"):
                            try:
                                (directory / f"{base}{suffix}").unlink(missing_ok=True)
                            except OSError:
                                pass

                await asyncio.to_thread(_remove)

    # ------------------------------------------------------------------ #
    # Ticket commands (inside tickets / listing)
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="ticket")
    @commands.guild_only()
    async def ticket(self, ctx: commands.Context) -> None:
        """Manage support tickets."""

    @ticket.command(name="close")
    async def ticket_close(self, ctx: commands.Context) -> None:
        """Close the current ticket (opener or support)."""
        lang = await self._lang(ctx.guild)
        t = await self._find_ticket(ctx.guild, ctx.channel.id)
        if t is None or t.get("status") != "open":
            await ctx.send(tr_lang(lang, "Dies ist kein offenes Ticket.", "This is not an open ticket."))
            return
        support_roles = await self.config.guild(ctx.guild).support_roles()
        if ctx.author.id != t.get("opener") and not self._is_support(ctx.author, support_roles):
            await ctx.send(tr_lang(
                lang,
                "Nur der Ersteller oder Support kann das Ticket schließen.",
                "Only the opener or support staff can close this ticket.",
            ))
            return
        await self._close_ticket(ctx.guild, ctx.channel, t, closer=ctx.author, lang=lang)

    @ticket.command(name="claim")
    async def ticket_claim(self, ctx: commands.Context) -> None:
        """Claim the current ticket (support only)."""
        lang = await self._lang(ctx.guild)
        support_roles = await self.config.guild(ctx.guild).support_roles()
        if not self._is_support(ctx.author, support_roles):
            await ctx.send(tr_lang(lang, "Nur Support kann Tickets übernehmen.", "Only support staff can claim tickets."))
            return
        claimed = False
        async with self.config.guild(ctx.guild).tickets() as tickets:
            t = next((x for x in tickets if x.get("channel") == ctx.channel.id), None)
            if t is not None and t.get("status") == "open" and not t.get("claimer"):
                t["claimer"] = ctx.author.id
                claimed = True
        await ctx.send(tr_lang(
            lang,
            f"🙋 Ticket übernommen von {ctx.author.mention}." if claimed else "Ticket ist bereits übernommen oder geschlossen.",
            f"🙋 Ticket claimed by {ctx.author.mention}." if claimed else "Ticket is already claimed or closed.",
        ))

    @ticket.command(name="reopen")
    async def ticket_reopen(self, ctx: commands.Context) -> None:
        """Reopen the current closed ticket (support only)."""
        lang = await self._lang(ctx.guild)
        support_roles = await self.config.guild(ctx.guild).support_roles()
        if not self._is_support(ctx.author, support_roles):
            await ctx.send(tr_lang(lang, "Nur Support kann Tickets wieder öffnen.", "Only support staff can reopen tickets."))
            return
        reopened = None
        async with self.config.guild(ctx.guild).tickets() as tickets:
            t = next((x for x in tickets if x.get("channel") == ctx.channel.id), None)
            if t is not None and t.get("status") == "closed":
                t["status"] = "open"
                t["closed"] = None
                reopened = t
        if reopened is None:
            await ctx.send(tr_lang(lang, "Dies ist kein geschlossenes Ticket.", "This is not a closed ticket."))
            return
        try:
            if isinstance(ctx.channel, discord.Thread):
                await ctx.channel.edit(archived=False, locked=False)
            else:
                opener = ctx.guild.get_member(reopened.get("opener") or 0)
                if opener is not None:
                    await ctx.channel.set_permissions(opener, view_channel=True, send_messages=True)
        except discord.HTTPException:
            pass
        await ctx.send(tr_lang(lang, "🔓 Ticket wieder geöffnet.", "🔓 Ticket reopened."))

    @ticket.command(name="delete")
    @commands.admin_or_permissions(manage_guild=True)
    async def ticket_delete(self, ctx: commands.Context) -> None:
        """Delete the current ticket channel/thread (admin only, record is kept)."""
        lang = await self._lang(ctx.guild)
        t = await self._find_ticket(ctx.guild, ctx.channel.id)
        if t is None:
            await ctx.send(tr_lang(lang, "Dies ist kein Ticket.", "This is not a ticket."))
            return
        async with self.config.guild(ctx.guild).tickets() as tickets:
            rec = next((x for x in tickets if x.get("id") == t.get("id")), None)
            if rec is not None:
                rec["status"] = "deleted"
                rec["closed"] = rec.get("closed") or time.time()
        try:
            await ctx.channel.delete(reason=f"Ticket #{t['id']:04d} deleted by {ctx.author}")
        except discord.HTTPException:
            await ctx.send(tr_lang(lang, "Kanal konnte nicht gelöscht werden.", "Could not delete the channel."))

    @ticket.command(name="mine")
    async def ticket_mine(self, ctx: commands.Context) -> None:
        """Show your own tickets."""
        lang = await self._lang(ctx.guild)
        tickets = [
            t for t in await self.config.guild(ctx.guild).tickets() if t.get("opener") == ctx.author.id
        ]
        if not tickets:
            await ctx.send(tr_lang(lang, "Du hast keine Tickets.", "You have no tickets."))
            return
        lines = self._ticket_lines(ctx.guild, tickets, lang)
        colour = await ctx.embed_colour()
        title = tr_lang(lang, "Deine Tickets", "Your tickets")
        pages = [
            discord.Embed(title=title, description="\n".join(lines[i:i + 10])[:4000], colour=colour)
            for i in range(0, len(lines), 10)
        ]
        await self._send_pages(ctx, pages)

    @ticket.command(name="list")
    @commands.mod_or_permissions(manage_messages=True)
    async def ticket_list(self, ctx: commands.Context) -> None:
        """List all open tickets (support/mod)."""
        lang = await self._lang(ctx.guild)
        tickets = [t for t in await self.config.guild(ctx.guild).tickets() if t.get("status") == "open"]
        if not tickets:
            await ctx.send(tr_lang(lang, "Keine offenen Tickets.", "No open tickets."))
            return
        lines = self._ticket_lines(ctx.guild, tickets, lang)
        colour = await ctx.embed_colour()
        title = tr_lang(lang, "Offene Tickets", "Open tickets")
        pages = [
            discord.Embed(title=title, description="\n".join(lines[i:i + 10])[:4000], colour=colour)
            for i in range(0, len(lines), 10)
        ]
        await self._send_pages(ctx, pages)

    # ------------------------------------------------------------------ #
    # Configuration (admin)
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="ticketset")
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def ticketset(self, ctx: commands.Context) -> None:
        """Configure the ticket system."""

    @ticketset.command(name="panel")
    @app_commands.describe(
        channel="Channel to post the panel in",
        subject="Subject shown on the panel and in ticket titles",
    )
    async def ts_panel(self, ctx: commands.Context, channel: discord.TextChannel, *, subject: str) -> None:
        """Create a ticket panel (embed with an Open-ticket button) in a channel."""
        lang = await self._lang(ctx.guild)
        if not channel.permissions_for(ctx.guild.me).send_messages:
            await ctx.send(tr_lang(lang, "Keine Senderechte in dem Kanal.", "I can't send messages in that channel."))
            return
        subject = subject.strip()[:100]
        embed = discord.Embed(
            title=f"🎫 {subject}",
            description=tr_lang(
                lang,
                "Klick auf **🎫 Open ticket**, um ein Support-Ticket zu eröffnen.",
                "Click **🎫 Open ticket** to open a support ticket.",
            ),
            colour=discord.Colour.blurple(),
        )
        msg = await channel.send(embed=embed, view=self._panel_view or PanelView(self))
        panel = {
            "id": uuid.uuid4().hex[:8],
            "channel": channel.id,
            "message": msg.id,
            "subject": subject,
        }
        async with self.config.guild(ctx.guild).panels() as panels:
            panels.append(panel)
        await ctx.send(tr_lang(
            lang,
            f"Panel erstellt in {channel.mention} (ID `{panel['id']}`).",
            f"Panel created in {channel.mention} (ID `{panel['id']}`).",
        ))

    @ticketset.command(name="panels")
    async def ts_panels(self, ctx: commands.Context) -> None:
        """List all ticket panels."""
        lang = await self._lang(ctx.guild)
        panels = await self.config.guild(ctx.guild).panels()
        if not panels:
            await ctx.send(tr_lang(lang, "Keine Panels vorhanden.", "No panels yet."))
            return
        lines = []
        for p in panels:
            ch = ctx.guild.get_channel(p.get("channel") or 0)
            lines.append(f"`{p.get('id')}` · {ch.mention if ch else '?'} · **{p.get('subject')}**")
        await ctx.send("\n".join(lines)[:1900])

    @ticketset.command(name="removepanel")
    @app_commands.describe(panel_id="The panel ID (from 'ticketset panels')")
    async def ts_removepanel(self, ctx: commands.Context, panel_id: str) -> None:
        """Remove a ticket panel (and its message, if possible)."""
        lang = await self._lang(ctx.guild)
        removed = None
        async with self.config.guild(ctx.guild).panels() as panels:
            removed = next((p for p in panels if p.get("id") == panel_id), None)
            if removed is not None:
                panels.remove(removed)
        if removed is None:
            await ctx.send(tr_lang(lang, "Panel nicht gefunden.", "Panel not found."))
            return
        channel = ctx.guild.get_channel(removed.get("channel") or 0)
        if channel is not None:
            try:
                msg = await channel.fetch_message(removed.get("message"))
                await msg.delete()
            except discord.HTTPException:
                pass
        await ctx.send(tr_lang(lang, "Panel entfernt.", "Panel removed."))

    @ticketset.command(name="supportrole")
    @app_commands.describe(action="add or remove", role="The support role")
    async def ts_supportrole(self, ctx: commands.Context, action: str, role: discord.Role) -> None:
        """Add or remove a support role."""
        lang = await self._lang(ctx.guild)
        action = action.lower()
        async with self.config.guild(ctx.guild).support_roles() as roles:
            if action == "add":
                if role.id not in roles:
                    roles.append(role.id)
                msg = tr_lang(lang, f"Support-Rolle hinzugefügt: {role.mention}", f"Support role added: {role.mention}")
            elif action == "remove":
                if role.id in roles:
                    roles.remove(role.id)
                msg = tr_lang(lang, f"Support-Rolle entfernt: {role.mention}", f"Support role removed: {role.mention}")
            else:
                msg = tr_lang(lang, "Aktion muss `add` oder `remove` sein.", "Action must be `add` or `remove`.")
        await ctx.send(msg)

    @ticketset.command(name="category")
    @app_commands.describe(category="Category for ticket channels (leave empty to clear)")
    async def ts_category(self, ctx: commands.Context, category: Optional[discord.CategoryChannel] = None) -> None:
        """Set (or clear) the category used for ticket text channels."""
        lang = await self._lang(ctx.guild)
        if category is None:
            await self.config.guild(ctx.guild).ticket_category.clear()
            await ctx.send(tr_lang(lang, "Kategorie entfernt.", "Category cleared."))
            return
        await self.config.guild(ctx.guild).ticket_category.set(category.id)
        await ctx.send(tr_lang(lang, f"Ticket-Kategorie: **{category.name}**", f"Ticket category: **{category.name}**"))

    @ticketset.command(name="mode")
    @app_commands.describe(mode="threads or channels")
    async def ts_mode(self, ctx: commands.Context, mode: str) -> None:
        """Choose whether tickets use private threads or text channels."""
        lang = await self._lang(ctx.guild)
        use_threads = mode.lower().startswith("t")
        await self.config.guild(ctx.guild).use_threads.set(use_threads)
        await ctx.send(tr_lang(
            lang,
            "Tickets nutzen jetzt **private Threads**." if use_threads else "Tickets nutzen jetzt **Textkanäle**.",
            "Tickets now use **private threads**." if use_threads else "Tickets now use **text channels**.",
        ))

    @ticketset.command(name="maxopen")
    @app_commands.describe(amount="Maximum open tickets per user (1-10)")
    async def ts_maxopen(self, ctx: commands.Context, amount: int) -> None:
        """Set the maximum number of open tickets per user."""
        lang = await self._lang(ctx.guild)
        if not 1 <= amount <= 10:
            await ctx.send(tr_lang(lang, "Wert muss 1–10 sein.", "Value must be 1–10."))
            return
        await self.config.guild(ctx.guild).max_open.set(amount)
        await ctx.send(tr_lang(lang, f"Maximal **{amount}** offene Tickets pro Nutzer.", f"Maximum **{amount}** open tickets per user."))

    @ticketset.command(name="welcome")
    @app_commands.describe(message="Welcome template ({user}, {subject}, {ticket})")
    async def ts_welcome(self, ctx: commands.Context, *, message: str) -> None:
        """Set the welcome message template ({user}, {subject}, {ticket})."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).welcome_message.set(message.strip()[:1500])
        await ctx.send(tr_lang(lang, "Willkommensnachricht gespeichert.", "Welcome message saved."))

    @ticketset.command(name="logchannel")
    @app_commands.describe(channel="Channel for transcripts (leave empty to clear)")
    async def ts_logchannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Set (or clear) the transcript log channel."""
        lang = await self._lang(ctx.guild)
        if channel is None:
            await self.config.guild(ctx.guild).log_channel.clear()
            await ctx.send(tr_lang(lang, "Log-Kanal entfernt.", "Log channel cleared."))
            return
        await self.config.guild(ctx.guild).log_channel.set(channel.id)
        await ctx.send(tr_lang(lang, f"Transkripte gehen an {channel.mention}.", f"Transcripts go to {channel.mention}."))

    @ticketset.command(name="retention")
    @app_commands.describe(days="Days to keep closed ticket records/transcripts (1-365)")
    async def ts_retention(self, ctx: commands.Context, days: int) -> None:
        """Set how long closed ticket records and transcripts are kept."""
        lang = await self._lang(ctx.guild)
        if not 1 <= days <= 365:
            await ctx.send(tr_lang(lang, "Tage müssen 1–365 sein.", "Days must be 1–365."))
            return
        await self.config.guild(ctx.guild).retention_days.set(days)
        await ctx.send(tr_lang(
            lang,
            f"Geschlossene Tickets werden **{days}** Tag(e) aufbewahrt.",
            f"Closed tickets are kept for **{days}** day(s).",
        ))

    @ticketset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def ts_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(tr_lang(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel (guild settings)
    # ------------------------------------------------------------------ #
    @dashboard_panel("tickets", L("Tickets", "Tickets"), mount="guild_settings", permission="guild_admin", order=60)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        panels = await conf.panels()
        listing = "\n".join(
            f"• `{p.get('id')}` — {p.get('subject')}" for p in panels[:15]
        ) or "—"
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Panels per Befehl `ticketset panel` erstellen.\nPanels:\n{listing}",
                f"Create panels with `ticketset panel`.\nPanels:\n{listing}",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.switch("use_threads", L("Private Threads statt Textkanälen", "Private threads instead of text channels"), value=bool(await conf.use_threads())),
                Field.channel("ticket_category", L("Ticket-Kategorie (Kanalmodus)", "Ticket category (channel mode)"), value=str(await conf.ticket_category() or "")),
                Field.channel("log_channel", L("Log-Kanal für Transkripte", "Transcript log channel"), value=str(await conf.log_channel() or "")),
                Field.number("max_open", L("Max. offene Tickets pro Nutzer", "Max open tickets per user"), value=int(await conf.max_open() or 3), min=1, max=10),
                Field.number("retention_days", L("Aufbewahrung geschlossener Tickets (Tage)", "Closed ticket retention (days)"), value=int(await conf.retention_days() or 30), min=1, max=365),
                Field.textarea("welcome_message", L("Willkommensnachricht ({user}, {subject}, {ticket})", "Welcome message ({user}, {subject}, {ticket})"), value=str(await conf.welcome_message() or _DEFAULT_WELCOME)),
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

        max_open = _int_in("max_open", 1, 10, 3)
        retention = _int_in("retention_days", 1, 365, 30)
        if errors:
            return SubmitResult.fail(tr_lang(lang, "Bitte Eingaben prüfen.", "Please check your input."), errors)

        await conf.enabled.set(bool(data.get("enabled")))
        await conf.use_threads.set(bool(data.get("use_threads")))
        category = str(data.get("ticket_category") or "").strip()
        await (conf.ticket_category.set(int(category)) if category.isdigit() else conf.ticket_category.clear())
        log_channel = str(data.get("log_channel") or "").strip()
        await (conf.log_channel.set(int(log_channel)) if log_channel.isdigit() else conf.log_channel.clear())
        await conf.max_open.set(max_open)
        await conf.retention_days.set(retention)
        welcome = str(data.get("welcome_message") or "").strip()[:1500]
        await conf.welcome_message.set(welcome or _DEFAULT_WELCOME)
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard page: ticket overview + transcript viewer (guild scope)
    # ------------------------------------------------------------------ #
    @dashboard_page(
        "tickets",
        L("Ticket-Übersicht", "Ticket overview"),
        scope="guild",
        permission="guild_mod",
        icon="ticket",
    )
    async def overview_page(self, ctx):
        tickets = await self.config.guild(ctx.guild).tickets()
        rows = []
        for t in sorted(tickets, key=lambda x: -float(x.get("created", 0))):
            opener = ctx.guild.get_member(t.get("opener") or 0)
            claimer = ctx.guild.get_member(t.get("claimer") or 0)
            created = datetime.datetime.fromtimestamp(
                float(t.get("created", 0)), datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            rows.append({
                "id": f"#{int(t.get('id', 0)):04d}",
                "subject": str(t.get("subject", ""))[:60],
                "opener": opener.display_name if opener else (str(t.get("opener")) if t.get("opener") else "—"),
                "status": str(t.get("status", "?")),
                "claimer": claimer.display_name if claimer else "—",
                "created": created,
                "transcript": "yes" if t.get("transcript") else "—",
            })
        open_count = sum(1 for r in rows if r["status"] == "open")

        comps = [
            Component.heading(L("Ticket-Übersicht", "Ticket overview")),
            Component.text(L(
                f"{open_count} offen · {len(rows) - open_count} geschlossen/gelöscht",
                f"{open_count} open · {len(rows) - open_count} closed/deleted",
            )),
        ]
        if rows:
            comps.append(Component.table(
                columns=[
                    {"key": "id", "label": "ID"},
                    {"key": "subject", "label": L("Betreff", "Subject")},
                    {"key": "opener", "label": L("Ersteller", "Opener")},
                    {"key": "status", "label": L("Status", "Status")},
                    {"key": "claimer", "label": L("Übernommen von", "Claimed by")},
                    {"key": "created", "label": L("Erstellt", "Created")},
                    {"key": "transcript", "label": L("Transkript", "Transcript")},
                ],
                rows=rows[:200],
            ))
        else:
            comps.append(Component.text(L("Keine Tickets vorhanden.", "No tickets yet.")))

        # Transcript viewer: a server-driven dropdown selects the ticket, the
        # stored plain-text transcript is rendered inline.
        with_transcript = [t for t in tickets if t.get("transcript")]
        controls = []
        if with_transcript:
            options = [{"value": "", "label": L("— Transkript wählen —", "— select transcript —")}] + [
                {"value": str(t["transcript"]), "label": f"#{int(t.get('id', 0)):04d} — {str(t.get('subject', ''))[:50]}"}
                for t in with_transcript
            ]
            params = getattr(ctx, "params", None) or {}
            selected = str(params.get("transcript") or "")
            controls.append(Control.select("transcript", L("Transkript ansehen", "View transcript"), options, value=selected))
            valid = {str(t["transcript"]) for t in with_transcript}
            if selected in valid:
                path = self._transcript_dir() / f"{selected}.txt"

                def _read(p=path) -> str:
                    try:
                        return p.read_text(encoding="utf-8")
                    except OSError:
                        return ""

                content = await asyncio.to_thread(_read)
                comps.append(Component.divider())
                if content:
                    comps.append(Component.heading(L("Transkript", "Transcript"), level=3))
                    comps.append(Component.text(content[:8000]))
                else:
                    comps.append(Component.text(L("Transkript-Datei nicht gefunden.", "Transcript file not found.")))
        return PageSchema(components=comps, controls=controls)
