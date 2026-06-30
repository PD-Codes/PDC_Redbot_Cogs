from .scheduledmsg import ScheduledMsg


async def setup(bot):
    await bot.add_cog(ScheduledMsg(bot))
