# GuildTools ReadyTimes Cog — Slash-only, ephemeral interactions
# Author: pd-codes (per project context)
# Requires: Red-DiscordBot (v3.5+), discord.py 2.3+
# File path suggestion: guildtools/readytimes.py (inside your GuildTools repo/package)

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List, Literal

import discord
from discord import app_commands
from redbot.core import commands, Config
from redbot.core.bot import Red

from .pdc_dashboard import register_dashboard, unregister_dashboard, tr_lang

WEEKDAYS = [
    ("monday", "Montag"),
    ("tuesday", "Dienstag"),
    ("wednesday", "Mittwoch"),
    ("thursday", "Donnerstag"),
    ("friday", "Freitag"),
    ("saturday", "Samstag"),
    ("sunday", "Sonntag"),
]

# Useful maps
DAY_KEY_TO_DE = {k: de for k, de in WEEKDAYS}
DAY_DE_TO_KEY = {de: k for k, de in WEEKDAYS}
DAY_ORDER = [k for k, _ in WEEKDAYS]

DAY_KEY_TO_EN = {
    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday",
    "thursday": "Thursday", "friday": "Friday", "saturday": "Saturday",
    "sunday": "Sunday",
}


def day_label(key: str, lang: str = "en-US") -> str:
    if lang.startswith("de"):
        return DAY_KEY_TO_DE.get(key, key)
    return DAY_KEY_TO_EN.get(key, key)

TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")  # 24h HH:MM

@dataclass
class DayAvailability:
    can: bool = False
    start: Optional[str] = None  # "HH:MM"
    end: Optional[str] = None    # "HH:MM"

    def as_tuple_minutes(self) -> Optional[Tuple[int, int]]:
        if not self.can or not self.start or not self.end:
            return None
        return (hhmm_to_min(self.start), hhmm_to_min(self.end))


def hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def min_to_hhmm(v: int) -> str:
    v = max(0, min(23 * 60 + 59, v))
    return f"{v // 60:02d}:{v % 60:02d}"


