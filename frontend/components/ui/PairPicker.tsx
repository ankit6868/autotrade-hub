'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '@/lib/api';

interface Props {
  value: string[];
  onChange: (pairs: string[]) => void;
  multi?: boolean;
  placeholder?: string;
  disabled?: boolean;
}

/**
 * Searchable autocomplete pair picker backed by /api/market/pairs (full
 * KuCoin USDT universe — ~900 pairs). Replaces the comma-separated text
 * field used historically on Paper / Live / Backtest pages.
 */
export default function PairPicker({
  value,
  onChange,
  multi = true,
  placeholder = 'Search KuCoin pair (e.g. PEPE/USDT)',
  disabled = false,
}: Props) {
  const [pairs, setPairs] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.market.pairs().then((d) => setPairs(d.pairs || [])).catch(() => {});
  }, []);

  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toUpperCase();
    if (!q) return pairs.slice(0, 50);
    return pairs.filter((p) => p.toUpperCase().includes(q)).slice(0, 50);
  }, [pairs, query]);

  function add(pair: string) {
    if (!pair) return;
    if (value.includes(pair)) return;
    onChange(multi ? [...value, pair] : [pair]);
    setQuery('');
    if (!multi) setOpen(false);
  }
  function remove(pair: string) {
    onChange(value.filter((p) => p !== pair));
  }

  return (
    <div className="w-full" ref={wrapRef}>
      {multi && value.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-2">
          {value.map((p) => (
            <span
              key={p}
              className="inline-flex items-center gap-1 px-2 py-1 rounded bg-blue-600/20 border border-blue-500/30 text-xs"
            >
              {p}
              {!disabled && (
                <button
                  type="button"
                  onClick={() => remove(p)}
                  className="text-blue-300 hover:text-white"
                  aria-label={`Remove ${p}`}
                >
                  ✕
                </button>
              )}
            </span>
          ))}
        </div>
      )}

      <div className="relative">
        <input
          className="input w-full"
          value={!multi && value.length === 1 && !open ? value[0] : query}
          placeholder={placeholder}
          disabled={disabled}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setQuery(e.target.value.toUpperCase());
            setOpen(true);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && filtered[0]) {
              e.preventDefault();
              add(filtered[0]);
            }
          }}
        />
        {open && filtered.length > 0 && (
          <ul className="absolute z-20 mt-1 max-h-60 w-full overflow-auto rounded border border-slate-700 bg-slate-900 shadow-lg">
            {filtered.map((p) => (
              <li
                key={p}
                onMouseDown={(e) => {
                  e.preventDefault();
                  add(p);
                }}
                className={`cursor-pointer px-3 py-1.5 text-sm hover:bg-blue-600/30 ${
                  value.includes(p) ? 'text-slate-500' : ''
                }`}
              >
                {p}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="mt-1 text-xs text-slate-400">
        {pairs.length > 0 ? `${pairs.length} KuCoin USDT pairs available` : 'Loading pairs…'}
        {multi && value.length > 0 && ` · ${value.length} selected`}
      </div>
    </div>
  );
}
