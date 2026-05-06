"""
Per-user Freqtrade process management.

Design
------
`FreqtradeRegistry` is a process-wide map of `user_id -> FreqtradeManager`.
Each `FreqtradeManager` owns ONE freqtrade subprocess for ONE user, working
out of its own `user_data/<user_id>/` directory (so configs, trade SQLite
DBs, downloaded candles, and backtest results are all isolated). Two
different users hitting `/api/trade/start` therefore launch two parallel
freqtrade processes that share nothing — they can paper- or live-trade
concurrently without stepping on each other.

The freqtrade `api_server` is disabled in the per-user config to avoid a
shared-port collision (it would otherwise want :8080 for every instance).
We don't use the freqtrade REST API anywhere — we read trades straight
from each user's SQLite DB via `trade_sync.sync(db, user_id)`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

FREQTRADE_PATH = os.getenv("FREQTRADE_PATH", "freqtrade")
FREQTRADE_USERDIR_ROOT = os.getenv("FREQTRADE_USERDIR", "./user_data")


def _resolve_freqtrade_cmd() -> list[str]:
    """Return the command list to invoke freqtrade.

    Order of preference:
    1) FREQTRADE_PATH env var if it resolves.
    2) `freqtrade` on PATH (covers /opt/venv/bin on Railway).
    3) Common venv locations checked explicitly.
    4) `python -m freqtrade` when the package is importable but the script
       isn't on PATH (common on Windows pip installs).
    """
    if FREQTRADE_PATH and shutil.which(FREQTRADE_PATH):
        return [shutil.which(FREQTRADE_PATH)]
    if shutil.which("freqtrade"):
        return [shutil.which("freqtrade")]
    # Explicit venv paths for Docker / Railway / Render environments
    for candidate in ["/opt/venv/bin/freqtrade", "/usr/local/bin/freqtrade"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate]
    try:
        import freqtrade  # noqa: F401
        return [sys.executable, "-m", "freqtrade"]
    except ImportError:
        return [FREQTRADE_PATH]


def _safe_subdir(user_id: str) -> str:
    """Sanitize a user id into a filesystem-safe directory name. Clerk
    `user_xxx` ids are already safe, but the local-dev id can be anything.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", user_id or "anon").strip("_")
    return cleaned or "anon"


