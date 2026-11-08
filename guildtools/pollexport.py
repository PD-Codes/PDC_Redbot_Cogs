# guildtools/pollexport.py
import asyncio
import csv
import io
import re
import time
from typing import Dict, List, Tuple, Optional

import discord
from discord import app_commands
from redbot.core import commands

from .guildtools import csv_safe
from .pdc_dashboard import register_dashboard, unregister_dashboard, tr_lang

# Autocomplete reads channel history; cache the poll list per channel briefly
# instead of loading up to 400 messages on every keystroke.
POLL_CACHE_TTL = 60.0
POLL_SCAN_LIMIT = 400


# ---- small helper: fetch voters via REST call (paged) ----
async def fetch_answer_voters(
    client: discord.Client, channel_id: int, message_id: int, answer_id: int, limit: int = 1000
) -> List[int]:
    user_ids: List[int] = []
    after: Optional[int] = None
    fetched = 0

    while True:
        try:
            data = await client.http.get_poll_answer_voters(
                channel_id, message_id, answer_id, limit=min(100, limit - fetched), after=after
            )
        except AttributeError:
            route = discord.http.Route(
                "GET",
                "/channels/{channel_id}/polls/{message_id}/answers/{answer_id}/voters",
                channel_id=channel_id,
                message_id=message_id,
                answer_id=answer_id,
            )
            params = {"limit": min(100, limit - fetched)}
            if after:
                params["after"] = after
            data = await client.http.request(route, params=params)

        users = data.get("users", []) if isinstance(data, dict) else data
        if not users:
            break

        ids = []
        for u in users:
            uid = int(u["id"] if isinstance(u, dict) else int(u))
            ids.append(uid)
        user_ids.extend(ids)
        fetched += len(ids)

        if len(ids) < 100 or fetched >= limit:
            break
        after = ids[-1]

    return user_ids


