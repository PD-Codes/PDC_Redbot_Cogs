from redbot.core.utils import get_end_user_data_statement

from .channeljoinnotification import ChannelJoinNotification

__red_end_user_data_statement__ = get_end_user_data_statement(__file__)


async def setup(bot):
    await bot.add_cog(ChannelJoinNotification(bot))

