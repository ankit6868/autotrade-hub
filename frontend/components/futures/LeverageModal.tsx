'use client';
import { useState, useEffect } from 'react';

interface Props {
  isOpen: boolean;
  currentLeverage: number;
  maxLeverage?: number;
  onConfirm: (leverage: number) => void;
  onClose: () => void;
}

const QUICK_VALUES = [1, 3, 5, 10, 15, 20];

export default function LeverageModal({ isOpen, currentLeverage, maxLeverage = 20, onConfirm, onClose }: Props) {
  const [leverage, setLeverage] = useState(currentLeverage);

  useEffect(() => {
    setLeverage(currentLeverage);
  }, [currentLeverage, isOpen]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className="bg-[#1a1e2e] rounded-xl border border-white/[0.08] p-6 w-[420px] max-w-[90vw] shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-bold text-white">Adjust Leverage</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl leading-none">&times;</button>
        </div>

        <p className="text-sm text-slate-400 mb-4">
          Current: <span className="text-emerald-400 font-bold">{currentLeverage}.0x</span>
        </p>

        {/* Big number display */}
        <div className="flex items-center justify-center gap-4 mb-6">
          <button
            onClick={() => setLeverage(Math.max(1, leverage - 1))}
            className="w-10 h-10 rounded-lg bg-slate-700 text-white text-xl hover:bg-slate-600 flex items-center justify-center"
          >
            &minus;
          </button>
          <div className="flex items-baseline gap-1">
            <span className="text-4xl font-bold text-emerald-400">{leverage}</span>
            <span className="text-xl text-emerald-400 font-bold">&times;</span>
          </div>
          <button
            onClick={() => setLeverage(Math.min(maxLeverage, leverage + 1))}
            className="w-10 h-10 rounded-lg bg-slate-700 text-white text-xl hover:bg-slate-600 flex items-center justify-center"
          >
            +
          </button>
        </div>

        {/* Slider */}
        <div className="mb-4 px-2">
          <input
            type="range"
            min={1}
            max={maxLeverage}
            value={leverage}
            onChange={e => setLeverage(parseInt(e.target.value))}
            className="w-full h-1.5 rounded-full appearance-none cursor-pointer
              bg-gradient-to-r from-emerald-600 via-yellow-500 to-red-500
              [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:h-4
              [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white [&::-webkit-slider-thumb]:shadow-lg
              [&::-webkit-slider-thumb]:cursor-pointer"
          />
          <div className="flex justify-between text-[10px] text-slate-500 mt-1">
            <span>1x</span>
            <span>10x</span>
            <span>15x</span>
            <span>{maxLeverage}x</span>
          </div>
        </div>

        {/* Quick select */}
        <div className="flex gap-2 mb-6 flex-wrap">
          {QUICK_VALUES.filter(v => v <= maxLeverage).map(v => (
            <button
              key={v}
              onClick={() => setLeverage(v)}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                leverage === v
                  ? 'bg-emerald-500 text-white'
                  : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
              }`}
            >
              {v}x
            </button>
          ))}
        </div>

        {/* Actions */}
        <div className="flex gap-3">
          <button
            onClick={onClose}
            className="flex-1 py-2.5 rounded-lg border border-white/[0.1] text-slate-300 text-sm font-medium hover:bg-white/[0.05]"
          >
            Cancel
          </button>
          <button
            onClick={() => onConfirm(leverage)}
            className="flex-1 py-2.5 rounded-lg bg-emerald-500 text-white text-sm font-bold hover:bg-emerald-400"
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}
