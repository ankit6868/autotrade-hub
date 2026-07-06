// TradingView-style trade "boxes" for lightweight-charts — a cosmetic overlay.
// A box spans entry-time → exit-time (or now) horizontally and SL ↔ TP
// vertically, with a dashed entry line and a small label. Pure display: it
// reads trade/formation data and draws; it never affects any trading logic.
//
// Implemented as an ISeriesPrimitive (lightweight-charts v4.1+). Attach once to
// the candlestick series, then call setBoxes(...) whenever the data changes.

import type {
  ISeriesPrimitive,
  Time,
  IChartApi,
  ISeriesApi,
  SeriesAttachedParameter,
} from 'lightweight-charts';

export interface TradeBox {
  t1: Time;            // entry time (unix seconds)
  t2: Time;            // exit time, or the latest bar time for open/pending
  entry: number;
  top: number;         // max(sl, tp)
  bottom: number;      // min(sl, tp)
  fill: string;
  border: string;
  entryColor?: string;
  label?: string;
  labelColor?: string;
  dashed?: boolean;    // pending setups render with a dashed border
}

class BoxRenderer {
  constructor(private readonly source: TradeBoxPrimitive) {}

  draw(target: {
    useBitmapCoordinateSpace: (
      cb: (scope: {
        context: CanvasRenderingContext2D;
        horizontalPixelRatio: number;
        verticalPixelRatio: number;
      }) => void,
    ) => void;
  }) {
    const src = this.source;
    const series = src.series;
    const chart = src.chart;
    if (!series || !chart) return;
    const ts = chart.timeScale();

    target.useBitmapCoordinateSpace(scope => {
      const ctx = scope.context;
      const hpr = scope.horizontalPixelRatio;
      const vpr = scope.verticalPixelRatio;

      for (const b of src.boxes) {
        const x1 = ts.timeToCoordinate(b.t1);
        const x2 = ts.timeToCoordinate(b.t2);
        const yTop = series.priceToCoordinate(b.top);
        const yBot = series.priceToCoordinate(b.bottom);
        if (x1 === null || x2 === null || yTop === null || yBot === null) continue;

        const left = Math.round(Math.min(x1, x2) * hpr);
        const width = Math.max(Math.round(Math.abs(x2 - x1) * hpr), 2);
        const top = Math.round(yTop * vpr);
        const height = Math.max(Math.round((yBot - yTop) * vpr), 2);

        // Fill + border.
        ctx.fillStyle = b.fill;
        ctx.fillRect(left, top, width, height);
        ctx.lineWidth = Math.max(1, Math.round(vpr));
        ctx.strokeStyle = b.border;
        if (b.dashed) ctx.setLineDash([4 * hpr, 3 * hpr]);
        ctx.strokeRect(left, top, width, height);
        ctx.setLineDash([]);

        // Dashed entry line inside the box.
        const yE = series.priceToCoordinate(b.entry);
        if (yE !== null) {
          const ey = Math.round(yE * vpr);
          ctx.beginPath();
          ctx.strokeStyle = b.entryColor || 'rgba(96,165,250,0.9)';
          ctx.setLineDash([2 * hpr, 2 * hpr]);
          ctx.moveTo(left, ey);
          ctx.lineTo(left + width, ey);
          ctx.stroke();
          ctx.setLineDash([]);
        }

        // Label at the top-left of the box.
        if (b.label) {
          ctx.font = `${Math.round(10 * vpr)}px -apple-system, system-ui, sans-serif`;
          ctx.fillStyle = b.labelColor || '#e6eaf2';
          ctx.textBaseline = 'top';
          ctx.fillText(b.label, left + 4 * hpr, top + 3 * vpr);
        }
      }
    });
  }
}

class BoxPaneView {
  private readonly _renderer: BoxRenderer;
  constructor(source: TradeBoxPrimitive) {
    this._renderer = new BoxRenderer(source);
  }
  renderer() {
    return this._renderer;
  }
  zOrder() {
    return 'normal' as const;
  }
}

export class TradeBoxPrimitive implements ISeriesPrimitive<Time> {
  series: ISeriesApi<'Candlestick'> | null = null;
  chart: IChartApi | null = null;
  boxes: TradeBox[] = [];
  private readonly _paneViews: BoxPaneView[];
  private _requestUpdate?: () => void;

  constructor() {
    this._paneViews = [new BoxPaneView(this)];
  }

  attached(param: SeriesAttachedParameter<Time>) {
    this.series = param.series as ISeriesApi<'Candlestick'>;
    this.chart = param.chart;
    this._requestUpdate = param.requestUpdate;
  }

  detached() {
    this.series = null;
    this.chart = null;
    this._requestUpdate = undefined;
  }

  updateAllViews() {}

  paneViews() {
    return this._paneViews;
  }

  setBoxes(boxes: TradeBox[]) {
    this.boxes = boxes;
    this._requestUpdate?.();
  }
}
