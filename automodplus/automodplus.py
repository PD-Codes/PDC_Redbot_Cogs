"""AutoModPlus — anti-spam, anti-raid and regex word filter.

Complements Red's built-in Filter/Mod cogs. All detection state (sliding
windows, content hashes, join timestamps) lives purely in memory and is never
persisted — message content is not stored anywhere (privacy by design).

Rules (each individually configurable: toggle, thresholds, action, exempt
roles/channels): message flood, duplicate messages, mention spam, emoji spam,
invite links (with allowlist), external links (block/allow list), attachment
spam, ALL-CAPS ratio. Plus a regex word filter with per-pattern actions and a
join-surge (raid) detector with alert / lockdown / kick responses.

Actions are logged to a configurable log channel and forwarded to the
AdminProtocol cog when it is loaded (best-effort, no hard dependency).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import deque
from datetime import timedelta
from typing import Deque, Dict, List, Optional, Tuple

try:  # third-party engine: per-call timeout and GIL release (see info.json requirements)
    import regex as _regex
except ImportError:  # pragma: no cover - fallback when the requirement is missing
    _regex = None
try:  # Python 3.11+
    import re._parser as _sre_parse
except ImportError:  # Python <= 3.10
    import sre_parse as _sre_parse

import discord
from discord import app_commands
from redbot.core import Config, commands

from .pdc_dashboard import (
    Field,
    L,
    PanelSchema,
    SubmitResult,
    WidgetData,
    dashboard_panel,
    dashboard_widget,
    register_dashboard,
    tr_lang,
    unregister_dashboard,
)

log = logging.getLogger("red.pdc.automodplus")  # cog-wide logger

# rule key -> (DE label, EN label)
RULES: Dict[str, Tuple[str, str]] = {
    "flood": ("Nachrichten-Flut", "Message flood"),
    "duplicate": ("Doppelte Nachrichten", "Duplicate messages"),
    "mentions": ("Erwähnungs-Spam", "Mention spam"),
    "emoji": ("Emoji-Spam", "Emoji spam"),
    "invites": ("Einladungs-Links", "Invite links"),
    "links": ("Externe Links", "External links"),
    "attachments": ("Anhang-Spam", "Attachment spam"),
    "caps": ("GROSSBUCHSTABEN", "ALL-CAPS"),
}

ACTIONS = ("delete", "warn", "timeout", "kick", "ban")
RAID_ACTIONS = ("alert", "lockdown", "kick")

# Per-rule defaults. `count`/`seconds` semantics per rule:
#   flood:       count messages per `seconds` (per user)
#   duplicate:   count identical messages per `seconds` (per user)
#   mentions:    count = max mentions in a single message
#   emoji:       count = max emojis in a single message
#   attachments: count messages with attachments per `seconds` (per user)
#   caps:        count = minimum message length; `ratio` = uppercase percent
#   invites/links: no thresholds (presence-based)
DEFAULT_RULES: Dict[str, dict] = {
    "flood": {"enabled": False, "action": "delete", "timeout_minutes": 10, "count": 8, "seconds": 6, "exempt_roles": [], "exempt_channels": []},
    "duplicate": {"enabled": False, "action": "delete", "timeout_minutes": 10, "count": 4, "seconds": 30, "exempt_roles": [], "exempt_channels": []},
    "mentions": {"enabled": False, "action": "delete", "timeout_minutes": 10, "count": 6, "seconds": 0, "exempt_roles": [], "exempt_channels": []},
    "emoji": {"enabled": False, "action": "delete", "timeout_minutes": 10, "count": 15, "seconds": 0, "exempt_roles": [], "exempt_channels": []},
    "invites": {"enabled": False, "action": "delete", "timeout_minutes": 10, "count": 0, "seconds": 0, "allowlist": [], "exempt_roles": [], "exempt_channels": []},
    "links": {"enabled": False, "action": "delete", "timeout_minutes": 10, "count": 0, "seconds": 0, "mode": "block", "domains": [], "exempt_roles": [], "exempt_channels": []},
    "attachments": {"enabled": False, "action": "delete", "timeout_minutes": 10, "count": 5, "seconds": 20, "exempt_roles": [], "exempt_channels": []},
    "caps": {"enabled": False, "action": "delete", "timeout_minutes": 10, "count": 12, "seconds": 0, "ratio": 80, "exempt_roles": [], "exempt_channels": []},
}

DEFAULT_ANTIRAID = {
    "enabled": False,
    "joins": 8,
    "seconds": 30,
    "action": "alert",  # alert | lockdown | kick
    "alert_channel": None,
    "lockdown_channels": [],  # empty -> all text channels (capped)
    "lockdown_minutes": 10,
}

_INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/([a-zA-Z0-9-]+)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://([^\s/<>]+)", re.IGNORECASE)
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_UNICODE_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️]")

# Regex filter safety limits (guard against catastrophic patterns).
MAX_PATTERN_LENGTH = 200
MAX_PATTERNS_PER_GUILD = 50
REGEX_TIMEOUT = 0.1  # seconds per pattern per message
REGEX_TOTAL_BUDGET = 0.5  # seconds for all patterns of one message
MAX_CONTENT_SCAN = 2000  # only scan the first N characters
MAX_REGEX_CACHE = 500
# Quantified group whose alternatives overlap, e.g. (a|a)* or (ab|a)+ .
_RISKY_ALT_RE = re.compile(r"\((?:\?:)?((?:[^()\\]|\\.)*\|(?:[^()\\]|\\.)*)\)\s*(?:[*+]|\{\d*,\d*\})")

# How many channels a lockdown touches at most (rate-limit safety).
MAX_LOCKDOWN_CHANNELS = 50


def _content_hash(text: str) -> str:
    """Hash message content for duplicate detection — the raw text is never kept."""
    return hashlib.sha256(text.strip().lower().encode("utf-8", "ignore")).hexdigest()[:16]


def _count_emojis(text: str) -> int:
    return len(_CUSTOM_EMOJI_RE.findall(text)) + len(_UNICODE_EMOJI_RE.findall(text))


def _split_alternatives(text: str) -> List[str]:
    """Split a group body on unescaped `|`."""
    parts, cur, esc = [], [], False
    for ch in text:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            cur.append(ch)
            esc = True
        elif ch == "|":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _first_chars(items) -> Optional[set]:
    """Possible first characters of a parsed sequence; None means 'unknown/any'."""
    for op, av in items:
        name = str(op)
        if name == "AT":
            continue
        if name == "LITERAL":
            return {chr(av).lower()}
        if name == "IN":
            out: set = set()
            for iop, iav in av:
                iname = str(iop)
                if iname == "LITERAL":
                    out.add(chr(iav).lower())
                elif iname == "RANGE" and iav[1] - iav[0] <= 256:
                    out.update(chr(c).lower() for c in range(iav[0], iav[1] + 1))
                else:
                    return None
            return out
        if name == "SUBPATTERN":
            return _first_chars(av[-1])
        if name == "BRANCH":
            out = set()
            for alt in av[1]:
                f = _first_chars(alt)
                if f is None:
                    return None
                out |= f
            return out
        return None
    return None  # empty sequence can match the empty string


def _subpatterns(av):
    if isinstance(av, _sre_parse.SubPattern):
        yield av
    elif isinstance(av, (list, tuple)):
        for x in av:
            yield from _subpatterns(x)


def _pattern_risk(pattern: str) -> Optional[str]:
    """Return a reason string when a pattern is prone to catastrophic
    backtracking (nested quantifiers, overlapping alternation under a
    quantifier, excessive length); None when it looks safe."""
    if len(pattern) > MAX_PATTERN_LENGTH:
        return "length"
    for m in _RISKY_ALT_RE.finditer(pattern):
        alts = _split_alternatives(m.group(1))
        for i, a in enumerate(alts):
            for b in alts[i + 1:]:
                if not a or not b or a[0] == b[0] or a.startswith(b) or b.startswith(a):
                    return "alternation"
    try:
        parsed = _sre_parse.parse(pattern)
    except Exception:
        return None  # syntax errors are reported by the compiler

    def walk(items, in_repeat: bool) -> Optional[str]:
        for op, av in items:
            name = str(op)
            if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                _lo, hi, sub = av
                unbounded = hi == _sre_parse.MAXREPEAT or hi > 16
                if unbounded and in_repeat:
                    return "nested"
                r = walk(sub, in_repeat or unbounded)
            elif name == "BRANCH":
                if in_repeat:
                    seen: set = set()
                    for alt in av[1]:
                        f = _first_chars(alt)
                        if f is None or (seen & f):
                            return "alternation"
                        seen |= f
                r = None
                for alt in av[1]:
                    r = r or walk(alt, in_repeat)
            else:
                r = None
                for sp in _subpatterns(av):
                    r = r or walk(sp, in_repeat)
            if r:
                return r
        return None

    try:
        return walk(parsed, False)
    except RecursionError:
        return "nested"


def _compile_user_regex(pattern: str):
    """Compile a user-supplied filter pattern (case-insensitive)."""
    if _regex is not None:
        return _regex.compile(pattern, _regex.IGNORECASE | _regex.V0)
    return re.compile(pattern, re.IGNORECASE)


class AutoModPlus(commands.Cog):
    """Anti-spam, anti-raid and regex word filter."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xA7770D51, force_registration=True)
        self.config.register_guild(
            language="en-US",
            log_channel=None,
            exempt_mods=True,  # mods/admins are exempt from all rules by default
            rules=DEFAULT_RULES,
            # list of {"pattern": str, "action": str, "timeout_minutes": int}
            regex_filters=[],
            antiraid=DEFAULT_ANTIRAID,
            # Persisted lockdown so a restart can still restore the channels:
            # {"until": unix_ts, "overwrites": {channel_id(str): prior send_messages}}
            lockdown_state=None,
        )
        # ---- transient in-memory state (never persisted) ---- #
        self._flood: Dict[Tuple[int, int], Deque[float]] = {}
        self._dupes: Dict[Tuple[int, int], Deque[Tuple[float, str]]] = {}
        self._attach: Dict[Tuple[int, int], Deque[float]] = {}
        self._joins: Dict[int, Deque[float]] = {}
        # guild id -> unix timestamp until which a raid is considered active
        self._raid_active_until: Dict[int, float] = {}
        # guild id -> {"overwrites": {channel_id: prior send_messages value}, "task": Task}
        self._lockdowns: Dict[int, dict] = {}
        self._regex_cache: Dict[str, re.Pattern] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def cog_load(self) -> None:
        register_dashboard(self)
        self._restore_task = asyncio.create_task(self._restore_lockdowns())

    def cog_unload(self) -> None:
        # Cancel pending auto-unlock tasks; channel overwrites of an active
        # lockdown are NOT reverted here. The saved state stays in Config and
        # is restored (or lifted, if expired) on the next load.
        restore = getattr(self, "_restore_task", None)
        if restore is not None:
            restore.cancel()
        for state in self._lockdowns.values():
            task = state.get("task")
            if task is not None:
                task.cancel()
        self._lockdowns.clear()
        unregister_dashboard(self)

    async def _restore_lockdowns(self) -> None:
        """Re-arm (or lift, if already expired) lockdowns persisted before a restart."""
        try:
            await self.bot.wait_until_red_ready()
            all_guilds = await self.config.all_guilds()
            for gid, data in all_guilds.items():
                saved = data.get("lockdown_state")
                if not saved:
                    continue
                guild = self.bot.get_guild(int(gid))
                if guild is None or guild.id in self._lockdowns:
                    continue
                prior = {int(k): v for k, v in (saved.get("overwrites") or {}).items()}
                state: dict = {"overwrites": prior, "task": None}
                self._lockdowns[guild.id] = state
                remaining = float(saved.get("until") or 0) - time.time()
                if remaining <= 0:
                    await self._disable_lockdown(guild)
                else:
                    state["task"] = asyncio.create_task(self._auto_unlock(guild, remaining))
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Restoring persisted lockdowns failed")

    async def _auto_unlock(self, guild: discord.Guild, seconds: float) -> None:
        try:
            await asyncio.sleep(max(1.0, seconds))
            await self._disable_lockdown(guild)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Auto-unlock failed for guild %s", guild.id)

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        """Nothing is persisted; purge the transient in-memory counters."""
        for store in (self._flood, self._dupes, self._attach):
            for key in [k for k in store if k[1] == user_id]:
                store.pop(key, None)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _lang(self, guild: Optional[discord.Guild]) -> str:
        if guild is None:
            return "en-US"
        return await self.config.guild(guild).language()

    async def _is_globally_exempt(self, member: discord.Member, exempt_mods: bool) -> bool:
        if member.bot:
            return True
        if member.id == member.guild.owner_id:
            return True
        if exempt_mods:
            if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
                return True
            try:
                if await self.bot.is_mod(member):
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _is_rule_exempt(rule: dict, member: discord.Member, channel_id: int) -> bool:
        if channel_id in (rule.get("exempt_channels") or []):
            return True
        exempt_roles = set(rule.get("exempt_roles") or [])
        if exempt_roles and any(r.id in exempt_roles for r in member.roles):
            return True
        return False

    @staticmethod
    def _can_act_on(guild: discord.Guild, member: discord.Member) -> bool:
        """Never action the owner or anyone at/above the bot's top role."""
        me = guild.me
        if me is None:
            return False
        if member.id == guild.owner_id:
            return False
        return member.top_role < me.top_role

    def _window(self, store: Dict, key, seconds: float) -> Deque:
        dq = store.get(key)
        if dq is None:
            dq = store[key] = deque(maxlen=100)
        now = time.monotonic()
        while dq and isinstance(dq[0], float) and now - dq[0] > seconds:
            dq.popleft()
        return dq

    # ------------------------------------------------------------------ #
    # Logging (log channel embed + AdminProtocol best-effort)
    # ------------------------------------------------------------------ #
    async def _log_action(
        self,
        guild: discord.Guild,
        rule_label: str,
        action: str,
        member: Optional[discord.abc.User],
        detail: str = "",
    ) -> None:
        lang = await self._lang(guild)
        try:
            cid = await self.config.guild(guild).log_channel()
            channel = guild.get_channel(cid) if cid else None
            if channel is not None and channel.permissions_for(guild.me).send_messages:
                emb = discord.Embed(
                    title=tr_lang(lang, "AutoModPlus-Aktion", "AutoModPlus action"),
                    colour=discord.Colour.orange(),
                    timestamp=discord.utils.utcnow(),
                )
                emb.add_field(name=tr_lang(lang, "Regel", "Rule"), value=rule_label, inline=True)
                emb.add_field(name=tr_lang(lang, "Aktion", "Action"), value=action, inline=True)
                if member is not None:
                    emb.add_field(
                        name=tr_lang(lang, "Benutzer", "User"),
                        value=f"{member.mention} (`{member.id}`)",
                        inline=True,
                    )
                if detail:
                    emb.add_field(name=tr_lang(lang, "Details", "Details"), value=detail[:1024], inline=False)
                await channel.send(embed=emb)
        except Exception:
            log.exception("Failed to send AutoModPlus log embed")
        # Best-effort forward to AdminProtocol (same pattern as AdminUtils).
        try:
            ap = self.bot.get_cog("AdminProtocol")
            if ap is not None:
                hook = getattr(ap, "log_external_action", None)
                if hook is not None:
                    await hook(
                        guild,
                        actor=guild.me,
                        action=f"automodplus:{action}",
                        target=member,
                        reason=rule_label,
                        extra=detail or None,
                    )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Action executor
    # ------------------------------------------------------------------ #
    async def _execute_action(
        self,
        message: Optional[discord.Message],
        member: discord.Member,
        rule_key: str,
        rule_label: str,
        action: str,
        timeout_minutes: int,
        detail: str = "",
    ) -> None:
        guild = member.guild
        lang = await self._lang(guild)
        reason = f"AutoModPlus: {rule_label}"

        # Always try to remove the offending message first (all actions).
        if message is not None:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

        try:
            if action == "warn" and message is not None:
                warn_text = tr_lang(
                    lang,
                    f"⚠️ {member.mention}, bitte beachte die Serverregeln ({rule_label}).",
                    f"⚠️ {member.mention}, please follow the server rules ({rule_label}).",
                )
                try:
                    await message.channel.send(warn_text, delete_after=15)
                except discord.HTTPException:
                    pass
            elif action == "timeout":
                if guild.me.guild_permissions.moderate_members and self._can_act_on(guild, member):
                    until = discord.utils.utcnow() + timedelta(minutes=max(1, timeout_minutes))
                    await member.timeout(until, reason=reason)
            elif action == "kick":
                if guild.me.guild_permissions.kick_members and self._can_act_on(guild, member):
                    await member.kick(reason=reason)
            elif action == "ban":
                if guild.me.guild_permissions.ban_members and self._can_act_on(guild, member):
                    await guild.ban(member, reason=reason, delete_message_seconds=0)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("AutoModPlus action %s failed in guild %s", action, guild.id)

        await self._log_action(guild, rule_label, action, member, detail)

    # ------------------------------------------------------------------ #
    # Regex filter
    # ------------------------------------------------------------------ #
    def _compile_pattern(self, pattern: str):
        rx = self._regex_cache.get(pattern)
        if rx is not None:
            return rx
        try:
            rx = _compile_user_regex(pattern)
        except Exception:
            return None
        if len(self._regex_cache) >= MAX_REGEX_CACHE:
            self._regex_cache.clear()
        self._regex_cache[pattern] = rx
        return rx

    async def _regex_matches(self, pattern: str, content: str) -> bool:
        return await self._regex_match_any([pattern], content) is not None

    async def _regex_match_any(self, patterns: List[str], content: str) -> Optional[int]:
        """Check all patterns in a SINGLE worker thread with one overall time
        budget. Returns the index of the first matching pattern, or None.

        With the `regex` package every search gets a hard timeout and runs with
        the GIL released (concurrent=True), so a pathological pattern can neither
        stall the event loop nor keep a worker thread busy. The stdlib fallback
        cannot be interrupted; risky patterns are rejected when they are added."""
        compiled: List[Tuple[int, object]] = []
        for idx, pattern in enumerate(patterns):
            if not pattern:
                continue
            rx = self._compile_pattern(pattern)
            if rx is not None:
                compiled.append((idx, rx))
        if not compiled:
            return None
        text = content[:MAX_CONTENT_SCAN]

        def _scan() -> Optional[int]:
            deadline = time.monotonic() + REGEX_TOTAL_BUDGET
            for idx, rx in compiled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("Regex filter budget exhausted (remaining patterns skipped)")
                    return None
                try:
                    if _regex is not None:
                        hit = rx.search(text, timeout=min(REGEX_TIMEOUT, remaining), concurrent=True)
                    else:
                        hit = rx.search(text)
                    if hit:
                        return idx
                except TimeoutError:
                    log.warning("Regex filter #%s timed out (pattern skipped)", idx + 1)
                    continue
                except Exception:
                    continue
            return None

        try:
            return await asyncio.wait_for(asyncio.to_thread(_scan), timeout=REGEX_TOTAL_BUDGET + 0.5)
        except asyncio.TimeoutError:
            log.warning("Regex filters timed out (message skipped)")
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Message processing
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Everything is wrapped: one failure must never break message handling.
        try:
            await self._process_message(message)
        except Exception:
            log.exception("AutoModPlus message processing failed")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        # Edits must not bypass the content rules. Rate-based rules (flood,
        # duplicate, attachment spam) only count newly sent messages.
        if before.content == after.content:
            return
        try:
            await self._process_message(after, edited=True)
        except Exception:
            log.exception("AutoModPlus edit processing failed")

    async def _process_message(self, message: discord.Message, edited: bool = False) -> None:
        guild = message.guild
        if guild is None or message.author.bot or message.webhook_id is not None:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return

        data = await self.config.guild(guild).all()
        rules = data["rules"]
        regex_filters = data["regex_filters"]
        if not regex_filters and not any(r.get("enabled") for r in rules.values()):
            return
        if await self._is_globally_exempt(member, data["exempt_mods"]):
            return

        content = message.content or ""
        gid, uid, cid = guild.id, member.id, message.channel.id

        def active(key: str) -> Optional[dict]:
            rule = {**DEFAULT_RULES.get(key, {}), **(rules.get(key) or {})}
            if not rule.get("enabled"):
                return None
            if self._is_rule_exempt(rule, member, cid):
                return None
            return rule

        async def trigger(key: str, rule: dict, detail: str) -> None:
            label = tr_lang(data["language"], *RULES[key])
            await self._execute_action(
                message, member, key, label,
                str(rule.get("action") or "delete"),
                int(rule.get("timeout_minutes") or 10),
                detail,
            )

        # ---- flood ---- #
        rule = None if edited else active("flood")
        if rule:
            dq = self._window(self._flood, (gid, uid), max(1, int(rule["seconds"])))
            dq.append(time.monotonic())
            if len(dq) >= max(2, int(rule["count"])):
                dq.clear()
                return await trigger("flood", rule, f"{rule['count']} msgs / {rule['seconds']}s")

        # ---- duplicate messages (content hashed, never stored raw) ---- #
        rule = None if edited else active("duplicate")
        if rule and content:
            key = (gid, uid)
            dq = self._dupes.setdefault(key, deque(maxlen=50))
            now = time.monotonic()
            window = max(1, int(rule["seconds"]))
            while dq and now - dq[0][0] > window:
                dq.popleft()
            h = _content_hash(content)
            dq.append((now, h))
            if sum(1 for _, hh in dq if hh == h) >= max(2, int(rule["count"])):
                dq.clear()
                return await trigger("duplicate", rule, f"{rule['count']}x / {rule['seconds']}s")

        # ---- mention spam ---- #
        rule = active("mentions")
        if rule:
            n = len(message.mentions) + len(message.role_mentions) + (5 if message.mention_everyone else 0)
            if n >= max(1, int(rule["count"])):
                return await trigger("mentions", rule, f"{n} mentions")

        # ---- emoji spam ---- #
        rule = active("emoji")
        if rule and content:
            n = _count_emojis(content)
            if n >= max(1, int(rule["count"])):
                return await trigger("emoji", rule, f"{n} emojis")

        # ---- invite links (with allowlist) ---- #
        rule = active("invites")
        if rule and content:
            allow = {str(c).lower() for c in (rule.get("allowlist") or [])}
            codes = [c for c in _INVITE_RE.findall(content) if c.lower() not in allow]
            if codes:
                return await trigger("invites", rule, f"invite: {codes[0]}")

        # ---- external links (block/allow list) ---- #
        rule = active("links")
        if rule and content:
            domains = [str(d).lower().lstrip(".") for d in (rule.get("domains") or [])]
            mode = str(rule.get("mode") or "block")
            hit = None
            for host in _URL_RE.findall(content):
                host = host.lower().split(":")[0]
                if _INVITE_RE.search(f"https://{host}/"):
                    continue  # invites are handled by their own rule
                listed = any(host == d or host.endswith("." + d) for d in domains)
                if (mode == "block" and listed) or (mode == "allow" and not listed):
                    hit = host
                    break
            if hit:
                return await trigger("links", rule, f"link: {hit}")

        # ---- attachment spam ---- #
        rule = None if edited else active("attachments")
        if rule and message.attachments:
            dq = self._window(self._attach, (gid, uid), max(1, int(rule["seconds"])))
            dq.append(time.monotonic())
            if len(dq) >= max(2, int(rule["count"])):
                dq.clear()
                return await trigger("attachments", rule, f"{rule['count']} attach-msgs / {rule['seconds']}s")

        # ---- ALL-CAPS ratio ---- #
        rule = active("caps")
        if rule and content:
            letters = [c for c in content if c.isalpha()]
            min_len = max(1, int(rule["count"]))
            if len(letters) >= min_len:
                ratio = sum(1 for c in letters if c.isupper()) / len(letters)
                if ratio * 100 >= max(1, int(rule.get("ratio") or 80)):
                    return await trigger("caps", rule, f"{int(ratio * 100)}% caps")

        # ---- regex word filter (all patterns in one worker thread) ---- #
        if content and regex_filters:
            patterns = [str(e.get("pattern") or "") for e in regex_filters]
            hit_idx = await self._regex_match_any(patterns, content)
            if hit_idx is not None:
                entry = regex_filters[hit_idx]
                label = tr_lang(data["language"], "Regex-Filter", "Regex filter")
                return await self._execute_action(
                    message, member, "regex", label,
                    str(entry.get("action") or "delete"),
                    int(entry.get("timeout_minutes") or 10),
                    f"pattern #{hit_idx + 1}",
                )

    # ------------------------------------------------------------------ #
    # Anti-raid: join surge detection
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await self._process_join(member)
        except Exception:
            log.exception("AutoModPlus join processing failed")

    async def _process_join(self, member: discord.Member) -> None:
        guild = member.guild
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        ar = {**DEFAULT_ANTIRAID, **(await self.config.guild(guild).antiraid())}
        if not ar.get("enabled"):
            return
        lang = await self._lang(guild)
        now = time.monotonic()

        # During an active raid window the "kick" action removes new joins.
        if ar.get("action") == "kick" and now < self._raid_active_until.get(guild.id, 0.0):
            if guild.me.guild_permissions.kick_members and self._can_act_on(guild, member):
                try:
                    await member.kick(reason="AutoModPlus: raid protection (join surge)")
                except (discord.Forbidden, discord.HTTPException):
                    pass
                await self._log_action(
                    guild, tr_lang(lang, "Anti-Raid", "Anti-raid"), "kick", member,
                    tr_lang(lang, "Beitritt während aktiver Raid-Erkennung", "Join during active raid window"),
                )
            return

        window = max(1, int(ar.get("seconds") or 30))
        dq = self._joins.setdefault(guild.id, deque(maxlen=200))
        while dq and now - dq[0] > window:
            dq.popleft()
        dq.append(now)
        if len(dq) < max(2, int(ar.get("joins") or 8)):
            return

        # Surge detected — arm the raid window and run the configured action.
        dq.clear()
        cooldown = max(1, int(ar.get("lockdown_minutes") or 10)) * 60
        self._raid_active_until[guild.id] = now + cooldown

        detail = tr_lang(
            lang,
            f"{ar['joins']} Beitritte in {ar['seconds']}s",
            f"{ar['joins']} joins in {ar['seconds']}s",
        )
        await self._log_action(guild, tr_lang(lang, "Anti-Raid", "Anti-raid"), str(ar.get("action")), None, detail)

        # Alert message (always sent when an alert channel is configured).
        try:
            acid = ar.get("alert_channel")
            channel = guild.get_channel(acid) if acid else None
            if channel is not None and channel.permissions_for(guild.me).send_messages:
                await channel.send(
                    tr_lang(
                        lang,
                        f"🚨 **Raid-Verdacht:** {detail}. Aktion: `{ar.get('action')}`.",
                        f"🚨 **Possible raid:** {detail}. Action: `{ar.get('action')}`.",
                    )
                )
        except Exception:
            pass

        if ar.get("action") == "lockdown" and guild.id not in self._lockdowns:
            await self._enable_lockdown(guild, int(ar.get("lockdown_minutes") or 10))

    # ------------------------------------------------------------------ #
    # Lockdown
    # ------------------------------------------------------------------ #
    async def _enable_lockdown(self, guild: discord.Guild, minutes: int) -> bool:
        if guild.id in self._lockdowns:
            return False
        # Reserve the slot BEFORE the channel loop so a concurrent call cannot
        # start a second lockdown and clobber the saved overwrites.
        state: dict = {"overwrites": {}, "task": None}
        self._lockdowns[guild.id] = state
        if await self.config.guild(guild).lockdown_state():
            # A persisted lockdown (from before a restart) is still pending.
            self._lockdowns.pop(guild.id, None)
            return False

        ar = {**DEFAULT_ANTIRAID, **(await self.config.guild(guild).antiraid())}
        ids = ar.get("lockdown_channels") or []
        channels: List[discord.TextChannel] = []
        if ids:
            channels = [c for c in (guild.get_channel(i) for i in ids) if isinstance(c, discord.TextChannel)]
        else:
            channels = list(guild.text_channels)
        channels = channels[:MAX_LOCKDOWN_CHANNELS]

        prior: Dict[int, Optional[bool]] = state["overwrites"]
        role = guild.default_role
        for ch in channels:
            try:
                ow = ch.overwrites_for(role)
                prior[ch.id] = ow.send_messages
                ow.send_messages = False
                await ch.set_permissions(role, overwrite=ow, reason="AutoModPlus lockdown")
            except (discord.Forbidden, discord.HTTPException):
                prior.pop(ch.id, None)
                continue
        if not prior:
            self._lockdowns.pop(guild.id, None)
            return False

        seconds = max(1, minutes) * 60
        await self.config.guild(guild).lockdown_state.set(
            {"until": time.time() + seconds, "overwrites": {str(k): v for k, v in prior.items()}}
        )
        state["task"] = asyncio.create_task(self._auto_unlock(guild, seconds))
        lang = await self._lang(guild)
        await self._log_action(
            guild, tr_lang(lang, "Anti-Raid", "Anti-raid"), "lockdown", None,
            tr_lang(lang, f"{len(prior)} Kanäle für {minutes} min gesperrt",
                    f"{len(prior)} channels locked for {minutes} min"),
        )
        return True

    async def _disable_lockdown(self, guild: discord.Guild) -> bool:
        state = self._lockdowns.pop(guild.id, None)
        saved = await self.config.guild(guild).lockdown_state()
        if state is None and saved:
            # Lockdown from before a restart that has not been re-armed yet.
            state = {"overwrites": {int(k): v for k, v in (saved.get("overwrites") or {}).items()}, "task": None}
        if state is None:
            return False
        await self.config.guild(guild).lockdown_state.clear()
        task = state.get("task")
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        role = guild.default_role
        for cid, prior in state.get("overwrites", {}).items():
            channel = guild.get_channel(cid)
            if channel is None:
                continue
            try:
                ow = channel.overwrites_for(role)
                ow.send_messages = prior
                if ow.is_empty():
                    await channel.set_permissions(role, overwrite=None, reason="AutoModPlus unlock")
                else:
                    await channel.set_permissions(role, overwrite=ow, reason="AutoModPlus unlock")
            except (discord.Forbidden, discord.HTTPException):
                continue
        lang = await self._lang(guild)
        await self._log_action(guild, tr_lang(lang, "Anti-Raid", "Anti-raid"), "unlock", None, "")
        return True

    # ------------------------------------------------------------------ #
    # Commands (hybrid, admin tier)
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="automodplus", aliases=["amp"])
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def amp(self, ctx: commands.Context) -> None:
        """Configure AutoModPlus (anti-spam, anti-raid, regex filter)."""

    @amp.command(name="status")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_status(self, ctx: commands.Context) -> None:
        """Show all rules with their state and actions."""
        data = await self.config.guild(ctx.guild).all()
        lang = data["language"]
        lines = []
        for key, (de, en) in RULES.items():
            rule = {**DEFAULT_RULES[key], **(data["rules"].get(key) or {})}
            state = "✅" if rule.get("enabled") else "❌"
            lines.append(f"{state} **{tr_lang(lang, de, en)}** (`{key}`) → `{rule.get('action')}` "
                         f"[{rule.get('count')}/{rule.get('seconds')}s]")
        ar = {**DEFAULT_ANTIRAID, **data["antiraid"]}
        state = "✅" if ar.get("enabled") else "❌"
        lines.append(f"{state} **{tr_lang(lang, 'Anti-Raid', 'Anti-raid')}** → `{ar.get('action')}` "
                     f"[{ar.get('joins')}/{ar.get('seconds')}s, {ar.get('lockdown_minutes')} min]")
        lines.append(tr_lang(lang, f"Regex-Filter: {len(data['regex_filters'])}",
                             f"Regex filters: {len(data['regex_filters'])}"))
        cid = data["log_channel"]
        ch = ctx.guild.get_channel(cid) if cid else None
        lines.append(tr_lang(lang, f"Log-Kanal: {ch.mention if ch else '—'}",
                             f"Log channel: {ch.mention if ch else '—'}"))
        emb = discord.Embed(title="AutoModPlus", description="\n".join(lines), colour=discord.Colour.blurple())
        await ctx.send(embed=emb)

    @amp.command(name="logchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_logchannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Set (or clear) the action log channel."""
        lang = await self._lang(ctx.guild)
        if channel is None:
            await self.config.guild(ctx.guild).log_channel.clear()
            await ctx.send(tr_lang(lang, "Log-Kanal entfernt.", "Log channel cleared."))
        else:
            await self.config.guild(ctx.guild).log_channel.set(channel.id)
            await ctx.send(tr_lang(lang, f"Log-Kanal: {channel.mention}", f"Log channel: {channel.mention}"))

    @amp.command(name="language")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_language(self, ctx: commands.Context, language: str) -> None:
        """Set the output language (de/en)."""
        language = "de-DE" if language.lower().startswith("de") else "en-US"
        await self.config.guild(ctx.guild).language.set(language)
        await ctx.send(tr_lang(language, "Sprache: Deutsch", "Language: English"))

    @amp.command(name="exemptmods")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_exemptmods(self, ctx: commands.Context, enabled: bool) -> None:
        """Toggle the global mod/admin exemption (default: on)."""
        await self.config.guild(ctx.guild).exempt_mods.set(bool(enabled))
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang,
                               f"Mod/Admin-Ausnahme: {'an' if enabled else 'aus'}",
                               f"Mod/admin exemption: {'on' if enabled else 'off'}"))

    # ---- rule configuration ---- #
    @amp.group(name="rule")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_rule(self, ctx: commands.Context) -> None:
        """Configure a single rule (flood, duplicate, mentions, emoji, invites, links, attachments, caps)."""

    async def _get_rule(self, ctx: commands.Context, rule: str) -> Optional[str]:
        rule = rule.lower().strip()
        if rule not in RULES:
            lang = await self._lang(ctx.guild)
            await ctx.send(tr_lang(lang,
                                   f"Unbekannte Regel. Verfügbar: {', '.join(RULES)}",
                                   f"Unknown rule. Available: {', '.join(RULES)}"))
            return None
        return rule

    async def _update_rule(self, guild: discord.Guild, rule: str, **changes) -> None:
        async with self.config.guild(guild).rules() as rules:
            cur = {**DEFAULT_RULES[rule], **(rules.get(rule) or {})}
            cur.update(changes)
            rules[rule] = cur

    @amp_rule.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def rule_enable(self, ctx: commands.Context, rule: str) -> None:
        """Enable a rule."""
        key = await self._get_rule(ctx, rule)
        if key is None:
            return
        await self._update_rule(ctx.guild, key, enabled=True)
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang, f"Regel `{key}` aktiviert.", f"Rule `{key}` enabled."))

    @amp_rule.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def rule_disable(self, ctx: commands.Context, rule: str) -> None:
        """Disable a rule."""
        key = await self._get_rule(ctx, rule)
        if key is None:
            return
        await self._update_rule(ctx.guild, key, enabled=False)
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang, f"Regel `{key}` deaktiviert.", f"Rule `{key}` disabled."))

    @amp_rule.command(name="action")
    @commands.admin_or_permissions(manage_guild=True)
    async def rule_action(self, ctx: commands.Context, rule: str, action: str, timeout_minutes: int = 10) -> None:
        """Set the action for a rule: delete / warn / timeout / kick / ban."""
        key = await self._get_rule(ctx, rule)
        if key is None:
            return
        lang = await self._lang(ctx.guild)
        action = action.lower().strip()
        if action not in ACTIONS:
            return await ctx.send(tr_lang(lang,
                                          f"Ungültige Aktion. Verfügbar: {', '.join(ACTIONS)}",
                                          f"Invalid action. Available: {', '.join(ACTIONS)}"))
        await self._update_rule(ctx.guild, key, action=action,
                                timeout_minutes=max(1, min(40320, timeout_minutes)))
        await ctx.send(tr_lang(lang, f"Regel `{key}`: Aktion `{action}`.", f"Rule `{key}`: action `{action}`."))

    @amp_rule.command(name="threshold")
    @commands.admin_or_permissions(manage_guild=True)
    async def rule_threshold(self, ctx: commands.Context, rule: str, count: int, seconds: int = 0) -> None:
        """Set a rule's threshold (count and, where applicable, window seconds)."""
        key = await self._get_rule(ctx, rule)
        if key is None:
            return
        changes = {"count": max(1, min(1000, count))}
        if seconds > 0:
            changes["seconds"] = max(1, min(3600, seconds))
        await self._update_rule(ctx.guild, key, **changes)
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang, f"Regel `{key}`: Schwelle gesetzt.", f"Rule `{key}`: threshold set."))

    @amp_rule.command(name="exemptrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def rule_exemptrole(self, ctx: commands.Context, rule: str, mode: str, role: discord.Role) -> None:
        """Add or remove an exempt role for a rule (mode: add/remove)."""
        key = await self._get_rule(ctx, rule)
        if key is None:
            return
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).rules() as rules:
            cur = {**DEFAULT_RULES[key], **(rules.get(key) or {})}
            roles = list(cur.get("exempt_roles") or [])
            if mode.lower() == "add" and role.id not in roles:
                roles.append(role.id)
            elif mode.lower() == "remove" and role.id in roles:
                roles.remove(role.id)
            cur["exempt_roles"] = roles
            rules[key] = cur
        await ctx.send(tr_lang(lang, f"Regel `{key}`: Ausnahme-Rollen aktualisiert.",
                               f"Rule `{key}`: exempt roles updated."))

    @amp_rule.command(name="exemptchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def rule_exemptchannel(self, ctx: commands.Context, rule: str, mode: str, channel: discord.TextChannel) -> None:
        """Add or remove an exempt channel for a rule (mode: add/remove)."""
        key = await self._get_rule(ctx, rule)
        if key is None:
            return
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).rules() as rules:
            cur = {**DEFAULT_RULES[key], **(rules.get(key) or {})}
            channels = list(cur.get("exempt_channels") or [])
            if mode.lower() == "add" and channel.id not in channels:
                channels.append(channel.id)
            elif mode.lower() == "remove" and channel.id in channels:
                channels.remove(channel.id)
            cur["exempt_channels"] = channels
            rules[key] = cur
        await ctx.send(tr_lang(lang, f"Regel `{key}`: Ausnahme-Kanäle aktualisiert.",
                               f"Rule `{key}`: exempt channels updated."))

    @amp.command(name="capsratio")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_capsratio(self, ctx: commands.Context, percent: int) -> None:
        """Set the ALL-CAPS trigger ratio in percent (e.g. 80)."""
        await self._update_rule(ctx.guild, "caps", ratio=max(10, min(100, percent)))
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang, f"CAPS-Schwelle: {percent}%", f"Caps threshold: {percent}%"))

    @amp.command(name="inviteallow")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_inviteallow(self, ctx: commands.Context, mode: str, code: Optional[str] = None) -> None:
        """Manage the invite allowlist (mode: add/remove/list)."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).rules() as rules:
            cur = {**DEFAULT_RULES["invites"], **(rules.get("invites") or {})}
            allow = [str(c) for c in (cur.get("allowlist") or [])]
            mode = mode.lower()
            if mode == "list":
                text = ", ".join(allow) or "—"
                return await ctx.send(tr_lang(lang, f"Erlaubte Invites: {text}", f"Allowed invites: {text}"))
            if not code:
                return await ctx.send(tr_lang(lang, "Bitte einen Invite-Code angeben.", "Please provide an invite code."))
            code = code.strip().rsplit("/", 1)[-1]
            if mode == "add" and code not in allow:
                allow.append(code)
            elif mode == "remove" and code in allow:
                allow.remove(code)
            cur["allowlist"] = allow
            rules["invites"] = cur
        await ctx.send(tr_lang(lang, "Invite-Allowlist aktualisiert.", "Invite allowlist updated."))

    @amp.command(name="linkmode")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_linkmode(self, ctx: commands.Context, mode: str) -> None:
        """Set the link filter mode: block (blocklist) or allow (allowlist only)."""
        lang = await self._lang(ctx.guild)
        mode = mode.lower().strip()
        if mode not in ("block", "allow"):
            return await ctx.send(tr_lang(lang, "Modus: `block` oder `allow`.", "Mode: `block` or `allow`."))
        await self._update_rule(ctx.guild, "links", mode=mode)
        await ctx.send(tr_lang(lang, f"Link-Modus: `{mode}`", f"Link mode: `{mode}`"))

    @amp.command(name="linkdomain")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_linkdomain(self, ctx: commands.Context, mode: str, domain: Optional[str] = None) -> None:
        """Manage the link domain list (mode: add/remove/list)."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).rules() as rules:
            cur = {**DEFAULT_RULES["links"], **(rules.get("links") or {})}
            domains = [str(d) for d in (cur.get("domains") or [])]
            mode = mode.lower()
            if mode == "list":
                text = ", ".join(domains) or "—"
                return await ctx.send(tr_lang(lang, f"Domains: {text}", f"Domains: {text}"))
            if not domain:
                return await ctx.send(tr_lang(lang, "Bitte eine Domain angeben.", "Please provide a domain."))
            domain = domain.lower().strip().lstrip(".")
            if mode == "add" and domain not in domains:
                domains.append(domain)
            elif mode == "remove" and domain in domains:
                domains.remove(domain)
            cur["domains"] = domains
            rules["links"] = cur
        await ctx.send(tr_lang(lang, "Domain-Liste aktualisiert.", "Domain list updated."))

    # ---- regex filter ---- #
    @amp.group(name="regex")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_regex(self, ctx: commands.Context) -> None:
        """Manage the regex word filter."""

    @amp_regex.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def regex_add(self, ctx: commands.Context, action: str, timeout_minutes: int, *, pattern: str) -> None:
        """Add a regex pattern: action (delete/warn/timeout/kick/ban), timeout minutes, pattern."""
        lang = await self._lang(ctx.guild)
        action = action.lower().strip()
        if action not in ACTIONS:
            return await ctx.send(tr_lang(lang,
                                          f"Ungültige Aktion. Verfügbar: {', '.join(ACTIONS)}",
                                          f"Invalid action. Available: {', '.join(ACTIONS)}"))
        pattern = pattern.strip()
        if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
            return await ctx.send(tr_lang(lang,
                                          f"Muster muss 1–{MAX_PATTERN_LENGTH} Zeichen lang sein.",
                                          f"Pattern must be 1–{MAX_PATTERN_LENGTH} characters long."))
        try:
            re.compile(pattern, re.IGNORECASE)
            _compile_user_regex(pattern)
        except Exception as e:
            return await ctx.send(tr_lang(lang, f"Ungültiges Regex-Muster: `{e}`", f"Invalid regex pattern: `{e}`"))
        risk = _pattern_risk(pattern)
        if risk is not None:
            return await ctx.send(tr_lang(
                lang,
                "Muster abgelehnt: verschachtelte Quantifizierer wie `(a+)+` oder überlappende "
                "Alternativen wie `(a|a)*` können den Bot ausbremsen. Bitte vereinfachen.",
                "Pattern rejected: nested quantifiers like `(a+)+` or overlapping alternatives "
                "like `(a|a)*` can stall the bot. Please simplify it.",
            ))
        async with self.config.guild(ctx.guild).regex_filters() as filters:
            if len(filters) >= MAX_PATTERNS_PER_GUILD:
                return await ctx.send(tr_lang(lang,
                                              f"Maximal {MAX_PATTERNS_PER_GUILD} Muster pro Server.",
                                              f"At most {MAX_PATTERNS_PER_GUILD} patterns per server."))
            filters.append({
                "pattern": pattern,
                "action": action,
                "timeout_minutes": max(1, min(40320, int(timeout_minutes or 10))),
            })
        await ctx.send(tr_lang(lang, "Regex-Filter hinzugefügt.", "Regex filter added."))

    @amp_regex.command(name="remove")
    @commands.admin_or_permissions(manage_guild=True)
    async def regex_remove(self, ctx: commands.Context, index: int) -> None:
        """Remove a regex pattern by its list number (see `regex list`)."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).regex_filters() as filters:
            if not 1 <= index <= len(filters):
                return await ctx.send(tr_lang(lang, "Ungültige Nummer.", "Invalid number."))
            filters.pop(index - 1)
        await ctx.send(tr_lang(lang, "Regex-Filter entfernt.", "Regex filter removed."))

    @amp_regex.command(name="list")
    @commands.admin_or_permissions(manage_guild=True)
    async def regex_list(self, ctx: commands.Context) -> None:
        """List all regex patterns."""
        lang = await self._lang(ctx.guild)
        filters = await self.config.guild(ctx.guild).regex_filters()
        if not filters:
            return await ctx.send(tr_lang(lang, "Keine Regex-Filter konfiguriert.", "No regex filters configured."))
        lines = [
            f"`{i + 1}.` `{f.get('pattern')}` → `{f.get('action')}`"
            for i, f in enumerate(filters)
        ]
        await ctx.send("\n".join(lines)[:1900])

    # ---- anti-raid ---- #
    @amp.group(name="raid")
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_raid(self, ctx: commands.Context) -> None:
        """Configure join-surge (raid) detection."""

    async def _update_raid(self, guild: discord.Guild, **changes) -> None:
        async with self.config.guild(guild).antiraid() as ar:
            merged = {**DEFAULT_ANTIRAID, **ar}
            merged.update(changes)
            ar.clear()
            ar.update(merged)

    @amp_raid.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def raid_enable(self, ctx: commands.Context) -> None:
        """Enable raid detection."""
        await self._update_raid(ctx.guild, enabled=True)
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang, "Anti-Raid aktiviert.", "Anti-raid enabled."))

    @amp_raid.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def raid_disable(self, ctx: commands.Context) -> None:
        """Disable raid detection."""
        await self._update_raid(ctx.guild, enabled=False)
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang, "Anti-Raid deaktiviert.", "Anti-raid disabled."))

    @amp_raid.command(name="threshold")
    @commands.admin_or_permissions(manage_guild=True)
    async def raid_threshold(self, ctx: commands.Context, joins: int, seconds: int) -> None:
        """Set the surge threshold: X joins within Y seconds."""
        await self._update_raid(ctx.guild, joins=max(2, min(500, joins)), seconds=max(5, min(3600, seconds)))
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang, f"Schwelle: {joins} Beitritte / {seconds}s",
                               f"Threshold: {joins} joins / {seconds}s"))

    @amp_raid.command(name="action")
    @commands.admin_or_permissions(manage_guild=True)
    async def raid_action(self, ctx: commands.Context, action: str) -> None:
        """Set the raid response: alert / lockdown / kick."""
        lang = await self._lang(ctx.guild)
        action = action.lower().strip()
        if action not in RAID_ACTIONS:
            return await ctx.send(tr_lang(lang,
                                          f"Ungültige Aktion. Verfügbar: {', '.join(RAID_ACTIONS)}",
                                          f"Invalid action. Available: {', '.join(RAID_ACTIONS)}"))
        await self._update_raid(ctx.guild, action=action)
        await ctx.send(tr_lang(lang, f"Raid-Aktion: `{action}`", f"Raid action: `{action}`"))

    @amp_raid.command(name="alertchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def raid_alertchannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Set (or clear) the raid alert channel."""
        await self._update_raid(ctx.guild, alert_channel=channel.id if channel else None)
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang,
                               f"Alarm-Kanal: {channel.mention if channel else '—'}",
                               f"Alert channel: {channel.mention if channel else '—'}"))

    @amp_raid.command(name="lockdownchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def raid_lockdownchannel(self, ctx: commands.Context, mode: str, channel: discord.TextChannel) -> None:
        """Add or remove a lockdown channel (mode: add/remove). Empty list = all text channels."""
        lang = await self._lang(ctx.guild)
        async with self.config.guild(ctx.guild).antiraid() as ar:
            merged = {**DEFAULT_ANTIRAID, **ar}
            channels = list(merged.get("lockdown_channels") or [])
            if mode.lower() == "add" and channel.id not in channels:
                channels.append(channel.id)
            elif mode.lower() == "remove" and channel.id in channels:
                channels.remove(channel.id)
            merged["lockdown_channels"] = channels
            ar.clear()
            ar.update(merged)
        await ctx.send(tr_lang(lang, "Lockdown-Kanäle aktualisiert.", "Lockdown channels updated."))

    @amp_raid.command(name="duration")
    @commands.admin_or_permissions(manage_guild=True)
    async def raid_duration(self, ctx: commands.Context, minutes: int) -> None:
        """Set the lockdown/raid-window duration in minutes."""
        await self._update_raid(ctx.guild, lockdown_minutes=max(1, min(1440, minutes)))
        lang = await self._lang(ctx.guild)
        await ctx.send(tr_lang(lang, f"Dauer: {minutes} min", f"Duration: {minutes} min"))

    @amp.command(name="lockdown")
    @commands.bot_has_guild_permissions(manage_channels=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_lockdown(self, ctx: commands.Context, minutes: Optional[int] = None) -> None:
        """Manually enable lockdown (removes @everyone send permission in the configured channels)."""
        lang = await self._lang(ctx.guild)
        ar = {**DEFAULT_ANTIRAID, **(await self.config.guild(ctx.guild).antiraid())}
        duration = max(1, min(1440, minutes or int(ar.get("lockdown_minutes") or 10)))
        if await self._enable_lockdown(ctx.guild, duration):
            await ctx.send(tr_lang(lang, f"🔒 Lockdown aktiv für {duration} min.",
                                   f"🔒 Lockdown active for {duration} min."))
        else:
            await ctx.send(tr_lang(lang, "Lockdown ist bereits aktiv oder fehlgeschlagen.",
                                   "Lockdown already active or failed."))

    @amp.command(name="unlock")
    @commands.bot_has_guild_permissions(manage_channels=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def amp_unlock(self, ctx: commands.Context) -> None:
        """Manually lift an active lockdown."""
        lang = await self._lang(ctx.guild)
        self._raid_active_until.pop(ctx.guild.id, None)
        if await self._disable_lockdown(ctx.guild):
            await ctx.send(tr_lang(lang, "🔓 Lockdown aufgehoben.", "🔓 Lockdown lifted."))
        else:
            await ctx.send(tr_lang(lang, "Kein aktiver Lockdown.", "No active lockdown."))

    # ------------------------------------------------------------------ #
    # Dashboard integration
    # ------------------------------------------------------------------ #
    @dashboard_widget("automodplus_rules", L("AutoModPlus-Regeln", "AutoModPlus rules"), size="sm", permission="guild_member")
    async def rules_widget(self, ctx):
        try:
            rules = await self.config.guild(ctx.guild).rules()
            enabled = sum(1 for r in rules.values() if r.get("enabled"))
            return WidgetData.kpi(value=f"{enabled}/{len(RULES)}", label="AutoModPlus")
        except Exception:
            return WidgetData.kpi(value="–", label="AutoModPlus")

    @dashboard_panel("automodplus", L("AutoModPlus — Allgemein", "AutoModPlus — General"), mount="guild_settings", permission="guild_admin", order=60)
    async def general_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        channels = [{"value": "", "label": "—"}] + [
            {"value": str(c.id), "label": f"#{c.name}"} for c in ctx.guild.text_channels
        ]
        return PanelSchema(
            description=tr_lang(lang, "Anti-Spam, Anti-Raid und Regex-Filter.",
                                "Anti-spam, anti-raid and regex filter."),
            fields=[
                Field.select("log_channel", L("Log-Kanal", "Log channel"), channels,
                             value=str(await conf.log_channel() or "")),
                Field.switch("exempt_mods", L("Mods/Admins ausnehmen", "Exempt mods/admins"),
                             value=bool(await conf.exempt_mods())),
                Field.select("language", L("Sprache", "Language"),
                             [{"value": "de-DE", "label": "Deutsch"}, {"value": "en-US", "label": "English"}],
                             value=str(lang), reload_on_change=True),
            ],
        )

    @general_panel.on_submit
    async def _save_general(self, ctx, data):
        conf = self.config.guild(ctx.guild)
        ch = str(data.get("log_channel") or "").strip()
        if ch.isdigit() and ctx.guild.get_channel(int(ch)) is not None:
            await conf.log_channel.set(int(ch))
        else:
            await conf.log_channel.clear()
        await conf.exempt_mods.set(bool(data.get("exempt_mods")))
        lang = str(data.get("language", "en-US")).strip()
        if lang not in ("de-DE", "en-US"):
            lang = "en-US"
        await conf.language.set(lang)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    @dashboard_panel("automodplus_rules", L("AutoModPlus — Regeln", "AutoModPlus — Rules"), mount="guild_settings", permission="guild_admin", order=61)
    async def rules_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        rules = await conf.rules()
        actions = [{"value": a, "label": a} for a in ACTIONS]
        fields = []
        for key, (de, en) in RULES.items():
            rule = {**DEFAULT_RULES[key], **(rules.get(key) or {})}
            fields.append(Field.switch(f"{key}_enabled", L(f"{de} aktiv", f"{en} enabled"),
                                       value=bool(rule.get("enabled"))))
            fields.append(Field.select(f"{key}_action", L(f"{de}: Aktion", f"{en}: action"),
                                       actions, value=str(rule.get("action") or "delete")))
            fields.append(Field.number(f"{key}_count", L(f"{de}: Anzahl", f"{en}: count"),
                                       value=int(rule.get("count") or 0)))
            fields.append(Field.number(f"{key}_seconds", L(f"{de}: Sekunden", f"{en}: seconds"),
                                       value=int(rule.get("seconds") or 0)))
        return PanelSchema(
            description=tr_lang(lang, "Schwellen und Aktionen pro Regel.",
                                "Thresholds and actions per rule."),
            fields=fields,
        )

    @rules_panel.on_submit
    async def _save_rules(self, ctx, data):
        lang = await self.config.guild(ctx.guild).language()
        async with self.config.guild(ctx.guild).rules() as rules:
            for key in RULES:
                cur = {**DEFAULT_RULES[key], **(rules.get(key) or {})}
                if f"{key}_enabled" in data:
                    cur["enabled"] = bool(data.get(f"{key}_enabled"))
                action = str(data.get(f"{key}_action") or cur.get("action"))
                if action in ACTIONS:
                    cur["action"] = action
                for num_key in ("count", "seconds"):
                    try:
                        cur[num_key] = max(0, min(3600, int(data.get(f"{key}_{num_key}", cur.get(num_key) or 0))))
                    except (TypeError, ValueError):
                        pass
                rules[key] = cur
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))

    @dashboard_panel("automodplus_raid", L("AutoModPlus — Anti-Raid", "AutoModPlus — Anti-raid"), mount="guild_settings", permission="guild_admin", order=62)
    async def raid_panel(self, ctx):
        conf = self.config.guild(ctx.guild)
        lang = await conf.language()
        ar = {**DEFAULT_ANTIRAID, **(await conf.antiraid())}
        channels = [{"value": "", "label": "—"}] + [
            {"value": str(c.id), "label": f"#{c.name}"} for c in ctx.guild.text_channels
        ]
        return PanelSchema(
            description=tr_lang(lang, "Erkennung von Beitritts-Wellen.", "Join surge detection."),
            fields=[
                Field.switch("enabled", L("Aktiviert", "Enabled"), value=bool(ar.get("enabled"))),
                Field.number("joins", L("Beitritte", "Joins"), value=int(ar.get("joins") or 8)),
                Field.number("seconds", L("Sekunden", "Seconds"), value=int(ar.get("seconds") or 30)),
                Field.select("action", L("Aktion", "Action"),
                             [{"value": a, "label": a} for a in RAID_ACTIONS],
                             value=str(ar.get("action") or "alert")),
                Field.select("alert_channel", L("Alarm-Kanal", "Alert channel"), channels,
                             value=str(ar.get("alert_channel") or "")),
                Field.number("lockdown_minutes", L("Dauer (Minuten)", "Duration (minutes)"),
                             value=int(ar.get("lockdown_minutes") or 10)),
            ],
        )

    @raid_panel.on_submit
    async def _save_raid(self, ctx, data):
        lang = await self.config.guild(ctx.guild).language()
        changes = {"enabled": bool(data.get("enabled"))}
        try:
            changes["joins"] = max(2, min(500, int(data.get("joins") or 8)))
            changes["seconds"] = max(5, min(3600, int(data.get("seconds") or 30)))
            changes["lockdown_minutes"] = max(1, min(1440, int(data.get("lockdown_minutes") or 10)))
        except (TypeError, ValueError):
            pass
        action = str(data.get("action") or "alert")
        if action in RAID_ACTIONS:
            changes["action"] = action
        ch = str(data.get("alert_channel") or "").strip()
        changes["alert_channel"] = int(ch) if ch.isdigit() and ctx.guild.get_channel(int(ch)) else None
        await self._update_raid(ctx.guild, **changes)
        return SubmitResult.ok(tr_lang(lang, "Gespeichert.", "Saved."))
