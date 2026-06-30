from .adminprotocol import AdminProtocol

async def setup(bot):
    await bot.add_cog(AdminProtocol(bot))
