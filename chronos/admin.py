from django.contrib import admin
from .models import EngineSettings, MarketTick, StrikeExecution


@admin.register(EngineSettings)
class EngineSettingsAdmin(admin.ModelAdmin):
    list_display = ('feed_mode', 'capital_usd', 'arbitrage_threshold_pct', 'poll_interval_sec', 'auto_strike', 'updated_at')


@admin.register(MarketTick)
class MarketTickAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'spot_price', 'oracle_price', 'delta_pct', 'spot_source', 'feed_mode', 'strike_triggered')
    list_filter = ('feed_mode', 'spot_source', 'strike_triggered')
    ordering = ('-created_at',)


@admin.register(StrikeExecution)
class StrikeExecutionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'execution_mode', 'direction', 'delta_pct', 'capital_usd', 'net_profit_usd', 'succeeded')
    list_filter = ('execution_mode', 'direction', 'succeeded')
    ordering = ('-created_at',)
