import time
from django.core.management.base import BaseCommand

from chronos.engine import ChronosLiveEngine
from chronos.models import EngineSettings
from chronos.services import record_tick_and_maybe_strike


class Command(BaseCommand):
    help = 'Run the C.H.R.O.N.O.S. arbitrage scanner loop directly from Django.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--simulate',
            action='store_true',
            help='Use synthetic simulation quotes instead of live RPC/exchange feeds.',
        )
        parser.add_argument(
            '--once',
            action='store_true',
            help='Execute a single scan cycle and exit.',
        )

    def handle(self, *args, **options):
        engine = ChronosLiveEngine()
        simulate = options['simulate']
        once = options['once']

        self.stdout.write(self.style.SUCCESS('C.H.R.O.N.O.S. Scanner active (Ctrl+C to stop).'))
        try:
            while True:
                cfg = EngineSettings.get_active()
                mode = EngineSettings.FEED_SIMULATION if simulate else cfg.feed_mode
                result = engine.evaluate_cycle(
                    capital_usd=cfg.capital_usd,
                    threshold_pct=cfg.arbitrage_threshold_pct,
                    feed_mode=mode,
                    auto_strike=cfg.auto_strike,
                )
                if result.get('ok'):
                    record_tick_and_maybe_strike(result, broadcast=True)
                    self.stdout.write(result['log'])
                else:
                    self.stderr.write(self.style.WARNING(result.get('error', 'Feed error')))

                if once:
                    break
                time.sleep(max(1.0, float(cfg.poll_interval_sec)))
        except KeyboardInterrupt:
            self.stdout.write(self.style.NOTICE('Scanner stopped cleanly.'))
