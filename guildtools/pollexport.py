# guildtools/pollexport.py
import io
import re
from typing import Dict, List, Tuple, Optional

import discord
from discord import app_commands
from redbot.core import commands

from .pdc_dashboard import register_dashboard, unregister_dashboard, tr_lang


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
    async def export_poll(self, interaction: discord.Interaction, poll: str, mode: app_commands.Choice[str]):
        """Export the results of a poll."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        lang = await self._lang(interaction.guild)

        # parse ID or link
        try:
            chan_id, message_id = self.parse_message_ref(poll, interaction.channel.id)
        except Exception:
            return await interaction.followup.send(tr_lang(lang, "❌ Ungültige Umfrage-Auswahl.", "❌ Invalid poll selection."), ephemeral=True)

        # fetch channel (may be a different channel/thread)
        ch = interaction.guild.get_channel(chan_id)
        if ch is None:
            try:
                ch = await interaction.client.fetch_channel(chan_id)
            except Exception:
                return await interaction.followup.send(tr_lang(lang, "❌ Ziel-Channel nicht gefunden/zugreifbar.", "❌ Target channel not found/accessible."), ephemeral=True)

        if not isinstance(ch, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
            return await interaction.followup.send(tr_lang(lang, "❌ Dieser Befehl funktioniert nur in Textchannels/Threads.", "❌ This command only works in text channels/threads."), ephemeral=True)

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
        seen_ids = set()

        async def try_add_from_message(m: discord.Message):
            if getattr(m, "poll", None) and m.id not in seen_ids:
                q = getattr(m.poll.question, "text", str(m.poll.question))
                label = safe_label(q, m.id)
                if (not cur) or (cur in (q or "").lower()) or (cur in str(m.id)):
                    choices.append(app_commands.Choice(name=label, value=str(m.id)))
                    seen_ids.add(m.id)

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            async for m in channel.history(limit=400, oldest_first=False):
                await try_add_from_message(m)
                if len(choices) >= 25:
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
                await try_add_from_message(sm)
                if len(choices) >= 25:
                    break

        if not choices:
            typed = (current or "").strip()
            if typed:
                choices = [app_commands.Choice(name=tr_lang(lang, f"Direkte Eingabe verwenden: {typed[:100]}", f"Use direct input: {typed[:100]}"), value=typed)]
            else:
                choices = [app_commands.Choice(name=tr_lang(lang, "Keine Umfragen gefunden – gib ID/Link ein", "No polls found – enter an ID/link"), value="0")]

        return choices[:25]

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
        sep = ";"

        def esc(s: str) -> str:
            return (s or "").replace("\r", " ").replace("\n", " ").strip()

        # Map: user_id -> [answer texts]
        user_choices: Dict[int, List[str]] = {}
        for ans_id, ans_text in answers:
            for uid in answer_to_voters.get(ans_id, []):
                user_choices.setdefault(uid, []).append(ans_text)

        lines: List[str] = []
        if mode == "key":
            lines.append(tr_lang(lang, "Wahlmöglichkeit;Wähler (Komma getrennt)", "Option;Voters (comma separated)"))
            for aid, ans_text in answers:
                voters = answer_to_voters.get(aid, [])
                voters_names = ", ".join(self._user_name(guild, uid) for uid in voters)
                lines.append(f"{esc(ans_text)}{sep}{esc(voters_names)}")
            filename = "poll_export_key_oriented.csv"
        else:
            lines.append(tr_lang(lang, "Wähler;HatGewählt (Komma getrennt)", "Voter;Voted for (comma separated)"))
            for uid, picks in user_choices.items():
                picks_str = ", ".join(sorted(picks))
                voter_name = self._user_name(guild, uid)
                lines.append(f"{voter_name}{sep}{esc(picks_str)}")
            filename = "poll_export_value_oriented.csv"


        content = "\n".join(lines) + "\n"
        return content.encode("utf-8-sig"), filename

    def _find_answer_id(self, answers: List[Tuple[int, str]], text: str) -> int:
        for aid, t in answers:
            if t == text:
                return aid
        return -1
