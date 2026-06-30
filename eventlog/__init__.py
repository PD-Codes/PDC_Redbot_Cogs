from .eventlog import EventLog


async def setup(bot):
    await bot.add_cog(EventLog(bot))
