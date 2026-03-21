function renderChart(sym, interval) {
    const container = document.getElementById('tv-chart-container');
    container.innerHTML = '';

    // Auto-detect price precision
    function getPrecision(price) {
        if (price >= 100) return 2;
        if (price >= 1) return 4;
        if (price >= 0.01) return 6;
        if (price >= 0.0001) return 8;
        return 10;
    }
    function getMinMove(price) {
        var p = getPrecision(price);
        return parseFloat('1e-' + p);
    }

    // Toolbar
    const toolbar = document.createElement('div');
    toolbar.style.cssText = 'display:flex;gap:4px;padding:6px 10px;background:#fff;border-bottom:1px solid #e0e3eb;align-items:center;flex-shrink:0;flex-wrap:wrap;';
    const intervals = [['1m', '1分'], ['15m', '15分'], ['1h', '1小时'], ['4h', '4小时'], ['1d', '日线']];
    intervals.forEach(function (item) {
        var val = item[0], label = item[1];
        var btn = document.createElement('button');
        btn.textContent = label;
        btn.style.cssText = 'padding:3px 10px;border:1px solid ' + (val === interval ? '#2962FF' : '#e0e3eb') + ';background:' + (val === interval ? '#2962FF' : '#f8f9fd') + ';color:' + (val === interval ? '#fff' : '#131722') + ';border-radius:4px;cursor:pointer;font-size:12px;';
        btn.onclick = function () { switchInterval(val); };
        toolbar.appendChild(btn);
    });
    // Separator
    var sep1 = document.createElement('span');
    sep1.style.cssText = 'width:1px;height:18px;background:#e0e3eb;margin:0 4px;';
    toolbar.appendChild(sep1);
    // Indicator buttons
    var indBtns = ['\u2197 BB', '\u2193 MACD', '\ud83d\udcca Vol'];
    indBtns.forEach(function (label) {
        var ibtn = document.createElement('span');
        ibtn.textContent = label;
        ibtn.style.cssText = 'padding:2px 8px;font-size:11px;color:#2962FF;cursor:default;background:#f0f4ff;border-radius:3px;';
        toolbar.appendChild(ibtn);
    });
    var infoSpan = document.createElement('span');
    infoSpan.style.cssText = 'margin-left:auto;color:#787B86;font-size:11px;';
    infoSpan.textContent = sym + ' \u00b7 Binance Futures \u00b7 ' + intervals.find(function (i) { return i[0] === interval; })[1];
    toolbar.appendChild(infoSpan);
    container.appendChild(toolbar);

    // Info overlay
    var infoOverlay = document.createElement('div');
    infoOverlay.style.cssText = 'padding:3px 10px;background:#fff;font-size:11px;color:#787B86;flex-shrink:0;line-height:1.5;';
    infoOverlay.id = 'chart-info-overlay';
    infoOverlay.innerHTML = '<span style="color:#131722;font-weight:600">' + sym + '</span> loading...';
    container.appendChild(infoOverlay);

    // Main chart area (75%)
    var mainArea = document.createElement('div');
    mainArea.style.cssText = 'flex:3;position:relative;min-height:0;';
    container.appendChild(mainArea);

    // Divider
    var divider = document.createElement('div');
    divider.style.cssText = 'height:1px;background:#e0e3eb;flex-shrink:0;';
    container.appendChild(divider);

    // MACD label
    var macdLabel = document.createElement('div');
    macdLabel.style.cssText = 'padding:2px 10px;background:#fff;font-size:10px;color:#787B86;flex-shrink:0;';
    macdLabel.id = 'macd-info-label';
    macdLabel.innerHTML = 'MACD 12 26 close 9';
    container.appendChild(macdLabel);

    // MACD area (25%)
    var macdArea = document.createElement('div');
    macdArea.style.cssText = 'flex:1;position:relative;min-height:0;';
    container.appendChild(macdArea);

    mainArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#787B86">加载K线数据中...</div>';

    fetch('/api/binance/klines?symbol=' + sym + '&interval=' + interval + '&limit=500')
        .then(function (res) { return res.json(); })
        .then(function (data) {
            if (!data || !data.length) {
                mainArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#ef5350">K线数据加载失败</div>';
                return;
            }
            mainArea.innerHTML = '';

            var samplePrice = parseFloat(data[data.length - 1][4]);
            var pricePrecision = getPrecision(samplePrice);
            var priceMinMove = getMinMove(samplePrice);

            var chartOpts = {
                layout: { background: { type: 'solid', color: '#ffffff' }, textColor: '#131722', fontSize: 11 },
                grid: { vertLines: { color: '#f0f3fa' }, horzLines: { color: '#f0f3fa' } },
                crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
                rightPriceScale: { borderColor: '#e0e3eb', autoScale: true },
                timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#e0e3eb' },
            };

            // === MAIN CHART ===
            var mainChart = LightweightCharts.createChart(mainArea, Object.assign({}, chartOpts, { width: mainArea.clientWidth, height: mainArea.clientHeight }));

            var candles = data.map(function (k) {
                return {
                    time: Math.floor(k[0] / 1000),
                    open: parseFloat(k[1]), high: parseFloat(k[2]),
                    low: parseFloat(k[3]), close: parseFloat(k[4]),
                };
            });
            var closes = candles.map(function (c) { return c.close; });

            // Candlestick with correct precision
            var candleSeries = mainChart.addCandlestickSeries({
                upColor: '#26a69a', downColor: '#ef5350',
                borderUpColor: '#26a69a', borderDownColor: '#ef5350',
                wickUpColor: '#26a69a', wickDownColor: '#ef5350',
                priceFormat: { type: 'price', precision: pricePrecision, minMove: priceMinMove },
            });
            candleSeries.setData(candles);

            // Bollinger Bands (20, 2)
            var bbPeriod = 20, bbStdDev = 2;
            var bbUpper = [], bbMiddle = [], bbLower = [];
            for (var i = 0; i < candles.length; i++) {
                if (i < bbPeriod - 1) continue;
                var slice = closes.slice(i - bbPeriod + 1, i + 1);
                var mean = slice.reduce(function (a, b) { return a + b; }, 0) / bbPeriod;
                var std = Math.sqrt(slice.reduce(function (s, v) { return s + Math.pow(v - mean, 2); }, 0) / bbPeriod);
                var t = candles[i].time;
                bbUpper.push({ time: t, value: mean + bbStdDev * std });
                bbMiddle.push({ time: t, value: mean });
                bbLower.push({ time: t, value: mean - bbStdDev * std });
            }

            // BB fill - upper area (light blue fill from upper band down)
            var bbAreaUpper = mainChart.addAreaSeries({
                topColor: 'rgba(33, 150, 243, 0.15)',
                bottomColor: 'rgba(33, 150, 243, 0.0)',
                lineColor: 'rgba(33, 150, 243, 0.6)',
                lineWidth: 1,
                priceLineVisible: false,
                lastValueVisible: false,
                crosshairMarkerVisible: false,
            });
            bbAreaUpper.setData(bbUpper);

            // BB fill - lower area (to create the fill effect)
            var bbAreaLower = mainChart.addAreaSeries({
                topColor: 'rgba(33, 150, 243, 0.0)',
                bottomColor: 'rgba(33, 150, 243, 0.15)',
                lineColor: 'rgba(33, 150, 243, 0.6)',
                lineWidth: 1,
                priceLineVisible: false,
                lastValueVisible: false,
                crosshairMarkerVisible: false,
            });
            bbAreaLower.setData(bbLower);

            // BB middle line
            mainChart.addLineSeries({
                color: 'rgba(255, 152, 0, 0.7)',
                lineWidth: 1,
                priceLineVisible: false,
                lastValueVisible: false,
                crosshairMarkerVisible: false,
            }).setData(bbMiddle);

            // Volume
            var volumeSeries = mainChart.addHistogramSeries({
                priceFormat: { type: 'volume' },
                priceScaleId: 'vol',
            });
            mainChart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
            volumeSeries.setData(data.map(function (k) {
                return {
                    time: Math.floor(k[0] / 1000),
                    value: parseFloat(k[5]),
                    color: parseFloat(k[4]) >= parseFloat(k[1]) ? 'rgba(38,166,154,0.35)' : 'rgba(239,83,80,0.35)',
                };
            }));

            // Update info overlay
            var last = candles[candles.length - 1];
            var lastBB = bbUpper.length > 0 ? { u: bbUpper[bbUpper.length - 1].value, m: bbMiddle[bbMiddle.length - 1].value, l: bbLower[bbLower.length - 1].value } : null;
            var chg = last.close - last.open;
            var chgPct = ((chg / last.open) * 100).toFixed(2);
            var chgColor = chg >= 0 ? '#26a69a' : '#ef5350';
            var fp = function (v) { return v.toFixed(pricePrecision); };
            var infoHTML = '<span style="color:#131722;font-weight:600">\u25cf ' + sym + ' / TetherUS PERPETUAL \u00b7 Binance</span>';
            infoHTML += ' &nbsp; <span style="color:#131722">' + fp(last.close) + '</span>';
            infoHTML += ' <span style="color:' + chgColor + '">' + (chg >= 0 ? '+' : '') + fp(chg) + ' (' + (chg >= 0 ? '+' : '') + chgPct + '%)</span>';
            infoHTML += '<br><span style="color:#787B86">开=</span>' + fp(last.open);
            infoHTML += ' <span style="color:#787B86">高=</span>' + fp(last.high);
            infoHTML += ' <span style="color:#787B86">低=</span>' + fp(last.low);
            infoHTML += ' <span style="color:#787B86">收=</span><span style="color:' + chgColor + '">' + fp(last.close) + '</span>';
            if (lastBB) {
                infoHTML += '<br><span style="color:#787B86">BB 20 close 2</span> <span style="color:#ef5350">' + fp(lastBB.u) + '</span> <span style="color:#FF9800">' + fp(lastBB.m) + '</span> <span style="color:#2196F3">' + fp(lastBB.l) + '</span>';
            }
            var volLast = parseFloat(data[data.length - 1][5]);
            var volStr = volLast >= 1e6 ? (volLast / 1e6).toFixed(2) + 'M' : (volLast / 1e3).toFixed(2) + 'K';
            infoHTML += ' &nbsp; <span style="color:#787B86">Vol</span> <span style="color:' + chgColor + '">' + volStr + '</span>';
            document.getElementById('chart-info-overlay').innerHTML = infoHTML;

            mainChart.timeScale().fitContent();

            // === MACD CHART ===
            var macdChart = LightweightCharts.createChart(macdArea, Object.assign({}, chartOpts, {
                width: macdArea.clientWidth, height: macdArea.clientHeight,
                rightPriceScale: { borderColor: '#e0e3eb', autoScale: true },
            }));

            function ema(arr, period) {
                var k = 2 / (period + 1);
                var res = [arr[0]];
                for (var i = 1; i < arr.length; i++) res.push(arr[i] * k + res[i - 1] * (1 - k));
                return res;
            }
            var ema12 = ema(closes, 12), ema26 = ema(closes, 26);
            var macdLineArr = closes.map(function (_, i) { return ema12[i] - ema26[i]; });
            var signalLineArr = ema(macdLineArr, 9);
            var macdHistArr = macdLineArr.map(function (v, i) { return v - signalLineArr[i]; });

            // MACD precision
            var macdSample = Math.abs(macdLineArr[macdLineArr.length - 1]);
            var macdPrec = getPrecision(macdSample);
            var macdMM = getMinMove(macdSample);

            var macdHistSeries = macdChart.addHistogramSeries({
                priceFormat: { type: 'price', precision: macdPrec, minMove: macdMM },
            });
            macdHistSeries.setData(candles.map(function (c, i) {
                return {
                    time: c.time, value: macdHistArr[i],
                    color: macdHistArr[i] >= 0 ? 'rgba(38,166,154,0.7)' : 'rgba(239,83,80,0.7)',
                };
            }));

            macdChart.addLineSeries({
                color: '#2962FF', lineWidth: 1,
                priceLineVisible: false, lastValueVisible: true,
                priceFormat: { type: 'price', precision: macdPrec, minMove: macdMM },
            }).setData(candles.map(function (c, i) { return { time: c.time, value: macdLineArr[i] }; }));

            macdChart.addLineSeries({
                color: '#FF6D00', lineWidth: 1,
                priceLineVisible: false, lastValueVisible: true,
                priceFormat: { type: 'price', precision: macdPrec, minMove: macdMM },
            }).setData(candles.map(function (c, i) { return { time: c.time, value: signalLineArr[i] }; }));

            macdChart.timeScale().fitContent();

            // Update MACD label
            var lm = macdLineArr[macdLineArr.length - 1];
            var ls = signalLineArr[signalLineArr.length - 1];
            var lh = macdHistArr[macdHistArr.length - 1];
            document.getElementById('macd-info-label').innerHTML = 'MACD 12 26 close 9 &nbsp; <span style="color:' + (lh >= 0 ? '#26a69a' : '#ef5350') + '">' + lh.toFixed(macdPrec) + '</span> <span style="color:#2962FF">' + lm.toFixed(macdPrec) + '</span> <span style="color:#FF6D00">' + ls.toFixed(macdPrec) + '</span>';

            // Sync time scales
            mainChart.timeScale().subscribeVisibleLogicalRangeChange(function (range) {
                if (range) macdChart.timeScale().setVisibleLogicalRange(range);
            });
            macdChart.timeScale().subscribeVisibleLogicalRangeChange(function (range) {
                if (range) mainChart.timeScale().setVisibleLogicalRange(range);
            });

            // Responsive
            var ro = new ResizeObserver(function () {
                mainChart.applyOptions({ width: mainArea.clientWidth, height: mainArea.clientHeight });
                macdChart.applyOptions({ width: macdArea.clientWidth, height: macdArea.clientHeight });
            });
            ro.observe(mainArea);
            ro.observe(macdArea);
        })
        .catch(function (err) {
            mainArea.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#ef5350">K线数据请求失败: ' + err.message + '</div>';
        });
}

