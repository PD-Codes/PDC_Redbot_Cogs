from .memegen import MemeGen


async def setup(bot):
    await bot.add_cog(MemeGen(bot))
