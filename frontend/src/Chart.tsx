import { useEffect, useRef } from 'react';
import { CandlestickSeries, ColorType, HistogramSeries, LineSeries, LineStyle, TickMarkType, createChart, createSeriesMarkers, type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp } from 'lightweight-charts';
import { ZONES, type ChartData, type ZoneKey } from './api';

export type Overlay = { price: number; label: string; source: 'user' | 'algorithm' | 'calculated'; kind: 'entry' | 'stop' | 'target' | 'level' };
export type Toggles = { ema: boolean; vwap: boolean; swings: boolean; signals: boolean; rsi: boolean; macd: boolean };

const COLORS = { up: '#0f9d58', down: '#d93025', forming: '#9aa0a6', emaFast: '#f29900', emaSlow: '#1a73e8', vwap: '#a142f4', user: '#e37400', algorithm: '#1967d2', calculated: '#5f6368' };

function formatter(zone: ZoneKey, withDate: boolean) {
  const f = new Intl.DateTimeFormat('en-US', withDate ? { timeZone: ZONES[zone], month: 'short', day: '2-digit' } : { timeZone: ZONES[zone], hour: '2-digit', minute: '2-digit', hour12: false });
  return (t: Time) => f.format(new Date((t as number) * 1000));
}

// Draws exactly the candles in `data.candles`; indicators come from the same
// response, aligned by index, so nothing is recalculated in the browser.
export default function Chart({ data, zone, toggles, overlays = [] }: { data: ChartData; zone: ZoneKey; toggles: Toggles; overlays?: Overlay[] }) {
  const host = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const chart = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#ffffff' }, textColor: '#202124', panes: { separatorColor: '#dadce0' } },
      grid: { vertLines: { color: '#f1f3f4' }, horzLines: { color: '#f1f3f4' } },
      localization: { timeFormatter: (t: Time) => `${formatter(zone, true)(t)} ${formatter(zone, false)(t)}` },
      timeScale: { timeVisible: data.timeframe !== '1Day', secondsVisible: false, tickMarkFormatter: (t: Time, type: TickMarkType) => formatter(zone, data.timeframe === '1Day' || type < TickMarkType.Time)(t) },
    });
    chartRef.current = chart;
    const time = (t: number) => t as UTCTimestamp;
    const candles: ISeriesApi<'Candlestick'> = chart.addSeries(CandlestickSeries, { upColor: COLORS.up, downColor: COLORS.down, wickUpColor: COLORS.up, wickDownColor: COLORS.down, borderVisible: false });
    candles.setData(data.candles.map(c => ({ time: time(c.time), open: c.open, high: c.high, low: c.low, close: c.close, ...(c.complete ? {} : { color: COLORS.forming, wickColor: COLORS.forming, borderColor: COLORS.forming }) })));
    const volume = chart.addSeries(HistogramSeries, { priceScaleId: 'volume', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    volume.setData(data.candles.map(c => ({ time: time(c.time), value: c.volume, color: !c.complete ? '#dadce0' : c.close >= c.open ? '#a8dab5' : '#f6aea9' })));
    const line = (key: string, color: string, pane = 0, width: 1 | 2 = 1) => {
      const series = chart.addSeries(LineSeries, { color, lineWidth: width, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false }, pane);
      series.setData(data.candles.map((c, i) => (data.indicators[key][i] === null ? { time: time(c.time) } : { time: time(c.time), value: data.indicators[key][i] as number })));
      return series;
    };
    if (toggles.ema) { line('ema_fast', COLORS.emaFast); line('ema_slow', COLORS.emaSlow); }
    if (toggles.vwap && data.timeframe !== '1Day') line('vwap', COLORS.vwap);
    let pane = 1;
    if (toggles.rsi) {
      const rsi = line('rsi', '#7b1fa2', pane);
      rsi.createPriceLine({ price: data.config.rsi_overbought, color: '#bdc1c6', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: '' });
      rsi.createPriceLine({ price: data.config.rsi_oversold, color: '#bdc1c6', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: '' });
      pane += 1;
    }
    if (toggles.macd) {
      const hist = chart.addSeries(HistogramSeries, { lastValueVisible: false, priceLineVisible: false }, pane);
      hist.setData(data.candles.map((c, i) => { const v = data.indicators.macd_hist[i]; return v === null ? { time: time(c.time) } : { time: time(c.time), value: v, color: v >= 0 ? '#81c995' : '#f28b82' }; }));
      line('macd', '#1a73e8', pane);
      line('macd_signal', '#f29900', pane);
    }
    const markers: SeriesMarker<Time>[] = [];
    if (toggles.swings) {
      // Swings are drawn at the swing bar but only exist from their confirmation bar on.
      for (const s of data.swings) markers.push({ time: time(s.time), position: s.kind === 'high' ? 'aboveBar' : 'belowBar', shape: 'circle', color: '#5f6368', size: 0.6, text: s.kind === 'high' ? 'SH' : 'SL' });
    }
    if (toggles.signals) {
      for (const s of data.signals) {
        if (s.kind === 'breakout_unconfirmed_volume') continue;
        const bull = s.direction === 'bullish';
        const text = { breakout: s.variant === 'early' ? 'early' : 'BO', retest: 'RT', failed_breakout: 'FB' }[s.kind] ?? s.kind;
        markers.push({ time: time(s.time), position: bull ? 'belowBar' : 'aboveBar', shape: bull ? 'arrowUp' : 'arrowDown', color: s.variant === 'early' ? '#9aa0a6' : bull ? COLORS.up : COLORS.down, text });
      }
    }
    for (const a of data.corporate_actions) {
      const at = data.candles.find(c => c.session_date >= a.date);
      if (at) markers.push({ time: time(at.time), position: 'aboveBar', shape: 'square', color: '#b06000', text: 'CA' });
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    createSeriesMarkers(candles, markers);
    for (const o of overlays) {
      candles.createPriceLine({ price: o.price, color: COLORS[o.source], lineWidth: o.kind === 'level' ? 1 : 2, lineStyle: o.source === 'user' ? LineStyle.Solid : LineStyle.Dashed, axisLabelVisible: true, title: `${o.source === 'user' ? 'You' : o.source === 'algorithm' ? 'Algo' : 'Calc'}: ${o.label}` });
    }
    chart.timeScale().fitContent();
    return () => { chart.remove(); chartRef.current = null; };
  }, [data, zone, toggles, overlays]);

  return <div className="chart" ref={host} role="img" aria-label={`${data.symbol} ${data.timeframe} candlestick chart`} />;
}
