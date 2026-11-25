(() => {
    'use strict';

    const MAX_CHART_POINTS = 40;
    const MAX_LOG_LINES = 150;

    const state = {
        chartMode: 'drift', // 'drift' | 'prices'
        ticks: [],
        strikes: [],
        settings: {
            capital_usd: 1_000_000,
            arbitrage_threshold_pct: 0.5,
            poll_interval_sec: 3.0,
            feed_mode: 'LIVE',
            auto_strike: true,
        },
        engine: {
            is_running: false,
            active_mode: 'LIVE',
            last_error: null,
            last_tick_at: null,
        },
        stats: {
            total_profit_usd: 0,
            strike_count: 0,
            max_delta_pct: 0,
            avg_delta_pct: 0,
        },
        logFilter: 'ALL',
        socket: null,
        reconnectAttempts: 0,
        pingTimer: null,
    };

    const el = {
        body: document.getElementById('main-body'),
        wsDot: document.getElementById('ws-dot'),
        wsStatus: document.getElementById('ws-status'),
        engineDot: document.getElementById('engine-dot'),
        engineStatus: document.getElementById('engine-status'),
        modeBadge: document.getElementById('mode-badge'),
        alertBanner: document.getElementById('alert-banner'),
        btnToggleEngine: document.getElementById('btn-toggle-engine'),
        btnDryRun: document.getElementById('btn-dry-run'),
        spotPrice: document.getElementById('current-price'),
        spotSourceBadge: document.getElementById('spot-source-badge'),
        spotMeta: document.getElementById('spot-meta'),
        oraclePrice: document.getElementById('oracle-price'),
        oracleMeta: document.getElementById('oracle-meta'),
        deltaDisplay: document.getElementById('delta-display'),
        directionBadge: document.getElementById('direction-badge'),
        thresholdProgress: document.getElementById('threshold-progress'),
        deltaMeta: document.getElementById('delta-meta'),
        totalProfit: document.getElementById('total-profit'),
        profitMeta: document.getElementById('profit-meta'),
        chartCaption: document.getElementById('chart-caption'),
        tabDrift: document.getElementById('tab-drift'),
        tabPrices: document.getElementById('tab-prices'),
        statSamples: document.getElementById('stat-samples'),
        statAvgDelta: document.getElementById('stat-avg-delta'),
        statMaxDelta: document.getElementById('stat-max-delta'),
        statLastTick: document.getElementById('stat-last-tick'),
        settingsForm: document.getElementById('settings-form'),
        inputFeedMode: document.getElementById('input-feed-mode'),
        inputCapital: document.getElementById('input-capital'),
        inputThreshold: document.getElementById('input-threshold'),
        inputInterval: document.getElementById('input-interval'),
        inputAutoStrike: document.getElementById('input-auto-strike'),
        btnSaveConfig: document.getElementById('btn-save-config'),
        btnResetHistory: document.getElementById('btn-reset-history'),
        ledgerCount: document.getElementById('ledger-count'),
        ledgerBody: document.getElementById('strike-ledger-body'),
        logFilter: document.getElementById('log-filter'),
        btnClearLogs: document.getElementById('btn-clear-logs'),
        terminal: document.getElementById('terminal-output'),
        canvas: document.getElementById('deltaChart'),
        fallbackSvg: document.getElementById('fallback-sparkline'),
    };

    let chartInstance = null;

    function getCookie(name) {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) {
            return parts.pop().split(';').shift();
        }
        return '';
    }

    function formatCurrency(val) {
        return `$${Number(val || 0).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        })}`;
    }

    function formatTime(isoString) {
        const dt = isoString ? new Date(isoString) : new Date();
        if (Number.isNaN(dt.getTime())) {
            return new Date().toLocaleTimeString('en-US', { hour12: false });
        }
        return dt.toLocaleTimeString('en-US', {
            hour12: false,
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    }

    function showBanner(message, variant = 'warn') {
        if (!message) {
            el.alertBanner.classList.add('hidden');
            el.alertBanner.textContent = '';
            return;
        }
        el.alertBanner.className = 'alert-banner';
        if (variant === 'error') el.alertBanner.classList.add('alert-banner--error');
        if (variant === 'success') el.alertBanner.classList.add('alert-banner--success');
        el.alertBanner.textContent = message;
    }

    function initChart() {
        if (typeof Chart === 'undefined') {
            el.canvas.classList.add('hidden');
            el.fallbackSvg.classList.remove('hidden');
            return;
        }

        const ctx = el.canvas.getContext('2d');
        const blueGrad = ctx.createLinearGradient(0, 0, 0, 280);
        blueGrad.addColorStop(0, 'rgba(88, 166, 255, 0.38)');
        blueGrad.addColorStop(1, 'rgba(88, 166, 255, 0.0)');

        chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    {
                        label: 'Arbitrage Drift (%)',
                        data: [],
                        borderColor: '#58a6ff',
                        backgroundColor: blueGrad,
                        borderWidth: 2,
                        pointRadius: 2.5,
                        pointBackgroundColor: '#ffffff',
                        pointBorderColor: '#58a6ff',
                        fill: true,
                        tension: 0.32,
                    },
                    {
                        label: 'Trigger Threshold (%)',
                        data: [],
                        borderColor: 'rgba(240, 180, 41, 0.78)',
                        borderDash: [6, 5],
                        borderWidth: 1.5,
                        pointRadius: 0,
                        fill: false,
                        tension: 0,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        grid: { color: 'rgba(255, 255, 255, 0.04)' },
                        ticks: {
                            color: '#64748b',
                            maxTicksLimit: 8,
                            font: { family: 'JetBrains Mono, monospace', size: 10 },
                        },
                    },
                    y: {
                        suggestedMin: 0,
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: {
                            color: '#94a3b8',
                            font: { family: 'JetBrains Mono, monospace', size: 10 },
                            callback: (val) =>
                                state.chartMode === 'drift'
                                    ? `${Number(val).toFixed(3)}%`
                                    : `$${Number(val).toLocaleString('en-US')}`,
                        },
                    },
                },
                plugins: {
                    legend: {
                        display: true,
                        labels: {
                            color: '#94a3b8',
                            boxWidth: 12,
                            font: { family: 'JetBrains Mono, monospace', size: 11 },
                        },
                    },
                    tooltip: {
                        backgroundColor: 'rgba(6, 8, 15, 0.92)',
                        borderColor: 'rgba(148, 163, 184, 0.2)',
                        borderWidth: 1,
                        titleFont: { family: 'JetBrains Mono, monospace' },
                        bodyFont: { family: 'JetBrains Mono, monospace' },
                    },
                },
            },
        });
    }

    function renderFallbackSvg() {
        if (el.fallbackSvg.classList.contains('hidden')) return;
        const svgNS = 'http://www.w3.org/2000/svg';
        el.fallbackSvg.replaceChildren();
        const values = state.ticks.map((t) =>
            state.chartMode === 'drift' ? Number(t.delta || 0) : Number(t.spot_price || 0)
        );
        if (values.length < 2) return;

        const min = Math.min(...values);
        const max = Math.max(...values, min + 0.0001);
        const points = values
            .map((v, idx) => {
                const x = (idx / (values.length - 1)) * 580 + 10;
                const y = 200 - ((v - min) / (max - min)) * 180;
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            })
            .join(' ');

        const polyline = document.createElementNS(svgNS, 'polyline');
        polyline.setAttribute('fill', 'none');
        polyline.setAttribute('stroke', '#58a6ff');
        polyline.setAttribute('stroke-width', '2.5');
        polyline.setAttribute('points', points);
        el.fallbackSvg.appendChild(polyline);
    }

    function renderChart() {
        const ticks = state.ticks.slice(-MAX_CHART_POINTS);
        const labels = ticks.map((t) => formatTime(t.timestamp));

        if (chartInstance) {
            chartInstance.data.labels = labels;
            const threshold = Number(state.settings.arbitrage_threshold_pct || 0.5);

            if (state.chartMode === 'drift') {
                el.chartCaption.textContent =
                    'Real-time absolute spread vs. trigger threshold (last 40 ticks)';
                chartInstance.data.datasets[0].label = 'Arbitrage Drift (%)';
                chartInstance.data.datasets[0].borderColor = '#58a6ff';
                chartInstance.data.datasets[0].fill = true;
                chartInstance.data.datasets[0].data = ticks.map((t) => Number(t.delta || 0));

                chartInstance.data.datasets[1].label = `Threshold (${threshold.toFixed(2)}%)`;
                chartInstance.data.datasets[1].borderColor = 'rgba(240, 180, 41, 0.8)';
                chartInstance.data.datasets[1].borderDash = [6, 5];
                chartInstance.data.datasets[1].data = ticks.map(() => threshold);
                chartInstance.options.scales.y.suggestedMin = 0;
            } else {
                el.chartCaption.textContent =
                    'Spot market quote vs. Chainlink on-chain oracle price (last 40 ticks)';
                chartInstance.data.datasets[0].label = 'Spot Price ($)';
                chartInstance.data.datasets[0].borderColor = '#58a6ff';
                chartInstance.data.datasets[0].fill = false;
                chartInstance.data.datasets[0].data = ticks.map((t) => Number(t.spot_price || 0));

                chartInstance.data.datasets[1].label = 'Chainlink Oracle ($)';
                chartInstance.data.datasets[1].borderColor = '#a371f7';
                chartInstance.data.datasets[1].borderDash = [];
                chartInstance.data.datasets[1].data = ticks.map((t) => Number(t.oracle_price || 0));
                chartInstance.options.scales.y.suggestedMin = undefined;
            }

            chartInstance.update('none');
        } else {
            renderFallbackSvg();
        }

        el.statSamples.textContent = `${ticks.length} / ${MAX_CHART_POINTS}`;
        if (ticks.length > 0) {
            const deltas = ticks.map((t) => Number(t.delta || 0));
            const avg = deltas.reduce((a, b) => a + b, 0) / deltas.length;
            const max = Math.max(...deltas);
            el.statAvgDelta.textContent = `${avg.toFixed(4)}%`;
            el.statMaxDelta.textContent = `${max.toFixed(4)}%`;
            el.statLastTick.textContent = formatTime(ticks[ticks.length - 1].timestamp);
        } else {
            el.statAvgDelta.textContent = '0.0000%';
            el.statMaxDelta.textContent = '0.0000%';
            el.statLastTick.textContent = '--';
        }
    }

    function renderEngineStatus() {
        const running = Boolean(state.engine.is_running);
        const mode = state.engine.active_mode || state.settings.feed_mode || 'LIVE';

        el.modeBadge.textContent = mode;
        el.modeBadge.classList.toggle('mode-badge--sim', mode === 'SIMULATION');

        if (running) {
            el.engineStatus.textContent = state.engine.last_error ? 'DEGRADED' : 'SCANNING';
            el.engineDot.className = state.engine.last_error ? 'dot dot--warn' : 'dot dot--ok';
            el.btnToggleEngine.textContent = 'Pause Scanner';
            el.btnToggleEngine.classList.add('is-running');
        } else {
            el.engineStatus.textContent = 'STANDBY';
            el.engineDot.className = 'dot dot--idle';
            el.btnToggleEngine.textContent = 'Start Scanner';
            el.btnToggleEngine.classList.remove('is-running');
        }

        if (state.engine.last_error) {
            showBanner(`Feed Alert: ${state.engine.last_error}`, 'warn');
        } else if (el.alertBanner.textContent.startsWith('Feed Alert:')) {
            showBanner('');
        }
    }

    function renderSettingsForm() {
        el.inputFeedMode.value = state.settings.feed_mode || 'LIVE';
        el.inputCapital.value = Number(state.settings.capital_usd || 1_000_000);
        el.inputThreshold.value = Number(state.settings.arbitrage_threshold_pct || 0.5).toFixed(2);
        el.inputInterval.value = Number(state.settings.poll_interval_sec || 3.0);
        el.inputAutoStrike.checked = Boolean(state.settings.auto_strike);
        el.deltaMeta.textContent = `Trigger threshold: ${Number(state.settings.arbitrage_threshold_pct || 0.5).toFixed(4)}%`;
    }

    function renderLatestTick() {
        const latest = state.ticks[state.ticks.length - 1];
        const threshold = Math.max(0.01, Number(state.settings.arbitrage_threshold_pct || 0.5));

        if (!latest) {
            el.spotPrice.textContent = '$0.00';
            el.spotSourceBadge.textContent = '--';
            el.spotMeta.textContent = 'Awaiting market tick...';
            el.oraclePrice.textContent = '$0.00';
            el.oracleMeta.textContent = 'Heartbeat age: --';
            el.deltaDisplay.textContent = '0.0000%';
            el.directionBadge.textContent = 'NEUTRAL';
            el.thresholdProgress.style.width = '0%';
            el.thresholdProgress.classList.remove('is-hot');
        } else {
            const spot = Number(latest.spot_price || 0);
            const oracle = Number(latest.oracle_price || 0);
            const delta = Number(latest.delta || 0);
            const signedDelta = Number(
                latest.signed_delta !== undefined ? latest.signed_delta : delta
            );

            el.spotPrice.textContent = formatCurrency(spot);
            el.spotSourceBadge.textContent = latest.spot_source || 'Spot';
            el.spotMeta.textContent = `Spread vs Oracle: ${formatCurrency(spot - oracle)} · ${formatTime(latest.timestamp)}`;

            el.oraclePrice.textContent = formatCurrency(oracle);
            const ageText =
                latest.oracle_age_sec !== null && latest.oracle_age_sec !== undefined
                    ? `${latest.oracle_age_sec}s ago`
                    : 'unknown';
            el.oracleMeta.textContent = `Heartbeat age: ${ageText}`;

            el.deltaDisplay.textContent = `${signedDelta >= 0 ? '+' : ''}${signedDelta.toFixed(4)}%`;
            el.directionBadge.textContent = signedDelta >= 0 ? 'SPOT PREMIUM' : 'SPOT DISCOUNT';

            const pct = Math.min(100, (delta / threshold) * 100);
            el.thresholdProgress.style.width = `${pct.toFixed(1)}%`;
            el.thresholdProgress.classList.toggle('is-hot', delta >= threshold);
        }

        el.totalProfit.textContent = formatCurrency(state.stats.total_profit_usd);
        const strikeCount = Number(state.stats.strike_count || 0);
        const maxDelta = Number(state.stats.max_delta_pct || 0);
        el.profitMeta.textContent = `${strikeCount} strike${strikeCount === 1 ? '' : 's'} executed · Peak drift: ${maxDelta.toFixed(4)}%`;
    }

    function renderStrikeLedger() {
        const strikes = state.strikes || [];
        el.ledgerCount.textContent = `${strikes.length} entr${strikes.length === 1 ? 'y' : 'ies'}`;
        el.ledgerBody.replaceChildren();

        if (strikes.length === 0) {
            const row = document.createElement('tr');
            row.className = 'empty-row';
            const cell = document.createElement('td');
            cell.colSpan = 7;
            cell.textContent = 'No strikes recorded yet. Start the scanner or click Dry-Run Strike.';
            row.appendChild(cell);
            el.ledgerBody.appendChild(row);
            return;
        }

        for (const s of strikes.slice(0, 25)) {
            const tr = document.createElement('tr');
            const totalCosts = Number(s.flash_fee_usd || 0) + Number(s.estimated_gas_usd || 0);

            const cells = [
                { text: formatTime(s.timestamp), cls: '' },
                { text: s.execution_mode || 'SIMULATED', cls: '' },
                { text: s.direction || 'SPOT_PREMIUM', cls: '' },
                { text: `${Number(s.delta_pct || 0).toFixed(4)}%`, cls: 'num' },
                { text: formatCurrency(s.capital_usd), cls: 'num' },
                { text: formatCurrency(totalCosts), cls: 'num' },
                { text: `+${formatCurrency(s.net_profit_usd)}`, cls: 'num profit-pos' },
            ];

            for (const c of cells) {
                const td = document.createElement('td');
                if (c.cls) td.className = c.cls;
                td.textContent = c.text;
                tr.appendChild(td);
            }
            el.ledgerBody.appendChild(tr);
        }
    }

    function appendLog(message, level = 'MUTED', timestamp = null) {
        if (!message) return;

        const row = document.createElement('div');
        row.dataset.level = level;

        let levelClass = 'log-line--muted';
        if (level === 'STRIKE') levelClass = 'log-line--strike';
        else if (level === 'INFO') levelClass = 'log-line--info';
        else if (level === 'WARN') levelClass = 'log-line--warn';
        else if (level === 'ERROR') levelClass = 'log-line--error';

        row.className = `log-line ${levelClass}`;
        row.textContent = `[${formatTime(timestamp)}] > ${message}`;

        if (!matchesLogFilter(level)) {
            row.classList.add('hidden');
        }

        el.terminal.appendChild(row);
        while (el.terminal.children.length > MAX_LOG_LINES) {
            el.terminal.removeChild(el.terminal.firstChild);
        }
        el.terminal.scrollTop = el.terminal.scrollHeight;
    }

    function matchesLogFilter(level) {
        if (state.logFilter === 'ALL') return true;
        if (state.logFilter === 'STRIKE') return level === 'STRIKE';
        if (state.logFilter === 'WARN') return level === 'WARN' || level === 'ERROR';
        return true;
    }

    function applyLogFilter() {
        for (const child of el.terminal.children) {
            const lvl = child.dataset.level || 'MUTED';
            child.classList.toggle('hidden', !matchesLogFilter(lvl));
        }
    }

    function triggerStrikeFlash() {
        el.body.classList.add('strike-flash');
        setTimeout(() => {
            el.body.classList.remove('strike-flash');
        }, 900);
    }

    function applySnapshot(snapshot) {
        if (snapshot.settings) {
            state.settings = snapshot.settings;
            renderSettingsForm();
        }
        if (snapshot.engine) {
            state.engine = snapshot.engine;
            renderEngineStatus();
        }
        if (snapshot.stats) {
            state.stats = snapshot.stats;
        }
        if (Array.isArray(snapshot.ticks)) {
            state.ticks = snapshot.ticks.slice(-MAX_CHART_POINTS);
        }
        if (Array.isArray(snapshot.strikes)) {
            state.strikes = snapshot.strikes;
        }

        renderLatestTick();
        renderChart();
        renderStrikeLedger();
    }

    function handleTickMessage(payload) {
        const tick = payload.tick || payload;
        if (tick && tick.spot_price !== undefined && tick.oracle_price !== undefined) {
            state.ticks.push(tick);
            if (state.ticks.length > MAX_CHART_POINTS) {
                state.ticks.shift();
            }
        }
        if (payload.stats) {
            state.stats = payload.stats;
        } else if (payload.total_profit !== undefined) {
            state.stats.total_profit_usd = payload.total_profit;
        }
        if (payload.engine) {
            state.engine = payload.engine;
            renderEngineStatus();
        }
        if (payload.strike) {
            state.strikes.unshift(payload.strike);
            state.strikes = state.strikes.slice(0, 25);
            renderStrikeLedger();
        }

        renderLatestTick();
        renderChart();

        if (tick && tick.log) {
            const isStrike = Boolean(tick.strike_flash || payload.strike);
            appendLog(tick.log, isStrike ? 'STRIKE' : 'MUTED', tick.timestamp);
            if (isStrike) triggerStrikeFlash();
        }
    }

    async function postControl(actionPayload) {
        const resp = await fetch('/api/control/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken'),
            },
            body: JSON.stringify(actionPayload),
        });
        const data = await resp.json();
        if (!resp.ok) {
            throw new Error(data.error || `Request failed (${resp.status})`);
        }
        return data;
    }

    async function loadInitialState() {
        try {
            const resp = await fetch('/api/state/');
            if (!resp.ok) return;
            const snapshot = await resp.json();
            applySnapshot(snapshot);
            if (state.ticks.length > 0) {
                appendLog(`Hydrated ${state.ticks.length} recent ticks from SQLite.`, 'INFO');
            }
        } catch (err) {
            appendLog(`State hydration failed: ${err.message}`, 'WARN');
        }
    }

    function connectWebSocket() {
        const wsProtocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
        const socket = new WebSocket(`${wsProtocol}${window.location.host}/ws/chronos/`);
        state.socket = socket;

        socket.onopen = () => {
            state.reconnectAttempts = 0;
            el.wsStatus.textContent = 'CONNECTED';
            el.wsDot.className = 'dot dot--ok';
            showBanner(state.engine.last_error ? `Feed Alert: ${state.engine.last_error}` : '');
            appendLog('WebSocket stream connected.', 'INFO');

            if (state.pingTimer) clearInterval(state.pingTimer);
            state.pingTimer = setInterval(() => {
                if (socket.readyState === WebSocket.OPEN) {
                    socket.send(JSON.stringify({ action: 'ping' }));
                }
            }, 25_000);
        };

        socket.onmessage = (event) => {
            try {
                const payload = JSON.parse(event.data);
                const eventType = payload.event;

                if (eventType === 'pong') return;
                if (eventType === 'snapshot' || eventType === 'reset') {
                    applySnapshot(payload);
                    if (eventType === 'reset') {
                        appendLog('Telemetry & strike ledger reset.', 'INFO');
                    }
                    return;
                }
                if (eventType === 'settings_updated') {
                    if (payload.settings) {
                        state.settings = payload.settings;
                        renderSettingsForm();
                        renderChart();
                        renderLatestTick();
                    }
                    if (payload.engine) {
                        state.engine = payload.engine;
                        renderEngineStatus();
                    }
                    appendLog(
                        `Settings saved (${state.settings.feed_mode}, threshold ${Number(state.settings.arbitrage_threshold_pct).toFixed(2)}%).`,
                        'INFO'
                    );
                    return;
                }
                if (eventType === 'engine_status') {
                    if (payload.engine) {
                        state.engine = payload.engine;
                        renderEngineStatus();
                    }
                    if (payload.log) {
                        appendLog(payload.log, payload.level || 'WARN');
                    }
                    return;
                }

                handleTickMessage(payload);
            } catch (err) {
                console.error('WebSocket packet parse error:', err, event.data);
                appendLog(`Malformed WebSocket packet ignored: ${err.message}`, 'ERROR');
            }
        };

        socket.onclose = () => {
            if (state.pingTimer) {
                clearInterval(state.pingTimer);
                state.pingTimer = null;
            }
            el.wsStatus.textContent = 'RECONNECTING';
            el.wsDot.className = 'dot dot--warn';

            state.reconnectAttempts += 1;
            const delayMs = Math.min(10_000, 1000 * Math.pow(2, state.reconnectAttempts - 1));
            setTimeout(connectWebSocket, delayMs);
        };

        socket.onerror = () => {
            el.wsDot.className = 'dot dot--err';
            socket.close();
        };
    }

    function bindEvents() {
        el.tabDrift.addEventListener('click', () => {
            state.chartMode = 'drift';
            el.tabDrift.classList.add('seg-btn--active');
            el.tabPrices.classList.remove('seg-btn--active');
            el.tabDrift.setAttribute('aria-selected', 'true');
            el.tabPrices.setAttribute('aria-selected', 'false');
            renderChart();
        });

        el.tabPrices.addEventListener('click', () => {
            state.chartMode = 'prices';
            el.tabPrices.classList.add('seg-btn--active');
            el.tabDrift.classList.remove('seg-btn--active');
            el.tabPrices.setAttribute('aria-selected', 'true');
            el.tabDrift.setAttribute('aria-selected', 'false');
            renderChart();
        });

        el.btnToggleEngine.addEventListener('click', async () => {
            el.btnToggleEngine.disabled = true;
            try {
                if (state.engine.is_running) {
                    const res = await postControl({ action: 'stop' });
                    state.engine = res.engine;
                    showBanner('Scanner paused.', 'warn');
                } else {
                    const feedMode = el.inputFeedMode.value;
                    const res = await postControl({ action: 'start', feed_mode: feedMode });
                    state.engine = res.engine;
                    showBanner(`Scanner active in ${feedMode} mode.`, 'success');
                    setTimeout(() => showBanner(''), 3000);
                }
                renderEngineStatus();
            } catch (err) {
                showBanner(err.message, 'error');
            } finally {
                el.btnToggleEngine.disabled = false;
            }
        });

        el.btnDryRun.addEventListener('click', async () => {
            el.btnDryRun.disabled = true;
            try {
                await postControl({ action: 'simulate_strike' });
                showBanner('Dry-run simulated flash-loan cycle completed.', 'success');
                setTimeout(() => showBanner(''), 3000);
            } catch (err) {
                showBanner(err.message, 'error');
            } finally {
                el.btnDryRun.disabled = false;
            }
        });

        el.settingsForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            el.btnSaveConfig.disabled = true;
            try {
                const res = await postControl({
                    action: 'update_settings',
                    feed_mode: el.inputFeedMode.value,
                    capital_usd: Number(el.inputCapital.value),
                    arbitrage_threshold_pct: Number(el.inputThreshold.value),
                    poll_interval_sec: Number(el.inputInterval.value),
                    auto_strike: el.inputAutoStrike.checked,
                });
                state.settings = res.settings;
                renderSettingsForm();
                renderChart();
                showBanner('Strategy parameters saved.', 'success');
                setTimeout(() => showBanner(''), 2500);
            } catch (err) {
                showBanner(err.message, 'error');
            } finally {
                el.btnSaveConfig.disabled = false;
            }
        });

        el.btnResetHistory.addEventListener('click', async () => {
            el.btnResetHistory.disabled = true;
            try {
                const res = await postControl({ action: 'reset_history' });
                applySnapshot(res);
                showBanner('Historical ticks and strike ledger cleared.', 'warn');
                setTimeout(() => showBanner(''), 2500);
            } catch (err) {
                showBanner(err.message, 'error');
            } finally {
                el.btnResetHistory.disabled = false;
            }
        });

        el.logFilter.addEventListener('change', (e) => {
            state.logFilter = e.target.value;
            applyLogFilter();
        });

        el.btnClearLogs.addEventListener('click', () => {
            el.terminal.replaceChildren();
            appendLog('Terminal display cleared.', 'INFO');
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        initChart();
        bindEvents();
        loadInitialState();
        connectWebSocket();
    });
})();
