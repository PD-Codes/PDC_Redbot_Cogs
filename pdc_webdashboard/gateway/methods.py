"""Core RPC methods of the gateway.

Convention for ``params``::

    {
      "auth": {"user_id": "123", "guild_id": "456", "locale": "de-DE"},
      "args": { ... method-specific ... }
    }

The auth data comes from the (trusted) BFF, which has already performed the
Discord OAuth2 login. Permissions are re-checked here on the server side.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from ..integration.context import DashboardContext
from ..permissions import Level, _level_value, has_permission, resolve_level
from .rpc import (
    FORBIDDEN,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    UNAUTHORIZED,
    Dispatcher,
    RpcError,
)

log = logging.getLogger("red.pdc.pdc_webdashboard.methods")

dispatcher = Dispatcher()

# Serialises all Downloader operations (install/update/uninstall AND the repo
# listing read). The Downloader is not safe for concurrent access: an install
# rewrites the repo working tree + Config, and reading repo.available_cogs /
# _available_updates while that happens returns a PARTIAL scan (cogs/update flags
# momentarily vanish). The read path therefore takes the same lock so a page
# refresh never observes a mid-mutation state. asyncio.Lock is fair (FIFO) so
# requests run in click order.
_downloader_lock = asyncio.Lock()


class _LightUser:
    """Minimal user stand-in (ID only) for when the real user is neither in the
    cache nor retrievable via the API. Sufficient for all permission checks
    (id-based) and avoids a hard failure on cache/network problems."""

    __slots__ = ("id", "name", "bot")

    def __init__(self, uid: int) -> None:
        self.id = uid
        self.name = None
        self.bot = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _build_context(gateway: Any, params: Dict[str, Any]) -> DashboardContext:
    bot = gateway.bot
    auth = params.get("auth") or {}
    user_id = auth.get("user_id")
    if not user_id:
        raise RpcError(UNAUTHORIZED, "Kein authentifizierter Benutzer im Request")

    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise RpcError(INVALID_PARAMS, "Ungültige user_id")

    user = bot.get_user(uid)
    if user is None:
        try:
            user = await bot.fetch_user(uid)
        except Exception:
            # Not a hard error: id-based permission checks still work.
            user = _LightUser(uid)

    guild = None
    member = None
    gid = auth.get("guild_id")
    if gid:
        guild = bot.get_guild(int(gid))
        if guild is not None:
            member = guild.get_member(uid)

    return DashboardContext(
        bot=bot,
        user=user,
        guild=guild,
        member=member,
        locale=auth.get("locale", "en-US"),
        params=params.get("args") or {},
    )


async def _require(gateway: Any, ctx: DashboardContext, permission: str) -> None:
    # Lock: if the dashboard is locked, only the bot owner may run protected calls.
    cog = gateway.bot.get_cog("pdc_webdashboard") or gateway.bot.get_cog("WebDashboard")
    if cog is not None:
        try:
            if await cog.config.locked() and not await gateway.bot.is_owner(ctx.user):
                raise RpcError(FORBIDDEN, "Dashboard ist gesperrt")
        except RpcError:
            raise
        except Exception:
            pass
    # SECURITY: Any guild context requires at least membership – even if the
    # contribution only requires "authenticated". Otherwise logged-in non-members
    # could read content of other servers by manipulating the guild_id.
    required = permission
    if ctx.guild is not None and _level_value(permission) < int(Level.GUILD_MEMBER):
        required = "guild_member"
    if not await has_permission(gateway.bot, ctx.user, required, ctx.guild):
        raise RpcError(FORBIDDEN, f"Berechtigung '{permission}' erforderlich")


# --------------------------------------------------------------------------- #
# Central input validation for write payloads coming from the web app
# --------------------------------------------------------------------------- #
# Applied in the gateway BEFORE data reaches any integrated cog's on_submit /
# on_edit handler, so every third-party panel benefits without doing its own
# sanitisation. Limits are deliberately generous (Discord messages cap at
# 2000/4096 chars) but hard enough to stop abuse via oversized payloads.
_SANITIZE_MAX_STR = 8000       # max characters per string value
_SANITIZE_MAX_KEY = 200        # max characters per dict key
_SANITIZE_MAX_ITEMS = 500      # max entries per list
_SANITIZE_MAX_KEYS = 100       # max keys per dict
_SANITIZE_MAX_DEPTH = 6        # max nesting depth
_SANITIZE_MAX_NUM = 1e15       # max magnitude for numbers

# C0/C1 control characters except \n (0x0A) and \t (0x09); also DEL (0x7F).
_CTRL_TABLE = {c: None for c in list(range(0x00, 0x20)) + list(range(0x7F, 0xA0))}
del _CTRL_TABLE[0x0A]
del _CTRL_TABLE[0x09]


def _sanitize_str(value: str, max_len: int = _SANITIZE_MAX_STR) -> str:
    """Strip control characters (keep newline/tab) and cap the length."""
    cleaned = value.translate(_CTRL_TABLE)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def _sanitize_value(value: Any, depth: int = 0) -> Any:
    """Recursively validate/sanitise a JSON value from a panel submit.

    - Strings: control characters stripped, length capped.
    - Numbers: bool/int/float allowed; NaN/inf and absurd magnitudes rejected.
    - Lists/dicts: size, key length and nesting depth capped.
    - Any other type is rejected with INVALID_PARAMS.
    """
    if depth > _SANITIZE_MAX_DEPTH:
        raise RpcError(INVALID_PARAMS, "Payload nesting too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _sanitize_str(value)
    if isinstance(value, int):
        if abs(value) > _SANITIZE_MAX_NUM:
            raise RpcError(INVALID_PARAMS, "Numeric value out of range")
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or abs(value) > _SANITIZE_MAX_NUM:
            raise RpcError(INVALID_PARAMS, "Numeric value out of range")
        return value
    if isinstance(value, list):
        if len(value) > _SANITIZE_MAX_ITEMS:
            raise RpcError(INVALID_PARAMS, "List payload too large")
        return [_sanitize_value(v, depth + 1) for v in value]
    if isinstance(value, dict):
        if len(value) > _SANITIZE_MAX_KEYS:
            raise RpcError(INVALID_PARAMS, "Object payload too large")
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise RpcError(INVALID_PARAMS, "Object keys must be strings")
            out[_sanitize_str(k, _SANITIZE_MAX_KEY)] = _sanitize_value(v, depth + 1)
        return out
    raise RpcError(INVALID_PARAMS, f"Unsupported value type: {type(value).__name__}")


def _sanitize_submit_data(data: Any) -> Dict[str, Any]:
    """Validate a submit payload (must be an object) and sanitise it in depth."""
    if not isinstance(data, dict):
        raise RpcError(INVALID_PARAMS, "Submit payload must be an object")
    return _sanitize_value(data)


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
@dispatcher.method("core.botinfo")
async def core_botinfo(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    bot = gateway.bot
    user = bot.user
    latency_ms = round(bot.latency * 1000) if bot.latency else None
    return {
        "name": user.name if user else None,
        "id": str(user.id) if user else None,
        "avatar": str(user.display_avatar.url) if user else None,
        "guild_count": len(bot.guilds),
        "latency_ms": latency_ms,
        "is_owner": await bot.is_owner(ctx.user),
    }


@dispatcher.method("core.guilds")
async def core_guilds(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Guilds in which the user has permissions (with the highest level per guild)."""
    ctx = await _build_context(gateway, params)
    bot = gateway.bot
    is_owner = await bot.is_owner(ctx.user)  # compute once
    result = []
    for guild in bot.guilds:
        if guild.get_member(ctx.user.id) is None and not is_owner:
            continue
        level = await resolve_level(bot, ctx.user, guild)
        if level < 1:  # less than guild_member
            continue
        result.append({
            "id": str(guild.id),
            "name": guild.name,
            "icon": str(guild.icon.url) if guild.icon else None,
            "member_count": guild.member_count,
            "level": int(level),
        })
    return {"guilds": result}


