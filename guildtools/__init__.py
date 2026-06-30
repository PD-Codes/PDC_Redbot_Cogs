from .guildtools import GuildTools
from .pollexport import GuildToolsPollExport
from .readytimes import ReadyTimes

async def setup(bot):
    await bot.add_cog(GuildTools(bot))
    await bot.add_cog(GuildToolsPollExport(bot))
    await bot.add_cog(ReadyTimes(bot))
