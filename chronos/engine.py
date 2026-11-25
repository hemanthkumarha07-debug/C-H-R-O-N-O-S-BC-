import logging
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from web3 import Web3

# Allow running directly as `python chronos/engine.py` as well as module import
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / '.env')

logger = logging.getLogger('chronos.engine')

CHAINLINK_BTC_USD_FEED = '0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c'
CHAINLINK_AGGREGATOR_ABI = [
    {
        'inputs': [],
        'name': 'decimals',
        'outputs': [{'internalType': 'uint8', 'name': '', 'type': 'uint8'}],
        'stateMutability': 'view',
        'type': 'function',
    },
    {
        'inputs': [],
        'name': 'latestRoundData',
        'outputs': [
            {'internalType': 'uint80', 'name': 'roundId', 'type': 'uint80'},
            {'internalType': 'int256', 'name': 'answer', 'type': 'int256'},
            {'internalType': 'uint256', 'name': 'startedAt', 'type': 'uint256'},
            {'internalType': 'uint256', 'name': 'updatedAt', 'type': 'uint256'},
            {'internalType': 'uint80', 'name': 'answeredInRound', 'type': 'uint80'},
        ],
        'stateMutability': 'view',
        'type': 'function',
    },
    {
        'inputs': [],
        'name': 'latestAnswer',
        'outputs': [{'internalType': 'int256', 'name': '', 'type': 'int256'}],
        'stateMutability': 'view',
        'type': 'function',
    },
]

BINANCE_TICKER_URL = 'https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT'
COINBASE_TICKER_URL = 'https://api.coinbase.com/v2/prices/BTC-USD/spot'


def _compute_profit(capital_usd: float, delta_pct: float) -> dict[str, float]:
    """Compute profit breakdown without requiring Django settings during standalone imports."""
    try:
        from chronos.services import compute_profit_breakdown
        return compute_profit_breakdown(capital_usd, delta_pct)
    except Exception:
        gross = round(capital_usd * (abs(delta_pct) / 100.0), 2)
        flash_fee = round(capital_usd * 0.0005, 2)
        gas_and_slippage = round(min(max(capital_usd * 0.00025 + 45.0, 45.0), 750.0), 2)
        return {
            'gross_profit_usd': gross,
            'flash_fee_usd': flash_fee,
            'estimated_gas_usd': gas_and_slippage,
            'net_profit_usd': round(gross - flash_fee - gas_and_slippage, 2),
        }


