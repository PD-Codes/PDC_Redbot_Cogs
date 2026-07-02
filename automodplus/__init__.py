from .automodplus import AutoModPlus


async def setup(bot):
    await bot.add_cog(AutoModPlus(bot))
