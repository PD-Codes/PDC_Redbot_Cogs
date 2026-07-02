from .livealerts import LiveAlerts


async def setup(bot):
    await bot.add_cog(LiveAlerts(bot))