@dispatcher.method("core.guild_detail")
async def core_guild_detail(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Detailed overview of a guild (members, channels, roles, status, data)."""
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_member")
    g = ctx.guild

    online = idle = dnd = offline = 0
    try:
        for m in g.members:
            s = str(getattr(m, "status", "offline"))
            if s == "online":
                online += 1
            elif s == "idle":
                idle += 1
            elif s == "dnd":
                dnd += 1
            else:
                offline += 1
    except Exception:
        pass

    owner = g.owner
    me = g.me
    return {
        "id": str(g.id),
        "name": g.name,
        "icon": str(g.icon.url) if g.icon else None,
        "owner": (owner.display_name if owner else None),
        "member_count": g.member_count,
        "channels": {
            "text": len(g.text_channels),
            "voice": len(g.voice_channels),
            "categories": len(g.categories),
            "total": len(g.text_channels) + len(g.voice_channels),
        },
        "roles": len(g.roles),
        "presence": {"online": online, "idle": idle, "dnd": dnd, "offline": offline},
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "joined_at": me.joined_at.isoformat() if me and me.joined_at else None,
    }


# --------------------------------------------------------------------------- #
# Public command overview (NO login required – only active commands)
# --------------------------------------------------------------------------- #
async def _repo_map(bot: Any) -> Dict[str, str]:
    """Package name (lowercase) → repo name, from Red's Downloader. Empty dict if n/a."""
    out: Dict[str, str] = {}
    dl = bot.get_cog("Downloader")
    if dl is None:
        return out
    try:
        installed = await dl.installed_cogs()
    except Exception:
        installed = []
    for m in installed or []:
        try:
            name = getattr(m, "name", None)
            # Depending on the Red version: repo_name (str) OR repo.name (Repo object).
            repo = getattr(m, "repo_name", None) or getattr(getattr(m, "repo", None), "name", None)
            if name and repo:
                out[str(name).lower()] = str(repo)
        except Exception:
            continue
    return out


def _repo_for_cog(cog_obj: Any, repo_map: Dict[str, str]) -> Optional[str]:
    """Repo name for a cog instance (via its Python package)."""
    if cog_obj is None:
        return None
    try:
        pkg = str(type(cog_obj).__module__).split(".")[0].lower()
        return repo_map.get(pkg)
    except Exception:
        return None


# Cache for Discord's registered (synced) application commands. fetch_commands()
# is an HTTP call, so the result is cached briefly. Used to surface "ghost"
# commands still registered with Discord but not backed by any loaded cog.
_registered_cmd_cache: Dict[str, Any] = {"t": 0.0, "cmds": None}


async def _fetch_registered_commands(bot: Any) -> list:
    """Global application commands as registered with Discord (cached ~60s)."""
    import time

    now = time.monotonic()
    if _registered_cmd_cache["cmds"] is not None and now - _registered_cmd_cache["t"] < 60:
        return _registered_cmd_cache["cmds"]
    tree = getattr(bot, "tree", None)
    if tree is None:
        return []
    try:
        cmds = list(await tree.fetch_commands())
    except Exception:
        log.debug("fetch_commands failed", exc_info=True)
        return _registered_cmd_cache["cmds"] or []
    _registered_cmd_cache["t"] = now
    _registered_cmd_cache["cmds"] = cmds
    return cmds


def _i18n_desc(value: Any, locale: Any) -> Optional[str]:
    """Resolve a bilingual command description from ``command.extras['i18n_desc']``.

    ``value`` may be a ``{"de-DE": "...", "en-US": "..."}`` dict. Returns the entry
    matching ``locale`` (exact, then by language prefix), else None so the caller
    can fall back to the plain Discord description/short_doc.
    """
    if not isinstance(value, dict):
        return None
    loc = str(locale or "en-US")
    if loc in value:
        return value[loc]
    lang = loc.split("-")[0].lower()
    for k, v in value.items():
        if str(k).split("-")[0].lower() == lang:
            return v
    # Default fallback is ALWAYS English (never the first dict entry, which
    # would make German the implicit default for unknown locales).
    if "en-US" in value:
        return value["en-US"]
    for k, v in value.items():
        if str(k).split("-")[0].lower() == "en":
            return v
    return next(iter(value.values()), None)


def _category_from_name(name: str) -> str:
    """Name-only heuristic fallback when no privilege level is available."""
    n = (name or "").lower()
    first = n.split(" ")[0]
    if first.endswith("set") or n.endswith("set") or any(k in n for k in ("config", "setup", "settings")):
        return "Setup"
    if any(k in n for k in (
        "ban", "kick", "timeout", "mute", "purge", "warn", "clean", "clear",
        "lock", "slowmode", "move", "copy-role", "copy-channelrole", "modlog",
    )):
        return "Moderator"
    return "User"


# Discord permissions that imply a server-management (admin) command.
_ADMIN_PERMS = ("administrator", "manage_guild")
# Discord permissions that imply a moderator-level command.
_MOD_PERMS = (
    "manage_roles", "manage_channels", "manage_messages", "kick_members",
    "ban_members", "manage_nicknames", "moderate_members", "mute_members",
    "deafen_members", "move_members", "manage_threads", "manage_webhooks",
)


def _category_from_perms(cmd) -> Optional[str]:
    """Categorise from the Discord permissions a command's checks require.

    Reads ``cmd.requires.user_perms`` (a ``discord.Permissions`` set by checks like
    ``has_permissions``/``mod_or_permissions``). Returns Admin/Moderator or None.
    """
    try:
        perms = getattr(getattr(cmd, "requires", None), "user_perms", None)
        if perms is None:
            return None
        if any(getattr(perms, p, False) for p in _ADMIN_PERMS):
            return "Admin"
        if any(getattr(perms, p, False) for p in _MOD_PERMS):
            return "Moderator"
    except Exception:
        pass
    return None


def _command_category(cmd) -> str:
    """Categorise a Red command into Admin / Moderator / Setup / User.

    Signals, in order: Red's privilege level, the Discord permissions required by
    the command's checks, then a name heuristic. ``Setup`` is the configuration
    subset of admin-level commands (names like ``*set``, ``config``, ``setup``).
    """
    name = getattr(cmd, "qualified_name", "") or ""
    setupish = _category_from_name(name) == "Setup"
    try:
        from redbot.core.commands import PrivilegeLevel as _PL  # type: ignore
        pl = getattr(getattr(cmd, "requires", None), "privilege_level", None)
        if pl is not None:
            # Bot-owner commands (e.g. @commands.is_owner()) must never be
            # advertised as Admin — only the bot owner can run them.
            if pl >= _PL.BOT_OWNER:
                return "Owner"
            if pl >= _PL.ADMIN:
                return "Setup" if setupish else "Admin"
            if pl >= _PL.MOD:
                return "Moderator"
    except Exception:
        pass
    by_perms = _category_from_perms(cmd)
    if by_perms == "Admin":
        return "Setup" if setupish else "Admin"
    if by_perms == "Moderator":
        return "Moderator"
    return _category_from_name(name)


@dispatcher.method("core.commands")
async def core_commands(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """List of active text and slash commands. Public (without user context).

    Only visible, enabled commands are returned (no hidden ones). Descriptions
    follow the dashboard language via ``params['locale']`` when a command provides
    a bilingual ``extras['i18n_desc']``; otherwise the plain description is used.
    """
    bot = gateway.bot
    repo_map = await _repo_map(bot)
    locale = params.get("locale") or "en-US"
    include_orphans = bool(params.get("include_orphans"))

    # qualified_name -> bilingual i18n_desc dict, collected from the text/hybrid
    # side so the slash list can reuse it (hybrid app-commands don't reliably carry
    # the parent's extras).
    i18n_by_name: Dict[str, Any] = {}
    category_by_name: Dict[str, str] = {}

    prefix: list = []
    try:
        for c in bot.walk_commands():
            if getattr(c, "hidden", False) or not getattr(c, "enabled", True):
                continue
            bi = (getattr(c, "extras", None) or {}).get("i18n_desc")
            if isinstance(bi, dict):
                i18n_by_name[c.qualified_name] = bi
            desc = _i18n_desc(bi, locale)
            if desc is None:
                desc = (getattr(c, "short_doc", "") or "").strip()
            cat = _command_category(c)
            category_by_name[c.qualified_name] = cat
            prefix.append({
                "name": c.qualified_name,
                "description": desc,
                "cog": c.cog_name or "—",
                "repo": _repo_for_cog(getattr(c, "cog", None), repo_map),
                "category": cat,
            })
    except Exception:
        log.exception("Fehler beim Sammeln der Text-Commands")

    # Discord-registered (synced) chat-input command names, for the "synced" flag.
    registered_names = set()
    try:
        for ac in await _fetch_registered_commands(bot):
            if getattr(getattr(ac, "type", None), "value", 1) == 1:
                registered_names.add(ac.name)
    except Exception:
        pass

    def _synced(name) -> bool:
        return str(name).split(" ")[0] in registered_names

    slash: list = []
    slash_names: set = set()
    try:
        from discord import app_commands  # local, to avoid a hard import dependency

        def _add_slash(c, binding, cog_name=None):
            name = getattr(c, "qualified_name", None) or getattr(c, "name", None)
            if not name or name in slash_names:
                return
            slash_names.add(name)
            bi = (getattr(c, "extras", None) or {}).get("i18n_desc") or i18n_by_name.get(name)
            desc = _i18n_desc(bi, locale)
            if desc is None:
                desc = (getattr(c, "description", "") or "").strip()
            if cog_name is None:
                cog_name = type(binding).__name__ if binding is not None else "—"
            slash.append({
                "name": name,
                "description": desc,
                "cog": cog_name,
                "repo": _repo_for_cog(binding, repo_map),
                "synced": _synced(name),
                "category": category_by_name.get(name) or _category_from_name(name),
            })

        # 1) Commands actually in the tree.
        tree = getattr(bot, "tree", None)
        if tree is not None:
            for c in tree.walk_commands():
                if isinstance(c, app_commands.Command):
                    _add_slash(c, getattr(c, "binding", None))

        # 2) Enabled app/hybrid commands that Red keeps OUTSIDE the tree until a sync
        #    (e.g. a freshly enabled /ban). Read Red's enabled config and pull the
        #    matching app command off each cog (hybrids store it on the text command).
        enabled_top: set = set()
        try:
            res = bot.list_enabled_app_commands()
            if hasattr(res, "__await__"):
                res = await res
            if isinstance(res, dict):
                enabled_top = set((res.get("slash") or {}).keys())
        except Exception:
            enabled_top = set()
        if enabled_top:
            for cog_name, cog in list(bot.cogs.items()):
                try:
                    cands = []
                    if hasattr(cog, "get_app_commands"):
                        cands += [a for a in cog.get_app_commands() if isinstance(a, app_commands.Command)]
                    for tc in (cog.get_commands() if hasattr(cog, "get_commands") else []):
                        ac = getattr(tc, "app_command", None)
                        if ac is not None:
                            cands.append(ac)
                except Exception:
                    continue
                for ac in cands:
                    name = getattr(ac, "qualified_name", None) or getattr(ac, "name", None)
                    if not name or name in slash_names:
                        continue
                    if str(name).split(" ")[0] not in enabled_top:
                        continue
                    _add_slash(ac, cog, cog_name)
    except Exception:
        log.exception("Fehler beim Sammeln der Slash-Commands")

    # unique, sorted output
    seen_p, uniq_p = set(), []
    for c in sorted(prefix, key=lambda x: x["name"]):
        if c["name"] in seen_p:
            continue
        seen_p.add(c["name"]); uniq_p.append(c)
    seen_s, uniq_s = set(), []
    for c in sorted(slash, key=lambda x: x["name"]):
        if c["name"] in seen_s:
            continue
        seen_s.add(c["name"]); uniq_s.append(c)

    counts = {"prefix": len(uniq_p), "slash": len(uniq_s)}

    # Discord registrations not backed by any loaded cog -> "(Not existent)" ghosts.
    if include_orphans:
        try:
            live_top = {str(c["name"]).split(" ")[0] for c in uniq_s}
            label = "(Nicht existierend)" if str(locale).lower().startswith("de") else "(Not existent)"
            for ac in await _fetch_registered_commands(bot):
                if getattr(getattr(ac, "type", None), "value", 1) != 1:
                    continue  # chat-input (slash) commands only
                if ac.name in live_top:
                    continue
                uniq_s.append({
                    "name": ac.name,
                    "description": (getattr(ac, "description", "") or "").strip(),
                    "cog": label,
                    "repo": None,
                    "orphan": True,
                })
        except Exception:
            log.exception("Fehler beim Abgleich der Discord-Registrierungen")

    return {
        "bot": {"name": bot.user.name if bot.user else None,
                "avatar": str(bot.user.display_avatar.url) if bot.user else None},
        "prefix": uniq_p,
        "slash": uniq_s,
        "counts": counts,
    }


@dispatcher.method("core.stats")
async def core_stats(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Public bot statistics for the landing/overview (without login)."""
    bot = gateway.bot
    cog = _dashboard_cog(gateway)
    ui = (await cog.config.ui()) if cog else {}

    owner = None
    try:
        oid = next(iter(getattr(bot, "owner_ids", []) or []), None)
        if oid:
            u = bot.get_user(oid)
            owner = u.name if u else None
    except Exception:
        owner = None

    uptime_s = None
    up = getattr(bot, "uptime", None)
    if up is not None:
        try:
            now = datetime.now(up.tzinfo) if getattr(up, "tzinfo", None) else datetime.utcnow()
            uptime_s = int((now - up).total_seconds())
        except Exception:
            uptime_s = None

    return {
        "name": bot.user.name if bot.user else None,
        "avatar": str(bot.user.display_avatar.url) if bot.user else None,
        "owner": owner,
        "description": ui.get("description") or "",
        "guild_count": len(bot.guilds),
        "user_count": len(bot.users),
        "uptime_s": uptime_s,
        "latency_ms": round(bot.latency * 1000) if bot.latency else None,
    }


# --------------------------------------------------------------------------- #
# Manifest & contributions (widgets / panels / pages)
# --------------------------------------------------------------------------- #
@dispatcher.method("manifest.get")
async def manifest_get(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Returns all contributions the user is allowed to see (filtered by permissions)."""
    ctx = await _build_context(gateway, params)
    # Resolve the permission level only ONCE and then compare (instead of an
    # expensive resolution per contribution – saves many config reads with many cogs).
    level = await resolve_level(gateway.bot, ctx.user, ctx.guild)
    # SECURITY: In a guild context, only members may see contributions of that
    # guild at all (otherwise an info leak of other servers for logged-in non-members).
    if ctx.guild is not None and level < int(Level.GUILD_MEMBER):
        return {"contributions": []}
    locale = getattr(ctx, "locale", None)
    visible = [
        contrib.manifest(locale)
        for contrib in gateway.registry.all()
        if level >= _level_value(contrib.meta.permission)
    ]
    return {"contributions": visible}


@dispatcher.method("widget.data")
async def widget_data(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    key = (params.get("args") or {}).get("key")
    contrib = gateway.registry.get(key)
    if contrib is None or contrib.kind != "widget":
        raise RpcError(INVALID_PARAMS, "Unbekanntes Widget")
    await _require(gateway, ctx, contrib.meta.permission)
    data = await contrib.handler(ctx)
    return {"data": data.to_dict(getattr(ctx, "locale", None)) if hasattr(data, "to_dict") else data}


@dispatcher.method("panel.schema")
async def panel_schema(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    key = (params.get("args") or {}).get("key")
    contrib = gateway.registry.get(key)
    if contrib is None or contrib.kind != "panel":
        raise RpcError(INVALID_PARAMS, "Unbekanntes Panel")
    await _require(gateway, ctx, contrib.meta.permission)
    schema = await contrib.handler(ctx)
    return {"schema": schema.to_dict(getattr(ctx, "locale", None)) if hasattr(schema, "to_dict") else schema}


@dispatcher.method("panel.submit")
async def panel_submit(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    args = params.get("args") or {}
    key = args.get("key")
    data = args.get("data") or {}
    contrib = gateway.registry.get(key)
    if contrib is None or contrib.kind != "panel":
        raise RpcError(INVALID_PARAMS, "Unbekanntes Panel")
    await _require(gateway, ctx, contrib.meta.permission)
    if contrib.submit is None:
        raise RpcError(INVALID_PARAMS, "Panel ist schreibgeschützt (kein on_submit)")
    # Central input validation: every integrated cog gets sanitised data.
    data = _sanitize_submit_data(data)
    result = await contrib.submit(ctx, data)
    gateway.audit("panel.submit", ctx, {"key": key})
    return {"result": result.to_dict(getattr(ctx, "locale", None)) if hasattr(result, "to_dict") else result}


@dispatcher.method("page.schema")
async def page_schema(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    key = (params.get("args") or {}).get("key")
    contrib = gateway.registry.get(key)
    if contrib is None or contrib.kind != "page":
        raise RpcError(INVALID_PARAMS, "Unbekannte Seite")
    await _require(gateway, ctx, contrib.meta.permission)
    schema = await contrib.handler(ctx)
    return {"schema": schema.to_dict(getattr(ctx, "locale", None)) if hasattr(schema, "to_dict") else schema}


# --------------------------------------------------------------------------- #
# Cog management (bot owner only)
# --------------------------------------------------------------------------- #
@dispatcher.method("list.rows")
async def list_rows(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    key = (params.get("args") or {}).get("key")
    contrib = gateway.registry.get(key)
    if contrib is None or contrib.kind != "list":
        raise RpcError(INVALID_PARAMS, "Unbekannte Liste")
    await _require(gateway, ctx, contrib.meta.permission)
    rows = await contrib.handler(ctx)
    return {"rows": rows, "columns": contrib.meta.extra.get("columns", [])}


@dispatcher.method("list.delete")
async def list_delete(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    args = params.get("args") or {}
    key = args.get("key")
    item_id = args.get("id")
    contrib = gateway.registry.get(key)
    if contrib is None or contrib.kind != "list":
        raise RpcError(INVALID_PARAMS, "Unbekannte Liste")
    await _require(gateway, ctx, contrib.meta.permission)
    if contrib.delete is None:
        raise RpcError(INVALID_PARAMS, "Liste ist schreibgeschützt (kein on_delete)")
    result = await contrib.delete(ctx, item_id)
    gateway.audit("list.delete", ctx, {"key": key, "id": item_id})
    return {"result": result.to_dict(getattr(ctx, "locale", None)) if hasattr(result, "to_dict") else result}


@dispatcher.method("list.edit_form")
async def list_edit_form(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    args = params.get("args") or {}
    key = args.get("key")
    item_id = args.get("id")
    contrib = gateway.registry.get(key)
    if contrib is None or contrib.kind != "list":
        raise RpcError(INVALID_PARAMS, "Unbekannte Liste")
    await _require(gateway, ctx, contrib.meta.permission)
    if contrib.edit_form is None:
        raise RpcError(INVALID_PARAMS, "Liste ist nicht bearbeitbar (kein edit_form)")
    schema = await contrib.edit_form(ctx, item_id)
    return {"schema": schema.to_dict(getattr(ctx, "locale", None)) if hasattr(schema, "to_dict") else schema}


@dispatcher.method("list.edit")
async def list_edit(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    args = params.get("args") or {}
    key = args.get("key")
    item_id = args.get("id")
    data = args.get("data") or {}
    contrib = gateway.registry.get(key)
    if contrib is None or contrib.kind != "list":
        raise RpcError(INVALID_PARAMS, "Unbekannte Liste")
    await _require(gateway, ctx, contrib.meta.permission)
    if contrib.edit is None:
        raise RpcError(INVALID_PARAMS, "Liste ist nicht bearbeitbar (kein on_edit)")
    # Central input validation: every integrated cog gets sanitised data.
    data = _sanitize_submit_data(data)
    result = await contrib.edit(ctx, item_id, data)
    gateway.audit("list.edit", ctx, {"key": key, "id": item_id})
    return {"result": result.to_dict(getattr(ctx, "locale", None)) if hasattr(result, "to_dict") else result}


@dispatcher.method("cogs.list")
async def cogs_list(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """All installed cogs with load state (owner)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    bot = gateway.bot
    loaded = set(bot.extensions.keys())  # package names (lowercase)
    try:
        available = set(await bot._cog_mgr.available_modules())
    except Exception:
        available = set(loaded)
    contributing = {c.cog_name.lower() for c in gateway.registry.all()}
    repo_map = await _repo_map(bot)  # package name (lowercase) -> repo name
    names = sorted(available | loaded)
    cogs = [
        {
            "name": name,
            "loaded": name in loaded,
            "has_dashboard": name.lower() in contributing,
            "repo": repo_map.get(name.lower()),
        }
        for name in names
    ]
    return {"cogs": cogs, "loaded_count": len(loaded), "total": len(names)}


def _loaded_pkg_name(bot: Any, name: str) -> Optional[str]:
    """Actual key in bot.extensions matching `name` case-insensitively (or None).

    Cog package names are NOT always lowercase (e.g. ``WarcraftlogsClassic``,
    ``AdminUtils``). Lowercasing breaks reload/unload for those, so we match the
    real, case-correct extension key instead.
    """
    if name in bot.extensions:
        return name
    low = name.lower()
    for ext in bot.extensions:
        if ext.lower() == low:
            return ext
    return None


async def _resolve_cog_name(bot: Any, name: str) -> str:
    """Canonical (case-correct) cog name. Tries loaded extensions first, then the
    cog manager's available modules. Falls back to the given name."""
    loaded = _loaded_pkg_name(bot, name)
    if loaded:
        return loaded
    low = name.lower()
    try:
        avail = await bot._cog_mgr.available_modules()
        for mod in avail:
            if str(mod).lower() == low:
                return str(mod)
    except Exception:
        pass
    return name


def _purge_pkg_modules(pkg: str) -> None:
    """Drop a package and all its submodules from ``sys.modules`` so the next
    import reads fresh files from disk.

    discord.py's ``unload_extension`` only removes the top-level extension
    module, leaving submodules such as ``neko.pdc_dashboard`` cached. A later
    ``from .pdc_dashboard import ...`` would then silently re-use the stale
    in-memory copy and ignore updated files on disk – which is exactly why web
    reloads/updates kept running old code while Red's native ``[p]reload``
    (which purges these) worked. Mirrors Red's ``_cleanup_and_refresh_modules``.
    """
    import sys
    import importlib

    for mod in [m for m in list(sys.modules) if m == pkg or m.startswith(pkg + ".")]:
        try:
            del sys.modules[mod]
        except KeyError:
            pass
    importlib.invalidate_caches()


@dispatcher.method("cogs.set")
async def cogs_set(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Load/unload/reload a cog (owner). action: load | unload | reload."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    args = params.get("args") or {}
    name = str(args.get("name", "")).strip()
    action = args.get("action")
    if not name or action not in ("load", "unload", "reload"):
        raise RpcError(INVALID_PARAMS, "name/action fehlt oder ungültig")
    bot = gateway.bot
    # Resolve the real, case-correct cog name (do NOT lowercase – breaks
    # mixed-case cogs like WarcraftlogsClassic / AdminUtils).
    name = await _resolve_cog_name(bot, name)

    # Determine our own package (which runs the gateway) – a self-reload
    # would terminate the gateway in the middle of the response.
    own_pkg = None
    try:
        dcog = bot.get_cog("pdc_webdashboard") or bot.get_cog("WebDashboard")
        if dcog is not None:
            own_pkg = str(type(dcog).__module__).split(".")[0].lower()
    except Exception:
        own_pkg = None

    async def _reload_pkg(pkg: str) -> None:
        # discord.py 2.x: load/unload/reload_extension are coroutines → await them!
        if pkg in bot.extensions:
            await bot.unload_extension(pkg)
        # Purge cached submodules so updated files on disk are actually re-read.
        _purge_pkg_modules(pkg)
        spec = await bot._cog_mgr.find_cog(pkg)
        if spec is None:
            raise RpcError(INVALID_PARAMS, f"Cog '{pkg}' nicht gefunden")
        await bot.load_extension(spec)
        async with bot._config.packages() as pkgs:
            if pkg not in pkgs:
                pkgs.append(pkg)

    # Self-reload of the dashboard cog: run deferred so this response still
    # goes out before the gateway restarts.
    if action == "reload" and own_pkg and name.lower() == own_pkg:
        import asyncio

        async def _deferred() -> None:
            try:
                await asyncio.sleep(1.0)
                await _reload_pkg(name)
            except Exception:
                logging.getLogger("red.pdc.pdc_webdashboard.gateway").exception(
                    "Self-Reload von %s fehlgeschlagen", name
                )

        asyncio.ensure_future(_deferred())
        gateway.audit("cogs.reload", ctx, {"name": name, "deferred": True})
        return {"ok": True, "name": name, "deferred": True,
                "hint": "Dashboard startet in ~1s neu – Seite danach neu laden."}

    try:
        if action == "load":
            # Purge stale cached submodules (e.g. from an earlier failed load).
            _purge_pkg_modules(name)
            spec = await bot._cog_mgr.find_cog(name)
            if spec is None:
                raise RpcError(INVALID_PARAMS, f"Cog '{name}' nicht gefunden")
            await bot.load_extension(spec)
            async with bot._config.packages() as pkgs:
                if name not in pkgs:
                    pkgs.append(name)
        elif action == "reload":
            await _reload_pkg(name)
        elif action == "unload":
            if name in bot.extensions:
                await bot.unload_extension(name)
            async with bot._config.packages() as pkgs:
                if name in pkgs:
                    pkgs.remove(name)
    except RpcError:
        raise
    except Exception as e:  # cleanly pass through Red/import errors
        raise RpcError(INTERNAL_ERROR, f"{action} fehlgeschlagen: {e}")

    gateway.audit(f"cogs.{action}", ctx, {"name": name})
    return {"ok": True, "name": name, "loaded": name in bot.extensions}


# --------------------------------------------------------------------------- #
# Slash/app command management (bot owner only)
# --------------------------------------------------------------------------- #
@dispatcher.method("slash.list")
async def slash_list(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Top-level app commands with cog and enabled status (owner)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    bot = gateway.bot

    items = []
    seen = set()

    try:
        from discord import app_commands  # type: ignore
        ContextMenu = app_commands.ContextMenu
    except Exception:
        app_commands = None  # type: ignore
        ContextMenu = ()  # isinstance(..., ()) is always False

    def _ctype(c) -> int:
        try:
            if ContextMenu and isinstance(c, ContextMenu):
                return int(getattr(c, "type").value)  # 2=user, 3=message
        except Exception:
            pass
        return 1  # chat_input / slash

    # ENABLED status: PRIMARILY from Red's enabled config (reflects enable/disable_app_command
    # IMMEDIATELY, even without sync). list_enabled_app_commands() is, depending on the
    # Red version, sync OR async → handle both. FALLBACK: tree membership (steady state).
    enabled_keys = set()
    used_cfg = False
    try:
        res = bot.list_enabled_app_commands()
        if hasattr(res, "__await__"):
            res = await res
        if isinstance(res, dict):
            for k, ctype in (("slash", 1), ("user", 2), ("message", 3)):
                for nm in (res.get(k) or {}).keys():
                    enabled_keys.add((nm, ctype))
            used_cfg = True
    except Exception:
        used_cfg = False

    tree_cmds = []
    try:
        tree_cmds = list(bot.tree.get_commands())
        if not used_cfg:
            # No config access → use the tree as enabled indicator (Red removes disabled
            # app commands from the tree).
            for c in tree_cmds:
                enabled_keys.add((getattr(c, "name", None), _ctype(c)))
    except Exception:
        pass

    # SYNCED status: name is actually registered with Discord (cached fetch). An
    # enabled-but-not-yet-synced command is flagged so the UI can show a hint.
    registered_names = set()
    try:
        for ac in await _fetch_registered_commands(bot):
            registered_names.add(getattr(ac, "name", None))
    except Exception:
        pass

    # Determine the cog for a tree command – binding first, otherwise via the module.
    def _cog_for(c) -> str:
        b = getattr(c, "binding", None)
        if b is not None:
            return type(b).__name__
        mod = getattr(c, "module", None) or getattr(getattr(c, "callback", None), "__module__", None)
        if mod:
            top = str(mod).split(".")[0]
            for cn, cg in bot.cogs.items():
                try:
                    if str(getattr(type(cg), "__module__", "")).split(".")[0] == top:
                        return cn
                except Exception:
                    continue
        return "—"

    def _add(c, cog_name):
        try:
            name = getattr(c, "name", None)
            if not name:
                return
            ctype = _ctype(c)
            key = (name, ctype)
            if key in seen:
                return
            seen.add(key)
            items.append({
                "name": name,
                "type": ctype,
                "cog": cog_name,
                "enabled": key in enabled_keys,
                "synced": name in registered_names,
            })
        except Exception:
            return

    # 1) All app commands DEFINED by cogs – including disabled ones (not in the tree).
    try:
        for cog_name, cog in list(bot.cogs.items()):
            try:
                cmds = []
                if hasattr(cog, "get_app_commands"):
                    cmds = list(cog.get_app_commands())
                elif hasattr(cog, "walk_app_commands"):
                    cmds = list(cog.walk_app_commands())
                cmds += list(getattr(cog, "__cog_context_menus__", []) or [])
                # Hybrid commands keep their app command on the TEXT command (not in
                # get_app_commands()) -> collect them so DISABLED hybrids (e.g. ban,
                # timeout) also show up and can be toggled.
                try:
                    for tc in cog.get_commands():
                        ac = getattr(tc, "app_command", None)
                        if ac is not None:
                            cmds.append(ac)
                except Exception:
                    pass
                for c in cmds:
                    _add(c, cog_name)
            except Exception:
                continue
    except Exception:
        pass

    # 2) Add tree commands (with module fallback for categorization).
    try:
        for c in tree_cmds:
            _add(c, _cog_for(c))
    except Exception:
        pass

    # 3) Discord registrations not backed by any loaded cog -> "(Not existent)"
    # ghosts, so they are visible here and can be cleared via a sync.
    try:
        locale = params.get("locale") or "en-US"
        ghost_label = "(Nicht existierend)" if str(locale).lower().startswith("de") else "(Not existent)"
        present = {it["name"] for it in items}
        for ac in await _fetch_registered_commands(bot):
            if getattr(getattr(ac, "type", None), "value", 1) != 1:
                continue  # chat-input only
            if ac.name in present:
                continue
            items.append({
                "name": ac.name,
                "type": 1,
                "cog": ghost_label,
                "enabled": True,
                "orphan": True,
                "synced": True,
            })
    except Exception:
        log.exception("Fehler beim Abgleich der Discord-Registrierungen (slash.list)")

    items.sort(key=lambda x: (bool(x.get("orphan")), x["cog"].lower(), x["name"]))
    return {"commands": items, "count": len(items), "managed": True}


@dispatcher.method("slash.sync")
async def slash_sync(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronize app commands with Discord (owner)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    try:
        synced = await gateway.bot.tree.sync()
        # Invalidate the registered-commands cache so "not synced" pills clear
        # immediately on the next read instead of after the cache TTL (~60 s).
        _registered_cmd_cache["t"] = 0.0
        _registered_cmd_cache["cmds"] = None
        gateway.audit("slash.sync", ctx, {"count": len(synced)})
        return {"ok": True, "count": len(synced)}
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Sync fehlgeschlagen: {e}")


@dispatcher.method("slash.set")
async def slash_set(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Enable/disable a single app command (owner). Sync afterwards."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    args = params.get("args") or {}
    name = str(args.get("name", "")).strip()
    ctype = int(args.get("type", 1) or 1)
    enabled = bool(args.get("enabled"))
    if not name:
        raise RpcError(INVALID_PARAMS, "name erforderlich")
    bot = gateway.bot
    try:
        from discord import AppCommandType

        t = AppCommandType(ctype)
        if enabled:
            await bot.enable_app_command(name, t)
        else:
            await bot.disable_app_command(name, t)
    except RpcError:
        raise
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Umschalten fehlgeschlagen: {e}")
    gateway.audit("slash.set", ctx, {"name": name, "type": ctype, "enabled": enabled})
    return {"ok": True, "name": name, "enabled": enabled}


@dispatcher.method("slash.set_cog")
async def slash_set_cog(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Enable/disable all top-level app commands of a cog (owner)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    args = params.get("args") or {}
    cog_name = str(args.get("cog", "")).strip()
    enabled = bool(args.get("enabled"))
    if not cog_name:
        raise RpcError(INVALID_PARAMS, "cog erforderlich")
    bot = gateway.bot
    changed = 0
    try:
        from discord import AppCommandType, app_commands

        for c in bot.tree.get_commands():
            binding = getattr(c, "binding", None)
            if binding is None or type(binding).__name__ != cog_name:
                continue
            ctype = int(c.type.value) if isinstance(c, app_commands.ContextMenu) else 1
            t = AppCommandType(ctype)
            try:
                if enabled:
                    await bot.enable_app_command(c.name, t)
                else:
                    await bot.disable_app_command(c.name, t)
                changed += 1
            except Exception:
                pass
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Cog-Umschalten fehlgeschlagen: {e}")
    gateway.audit("slash.set_cog", ctx, {"cog": cog_name, "enabled": enabled, "changed": changed})
    return {"ok": True, "cog": cog_name, "enabled": enabled, "changed": changed}


# --------------------------------------------------------------------------- #
# Downloader (repos/cogs) – uses Red's Downloader cog (bot owner only)
# --------------------------------------------------------------------------- #
def _downloader(gateway: Any):
    return gateway.bot.get_cog("Downloader")


def _iter_repos(dl):
    """Repos depending on the Red version: dict {name: Repo} OR tuple/list of Repo."""
    repos = dl._repo_manager.repos
    if isinstance(repos, dict):
        return list(repos.values())
    return list(repos)


async def _installed_cogs(dl):
    try:
        return list(await dl.installed_cogs())
    except Exception:
        return []


async def _cogs_with_updates(dl, installed):
    """Names of installed cogs for which an update is available (best effort).

    Uses Red's internal ``_available_updates`` (compares the installed commit with
    the current repo checkout – no network). On failure, returns an empty set.
    """
    try:
        result = await dl._available_updates(installed)
        cogs_to_update = result[0] if isinstance(result, (tuple, list)) else result
        return {getattr(c, "name", None) for c in (cogs_to_update or [])}
    except Exception:
        return set()


async def _update_all_repos(dl):
    """Updates all repos in a version-robust way and returns the names of changed repos."""
    before = {r.name: getattr(r, "commit", None) for r in _iter_repos(dl)}
    rm = dl._repo_manager
    # Newer Red versions: update_all_repos(); older ones: Repo.update() per repo.
    if hasattr(rm, "update_all_repos"):
        await rm.update_all_repos()
    else:
        for repo in _iter_repos(dl):
            try:
                await repo.update()
            except Exception:
                continue
    return [r.name for r in _iter_repos(dl) if before.get(r.name) != getattr(r, "commit", None)]


@dispatcher.method("downloader.repos")
async def downloader_repos(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Repos with installed and available cogs (owner)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    dl = _downloader(gateway)
    if dl is None:
        return {"available": False, "repos": []}
    # Take the Downloader lock so the listing is never read mid-install/-update
    # (which would yield a partial scan: missing cogs / wrong update flags).
    try:
        async with _downloader_lock:
            installed = await _installed_cogs(dl)
            # Update detection can shell out to git per repo and occasionally
            # stalls (network, locked index). It is a non-essential adornment, so
            # cap it: if it does not finish quickly we still return the repo list
            # (without update flags) instead of letting the RPC hit the 15s 504.
            try:
                update_names = await asyncio.wait_for(
                    _cogs_with_updates(dl, installed), timeout=8
                )
            except (asyncio.TimeoutError, Exception):
                update_names = set()
            by_repo: Dict[str, list] = {}
            for m in installed:
                by_repo.setdefault(getattr(m, "repo_name", "?"), []).append({
                    "name": m.name, "commit": getattr(m, "commit", None),
                    "update_available": m.name in update_names,
                })
            repos = []
            for repo in _iter_repos(dl):
                avail = [
                    {"name": inst.name, "description": (getattr(inst, "short", "") or "").strip()}
                    for inst in getattr(repo, "available_cogs", [])
                    if not getattr(inst, "hidden", False)
                ]
                repos.append({
                    "name": repo.name,
                    "url": getattr(repo, "url", None),
                    "branch": getattr(repo, "branch", None),
                    "commit": getattr(repo, "commit", None),
                    "installed": sorted(by_repo.get(repo.name, []), key=lambda x: x["name"]),
                    "available_cogs": sorted(avail, key=lambda x: x["name"]),
                })
            repos.sort(key=lambda r: r["name"])
        return {"available": True, "repos": repos}
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Repo-Liste fehlgeschlagen: {e}")


@dispatcher.method("downloader.repo_add")
async def downloader_repo_add(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    dl = _downloader(gateway)
    if dl is None:
        raise RpcError(INVALID_PARAMS, "Downloader-Cog ist nicht geladen")
    args = params.get("args") or {}
    name = str(args.get("name", "")).strip()
    url = str(args.get("url", "")).strip()
    branch = (args.get("branch") or None) or None
    if not name or not url:
        raise RpcError(INVALID_PARAMS, "name und url erforderlich")
    try:
        await dl._repo_manager.add_repo(url=url, name=name, branch=branch)
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Repo hinzufügen fehlgeschlagen: {e}")
    gateway.audit("downloader.repo_add", ctx, {"name": name, "url": url})
    return {"ok": True, "name": name}


@dispatcher.method("downloader.repo_remove")
async def downloader_repo_remove(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    dl = _downloader(gateway)
    if dl is None:
        raise RpcError(INVALID_PARAMS, "Downloader-Cog ist nicht geladen")
    name = str((params.get("args") or {}).get("name", "")).strip()
    if not name:
        raise RpcError(INVALID_PARAMS, "name erforderlich")
    # Safeguard: only remove if no cogs from this repo are still installed.
    installed = await _installed_cogs(dl)
    if any(getattr(m, "repo_name", None) == name for m in installed):
        raise RpcError(INVALID_PARAMS, "Repo hat noch installierte Cogs – diese zuerst deinstallieren.")
    try:
        await dl._repo_manager.delete_repo(name)
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Repo entfernen fehlgeschlagen: {e}")
    gateway.audit("downloader.repo_remove", ctx, {"name": name})
    return {"ok": True}


@dispatcher.method("downloader.update_check")
async def downloader_update_check(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Updates the repos (git fetch/pull) and reports what has changed."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    dl = _downloader(gateway)
    if dl is None:
        raise RpcError(INVALID_PARAMS, "Downloader-Cog ist nicht geladen")
    try:
        changed = await _update_all_repos(dl)
        # After the repo update: which installed cogs now have an update?
        installed = await _installed_cogs(dl)
        cogs_update = sorted(await _cogs_with_updates(dl, installed))
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Update-Check fehlgeschlagen: {e}")
    gateway.audit("downloader.update_check", ctx, {"changed": changed, "cogs": cogs_update})
    return {"ok": True, "updated_repos": changed, "cogs_with_updates": cogs_update}


@dispatcher.method("downloader.cog_update")
async def downloader_cog_update(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Bring an installed cog up to the latest state of the repo (owner)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    dl = _downloader(gateway)
    if dl is None:
        raise RpcError(INVALID_PARAMS, "Downloader-Cog ist nicht geladen")
    cog_name = str((params.get("args") or {}).get("cog", "")).strip()
    if not cog_name:
        raise RpcError(INVALID_PARAMS, "cog erforderlich")
    # A full bot.tree.sync() is slow + heavily rate-limited by Discord. Per-cog
    # syncing makes the UI hang. The client therefore updates+reloads per cog and
    # triggers ONE slash sync at the end (sync=True only on the final call / or via
    # the Slash tab). Default: no sync here.
    do_sync = bool((params.get("args") or {}).get("sync", False))
    # Serialise mutating Downloader work so rapid/parallel clicks queue (FIFO)
    # instead of racing the Downloader or running concurrent operations.
    async with _downloader_lock:
        return await _do_cog_update(gateway, dl, cog_name, ctx, do_sync)


async def _do_cog_update(
    gateway: Any, dl: Any, cog_name: str, ctx: Any, do_sync: bool = False
) -> Dict[str, Any]:
    bot = gateway.bot
    try:
        installed = await _installed_cogs(dl)
        target = next((m for m in installed if m.name == cog_name), None)
        if target is None:
            raise RpcError(INVALID_PARAMS, f"Cog '{cog_name}' ist nicht installiert")
        # IMPORTANT: Do NOT use the install path (_filter_incorrect_cogs_by_names) –
        # it rejects already-installed cogs ("already installed"). For an update,
        # reinstall the installable directly (overwrites the files).
        cog_obj = None
        try:
            res = await dl._available_updates(installed)
            updatable = res[0] if isinstance(res, (tuple, list)) else res
            cog_obj = next((c for c in (updatable or []) if getattr(c, "name", None) == cog_name), None)
        except Exception:
            cog_obj = None
        if cog_obj is None:
            # No detected update left → fetch directly from the (updated) repo checkout.
            repo = dl._repo_manager.get_repo(getattr(target, "repo_name", ""))
            if repo is None:
                raise RpcError(INVALID_PARAMS, "Zugehöriges Repo nicht gefunden")
            cog_obj = next(
                (c for c in getattr(repo, "available_cogs", []) if getattr(c, "name", None) == cog_name),
                None,
            )
        if cog_obj is None:
            raise RpcError(INVALID_PARAMS, f"Cog '{cog_name}' nicht im Repo gefunden")
        installed_cogs, failed = await dl._install_cogs([cog_obj])
        if hasattr(dl, "_save_to_installed"):
            await dl._save_to_installed(installed_cogs)
        if failed:
            raise RpcError(INTERNAL_ERROR, f"Update fehlgeschlagen: {cog_name}")
    except RpcError:
        raise
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Update fehlgeschlagen: {e}")

    # Automatically after the update: 1) reload the cog (if loaded), 2) sync slash.
    # Use the real, case-correct extension key (NOT cog_name.lower() – that breaks
    # mixed-case cogs like WarcraftlogsClassic).
    pkg = _loaded_pkg_name(bot, cog_name) or await _resolve_cog_name(bot, cog_name)
    own_pkg = None
    try:
        dcog = bot.get_cog("pdc_webdashboard") or bot.get_cog("WebDashboard")
        if dcog is not None:
            own_pkg = str(type(dcog).__module__).split(".")[0].lower()
    except Exception:
        own_pkg = None

    reloaded = False
    reload_error = None
    if pkg in bot.extensions and pkg.lower() != own_pkg:
        try:
            await bot.unload_extension(pkg)
            # Purge cached submodules so the freshly updated files are re-read.
            _purge_pkg_modules(pkg)
            spec = await bot._cog_mgr.find_cog(pkg)
            if spec is not None:
                await bot.load_extension(spec)
                reloaded = True
        except Exception as e:
            reload_error = str(e)

    synced = None
    if do_sync:
        try:
            synced = len(await bot.tree.sync())
        except Exception:
            synced = None

    gateway.audit("downloader.cog_update", ctx,
                  {"cog": cog_name, "reloaded": reloaded, "synced": synced})
    return {
        "ok": True,
        "cog": cog_name,
        "reloaded": reloaded,
        "reload_error": reload_error,
        "synced": synced,
        "self_skipped": pkg == own_pkg,
    }


@dispatcher.method("downloader.cog_install")
async def downloader_cog_install(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    dl = _downloader(gateway)
    if dl is None:
        raise RpcError(INVALID_PARAMS, "Downloader-Cog ist nicht geladen")
    args = params.get("args") or {}
    repo_name = str(args.get("repo", "")).strip()
    cog_name = str(args.get("cog", "")).strip()
    if not repo_name or not cog_name:
        raise RpcError(INVALID_PARAMS, "repo und cog erforderlich")
    async with _downloader_lock:  # serialise with other Downloader operations
        try:
            repo = dl._repo_manager.get_repo(repo_name)
            if repo is None:
                raise RpcError(INVALID_PARAMS, f"Repo '{repo_name}' nicht gefunden")
            cogs, message = await dl._filter_incorrect_cogs_by_names(repo, [cog_name])
            if not cogs:
                raise RpcError(INVALID_PARAMS, message or f"Cog '{cog_name}' nicht im Repo")
            installed_cogs, failed = await dl._install_cogs(cogs)
            if hasattr(dl, "_save_to_installed"):
                await dl._save_to_installed(installed_cogs)
            if failed:
                raise RpcError(INTERNAL_ERROR, f"Installation fehlgeschlagen: {cog_name}")
        except RpcError:
            raise
        except Exception as e:
            raise RpcError(INTERNAL_ERROR, f"Installation fehlgeschlagen: {e}")
    gateway.audit("downloader.cog_install", ctx, {"repo": repo_name, "cog": cog_name})
    return {"ok": True, "cog": cog_name, "hint": "Mit cogs.set/load aktivieren."}


@dispatcher.method("downloader.cog_uninstall")
async def downloader_cog_uninstall(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    dl = _downloader(gateway)
    if dl is None:
        raise RpcError(INVALID_PARAMS, "Downloader-Cog ist nicht geladen")
    cog_name = str((params.get("args") or {}).get("cog", "")).strip()
    if not cog_name:
        raise RpcError(INVALID_PARAMS, "cog erforderlich")
    async with _downloader_lock:  # serialise with other Downloader operations
        return await _do_cog_uninstall(gateway, dl, cog_name, ctx)


async def _do_cog_uninstall(gateway: Any, dl: Any, cog_name: str, ctx: Any) -> Dict[str, Any]:
    bot = gateway.bot
    # 1) Disable slash commands: unloading removes the app commands from the tree;
    #    a sync follows afterwards. Use the real, case-correct extension key.
    pkg = _loaded_pkg_name(bot, cog_name) or cog_name
    try:
        if pkg in bot.extensions:
            await bot.unload_extension(pkg)  # discord.py 2.x: coroutine!
        async with bot._config.packages() as pkgs:
            for p in [pkg, cog_name, cog_name.lower()]:
                if p in pkgs:
                    pkgs.remove(p)
    except Exception:
        pass
    # 2) Remove the installation record (critical part).
    try:
        installed = await _installed_cogs(dl)
        target = [m for m in installed if m.name == cog_name]
        if not target:
            raise RpcError(INVALID_PARAMS, f"'{cog_name}' is not installed")
        if hasattr(dl, "_remove_from_installed"):
            await dl._remove_from_installed(target)
    except RpcError:
        raise
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Uninstall failed: {e}")
    # Delete the cog files (best effort). In current Red, _delete_cog expects a PATH
    # (it calls target.exists()); passing m.name (a str) raised
    # "'str' object has no attribute 'exists'". Build the install path; fall back to
    # the module / name for other versions. A file-deletion failure must NOT fail the
    # uninstall — the cog is already removed from the installed list above.
    if hasattr(dl, "_delete_cog"):
        base = None
        try:
            if hasattr(dl, "cog_install_path"):
                base = dl.cog_install_path()
                # cog_install_path() is a coroutine in current Red versions -> await it.
                if hasattr(base, "__await__"):
                    base = await base
        except Exception:
            base = None
        for m in target:
            candidates = ([base / m.name] if base is not None else []) + [m, m.name]
            for arg in candidates:
                try:
                    await dl._delete_cog(arg)
                    break
                except Exception:
                    continue
    # 3) Deregister slash commands in the BACKGROUND. A full bot.tree.sync() is
    #    slow and heavily rate-limited by Discord; awaiting it here is what made
    #    the uninstall occasionally hit the 15s gateway timeout. Fire-and-forget
    #    so the RPC returns immediately — the sync still completes shortly after.
    async def _bg_sync() -> None:
        try:
            await bot.tree.sync()
        except Exception:
            pass

    asyncio.ensure_future(_bg_sync())
    gateway.audit("downloader.cog_uninstall", ctx, {"cog": cog_name})
    return {"ok": True, "cog": cog_name}


# --------------------------------------------------------------------------- #
# Bot settings (Red's prefixes/roles/nick/embeds) – global & per guild
# --------------------------------------------------------------------------- #
@dispatcher.method("settings.get")
async def settings_get(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    bot = gateway.bot
    scope = (params.get("args") or {}).get("scope", "guild")

    if scope == "global":
        await _require(gateway, ctx, "bot_owner")
        return {
            "scope": "global",
            "prefixes": list(await bot._config.prefix()),
            "embeds": await bot._config.embeds(),
            "fuzzy": await bot._config.fuzzy(),
        }

    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Guild-Kontext erforderlich")
    await _require(gateway, ctx, "guild_admin")
    g = ctx.guild
    me = g.me
    return {
        "scope": "guild",
        "guild_id": str(g.id),
        "global_prefixes": list(await bot._config.prefix()),
        "guild_prefixes": list(await bot._config.guild(g).prefix()),
        "nickname": (me.nick if me else None),
        "admin_roles": [str(r) for r in await bot._config.guild(g).admin_role()],
        "mod_roles": [str(r) for r in await bot._config.guild(g).mod_role()],
        "embeds": await bot._config.guild(g).embeds(),
        "roles": [
            {"id": str(r.id), "name": r.name}
            for r in sorted(g.roles, key=lambda r: r.position, reverse=True)
            if not r.is_default()
        ],
    }


@dispatcher.method("settings.set")
async def settings_set(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    bot = gateway.bot
    args = params.get("args") or {}
    scope = args.get("scope", "guild")
    field = args.get("field")
    value = args.get("value")

    try:
        if scope == "global":
            await _require(gateway, ctx, "bot_owner")
            if field == "prefixes":
                prefixes = [str(p) for p in (value or []) if str(p).strip()]
                if not prefixes:
                    raise RpcError(INVALID_PARAMS, "Mindestens ein globaler Prefix nötig")
                await bot.set_prefixes(prefixes, guild=None)
            elif field == "embeds":
                await bot._config.embeds.set(bool(value))
            elif field == "fuzzy":
                await bot._config.fuzzy.set(bool(value))
            else:
                raise RpcError(INVALID_PARAMS, f"Unbekanntes Feld '{field}'")
            gateway.audit("settings.set", ctx, {"scope": scope, "field": field})
            return {"ok": True}

        if ctx.guild is None:
            raise RpcError(INVALID_PARAMS, "Guild-Kontext erforderlich")
        await _require(gateway, ctx, "guild_admin")
        g = ctx.guild
        if field == "prefixes":
            await bot.set_prefixes([str(p) for p in (value or []) if str(p).strip()], guild=g)
        elif field == "nickname":
            await g.me.edit(nick=(str(value) or None) if value else None)
        elif field == "admin_roles":
            await bot._config.guild(g).admin_role.set([int(x) for x in (value or [])])
        elif field == "mod_roles":
            await bot._config.guild(g).mod_role.set([int(x) for x in (value or [])])
        elif field == "embeds":
            await bot._config.guild(g).embeds.set(None if value is None else bool(value))
        else:
            raise RpcError(INVALID_PARAMS, f"Unbekanntes Feld '{field}'")
    except RpcError:
        raise
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Speichern fehlgeschlagen: {e}")

    gateway.audit("settings.set", ctx, {"scope": scope, "field": field})
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Dashboard-Branding, Overview, Lock, Sessions, Custom Pages
# --------------------------------------------------------------------------- #
def _dashboard_cog(gateway: Any):
    return gateway.bot.get_cog("pdc_webdashboard") or gateway.bot.get_cog("WebDashboard")


@dispatcher.method("dashboard.branding")
async def dashboard_branding(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Public branding (title/icon/theme) – usable without login.

    Also exposes the bot avatar (used as favicon) and an invite URL. The invite
    URL is taken from ``ui.invite_url`` if set, otherwise auto-built from the bot's
    own client id (scope bot+applications.commands, Administrator).
    """
    cog = _dashboard_cog(gateway)
    ui = (await cog.config.ui()) if cog else {}
    locked = bool(await cog.config.locked()) if cog else False
    bot = gateway.bot
    bot_avatar = str(bot.user.display_avatar.url) if getattr(bot, "user", None) else None
    invite_url = (ui.get("invite_url") or "").strip() if isinstance(ui, dict) else ""
    if not invite_url and getattr(bot, "user", None):
        invite_url = (
            f"https://discord.com/oauth2/authorize?client_id={bot.user.id}"
            f"&scope=bot+applications.commands&permissions=8"
        )
    return {"ui": ui, "locked": locked, "bot_avatar": bot_avatar, "invite_url": invite_url}


@dispatcher.method("logs.list")
async def logs_list(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Recent in-memory bot log records for the dashboard Log-Viewer (owner only)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    from .logbuffer import level_value, snapshot
    args = params.get("args") or {}
    min_level = level_value(str(args.get("level", "") or ""))
    query = str(args.get("query", "") or "")
    try:
        limit = int(args.get("limit", 300) or 300)
    except Exception:
        limit = 300
    limit = max(1, min(limit, 1000))
    return {"logs": snapshot(min_level=min_level, query=query, limit=limit)}


@dispatcher.method("dashboard.overview")
async def dashboard_overview(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)  # authenticated is sufficient
    bot = gateway.bot
    cog = _dashboard_cog(gateway)

    bot_uptime = None
    up = getattr(bot, "uptime", None)
    if up is not None:
        try:
            now = datetime.now(up.tzinfo) if getattr(up, "tzinfo", None) else datetime.utcnow()
            bot_uptime = int((now - up).total_seconds())
        except Exception:
            bot_uptime = None

    gw_uptime = None
    if getattr(gateway, "started_at", None):
        gw_uptime = int(time.time() - gateway.started_at)

    return {
        "bot_name": bot.user.name if bot.user else None,
        "bot_avatar": str(bot.user.display_avatar.url) if bot.user else None,
        "guild_count": len(bot.guilds),
        "user_count": len(bot.users),
        "loaded_cogs": len(bot.cogs),
        "contributions": len(gateway.registry.all()),
        "bot_uptime_s": bot_uptime,
        "gateway_uptime_s": gw_uptime,
        "locked": bool(await cog.config.locked()) if cog else False,
        "is_owner": await bot.is_owner(ctx.user),
    }


@dispatcher.method("dashboard.settings_get")
async def dashboard_settings_get(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    cog = _dashboard_cog(gateway)
    return {"ui": await cog.config.ui(), "locked": await cog.config.locked()}


@dispatcher.method("dashboard.settings_set")
async def dashboard_settings_set(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    cog = _dashboard_cog(gateway)
    ui = dict(await cog.config.ui())
    incoming = (params.get("args") or {}).get("ui") or {}
    for k in ("title", "icon", "description", "support_url", "color", "theme"):
        if k in incoming:
            ui[k] = incoming[k]
    await cog.config.ui.set(ui)
    gateway.audit("dashboard.settings_set", ctx, {})
    return {"ok": True, "ui": ui}


# NOTE: a second, legacy ``dashboard.branding`` used to live here and returned a
# *flat* payload without the ``ui`` wrapper / bot_avatar / invite_url. Because
# rpc registration is last-wins, it silently shadowed the correct handler above
# and broke the site title (and favicon/invite) on the web UI. Removed.


@dispatcher.method("dashboard.lock")
async def dashboard_lock(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    cog = _dashboard_cog(gateway)
    value = bool((params.get("args") or {}).get("locked"))
    await cog.config.locked.set(value)
    gateway.audit("dashboard.lock", ctx, {"locked": value})
    return {"ok": True, "locked": value}


@dispatcher.method("dashboard.refresh_sessions")
async def dashboard_refresh_sessions(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    cog = _dashboard_cog(gateway)
    epoch = time.time()
    await cog.config.session_epoch.set(epoch)
    gateway.audit("dashboard.refresh_sessions", ctx, {})
    return {"ok": True, "epoch": epoch}


@dispatcher.method("dashboard.session_epoch")
async def dashboard_session_epoch(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Current session epoch (used by the BFF for invalidation)."""
    cog = _dashboard_cog(gateway)
    return {"epoch": float(await cog.config.session_epoch()) if cog else 0.0}


@dispatcher.method("system.info")
async def system_info(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Bot health/ops (bot owner only): uptime, latency, versions, memory, cogs."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    bot = gateway.bot
    import platform

    uptime_s = None
    try:
        up = getattr(bot, "uptime", None)
        if up is not None:
            now = datetime.now(up.tzinfo) if getattr(up, "tzinfo", None) else datetime.utcnow()
            uptime_s = max(0.0, (now - up).total_seconds())
    except Exception:
        uptime_s = None

    memory_mb = None
    try:
        import resource  # Linux/Unix
        # ru_maxrss: Linux = KB, macOS = bytes. We assume KB (Linux server).
        memory_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except Exception:
        memory_mb = None

    loaded = len(getattr(bot, "extensions", {}))
    available = loaded
    try:
        available = len(await bot._cog_mgr.available_modules())
    except Exception:
        pass

    discord_ver = None
    red_ver = None
    try:
        import discord as _d
        discord_ver = getattr(_d, "__version__", None)
    except Exception:
        pass
    try:
        from redbot import __version__ as _rv
        red_ver = str(_rv)
    except Exception:
        pass

    return {
        "uptime_s": uptime_s,
        "latency_ms": round(bot.latency * 1000) if bot.latency else None,
        "guild_count": len(bot.guilds),
        "user_count": len(bot.users),
        "cogs_loaded": loaded,
        "cogs_available": available,
        "contributions": len(gateway.registry.all()),
        "shard_count": getattr(bot, "shard_count", None) or 1,
        "python": platform.python_version(),
        "discord": discord_ver,
        "red": red_ver,
        "memory_mb": memory_mb,
        "gateway_host": gateway.host,
        "gateway_port": gateway.port,
    }


@dispatcher.method("audit.list")
async def audit_list(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Audit log (bot owner only): who changed what and when (newest first)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    cog = _dashboard_cog(gateway)
    logs = list(await cog.config.audit_log()) if cog else []
    bot = gateway.bot
    args = params.get("args") or {}
    limit = max(1, min(int(args.get("limit", 200) or 200), 1000))
    out = []
    for e in reversed(logs[-limit:]):  # newest first
        uid = e.get("user")
        gid = e.get("guild")
        uname = None
        if uid and str(uid).isdigit():
            u = bot.get_user(int(uid))
            uname = u.name if u else str(uid)
        gname = None
        if gid and str(gid).isdigit():
            g = bot.get_guild(int(gid))
            gname = g.name if g else str(gid)
        out.append({
            "action": e.get("action"),
            "user_id": uid,
            "user": uname,
            "guild_id": gid,
            "guild": gname,
            "detail": e.get("detail") or {},
            "time": e.get("time"),
        })
    return {"entries": out, "count": len(logs)}


@dispatcher.method("requests.list")
async def requests_list(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Recent RPC request-log entries for auditing (bot owner only).

    Only populated while request logging is enabled (``[p]pdcdashboard reqlog``).
    Newest entries first.
    """
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    args = params.get("args") or {}
    try:
        limit = int(args.get("limit", 200) or 200)
    except Exception:
        limit = 200
    limit = max(1, min(limit, 500))
    entries = list(getattr(gateway, "request_log", []) or [])
    return {
        "enabled": bool(getattr(gateway, "request_log_enabled", False)),
        "entries": list(reversed(entries[-limit:])),
        "count": len(entries),
    }


# ----- Custom Pages -------------------------------------------------------- #
@dispatcher.method("pages.list")
async def pages_list(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Public list of custom pages (without HTML, for navigation)."""
    cog = _dashboard_cog(gateway)
    pages = list(await cog.config.custom_pages()) if cog else []
    return {"pages": [{
        "slug": p["slug"],
        "title": p["title"],
        "nav": p.get("nav", True),
        "visibility": p.get("visibility", "public"),
    } for p in pages]}


@dispatcher.method("pages.get")
async def pages_get(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    cog = _dashboard_cog(gateway)
    slug = str((params.get("args") or {}).get("slug", ""))
    pages = list(await cog.config.custom_pages()) if cog else []
    for p in pages:
        if p["slug"] == slug:
            return {"page": p}
    raise RpcError(INVALID_PARAMS, "Seite nicht gefunden")


@dispatcher.method("pages.save")
async def pages_save(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    cog = _dashboard_cog(gateway)
    args = params.get("args") or {}
    slug = str(args.get("slug", "")).strip().lower().replace(" ", "-")
    if not slug:
        raise RpcError(INVALID_PARAMS, "slug erforderlich")
    visibility = "private" if str(args.get("visibility", "public")).lower() == "private" else "public"
    entry = {
        "slug": slug,
        "title": str(args.get("title", slug)),
        # Content is stored as Markdown; `html` is kept as a legacy fallback.
        "markdown": str(args.get("markdown", "")),
        "html": str(args.get("html", "")),
        "nav": bool(args.get("nav", True)),
        "visibility": visibility,
    }
    async with cog.config.custom_pages() as pages:
        for i, p in enumerate(pages):
            if p["slug"] == slug:
                pages[i] = entry
                break
        else:
            pages.append(entry)
    gateway.audit("pages.save", ctx, {"slug": slug})
    return {"ok": True, "slug": slug}


@dispatcher.method("pages.delete")
async def pages_delete(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    cog = _dashboard_cog(gateway)
    slug = str((params.get("args") or {}).get("slug", ""))
    async with cog.config.custom_pages() as pages:
        pages[:] = [p for p in pages if p["slug"] != slug]
    gateway.audit("pages.delete", ctx, {"slug": slug})
    return {"ok": True}


# ----- Server statistics (WebDashboardStats cog) --------------------------- #
def _serverstats(gateway: Any):
    bot = gateway.bot
    # Current name first, then the legacy name (web_serverstats was renamed).
    for cog_name in ("pdc_webdashboard_stats", "WebDashboardStats", "WebServerStats"):
        cog = bot.get_cog(cog_name)
        if cog is not None:
            return cog
    # Fallback: find the cog via class name or package module, in case the
    # qualified name differs.
    for c in bot.cogs.values():
        try:
            if type(c).__name__ in ("WebDashboardStats", "WebServerStats"):
                return c
            if str(getattr(type(c), "__module__", "")).split(".")[0] in ("pdc_webdashboard_stats", "web_serverstats"):
                return c
        except Exception:
            continue
    return None


async def _stats_call(gateway: Any, params: Dict[str, Any], method_name: str, *extra_keys):
    """Shared helper: build context + check permission + call the cog method."""
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_member")
    cog = _serverstats(gateway)
    if cog is None:
        raise RpcError(INVALID_PARAMS, "WebDashboardStats-Cog ist nicht geladen")
    args = params.get("args") or {}
    days = int(args.get("days", 30) or 30)
    fn = getattr(cog, method_name)
    call_args = [ctx.guild]
    for k in extra_keys:
        call_args.append(args.get(k))
    call_args.append(days)
    return await fn(*call_args)


@dispatcher.method("serverstats.overview")
async def serverstats_overview(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_call(gateway, params, "stats_overview")


@dispatcher.method("serverstats.messages")
async def serverstats_messages(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_call(gateway, params, "stats_messages")


@dispatcher.method("serverstats.voice")
async def serverstats_voice(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_call(gateway, params, "stats_voice")


@dispatcher.method("serverstats.status")
async def serverstats_status(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_call(gateway, params, "stats_status")


@dispatcher.method("serverstats.invites")
async def serverstats_invites(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_call(gateway, params, "stats_invites")


@dispatcher.method("serverstats.activity")
async def serverstats_activity(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_call(gateway, params, "stats_activity")


@dispatcher.method("serverstats.commands")
async def serverstats_commands(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_call(gateway, params, "stats_commands")


# ----- Announcements / embed builder (guild_admin) ------------------------- #
@dispatcher.method("announce.channels")
async def announce_channels(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_admin")
    chans = []
    for c in ctx.guild.text_channels:
        perms = c.permissions_for(ctx.guild.me) if ctx.guild.me else None
        chans.append({
            "id": str(c.id),
            "name": c.name,
            "can_send": bool(perms.send_messages) if perms else True,
        })
    return {"channels": chans}


@dispatcher.method("announce.send")
async def announce_send(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_admin")
    import discord

    args = params.get("args") or {}
    ch_id = args.get("channel_id")
    channel = ctx.guild.get_channel(int(ch_id)) if ch_id and str(ch_id).isdigit() else None
    if channel is None or not isinstance(channel, discord.TextChannel):
        raise RpcError(INVALID_PARAMS, "Kanal nicht gefunden")

    content = (str(args.get("content", "")).strip() or None)
    emb = args.get("embed") or {}
    embed = None
    if isinstance(emb, dict) and any(str(emb.get(k, "")).strip() for k in
                                     ("title", "description", "image_url", "footer", "author")):
        col = None
        raw = str(emb.get("color", "")).strip().lstrip("#")
        if raw:
            try:
                col = int(raw, 16)
            except Exception:
                col = None
        embed = discord.Embed(
            title=(str(emb.get("title")).strip() or None),
            description=(str(emb.get("description")).strip() or None),
            color=col if col is not None else None,
        )
        if str(emb.get("footer", "")).strip():
            embed.set_footer(text=str(emb["footer"]).strip()[:2048])
        if str(emb.get("author", "")).strip():
            embed.set_author(name=str(emb["author"]).strip()[:256])
        if str(emb.get("image_url", "")).strip():
            try:
                embed.set_image(url=str(emb["image_url"]).strip())
            except Exception:
                pass

    if not content and embed is None:
        raise RpcError(INVALID_PARAMS, "Nichts zu senden (Text oder Embed nötig)")
    try:
        msg = await channel.send(content=content, embed=embed)
    except discord.Forbidden:
        raise RpcError(FORBIDDEN, "Dem Bot fehlt die Berechtigung in diesem Kanal")
    except Exception as e:
        raise RpcError(INTERNAL_ERROR, f"Senden fehlgeschlagen: {e}")
    gateway.audit("announce.send", ctx, {"channel": str(ch_id)})
    return {"ok": True, "message_id": str(msg.id)}


@dispatcher.method("serverstats.member_drilldown")
async def serverstats_member_drilldown(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    args = params.get("args") or {}
    mid = args.get("member_id")
    member_id = int(mid) if mid and str(mid).isdigit() else 0
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_member")
    cog = _serverstats(gateway)
    if cog is None:
        raise RpcError(INVALID_PARAMS, "WebDashboardStats-Cog ist nicht geladen")
    return await cog.stats_member_drilldown(ctx.guild, member_id, int(args.get("days", 30) or 30))


@dispatcher.method("serverstats.channel_drilldown")
async def serverstats_channel_drilldown(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    args = params.get("args") or {}
    cid = args.get("channel_id")
    channel_id = int(cid) if cid and str(cid).isdigit() else 0
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_member")
    cog = _serverstats(gateway)
    if cog is None:
        raise RpcError(INVALID_PARAMS, "WebDashboardStats-Cog ist nicht geladen")
    return await cog.stats_channel_drilldown(ctx.guild, channel_id, int(args.get("days", 30) or 30))


async def _stats_guild_only(gateway: Any, params: Dict[str, Any], method_name: str) -> Dict[str, Any]:
    """Shared helper for serverstats methods that take only the guild (no days/extra)."""
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_member")
    cog = _serverstats(gateway)
    if cog is None:
        raise RpcError(INVALID_PARAMS, "WebDashboardStats-Cog ist nicht geladen")
    return await getattr(cog, method_name)(ctx.guild)


@dispatcher.method("serverstats.peaks")
async def serverstats_peaks(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_call(gateway, params, "stats_peaks")


@dispatcher.method("serverstats.heatmap")
async def serverstats_heatmap(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_member")
    cog = _serverstats(gateway)
    if cog is None:
        raise RpcError(INVALID_PARAMS, "WebDashboardStats-Cog ist nicht geladen")
    args = params.get("args") or {}
    metric = str(args.get("metric", "messages"))
    return await cog.stats_heatmap(ctx.guild, int(args.get("days", 30) or 30), metric)


@dispatcher.method("serverstats.now")
async def serverstats_now(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_guild_only(gateway, params, "stats_now")


@dispatcher.method("serverstats.leaderboard")
async def serverstats_leaderboard(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_member")
    cog = _serverstats(gateway)
    if cog is None:
        raise RpcError(INVALID_PARAMS, "WebDashboardStats-Cog ist nicht geladen")
    args = params.get("args") or {}
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 100))
    return await cog.stats_leaderboard(ctx.guild, limit=limit)


@dispatcher.method("serverstats.retention")
async def serverstats_retention(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    return await _stats_guild_only(gateway, params, "stats_retention")


@dispatcher.method("serverstats.export")
async def serverstats_export(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Export the collected statistics of a guild as JSON or CSV (guild admin).

    args: {"format": "json" | "csv", "days": int}
    Returns {"filename", "mimetype", "content"} — the web app offers it as a
    file download.
    """
    ctx = await _build_context(gateway, params)
    if ctx.guild is None:
        raise RpcError(INVALID_PARAMS, "Unbekannte Guild")
    await _require(gateway, ctx, "guild_admin")
    cog = _serverstats(gateway)
    if cog is None:
        raise RpcError(INVALID_PARAMS, "WebDashboardStats-Cog ist nicht geladen")
    if not hasattr(cog, "stats_export"):
        raise RpcError(INVALID_PARAMS, "Export is not supported by the loaded stats cog")
    args = params.get("args") or {}
    fmt = str(args.get("format", "json")).lower()
    days = int(args.get("days", 30) or 30)
    result = await cog.stats_export(ctx.guild, days=days, fmt=fmt)
    gateway.audit("serverstats.export", ctx, {"format": fmt, "days": days})
    return result


def _maybe_integration_base():
    from ..integration.base import DashboardIntegration
    return DashboardIntegration


def setup_core_methods(gateway: Any) -> Dispatcher:
    """Returns the prepared dispatcher (core methods already registered)."""
    return dispatcher


# --------------------------------------------------------------------------- #
# Background monitor (cog-update check + alerts) – config for the web UI
# --------------------------------------------------------------------------- #
def _dashboard_cog(gateway: Any):
    return gateway.bot.get_cog("pdc_webdashboard") or gateway.bot.get_cog("WebDashboard")


@dispatcher.method("monitor.get")
async def monitor_get(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Monitor config + last cog-update result (bot owner only)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    cog = _dashboard_cog(gateway)
    if cog is None:
        raise RpcError(INVALID_PARAMS, "Dashboard-Cog nicht geladen")
    return {"config": await cog.config.monitor(), "last": await cog.config.monitor_last()}


@dispatcher.method("monitor.set")
async def monitor_set(gateway: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Update monitor config (bot owner only)."""
    ctx = await _build_context(gateway, params)
    await _require(gateway, ctx, "bot_owner")
    cog = _dashboard_cog(gateway)
    if cog is None:
        raise RpcError(INVALID_PARAMS, "Dashboard-Cog nicht geladen")
    args = params.get("args") or {}
    cur = await cog.config.monitor()
    allowed_h = {0, 1, 2, 4, 8, 16, 24}
    if "cog_update_interval_h" in args:
        try:
            h = int(args["cog_update_interval_h"])
        except (TypeError, ValueError):
            h = 0
        cur["cog_update_interval_h"] = h if h in allowed_h else 0
    if "alerts_dm" in args:
        cur["alerts_dm"] = bool(args["alerts_dm"])
    if "mem_threshold_mb" in args:
        try:
            cur["mem_threshold_mb"] = max(0, int(args["mem_threshold_mb"]))
        except (TypeError, ValueError):
            cur["mem_threshold_mb"] = 0
    await cog.config.monitor.set(cur)
    gateway.audit("monitor.set", ctx, cur)
    return {"ok": True, "config": cur}