class ChronosLiveEngine:
    """Monitors Chainlink BTC/USD vs CEX spot feeds and evaluates flash-loan spread opportunities."""

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        server_url: str | None = None,
        api_token: str | None = None,
        http_session: requests.Session | None = None,
        w3_client: Web3 | None = None,
    ) -> None:
        self.FLASH_LOAN_CAPITAL = 1_000_000.0
        self.ARBITRAGE_THRESHOLD = 0.5
        self.TOTAL_PROFIT = 0.0

        self.eth_rpc = (rpc_url if rpc_url is not None else os.getenv('ALCHEMY_RPC_URL', '')).strip()
        self.server_url = (
            server_url if server_url is not None else os.getenv('CHRONOS_SERVER_URL', 'http://127.0.0.1:8000/api/update/')
        ).strip()
        self.api_token = (
            api_token if api_token is not None else os.getenv('CHRONOS_API_TOKEN', '')
        ).strip()
        self.http = http_session or requests.Session()

        self.w3: Web3 | None = w3_client
        self.rpc_error: str | None = None
        self._oracle_decimals: int | None = None

        if self.w3 is None:
            if not self.eth_rpc or 'your-api-key' in self.eth_rpc:
                self.rpc_error = 'ALCHEMY_RPC_URL is missing or uses placeholder value in .env'
            else:
                try:
                    self.w3 = Web3(Web3.HTTPProvider(self.eth_rpc, request_kwargs={'timeout': 5}))
                except Exception as exc:
                    self.rpc_error = f'Failed to initialize Web3 provider: {exc}'

        self.oracle_address = Web3.to_checksum_address(CHAINLINK_BTC_USD_FEED)
        self.oracle_abi = CHAINLINK_AGGREGATOR_ABI

        # Synthetic simulation anchor state
        self._sim_oracle_price = 95_400.0
        self._sim_spot_price = 95_520.0
        self._sim_tick_count = 0

    def fetch_oracle_price(self) -> tuple[float | None, int | None, str | None]:
        """Fetch Chainlink BTC/USD price and staleness age in seconds."""
        if self.w3 is None:
            return None, None, self.rpc_error or 'Web3 RPC client unavailable'
        try:
            contract = self.w3.eth.contract(address=self.oracle_address, abi=self.oracle_abi)
            if self._oracle_decimals is None:
                try:
                    self._oracle_decimals = int(contract.functions.decimals().call())
                except Exception:
                    self._oracle_decimals = 8

            round_data = contract.functions.latestRoundData().call()
            answer = int(round_data[1])
            updated_at = int(round_data[3])
            if answer <= 0:
                return None, None, f'Invalid Chainlink oracle answer: {answer}'

            price = float(answer) / (10 ** self._oracle_decimals)
            age_sec = max(0, int(time.time()) - updated_at) if updated_at > 0 else None
            self._sim_oracle_price = price
            return price, age_sec, None
        except Exception as exc:
            logger.warning('Chainlink oracle read failed: %s', exc)
            return None, None, f'Oracle RPC error: {exc}'

    def get_oracle_price(self) -> float | None:
        """Backward-compatible helper returning only the numeric Chainlink price."""
        price, _, _ = self.fetch_oracle_price()
        return price

    def fetch_spot_price(self) -> tuple[float | None, str | None, str | None]:
        """Fetch live BTC/USD spot price from Binance, falling back to Coinbase on geo-block/error."""
        errors: list[str] = []
        try:
            resp = self.http.get(BINANCE_TICKER_URL, timeout=4)
            resp.raise_for_status()
            price = float(resp.json()['price'])
            if price > 0:
                self._sim_spot_price = price
                return price, 'Binance', None
        except Exception as exc:
            errors.append(f'Binance: {exc}')

        try:
            resp = self.http.get(COINBASE_TICKER_URL, timeout=4)
            resp.raise_for_status()
            price = float(resp.json()['data']['amount'])
            if price > 0:
                self._sim_spot_price = price
                return price, 'Coinbase', None
        except Exception as exc:
            errors.append(f'Coinbase: {exc}')

        err_msg = ' | '.join(errors)
        logger.warning('Spot feed read failed (%s)', err_msg)
        return None, None, err_msg

    def get_spot_price(self) -> float | None:
        """Backward-compatible helper returning only the numeric spot price."""
        price, _, _ = self.fetch_spot_price()
        return price

    def generate_synthetic_pair(
        self,
        *,
        force_spike: bool = False,
        threshold_pct: float = 0.5,
    ) -> tuple[float, float, int]:
        """Produce realistic synthetic spot & lagging oracle quotes for offline demo/testing."""
        self._sim_tick_count += 1

        # Spot random-walks smoothly each tick; oracle lags and re-anchors periodically
        spot_move_pct = random.uniform(-0.0018, 0.0018)
        self._sim_spot_price = round(max(10_000.0, self._sim_spot_price * (1.0 + spot_move_pct)), 2)

        if force_spike:
            direction = random.choice([1.0, -1.0])
            spike_pct = max(threshold_pct, 0.5) + random.uniform(0.18, 0.45)
            self._sim_spot_price = round(self._sim_oracle_price * (1.0 + direction * (spike_pct / 100.0)), 2)
        elif self._sim_tick_count % 6 == 0:
            # Periodic simulated volatility spike so SIMULATION mode demonstrates strike cycles
            direction = random.choice([1.0, -1.0])
            spike_pct = threshold_pct + random.uniform(0.08, 0.35)
            self._sim_spot_price = round(self._sim_oracle_price * (1.0 + direction * (spike_pct / 100.0)), 2)
        elif abs(self._sim_spot_price - self._sim_oracle_price) / self._sim_oracle_price > 0.004:
            # Simulate Chainlink heartbeat/deviation re-anchoring toward spot
            self._sim_oracle_price = round(
                self._sim_oracle_price + (self._sim_spot_price - self._sim_oracle_price) * 0.75,
                2,
            )

        oracle_age = (self._sim_tick_count * 3) % 180
        return self._sim_spot_price, self._sim_oracle_price, oracle_age

    def evaluate_cycle(
        self,
        *,
        capital_usd: float | None = None,
        threshold_pct: float | None = None,
        feed_mode: str = 'LIVE',
        auto_strike: bool = True,
        force_spike: bool = False,
    ) -> dict[str, Any]:
        """Run one scan cycle and return a normalized telemetry (and optional strike) dict."""
        capital = float(capital_usd if capital_usd is not None else self.FLASH_LOAN_CAPITAL)
        threshold = float(threshold_pct if threshold_pct is not None else self.ARBITRAGE_THRESHOLD)

        effective_mode = 'SIMULATION' if (feed_mode == 'SIMULATION' or force_spike) else 'LIVE'
        if effective_mode == 'SIMULATION':
            spot_p, oracle_p, oracle_age = self.generate_synthetic_pair(
                force_spike=force_spike,
                threshold_pct=threshold,
            )
            spot_source = 'Synthetic'
        else:
            oracle_p, oracle_age, oracle_err = self.fetch_oracle_price()
            spot_p, spot_source, spot_err = self.fetch_spot_price()
            if oracle_p is None or spot_p is None:
                errors = [err for err in (oracle_err, spot_err) if err]
                return {
                    'ok': False,
                    'feed_mode': feed_mode,
                    'status': 'FEED_DEGRADED',
                    'error': ' ; '.join(errors) or 'Price feed unavailable',
                }

        signed_delta = ((spot_p - oracle_p) / oracle_p) * 100.0
        delta = abs(signed_delta)
        direction = 'SPOT_PREMIUM' if signed_delta >= 0 else 'SPOT_DISCOUNT'

        payload: dict[str, Any] = {
            'ok': True,
            'spot_price': round(spot_p, 2),
            'oracle_price': round(oracle_p, 2),
            'delta': round(delta, 4),
            'signed_delta': round(signed_delta, 4),
            'oracle_age_sec': oracle_age,
            'spot_source': spot_source or 'Binance',
            'feed_mode': effective_mode,
            'status': 'SCANNING',
            'log': (
                f'[{effective_mode}] {spot_source}: ${spot_p:,.2f} | '
                f'Chainlink: ${oracle_p:,.2f} | Drift: {signed_delta:+.4f}%'
            ),
            'total_profit': self.TOTAL_PROFIT,
            'strike_flash': False,
            'strike': None,
        }

        should_evaluate_strike = force_spike or (delta >= threshold and auto_strike)
        if should_evaluate_strike:
            breakdown = _compute_profit(capital, delta)
            net_profit = breakdown['net_profit_usd']
            if net_profit > 0:
                self.TOTAL_PROFIT = round(self.TOTAL_PROFIT + net_profit, 2)
                payload['status'] = 'STRIKE COMPLETE'
                payload['strike_flash'] = True
                payload['total_profit'] = self.TOTAL_PROFIT
                payload['log'] = (
                    f'SIMULATED STRIKE [{direction}] {delta:.4f}% drift | '
                    f'Gross ${breakdown["gross_profit_usd"]:,.2f} - Fees/Gas '
                    f'${breakdown["flash_fee_usd"] + breakdown["estimated_gas_usd"]:,.2f} '
                    f'= +${net_profit:,.2f}'
                )
                payload['strike'] = {
                    'execution_mode': 'SIMULATED',
                    'direction': direction,
                    'capital_usd': capital,
                    **breakdown,
                    'succeeded': True,
                    'notes': (
                        f'Dry-run simulation triggered at {delta:.4f}% spread '
                        f'({spot_source} vs Chainlink).'
                    ),
                }
            else:
                payload['status'] = 'SPREAD UNPROFITABLE'
                payload['log'] = (
                    f'GAP {delta:.4f}% skipped: fees/gas exceed gross spread '
                    f'(net ${net_profit:,.2f}).'
                )
        elif delta >= threshold and not auto_strike:
            payload['status'] = 'OPPORTUNITY DETECTED'
            payload['log'] = (
                f'ALERT: {delta:.4f}% drift >= {threshold:.2f}% threshold (Auto-Strike paused).'
            )

        return payload

    def broadcast_to_ui(self, payload: dict[str, Any]) -> bool:
        """Send payload to the Django HTTP bridge when running standalone."""
        headers = {}
        if self.api_token:
            headers['X-Chronos-Token'] = self.api_token
        try:
            resp = self.http.post(self.server_url, json=payload, headers=headers, timeout=4)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning('Command Center unreachable (%s): %s', self.server_url, exc)
            return False

    def scan(self, poll_interval: float = 3.0, max_cycles: int | None = None) -> None:
        """Run standalone continuous polling loop."""
        print('-' * 60)
        print('C.H.R.O.N.O.S. MICROSERVICE ENGINE: ACTIVE')
        if self.rpc_error:
            print(f'WARNING: {self.rpc_error}')
        print('-' * 60)

        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            result = self.evaluate_cycle()
            if result.get('ok'):
                self.broadcast_to_ui(result)
                print(
                    f"[PING] {result['spot_source']}: ${result['spot_price']:,.2f} | "
                    f"Oracle: ${result['oracle_price']:,.2f} | Drift: {result['signed_delta']:+.4f}%"
                )
            else:
                print(f"[WARN] {result.get('error')}")
            if max_cycles is None or cycles < max_cycles:
                time.sleep(poll_interval)


