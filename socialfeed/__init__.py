from .socialfeed import SocialFeed


async def setup(bot):
    await bot.add_cog(SocialFeed(bot))