def parse_time_or_none(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    if TIME_RE.match(s):
        return s
    return None


def normalize_time_input(s: Optional[str]) -> Optional[str]:
    if s is None: return None
    s = str(s).strip()
    if not s: return None
    if TIME_RE.match(s):  # already HH:MM
        return s
    if s.isdigit():
        if len(s) <= 2:  # "22" -> 22:00
            h = int(s)
            if 0 <= h <= 23:
                return f"{h:02d}:00"
        elif len(s) == 3:  # "915" -> 09:15
            h, m = int(s[0]), int(s[1:])
        elif len(s) == 4:  # "2230" -> 22:30
            h, m = int(s[:2]), int(s[2:])
        else:
            return None
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    return None

def parse_time_or_none(s: Optional[str]) -> Optional[str]:
    return normalize_time_input(s)


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Classic overlap for non-wrapping intervals [a_start, a_end) vs [b_start, b_end)."""
    return max(a_start, b_start) < min(a_end, b_end)

def overlaps_wrap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Overlap where [a_start, a_end) may wrap past midnight (end < start)."""
    if a_end >= a_start:
        # normal same-day window
        return overlaps(a_start, a_end, b_start, b_end)
    # wrap: split into [a_start, 1440) and [0, a_end)
    return overlaps(a_start, 24 * 60, b_start, b_end) or overlaps(0, a_end, b_start, b_end)



def format_range(start: Optional[str], end: Optional[str], lang: str = "en-US") -> str:
    if start and end:
        smin, emin = hhmm_to_min(start), hhmm_to_min(end)
        if smin == emin:
            return f"{start} - {end}"  # In case you later allow 'whole day', otherwise stays like this
        # (+1) marks an overhang into the next day
        return f"{start} - {end}" + (" (+1)" if emin < smin else "")
    if start and not end:
        return tr_lang(lang, f"Ab {start}", f"From {start}")
    if end and not start:
        return tr_lang(lang, f"Bis {end}", f"Until {end}")
    return "-"



def format_range_with_parens(start: Optional[str], end: Optional[str], lang: str = "en-US") -> str:
    # For filtering displays where one side is missing
    if start and end:
        return f"{start} - {end}"
    if start and not end:
        return tr_lang(lang, f"Beginn: {start} (Bis)", f"Start: {start} (until)")
    if end and not start:
        return tr_lang(lang, f"(Ab) Ende: {end}", f"(from) End: {end}")
    return "-"


class ReadyTimes(commands.Cog):
    """GuildTools add-on: manage & query availability per weekday (ephemeral)."""

    __author__ = "pd-codes"
    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xD0DE20251, force_registration=True)
        member_defaults = {
            day: {"can": False, "start": None, "end": None} for day, _ in WEEKDAYS
        }
        self.config.register_member(**member_defaults)

    async def cog_load(self) -> None:
        register_dashboard(self)

    def cog_unload(self) -> None:
        unregister_dashboard(self)

    async def _lang(self, guild) -> str:
        """Read the per-guild output language from the GuildTools cog (shared setting)."""
        if guild is None:
            return "en-US"
        gt = self.bot.get_cog("GuildTools")
        if gt is None or not hasattr(gt, "config"):
            return "en-US"
        try:
            return await gt.config.guild(guild).language()
        except Exception:
            return "en-US"

    # ------------------------------
    # Slash: /set-readytimes (ephemeral UI)
    # ------------------------------

    @app_commands.command(name="set-readytimes", description="Set/manage your raid availability privately.", extras={"i18n_desc": {"de-DE": "Deine Raid-Verfügbarkeit privat festlegen/verwalten.", "en-US": "Set/manage your raid availability privately."}})
    async def set_readytimes(self, interaction: discord.Interaction):
        """Set your weekly ready times."""
        lang = await self._lang(interaction.guild)
        if not interaction.guild or not isinstance(interaction.user, (discord.Member,)):
            return await interaction.response.send_message(tr_lang(lang, "Nur in einem Server benutzbar.", "Only usable inside a server."), ephemeral=True)

        # Load current state
        member_cfg = await self.config.member(interaction.user).get_raw()
        avail_map: Dict[str, DayAvailability] = {
            day: DayAvailability(can=v["can"], start=v["start"], end=v["end"]) for day, v in member_cfg.items()
        }

        view = ReadyTimesView(self, interaction.user, avail_map, lang=lang)
        embed = await view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        try:
            original = await interaction.original_response()
            view.message_id = original.id
        except Exception:
            view.message_id = None
    # ------------------------------
    # Slash: /get-readytimes [day] [start] [end]
    # ------------------------------

    @app_commands.command(name="get-readytimes", description="Query who is available and when (reply is private).", extras={"i18n_desc": {"de-DE": "Abfragen, wer wann verfügbar ist (Antwort privat).", "en-US": "Query who is available and when (reply is private)."}})
    @app_commands.describe(day="Optional: weekday", start="Optional: start time HH:MM", end="Optional: end time HH:MM",user="Optional: user (shows only their times)")
    async def get_readytimes(
        self,
        interaction: discord.Interaction,
        day: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        user: Optional[discord.Member] = None,
    ):
        """Show recorded ready times."""
        lang = await self._lang(interaction.guild)
        if not interaction.guild:
            return await interaction.response.send_message(tr_lang(lang, "Nur in einem Server benutzbar.", "Only usable inside a server."), ephemeral=True)

        # ——— Read-only single overview like /set-readytimes when a user is chosen ———
        if user is not None:
            if user.bot:
                return await interaction.response.send_message(tr_lang(lang, "Bots werden nicht berücksichtigt.", "Bots are not considered."), ephemeral=True)
            if user not in interaction.guild.members:
                return await interaction.response.send_message(tr_lang(lang, "Dieser Benutzer ist nicht auf diesem Server.", "This user is not on this server."), ephemeral=True)

            data = await self.config.member(user).get_raw()

            embed = discord.Embed(
                title=tr_lang(lang, f"Verfügbarkeiten von {user.display_name}", f"Availability of {user.display_name}"),
                description=tr_lang(lang, "**Status:** Schreibgeschützt", "**Status:** Read-only"),
                color=discord.Color.blurple(),
            )
            for key in DAY_ORDER:
                info = data.get(key, {"can": False, "start": None, "end": None})
                icon = "✅" if info["can"] else "❌"
                text = tr_lang(lang, "Kann nicht", "Not available") if not info["can"] else format_range(info["start"], info["end"], lang)
                embed.add_field(name=day_label(key, lang), value=f"{icon} {text}", inline=False)

            # Optional: footer analogous to /set, but without controls
            embed.set_footer(text=tr_lang(lang, "Übersicht ohne Bearbeitungsmöglichkeiten (read-only)", "Overview without editing options (read-only)"))
            return await interaction.response.send_message(embed=embed, ephemeral=True)


        start_t = parse_time_or_none(start)
        end_t   = parse_time_or_none(end)
        if start and not start_t:
            return await interaction.response.send_message(tr_lang(lang, "Ungültige Startzeit. Nutze HH:MM (24h).", "Invalid start time. Use HH:MM (24h)."), ephemeral=True)
        if end and not end_t:
            return await interaction.response.send_message(tr_lang(lang, "Ungültige Endzeit. Nutze HH:MM (24h).", "Invalid end time. Use HH:MM (24h)."), ephemeral=True)

        # Guild members (excluding bots)
        results = []
        for member in interaction.guild.members:
            if member.bot:
                continue
            data = await self.config.member(member).get_raw()
            results.append((member, data))

        # Helper: normalize day (key like "monday"); also accept "Montag"
        day_key = None
        if day:
            d = day.strip().lower()
            # direct key?
            if d in DAY_ORDER:
                day_key = d
            else:
                # try German/English label -> key
                de2key = {v.lower(): k for k, v in DAY_KEY_TO_DE.items()}
                en2key = {v.lower(): k for k, v in DAY_KEY_TO_EN.items()}
                if d in de2key:
                    day_key = de2key[d]
                elif d in en2key:
                    day_key = en2key[d]
                else:
                    return await interaction.response.send_message(tr_lang(lang, "Unbekannter Wochentag.", "Unknown weekday."), ephemeral=True)

        none_str = tr_lang(lang, "Keiner!", "Nobody!")

        # 1) No args => overall overview
        if not day_key and not start_t and not end_t:
            embed = discord.Embed(title=tr_lang(lang, "Gesamtübersicht Verfügbarkeiten", "Overall availability"), color=discord.Color.blurple())
            for key in DAY_ORDER:
                parts: List[str] = []
                for member, data in results:
                    info = data.get(key, {"can": False, "start": None, "end": None})
                    if info["can"]:
                        parts.append(f"{member.display_name} ({format_range(info['start'], info['end'], lang)})")
                embed.add_field(
                    name=day_label(key, lang),
                    value=", ".join(parts) if parts else none_str,
                    inline=False,
                )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # 2) Day only => list including time window
        if day_key and not start_t and not end_t:
            lines: List[str] = []
            for member, data in results:
                info = data.get(day_key, {"can": False, "start": None, "end": None})
                if info["can"]:
                    lines.append(f"{member.display_name} ({format_range(info['start'], info['end'], lang)})")
            embed = discord.Embed(
                title=f"{day_label(day_key, lang)}",
                description="\n".join(lines) if lines else none_str,
                color=discord.Color.blurple(),
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # 3) Day + (start/end)
        if day_key and (start_t or end_t):
            want_start = hhmm_to_min(start_t) if start_t else None
            want_end   = hhmm_to_min(end_t)   if end_t   else None

            lines: List[str] = []
            for member, data in results:
                info = data.get(day_key, {"can": False, "start": None, "end": None})
                if not info["can"] or not info["start"] or not info["end"]:
                    continue
                a_start, a_end = hhmm_to_min(info["start"]), hhmm_to_min(info["end"])
                b_start = want_start if want_start is not None else 0
                b_end   = want_end   if want_end   is not None else 24 * 60 - 1
                if overlaps_wrap(a_start, a_end, b_start, b_end):
                    # NEW: if only "from" -> show (end); if only "to" -> show (start)
                    if start_t and not end_t:
                        lines.append(f"{member.display_name} ({info['end']})")
                    elif end_t and not start_t:
                        lines.append(f"{member.display_name} ({info['start']})")
                    else:
                        # both times given -> as before: names only
                        lines.append(member.display_name)

            title = f"{day_label(day_key, lang)} — {format_range_with_parens(start_t, end_t, lang)}"
            embed = discord.Embed(
                title=title,
                description="\n".join(lines) if lines else none_str,
                color=discord.Color.green(),
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # 4) Time(s) only (no day) => days per user; for only-from show (end), for only-to show (start)
        if (start_t or end_t) and not day_key:
            want_start = hhmm_to_min(start_t) if start_t else None
            want_end   = hhmm_to_min(end_t)   if end_t   else None

            lines: List[str] = []
            for member, data in results:
                day_tokens: List[str] = []
                for key in DAY_ORDER:
                    info = data.get(key, {"can": False, "start": None, "end": None})
                    if not info["can"] or not info["start"] or not info["end"]:
                        continue
                    a_start, a_end = hhmm_to_min(info["start"]), hhmm_to_min(info["end"])
                    b_start = want_start if want_start is not None else 0
                    b_end   = want_end   if want_end   is not None else 24 * 60 - 1
                    if overlaps_wrap(a_start, a_end, b_start, b_end):
                        if start_t and not end_t:
                            day_tokens.append(f"{day_label(key, lang)} ({info['end']})")
                        elif end_t and not start_t:
                            day_tokens.append(f"{day_label(key, lang)} ({info['start']})")
                        else:
                            day_tokens.append(day_label(key, lang))
                if day_tokens:
                    lines.append(f"{member.display_name} ({', '.join(day_tokens)})")

            title = tr_lang(lang, f"Zeitfenster — {format_range_with_parens(start_t, end_t, 'de-DE')}", f"Time window — {format_range_with_parens(start_t, end_t, 'en-US')}")
            embed = discord.Embed(
                title=title,
                description="\n".join(lines) if lines else none_str,
                color=discord.Color.purple(),
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # Fallback
        return await interaction.response.send_message(tr_lang(lang, "Ungültige Kombination.", "Invalid combination."), ephemeral=True)



class ReadyTimesView(discord.ui.View):
    def __init__(self, cog: ReadyTimes, member: discord.Member, state: Dict[str, DayAvailability], lang: str = "en-US"):
        super().__init__(timeout=600)
        self.cog = cog
        self.member = member
        self.state = state  # key -> DayAvailability
        self.lang = lang

        self.finished = False

        self.current_day_key = DAY_ORDER[0]

        # Controls
        self.day_select = DaySelect(self)
        self.add_item(self.day_select)

        self.toggle_can = ToggleCanButton(self)
        self.add_item(self.toggle_can)

        self.edit_times = EditTimesButton(self)
        self.edit_times.disabled = not self.state.get(self.current_day_key, DayAvailability()).can
        self.edit_times.label = tr_lang(self.lang, f"Zeiten setzen ({day_label(self.current_day_key, self.lang)})", f"Set times ({day_label(self.current_day_key, self.lang)})")
        self.add_item(self.edit_times)

        self.finished_btn = FinishedButton(self)
        self.add_item(self.finished_btn)

        # default set
        self.current_day_key = DAY_ORDER[0]

    async def build_embed(self) -> discord.Embed:
        lang = self.lang
        emb = discord.Embed(
            title=tr_lang(lang, f"Verfügbarkeiten von {self.member.display_name}", f"Availability of {self.member.display_name}"),
            color=discord.Color.blurple() if not self.finished else discord.Color.green(),
        )
        status = tr_lang(lang, "Bearbeitung", "Editing") if not self.finished else tr_lang(lang, "Fertig (schreibgeschützt)", "Done (read-only)")
        emb.description = (
            tr_lang(lang, f"**Aktuell ausgewählt:** {day_label(self.current_day_key, lang)}\n", f"**Currently selected:** {day_label(self.current_day_key, lang)}\n")
            + tr_lang(lang, f"**Status:** {status}", f"**Status:** {status}")
        )
        for key in DAY_ORDER:
            info = self.state.get(key, DayAvailability())
            icon = "✅" if info.can else "❌"
            text = tr_lang(lang, "Kann nicht", "Not available") if not info.can else format_range(info.start, info.end, lang)
            emb.add_field(name=day_label(key, lang), value=f"{icon} {text}", inline=False)

        footer = tr_lang(lang, "Tag auswählen ▶️ | Ja/Nein togglen | Zeiten bearbeiten", "Select day ▶️ | toggle yes/no | edit times")
        if self.finished:
            footer = tr_lang(lang, "Fertig – diese Ansicht ist gesperrt.", "Done – this view is locked.")
        emb.set_footer(text=footer)
        return emb


    async def refresh_message(self, interaction: discord.Interaction):
        # When "finished": lock all controls (incl. itself), otherwise dynamic per day
        if getattr(self, "finished", False):
            for item in self.children:
                item.disabled = True
        else:
            can_today = self.state.get(self.current_day_key, DayAvailability()).can
            self.edit_times.disabled = not can_today
            self.edit_times.label = tr_lang(self.lang, f"Zeiten setzen ({day_label(self.current_day_key, self.lang)})", f"Set times ({day_label(self.current_day_key, self.lang)})")

        embed = await self.build_embed()

        if getattr(self, "message_id", None):
            # If already responded (e.g. after modal), edit via followup
            if interaction.response.is_done():
                await interaction.followup.edit_message(self.message_id, embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)




class DaySelect(discord.ui.Select):
    def __init__(self, parent: ReadyTimesView):
        self.parent_view = parent
        lang = parent.lang
        options = [discord.SelectOption(label=day_label(key, lang), value=key) for key, _ in WEEKDAYS]
        super().__init__(placeholder=tr_lang(lang, "Wochentag wählen", "Choose weekday"), min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.current_day_key = self.values[0]
        await self.parent_view.refresh_message(interaction)


class ToggleCanButton(discord.ui.Button):
    def __init__(self, parent: ReadyTimesView):
        self.parent_view = parent
        super().__init__(style=discord.ButtonStyle.primary, label=tr_lang(parent.lang, "Kann / Kann nicht", "Available / Not available"), emoji="🔁")

    async def callback(self, interaction: discord.Interaction):
        key = self.parent_view.current_day_key
        info = self.parent_view.state.get(key, DayAvailability())
        info.can = not info.can
        if not info.can:
            # wipe times if turning off
            info.start = None
            info.end = None
        self.parent_view.state[key] = info
        # Persist
        await self.parent_view.cog.config.member(self.parent_view.member).set_raw(key, value={"can": info.can, "start": info.start, "end": info.end})
        await self.parent_view.refresh_message(interaction)


class EditTimesButton(discord.ui.Button):
    def __init__(self, parent: ReadyTimesView):
        self.parent_view = parent
        super().__init__(style=discord.ButtonStyle.secondary, label=tr_lang(parent.lang, "Zeiten setzen", "Set times"), emoji="⏱️")
        # Disabled if current day cannot
        self.disabled = not self.parent_view.state.get(self.parent_view.current_day_key, DayAvailability()).can

    async def callback(self, interaction: discord.Interaction):
        key = self.parent_view.current_day_key
        info = self.parent_view.state.get(key, DayAvailability())
        modal = TimesModal(self.parent_view, key, info.start, info.end)
        await interaction.response.send_modal(modal)

class FinishedButton(discord.ui.Button):
    def __init__(self, parent: ReadyTimesView):
        self.parent_view = parent
        super().__init__(style=discord.ButtonStyle.success, label=tr_lang(parent.lang, "Fertig!", "Done!"), emoji="✅")

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.finished = True
        # Optional: change label so it stays visible even though disabled
        self.label = tr_lang(self.parent_view.lang, "Fertig ✓", "Done ✓")
        await self.parent_view.refresh_message(interaction)



class TimesModal(discord.ui.Modal, title="Zeiten eintragen (HH:MM)"):
    start = discord.ui.TextInput(label="Von (Start)", placeholder="z. B. 19:30", required=True, max_length=5)
    end = discord.ui.TextInput(label="Bis (Ende)", placeholder="z. B. 23:00", required=True, max_length=5)

    def __init__(self, parent: ReadyTimesView, day_key: str, cur_start: Optional[str], cur_end: Optional[str]):
        lang = parent.lang
        super().__init__(title=tr_lang(lang, "Zeiten eintragen (HH:MM)", "Enter times (HH:MM)"))
        self.parent_view = parent
        self.day_key = day_key
        self.lang = lang
        self.start.label = tr_lang(lang, "Von (Start)", "From (start)")
        self.start.placeholder = tr_lang(lang, "z. B. 19:30", "e.g. 19:30")
        self.end.label = tr_lang(lang, "Bis (Ende)", "To (end)")
        self.end.placeholder = tr_lang(lang, "z. B. 23:00", "e.g. 23:00")
        if cur_start:
            self.start.default = cur_start
        if cur_end:
            self.end.default = cur_end

    async def on_submit(self, interaction: discord.Interaction):
        s = normalize_time_input(self.start.value)
        e = normalize_time_input(self.end.value)
        if not TIME_RE.match(s) or not TIME_RE.match(e):
            return await interaction.response.send_message(tr_lang(self.lang, "Bitte HH:MM 24h-Format verwenden.", "Please use HH:MM 24h format."), ephemeral=True)
        if hhmm_to_min(s) == hhmm_to_min(e):
            return await interaction.response.send_message(tr_lang(self.lang, "Start und Ende dürfen nicht gleich sein.", "Start and end must not be equal."), ephemeral=True)
        # If end < start, we interpret this as 'until next day' → allowed.


        info = self.parent_view.state.get(self.day_key, DayAvailability(can=True))
        info.can = True
        info.start = s
        info.end = e
        self.parent_view.state[self.day_key] = info
        await self.parent_view.cog.config.member(self.parent_view.member).set_raw(self.day_key, value={"can": True, "start": s, "end": e})

        # After a modal, we must send a new response first, then edit the original ephemeral message.
        #try:
        #    await interaction.response.send_message("Gespeichert.", ephemeral=True)
        #except discord.InteractionResponded:
        #    await interaction.followup.send("Gespeichert.", ephemeral=True)

        # Find the original message to edit: interaction.message is None in modals, but the view will still be attached to the original message in memory.
        # We can safely update the embed via the View helper (using followup edit on the original message via the stored View).
        # Since we don't have the original message object here, we can refresh via a dummy edit on the parent view if interaction has a message reference.
        # Fallback: re-send the panel.
        try:
            await self.parent_view.refresh_message(interaction)
        except Exception:
            # Send a fresh panel
            emb = await self.parent_view.build_embed()
            await interaction.followup.send(embed=emb, view=self.parent_view, ephemeral=True)


async def setup(bot: Red):
    await bot.add_cog(ReadyTimes(bot))
