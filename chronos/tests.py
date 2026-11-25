import json
from unittest.mock import MagicMock

import requests
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.staticfiles import finders
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from chronos.engine import ChronosLiveEngine
from chronos.models import EngineSettings, MarketTick, StrikeExecution
from chronos.services import (
    compute_profit_breakdown,
    get_aggregate_stats,
    record_tick_and_maybe_strike,
)
from core.asgi import application


class EngineSettingsAndServicesTests(TestCase):
    def test_singleton_settings_defaults(self):
        cfg = EngineSettings.get_active()
        self.assertEqual(cfg.pk, 1)
        self.assertEqual(cfg.capital_usd, 1_000_000.0)
        self.assertEqual(cfg.arbitrage_threshold_pct, 0.5)
        self.assertEqual(cfg.feed_mode, EngineSettings.FEED_LIVE)
        self.assertTrue(cfg.auto_strike)

    def test_profit_breakdown_accounts_for_flash_fee_and_gas(self):
        # 1% drift on $1,000,000 => $10,000 gross; $500 flash fee; $295 gas+slippage
        breakdown = compute_profit_breakdown(1_000_000.0, 1.0)
        self.assertEqual(breakdown['gross_profit_usd'], 10_000.0)
        self.assertEqual(breakdown['flash_fee_usd'], 500.0)
        self.assertEqual(breakdown['estimated_gas_usd'], 295.0)
        self.assertEqual(breakdown['net_profit_usd'], 9_205.0)

    def test_record_tick_and_strike_updates_stats(self):
        breakdown = compute_profit_breakdown(1_000_000.0, 0.8)
        record_tick_and_maybe_strike(
            {
                'spot_price': 95_760.0,
                'oracle_price': 95_000.0,
                'delta': 0.8,
                'signed_delta': 0.8,
                'spot_source': 'Binance',
                'feed_mode': 'LIVE',
                'status': 'STRIKE COMPLETE',
                'log': 'Strike executed',
                'strike_flash': True,
                'strike': {
                    'execution_mode': 'SIMULATED',
                    'direction': 'SPOT_PREMIUM',
                    'capital_usd': 1_000_000.0,
                    **breakdown,
                    'succeeded': True,
                },
            },
            broadcast=False,
        )
        stats = get_aggregate_stats()
        self.assertEqual(stats['tick_count'], 1)
        self.assertEqual(stats['strike_count'], 1)
        self.assertEqual(stats['total_profit_usd'], breakdown['net_profit_usd'])
        self.assertEqual(stats['max_delta_pct'], 0.8)


class DashboardAndApiTests(TestCase):
    def test_dashboard_renders_cleanly(self):
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'C.H.R.O.N.O.S.')
        self.assertContains(resp, 'deltaChart')
        self.assertContains(resp, 'strike-ledger-body')
        self.assertIn('csrftoken', resp.cookies)
        self.assertIsNotNone(finders.find('chronos/css/dashboard.css'))
        self.assertIsNotNone(finders.find('chronos/js/dashboard.js'))

    def test_api_state_returns_snapshot(self):
        resp = self.client.get(reverse('api_state'))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('settings', data)
        self.assertIn('stats', data)
        self.assertIn('ticks', data)
        self.assertIn('strikes', data)
        self.assertIn('engine', data)

    def test_api_control_update_settings_validation_and_persistence(self):
        bad_resp = self.client.post(
            reverse('api_control'),
            data=json.dumps({'action': 'update_settings', 'capital_usd': 100}),
            content_type='application/json',
        )
        self.assertEqual(bad_resp.status_code, 400)

        good_resp = self.client.post(
            reverse('api_control'),
            data=json.dumps({
                'action': 'update_settings',
                'capital_usd': 2_500_000,
                'arbitrage_threshold_pct': 0.35,
                'poll_interval_sec': 2.0,
                'feed_mode': 'SIMULATION',
                'auto_strike': False,
            }),
            content_type='application/json',
        )
        self.assertEqual(good_resp.status_code, 200)
        cfg = EngineSettings.get_active()
        self.assertEqual(cfg.capital_usd, 2_500_000.0)
        self.assertEqual(cfg.arbitrage_threshold_pct, 0.35)
        self.assertEqual(cfg.feed_mode, EngineSettings.FEED_SIMULATION)
        self.assertFalse(cfg.auto_strike)

    def test_api_control_simulate_strike_and_reset(self):
        sim_resp = self.client.post(
            reverse('api_control'),
            data=json.dumps({'action': 'simulate_strike'}),
            content_type='application/json',
        )
        self.assertEqual(sim_resp.status_code, 200)
        self.assertEqual(MarketTick.objects.count(), 1)
        self.assertEqual(StrikeExecution.objects.count(), 1)

        csv_resp = self.client.get(reverse('export_strikes_csv'))
        self.assertEqual(csv_resp.status_code, 200)
        self.assertEqual(csv_resp['Content-Type'], 'text/csv')
        self.assertIn('SIMULATED', csv_resp.content.decode('utf-8'))

        reset_resp = self.client.post(
            reverse('api_control'),
            data=json.dumps({'action': 'reset_history'}),
            content_type='application/json',
        )
        self.assertEqual(reset_resp.status_code, 200)
        self.assertEqual(MarketTick.objects.count(), 0)
        self.assertEqual(StrikeExecution.objects.count(), 0)

    @override_settings(CHRONOS_API_TOKEN='secret-token')
    def test_engine_update_requires_token_and_validates_payload(self):
        unauth = self.client.post(
            reverse('engine_update'),
            data=json.dumps({'spot_price': 95000, 'oracle_price': 94500, 'delta': 0.52}),
            content_type='application/json',
        )
        self.assertEqual(unauth.status_code, 401)

        bad_payload = self.client.post(
            reverse('engine_update'),
            data=json.dumps({'spot_price': -1, 'oracle_price': 94500, 'delta': 0.52}),
            content_type='application/json',
            HTTP_X_CHRONOS_TOKEN='secret-token',
        )
        self.assertEqual(bad_payload.status_code, 400)

        ok_resp = self.client.post(
            reverse('engine_update'),
            data=json.dumps({'spot_price': 95000, 'oracle_price': 94500, 'delta': 0.5291}),
            content_type='application/json',
            HTTP_X_CHRONOS_TOKEN='secret-token',
        )
        self.assertEqual(ok_resp.status_code, 200)
        self.assertEqual(MarketTick.objects.count(), 1)


