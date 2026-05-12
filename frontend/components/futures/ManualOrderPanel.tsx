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

type OrderTab = 'limit' | 'market' | 'conditional';
type AdvancedTab = 'advanced_limit' | 'trailing_stop' | 'hidden' | 'twap';

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
  const [orderTab, setOrderTab] = useState<OrderTab>('limit');
  const [showAdvancedMenu, setShowAdvancedMenu] = useState(false);
  const [leverageModal, setLeverageModal] = useState(false);
  const [showMarginDropdown, setShowMarginDropdown] = useState(false);

  const [price, setPrice] = useState('');
  const [amount, setAmount] = useState('');
  const [costMode, setCostMode] = useState(false);   // true = input in USDT, derive BTC
  const [costUsdt, setCostUsdt] = useState('');       // USDT cost input
  const [stopPrice, setStopPrice] = useState('');
  const [tpPrice, setTpPrice] = useState('');
  const [slPrice, setSlPrice] = useState('');
  const [postOnly, setPostOnly] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [reduceOnly, setReduceOnly] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [sliderValue, setSliderValue] = useState(0);
  const [tpslSide, setTpslSide] = useState<'long' | 'short'>('long');

  const marginRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (marginRef.current && !marginRef.current.contains(e.target as Node)) {
        setShowMarginDropdown(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const priceNum  = parseFloat(price) || 0;
  const baseCoin  = pair.split('/')[0];
  // When costMode is on: user types USDT cost, derive BTC amount
  const effectiveRef = parseFloat(price) > 0 ? parseFloat(price) : (lastPrice || 1);
  const amountNum = costMode
    ? (parseFloat(costUsdt) || 0) / effectiveRef
    : (parseFloat(amount) || 0);
  const cost       = costMode ? (parseFloat(costUsdt) || 0) : priceNum * amountNum;
  const marginCost = cost / leverage;

  const maxLongAmount = priceNum > 0 ? (availableBalance * leverage) / priceNum : 0;
  const maxShortAmount = maxLongAmount;

  async function placeOrder(side: 'buy' | 'sell') {
    setSubmitting(true);
    setError('');
    try {
      if (orderTab === 'market') {
        const direction = side === 'buy' ? 'long' : 'short';
        const stakePct = availableBalance > 0 ? (amountNum / availableBalance) * 100 : 5;
        const r = await api.futures.manualEntry(pair, direction, stakePct, leverage);
        if (r.error) setError(r.error);
        else onOrderPlaced();
      } else {
        const futSymbol = symbol.replace('/', '').replace('USDT', 'USDTM');
        const r = await api.futures.placeOrder({
          symbol: futSymbol,
          side,
          order_type: orderTab === 'conditional' ? 'stop' : 'limit',
          size: amountNum,
          price: priceNum || undefined,
          stop_price: orderTab === 'conditional' ? parseFloat(stopPrice) || undefined : undefined,
          leverage,
          tp_price: tpPrice ? parseFloat(tpPrice) : undefined,
          sl_price: slPrice ? parseFloat(slPrice) : undefined,
          hidden,
          post_only: postOnly,
          reduce_only: reduceOnly,
        });
        if (r.error) setError(r.error);
        else onOrderPlaced();
      }
    } catch (e) {
      setError(String(e));
    }
    setSubmitting(false);
  }

  function handleSliderChange(pct: number) {
    setSliderValue(pct);
    const ref = effectiveRef > 0 ? effectiveRef : 1;
    if (availableBalance > 0) {
      const usdtCost = availableBalance * pct / 100;
      if (costMode) {
        setCostUsdt(usdtCost.toFixed(2));
      } else if (ref > 0) {
        const btcAmount = (usdtCost * leverage) / ref;
        setAmount(btcAmount.toFixed(6));
      }
    }
  }

  function fillLastPrice() {
    if (lastPrice) setPrice(lastPrice.toString());
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Lead Trading / Paper Account badge — always visible */}
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
        {/* Margin mode dropdown */}
        <div className="relative" ref={marginRef}>
          <button
            onClick={() => setShowMarginDropdown(!showMarginDropdown)}
            className="flex items-center gap-1 text-xs text-white"
          >
            <span className="w-2 h-2 rounded-full bg-emerald-400 inline-block" />
            <span className="capitalize font-medium">{marginMode}</span>
            <svg className="w-3 h-3 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
          </button>
          {showMarginDropdown && (
            <div className="absolute top-full left-0 mt-1 z-30 bg-[#1e222d] border border-white/[0.1] rounded-lg shadow-xl py-1 min-w-[140px]">
              {['cross', 'isolated'].map(m => (
                <button
                  key={m}
                  onClick={() => { onMarginModeChange(m); setShowMarginDropdown(false); }}
                  className={`block w-full text-left px-3 py-2 text-xs capitalize ${marginMode === m ? 'text-emerald-400' : 'text-slate-300 hover:bg-white/[0.06]'}`}
                >
                  {m}
                </button>
              ))}
              <div className="border-t border-white/[0.06] mt-1 pt-1">
                <button className="block w-full text-left px-3 py-2 text-xs text-slate-400 hover:bg-white/[0.06]">
                  Edit Multiple
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Leverage button */}
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
          {([
            { key: 'limit' as OrderTab, label: 'Limit' },
            { key: 'market' as OrderTab, label: 'Market' },
            { key: 'conditional' as OrderTab, label: 'Conditional' },
          ]).map(t => (
            <button
              key={t.key}
              onClick={() => { setOrderTab(t.key); setShowAdvancedMenu(false); }}
              className={`px-2 py-1 text-xs font-medium ${
                orderTab === t.key ? 'text-white' : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        {/* Dropdown arrow for advanced */}
        <button
          onClick={() => setShowAdvancedMenu(!showAdvancedMenu)}
          className="ml-1 text-slate-500 hover:text-white text-xs"
        >
          ▾
        </button>

        {showAdvancedMenu && (
          <div className="absolute top-full left-2 z-20 mt-1 bg-[#1e222d] border border-white/[0.1] rounded-lg shadow-xl py-1 min-w-[160px]">
            {[
              { key: 'advanced_limit' as AdvancedTab, label: 'Advanced Limit' },
              { key: 'conditional' as AdvancedTab, label: 'Conditional' },
              { key: 'trailing_stop' as AdvancedTab, label: 'Trailing Stop' },
              { key: 'hidden' as AdvancedTab, label: 'Hidden Order' },
              { key: 'twap' as AdvancedTab, label: 'TWAP' },
            ].map(a => (
              <button
                key={a.key}
                onClick={() => { setShowAdvancedMenu(false); }}
                className="block w-full text-left px-3 py-2 text-xs text-slate-300 hover:bg-white/[0.06]"
              >
                {a.label}
              </button>
            ))}
          </div>
        )}

        <div className="ml-auto">
          <button className="text-slate-500 hover:text-white text-xs">?</button>
        </div>
      </div>

      {/* Order form */}
      <div className="flex-1 overflow-y-auto px-3 py-2.5 space-y-3">
        {/* Price */}
        {orderTab !== 'market' && (
          <div>
            <label className="text-[10px] text-slate-500 mb-1 block">Price</label>
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
                <button className="text-[10px] text-slate-400 hover:text-white font-medium px-1 py-0.5 rounded bg-slate-700/50">
                  BBO
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Stop price (conditional) */}
        {orderTab === 'conditional' && (
          <div>
            <label className="text-[10px] text-slate-500 mb-1 block">Stop Price</label>
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

        {/* Amount / Cost toggle */}
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
            <span className="text-[10px] text-slate-400 pr-2 shrink-0">{baseCoin}</span>
          </div>

          {/* Slider with dots */}
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

        {/* Available + Info */}
        <div className="space-y-1 text-[11px]">
          <div className="flex justify-between">
            <span className="text-slate-500">Available</span>
            <span className="text-white">{availableBalance.toFixed(2)} USDT <span className="text-emerald-400 cursor-pointer">⊕</span></span>
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
              <span className="text-slate-600">≈ {baseCoin} amount</span>
              <span className="text-slate-400">{amountNum.toFixed(6)} {baseCoin}</span>
            </div>
          )}
        </div>

        {/* TP/SL toggles */}
        <div className="space-y-2">
          <div className="flex items-center gap-4 text-[11px]">
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input
                type="radio"
                name="tpsl"
                checked={tpslSide === 'long'}
                onChange={() => setTpslSide('long')}
                className="accent-emerald-500 w-3 h-3"
              />
              <span className="text-slate-400">TP/SL of Long</span>
            </label>
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input
                type="radio"
                name="tpsl"
                checked={tpslSide === 'short'}
                onChange={() => setTpslSide('short')}
                className="accent-red-500 w-3 h-3"
              />
              <span className="text-slate-400">TP/SL of Short</span>
            </label>
          </div>
        </div>

        {/* Reduce Only */}
        <label className="flex items-center gap-2 text-[11px] text-slate-400 cursor-pointer">
          <input
            type="radio"
            checked={reduceOnly}
            onChange={() => setReduceOnly(!reduceOnly)}
            className="accent-emerald-500 w-3 h-3"
          />
          Reduce Only
        </label>

        {/* Error */}
        {error && <p className="text-red-400 text-xs">{error}</p>}
      </div>

      {/* Buy/Long + Sell/Short buttons */}
      <div className="px-3 py-2 space-y-2">
        <div className="grid grid-cols-2 gap-2">
          <button
            disabled={submitting}
            onClick={() => placeOrder('buy')}
            className="py-2.5 rounded-lg bg-emerald-500 text-white text-sm font-bold hover:bg-emerald-400 disabled:opacity-50 transition-colors"
          >
            Buy/Long
          </button>
          <button
            disabled={submitting}
            onClick={() => placeOrder('sell')}
            className="py-2.5 rounded-lg bg-red-500 text-white text-sm font-bold hover:bg-red-400 disabled:opacity-50 transition-colors"
          >
            Sell/Short
          </button>
        </div>

        {/* Margin info */}
        <div className="grid grid-cols-2 gap-2 text-[10px] text-slate-500">
          <div>Margin {marginCost > 0 ? marginCost.toFixed(2) : '0.00'} USDT</div>
          <div className="text-right">Margin {marginCost > 0 ? marginCost.toFixed(2) : '0.00'} USDT</div>
          <div>Est. Liq. Price —</div>
          <div className="text-right">Est. Liq. Price —</div>
        </div>
      </div>

      <LeverageModal
        isOpen={leverageModal}
        currentLeverage={leverage}
        onConfirm={(lev) => { onLeverageChange(lev); setLeverageModal(false); }}
        onClose={() => setLeverageModal(false)}
      />
    </div>
  );
}
