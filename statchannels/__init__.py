from .statchannels import StatChannels


async def setup(bot):
    await bot.add_cog(StatChannels(bot))
