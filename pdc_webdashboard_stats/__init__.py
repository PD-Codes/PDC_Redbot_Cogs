from .pdc_webdashboard_stats import WebDashboardStats


async def setup(bot):
    await bot.add_cog(WebDashboardStats(bot))