def _load_backtest_result(path: Path) -> dict:
    """Load a freqtrade backtest result from either the new .zip container
    or the old bare .json."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            target = None
            for n in zf.namelist():
                if n.endswith(".json") and not n.endswith("_config.json"):
                    target = n
                    break
            if target is None:
                return {}
            with zf.open(target) as f:
                return json.loads(f.read().decode())
    with open(path) as f:
        return json.load(f)


class FreqtradeManager:
    """One freqtrade subprocess + one user_data dir, owned by a single user."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._safe_id = _safe_subdir(user_id)
        self._userdir = Path(FREQTRADE_USERDIR_ROOT) / self._safe_id
        self._config_path = self._userdir / "config.json"
        self._strategies_dir = Path("strategies")
        self._process: Optional[subprocess.Popen] = None
        self._mode: str = ""
        self._strategy: str = ""

    # ----- public state -----
    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def status(self) -> dict:
        return {
            "running": self.is_running,
            "mode": self._mode,
            "strategy": self._strategy,
            "pid": self._process.pid if self.is_running else None,
            "user_id": self.user_id,
        }

    @property
    def userdir(self) -> Path:
        return self._userdir

    # ----- config builders -----
    def _build_config(
        self,
        dry_run: bool,
        strategy_name: str,
        pairs: list[str],
        stake_amount: float,
        timeframe: str,
        stoploss: float,
        kucoin_key: str = "",
        kucoin_secret: str = "",
        kucoin_passphrase: str = "",
        wallet: float = 1000,
        max_open_trades: int = 3,
        trailing_stop_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        position_adjustment: bool = False,
    ) -> dict:
        config = {
            "max_open_trades": max_open_trades,
            "trading_mode": "spot",
            "stake_currency": "USDT",
            "stake_amount": stake_amount if stake_amount and stake_amount > 0 else "unlimited",
            "tradable_balance_ratio": 0.99,
            "fiat_display_currency": "USD",
            "dry_run": dry_run,
            "dry_run_wallet": wallet,
            "cancel_open_orders_on_exit": True,
            "position_adjustment_enable": bool(position_adjustment),
            "unfilledtimeout": {"entry": 10, "exit": 10, "exit_timeout_count": 0, "unit": "minutes"},
            "exchange": {
                "name": "kucoin",
                "key": kucoin_key,
                "secret": kucoin_secret,
                "password": kucoin_passphrase,
                "pair_whitelist": pairs,
                "pair_blacklist": [],
                "ccxt_config": {},
                "ccxt_async_config": {},
            },
            "pairlists": [{"method": "StaticPairList"}],
            "entry_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1, "price_last_balance": 0.0, "check_depth_of_market": {"enabled": False, "bids_to_ask_delta": 1}},
            "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
            "stoploss": stoploss,
            "timeframe": timeframe,
            "internals": {"process_throttle_secs": 5},
            # Disabled — multi-tenant: one shared port can't serve N users.
            # We read trades directly from each user's SQLite DB instead.
            # Freqtrade 2026+ requires listen_ip_address and jwt_secret_key
            # even when api_server is disabled.
            "api_server": {
                "enabled": False,
                "listen_ip_address": "127.0.0.1",
                "listen_port": 8080,
                "verbosity": "error",
                "enable_openapi": False,
                "jwt_secret_key": "somethingRandomSomethingRandom123",
                "ws_token": "DeprecatedSoon",
                "CORS_origins": [],
                "username": "",
                "password": "",
            },
        }
        if trailing_stop_pct and trailing_stop_pct > 0:
            config["trailing_stop"] = True
            config["trailing_stop_positive"] = float(trailing_stop_pct) / 100.0
            config["trailing_stop_positive_offset"] = float(trailing_stop_pct) / 100.0 * 1.5
            config["trailing_only_offset_is_reached"] = True
        if take_profit_pct and take_profit_pct > 0:
            config["minimal_roi"] = {"0": float(take_profit_pct) / 100.0}
        return config

    def _write_config(self, config: dict):
        self._userdir.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w") as f:
            json.dump(config, f, indent=2)

    @staticmethod
    def _compute_stake(wallet: float, max_open_trades: int, max_position_pct: float) -> float:
        if not wallet or wallet <= 0:
            return 0
        pct_stake = wallet * (max_position_pct / 100.0)
        slot_stake = wallet / max(1, max_open_trades)
        return round(min(pct_stake, slot_stake), 2)

    def _strategy_paths(self) -> list[str]:
        """Search paths fed to freqtrade so it finds user-generated and template
        strategies. Each user's generated strategies live under
        `strategies/user_generated/<user_id>/`; templates are global."""
        base = Path("strategies")
        flags: list[str] = []
        per_user = base / "user_generated" / self._safe_id
        if per_user.exists():
            flags += ["--strategy-path", str(per_user.resolve())]
        # Fall back to legacy flat folder for backwards compat.
        legacy = base / "user_generated"
        if legacy.exists():
            flags += ["--strategy-path", str(legacy.resolve())]
        templates = base / "templates"
        if templates.exists():
            flags += ["--strategy-path", str(templates.resolve())]
        return flags

    # ----- lifecycle -----
    def start_paper(
        self,
        strategy_name: str,
        pairs: list[str],
        timeframe: str = "15m",
        stoploss: float = -0.03,
        wallet: float = 1000,
        max_open_trades: int = 3,
        max_position_pct: float = 5.0,
        trailing_stop_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        position_adjustment: bool = False,
    ) -> dict:
        if self.is_running:
            return {"error": "Bot is already running for this user. Stop it first."}
        stake = self._compute_stake(wallet, max_open_trades, max_position_pct)
        config = self._build_config(
            dry_run=True,
            strategy_name=strategy_name,
            pairs=pairs,
            stake_amount=stake,
            timeframe=timeframe,
            stoploss=stoploss,
            wallet=wallet,
            max_open_trades=max_open_trades,
            trailing_stop_pct=trailing_stop_pct,
            take_profit_pct=take_profit_pct,
            position_adjustment=position_adjustment,
        )
        self._write_config(config)
        return self._start_process(strategy_name, "paper")

    def start_live(
        self,
        strategy_name: str,
        pairs: list[str],
        timeframe: str,
        stoploss: float,
        kucoin_key: str,
        kucoin_secret: str,
        kucoin_passphrase: str,
        wallet: float = 1000,
        max_open_trades: int = 3,
        max_position_pct: float = 5.0,
        trailing_stop_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        position_adjustment: bool = False,
    ) -> dict:
        if self.is_running:
            return {"error": "Bot is already running for this user. Stop it first."}
        stake = self._compute_stake(wallet, max_open_trades, max_position_pct)
        config = self._build_config(
            dry_run=False,
            strategy_name=strategy_name,
            pairs=pairs,
            stake_amount=stake,
            timeframe=timeframe,
            stoploss=stoploss,
            kucoin_key=kucoin_key,
            kucoin_secret=kucoin_secret,
            kucoin_passphrase=kucoin_passphrase,
            wallet=wallet,
            max_open_trades=max_open_trades,
            trailing_stop_pct=trailing_stop_pct,
            take_profit_pct=take_profit_pct,
            position_adjustment=position_adjustment,
        )
        self._write_config(config)
        return self._start_process(strategy_name, "live")

    def _start_process(self, strategy_name: str, mode: str) -> dict:
        try:
            self._userdir.mkdir(parents=True, exist_ok=True)
            cmd = _resolve_freqtrade_cmd() + [
                "trade",
                "--config", str(self._config_path),
                "--strategy", strategy_name,
                "--userdir", str(self._userdir),
                *self._strategy_paths(),
            ]
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._mode = mode
            self._strategy = strategy_name
            return {
                "started": True,
                "mode": mode,
                "pid": self._process.pid,
                "user_id": self.user_id,
            }
        except FileNotFoundError:
            return {"error": f"Freqtrade not found at: {FREQTRADE_PATH}. Install it first."}
        except Exception as e:
            return {"error": str(e)}

    def stop(self) -> dict:
        if not self.is_running:
            return {"stopped": True, "message": "Bot was not running"}
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        mode = self._mode
        self._process = None
        self._mode = ""
        self._strategy = ""
        return {"stopped": True, "mode": mode, "user_id": self.user_id}

    # ----- backtesting -----

    # KuCoin timeframe label → (API kline type, candle seconds)
    _TF_INFO: dict[str, tuple[str, int]] = {
        "1m":  ("1min",   60),
        "3m":  ("3min",   180),
        "5m":  ("5min",   300),
        "15m": ("15min",  900),
        "30m": ("30min",  1800),
        "1h":  ("1hour",  3600),
        "2h":  ("2hour",  7200),
        "4h":  ("4hour",  14400),
        "6h":  ("6hour",  21600),
        "8h":  ("8hour",  28800),
        "12h": ("12hour", 43200),
        "1d":  ("1day",   86400),
        "1w":  ("1week",  604800),
    }

    def _download_pair_from_kucoin(
        self,
        pair: str,
        timeframe: str,
        start_ts: int,
        end_ts: int,
        data_dir: Path,
    ) -> dict | None:
        """Download OHLCV data from KuCoin public REST API (no API keys needed).

        KuCoin candle format per element:
          [timestamp, open, close, high, low, volume, turnover]

        Freqtrade feather expects columns:
          date (tz-aware UTC datetime), open, high, low, close, volume

        Returns None on success, error dict on failure.
        """
        import pandas as pd  # local import keeps startup fast; pandas is always installed

        kline_type, tf_secs = self._TF_INFO.get(timeframe, ("15min", 900))
        symbol = pair.replace("/", "-")          # BTC/USDT → BTC-USDT
        chunk_secs = 1500 * tf_secs              # KuCoin max 1 500 candles/request

        all_rows: list[list] = []
        current_start = start_ts

        while current_start < end_ts:
            current_end = min(current_start + chunk_secs, end_ts)
            try:
                # Use stdlib urllib — zero pip dependencies, always available
                qs = urllib.parse.urlencode({
                    "type": kline_type,
                    "symbol": symbol,
                    "startAt": current_start,
                    "endAt": current_end,
                })
                url = f"https://api.kucoin.com/api/v1/market/candles?{qs}"
                req = urllib.request.Request(url, headers={"User-Agent": "AutoTradeHub/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
            except Exception as exc:
                return {"error": f"KuCoin API network error for {pair}: {exc}"}

            if str(data.get("code")) != "200000":
                return {
                    "error": (
                        f"KuCoin API error for {pair}: "
                        f"code={data.get('code')} msg={data.get('msg', 'unknown')}"
                    )
                }

            rows = data.get("data", [])
            all_rows.extend(rows)
            current_start = current_end + 1
            if not rows:
                break  # no data in this window, don't spin forever

        if not all_rows:
            return {
                "error": (
                    f"No OHLCV data returned by KuCoin for {pair} ({timeframe}) "
                    f"in range {datetime.utcfromtimestamp(start_ts).date()} – "
                    f"{datetime.utcfromtimestamp(end_ts).date()}. "
                    "The pair may not have existed at that date."
                )
            }

        # Build DataFrame — KuCoin order: timestamp, open, CLOSE, HIGH, low, volume, turnover
        df = pd.DataFrame(
            all_rows,
            columns=["timestamp", "open", "close", "high", "low", "volume", "turnover"],
        )
        df["date"] = pd.to_datetime(df["timestamp"].astype(int), unit="s", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)

        df = (
            df[["date", "open", "high", "low", "close", "volume"]]
            .sort_values("date")
            .drop_duplicates("date")
            .reset_index(drop=True)
        )

        stem = f"{pair.replace('/', '_')}-{timeframe}"
        df.to_feather(data_dir / f"{stem}.feather")
        return None  # success

    def _ensure_historical_data(
        self,
        pairs: list[str],
        timeframe: str,
        timerange: str,
    ) -> dict | None:
        """Ensure candle data for `pairs` exists locally.

        Strategy:
        1. Skip pairs that already have a local feather file.
        2. For missing pairs, try Freqtrade download-data (reliable when
           Freqtrade can reach the exchange).
        3. If that fails (common in dev/firewalled environments because
           Freqtrade's async market-loader can't connect), fall back to the
           KuCoin public REST API which needs no exchange initialisation.
        """
        data_dir = self._userdir / "data" / "kucoin"
        data_dir.mkdir(parents=True, exist_ok=True)

        def _has_data(pair: str) -> bool:
            stem = f"{pair.replace('/', '_')}-{timeframe}"
            for ext in ("feather", "json", "json.gz", "parquet"):
                if (data_dir / f"{stem}.{ext}").exists():
                    return True
            return False

        missing = [p for p in pairs if not _has_data(p)]
        if not missing:
            return None  # all data already cached

        # ── Parse timerange ─────────────────────────────────────────────────
        # Expected format: YYYYMMDD-YYYYMMDD  (e.g. "20240101-20240401")
        try:
            parts = timerange.split("-")
            start_ts = int(
                datetime(int(parts[0][:4]), int(parts[0][4:6]), int(parts[0][6:8])).timestamp()
            )
            end_ts = int(
                datetime(int(parts[1][:4]), int(parts[1][4:6]), int(parts[1][6:8])).timestamp()
            )
        except Exception:
            return {"error": f"Invalid timerange '{timerange}'. Expected YYYYMMDD-YYYYMMDD"}

        # ── Attempt 1: Freqtrade download-data ──────────────────────────────
        dl_config = {
            "max_open_trades": 1,
            "stake_currency": "USDT",
            "stake_amount": "unlimited",
            "dry_run": True,
            "trading_mode": "spot",
            "fiat_display_currency": "USD",
            "exchange": {
                "name": "kucoin",
                "key": "", "secret": "", "password": "",
                "pair_whitelist": missing,
                "pair_blacklist": [],
                "ccxt_config": {"enableRateLimit": True},
                "ccxt_async_config": {"enableRateLimit": True},
            },
            "pairlists": [{"method": "StaticPairList"}],
            "dataformat_ohlcv": "feather",
            "api_server": {
                "enabled": False,
                "listen_ip_address": "127.0.0.1",
                "listen_port": 8080,
                "verbosity": "error",
                "enable_openapi": False,
                "jwt_secret_key": "somethingRandomSomethingRandom123",
                "ws_token": "DeprecatedSoon",
                "CORS_origins": [],
                "username": "",
                "password": "",
            },
        }
        dl_config_path = self._userdir / "dl_config.json"
        self._userdir.mkdir(parents=True, exist_ok=True)
        with open(dl_config_path, "w") as f:
            json.dump(dl_config, f, indent=2)

        cmd = _resolve_freqtrade_cmd() + [
            "download-data",
            "--config", str(dl_config_path),
            "--pairs", *missing,
            "--timeframes", timeframe,
            "--timerange", timerange,
            "--userdir", str(self._userdir),
            "--data-format-ohlcv", "feather",
        ]
        ft_ok = False
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            ft_ok = res.returncode == 0
        except Exception:
            ft_ok = False

        if ft_ok:
            return None  # Freqtrade download succeeded

        # ── Attempt 2: KuCoin public REST API (no exchange init needed) ──────
        still_missing = [p for p in missing if not _has_data(p)]
        for pair in still_missing:
            err = self._download_pair_from_kucoin(
                pair, timeframe, start_ts, end_ts, data_dir
            )
            if err:
                return err  # surface the first failure

        return None  # all pairs downloaded

    def run_backtest(
        self,
        strategy_name: str,
        pairs: list[str],
        timeframe: str,
        timerange: str,
        stoploss: float = -0.03,
        starting_balance: float = 1000,
    ) -> dict:
        # If a live/paper bot is running, pause it during backtest so we
        # don't overwrite its config mid-run, then restart after.
        bot_was_running = self.is_running
        saved_mode = self._mode
        saved_strategy = self._strategy
        if bot_was_running:
            self.stop()

        bt_stake = self._compute_stake(starting_balance, 3, 33.3)
        config = self._build_config(
            dry_run=True,
            strategy_name=strategy_name,
            pairs=pairs,
            stake_amount=bt_stake,
            timeframe=timeframe,
            stoploss=stoploss,
            wallet=starting_balance,
        )
        self._write_config(config)

        dl_err = self._ensure_historical_data(pairs, timeframe, timerange)
        if dl_err is not None:
            return dl_err

        results_dir = self._userdir / "backtest_results"
        results_dir.mkdir(parents=True, exist_ok=True)
        existing_before = {
            p.name
            for p in list(results_dir.glob("backtest-result-*.zip"))
            + list(results_dir.glob("backtest-result-*.json"))
            if not p.name.endswith(".meta.json")
        }

        cmd = _resolve_freqtrade_cmd() + [
            "backtesting",
            "--config", str(self._config_path),
            "--strategy", strategy_name,
            "--userdir", str(self._userdir),
            "--timerange", timerange,
            "--export", "trades",
            *self._strategy_paths(),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            if result.returncode != 0:
                tail = stderr.splitlines()[-5:] if stderr else stdout.splitlines()[-5:]
                return {"error": "\n".join(tail) or "Backtest failed"}
            candidates = [
                p
                for p in list(results_dir.glob("backtest-result-*.zip"))
                + list(results_dir.glob("backtest-result-*.json"))
                if not p.name.endswith(".meta.json")
            ]
            all_files = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
            new_files = [p for p in all_files if p.name not in existing_before]
            result_files = new_files or all_files
            if not result_files:
                tail = stderr.splitlines()[-5:] if stderr else stdout.splitlines()[-5:]
                reason = "\n".join(tail).strip()
                return {"error": reason or "No results file generated"}
            data = _load_backtest_result(result_files[0])
            return {"success": True, "results": data}
        except subprocess.TimeoutExpired:
            return {"error": "Backtest timed out (5min limit)"}
        except FileNotFoundError:
            return {"error": f"Freqtrade not found at: {FREQTRADE_PATH}"}
        except Exception as e:
            return {"error": str(e)}
        finally:
            # Restore the bot that was running before the backtest
            if bot_was_running and saved_strategy and saved_mode:
                try:
                    self._start_process(saved_strategy, saved_mode)
                except Exception:
                    pass


class FreqtradeRegistry:
    """Process-wide registry of per-user FreqtradeManager instances.

    Concurrency: a Lock guards the dict so two simultaneous requests for
    the same brand-new user can't race to create two managers. Each
    manager's own lifecycle (start/stop/poll) is single-threaded per user
    because FastAPI handles each request on a worker.
    """

    def __init__(self) -> None:
        self._managers: dict[str, FreqtradeManager] = {}
        self._lock = threading.Lock()

    def for_user(self, user_id: str) -> FreqtradeManager:
        with self._lock:
            mgr = self._managers.get(user_id)
            if mgr is None:
                mgr = FreqtradeManager(user_id)
                self._managers[user_id] = mgr
            return mgr

    def active_users(self) -> list[str]:
        with self._lock:
            return [uid for uid, m in self._managers.items() if m.is_running]

    def stop_all(self) -> None:
        with self._lock:
            for m in self._managers.values():
                if m.is_running:
                    try:
                        m.stop()
                    except Exception:
                        pass


freqtrade_mgr = FreqtradeRegistry()