class EngineController:
    """Thread-safe controller managing an in-process scanner thread inside Django/Daphne."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._engine = ChronosLiveEngine()
        self._active_mode = 'LIVE'
        self._last_error: str | None = None
        self._last_tick_at: str | None = None

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                'is_running': running,
                'active_mode': self._active_mode,
                'last_error': self._last_error,
                'last_tick_at': self._last_tick_at,
            }

    def start(self, feed_mode: str | None = None) -> dict[str, Any]:
        from chronos.models import EngineSettings
        from chronos.services import broadcast_event

        settings_obj = EngineSettings.get_active()
        if feed_mode in (EngineSettings.FEED_LIVE, EngineSettings.FEED_SIMULATION):
            settings_obj.feed_mode = feed_mode
            settings_obj.save(update_fields=['feed_mode', 'updated_at'])

        with self._lock:
            self._active_mode = settings_obj.feed_mode
            if self._thread is not None and self._thread.is_alive():
                status = {
                    'is_running': True,
                    'active_mode': self._active_mode,
                    'last_error': self._last_error,
                    'last_tick_at': self._last_tick_at,
                }
            else:
                self._stop_event.clear()
                self._last_error = None
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name='chronos-scanner',
                    daemon=True,
                )
                self._thread.start()
                status = {
                    'is_running': True,
                    'active_mode': self._active_mode,
                    'last_error': None,
                    'last_tick_at': self._last_tick_at,
                }

        broadcast_event('engine_status', {'engine': status})
        return status

    def stop(self) -> dict[str, Any]:
        from chronos.services import broadcast_event

        with self._lock:
            self._stop_event.set()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

        status = self.get_status()
        broadcast_event('engine_status', {'engine': status})
        return status

    def trigger_dry_run_strike(self) -> dict[str, Any]:
        """Run one immediate simulated arbitrage cycle and persist/broadcast it."""
        from chronos.models import EngineSettings
        from chronos.services import record_tick_and_maybe_strike

        settings_obj = EngineSettings.get_active()
        result = self._engine.evaluate_cycle(
            capital_usd=settings_obj.capital_usd,
            threshold_pct=settings_obj.arbitrage_threshold_pct,
            feed_mode=EngineSettings.FEED_SIMULATION,
            auto_strike=True,
            force_spike=True,
        )
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        with self._lock:
            self._last_tick_at = now_iso
        status = self.get_status()
        return record_tick_and_maybe_strike(result, engine_status=status, broadcast=True)

    def _run_loop(self) -> None:
        from django.db import close_old_connections
        from chronos.models import EngineSettings
        from chronos.services import broadcast_event, record_tick_and_maybe_strike

        logger.info('In-process CHRONOS scanner started.')
        while not self._stop_event.is_set():
            close_old_connections()
            poll_interval = 3.0
            try:
                settings_obj = EngineSettings.get_active()
                poll_interval = max(1.0, float(settings_obj.poll_interval_sec))
                with self._lock:
                    self._active_mode = settings_obj.feed_mode

                result = self._engine.evaluate_cycle(
                    capital_usd=settings_obj.capital_usd,
                    threshold_pct=settings_obj.arbitrage_threshold_pct,
                    feed_mode=settings_obj.feed_mode,
                    auto_strike=settings_obj.auto_strike,
                )

                if result.get('ok'):
                    now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                    with self._lock:
                        self._last_error = None
                        self._last_tick_at = now_iso
                    record_tick_and_maybe_strike(result, engine_status=self.get_status(), broadcast=True)
                else:
                    err = result.get('error', 'Unknown feed error')
                    with self._lock:
                        self._last_error = err
                    broadcast_event(
                        'engine_status',
                        {
                            'engine': self.get_status(),
                            'log': f'FEED WARNING: {err}',
                            'level': 'WARN',
                        },
                    )
            except Exception as exc:
                logger.exception('Unexpected scanner loop failure')
                with self._lock:
                    self._last_error = str(exc)
                broadcast_event(
                    'engine_status',
                    {
                        'engine': self.get_status(),
                        'log': f'ENGINE ERROR: {exc}',
                        'level': 'ERROR',
                    },
                )
            finally:
                close_old_connections()

            self._stop_event.wait(poll_interval)

        logger.info('In-process CHRONOS scanner stopped.')


engine_controller = EngineController()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    ChronosLiveEngine().scan()
