"""Runtime context passed to every widget/panel/page handler."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import discord
    from redbot.core.bot import Red


@dataclass
class DashboardContext:
    """Safe context for a single dashboard call.

    Created by the gateway after the identity (Discord user) and the permissions
    have been validated server-side. Handlers may rely on access already being
    authorized.
    """

    bot: "Red"
    user: "discord.User"
    guild: Optional["discord.Guild"] = None
    member: Optional["discord.Member"] = None
    locale: str = "en-US"
    # raw request parameters provided by the BFF (already type-validated)
    params: Optional[dict] = None

    @property
    def is_guild_context(self) -> bool:
        return self.guild is not None
