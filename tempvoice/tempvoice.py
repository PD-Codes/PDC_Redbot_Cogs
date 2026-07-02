"""TempVoice — join-to-create temporary voice channels (VoiceMaster style).

Admins designate one or more "creator" voice channels. When a member joins a
creator channel, a personal temporary voice channel is created, the member is
moved into it and becomes its owner. Owners manage their channel via slash
commands or an interactive button panel posted into the voice channel's text
chat (rename, limit, lock, hide, transfer, claim, kick). Temp channels are
deleted automatically once empty (with a small grace delay). Temp channel IDs
are persisted so orphans are cleaned up after a restart. Bilingual output
(DE/EN, default en-US). Web dashboard integration via the resilient drop-in.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red

from .pdc_dashboard import (
    Field,
    L,
    PanelSchema,
    SubmitResult,
    WidgetData,
    dashboard_panel,
    dashboard_widget,
    register_dashboard,
    tr,
    tr_lang,
    unregister_dashboard,
)

log = logging.getLogger("red.pdc.tempvoice")  # module logger

# Grace delay (seconds) before an empty temp channel is deleted.
EMPTY_GRACE_SECONDS = 15
# Discord allows roughly 2 channel renames per 10 minutes.
RENAME_MAX = 2
RENAME_WINDOW = 600

# Fixed custom_ids so the control panel view survives bot restarts.
_BTN_RENAME = "pdc_tempvoice_rename"
_BTN_LIMIT = "pdc_tempvoice_limit"
_BTN_LOCK = "pdc_tempvoice_lock"
_BTN_HIDE = "pdc_tempvoice_hide"
_BTN_CLAIM = "pdc_tempvoice_claim"
_SEL_TRANSFER = "pdc_tempvoice_transfer"
_SEL_KICK = "pdc_tempvoice_kick"

DEFAULT_GUILD = {
    "language": "en-US",  # per-guild output language (de-DE | en-US)
    "creators": {
        # "<channel_id>": {"name_template": "...", "category_id": int|None,
        #                  "user_limit": int, "bitrate": int (kbps, 0 = default)}
    },
    "temp_channels": {
        # "<channel_id>": {"owner": int|None, "creator": int, "panel_message": int|None}
        # Persisted so orphaned channels can be cleaned up after a restart.
    },
}

DEFAULT_CREATOR = {
    "name_template": "{user}'s channel",
    "category_id": None,
    "user_limit": 0,
    "bitrate": 0,
}


def _render_name(template: str, member: discord.Member, count: int) -> str:
    """Render a channel name template ({user}, {count}) to max 100 chars."""
    name = (
        (template or DEFAULT_CREATOR["name_template"])
        .replace("{user}", member.display_name)
        .replace("{count}", str(count))
    )
    return name.strip()[:100] or member.display_name[:100]


class _TextModal(discord.ui.Modal):
    """Generic one-field modal used for rename / limit input."""

    def __init__(self, title: str, label: str, default: str = "", max_length: int = 100) -> None:
        super().__init__(title=title[:45])
        self.value: Optional[str] = None
        self.field = discord.ui.TextInput(
            label=label[:45], default=default[:max_length], required=True, max_length=max_length
        )
        self.add_item(self.field)
        self.interaction: Optional[discord.Interaction] = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.value = str(self.field.value or "").strip()
        self.interaction = interaction


class TempVoicePanelView(discord.ui.View):
    """Persistent control panel; identifies the temp channel per interaction."""

    def __init__(self, cog: "TempVoice") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="✏️ Rename", style=discord.ButtonStyle.secondary, custom_id=_BTN_RENAME, row=0)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_rename(interaction)

    @discord.ui.button(label="👥 Limit", style=discord.ButtonStyle.secondary, custom_id=_BTN_LIMIT, row=0)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_limit(interaction)

    @discord.ui.button(label="🔒 Lock/Unlock", style=discord.ButtonStyle.secondary, custom_id=_BTN_LOCK, row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_lock_toggle(interaction)

    @discord.ui.button(label="👻 Hide/Unhide", style=discord.ButtonStyle.secondary, custom_id=_BTN_HIDE, row=0)
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_hide_toggle(interaction)

    @discord.ui.button(label="🙋 Claim", style=discord.ButtonStyle.primary, custom_id=_BTN_CLAIM, row=0)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_claim(interaction)

    @discord.ui.select(
        cls=discord.ui.UserSelect, custom_id=_SEL_TRANSFER, row=1,
        placeholder="👑 Transfer ownership to…", min_values=1, max_values=1,
    )
    async def transfer(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        await self.cog.panel_transfer(interaction, select.values[0] if select.values else None)

    @discord.ui.select(
        cls=discord.ui.UserSelect, custom_id=_SEL_KICK, row=2,
        placeholder="👢 Kick user…", min_values=1, max_values=1,
    )
    async def kick(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        await self.cog.panel_kick(interaction, select.values[0] if select.values else None)


class TempVoice(commands.Cog):
    """Join-to-create temporary voice channels with owner controls."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x7E39_C01C_E5, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self._view: Optional[TempVoicePanelView] = None
        self._delete_tasks: Dict[int, asyncio.Task] = {}  # channel_id -> pending deletion
        self._rename_log: Dict[int, List[float]] = {}  # channel_id -> rename timestamps
        self._create_locks: Dict[int, asyncio.Lock] = {}  # guild_id -> creation lock
        self._selected_creator: Dict[Tuple[int, int], str] = {}  # dashboard panel selection

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def cog_load(self) -> None:
        register_dashboard(self)
        self._view = TempVoicePanelView(self)
        self.bot.add_view(self._view)  # persistent: panel buttons survive restarts
        # Restart-safe cleanup runs in the background once the bot is ready.
        self._delete_tasks[0] = asyncio.create_task(self._startup_cleanup())

    async def cog_unload(self) -> None:
        unregister_dashboard(self)
        for task in list(self._delete_tasks.values()):
            task.cancel()
        self._delete_tasks.clear()
        if self._view:
            self._view.stop()

    async def _startup_cleanup(self) -> None:
        """Remove orphaned temp channels recorded before a restart."""
        try:
            await self.bot.wait_until_red_ready()
            for guild in self.bot.guilds:
                temp = await self.config.guild(guild).temp_channels()
                if not isinstance(temp, dict) or not temp:
                    continue
                changed = False
                for cid in list(temp.keys()):
                    channel = guild.get_channel(int(cid)) if str(cid).isdigit() else None
                    if channel is None:
                        # Channel was deleted while the bot was offline.
                        temp.pop(cid, None)
                        changed = True
                        continue
                    members = [m for m in channel.members if not m.bot]
                    if not members:
                        try:
                            await channel.delete(reason="TempVoice: orphaned temp channel cleanup")
                        except discord.HTTPException:
                            pass
                        temp.pop(cid, None)
                        changed = True
                    else:
                        # Occupied channel survives the restart; watch it again.
                        self._watch_empty(channel)
                if changed:
                    await self.config.guild(guild).temp_channels.set(temp)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("TempVoice startup cleanup failed")
        finally:
            self._delete_tasks.pop(0, None)

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Ownership data is transient; clear any stored ownership for the user."""
        for guild in self.bot.guilds:
            async with self.config.guild(guild).temp_channels() as temp:
                for entry in temp.values():
                    if isinstance(entry, dict) and entry.get("owner") == user_id:
                        entry["owner"] = None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _lang(self, guild: Optional[discord.Guild]) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    async def _temp_entry(self, guild: discord.Guild, channel_id: int) -> Optional[dict]:
        temp = await self.config.guild(guild).temp_channels()
        entry = (temp or {}).get(str(channel_id))
        return entry if isinstance(entry, dict) else None

    def _rename_allowed(self, channel_id: int) -> bool:
        """Local guard for Discord's ~2 renames / 10 min channel rate limit."""
        now = time.time()
        stamps = [t for t in self._rename_log.get(channel_id, []) if now - t < RENAME_WINDOW]
        self._rename_log[channel_id] = stamps
        return len(stamps) < RENAME_MAX

    def _note_rename(self, channel_id: int) -> None:
        self._rename_log.setdefault(channel_id, []).append(time.time())

    async def _bot_can_manage(self, channel: discord.VoiceChannel) -> bool:
        me = channel.guild.me
        perms = channel.permissions_for(me)
        return bool(perms.manage_channels and perms.manage_roles)

    # ------------------------------------------------------------------ #
    # Channel creation / deletion
    # ------------------------------------------------------------------ #
    async def _create_temp_channel(self, member: discord.Member, creator: discord.VoiceChannel) -> None:
        guild = member.guild
        lang = await self._lang(guild)
        gconf = await self.config.guild(guild).all()
        settings = dict(DEFAULT_CREATOR)
        settings.update(gconf.get("creators", {}).get(str(creator.id), {}) or {})

        lock = self._create_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            temp = gconf.get("temp_channels", {}) or {}
            count = sum(1 for e in temp.values() if isinstance(e, dict) and e.get("creator") == creator.id) + 1
            name = _render_name(str(settings.get("name_template") or ""), member, count)

            category = None
            cat_id = settings.get("category_id")
            if cat_id:
                category = guild.get_channel(int(cat_id))
                if not isinstance(category, discord.CategoryChannel):
                    category = None
            if category is None:
                category = creator.category

            overwrites = dict(creator.overwrites)
            # The owner may manage their own channel (name, limit, lock, moves).
            overwrites[member] = discord.PermissionOverwrite(
                connect=True, view_channel=True, manage_channels=True,
                move_members=True, mute_members=True,
            )
            overwrites[guild.me] = discord.PermissionOverwrite(
                connect=True, view_channel=True, manage_channels=True,
                move_members=True, manage_roles=True, send_messages=True,
            )

            kwargs: dict = {
                "name": name,
                "category": category,
                "overwrites": overwrites,
                "user_limit": max(0, min(99, int(settings.get("user_limit") or 0))),
                "reason": f"TempVoice: created for {member} via #{creator.name}",
            }
            bitrate_kbps = int(settings.get("bitrate") or 0)
            if bitrate_kbps > 0:
                kwargs["bitrate"] = min(bitrate_kbps * 1000, guild.bitrate_limit)

            try:
                channel = await guild.create_voice_channel(**kwargs)
            except discord.Forbidden:
                log.warning("TempVoice: missing permissions to create a channel in %s", guild.id)
                return
            except discord.HTTPException:
                log.exception("TempVoice: channel creation failed in %s", guild.id)
                return

            entry = {"owner": member.id, "creator": creator.id, "panel_message": None}
            async with self.config.guild(guild).temp_channels() as tmp:
                tmp[str(channel.id)] = entry

        # Move the member into their new channel.
        try:
            await member.move_to(channel, reason="TempVoice: moved to own temp channel")
        except discord.HTTPException:
            # Member left the creator channel meanwhile; delete the empty temp channel.
            await self._delete_temp_channel(channel)
            return

        # Post the interactive control panel into the voice channel's text chat.
        try:
            embed = discord.Embed(
                title=tr_lang(lang, "🎛️ Kanal-Steuerung", "🎛️ Channel controls"),
                description=tr_lang(
                    lang,
                    f"Besitzer: {member.mention}\n"
                    "Nutze die Buttons unten oder `/tempvoice`-Befehle:\n"
                    "Umbenennen, Limit, Sperren, Verstecken, Übertragen, Beanspruchen, Kicken.",
                    f"Owner: {member.mention}\n"
                    "Use the buttons below or the `/tempvoice` commands:\n"
                    "rename, limit, lock, hide, transfer, claim, kick.",
                ),
                colour=discord.Colour.blurple(),
            )
            msg = await channel.send(embed=embed, view=self._view or TempVoicePanelView(self))
            async with self.config.guild(guild).temp_channels() as tmp:
                if str(channel.id) in tmp:
                    tmp[str(channel.id)]["panel_message"] = msg.id
        except discord.HTTPException:
            # No text-in-voice permission: slash commands still work.
            pass

        self._watch_empty(channel)

    def _watch_empty(self, channel: discord.VoiceChannel) -> None:
        """(Re)start the grace-delay deletion watcher for a temp channel."""
        old = self._delete_tasks.pop(channel.id, None)
        if old:
            old.cancel()
        members = [m for m in channel.members if not m.bot]
        if members:
            return
        self._delete_tasks[channel.id] = asyncio.create_task(self._delayed_delete(channel))

    async def _delayed_delete(self, channel: discord.VoiceChannel) -> None:
        try:
            await asyncio.sleep(EMPTY_GRACE_SECONDS)
            fresh = channel.guild.get_channel(channel.id)
            if isinstance(fresh, discord.VoiceChannel):
                if any(not m.bot for m in fresh.members):
                    return  # someone joined during the grace period
                await self._delete_temp_channel(fresh)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("TempVoice delayed delete failed")
        finally:
            self._delete_tasks.pop(channel.id, None)

    async def _delete_temp_channel(self, channel: discord.VoiceChannel) -> None:
        guild = channel.guild
        try:
            await channel.delete(reason="TempVoice: temp channel empty")
        except discord.HTTPException:
            pass
        async with self.config.guild(guild).temp_channels() as temp:
            temp.pop(str(channel.id), None)
        self._rename_log.pop(channel.id, None)

    # ------------------------------------------------------------------ #
    # Voice listener
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot or member.guild is None:
            return
        if before.channel == after.channel:
            return
        gconf = await self.config.guild(member.guild).all()
        creators = gconf.get("creators", {}) or {}
        temp = gconf.get("temp_channels", {}) or {}

        # Join of a creator channel -> spawn a temp channel.
        if after.channel is not None and str(after.channel.id) in creators:
            if isinstance(after.channel, discord.VoiceChannel):
                await self._create_temp_channel(member, after.channel)

        # Leave of a temp channel -> maybe schedule deletion.
        if before.channel is not None and str(before.channel.id) in temp:
            if isinstance(before.channel, discord.VoiceChannel):
                self._watch_empty(before.channel)

        # Join of a temp channel -> cancel a pending deletion.
        if after.channel is not None and str(after.channel.id) in temp:
            task = self._delete_tasks.pop(after.channel.id, None)
            if task:
                task.cancel()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Keep config tidy when a temp channel is deleted manually."""
        if not isinstance(channel, discord.VoiceChannel):
            return
        temp = await self.config.guild(channel.guild).temp_channels()
        if str(channel.id) in (temp or {}):
            async with self.config.guild(channel.guild).temp_channels() as tmp:
                tmp.pop(str(channel.id), None)
            self._rename_log.pop(channel.id, None)
            task = self._delete_tasks.pop(channel.id, None)
            if task:
                task.cancel()

    # ------------------------------------------------------------------ #
    # Owner-action core (shared by panel buttons and slash commands)
    # ------------------------------------------------------------------ #
    async def _resolve_temp_channel(
        self, guild: discord.Guild, member: discord.Member, channel_hint: Optional[discord.abc.GuildChannel]
    ) -> Tuple[Optional[discord.VoiceChannel], Optional[dict]]:
        """Find the temp channel targeted by an action (hint or member's current)."""
        candidates: List[int] = []
        if isinstance(channel_hint, discord.VoiceChannel):
            candidates.append(channel_hint.id)
        if member.voice and member.voice.channel:
            candidates.append(member.voice.channel.id)
        for cid in candidates:
            entry = await self._temp_entry(guild, cid)
            if entry is not None:
                ch = guild.get_channel(cid)
                if isinstance(ch, discord.VoiceChannel):
                    return ch, entry
        return None, None

    def _is_owner_or_admin(self, member: discord.Member, entry: dict) -> bool:
        return member.id == entry.get("owner") or member.guild_permissions.manage_channels

    async def _act_rename(self, channel: discord.VoiceChannel, name: str, lang: str) -> str:
        name = name.strip()[:100]
        if not name:
            return tr_lang(lang, "Bitte einen Namen angeben.", "Please provide a name.")
        if not await self._bot_can_manage(channel):
            return tr_lang(lang, "Mir fehlen Rechte zum Bearbeiten des Kanals.", "I lack permission to edit this channel.")
        if not self._rename_allowed(channel.id):
            # Discord limits channel renames to ~2 per 10 minutes.
            return tr_lang(
                lang,
                "Discord erlaubt nur ~2 Umbenennungen pro 10 Minuten. Bitte warte kurz.",
                "Discord only allows ~2 renames per 10 minutes. Please wait a bit.",
            )
        try:
            await channel.edit(name=name, reason="TempVoice: owner rename")
        except discord.HTTPException:
            return tr_lang(lang, "Umbenennen fehlgeschlagen.", "Rename failed.")
        self._note_rename(channel.id)
        return tr_lang(lang, f"Kanal heißt jetzt **{name}**.", f"Channel is now called **{name}**.")

    async def _act_limit(self, channel: discord.VoiceChannel, limit: int, lang: str) -> str:
        if not 0 <= limit <= 99:
            return tr_lang(lang, "Limit muss 0–99 sein (0 = unbegrenzt).", "Limit must be 0–99 (0 = unlimited).")
        if not await self._bot_can_manage(channel):
            return tr_lang(lang, "Mir fehlen Rechte zum Bearbeiten des Kanals.", "I lack permission to edit this channel.")
        try:
            await channel.edit(user_limit=limit, reason="TempVoice: owner set limit")
        except discord.HTTPException:
            return tr_lang(lang, "Limit setzen fehlgeschlagen.", "Setting the limit failed.")
        if limit == 0:
            return tr_lang(lang, "Nutzerlimit entfernt.", "User limit removed.")
        return tr_lang(lang, f"Nutzerlimit: **{limit}**.", f"User limit: **{limit}**.")

    async def _act_lock(self, channel: discord.VoiceChannel, lock: Optional[bool], lang: str) -> str:
        if not await self._bot_can_manage(channel):
            return tr_lang(lang, "Mir fehlen Rechte zum Bearbeiten des Kanals.", "I lack permission to edit this channel.")
        role = channel.guild.default_role
        ow = channel.overwrites_for(role)
        new_state = (ow.connect is not False) if lock is None else lock
        ow.connect = False if new_state else None
        try:
            await channel.set_permissions(role, overwrite=ow, reason="TempVoice: owner lock/unlock")
        except discord.HTTPException:
            return tr_lang(lang, "Aktion fehlgeschlagen.", "Action failed.")
        if new_state:
            return tr_lang(lang, "🔒 Kanal gesperrt.", "🔒 Channel locked.")
        return tr_lang(lang, "🔓 Kanal entsperrt.", "🔓 Channel unlocked.")

    async def _act_hide(self, channel: discord.VoiceChannel, hide: Optional[bool], lang: str) -> str:
        if not await self._bot_can_manage(channel):
            return tr_lang(lang, "Mir fehlen Rechte zum Bearbeiten des Kanals.", "I lack permission to edit this channel.")
        role = channel.guild.default_role
        ow = channel.overwrites_for(role)
        new_state = (ow.view_channel is not False) if hide is None else hide
        ow.view_channel = False if new_state else None
        try:
            await channel.set_permissions(role, overwrite=ow, reason="TempVoice: owner hide/unhide")
        except discord.HTTPException:
            return tr_lang(lang, "Aktion fehlgeschlagen.", "Action failed.")
        if new_state:
            return tr_lang(lang, "👻 Kanal versteckt.", "👻 Channel hidden.")
        return tr_lang(lang, "👁️ Kanal sichtbar.", "👁️ Channel visible.")

    async def _act_transfer(self, channel: discord.VoiceChannel, target: discord.Member, lang: str) -> str:
        guild = channel.guild
        if target.bot:
            return tr_lang(lang, "Bots können keinen Kanal besitzen.", "Bots cannot own a channel.")
        if target.voice is None or target.voice.channel != channel:
            return tr_lang(lang, "Die Person muss im Kanal sein.", "That user must be in the channel.")
        if not await self._bot_can_manage(channel):
            return tr_lang(lang, "Mir fehlen Rechte zum Bearbeiten des Kanals.", "I lack permission to edit this channel.")
        entry = await self._temp_entry(guild, channel.id)
        old_owner_id = entry.get("owner") if entry else None
        try:
            old_owner = guild.get_member(int(old_owner_id)) if old_owner_id else None
            if old_owner is not None:
                await channel.set_permissions(old_owner, overwrite=None, reason="TempVoice: ownership transfer")
            await channel.set_permissions(
                target,
                overwrite=discord.PermissionOverwrite(
                    connect=True, view_channel=True, manage_channels=True,
                    move_members=True, mute_members=True,
                ),
                reason="TempVoice: ownership transfer",
            )
        except discord.HTTPException:
            return tr_lang(lang, "Übertragung fehlgeschlagen.", "Transfer failed.")
        async with self.config.guild(guild).temp_channels() as temp:
            if str(channel.id) in temp:
                temp[str(channel.id)]["owner"] = target.id
        return tr_lang(lang, f"👑 Neuer Besitzer: {target.mention}", f"👑 New owner: {target.mention}")

    async def _act_kick(self, channel: discord.VoiceChannel, actor: discord.Member, target: discord.Member, lang: str) -> str:
        if target.voice is None or target.voice.channel != channel:
            return tr_lang(lang, "Die Person ist nicht im Kanal.", "That user is not in the channel.")
        if target.id == actor.id:
            return tr_lang(lang, "Du kannst dich nicht selbst kicken.", "You cannot kick yourself.")
        if target.guild_permissions.administrator:
            return tr_lang(lang, "Administratoren können nicht gekickt werden.", "Administrators cannot be kicked.")
        if not channel.permissions_for(channel.guild.me).move_members:
            return tr_lang(lang, "Mir fehlt das Recht **Mitglieder verschieben**.", "I lack the **Move Members** permission.")
        try:
            await target.move_to(None, reason=f"TempVoice: kicked by {actor}")
        except discord.HTTPException:
            return tr_lang(lang, "Kick fehlgeschlagen.", "Kick failed.")
        return tr_lang(lang, f"👢 {target.display_name} wurde gekickt.", f"👢 {target.display_name} was kicked.")

    async def _act_claim(self, channel: discord.VoiceChannel, member: discord.Member, lang: str) -> str:
        guild = channel.guild
        entry = await self._temp_entry(guild, channel.id)
        if entry is None:
            return tr_lang(lang, "Kein temporärer Kanal.", "Not a temporary channel.")
        owner_id = entry.get("owner")
        owner = guild.get_member(int(owner_id)) if owner_id else None
        if owner is not None and owner.voice and owner.voice.channel == channel:
            return tr_lang(
                lang,
                f"Der Besitzer ({owner.display_name}) ist noch im Kanal.",
                f"The owner ({owner.display_name}) is still in the channel.",
            )
        if member.voice is None or member.voice.channel != channel:
            return tr_lang(lang, "Du musst dafür im Kanal sein.", "You must be in the channel to claim it.")
        return await self._act_transfer(channel, member, lang)

    # ------------------------------------------------------------------ #
    # Panel button handlers
    # ------------------------------------------------------------------ #
    async def _panel_context(
        self, interaction: discord.Interaction, *, owner_only: bool = True
    ) -> Tuple[Optional[discord.VoiceChannel], Optional[dict], str]:
        """Resolve channel + entry for a panel interaction and check permissions."""
        guild = interaction.guild
        lang = await self._lang(guild)
        if guild is None or not isinstance(interaction.user, discord.Member):
            return None, None, lang
        channel = interaction.channel if isinstance(interaction.channel, discord.VoiceChannel) else None
        if channel is None:
            await interaction.response.send_message(
                tr_lang(lang, "Nur im Text-Chat eines Temp-Kanals nutzbar.", "Only usable in a temp channel's text chat."),
                ephemeral=True,
            )
            return None, None, lang
        entry = await self._temp_entry(guild, channel.id)
        if entry is None:
            await interaction.response.send_message(
                tr_lang(lang, "Dieser Kanal ist kein Temp-Kanal (mehr).", "This channel is not a temp channel (anymore)."),
                ephemeral=True,
            )
            return None, None, lang
        if owner_only and not self._is_owner_or_admin(interaction.user, entry):
            await interaction.response.send_message(
                tr_lang(lang, "Nur der Kanal-Besitzer darf das.", "Only the channel owner can do that."),
                ephemeral=True,
            )
            return None, None, lang
        return channel, entry, lang

    async def panel_rename(self, interaction: discord.Interaction) -> None:
        channel, entry, lang = await self._panel_context(interaction)
        if channel is None:
            return
        modal = _TextModal(
            tr_lang(lang, "Kanal umbenennen", "Rename channel"),
            tr_lang(lang, "Neuer Name", "New name"),
            default=channel.name,
        )
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value is None or modal.interaction is None:
            return
        msg = await self._act_rename(channel, modal.value, lang)
        await modal.interaction.response.send_message(msg, ephemeral=True)

    async def panel_limit(self, interaction: discord.Interaction) -> None:
        channel, entry, lang = await self._panel_context(interaction)
        if channel is None:
            return
        modal = _TextModal(
            tr_lang(lang, "Nutzerlimit setzen", "Set user limit"),
            tr_lang(lang, "Limit (0-99, 0 = unbegrenzt)", "Limit (0-99, 0 = unlimited)"),
            default=str(channel.user_limit or 0),
            max_length=2,
        )
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value is None or modal.interaction is None:
            return
        if not modal.value.isdigit():
            await modal.interaction.response.send_message(
                tr_lang(lang, "Bitte eine Zahl 0–99 eingeben.", "Please enter a number 0–99."), ephemeral=True
            )
            return
        msg = await self._act_limit(channel, int(modal.value), lang)
        await modal.interaction.response.send_message(msg, ephemeral=True)

    async def panel_lock_toggle(self, interaction: discord.Interaction) -> None:
        channel, entry, lang = await self._panel_context(interaction)
        if channel is None:
            return
        msg = await self._act_lock(channel, None, lang)
        await interaction.response.send_message(msg, ephemeral=True)

    async def panel_hide_toggle(self, interaction: discord.Interaction) -> None:
        channel, entry, lang = await self._panel_context(interaction)
        if channel is None:
            return
        msg = await self._act_hide(channel, None, lang)
        await interaction.response.send_message(msg, ephemeral=True)

    async def panel_claim(self, interaction: discord.Interaction) -> None:
        # Claiming is intentionally open to everyone (checked inside _act_claim).
        channel, entry, lang = await self._panel_context(interaction, owner_only=False)
        if channel is None:
            return
        msg = await self._act_claim(channel, interaction.user, lang)
        await interaction.response.send_message(msg, ephemeral=True)

    async def panel_transfer(self, interaction: discord.Interaction, target) -> None:
        channel, entry, lang = await self._panel_context(interaction)
        if channel is None:
            return
        member = channel.guild.get_member(target.id) if target is not None else None
        if member is None:
            await interaction.response.send_message(
                tr_lang(lang, "Mitglied nicht gefunden.", "Member not found."), ephemeral=True
            )
            return
        msg = await self._act_transfer(channel, member, lang)
        await interaction.response.send_message(msg, ephemeral=True)

    async def panel_kick(self, interaction: discord.Interaction, target) -> None:
        channel, entry, lang = await self._panel_context(interaction)
        if channel is None:
            return
        member = channel.guild.get_member(target.id) if target is not None else None
        if member is None:
            await interaction.response.send_message(
                tr_lang(lang, "Mitglied nicht gefunden.", "Member not found."), ephemeral=True
            )
            return
        msg = await self._act_kick(channel, interaction.user, member, lang)
        await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------------ #
    # Owner slash commands (everyone; ownership is checked at runtime)
    # ------------------------------------------------------------------ #
    async def _cmd_channel(self, ctx: commands.Context) -> Tuple[Optional[discord.VoiceChannel], Optional[dict], str]:
        """Resolve the caller's temp channel for a text/slash command."""
        lang = await self._lang(ctx.guild)
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return None, None, lang
        channel, entry = await self._resolve_temp_channel(ctx.guild, ctx.author, ctx.channel)
        if channel is None or entry is None:
            await ctx.send(
                tr_lang(lang, "Du bist in keinem Temp-Kanal.", "You are not in a temp channel."), ephemeral=True
            )
            return None, None, lang
        return channel, entry, lang

    async def _cmd_owner_channel(self, ctx: commands.Context) -> Tuple[Optional[discord.VoiceChannel], Optional[dict], str]:
        channel, entry, lang = await self._cmd_channel(ctx)
        if channel is None or entry is None:
            return None, None, lang
        if not self._is_owner_or_admin(ctx.author, entry):
            await ctx.send(
                tr_lang(lang, "Nur der Kanal-Besitzer darf das.", "Only the channel owner can do that."), ephemeral=True
            )
            return None, None, lang
        return channel, entry, lang

    @commands.hybrid_group(name="tempvoice", aliases=["tv"])
    @commands.guild_only()
    async def tempvoice(self, ctx: commands.Context) -> None:
        """Control your temporary voice channel."""

    @tempvoice.command(name="rename")
    @app_commands.describe(name="New channel name")
    async def tv_rename(self, ctx: commands.Context, *, name: str) -> None:
        """Rename your temp channel (Discord limit: ~2 renames / 10 min)."""
        channel, entry, lang = await self._cmd_owner_channel(ctx)
        if channel is None:
            return
        await ctx.send(await self._act_rename(channel, name, lang), ephemeral=True)

    @tempvoice.command(name="limit")
    @app_commands.describe(limit="User limit 0-99 (0 = unlimited)")
    async def tv_limit(self, ctx: commands.Context, limit: int) -> None:
        """Set the user limit of your temp channel."""
        channel, entry, lang = await self._cmd_owner_channel(ctx)
        if channel is None:
            return
        await ctx.send(await self._act_limit(channel, limit, lang), ephemeral=True)

    @tempvoice.command(name="lock")
    async def tv_lock(self, ctx: commands.Context) -> None:
        """Lock your temp channel (nobody new can connect)."""
        channel, entry, lang = await self._cmd_owner_channel(ctx)
        if channel is None:
            return
        await ctx.send(await self._act_lock(channel, True, lang), ephemeral=True)

    @tempvoice.command(name="unlock")
    async def tv_unlock(self, ctx: commands.Context) -> None:
        """Unlock your temp channel."""
        channel, entry, lang = await self._cmd_owner_channel(ctx)
        if channel is None:
            return
        await ctx.send(await self._act_lock(channel, False, lang), ephemeral=True)

    @tempvoice.command(name="hide")
    async def tv_hide(self, ctx: commands.Context) -> None:
        """Hide your temp channel from the channel list."""
        channel, entry, lang = await self._cmd_owner_channel(ctx)
        if channel is None:
            return
        await ctx.send(await self._act_hide(channel, True, lang), ephemeral=True)

    @tempvoice.command(name="unhide")
    async def tv_unhide(self, ctx: commands.Context) -> None:
        """Make your temp channel visible again."""
        channel, entry, lang = await self._cmd_owner_channel(ctx)
        if channel is None:
            return
        await ctx.send(await self._act_hide(channel, False, lang), ephemeral=True)

    @tempvoice.command(name="transfer")
    @app_commands.describe(member="New owner (must be in the channel)")
    async def tv_transfer(self, ctx: commands.Context, member: discord.Member) -> None:
        """Transfer ownership of your temp channel."""
        channel, entry, lang = await self._cmd_owner_channel(ctx)
        if channel is None:
            return
        await ctx.send(await self._act_transfer(channel, member, lang), ephemeral=True)

    @tempvoice.command(name="claim")
    async def tv_claim(self, ctx: commands.Context) -> None:
        """Claim the temp channel if its owner has left."""
        channel, entry, lang = await self._cmd_channel(ctx)
        if channel is None:
            return
        await ctx.send(await self._act_claim(channel, ctx.author, lang), ephemeral=True)

    @tempvoice.command(name="kick")
    @app_commands.describe(member="User to kick from your temp channel")
    async def tv_kick(self, ctx: commands.Context, member: discord.Member) -> None:
        """Kick a user from your temp channel."""
        channel, entry, lang = await self._cmd_owner_channel(ctx)
        if channel is None:
            return
        await ctx.send(await self._act_kick(channel, ctx.author, member, lang), ephemeral=True)

    # ------------------------------------------------------------------ #
    # Admin configuration
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="tempvoiceset", aliases=["tvset"])
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def tempvoiceset(self, ctx: commands.Context) -> None:
        """Configure the TempVoice module."""

    @tempvoiceset.command(name="creator")
    @app_commands.describe(
        action="add, remove or list",
        channel="The creator voice channel",
        name_template="Name template for temp channels ({user}, {count})",
        category="Category for new temp channels (default: creator's category)",
        user_limit="Default user limit 0-99 (0 = unlimited)",
        bitrate="Default bitrate in kbps (0 = server default)",
    )
    async def tvs_creator(
        self,
        ctx: commands.Context,
        action: str,
        channel: Optional[discord.VoiceChannel] = None,
        name_template: Optional[str] = None,
        category: Optional[discord.CategoryChannel] = None,
        user_limit: Optional[int] = None,
        bitrate: Optional[int] = None,
    ) -> None:
        """Add/remove/list creator channels ("join to create")."""
        lang = await self._lang(ctx.guild)
        action = (action or "").lower().strip()

        if action == "list":
            creators = await self.config.guild(ctx.guild).creators()
            if not creators:
                await ctx.send(tr_lang(lang, "Keine Creator-Kanäle konfiguriert.", "No creator channels configured."))
                return
            lines = []
            for cid, e in creators.items():
                ch = ctx.guild.get_channel(int(cid)) if str(cid).isdigit() else None
                cat = ctx.guild.get_channel(int(e.get("category_id") or 0))
                lines.append(
                    f"🔊 {ch.mention if ch else cid} · `{e.get('name_template', '')}` · "
                    f"{tr_lang(lang, 'Kategorie', 'Category')}: {cat.name if cat else '—'} · "
                    f"Limit: {e.get('user_limit', 0)} · Bitrate: {e.get('bitrate', 0) or '—'}"
                )
            await ctx.send("\n".join(lines)[:1900])
            return

        if channel is None:
            await ctx.send(tr_lang(lang, "Bitte einen Sprachkanal angeben.", "Please provide a voice channel."))
            return

        if action == "remove":
            async with self.config.guild(ctx.guild).creators() as creators:
                removed = creators.pop(str(channel.id), None) is not None
            await ctx.send(tr_lang(
                lang,
                "Creator-Kanal entfernt." if removed else "Dieser Kanal war kein Creator-Kanal.",
                "Creator channel removed." if removed else "That channel was not a creator channel.",
            ))
            return

        if action != "add":
            await ctx.send(tr_lang(lang, "Aktion muss `add`, `remove` oder `list` sein.", "Action must be `add`, `remove` or `list`."))
            return

        if user_limit is not None and not 0 <= user_limit <= 99:
            await ctx.send(tr_lang(lang, "Limit muss 0–99 sein.", "Limit must be 0–99."))
            return
        if bitrate is not None and not 0 <= bitrate <= 384:
            await ctx.send(tr_lang(lang, "Bitrate muss 0–384 kbps sein.", "Bitrate must be 0–384 kbps."))
            return

        async with self.config.guild(ctx.guild).creators() as creators:
            entry = dict(DEFAULT_CREATOR)
            entry.update(creators.get(str(channel.id), {}) or {})
            if name_template is not None:
                entry["name_template"] = name_template.strip()[:100] or DEFAULT_CREATOR["name_template"]
            if category is not None:
                entry["category_id"] = category.id
            if user_limit is not None:
                entry["user_limit"] = user_limit
            if bitrate is not None:
                entry["bitrate"] = bitrate
            creators[str(channel.id)] = entry
        await ctx.send(tr_lang(
            lang,
            f"Creator-Kanal **{channel.name}** gespeichert. Beitritt erstellt jetzt Temp-Kanäle.",
            f"Creator channel **{channel.name}** saved. Joining it now creates temp channels.",
        ))

    @tempvoiceset.command(name="list")
    async def tvs_list(self, ctx: commands.Context) -> None:
        """List active temporary channels."""
        lang = await self._lang(ctx.guild)
        temp = await self.config.guild(ctx.guild).temp_channels()
        lines = []
        for cid, e in (temp or {}).items():
            ch = ctx.guild.get_channel(int(cid)) if str(cid).isdigit() else None
            if ch is None:
                continue
            owner = ctx.guild.get_member(int(e.get("owner") or 0))
            lines.append(f"🔊 {ch.mention} · 👑 {owner.display_name if owner else '—'} · 👤 {len(ch.members)}")
        if not lines:
            await ctx.send(tr_lang(lang, "Keine aktiven Temp-Kanäle.", "No active temp channels."))
            return
        await ctx.send("\n".join(lines)[:1900])

    @tempvoiceset.command(name="language")
    @app_commands.describe(language="Output language: de-DE or en-US")
    async def tvs_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language for this server."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(tr_lang(language, "Sprache: Deutsch", "Language: English"))

    # ------------------------------------------------------------------ #
    # Dashboard integration
    # ------------------------------------------------------------------ #
    @dashboard_widget("tempvoice_active", L("Aktive Temp-Kanäle", "Active Temp Channels"), size="sm", permission="guild_member")
    async def tempvoice_widget(self, ctx):
        try:
            temp = await self.config.guild(ctx.guild).temp_channels()
            count = sum(1 for cid in (temp or {}) if ctx.guild.get_channel(int(cid)))
            return WidgetData.kpi(value=count, label="Temp Voice Channels")
        except Exception:
            return WidgetData.kpi(value="–", label="Temp Voice Channels")

    @dashboard_panel("tempvoice", L("Temp-Voice (Join to Create)", "Temp Voice (Join to Create)"), mount="guild_settings", permission="guild_admin")
    async def tempvoice_panel(self, ctx):
        guild_id = ctx.guild.id
        user_id = ctx.user.id

        voice = sorted(ctx.guild.voice_channels, key=lambda c: (c.position or 0, c.name.lower()))
        voice_choices = [{"value": "0", "label": "-- Select voice channel --"}]
        for c in voice:
            voice_choices.append({"value": str(c.id), "label": f"🔊 {c.name} ({c.id})"})

        selection = self._selected_creator.get((guild_id, user_id), "0")
        if selection not in {v["value"] for v in voice_choices}:
            selection = "0"
            self._selected_creator[(guild_id, user_id)] = "0"

        fields = [
            Field.select("channel_id", L("Creator-Kanal", "Creator channel"), voice_choices, value=selection, reload_on_change=True)
        ]

        if selection != "0":
            creators = await self.config.guild(ctx.guild).creators()
            entry = dict(DEFAULT_CREATOR)
            entry.update((creators or {}).get(selection, {}) or {})

            cat_choices = [{"value": "0", "label": "-- Creator's category --"}]
            for cat in sorted(ctx.guild.categories, key=lambda c: c.position or 0):
                cat_choices.append({"value": str(cat.id), "label": f"📁 {cat.name}"})
            cat_value = str(entry.get("category_id") or "0")
            if cat_value not in {v["value"] for v in cat_choices}:
                cat_value = "0"

            variables = [
                {"token": "{user}", "desc": "User"},
                {"token": "{count}", "desc": "Number"},
            ]
            fields.extend([
                Field.switch("enabled", L("Aktiv", "Enabled"), value=selection in (creators or {})),
                Field.text("name_template", L("Namens-Vorlage", "Name template"), value=str(entry.get("name_template", "")), max_length=100, variables=variables),
                Field.select("category_id", L("Kategorie für Temp-Kanäle", "Category for temp channels"), cat_choices, value=cat_value),
                Field.number("user_limit", L("Standard-Nutzerlimit (0 = aus)", "Default user limit (0 = off)"), value=int(entry.get("user_limit") or 0), min=0, max=99),
                Field.number("bitrate", L("Standard-Bitrate kbps (0 = Server-Standard)", "Default bitrate kbps (0 = server default)"), value=int(entry.get("bitrate") or 0), min=0, max=384),
            ])

        return PanelSchema(
            description=tr(
                ctx,
                "Creator-Kanäle festlegen: Beitritt erstellt einen persönlichen Temp-Kanal.",
                "Configure creator channels: joining one creates a personal temp channel.",
            ),
            fields=fields,
        )

    @tempvoice_panel.on_submit
    async def _save_tempvoice(self, ctx, data):
        guild_id = ctx.guild.id
        user_id = ctx.user.id
        channel_id = str(data.get("channel_id", "0")).strip()
        prev_sel = self._selected_creator.get((guild_id, user_id), "0")

        if channel_id != prev_sel:
            # User switched dropdown selection; just reload the panel.
            self._selected_creator[(guild_id, user_id)] = channel_id
            return SubmitResult.ok()

        if channel_id == "0":
            return SubmitResult.fail(tr(ctx, "Bitte wähle einen Sprachkanal aus.", "Please select a voice channel."))

        channel = ctx.guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        if not isinstance(channel, discord.VoiceChannel):
            return SubmitResult.fail(tr(ctx, "Ungültiger Kanal.", "Invalid channel."))

        creators = await self.config.guild(ctx.guild).creators()
        if not isinstance(creators, dict):
            creators = {}

        if not bool(data.get("enabled", False)):
            creators.pop(channel_id, None)
            await self.config.guild(ctx.guild).creators.set(creators)
            return SubmitResult.ok(tr(ctx, "Creator-Kanal entfernt.", "Creator channel removed."))

        try:
            user_limit = max(0, min(99, int(data.get("user_limit", 0) or 0)))
            bitrate = max(0, min(384, int(data.get("bitrate", 0) or 0)))
        except (TypeError, ValueError):
            return SubmitResult.fail(tr(ctx, "Bitte Zahlen prüfen.", "Please check the numbers."))

        cat_raw = str(data.get("category_id", "0") or "0")
        category_id = int(cat_raw) if cat_raw.isdigit() and cat_raw != "0" else None

        entry = dict(DEFAULT_CREATOR)
        entry.update(creators.get(channel_id, {}) or {})
        entry.update({
            "name_template": str(data.get("name_template", "")).strip()[:100] or DEFAULT_CREATOR["name_template"],
            "category_id": category_id,
            "user_limit": user_limit,
            "bitrate": bitrate,
        })
        creators[channel_id] = entry
        await self.config.guild(ctx.guild).creators.set(creators)
        return SubmitResult.ok(tr(ctx, "Gespeichert.", "Saved."))

    @dashboard_panel("language", L("Sprache", "Language"), mount="guild_settings", permission="guild_admin", order=99)
    async def language_panel(self, ctx):
        return PanelSchema(
            description=tr(ctx, "Sprache der Bot-Ausgaben für diesen Server.", "Output language for this server."),
            fields=[
                Field.select(
                    "language", L("Sprache", "Language"),
                    [{"value": "de-DE", "label": "Deutsch"}, {"value": "en-US", "label": "English"}],
                    value=str(await self.config.guild(ctx.guild).language()), reload_on_change=True,
                ),
            ],
        )

    @language_panel.on_submit
    async def _language_save(self, ctx, data):
        if "language" in data:
            await self.config.guild(ctx.guild).language.set("en-US" if data.get("language") == "en-US" else "de-DE")
        return SubmitResult.ok(tr(ctx, "Gespeichert.", "Saved."))
