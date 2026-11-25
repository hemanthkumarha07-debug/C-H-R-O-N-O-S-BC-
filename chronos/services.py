import logging
from typing import Any
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Avg, Count, Max, Sum
from django.utils import timezone

from .models import EngineSettings, MarketTick, StrikeExecution

logger = logging.getLogger('chronos')

MAX_STORED_TICKS = 500
AAVE_FLASH_LOAN_FEE_RATE = 0.0005  # 0.05% Aave V3 flashLoanSimple fee
DEX_SLIPPAGE_FEE_RATE = 0.00025    # 2.5 bps modeled DEX slippage / swap fee
BASE_GAS_OVERHEAD_USD = 45.0


def compute_profit_breakdown(capital_usd: float, delta_pct: float) -> dict[str, float]:
    """Calculate simulated gross spread, fees, gas estimate, and net arbitrage P&L."""
    gross = round(capital_usd * (abs(delta_pct) / 100.0), 2)
    flash_fee = round(capital_usd * AAVE_FLASH_LOAN_FEE_RATE, 2)
    gas_and_slippage = round(
        min(max(capital_usd * DEX_SLIPPAGE_FEE_RATE + BASE_GAS_OVERHEAD_USD, BASE_GAS_OVERHEAD_USD), 750.0),
        2,
    )
    net = round(gross - flash_fee - gas_and_slippage, 2)
    return {
        'gross_profit_usd': gross,
        'flash_fee_usd': flash_fee,
        'estimated_gas_usd': gas_and_slippage,
        'net_profit_usd': net,
    }


def get_aggregate_stats() -> dict[str, Any]:
    """Compute aggregate KPIs from persisted StrikeExecution and MarketTick rows."""
    strike_agg = StrikeExecution.objects.filter(succeeded=True).aggregate(
        total_profit=Sum('net_profit_usd'),
        strike_count=Count('id'),
        avg_strike_delta=Avg('delta_pct'),
    )
    tick_agg = MarketTick.objects.aggregate(
        tick_count=Count('id'),
        max_delta=Max('delta_pct'),
        avg_delta=Avg('delta_pct'),
    )
    return {
        'total_profit_usd': round(strike_agg['total_profit'] or 0.0, 2),
        'strike_count': strike_agg['strike_count'] or 0,
        'avg_strike_delta_pct': round(strike_agg['avg_strike_delta'] or 0.0, 4),
        'tick_count': tick_agg['tick_count'] or 0,
        'max_delta_pct': round(tick_agg['max_delta'] or 0.0, 4),
        'avg_delta_pct': round(tick_agg['avg_delta'] or 0.0, 4),
    }


def build_dashboard_snapshot(engine_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble initial dashboard state for HTTP hydration or WebSocket connect."""
    settings_obj = EngineSettings.get_active()
    recent_ticks = list(MarketTick.objects.all()[:40])
    recent_ticks.reverse()  # Oldest -> newest for time-series chart plotting
    recent_strikes = list(StrikeExecution.objects.all()[:25])

    return {
        'settings': settings_obj.to_dict(),
        'stats': get_aggregate_stats(),
        'ticks': [t.to_dict() for t in recent_ticks],
        'strikes': [s.to_dict() for s in recent_strikes],
        'engine': engine_status or {
            'is_running': False,
            'active_mode': settings_obj.feed_mode,
            'last_error': None,
            'last_tick_at': recent_ticks[-1].created_at.isoformat() if recent_ticks else None,
        },
    }


def broadcast_event(event_type: str, payload: dict[str, Any]) -> None:
    """Broadcast a structured JSON event to all connected dashboard WebSockets."""
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            'ticker',
            {
                'type': 'send_update',
                'data': {
                    'event': event_type,
                    **payload,
                },
            },
        )
    except Exception as exc:
        logger.warning('Failed to broadcast event %s: %s', event_type, exc)


def record_tick_and_maybe_strike(
    payload: dict[str, Any],
    *,
    engine_status: dict[str, Any] | None = None,
    broadcast: bool = True,
) -> dict[str, Any]:
    """Persist a MarketTick (and StrikeExecution if triggered) and broadcast to UI."""
    spot_price = float(payload['spot_price'])
    oracle_price = float(payload['oracle_price'])
    signed_delta = float(
        payload.get('signed_delta', ((spot_price - oracle_price) / oracle_price) * 100.0)
    )
    delta_pct = float(payload.get('delta', abs(signed_delta)))
    oracle_age = payload.get('oracle_age_sec')
    if oracle_age is not None:
        oracle_age = int(oracle_age)

    strike_flag = bool(payload.get('strike_flash', False))
    feed_mode = str(payload.get('feed_mode', EngineSettings.FEED_LIVE))[:16]
    spot_source = str(payload.get('spot_source', 'Binance'))[:32]
    status_text = str(payload.get('status', 'SCANNING'))[:64]
    log_text = str(payload.get('log', ''))

    tick = MarketTick.objects.create(
        spot_price=spot_price,
        oracle_price=oracle_price,
        delta_pct=delta_pct,
        signed_delta_pct=signed_delta,
        oracle_age_sec=oracle_age,
        spot_source=spot_source,
        feed_mode=feed_mode,
        status=status_text,
        log_message=log_text,
        strike_triggered=strike_flag,
    )

    # Keep SQLite footprint bounded to the newest MAX_STORED_TICKS rows
    excess_ids = list(
        MarketTick.objects.order_by('-created_at', '-id')
        .values_list('id', flat=True)[MAX_STORED_TICKS:]
    )
    if excess_ids:
        MarketTick.objects.filter(id__in=excess_ids).delete()

    strike_dict = None
    strike_data = payload.get('strike')
    if strike_data:
        strike = StrikeExecution.objects.create(
            execution_mode=str(strike_data.get('execution_mode', StrikeExecution.MODE_SIMULATED))[:16],
            direction=str(
                strike_data.get(
                    'direction',
                    StrikeExecution.DIRECTION_PREMIUM if signed_delta >= 0 else StrikeExecution.DIRECTION_DISCOUNT,
                )
            )[:32],
            spot_price=spot_price,
            oracle_price=oracle_price,
            delta_pct=delta_pct,
            capital_usd=float(strike_data['capital_usd']),
            gross_profit_usd=float(strike_data['gross_profit_usd']),
            flash_fee_usd=float(strike_data['flash_fee_usd']),
            estimated_gas_usd=float(strike_data['estimated_gas_usd']),
            net_profit_usd=float(strike_data['net_profit_usd']),
            succeeded=bool(strike_data.get('succeeded', True)),
            tx_hash=str(strike_data.get('tx_hash', ''))[:80],
            notes=str(strike_data.get('notes', '')),
        )
        strike_dict = strike.to_dict()

    stats = get_aggregate_stats()
    tick_dict = tick.to_dict()
    tick_dict['total_profit'] = stats['total_profit_usd']

    if broadcast:
        broadcast_event(
            'tick',
            {
                'tick': tick_dict,
                'strike': strike_dict,
                'stats': stats,
                'engine': engine_status or {
                    'is_running': True,
                    'active_mode': feed_mode,
                    'last_error': None,
                    'last_tick_at': timezone.now().isoformat(),
                },
                # Top-level convenience fields kept for backwards compatibility
                **tick_dict,
            },
        )

    return {
        'tick': tick_dict,
        'strike': strike_dict,
        'stats': stats,
    }
