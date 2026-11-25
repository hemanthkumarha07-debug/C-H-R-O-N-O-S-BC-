import csv
import hmac
import json

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .engine import engine_controller
from .models import EngineSettings, MarketTick, StrikeExecution
from .services import broadcast_event, build_dashboard_snapshot, record_tick_and_maybe_strike


@ensure_csrf_cookie
def dashboard(request):
    """Render the main C.H.R.O.N.O.S. command center UI."""
    return render(request, 'chronos/dashboard.html')


@require_GET
def api_state(request):
    """Return initial dashboard hydration state (settings, KPIs, ticks, strikes)."""
    return JsonResponse(build_dashboard_snapshot(engine_controller.get_status()))


@require_POST
def api_control(request):
    """Handle dashboard actions: start/stop scanner, dry-run strike, settings, reset."""
    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)

    action = body.get('action')

    if action == 'start':
        feed_mode = body.get('feed_mode')
        if feed_mode is not None and feed_mode not in (EngineSettings.FEED_LIVE, EngineSettings.FEED_SIMULATION):
            return JsonResponse({'error': 'Invalid feed_mode'}, status=400)
        status = engine_controller.start(feed_mode=feed_mode)
        return JsonResponse({'status': 'started', 'engine': status})

    if action == 'stop':
        status = engine_controller.stop()
        return JsonResponse({'status': 'stopped', 'engine': status})

    if action == 'simulate_strike':
        outcome = engine_controller.trigger_dry_run_strike()
        return JsonResponse({'status': 'simulated', **outcome})

    if action == 'update_settings':
        settings_obj = EngineSettings.get_active()
        try:
            if 'capital_usd' in body:
                capital = float(body['capital_usd'])
                if not (10_000.0 <= capital <= 100_000_000.0):
                    return JsonResponse({'error': 'capital_usd must be between $10,000 and $100,000,000'}, status=400)
                settings_obj.capital_usd = capital

            if 'arbitrage_threshold_pct' in body:
                threshold = float(body['arbitrage_threshold_pct'])
                if not (0.01 <= threshold <= 25.0):
                    return JsonResponse({'error': 'arbitrage_threshold_pct must be between 0.01% and 25%'}, status=400)
                settings_obj.arbitrage_threshold_pct = threshold

            if 'poll_interval_sec' in body:
                interval = float(body['poll_interval_sec'])
                if not (1.0 <= interval <= 60.0):
                    return JsonResponse({'error': 'poll_interval_sec must be between 1s and 60s'}, status=400)
                settings_obj.poll_interval_sec = interval

            if 'feed_mode' in body:
                feed_mode = str(body['feed_mode'])
                if feed_mode not in (EngineSettings.FEED_LIVE, EngineSettings.FEED_SIMULATION):
                    return JsonResponse({'error': 'Invalid feed_mode'}, status=400)
                settings_obj.feed_mode = feed_mode

            if 'auto_strike' in body:
                if not isinstance(body['auto_strike'], bool):
                    return JsonResponse({'error': 'auto_strike must be boolean'}, status=400)
                settings_obj.auto_strike = body['auto_strike']
        except (TypeError, ValueError):
            return JsonResponse({'error': 'Invalid numeric configuration parameter'}, status=400)

        settings_obj.save()
        payload = {'settings': settings_obj.to_dict(), 'engine': engine_controller.get_status()}
        broadcast_event('settings_updated', payload)
        return JsonResponse({'status': 'updated', **payload})

    if action == 'reset_history':
        MarketTick.objects.all().delete()
        StrikeExecution.objects.all().delete()
        snapshot = build_dashboard_snapshot(engine_controller.get_status())
        broadcast_event('reset', snapshot)
        return JsonResponse({'status': 'reset', **snapshot})

    return JsonResponse({'error': f'Unsupported action: {action}'}, status=400)


@csrf_exempt
@require_POST
def engine_update(request):
    """Bridge endpoint receiving validated telemetry from an external engine process."""
    expected_token = getattr(settings, 'CHRONOS_API_TOKEN', '')
    if expected_token:
        provided_token = request.headers.get('X-Chronos-Token', '')
        if not hmac.compare_digest(provided_token, expected_token):
            return JsonResponse({'error': 'Unauthorized engine token'}, status=401)

    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            return JsonResponse({'error': 'Payload must be a JSON object'}, status=400)
        spot_price = float(payload['spot_price'])
        oracle_price = float(payload['oracle_price'])
        delta = float(payload['delta'])
        if spot_price <= 0 or oracle_price <= 0 or delta < 0:
            return JsonResponse({'error': 'Prices must be positive and delta non-negative'}, status=400)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return JsonResponse({'error': f'Invalid payload: {exc}'}, status=400)

    recorded = record_tick_and_maybe_strike(payload, broadcast=True)
    return JsonResponse({'status': 'Broadcast Successful', 'tick_id': recorded['tick']['id']})


@require_GET
def export_strikes_csv(request):
    """Download strike execution audit history as a CSV file."""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="chronos_strikes.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'Timestamp (UTC)',
        'Mode',
        'Direction',
        'Spot Price ($)',
        'Oracle Price ($)',
        'Drift (%)',
        'Capital ($)',
        'Gross Profit ($)',
        'Flash Loan Fee ($)',
        'Est. Gas & Slippage ($)',
        'Net Profit ($)',
        'Succeeded',
        'Tx Hash',
        'Notes',
    ])

    for s in StrikeExecution.objects.all().iterator():
        writer.writerow([
            s.created_at.isoformat(),
            s.execution_mode,
            s.direction,
            f'{s.spot_price:.2f}',
            f'{s.oracle_price:.2f}',
            f'{s.delta_pct:.4f}',
            f'{s.capital_usd:.2f}',
            f'{s.gross_profit_usd:.2f}',
            f'{s.flash_fee_usd:.2f}',
            f'{s.estimated_gas_usd:.2f}',
            f'{s.net_profit_usd:.2f}',
            s.succeeded,
            s.tx_hash,
            s.notes,
        ])

    return response