class ChronosEngineUnitTests(TestCase):
    def _mock_w3(self, price_raw: int, updated_at: int, decimals: int = 8):
        mock_contract = MagicMock()
        mock_contract.functions.decimals.return_value.call.return_value = decimals
        mock_contract.functions.latestRoundData.return_value.call.return_value = (
            1,
            price_raw,
            updated_at - 10,
            updated_at,
            1,
        )
        mock_w3 = MagicMock()
        mock_w3.eth.contract.return_value = mock_contract
        return mock_w3

    def test_spot_price_falls_back_to_coinbase_when_binance_fails(self):
        session = MagicMock()
        binance_resp = MagicMock()
        binance_resp.raise_for_status.side_effect = requests.HTTPError('451 Restricted')

        coinbase_resp = MagicMock()
        coinbase_resp.raise_for_status.return_value = None
        coinbase_resp.json.return_value = {'data': {'amount': '96123.45'}}

        session.get.side_effect = [binance_resp, coinbase_resp]
        engine = ChronosLiveEngine(rpc_url='', http_session=session, w3_client=self._mock_w3(96000_00000000, 1_700_000_000))

        price, source, err = engine.fetch_spot_price()
        self.assertEqual(price, 96123.45)
        self.assertEqual(source, 'Coinbase')
        self.assertIsNone(err)

    def test_evaluate_cycle_handles_discount_and_degraded_feeds(self):
        session = MagicMock()
        binance_resp = MagicMock()
        binance_resp.raise_for_status.return_value = None
        binance_resp.json.return_value = {'price': '94000.00'}
        session.get.return_value = binance_resp

        engine = ChronosLiveEngine(
            rpc_url='',
            http_session=session,
            w3_client=self._mock_w3(95_000_00000000, 1_700_000_000),
        )
        res = engine.evaluate_cycle(capital_usd=1_000_000, threshold_pct=0.5, feed_mode='LIVE')
        self.assertTrue(res['ok'])
        self.assertLess(res['signed_delta'], 0)
        self.assertTrue(res['strike_flash'])
        self.assertEqual(res['strike']['direction'], 'SPOT_DISCOUNT')

        # Missing RPC key in LIVE mode reports explicit FEED_DEGRADED error
        offline_engine = ChronosLiveEngine(rpc_url='', http_session=session)
        degraded = offline_engine.evaluate_cycle(feed_mode='LIVE')
        self.assertFalse(degraded['ok'])
        self.assertEqual(degraded['status'], 'FEED_DEGRADED')
        self.assertIn('ALCHEMY_RPC_URL', degraded['error'])

    def test_broadcast_to_ui_includes_timeout_and_token(self):
        session = MagicMock()
        post_resp = MagicMock()
        post_resp.raise_for_status.return_value = None
        session.post.return_value = post_resp

        engine = ChronosLiveEngine(
            rpc_url='',
            server_url='http://127.0.0.1:8000/api/update/',
            api_token='my-token',
            http_session=session,
        )
        ok = engine.broadcast_to_ui({'spot_price': 95000, 'oracle_price': 95000, 'delta': 0.0})
        self.assertTrue(ok)
        session.post.assert_called_once()
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs['timeout'], 4)
        self.assertEqual(kwargs['headers']['X-Chronos-Token'], 'my-token')


class ChronosWebSocketTests(TransactionTestCase):
    def test_websocket_connect_snapshot_and_ping_pong(self):
        async def run_ws_flow():
            communicator = WebsocketCommunicator(application, '/ws/chronos/')
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            first_msg = json.loads(await communicator.receive_from())
            self.assertEqual(first_msg['event'], 'snapshot')
            self.assertIn('settings', first_msg)

            await communicator.send_to(text_data=json.dumps({'action': 'ping'}))
            pong_msg = json.loads(await communicator.receive_from())
            self.assertEqual(pong_msg['event'], 'pong')

            await communicator.disconnect()

        async_to_sync(run_ws_flow)()
