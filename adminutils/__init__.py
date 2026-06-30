from .adminutils import AdminUtils

async def setup(bot):
    await bot.add_cog(AdminUtils(bot))
