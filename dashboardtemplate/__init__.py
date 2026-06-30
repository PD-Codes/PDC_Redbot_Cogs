from redbot.core.bot import Red

from .dashboardtemplate import DashboardTemplate


async def setup(bot: Red) -> None:
    await bot.add_cog(DashboardTemplate(bot))
