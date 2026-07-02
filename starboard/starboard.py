"""Starboard — mirror community favourites into a star channel.

When a message collects enough star reactions it is re-posted as an embed
(author, content, image, star count, jump link) in a configurable star
channel. The count updates live, posts are removed again when they drop
below the threshold (configurable), NSFW content is never mirrored to a
non-NSFW starboard and bot messages can be ignored. Bilingual output
(DE/EN, default en-US). Web dashboard integration (settings panel +
top-starred page) via the resilient drop-in.
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

log = logging.getLogger("red.pdc.starboard")  # module logger

DELETED_USER = 0xDE1  # sentinel for anonymized author IDs


class Starboard(commands.Cog):
    """Pin community favourites: enough star reactions mirror a message."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x57A2B0A2D, force_registration=True)
        self.config.register_guild(
            enabled=True,
            language="en-US",
            channel=None,  # star channel ID
            emoji="⭐",  # reaction emoji that counts
            threshold=3,  # stars needed for a starboard post
            ignore_channels=[],  # channel IDs never mirrored
            selfstar=False,  # whether the author's own star counts
            jump_link=True,  # include a jump link in the embed
            remove_below=True,  # delete the star post when below threshold
            ignore_bots=True,  # ignore messages authored by bots
            # message ID (str) -> {star_message, channel, author, count}
            entries={},
        )

    async def cog_load(self) -> None:
        register_dashboard(self)

    def cog_unload(self) -> None:
        unregister_dashboard(self)

    # ------------------------------------------------------------------ #
    # Red data APIs
    # ------------------------------------------------------------------ #
    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Anonymize the stored author IDs of the requesting user."""
        for guild_id in await self.config.all_guilds():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            async with self.config.guild(guild).entries() as entries:
                for entry in entries.values():
                    if entry.get("author") == user_id:
                        entry["author"] = DELETED_USER

    async def red_get_data_for_user(self, *, user_id: int) -> dict:
        """Return which starboard entries reference the user as author."""
        lines: List[str] = []
        for guild_id, data in (await self.config.all_guilds()).items():
            for mid, entry in (data.get("entries") or {}).items():
                if entry.get("author") == user_id:
                    lines.append(f"guild {guild_id}: message {mid} with {entry.get('count', 0)} star(s)")
        if not lines:
            return {}
        return {"starboard.txt": "\n".join(lines).encode("utf-8")}

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

    @staticmethod
    def _emoji_matches(payload_emoji: discord.PartialEmoji, configured: str) -> bool:
        """Compare a raw reaction emoji with the configured emoji string."""
        if payload_emoji.is_unicode_emoji():
            return str(payload_emoji) == configured
        # Custom emoji: match "<a:name:id>" / "name:id" / bare ID forms.
        return (
            str(payload_emoji) == configured
            or f"{payload_emoji.name}:{payload_emoji.id}" in configured
            or (payload_emoji.id is not None and str(payload_emoji.id) in configured)
        )

    async def _count_stars(self, message: discord.Message, configured: str, selfstar: bool) -> int:
        """Count valid star reactions on a message (bots never count)."""
        for reaction in message.reactions:
            if str(reaction.emoji) != configured and not (
                isinstance(reaction.emoji, (discord.Emoji, discord.PartialEmoji))
                and (f"{reaction.emoji.name}:{reaction.emoji.id}" in configured
                     or str(getattr(reaction.emoji, "id", "")) in configured)
            ):
                continue
            count = 0
            try:
                async for user in reaction.users():
                    if user.bot:
                        continue
                    if not selfstar and user.id == message.author.id:
                        continue
                    count += 1
            except discord.HTTPException:
                # Fall back to the raw count when member fetching fails.
                count = reaction.count
            return count
        return 0

    def _star_embed(self, message: discord.Message, count: int, lang: str, jump: bool) -> discord.Embed:
        """Build the starboard embed for a message."""
        e = discord.Embed(
            description=message.content[:4000] or None,
            colour=discord.Colour.gold(),
            timestamp=message.created_at,
        )
        e.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
        )
        # Mirror the first image attachment or an embedded image, if any.
        image_url = None
        for att in message.attachments:
            if (att.content_type or "").startswith("image/") and not att.is_spoiler():
                image_url = att.url
                break
        if image_url is None:
            for emb in message.embeds:
                if emb.image and emb.image.url:
                    image_url = emb.image.url
                    break
                if emb.thumbnail and emb.thumbnail.url:
                    image_url = emb.thumbnail.url
                    break
        if image_url:
            e.set_image(url=image_url)
        if jump:
            e.add_field(
                name=tr_lang(lang, "Original", "Original"),
                value=tr_lang(lang, f"[Zur Nachricht]({message.jump_url})", f"[Jump to message]({message.jump_url})"),
                inline=False,
            )
        e.set_footer(text=f"#{getattr(message.channel, 'name', '?')}")
        return e

    # ------------------------------------------------------------------ #
    # Reaction listeners
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        conf = await self.config.guild(guild).all()
        if not conf.get("enabled", True):
            return
        star_channel = guild.get_channel(conf.get("channel") or 0)
        if star_channel is None:
            return
        if not self._emoji_matches(payload.emoji, str(conf.get("emoji") or "⭐")):
            return
        if payload.channel_id == star_channel.id:
            return  # never star the starboard itself
        if payload.channel_id in (conf.get("ignore_channels") or []):
            return
        source = guild.get_channel_or_thread(payload.channel_id)
        if source is None:
            return
        try:
            message = await source.fetch_message(payload.message_id)
        except discord.HTTPException:
            return  # deleted message or missing permissions
        if message.author.bot and conf.get("ignore_bots", True):
            return
        # Do not mirror NSFW channel content into a non-NSFW starboard.
        if getattr(source, "is_nsfw", lambda: False)() and not getattr(star_channel, "is_nsfw", lambda: False)():
            return
        count = await self._count_stars(message, str(conf.get("emoji") or "⭐"), bool(conf.get("selfstar")))
        await self._sync_star_post(guild, message, star_channel, count, conf)

    async def _sync_star_post(
        self,
        guild: discord.Guild,
        message: discord.Message,
        star_channel: discord.abc.Messageable,
        count: int,
        conf: dict,
    ) -> None:
        """Create, update or remove the starboard post for ``message``."""
        lang = str(conf.get("language") or "en-US")
        threshold = max(1, int(conf.get("threshold") or 3))
        emoji = str(conf.get("emoji") or "⭐")
        key = str(message.id)
        async with self.config.guild(guild).entries() as entries:
            entry = entries.get(key)
            if count >= threshold:
                content = f"{emoji} **{count}** · <#{message.channel.id}>"
                embed = self._star_embed(message, count, lang, bool(conf.get("jump_link", True)))
                if entry is None:
                    try:
                        star_msg = await star_channel.send(content, embed=embed)
                    except discord.HTTPException:
                        return
                    entries[key] = {
                        "star_message": star_msg.id,
                        "channel": message.channel.id,
                        "author": message.author.id,
                        "count": count,
                    }
                else:
                    entry["count"] = count
                    try:
                        star_msg = await star_channel.fetch_message(entry["star_message"])
                        await star_msg.edit(content=content, embed=embed)
                    except discord.HTTPException:
                        pass  # star post was deleted manually
            elif entry is not None:
                entry["count"] = count
                if conf.get("remove_below", True):
                    try:
                        star_msg = await star_channel.fetch_message(entry["star_message"])
                        await star_msg.delete()
                    except discord.HTTPException:
                        pass
                    entries.pop(key, None)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Remove the starboard post when the original message is deleted."""
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        conf = await self.config.guild(guild).all()
        key = str(payload.message_id)
        if key not in (conf.get("entries") or {}):
            return
        star_channel = guild.get_channel(conf.get("channel") or 0)
        async with self.config.guild(guild).entries() as entries:
            entry = entries.pop(key, None)
        if entry and star_channel is not None:
            try:
                star_msg = await star_channel.fetch_message(entry["star_message"])
                await star_msg.delete()
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ #
    # User command: top starred
    # ------------------------------------------------------------------ #
    @commands.hybrid_command(name="starboardtop", aliases=["startop"])
    @commands.guild_only()
    async def starboard_top(self, ctx: commands.Context) -> None:
        """Show the top starred messages of this server (paginated)."""
        lang = await self._lang(ctx.guild)
        conf = await self.config.guild(ctx.guild).all()
        emoji = str(conf.get("emoji") or "⭐")
        entries = sorted(
            (conf.get("entries") or {}).items(),
            key=lambda kv: -int(kv[1].get("count", 0)),
        )
        if not entries:
            await ctx.send(tr_lang(lang, "Noch keine Sterne-Nachrichten.", "No starred messages yet."))
            return
        lines = []
        for rank, (mid, entry) in enumerate(entries[:100], start=1):
            author_id = entry.get("author")
            member = ctx.guild.get_member(author_id) if author_id else None
            if author_id == DELETED_USER:
                name = tr_lang(lang, "Gelöschter Nutzer", "Deleted user")
            else:
                name = member.display_name if member else f"<@{author_id}>"
            link = f"https://discord.com/channels/{ctx.guild.id}/{entry.get('channel')}/{mid}"
            lines.append(f"**{rank}.** {emoji} {entry.get('count', 0)} · {name} · [{tr_lang(lang, 'Link', 'link')}]({link})")
        per_page = 10
        pages: List[discord.Embed] = []
        colour = await ctx.embed_colour()
        title = tr_lang(lang, "Top Sterne-Nachrichten", "Top starred messages")
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
    @commands.hybrid_group(name="starboardset", aliases=["sbset"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def starboardset(self, ctx: commands.Context) -> None:
        """Configure the starboard module."""

    @starboardset.command(name="enable")
    @app_commands.describe(on_off="Enable or disable the starboard")
    async def sbs_enable(self, ctx: commands.Context, on_off: bool) -> None:
        """Enable/disable the starboard for this server."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).enabled.set(on_off)
        state = tr_lang(lang, "aktiviert" if on_off else "deaktiviert", "enabled" if on_off else "disabled")
        await ctx.send(tr_lang(lang, f"Starboard **{state}**.", f"Starboard **{state}**."))

    @starboardset.command(name="channel")
    @app_commands.describe(channel="Channel where starred messages are posted")
    async def sbs_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the star channel."""
        lang = await self._lang(ctx.guild)
        if not channel.permissions_for(ctx.guild.me).send_messages:
            await ctx.send(tr_lang(lang, "Keine Senderechte in dem Kanal.", "I can't send messages in that channel."))
            return
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(tr_lang(lang, f"Star-Kanal: {channel.mention}", f"Star channel: {channel.mention}"))

    @starboardset.command(name="emoji")
    @app_commands.describe(emoji="The reaction emoji that counts as a star")
    async def sbs_emoji(self, ctx: commands.Context, emoji: str) -> None:
        """Set the star emoji (default: ⭐)."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).emoji.set(emoji.strip())
        await ctx.send(tr_lang(lang, f"Stern-Emoji: {emoji}", f"Star emoji: {emoji}"))

    @starboardset.command(name="threshold")
    @app_commands.describe(count="Stars needed before a message is posted (1-100)")
    async def sbs_threshold(self, ctx: commands.Context, count: int) -> None:
        """Set how many stars are needed for the starboard."""
        lang = await self._lang(ctx.guild)
        if not 1 <= count <= 100:
            await ctx.send(tr_lang(lang, "Wert muss 1–100 sein.", "Value must be 1–100."))
            return
        await self.config.guild(ctx.guild).threshold.set(count)
        await ctx.send(tr_lang(lang, f"Schwelle: **{count}** Sterne.", f"Threshold: **{count}** stars."))

    @starboardset.command(name="ignore")
    @app_commands.describe(channel="Channel to toggle on the ignore list")
    async def sbs_ignore(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Toggle a channel on the ignore list."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).ignore_channels() as ignored:
            if channel.id in ignored:
                ignored.remove(channel.id)
                added = False
            else:
                ignored.append(channel.id)
                added = True
        await ctx.send(tr_lang(
            lang,
            f"{channel.mention} wird {'ignoriert' if added else 'nicht mehr ignoriert'}.",
            f"{channel.mention} is {'now ignored' if added else 'no longer ignored'}.",
        ))

    @starboardset.command(name="selfstar")
    @app_commands.describe(on_off="Whether the author's own star counts")
    async def sbs_selfstar(self, ctx: commands.Context, on_off: bool) -> None:
        """Allow/disallow self-starring."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).selfstar.set(on_off)
        await ctx.send(tr_lang(
            lang,
            "Eigene Sterne zählen jetzt." if on_off else "Eigene Sterne zählen nicht mehr.",
            "Self-stars now count." if on_off else "Self-stars no longer count.",
        ))

    @starboardset.command(name="jumplink")
    @app_commands.describe(on_off="Include a jump link in the starboard embed")
    async def sbs_jumplink(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle the jump link in the starboard embed."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).jump_link.set(on_off)
        await ctx.send(tr_lang(
            lang,
            "Sprunglink wird angezeigt." if on_off else "Sprunglink wird ausgeblendet.",
            "Jump link enabled." if on_off else "Jump link disabled.",
        ))

    @starboardset.command(name="removebelow")
    @app_commands.describe(on_off="Remove the star post when it drops below the threshold")
    async def sbs_removebelow(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle removal of posts that fall below the threshold."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).remove_below.set(on_off)
        await ctx.send(tr_lang(
            lang,
            "Beiträge unter der Schwelle werden entfernt." if on_off else "Beiträge bleiben trotz Unterschreitung erhalten.",
            "Posts below the threshold are removed." if on_off else "Posts are kept even below the threshold.",
        ))

    @starboardset.command(name="ignorebots")
    @app_commands.describe(on_off="Ignore messages authored by bots")
    async def sbs_ignorebots(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle whether bot messages can be starred."""
        lang = await self._lang(ctx.guild)
        await self.config.guild(ctx.guild).ignore_bots.set(on_off)
        await ctx.send(tr_lang(
            lang,
            "Bot-Nachrichten werden ignoriert." if on_off else "Bot-Nachrichten können gestarrt werden.",
            "Bot messages are ignored." if on_off else "Bot messages can be starred.",
        ))

    @starboardset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def sbs_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(tr_lang(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard panel
    # ------------------------------------------------------------------ #
    @dashboard_panel("starboard", L("Starboard", "Starboard"), mount="guild_settings", permission="guild_admin", order=56)
    async def settings_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        entries = await conf.entries()
        return PanelSchema(
            description=tr_lang(
                lang,
                f"Nachrichten mit genug Sternen landen im Star-Kanal. Aktuell {len(entries)} Einträge.",
                f"Messages with enough stars are mirrored to the star channel. Currently {len(entries)} entries.",
            ),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(await conf.enabled())),
                Field.channel("channel", L("Star-Kanal", "Star channel"), value=str(await conf.channel() or "")),
                Field.text("emoji", L("Stern-Emoji", "Star emoji"), value=str(await conf.emoji() or "⭐")),
                Field.number("threshold", L("Schwelle (Sterne)", "Threshold (stars)"), value=int(await conf.threshold() or 3), min=1, max=100),
                Field.switch("selfstar", L("Eigene Sterne zählen", "Self-stars count"), value=bool(await conf.selfstar())),
                Field.switch("jump_link", L("Sprunglink im Embed", "Jump link in embed"), value=bool(await conf.jump_link())),
                Field.switch("remove_below", L("Unter Schwelle entfernen", "Remove below threshold"), value=bool(await conf.remove_below())),
                Field.switch("ignore_bots", L("Bot-Nachrichten ignorieren", "Ignore bot messages"), value=bool(await conf.ignore_bots())),
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
        try:
            threshold = int(data.get("threshold", 3))
        except (TypeError, ValueError):
            threshold = 0
        if not 1 <= threshold <= 100:
            return SubmitResult.fail(
                tr_lang(lang, "Bitte Eingaben prüfen.", "Please check your input."),
                {"threshold": tr_lang(lang, "Wert muss 1–100 sein.", "Value must be 1–100.")},
            )
        emoji = str(data.get("emoji") or "⭐").strip() or "⭐"
        await conf.enabled.set(bool(data.get("enabled")))
        channel = str(data.get("channel") or "").strip()
        await (conf.channel.set(int(channel)) if channel.isdigit() else conf.channel.clear())
        await conf.emoji.set(emoji)
        await conf.threshold.set(threshold)
        await conf.selfstar.set(bool(data.get("selfstar")))
        await conf.jump_link.set(bool(data.get("jump_link")))
        await conf.remove_below.set(bool(data.get("remove_below")))
        await conf.ignore_bots.set(bool(data.get("ignore_bots")))
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    # ------------------------------------------------------------------ #
    # Dashboard page: top starred (guild scope)
    # ------------------------------------------------------------------ #
    @dashboard_page(
        "top",
        L("Top-Sterne", "Top starred"),
        scope="guild",
        permission="guild_member",
        icon="star",
    )
    async def top_page(self, ctx):
        conf = await self.config.guild(ctx.guild).all()
        emoji = str(conf.get("emoji") or "⭐")
        entries = sorted(
            (conf.get("entries") or {}).items(),
            key=lambda kv: -int(kv[1].get("count", 0)),
        )
        rows = []
        for rank, (mid, entry) in enumerate(entries[:200], start=1):
            author_id = entry.get("author")
            member = ctx.guild.get_member(author_id) if author_id else None
            if author_id == DELETED_USER:
                author = "—"
            else:
                author = member.display_name if member else str(author_id)
            ch = ctx.guild.get_channel(entry.get("channel") or 0)
            rows.append({
                "rank": str(rank),
                "stars": f"{emoji} {entry.get('count', 0)}",
                "author": author[:60],
                "channel": f"#{ch.name}" if ch else "?",
                "message": str(mid),
            })
        comps = [
            Component.heading(L("Top-Sterne-Nachrichten", "Top starred messages")),
            Component.text(L(
                f"{len(rows)} Einträge auf dem Starboard.",
                f"{len(rows)} entries on the starboard.",
            )),
        ]
        if rows:
            comps.append(Component.table(
                columns=[
                    {"key": "rank", "label": "#"},
                    {"key": "stars", "label": L("Sterne", "Stars")},
                    {"key": "author", "label": L("Autor", "Author")},
                    {"key": "channel", "label": L("Kanal", "Channel")},
                    {"key": "message", "label": L("Nachricht", "Message")},
                ],
                rows=rows,
            ))
        else:
            comps.append(Component.text(L("Noch keine Einträge.", "No entries yet.")))
        return PageSchema(components=comps)
