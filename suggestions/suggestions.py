"""Suggestions — numbered suggestion system with votes and staff review.

Members submit suggestions with ``/suggest``; each becomes a numbered embed
with upvote/downvote reactions (optionally with a discussion thread and an
optional mod-review queue before publication). Staff approve/deny/consider
suggestions with a reason — the embed is recolored, a status field is added
and the suggester is notified via DM (best effort). Bilingual output
(DE/EN, default en-US). Web dashboard integration (settings panel +
suggestion list page) via the resilient drop-in.
"""
from __future__ import annotations

import logging
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

log = logging.getLogger("red.pdc.suggestions")  # module logger

DELETED_USER = 0xDE1  # sentinel for anonymized author IDs

# status -> embed colour
STATUS_COLOURS = {
    "open": discord.Colour.blurple(),
    "pending": discord.Colour.light_grey(),
    "approved": discord.Colour.green(),
    "denied": discord.Colour.red(),
    "considered": discord.Colour.orange(),
}


def status_label(status: str, lang: str) -> str:
    """Localized human-readable status label."""
    labels = {
        "open": ("Offen", "Open"),
        "pending": ("Wartet auf Prüfung", "Pending review"),
        "approved": ("Angenommen", "Approved"),
        "denied": ("Abgelehnt", "Denied"),
        "considered": ("In Überlegung", "Under consideration"),
    }
    de, en = labels.get(status, (status, status))
    return tr_lang(lang, de, en)


