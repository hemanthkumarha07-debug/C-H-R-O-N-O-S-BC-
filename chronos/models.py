from django.db import models


class EngineSettings(models.Model):
    """Singleton configuration controlling the arbitrage scanner and strike parameters."""

    FEED_LIVE = 'LIVE'
    FEED_SIMULATION = 'SIMULATION'
    FEED_MODE_CHOICES = [
        (FEED_LIVE, 'Live (Chainlink + Spot)'),
        (FEED_SIMULATION, 'Simulation (Synthetic Feed)'),
    ]

    capital_usd = models.FloatField(default=1_000_000.0)
    arbitrage_threshold_pct = models.FloatField(default=0.5)
    poll_interval_sec = models.FloatField(default=3.0)
    feed_mode = models.CharField(
        max_length=16,
        choices=FEED_MODE_CHOICES,
        default=FEED_LIVE,
    )
    auto_strike = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Engine Settings'
        verbose_name_plural = 'Engine Settings'

    @classmethod
    def get_active(cls) -> 'EngineSettings':
        """Return the singleton settings row, creating defaults if none exists."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def to_dict(self) -> dict:
        return {
            'capital_usd': self.capital_usd,
            'arbitrage_threshold_pct': self.arbitrage_threshold_pct,
            'poll_interval_sec': self.poll_interval_sec,
            'feed_mode': self.feed_mode,
            'auto_strike': self.auto_strike,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __str__(self) -> str:
        return (
            f"EngineSettings(mode={self.feed_mode}, "
            f"threshold={self.arbitrage_threshold_pct:.2f}%, "
            f"capital=${self.capital_usd:,.0f})"
        )


class MarketTick(models.Model):
    """Persisted snapshot of spot vs. Chainlink oracle pricing at each scan tick."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    spot_price = models.FloatField()
    oracle_price = models.FloatField()
    delta_pct = models.FloatField()
    signed_delta_pct = models.FloatField(default=0.0)
    oracle_age_sec = models.IntegerField(null=True, blank=True)
    spot_source = models.CharField(max_length=32, default='Binance')
    feed_mode = models.CharField(max_length=16, default=EngineSettings.FEED_LIVE)
    status = models.CharField(max_length=64, default='SCANNING')
    log_message = models.TextField(blank=True, default='')
    strike_triggered = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at', '-id']

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'timestamp': self.created_at.isoformat() if self.created_at else None,
            'spot_price': self.spot_price,
            'oracle_price': self.oracle_price,
            'delta': self.delta_pct,
            'signed_delta': self.signed_delta_pct,
            'oracle_age_sec': self.oracle_age_sec,
            'spot_source': self.spot_source,
            'feed_mode': self.feed_mode,
            'status': self.status,
            'log': self.log_message,
            'strike_flash': self.strike_triggered,
        }

    def __str__(self) -> str:
        return f"Tick({self.spot_price:,.2f} vs {self.oracle_price:,.2f}, delta={self.delta_pct:.4f}%)"


class StrikeExecution(models.Model):
    """Audit record for each evaluated or executed arbitrage strike."""

    MODE_SIMULATED = 'SIMULATED'
    MODE_ONCHAIN = 'ONCHAIN'
    EXECUTION_MODE_CHOICES = [
        (MODE_SIMULATED, 'Simulated Dry-Run'),
        (MODE_ONCHAIN, 'On-Chain Execution'),
    ]

    DIRECTION_PREMIUM = 'SPOT_PREMIUM'
    DIRECTION_DISCOUNT = 'SPOT_DISCOUNT'

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    execution_mode = models.CharField(
        max_length=16,
        choices=EXECUTION_MODE_CHOICES,
        default=MODE_SIMULATED,
    )
    direction = models.CharField(max_length=32, default=DIRECTION_PREMIUM)
    spot_price = models.FloatField()
    oracle_price = models.FloatField()
    delta_pct = models.FloatField()
    capital_usd = models.FloatField()
    gross_profit_usd = models.FloatField()
    flash_fee_usd = models.FloatField()
    estimated_gas_usd = models.FloatField()
    net_profit_usd = models.FloatField()
    succeeded = models.BooleanField(default=True)
    tx_hash = models.CharField(max_length=80, blank=True, default='')
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at', '-id']

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'timestamp': self.created_at.isoformat() if self.created_at else None,
            'execution_mode': self.execution_mode,
            'direction': self.direction,
            'spot_price': self.spot_price,
            'oracle_price': self.oracle_price,
            'delta_pct': self.delta_pct,
            'capital_usd': self.capital_usd,
            'gross_profit_usd': self.gross_profit_usd,
            'flash_fee_usd': self.flash_fee_usd,
            'estimated_gas_usd': self.estimated_gas_usd,
            'net_profit_usd': self.net_profit_usd,
            'succeeded': self.succeeded,
            'tx_hash': self.tx_hash,
            'notes': self.notes,
        }

    def __str__(self) -> str:
        return f"Strike({self.execution_mode}, {self.direction}, net=${self.net_profit_usd:,.2f})"
