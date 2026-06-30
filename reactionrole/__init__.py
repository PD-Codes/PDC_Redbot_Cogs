from .reactionrole import ReactionRole

async def setup(bot):
    await bot.add_cog(ReactionRole(bot))