class Suggestions(commands.Cog):
    """Numbered suggestions with vote reactions and staff review."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5F66E5710, force_registration=True)
        self.config.register_guild(
            enabled=True,
            language="en-US",
            channel=None,  # suggestion channel ID
            review_channel=None,  # mod-review queue channel ID (optional)
            review_mode=False,  # route new suggestions through the review queue
            upvote="👍",
            downvote="👎",
            threads=False,  # create a discussion thread per suggestion
            dm_notify=True,  # DM the suggester on a decision
            next_id=1,
            # suggestion: {id, author, text, message, channel, status, reason}
            suggestions=[],
        )

    async def cog_load(self) -> None:
        register_dashboard(self)

    def cog_unload(self) -> None:
        unregister_dashboard(self)

    # ------------------------------------------------------------------ #
    # Red data APIs
    # ------------------------------------------------------------------ #
    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Anonymize the author IDs of the requesting user's suggestions."""
        for guild_id in await self.config.all_guilds():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            async with self.config.guild(guild).suggestions() as suggestions:
                for s in suggestions:
                    if s.get("author") == user_id:
                        s["author"] = DELETED_USER

    async def red_get_data_for_user(self, *, user_id: int) -> dict:
        """Return the user's stored suggestions."""
        lines: List[str] = []
        for guild_id, data in (await self.config.all_guilds()).items():
            for s in data.get("suggestions") or []:
                if s.get("author") == user_id:
                    lines.append(
                        f"guild {guild_id}: #{s.get('id')} [{s.get('status')}] {s.get('text', '')}"
                    )
        if not lines:
            return {}
        return {"suggestions.txt": "\n".join(lines).encode("utf-8")}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
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

    def _suggestion_embed(self, guild: discord.Guild, s: dict, lang: str) -> discord.Embed:
        """Build the (re-)rendered embed for a suggestion."""
        status = s.get("status", "open")
        e = discord.Embed(
            title=tr_lang(lang, f"Vorschlag #{s.get('id')}", f"Suggestion #{s.get('id')}"),
            description=str(s.get("text", ""))[:4000],
            colour=STATUS_COLOURS.get(status, discord.Colour.blurple()),
        )
        author_id = s.get("author")
        member = guild.get_member(author_id) if author_id else None
        if author_id == DELETED_USER:
            e.set_footer(text=tr_lang(lang, "Gelöschter Nutzer", "Deleted user"))
        elif member is not None:
            e.set_footer(text=member.display_name, icon_url=member.display_avatar.url)
        else:
            e.set_footer(text=str(author_id))
        if status != "open":
            value = status_label(status, lang)
            reason = str(s.get("reason") or "").strip()
            if reason:
                value += f"\n{tr_lang(lang, 'Grund', 'Reason')}: {reason[:900]}"
            e.add_field(name=tr_lang(lang, "Status", "Status"), value=value, inline=False)
        return e

    async def _post_suggestion(self, guild: discord.Guild, s: dict, conf: dict, lang: str) -> Optional[discord.Message]:
        """Post a suggestion into the public channel with vote reactions."""
        channel = guild.get_channel(conf.get("channel") or 0)
        if channel is None:
            return None
        try:
            msg = await channel.send(embed=self._suggestion_embed(guild, s, lang))
        except discord.HTTPException:
            return None
        for emoji in (str(conf.get("upvote") or "👍"), str(conf.get("downvote") or "👎")):
            try:
                await msg.add_reaction(emoji)
            except discord.HTTPException:
                pass  # invalid custom emoji or missing permission
        if conf.get("threads"):
            try:
                await msg.create_thread(
                    name=tr_lang(lang, f"Vorschlag #{s.get('id')}", f"Suggestion #{s.get('id')}")[:100]
                )
            except discord.HTTPException:
                pass
        return msg

    async def _dm_author(self, guild: discord.Guild, s: dict, lang: str) -> None:
        """Best-effort DM to the suggester about the decision."""
        author_id = s.get("author")
        if not author_id or author_id == DELETED_USER:
            return
        member = guild.get_member(author_id)
        if member is None:
            return
        reason = str(s.get("reason") or "").strip()
        text = tr_lang(
            lang,
            f"Dein Vorschlag **#{s.get('id')}** auf **{guild.name}** ist jetzt: **{status_label(s.get('status', ''), lang)}**",
            f"Your suggestion **#{s.get('id')}** on **{guild.name}** is now: **{status_label(s.get('status', ''), lang)}**",
        )
        if reason:
            text += f"\n{tr_lang(lang, 'Grund', 'Reason')}: {reason}"
        try:
            await member.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass  # DMs closed — best effort only

    # ------------------------------------------------------------------ #
    # User command
    # ------------------------------------------------------------------ #
    @commands.hybrid_command(name="suggest")
    @commands.guild_only()
    @app_commands.describe(text="Your suggestion")
    async def suggest(self, ctx: commands.Context, *, text: str) -> None:
        """Submit a suggestion."""
        lang = await self._lang(ctx.guild)
        conf = await self.config.guild(ctx.guild).all()
        if not conf.get("enabled", True):
            await ctx.send(tr_lang(lang, "Vorschläge sind deaktiviert.", "Suggestions are disabled."), ephemeral=True)
            return
        if conf.get("channel") is None:
            await ctx.send(tr_lang(lang, "Es ist kein Vorschlags-Kanal konfiguriert.", "No suggestion channel is configured."), ephemeral=True)
            return
        text = text.strip()[:1900]
        if not text:
            await ctx.send(tr_lang(lang, "Bitte gib einen Text an.", "Please provide some text."), ephemeral=True)
            return
        review = bool(conf.get("review_mode")) and conf.get("review_channel")
        s = {
            "id": int(conf.get("next_id") or 1),
            "author": ctx.author.id,
            "text": text,
            "message": None,
            "channel": None,
            "status": "pending" if review else "open",
            "reason": "",
        }
        if review:
            queue = ctx.guild.get_channel(conf.get("review_channel") or 0)
            if queue is not None:
                try:
                    e = self._suggestion_embed(ctx.guild, s, lang)
                    e.add_field(
                        name=tr_lang(lang, "Prüfung", "Review"),
                        value=tr_lang(
                            lang,
                            f"`suggestion approve {s['id']}` zum Veröffentlichen, `suggestion deny {s['id']}` zum Ablehnen.",
                            f"`suggestion approve {s['id']}` to publish, `suggestion deny {s['id']}` to reject.",
                        ),
                        inline=False,
                    )
                    msg = await queue.send(embed=e)
                    s["message"] = msg.id
                    s["channel"] = queue.id
                except discord.HTTPException:
                    pass
        else:
            msg = await self._post_suggestion(ctx.guild, s, conf, lang)
            if msg is None:
                await ctx.send(tr_lang(lang, "Konnte den Vorschlag nicht posten.", "Could not post the suggestion."), ephemeral=True)
                return
            s["message"] = msg.id
            s["channel"] = msg.channel.id
        async with self.config.guild(ctx.guild).suggestions() as suggestions:
            suggestions.append(s)
        await self.config.guild(ctx.guild).next_id.set(s["id"] + 1)
        await ctx.send(
            tr_lang(
                lang,
                f"Vorschlag **#{s['id']}** eingereicht." + (" (wartet auf Prüfung)" if review else ""),
                f"Suggestion **#{s['id']}** submitted." + (" (awaiting review)" if review else ""),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    # Staff commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="suggestion")
    @commands.mod_or_permissions(manage_messages=True)
    @commands.guild_only()
    async def suggestion(self, ctx: commands.Context) -> None:
        """Review and manage suggestions."""

    async def _decide(self, ctx: commands.Context, suggestion_id: int, status: str, reason: Optional[str]) -> None:
        """Shared implementation for approve/deny/consider."""
        lang = await self._lang(ctx.guild)
        conf = await self.config.guild(ctx.guild).all()
        decided = None
        async with self.config.guild(ctx.guild).suggestions() as suggestions:
            s = next((x for x in suggestions if int(x.get("id", 0)) == suggestion_id), None)
            if s is None:
                await ctx.send(tr_lang(lang, "Vorschlag nicht gefunden.", "Suggestion not found."))
                return
            was_pending = s.get("status") == "pending"
            s["status"] = status
            s["reason"] = (reason or "").strip()[:900]
            if was_pending and status in ("approved", "considered", "open"):
                # A reviewed suggestion gets published to the public channel now.
                old_channel = ctx.guild.get_channel(s.get("channel") or 0)
                old_message_id = s.get("message")
                msg = await self._post_suggestion(ctx.guild, s, conf, lang)
                if msg is not None:
                    s["message"] = msg.id
                    s["channel"] = msg.channel.id
                    # Remove the queue message (best effort).
                    if old_channel is not None and old_message_id:
                        try:
                            old = await old_channel.fetch_message(old_message_id)
                            await old.delete()
                        except discord.HTTPException:
                            pass
            else:
                # Recolor / update the existing embed in place.
                channel = ctx.guild.get_channel(s.get("channel") or 0)
                if channel is not None and s.get("message"):
                    try:
                        msg = await channel.fetch_message(s["message"])
                        await msg.edit(embed=self._suggestion_embed(ctx.guild, s, lang))
                    except discord.HTTPException:
                        pass
            decided = dict(s)
        if decided is not None:
            if conf.get("dm_notify", True):
                await self._dm_author(ctx.guild, decided, lang)
            await ctx.send(tr_lang(
                lang,
                f"Vorschlag **#{suggestion_id}**: **{status_label(status, lang)}**.",
                f"Suggestion **#{suggestion_id}**: **{status_label(status, lang)}**.",
            ))

    @suggestion.command(name="approve")
    @app_commands.describe(suggestion_id="The suggestion number", reason="Optional reason")
    async def sg_approve(self, ctx: commands.Context, suggestion_id: int, *, reason: Optional[str] = None) -> None:
        """Approve a suggestion (with optional reason)."""
        await self._decide(ctx, suggestion_id, "approved", reason)

    @suggestion.command(name="deny")
    @app_commands.describe(suggestion_id="The suggestion number", reason="Optional reason")
    async def sg_deny(self, ctx: commands.Context, suggestion_id: int, *, reason: Optional[str] = None) -> None:
        """Deny a suggestion (with optional reason)."""
        await self._decide(ctx, suggestion_id, "denied", reason)

    @suggestion.command(name="consider")
    @app_commands.describe(suggestion_id="The suggestion number", reason="Optional reason")
    async def sg_consider(self, ctx: commands.Context, suggestion_id: int, *, reason: Optional[str] = None) -> None:
        """Mark a suggestion as under consideration (with optional reason)."""
        await self._decide(ctx, suggestion_id, "considered", reason)

    @suggestion.command(name="list")
    async def sg_list(self, ctx: commands.Context) -> None:
        """List open (and pending) suggestions (paginated)."""
        lang = await self._lang(ctx.guild)
        suggestions = [
            s for s in await self.config.guild(ctx.guild).suggestions()
            if s.get("status") in ("open", "pending")
        ]
        if not suggestions:
            await ctx.send(tr_lang(lang, "Keine offenen Vorschläge.", "No open suggestions."))
            return
        lines = []
        for s in suggestions:
            author_id = s.get("author")
            member = ctx.guild.get_member(author_id) if author_id else None
            if author_id == DELETED_USER:
                name = tr_lang(lang, "Gelöschter Nutzer", "Deleted user")
            else:
                name = member.display_name if member else f"<@{author_id}>"
            lines.append(f"**#{s.get('id')}** [{status_label(s.get('status', ''), lang)}] {name}: {str(s.get('text', ''))[:120]}")
        per_page = 10
        pages: List[discord.Embed] = []
        colour = await ctx.embed_colour()
        title = tr_lang(lang, "Offene Vorschläge", "Open suggestions")
        for i in range(0, len(lines), per_page):
            e = discord.Embed(title=title, description="\n".join(lines[i:i + per_page])[:4000], colour=colour)
            e.set_footer(text=tr_lang(
                lang,
                f"Seite {i // per_page + 1}/{(len(lines) - 1) // per_page + 1}",
                f"Page {i // per_page + 1}/{(len(lines) - 1) // per_page + 1}",
            ))
            pages.append(e)
        await self._send_pages(ctx, pages)

    # ------------------------------------------------------------------ #
    # Configuration (admin)
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="suggestset", aliases=["sgset"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def suggestset(self, ctx: commands.Context) -> None:
        """Configure the suggestion module."""

    @suggestset.command(name="enable")
    @app_commands.describe(on_off="Enable or disable suggestions")
    async def sgs_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable suggestions for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = tr_lang(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(tr_lang(lang, f"Vorschläge **{state}**.", f"Suggestions **{state}**."))

    @suggestset.command(name="channel")
    @app_commands.describe(channel="Channel where suggestions are posted")
    async def sgs_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the public suggestion channel."""
        lang = await self._lang(ctx.guild)
        if not channel.permissions_for(ctx.guild.me).send_messages:
            await ctx.send(tr_lang(lang, "Keine Senderechte in dem Kanal.", "I can't send messages in that channel."))
            return
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(tr_lang(lang, f"Vorschlags-Kanal: {channel.mention}", f"Suggestion channel: {channel.mention}"))

    @suggestset.command(name="reviewchannel")
    @app_commands.describe(channel="Mod-review queue channel (leave empty to clear)")
    async def sgs_reviewchannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Set (or clear) the mod-review queue channel."""
        lang = await self._lang(ctx.guild)
        if channel is None:
            await self.config.guild(ctx.guild).review_channel.clear()
            await ctx.send(tr_lang(lang, "Prüfkanal entfernt.", "Review channel cleared."))
            return
        await self.config.guild(ctx.guild).review_channel.set(channel.id)
        await ctx.send(tr_lang(lang, f"Prüfkanal: {channel.mention}", f"Review channel: {channel.mention}"))

    @suggestset.command(name="reviewmode")
    @app_commands.describe(on_off="Route new suggestions through the review queue first")
    async def sgs_reviewmode(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle the mod-review mode."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).review_mode.set(on_off)
        await ctx.send(tr_lang(
            lang,
            "Neue Vorschläge landen zuerst in der Prüfwarteschlange." if on_off else "Neue Vorschläge werden direkt gepostet.",
            "New suggestions go to the review queue first." if on_off else "New suggestions are posted directly.",
        ))

    @suggestset.command(name="emojis")
    @app_commands.describe(upvote="Upvote emoji", downvote="Downvote emoji")
    async def sgs_emojis(self, ctx: commands.Context, upvote: str, downvote: str) -> None:
        """Set the upvote/downvote emojis."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).upvote.set(upvote.strip())
        await self.config.guild(ctx.guild).downvote.set(downvote.strip())
        await ctx.send(tr_lang(lang, f"Abstimmung: {upvote} / {downvote}", f"Voting: {upvote} / {downvote}"))

    @suggestset.command(name="threads")
    @app_commands.describe(on_off="Create a discussion thread per suggestion")
    async def sgs_threads(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle discussion threads for suggestions."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).threads.set(on_off)
        await ctx.send(tr_lang(
            lang,
            "Diskussions-Threads aktiviert." if on_off else "Diskussions-Threads deaktiviert.",
            "Discussion threads enabled." if on_off else "Discussion threads disabled.",
        ))

    @suggestset.command(name="dmnotify")
    @app_commands.describe(on_off="DM the suggester when a decision is made")
    async def sgs_dmnotify(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle DM notifications on decisions."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).dm_notify.set(on_off)
        await ctx.send(tr_lang(
            lang,
            "Einreicher werden per DM benachrichtigt." if on_off else "Keine DM-Benachrichtigung mehr.",
            "Suggesters will be notified via DM." if on_off else "DM notifications disabled.",
        ))

    @suggestset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def sgs_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(tr_lang(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("suggestions", L("Vorschläge", "Suggestions"), mount="guild_settings", permission="guild_admin", order=57)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        suggestions = await conf.suggestions()
        open_count = sum(1 for s in suggestions if s.get("status") in ("open", "pending"))
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Vorschläge per `/suggest`. Aktuell {open_count} offen von {len(suggestions)} insgesamt.",
                f"Suggestions via `/suggest`. Currently {open_count} open out of {len(suggestions)} total.",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.channel("channel", L("Vorschlags-Kanal", "Suggestion channel"), value=str(await conf.channel() or "")),
                Field.switch("review_mode", L("Prüfmodus (Mod-Warteschlange)", "Review mode (mod queue)"), value=bool(await conf.review_mode())),
                Field.channel("review_channel", L("Prüfkanal", "Review channel"), value=str(await conf.review_channel() or "")),
                Field.text("upvote", L("Upvote-Emoji", "Upvote emoji"), value=str(await conf.upvote() or "👍")),
                Field.text("downvote", L("Downvote-Emoji", "Downvote emoji"), value=str(await conf.downvote() or "👎")),
                Field.switch("threads", L("Diskussions-Thread pro Vorschlag", "Discussion thread per suggestion"), value=bool(await conf.threads())),
                Field.switch("dm_notify", L("DM bei Entscheidung", "DM on decision"), value=bool(await conf.dm_notify())),
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
        await conf.enabled.set(bool(data.get("enabled")))
        channel = str(data.get("channel") or "").strip()
        await (conf.channel.set(int(channel)) if channel.isdigit() else conf.channel.clear())
        review_channel = str(data.get("review_channel") or "").strip()
        await (conf.review_channel.set(int(review_channel)) if review_channel.isdigit() else conf.review_channel.clear())
        await conf.review_mode.set(bool(data.get("review_mode")))
        await conf.upvote.set(str(data.get("upvote") or "👍").strip() or "👍")
        await conf.downvote.set(str(data.get("downvote") or "👎").strip() or "👎")
        await conf.threads.set(bool(data.get("threads")))
        await conf.dm_notify.set(bool(data.get("dm_notify")))
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard page: suggestion list (guild scope)
    # ------------------------------------------------------------------ #
    @dashboard_page(
        "list",
        L("Vorschlagsliste", "Suggestion list"),
        scope="guild",
        permission="guild_mod",
        icon="lightbulb",
    )
    async def list_page(self, ctx):
        conf = await self.config.guild(ctx.guild).all()
        lang = str(conf.get("language") or "en-US")
        suggestions = sorted(conf.get("suggestions") or [], key=lambda s: -int(s.get("id", 0)))
        rows = []
        for s in suggestions[:200]:
            author_id = s.get("author")
            member = ctx.guild.get_member(author_id) if author_id else None
            if author_id == DELETED_USER:
                author = "—"
            else:
                author = member.display_name if member else str(author_id)
            rows.append({
                "id": f"#{s.get('id')}",
                "author": author[:60],
                "text": str(s.get("text", ""))[:80],
                "status": status_label(s.get("status", ""), lang),
                "reason": str(s.get("reason", ""))[:80],
            })
        open_count = sum(1 for s in suggestions if s.get("status") in ("open", "pending"))
        comps = [
            Component.heading(L("Vorschläge", "Suggestions")),
            Component.text(L(
                f"{open_count} offen · {len(suggestions)} insgesamt",
                f"{open_count} open · {len(suggestions)} total",
            )),
        ]
        if rows:
            comps.append(Component.table(
                columns=[
                    {"key": "id", "label": "ID"},
                    {"key": "author", "label": L("Autor", "Author")},
                    {"key": "text", "label": L("Text", "Text")},
                    {"key": "status", "label": L("Status", "Status")},
                    {"key": "reason", "label": L("Grund", "Reason")},
                ],
                rows=rows,
            ))
        else:
            comps.append(Component.text(L("Noch keine Vorschläge.", "No suggestions yet.")))
        return PageSchema(components=comps)