class GuildToolsPollExport(commands.Cog):
    """Export native Discord polls as CSV (;-separated)."""

    def __init__(self, bot):
        self.bot = bot
        # channel_id -> (timestamp, [(message_id, question)])
        self._poll_cache: Dict[int, Tuple[float, List[Tuple[int, str]]]] = {}
        self._poll_scans: Dict[int, asyncio.Task] = {}

    async def cog_load(self) -> None:
        register_dashboard(self)

    def cog_unload(self) -> None:
        unregister_dashboard(self)
        for task in self._poll_scans.values():
            task.cancel()
        self._poll_scans.clear()

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

    # ---------- helpers within the class ----------
    def _ans_id(self, ans) -> int:
        # different discord.py versions: sometimes .id, sometimes .answer_id
        val = getattr(ans, "answer_id", None)
        if val is None:
            val = getattr(ans, "id", None)
        if val is None:
            raise AttributeError("PollAnswer hat weder 'answer_id' noch 'id'.")
        return int(val)

    def _ans_text(self, ans) -> str:
        # prefer plain text; otherwise maybe via poll_media; otherwise str(ans)
        txt = getattr(ans, "text", None)
        if not txt:
            pm = getattr(ans, "poll_media", None)
            txt = getattr(pm, "text", None) if pm else None
        return txt if txt else str(ans)

    @staticmethod
    def parse_message_ref(text: str, fallback_channel_id: int) -> Tuple[int, int]:
        """Parses a message ID or link. Returns (channel_id, message_id)."""
        m = re.search(r"/channels/\d+/(\d+)/(\d+)", text or "")
        if m:
            return int(m.group(1)), int(m.group(2))
        return fallback_channel_id, int(text)


    def _user_name(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        if member:
            return member.display_name  # server-specific
        user = self.bot.get_user(user_id)
        if user:
            return user.name  # global Discord name
        return str(user_id)  # fallback if completely unknown

    # ---------- Slash-Command ----------
    @app_commands.describe(
        poll="Choose the poll (autocomplete: recent polls in the channel, or paste an ID/link)",
        mode="Export view",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Key-Oriented", value="key"),
            app_commands.Choice(name="Value-Oriented", value="value"),
        ]
    )
    @app_commands.command(
        name="export-poll", description="Export a native Discord poll as CSV (;-separated).",
        extras={"i18n_desc": {"de-DE": "Native Discord-Umfrage als CSV exportieren (;-getrennt).", "en-US": "Export a native Discord poll as CSV (;-separated)."}}
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def export_poll(self, interaction: discord.Interaction, poll: str, mode: app_commands.Choice[str]):
        """Export the results of a poll."""
        # Runtime permission check (default_permissions can be overridden).
        if interaction.guild is None:
            return await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
        is_owner = await self.bot.is_owner(interaction.user)
        member_perms = getattr(interaction.user, "guild_permissions", None)
        if not is_owner and (member_perms is None or not member_perms.manage_messages):
            return await interaction.response.send_message(
                "You need the **Manage Messages** permission to use this command.",
                ephemeral=True,
            )

        await interaction.response.defer(thinking=True, ephemeral=True)
        lang = await self._lang(interaction.guild)

        # parse ID or link
        try:
            chan_id, message_id = self.parse_message_ref(poll, interaction.channel.id)
        except Exception:
            return await interaction.followup.send(tr_lang(lang, "❌ Ungültige Umfrage-Auswahl.", "❌ Invalid poll selection."), ephemeral=True)

        # fetch channel (may be a different channel/thread) — guild-local only
        ch = interaction.guild.get_channel_or_thread(chan_id)
        if ch is None:
            try:
                ch = await interaction.guild.fetch_channel(chan_id)
            except Exception:
                return await interaction.followup.send(tr_lang(lang, "❌ Ziel-Channel nicht gefunden/zugreifbar.", "❌ Target channel not found/accessible."), ephemeral=True)
        if getattr(ch, "guild", None) is None or ch.guild.id != interaction.guild.id:
            return await interaction.followup.send(tr_lang(lang, "❌ Der Ziel-Channel gehört nicht zu diesem Server.", "❌ The target channel does not belong to this server."), ephemeral=True)

        if not isinstance(ch, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
            return await interaction.followup.send(tr_lang(lang, "❌ Dieser Befehl funktioniert nur in Textchannels/Threads.", "❌ This command only works in text channels/threads."), ephemeral=True)

        # The caller must be able to read the target channel's history.
        caller_perms = ch.permissions_for(interaction.user)
        if not (caller_perms.read_message_history and caller_perms.view_channel):
            return await interaction.followup.send(tr_lang(lang, "❌ Du hast keinen Lesezugriff auf den Ziel-Channel.", "❌ You do not have read access to the target channel."), ephemeral=True)

        # load message
        try:
            msg = await ch.fetch_message(message_id)
        except discord.NotFound:
            return await interaction.followup.send(tr_lang(lang, "❌ Nachricht nicht gefunden.", "❌ Message not found."), ephemeral=True)
        except discord.Forbidden:
            return await interaction.followup.send(tr_lang(lang, "❌ Keine Berechtigung, die Nachricht zu lesen.", "❌ No permission to read the message."), ephemeral=True)

        if not getattr(msg, "poll", None):
            return await interaction.followup.send(tr_lang(lang, "❌ Diese Nachricht enthält keine Umfrage.", "❌ This message contains no poll."), ephemeral=True)

        poll_obj = msg.poll
        answers = list(poll_obj.answers or [])
        if not answers:
            return await interaction.followup.send(tr_lang(lang, "❌ Keine Antworten gefunden.", "❌ No answers found."), ephemeral=True)

        # --- collect voters per answer ---
        answer_to_voters: Dict[int, List[int]] = {}
        for ans in answers:
            ans_id = self._ans_id(ans)
            voters = await fetch_answer_voters(self.bot, msg.channel.id, msg.id, ans_id)
            answer_to_voters[ans_id] = voters

        # --- build CSV ---
        question_text = getattr(poll_obj.question, "text", str(poll_obj.question))
        answers_list: List[Tuple[int, str]] = [(self._ans_id(a), self._ans_text(a)) for a in answers]

        csv_bytes, filename = self._build_csv(
            guild=interaction.guild,
            question=question_text,
            answers=answers_list,
            answer_to_voters=answer_to_voters,
            mode=mode.value,
            lang=lang,
        )


        file = discord.File(fp=io.BytesIO(csv_bytes), filename=filename)
        title = tr_lang(lang, f"📤 CSV-Export: **{question_text}**", f"📤 CSV export: **{question_text}**")
        await interaction.followup.send(content=title, file=file, ephemeral=True)

    @export_poll.autocomplete("poll")
    async def poll_autocomplete(self, interaction: discord.Interaction, current: str):
        lang = await self._lang(interaction.guild)

        def safe_label(q: str, mid: int) -> str:
            q = (q or "").replace("\n", " ").replace("\r", " ").strip()
            if not q:
                q = tr_lang(lang, f"Umfrage {mid}", f"Poll {mid}")
            label = f"{q}  •  ID:{mid}"
            return label[:100]

        cur = (current or "").lower()
        channel = interaction.channel
        choices: List[app_commands.Choice[str]] = []
        polls = await self._recent_polls(channel) if channel is not None else []
        for mid, q in polls:
            if (not cur) or (cur in (q or "").lower()) or (cur in str(mid)):
                choices.append(app_commands.Choice(name=safe_label(q, mid), value=str(mid)))
                if len(choices) >= 25:
                    break

        if not choices:
            typed = (current or "").strip()
            if typed:
                choices = [app_commands.Choice(name=tr_lang(lang, f"Direkte Eingabe verwenden: {typed[:100]}", f"Use direct input: {typed[:100]}")[:100], value=typed[:100])]
            else:
                choices = [app_commands.Choice(name=tr_lang(lang, "Keine Umfragen gefunden – gib ID/Link ein", "No polls found – enter an ID/link"), value="0")]

        return choices[:25]

    async def _recent_polls(self, channel) -> List[Tuple[int, str]]:
        """Polls in `channel` (newest first), cached for POLL_CACHE_TTL seconds.
        Concurrent keystrokes share one running scan."""
        cid = channel.id
        hit = self._poll_cache.get(cid)
        if hit is not None and time.monotonic() - hit[0] < POLL_CACHE_TTL:
            return hit[1]
        task = self._poll_scans.get(cid)
        if task is None or task.done():
            task = asyncio.create_task(self._scan_polls(channel))
            self._poll_scans[cid] = task
        try:
            polls = await asyncio.shield(task)
        except Exception:
            return []
        finally:
            if task.done() and self._poll_scans.get(cid) is task:
                self._poll_scans.pop(cid, None)
        if len(self._poll_cache) > 500:
            self._poll_cache.clear()
        self._poll_cache[cid] = (time.monotonic(), polls)
        return polls

    async def _scan_polls(self, channel) -> List[Tuple[int, str]]:
        found: List[Tuple[int, str]] = []
        seen = set()

        def add(m: discord.Message) -> None:
            if getattr(m, "poll", None) and m.id not in seen:
                seen.add(m.id)
                found.append((m.id, getattr(m.poll.question, "text", str(m.poll.question))))

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            async for m in channel.history(limit=POLL_SCAN_LIMIT, oldest_first=False):
                add(m)
                if len(found) >= 100:
                    break
        elif isinstance(channel, discord.ForumChannel):
            threads = list(channel.threads)
            async for th in channel.archived_threads(limit=100, private=False):
                threads.append(th)
            for th in sorted(threads, key=lambda t: t.id, reverse=True):
                try:
                    sm = th.starter_message or await th.fetch_message(th.id)
                except Exception:
                    continue
                add(sm)
                if len(found) >= 100:
                    break
        return found

    # ---- CSV generation ----
    def _build_csv(
        self,
        guild: discord.Guild,
        question: str,
        answers: List[Tuple[int, str]],
        answer_to_voters: Dict[int, List[int]],
        mode: str,
        lang: str = "en-US",
    ) -> Tuple[bytes, str]:
        def esc(s: str) -> str:
            return csv_safe((s or "").replace("\r", " ").replace("\n", " ").strip())

        # Map: user_id -> [answer texts]
        user_choices: Dict[int, List[str]] = {}
        for ans_id, ans_text in answers:
            for uid in answer_to_voters.get(ans_id, []):
                user_choices.setdefault(uid, []).append(ans_text)

        # csv module: correct quoting of ";" / quotes; csv_safe: no formula injection.
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\n")
        if mode == "key":
            w.writerow(tr_lang(lang, "Wahlmöglichkeit;Wähler (Komma getrennt)", "Option;Voters (comma separated)").split(";"))
            for aid, ans_text in answers:
                voters = answer_to_voters.get(aid, [])
                voters_names = ", ".join(self._user_name(guild, uid) for uid in voters)
                w.writerow([esc(ans_text), esc(voters_names)])
            filename = "poll_export_key_oriented.csv"
        else:
            w.writerow(tr_lang(lang, "Wähler;HatGewählt (Komma getrennt)", "Voter;Voted for (comma separated)").split(";"))
            for uid, picks in user_choices.items():
                picks_str = ", ".join(sorted(picks))
                w.writerow([esc(self._user_name(guild, uid)), esc(picks_str)])
            filename = "poll_export_value_oriented.csv"

        return buf.getvalue().encode("utf-8-sig"), filename

    def _find_answer_id(self, answers: List[Tuple[int, str]], text: str) -> int:
        for aid, t in answers:
            if t == text:
                return aid
        return -1
