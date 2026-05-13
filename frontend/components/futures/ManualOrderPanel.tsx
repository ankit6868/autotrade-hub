'use client';
import { useState, useRef, useEffect } from 'react';
import { api } from '@/lib/api';
import LeverageModal from './LeverageModal';

interface Props {
  symbol: string;
  pair: string;
  mode: 'paper' | 'live';
  leverage: number;
  marginMode: string;
  availableBalance: number;
  lastPrice?: number;
  onLeverageChange: (lev: number) => void;
  onMarginModeChange: (mode: string) => void;
  onOrderPlaced: () => void;
  onPriceSet?: (price: string) => void;
}

// Unified order type — covers both basic tabs and advanced dropdown items
type OrderType = 'limit' | 'market' | 'conditional' | 'advanced_limit' | 'trailing_stop' | 'hidden' | 'twap';

const BASIC_TABS: { key: OrderType; label: string }[] = [
  { key: 'limit', label: 'Limit' },
  { key: 'market', label: 'Market' },
  { key: 'conditional', label: 'Conditional' },
];

const ADVANCED_ITEMS: { key: OrderType; label: string }[] = [
  { key: 'advanced_limit', label: 'Advanced Limit' },
  { key: 'trailing_stop', label: 'Trailing Stop' },
  { key: 'hidden', label: 'Hidden Order' },
  { key: 'twap', label: 'TWAP' },
];

const ALL_TYPES = [...BASIC_TABS, ...ADVANCED_ITEMS];

