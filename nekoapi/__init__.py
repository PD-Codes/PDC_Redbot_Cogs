from .nekoapi import NekoAPI


async def setup(bot):
    await bot.add_cog(NekoAPI(bot))
