'use client';
import { useState } from 'react';
import { api } from '@/lib/api';
import LeverageModal from './LeverageModal';

interface Props {
  symbol: string;
  pair: string;
  mode: 'paper' | 'live';
  leverage: number;
  marginMode: string;
  availableBalance: number;
  onLeverageChange: (lev: number) => void;
  onMarginModeChange: (mode: string) => void;
  onOrderPlaced: () => void;
}

type OrderTab = 'limit' | 'market' | 'conditional';
type AdvancedTab = 'advanced_limit' | 'trailing_stop' | 'hidden' | 'twap';

export default function ManualOrderPanel({
  symbol, pair, mode, leverage, marginMode, availableBalance,
  onLeverageChange, onMarginModeChange, onOrderPlaced,
}: Props) {
  const [orderTab, setOrderTab] = useState<OrderTab>('limit');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [advancedTab, setAdvancedTab] = useState<AdvancedTab>('advanced_limit');
  const [leverageModal, setLeverageModal] = useState(false);

  const [price, setPrice] = useState('');
  const [amount, setAmount] = useState('');
  const [stopPrice, setStopPrice] = useState('');
  const [tpPrice, setTpPrice] = useState('');
  const [slPrice, setSlPrice] = useState('');
  const [postOnly, setPostOnly] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [reduceOnly, setReduceOnly] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const priceNum = parseFloat(price) || 0;
  const amountNum = parseFloat(amount) || 0;
  const cost = priceNum * amountNum;
  const margin = cost / leverage;

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

  function setAmountPct(pct: number) {
    if (availableBalance > 0 && priceNum > 0) {
      const maxAmount = (availableBalance * leverage * pct / 100) / priceNum;
      setAmount(maxAmount.toFixed(6));
    }
  }

  const orderTabs: { key: OrderTab; label: string }[] = [
    { key: 'limit', label: 'Limit' },
    { key: 'market', label: 'Market' },
    { key: 'conditional', label: 'Conditional' },
  ];

  return (
    <div className="flex flex-col h-full">
      {/* Manual | Bot tab header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/[0.06]">
        <span className="text-sm font-bold text-white">Manual</span>
      </div>

      {/* Margin mode + Leverage */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/[0.06]">
        {/* Margin mode toggle */}
        <div className="flex rounded-md overflow-hidden border border-white/[0.1] text-[11px]">
          {['cross', 'isolated'].map(m => (
            <button
              key={m}
              onClick={() => onMarginModeChange(m)}
              className={`px-2.5 py-1 capitalize ${marginMode === m ? 'bg-emerald-500/20 text-emerald-400' : 'text-slate-400 hover:text-white'}`}
            >
              {m}
            </button>
          ))}
        </div>
        {/* Leverage button */}
        <button
          onClick={() => setLeverageModal(true)}
          className="px-2.5 py-1 rounded-md bg-slate-700 text-emerald-400 text-[11px] font-bold hover:bg-slate-600"
        >
          {leverage}.0x
        </button>
      </div>

      {/* Order type tabs */}
      <div className="flex items-center gap-1 px-3 py-2 border-b border-white/[0.06] relative">
        {orderTabs.map(t => (
          <button
            key={t.key}
            onClick={() => { setOrderTab(t.key); setShowAdvanced(false); }}
            className={`px-2.5 py-1 rounded text-xs font-medium ${
              orderTab === t.key ? 'text-white bg-white/[0.08]' : 'text-slate-400 hover:text-white'
            }`}
          >
            {t.label}
          </button>
        ))}
        {/* Dropdown arrow for advanced */}
        <button
          onClick={() => setShowAdvanced(!showAdvanced)}
          className="ml-auto text-slate-400 hover:text-white text-xs"
        >
          &#x25BE;
        </button>
        {showAdvanced && (
          <div className="absolute top-full right-2 z-20 mt-1 bg-[#1a1e2e] border border-white/[0.1] rounded-lg shadow-xl py-1 min-w-[160px]">
            {[
              { key: 'advanced_limit' as AdvancedTab, label: 'Advanced Limit' },
              { key: 'trailing_stop' as AdvancedTab, label: 'Trailing Stop' },
              { key: 'hidden' as AdvancedTab, label: 'Hidden Order' },
              { key: 'twap' as AdvancedTab, label: 'TWAP' },
            ].map(a => (
              <button
                key={a.key}
                onClick={() => { setAdvancedTab(a.key); setShowAdvanced(false); }}
                className="block w-full text-left px-3 py-1.5 text-xs text-slate-300 hover:bg-white/[0.06]"
              >
                {a.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Order form */}
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-3">
        {/* Price */}
        {orderTab !== 'market' && (
          <div>
            <label className="text-[10px] text-slate-500 mb-1 block">Price</label>
            <div className="flex items-center bg-slate-800 rounded-md border border-white/[0.06]">
              <input
                type="number"
                value={price}
                onChange={e => setPrice(e.target.value)}
                placeholder="0.00"
                className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
              />
              <div className="flex items-center gap-1 pr-2">
                <span className="text-[10px] text-slate-500">Last</span>
                <span className="text-[10px] text-slate-400">USDT</span>
              </div>
            </div>
          </div>
        )}

        {/* Stop price (conditional) */}
        {orderTab === 'conditional' && (
          <div>
            <label className="text-[10px] text-slate-500 mb-1 block">Stop Price</label>
            <div className="flex items-center bg-slate-800 rounded-md border border-white/[0.06]">
              <input
                type="number"
                value={stopPrice}
                onChange={e => setStopPrice(e.target.value)}
                placeholder="0.00"
                className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
              />
              <span className="text-[10px] text-slate-400 pr-2">USDT</span>
            </div>
          </div>
        )}

        {/* Amount */}
        <div>
          <label className="text-[10px] text-slate-500 mb-1 block">
            Amount ({pair.split('/')[0]})
          </label>
          <div className="flex items-center bg-slate-800 rounded-md border border-white/[0.06]">
            <input
              type="number"
              value={amount}
              onChange={e => setAmount(e.target.value)}
              placeholder="0.000"
              className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
            />
            <span className="text-[10px] text-slate-400 pr-2">{pair.split('/')[0]}</span>
          </div>
          {/* % buttons */}
          <div className="flex gap-1 mt-1.5">
            {/* Slider dots */}
            <input
              type="range"
              min={0}
              max={100}
              step={25}
              value={0}
              onChange={e => setAmountPct(parseInt(e.target.value))}
              className="w-full h-1 rounded-full appearance-none cursor-pointer bg-slate-700
                [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3
                [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-emerald-400 [&::-webkit-slider-thumb]:cursor-pointer"
            />
          </div>
        </div>

        {/* Available + Cost info */}
        <div className="space-y-1 text-[11px]">
          <div className="flex justify-between">
            <span className="text-slate-500">Available</span>
            <span className="text-white">{availableBalance.toFixed(2)} USDT</span>
          </div>
          {orderTab !== 'market' && cost > 0 && (
            <div className="flex justify-between">
              <span className="text-slate-500">Cost</span>
              <span className="text-slate-300">{cost.toFixed(2)} USDT</span>
            </div>
          )}
          {margin > 0 && (
            <div className="flex justify-between">
              <span className="text-slate-500">Margin</span>
              <span className="text-slate-300">{margin.toFixed(2)} USDT</span>
            </div>
          )}
          <div className="flex justify-between">
            <span className="text-slate-500">Max Long</span>
            <span className="text-slate-300">
              {priceNum > 0 ? ((availableBalance * leverage) / priceNum).toFixed(4) : '0.000'} {pair.split('/')[0]}
            </span>
          </div>
        </div>

        {/* TP/SL toggles */}
        <div className="space-y-2">
          <div className="flex items-center gap-4 text-[11px]">
            <label className="flex items-center gap-1.5 text-slate-400">
              <input type="radio" name="tpsl" className="accent-emerald-500" onChange={() => {}} /> TP/SL of Long
            </label>
            <label className="flex items-center gap-1.5 text-slate-400">
              <input type="radio" name="tpsl" className="accent-red-500" onChange={() => {}} /> TP/SL of Short
            </label>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-[10px] text-slate-500">Take Profit</label>
              <input
                type="number"
                value={tpPrice}
                onChange={e => setTpPrice(e.target.value)}
                placeholder="TP Price"
                className="w-full bg-slate-800 border border-white/[0.06] rounded px-2 py-1.5 text-xs text-white outline-none"
              />
            </div>
            <div>
              <label className="text-[10px] text-slate-500">Stop Loss</label>
              <input
                type="number"
                value={slPrice}
                onChange={e => setSlPrice(e.target.value)}
                placeholder="SL Price"
                className="w-full bg-slate-800 border border-white/[0.06] rounded px-2 py-1.5 text-xs text-white outline-none"
              />
            </div>
          </div>
        </div>

        {/* Reduce Only */}
        <label className="flex items-center gap-2 text-[11px] text-slate-400">
          <input
            type="checkbox"
            checked={reduceOnly}
            onChange={e => setReduceOnly(e.target.checked)}
            className="accent-emerald-500"
          />
          Reduce Only
        </label>

        {/* Error */}
        {error && <p className="text-red-400 text-xs">{error}</p>}
      </div>

      {/* Buy/Long + Sell/Short buttons */}
      <div className="grid grid-cols-2 gap-2 px-3 py-3 border-t border-white/[0.06]">
        <button
          disabled={submitting}
          onClick={() => placeOrder('buy')}
          className="py-2.5 rounded-md bg-emerald-500 text-white text-sm font-bold hover:bg-emerald-400 disabled:opacity-50"
        >
          Buy/Long
        </button>
        <button
          disabled={submitting}
          onClick={() => placeOrder('sell')}
          className="py-2.5 rounded-md bg-red-500 text-white text-sm font-bold hover:bg-red-400 disabled:opacity-50"
        >
          Sell/Short
        </button>
      </div>

      {/* Margin info */}
      <div className="px-3 pb-2 grid grid-cols-2 gap-2 text-[10px] text-slate-500">
        <div>Margin: {margin > 0 ? margin.toFixed(2) : '0.00'} USDT</div>
        <div className="text-right">Margin: {margin > 0 ? margin.toFixed(2) : '0.00'} USDT</div>
        <div>Est. Liq. Price: --</div>
        <div className="text-right">Est. Liq. Price: --</div>
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