export default function ManualOrderPanel({
  symbol, pair, mode, leverage, marginMode, availableBalance, lastPrice,
  onLeverageChange, onMarginModeChange, onOrderPlaced,
}: Props) {
  const [leadStatus, setLeadStatus] = useState<{ connected: boolean; account_type?: string; balance?: number; equity?: number } | null>(null);

  useEffect(() => {
    api.futures.leadTradingStatus()
      .then(d => setLeadStatus(d))
      .catch(() => setLeadStatus(null));
  }, []);

  const [orderType, setOrderType] = useState<OrderType>('limit');
  const [showAdvancedMenu, setShowAdvancedMenu] = useState(false);
  const [leverageModal, setLeverageModal] = useState(false);
  const [showMarginDropdown, setShowMarginDropdown] = useState(false);

  // Common fields
  const [price, setPrice] = useState('');
  const [amount, setAmount] = useState('');
  const [costMode, setCostMode] = useState(true);
  const [costUsdt, setCostUsdt] = useState('');
  const [stopPrice, setStopPrice] = useState('');
  const [tpEnabled, setTpEnabled] = useState(false);
  const [slEnabled, setSlEnabled] = useState(false);
  const [tpPrice, setTpPrice] = useState('');
  const [slPrice, setSlPrice] = useState('');
  const [postOnly, setPostOnly] = useState(false);
  const [reduceOnly, setReduceOnly] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [sliderValue, setSliderValue] = useState(0);

  // Advanced Limit fields
  const [timeInForce, setTimeInForce] = useState<'GTC' | 'IOC' | 'FOK'>('GTC');

  // Trailing Stop fields
  const [callbackRate, setCallbackRate] = useState('');
  const [activationPrice, setActivationPrice] = useState('');

  // TWAP fields
  const [twapDuration, setTwapDuration] = useState('60');   // minutes
  const [twapSlices, setTwapSlices] = useState('10');
  const [twapPriceLimit, setTwapPriceLimit] = useState('');

  const marginRef = useRef<HTMLDivElement>(null);
  const advancedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (marginRef.current && !marginRef.current.contains(e.target as Node)) {
        setShowMarginDropdown(false);
      }
      if (advancedRef.current && !advancedRef.current.contains(e.target as Node)) {
        setShowAdvancedMenu(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Clear success message after 3s
  useEffect(() => {
    if (success) {
      const t = setTimeout(() => setSuccess(''), 3000);
      return () => clearTimeout(t);
    }
  }, [success]);

  const baseCoin = pair.split('/')[0];
  const effectiveRef = parseFloat(price) > 0 ? parseFloat(price) : (lastPrice || 1);
  const priceNum = parseFloat(price) || 0;
  const amountNum = costMode
    ? (parseFloat(costUsdt) || 0) / effectiveRef
    : (parseFloat(amount) || 0);
  const costUsdt_ = costMode ? (parseFloat(costUsdt) || 0) : amountNum * effectiveRef;
  const marginCost = costUsdt_ / leverage;

  const refPrice = priceNum > 0 ? priceNum : (lastPrice || 0);
  const maxLongAmount = refPrice > 0 ? (availableBalance * leverage) / refPrice : 0;
  const maxShortAmount = maxLongAmount;

  // Is this an advanced type shown via dropdown?
  const isAdvancedType = ['advanced_limit', 'trailing_stop', 'hidden', 'twap'].includes(orderType);
  const activeLabel = ALL_TYPES.find(t => t.key === orderType)?.label || 'Limit';

  // Does this order type need a price field?
  const needsPrice = orderType !== 'market' && orderType !== 'trailing_stop';
  // Does this order type need a stop/trigger price?
  const needsStopPrice = orderType === 'conditional' || orderType === 'trailing_stop';

  async function placeOrder(side: 'buy' | 'sell') {
    setSubmitting(true);
    setError('');
    setSuccess('');
    try {
      if (orderType === 'market') {
        // Market order — uses manual entry endpoint
        const direction = side === 'buy' ? 'long' : 'short';
        const stakePct = availableBalance > 0
          ? (costUsdt_ / availableBalance) * 100
          : 5;
        if (stakePct <= 0) { setError('Enter an amount'); setSubmitting(false); return; }
        const r = await api.futures.manualEntry(pair, direction, Math.min(stakePct, 100), leverage, mode);
        if (r.error) setError(r.error);
        else {
          setSuccess(`${direction.toUpperCase()} market order placed at ${r.entry}`);
          onOrderPlaced();
          resetForm();
        }
      } else if (orderType === 'twap') {
        // TWAP: split into multiple smaller market orders over time
        const slices = parseInt(twapSlices) || 10;
        const totalCostUsdt = costUsdt_;
        if (totalCostUsdt <= 0) { setError('Enter an amount'); setSubmitting(false); return; }
        const direction = side === 'buy' ? 'long' : 'short';
        const perSliceStakePct = availableBalance > 0
          ? ((totalCostUsdt / slices) / availableBalance) * 100
          : 1;
        // Place first slice immediately
        const r = await api.futures.manualEntry(pair, direction, Math.min(perSliceStakePct, 100), leverage, mode);
        if (r.error) setError(r.error);
        else {
          setSuccess(`TWAP: Slice 1/${slices} placed. Remaining slices queued.`);
          onOrderPlaced();
          // Queue remaining slices via interval (client-side TWAP)
          const intervalMs = ((parseInt(twapDuration) || 60) * 60 * 1000) / slices;
          let sliceCount = 1;
          const interval = setInterval(async () => {
            sliceCount++;
            if (sliceCount > slices) { clearInterval(interval); return; }
            try {
              await api.futures.manualEntry(pair, direction, Math.min(perSliceStakePct, 100), leverage, mode);
            } catch { /* silent */ }
          }, intervalMs);
          resetForm();
        }
      } else {
        // Limit, Conditional, Advanced Limit, Trailing Stop, Hidden
        if (orderType !== 'trailing_stop' && priceNum <= 0) {
          setError('Enter a valid price');
          setSubmitting(false);
          return;
        }
        if (amountNum <= 0 && orderType !== 'trailing_stop') {
          setError('Enter an amount');
          setSubmitting(false);
          return;
        }

        const futSymbol = symbol.includes('USDTM') ? symbol : symbol.replace('/', '').replace('USDT', 'USDTM');

        // Build order payload
        // Send cost_usdt so the backend can calculate proper lot/position size
        // (amountNum is BTC — too small for int() conversion on KuCoin)
        const orderPayload: Record<string, unknown> = {
          symbol: futSymbol,
          side,
          size: amountNum,
          cost_usdt: costUsdt_,
          leverage,
          reduce_only: reduceOnly,
          mode,
        };

        // TP/SL
        if (tpEnabled && tpPrice) orderPayload.tp_price = parseFloat(tpPrice);
        if (slEnabled && slPrice) orderPayload.sl_price = parseFloat(slPrice);

        switch (orderType) {
          case 'limit':
            orderPayload.order_type = 'limit';
            orderPayload.price = priceNum;
            orderPayload.post_only = postOnly;
            orderPayload.time_in_force = 'GTC';
            break;

          case 'conditional':
            orderPayload.order_type = 'stop';
            orderPayload.price = priceNum;
            orderPayload.stop_price = parseFloat(stopPrice) || priceNum;
            break;

          case 'advanced_limit':
            orderPayload.order_type = 'limit';
            orderPayload.price = priceNum;
            orderPayload.post_only = postOnly;
            orderPayload.time_in_force = timeInForce;
            break;

          case 'trailing_stop': {
            // Trailing stop: use conditional order with callback
            const cbRate = parseFloat(callbackRate) || 1;
            const actPrice = parseFloat(activationPrice) || (lastPrice || 0);
            const trailStopPrice = side === 'buy'
              ? actPrice * (1 + cbRate / 100)
              : actPrice * (1 - cbRate / 100);
            // Use manual entry stake approach for trailing stop sizing
            const trailStakePct = availableBalance > 0
              ? (costUsdt_ / availableBalance) * 100
              : 5;
            if (trailStakePct <= 0) { setError('Enter an amount'); setSubmitting(false); return; }
            orderPayload.order_type = 'stop';
            orderPayload.price = actPrice;
            orderPayload.stop_price = trailStopPrice;
            orderPayload.size = costUsdt_ / (actPrice || 1);
            break;
          }

          case 'hidden':
            orderPayload.order_type = 'limit';
            orderPayload.price = priceNum;
            orderPayload.hidden = true;
            orderPayload.post_only = postOnly;
            orderPayload.time_in_force = timeInForce;
            break;
        }

        const r = await api.futures.placeOrder(orderPayload);
        if (r.error) setError(r.error);
        else {
          setSuccess(`${activeLabel} ${side.toUpperCase()} order placed successfully`);
          onOrderPlaced();
          resetForm();
        }
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setSubmitting(false);
  }

  function resetForm() {
    setCostUsdt('');
    setAmount('');
    setSliderValue(0);
    setStopPrice('');
    setCallbackRate('');
    setActivationPrice('');
  }

  function handleSliderChange(pct: number) {
    setSliderValue(pct);
    if (availableBalance > 0) {
      const usdtCost = availableBalance * pct / 100;
      if (costMode) {
        setCostUsdt(usdtCost.toFixed(2));
      } else {
        const ref = effectiveRef > 0 ? effectiveRef : 1;
        const btcAmount = (usdtCost * leverage) / ref;
        setAmount(btcAmount.toFixed(6));
      }
    }
  }

  function fillLastPrice() {
    if (lastPrice) setPrice(lastPrice.toString());
  }

  function handleOrderTypeChange(key: OrderType) {
    setOrderType(key);
    setShowAdvancedMenu(false);
    setError('');
    setSuccess('');
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Lead Trading / Paper Account badge */}
      <div className={`flex items-center justify-between px-3 py-2 text-xs font-bold border-b ${
        mode === 'paper'
          ? 'bg-indigo-500/20 border-indigo-500/30'
          : leadStatus?.connected
            ? 'bg-emerald-500/20 border-emerald-500/30'
            : 'bg-amber-500/20 border-amber-500/30'
      }`}>
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full shrink-0 ${
            mode === 'paper'
              ? 'bg-indigo-400'
              : leadStatus?.connected
                ? 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.6)]'
                : 'bg-amber-400'
          }`} />
          <span className={
            mode === 'paper' ? 'text-indigo-300'
              : leadStatus?.connected ? 'text-emerald-300' : 'text-amber-300'
          }>
            {mode === 'paper'
              ? 'Paper Trading Account'
              : leadStatus?.connected
                ? 'Lead Trading Account'
                : 'Lead Trading: Not Connected'}
          </span>
        </div>
        {mode === 'paper' ? (
          <span className="text-[11px] text-indigo-300 font-medium">{availableBalance.toFixed(2)} USDT</span>
        ) : leadStatus?.connected && leadStatus.balance != null ? (
          <span className="text-[11px] text-emerald-300 font-medium">{leadStatus.balance.toFixed(2)} USDT</span>
        ) : null}
      </div>

      {/* Cross/Isolated + Leverage row */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/[0.06]">
        <div className="relative" ref={marginRef}>
          <button
            onClick={() => setShowMarginDropdown(!showMarginDropdown)}
            className="flex items-center gap-1 text-xs text-white"
          >
            <span className={`w-2 h-2 rounded-full inline-block ${marginMode === 'cross' ? 'bg-blue-400' : 'bg-emerald-400'}`} />
            <span className="capitalize font-medium">{marginMode}</span>
            <svg className="w-3 h-3 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
          </button>
          {showMarginDropdown && (
            <div className="absolute top-full left-0 mt-1 z-30 bg-[#1e222d] border border-white/[0.1] rounded-lg shadow-xl py-1 min-w-[140px]">
              {['cross', 'isolated'].map(m => (
                <button
                  key={m}
                  onClick={() => { onMarginModeChange(m); setShowMarginDropdown(false); }}
                  className={`block w-full text-left px-3 py-2 text-xs capitalize ${marginMode === m ? 'text-emerald-400 bg-white/[0.04]' : 'text-slate-300 hover:bg-white/[0.06]'}`}
                >
                  <span className="flex items-center gap-2">
                    <span className={`w-1.5 h-1.5 rounded-full ${m === 'cross' ? 'bg-blue-400' : 'bg-emerald-400'}`} />
                    {m}
                    {marginMode === m && <span className="ml-auto text-emerald-400">&#10003;</span>}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        <button
          onClick={() => setLeverageModal(true)}
          className="px-2 py-0.5 rounded bg-slate-700/80 text-emerald-400 text-xs font-bold hover:bg-slate-600 border border-white/[0.06]"
        >
          {leverage}.00x
        </button>

        <div className="ml-auto">
          <button className="text-slate-500 hover:text-white">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" /><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" /></svg>
          </button>
        </div>
      </div>

      {/* Order type tabs */}
      <div className="flex items-center px-3 py-1.5 border-b border-white/[0.06] relative">
        <div className="flex items-center gap-0.5">
          {BASIC_TABS.map(t => (
            <button
              key={t.key}
              onClick={() => handleOrderTypeChange(t.key)}
              className={`px-2 py-1 text-xs font-medium transition-colors ${
                orderType === t.key
                  ? 'text-white border-b-2 border-emerald-500'
                  : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        {/* Advanced dropdown trigger */}
        <div className="relative" ref={advancedRef}>
          <button
            onClick={() => setShowAdvancedMenu(!showAdvancedMenu)}
            className={`ml-1 px-1.5 py-1 text-xs flex items-center gap-0.5 transition-colors ${
              isAdvancedType
                ? 'text-emerald-400 font-medium'
                : 'text-slate-500 hover:text-white'
            }`}
          >
            {isAdvancedType ? activeLabel : ''}
            <span className="text-[10px]">&#9662;</span>
          </button>

          {showAdvancedMenu && (
            <div className="absolute top-full left-0 z-20 mt-1 bg-[#1e222d] border border-white/[0.1] rounded-lg shadow-xl py-1 min-w-[160px]">
              {ADVANCED_ITEMS.map(a => (
                <button
                  key={a.key}
                  onClick={() => handleOrderTypeChange(a.key)}
                  className={`block w-full text-left px-3 py-2 text-xs transition-colors ${
                    orderType === a.key
                      ? 'text-emerald-400 bg-white/[0.04]'
                      : 'text-slate-300 hover:bg-white/[0.06]'
                  }`}
                >
                  <span className="flex items-center justify-between">
                    {a.label}
                    {orderType === a.key && <span className="text-emerald-400">&#10003;</span>}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="ml-auto">
          <button className="text-slate-500 hover:text-white text-xs" title="Order type help">?</button>
        </div>
      </div>

      {/* Order form */}
      <div className="flex-1 overflow-y-auto px-3 py-2.5 space-y-3">
        {/* Price field — shown for all except Market and Trailing Stop */}
        {needsPrice && (
          <div>
            <label className="text-[10px] text-slate-500 mb-1 block">
              {orderType === 'conditional' ? 'Limit Price' : 'Price'}
            </label>
            <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
              <input
                type="number"
                value={price}
                onChange={e => setPrice(e.target.value)}
                placeholder="0.00"
                className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none min-w-0"
              />
              <div className="flex items-center gap-1.5 pr-2 shrink-0">
                <button
                  onClick={fillLastPrice}
                  className="text-[10px] text-emerald-400 hover:text-emerald-300 font-medium"
                >
                  Last
                </button>
                <span className="text-[10px] text-slate-500">USDT</span>
                {orderType === 'limit' && (
                  <button
                    onClick={fillLastPrice}
                    className="text-[10px] text-slate-400 hover:text-white font-medium px-1 py-0.5 rounded bg-slate-700/50"
                  >
                    BBO
                  </button>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Stop / Trigger Price — shown for Conditional and Trailing Stop */}
        {orderType === 'conditional' && (
          <div>
            <label className="text-[10px] text-slate-500 mb-1 block">Trigger Price</label>
            <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
              <input
                type="number"
                value={stopPrice}
                onChange={e => setStopPrice(e.target.value)}
                placeholder="0.00"
                className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
              />
              <span className="text-[10px] text-slate-500 pr-2">USDT</span>
            </div>
          </div>
        )}

        {/* Trailing Stop specific fields */}
        {orderType === 'trailing_stop' && (
          <>
            <div>
              <label className="text-[10px] text-slate-500 mb-1 block">Activation Price</label>
              <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
                <input
                  type="number"
                  value={activationPrice}
                  onChange={e => setActivationPrice(e.target.value)}
                  placeholder={lastPrice ? lastPrice.toString() : '0.00'}
                  className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
                />
                <div className="flex items-center gap-1.5 pr-2 shrink-0">
                  <button
                    onClick={() => lastPrice && setActivationPrice(lastPrice.toString())}
                    className="text-[10px] text-emerald-400 hover:text-emerald-300 font-medium"
                  >
                    Last
                  </button>
                  <span className="text-[10px] text-slate-500">USDT</span>
                </div>
              </div>
            </div>
            <div>
              <label className="text-[10px] text-slate-500 mb-1 block">Callback Rate (%)</label>
              <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
                <input
                  type="number"
                  value={callbackRate}
                  onChange={e => setCallbackRate(e.target.value)}
                  placeholder="1.0"
                  min="0.1"
                  max="10"
                  step="0.1"
                  className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
                />
                <span className="text-[10px] text-slate-500 pr-2">%</span>
              </div>
              <div className="flex gap-1 mt-1.5">
                {[0.5, 1, 2, 3, 5].map(r => (
                  <button
                    key={r}
                    onClick={() => setCallbackRate(r.toString())}
                    className={`flex-1 text-[9px] py-1 rounded border transition-colors ${
                      callbackRate === r.toString()
                        ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400'
                        : 'border-white/[0.06] text-slate-500 hover:text-white hover:border-white/20'
                    }`}
                  >
                    {r}%
                  </button>
                ))}
              </div>
            </div>
          </>
        )}

        {/* Advanced Limit specific: Time in Force */}
        {(orderType === 'advanced_limit' || orderType === 'hidden') && (
          <div>
            <label className="text-[10px] text-slate-500 mb-1 block">Time in Force</label>
            <div className="flex gap-1">
              {(['GTC', 'IOC', 'FOK'] as const).map(tif => (
                <button
                  key={tif}
                  onClick={() => setTimeInForce(tif)}
                  className={`flex-1 py-1.5 text-[10px] font-medium rounded border transition-colors ${
                    timeInForce === tif
                      ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400'
                      : 'border-white/[0.06] text-slate-500 hover:text-white hover:border-white/20'
                  }`}
                >
                  {tif}
                </button>
              ))}
            </div>
            <p className="text-[9px] text-slate-600 mt-1">
              {timeInForce === 'GTC' && 'Good Till Cancel — stays until filled or cancelled'}
              {timeInForce === 'IOC' && 'Immediate or Cancel — fills what it can, cancels rest'}
              {timeInForce === 'FOK' && 'Fill or Kill — must fill entirely or cancel'}
            </p>
          </div>
        )}

        {/* TWAP specific fields */}
        {orderType === 'twap' && (
          <>
            <div>
              <label className="text-[10px] text-slate-500 mb-1 block">Duration (minutes)</label>
              <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
                <input
                  type="number"
                  value={twapDuration}
                  onChange={e => setTwapDuration(e.target.value)}
                  placeholder="60"
                  className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
                />
                <span className="text-[10px] text-slate-500 pr-2">min</span>
              </div>
              <div className="flex gap-1 mt-1.5">
                {[15, 30, 60, 120, 240].map(d => (
                  <button
                    key={d}
                    onClick={() => setTwapDuration(d.toString())}
                    className={`flex-1 text-[9px] py-1 rounded border transition-colors ${
                      twapDuration === d.toString()
                        ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400'
                        : 'border-white/[0.06] text-slate-500 hover:text-white hover:border-white/20'
                    }`}
                  >
                    {d >= 60 ? `${d / 60}h` : `${d}m`}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="text-[10px] text-slate-500 mb-1 block">Number of Slices</label>
              <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
                <input
                  type="number"
                  value={twapSlices}
                  onChange={e => setTwapSlices(e.target.value)}
                  placeholder="10"
                  min="2"
                  max="100"
                  className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
                />
                <span className="text-[10px] text-slate-500 pr-2">slices</span>
              </div>
            </div>
            <div>
              <label className="text-[10px] text-slate-500 mb-1 block">Price Limit (optional)</label>
              <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
                <input
                  type="number"
                  value={twapPriceLimit}
                  onChange={e => setTwapPriceLimit(e.target.value)}
                  placeholder="No limit"
                  className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
                />
                <span className="text-[10px] text-slate-500 pr-2">USDT</span>
              </div>
            </div>
          </>
        )}

        {/* Amount / Cost toggle — shown for all order types */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="text-[10px] text-slate-500">
              {costMode ? 'Cost (USDT)' : `Amount (${baseCoin})`}
            </label>
            <button
              type="button"
              onClick={() => { setCostMode(v => !v); setAmount(''); setCostUsdt(''); }}
              className="text-[9px] px-1.5 py-0.5 rounded border border-white/10 text-slate-400 hover:text-white hover:border-white/30 transition-colors"
            >
              {costMode ? `By ${baseCoin}` : 'By Cost (USDT)'}
            </button>
          </div>
          <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
            {costMode ? (
              <input
                type="number"
                value={costUsdt}
                onChange={e => setCostUsdt(e.target.value)}
                placeholder="0.00"
                className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none min-w-0"
              />
            ) : (
              <input
                type="number"
                value={amount}
                onChange={e => setAmount(e.target.value)}
                placeholder="0"
                className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none min-w-0"
              />
            )}
            <span className="text-[10px] text-slate-400 pr-2 shrink-0">{costMode ? 'USDT' : baseCoin}</span>
          </div>

          {/* Slider */}
          <div className="mt-2 px-1">
            <div className="relative py-2">
              <div className="absolute top-1/2 left-0 right-0 h-[2px] bg-slate-700 -translate-y-1/2 rounded" />
              <div
                className="absolute top-1/2 left-0 h-[2px] bg-emerald-500 -translate-y-1/2 rounded"
                style={{ width: `${sliderValue}%` }}
              />
              {[0, 25, 50, 75, 100].map(pct => (
                <button
                  key={pct}
                  onClick={() => handleSliderChange(pct)}
                  className={`absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-2.5 h-2.5 rounded-full border-2 transition-colors ${
                    sliderValue >= pct
                      ? 'bg-emerald-500 border-emerald-500'
                      : 'bg-[#1e222d] border-slate-600'
                  }`}
                  style={{ left: `${pct}%` }}
                />
              ))}
            </div>
          </div>
        </div>

        {/* Available + Max info */}
        <div className="space-y-1 text-[11px]">
          <div className="flex justify-between">
            <span className="text-slate-500">Available</span>
            <span className="text-white">{availableBalance.toFixed(2)} USDT</span>
          </div>
          <div className="flex justify-between">
            <span className="text-slate-500">Max Long</span>
            <span className="text-slate-300">{maxLongAmount.toFixed(4)} {baseCoin}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-slate-500">Max Short</span>
            <span className="text-slate-300">{maxShortAmount.toFixed(4)} {baseCoin}</span>
          </div>
          {costMode && amountNum > 0 && (
            <div className="flex justify-between text-[9px]">
              <span className="text-slate-600">&#8776; {baseCoin} amount</span>
              <span className="text-slate-400">{amountNum.toFixed(6)} {baseCoin}</span>
            </div>
          )}
          {orderType === 'twap' && costUsdt_ > 0 && (
            <div className="flex justify-between text-[9px]">
              <span className="text-slate-600">Per slice</span>
              <span className="text-slate-400">{(costUsdt_ / (parseInt(twapSlices) || 10)).toFixed(2)} USDT</span>
            </div>
          )}
        </div>

        {/* TP/SL section — for all order types except TWAP */}
        {orderType !== 'twap' && (
          <div className="space-y-2 border-t border-white/[0.06] pt-2">
            <div className="text-[10px] text-slate-500 font-medium">Take Profit / Stop Loss</div>
            {/* TP */}
            <div>
              <label className="flex items-center gap-2 text-[11px] text-slate-400 cursor-pointer mb-1">
                <input
                  type="checkbox"
                  checked={tpEnabled}
                  onChange={() => setTpEnabled(!tpEnabled)}
                  className="accent-emerald-500 w-3 h-3 rounded"
                />
                Take Profit
              </label>
              {tpEnabled && (
                <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
                  <input
                    type="number"
                    value={tpPrice}
                    onChange={e => setTpPrice(e.target.value)}
                    placeholder="TP Price"
                    className="flex-1 bg-transparent px-3 py-1.5 text-sm text-white outline-none min-w-0"
                  />
                  <span className="text-[10px] text-slate-500 pr-2">USDT</span>
                </div>
              )}
            </div>
            {/* SL */}
            <div>
              <label className="flex items-center gap-2 text-[11px] text-slate-400 cursor-pointer mb-1">
                <input
                  type="checkbox"
                  checked={slEnabled}
                  onChange={() => setSlEnabled(!slEnabled)}
                  className="accent-red-500 w-3 h-3 rounded"
                />
                Stop Loss
              </label>
              {slEnabled && (
                <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
                  <input
                    type="number"
                    value={slPrice}
                    onChange={e => setSlPrice(e.target.value)}
                    placeholder="SL Price"
                    className="flex-1 bg-transparent px-3 py-1.5 text-sm text-white outline-none min-w-0"
                  />
                  <span className="text-[10px] text-slate-500 pr-2">USDT</span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Options: Post Only, Reduce Only — context-dependent */}
        <div className="flex flex-wrap gap-x-4 gap-y-1.5">
          {(orderType === 'limit' || orderType === 'advanced_limit' || orderType === 'hidden') && (
            <label className="flex items-center gap-1.5 text-[11px] text-slate-400 cursor-pointer">
              <input
                type="checkbox"
                checked={postOnly}
                onChange={() => setPostOnly(!postOnly)}
                className="accent-emerald-500 w-3 h-3"
              />
              Post Only
            </label>
          )}
          <label className="flex items-center gap-1.5 text-[11px] text-slate-400 cursor-pointer">
            <input
              type="checkbox"
              checked={reduceOnly}
              onChange={() => setReduceOnly(!reduceOnly)}
              className="accent-emerald-500 w-3 h-3"
            />
            Reduce Only
          </label>
        </div>

        {/* Success / Error messages */}
        {success && (
          <div className="flex items-center gap-2 px-2 py-1.5 rounded bg-emerald-500/10 border border-emerald-500/20">
            <span className="text-emerald-400 text-xs">&#10003;</span>
            <p className="text-emerald-400 text-xs flex-1">{success}</p>
          </div>
        )}
        {error && (
          <div className="flex items-center gap-2 px-2 py-1.5 rounded bg-red-500/10 border border-red-500/20">
            <span className="text-red-400 text-xs">&#10007;</span>
            <p className="text-red-400 text-xs flex-1">{error}</p>
          </div>
        )}
      </div>

      {/* Buy/Long + Sell/Short buttons */}
      <div className="px-3 py-2 space-y-2 border-t border-white/[0.06]">
        <div className="grid grid-cols-2 gap-2">
          <button
            disabled={submitting}
            onClick={() => placeOrder('buy')}
            className="py-2.5 rounded-lg bg-emerald-500 text-white text-sm font-bold hover:bg-emerald-400 disabled:opacity-50 transition-colors active:scale-[0.98]"
          >
            {submitting ? '...' : 'Buy/Long'}
          </button>
          <button
            disabled={submitting}
            onClick={() => placeOrder('sell')}
            className="py-2.5 rounded-lg bg-red-500 text-white text-sm font-bold hover:bg-red-400 disabled:opacity-50 transition-colors active:scale-[0.98]"
          >
            {submitting ? '...' : 'Sell/Short'}
          </button>
        </div>

        {/* Margin info */}
        <div className="grid grid-cols-2 gap-2 text-[10px] text-slate-500">
          <div>Margin {marginCost > 0 ? marginCost.toFixed(2) : '0.00'} USDT</div>
          <div className="text-right">Margin {marginCost > 0 ? marginCost.toFixed(2) : '0.00'} USDT</div>
          <div>Cost {costUsdt_ > 0 ? costUsdt_.toFixed(2) : '0.00'} USDT</div>
          <div className="text-right">Cost {costUsdt_ > 0 ? costUsdt_.toFixed(2) : '0.00'} USDT</div>
        </div>
      </div>

      <LeverageModal
        isOpen={leverageModal}
        currentLeverage={leverage}
        maxLeverage={20}
        onConfirm={(lev) => { onLeverageChange(lev); setLeverageModal(false); }}
        onClose={() => setLeverageModal(false)}
      />
    </div>
  );
}
