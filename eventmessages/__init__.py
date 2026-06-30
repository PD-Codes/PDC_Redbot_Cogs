from .eventmessages import EventMessages

async def setup(bot):
    await bot.add_cog(EventMessages(bot))
