from redbot.core.bot import Red

from .dashboardexample import DashboardExample


async def setup(bot: Red) -> None:
    await bot.add_cog(DashboardExample(bot))
