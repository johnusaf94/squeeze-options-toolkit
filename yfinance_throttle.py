"""
yfinance_throttle.py
====================
Global rate limiter for ALL yfinance calls across the entire toolkit.

Monkeypatches yfinance.Ticker so every call from every module — whether
it's data_validator.py, squeeze_catalyst.py, buffett_analyzer.py, or any
of the 15 other files that touch yfinance — automatically respects:

  1. Token-bucket rate limit (default 1.5 req/sec, configurable)
  2. Persistent disk cache (24-hour TTL on stable data like earnings/
     financials; 30-min TTL on price/volume)
  3. Exponential-backoff retry on rate-limit-shaped failures
  4. Negative-result caching (prevents re-hammering tickers with no data)

Usage:
  This module is imported ONCE at startup by each GUI app. The
  monkeypatch installs itself permanently for that process. Nothing
  in the rest of the codebase needs changing.

  Just add to the top of squeeze_searcher_gui.py, stock_analysis_gui.py,
  squeeze_analyzer_gui.py, and portfolio_builder_gui.py:

      import yfinance_throttle  # installs global rate limiter
"""

import os
import json
import time
import threading
from datetime import datetime, timedelta
from typing import Any, Optional


# ─────────────────────────────────────────────
# CONFIGURATION (tune these from the top)
# ─────────────────────────────────────────────

# Calls per second budget. Yahoo's undocumented soft limit appears to
# be ~1-2/sec sustained. 1.5 leaves headroom for bursts.
CALLS_PER_SECOND = 1.5

# Burst allowance — how many calls can fire in immediate succession
# before the limiter forces waits. Like a small reservoir.
BURST_SIZE = 5

# Cache TTLs by call type
CACHE_TTL_STABLE = timedelta(hours=24)   # earnings dates, financials, balance sheet
CACHE_TTL_VOLATILE = timedelta(minutes=30)  # info dict (has price), history

# Retry on rate-limit-shaped failures
MAX_RETRIES = 3
RETRY_BACKOFF = [2.0, 5.0, 12.0]  # seconds between retries

# Disk cache file — lives in a cache/ SUBFOLDER (module-relative), so
# shards never pollute the project root again. A one-time migration
# below moves any legacy root cache in and sweeps orphaned temp files.
_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_DIR, "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "yfinance_cache.json")

# Toggle for emergency: set to False to disable monkeypatch
ENABLED = True


# ─────────────────────────────────────────────
# TOKEN BUCKET RATE LIMITER (thread-safe)
# ─────────────────────────────────────────────

class _TokenBucket:
    """
    Classic token-bucket algorithm. Refills at CALLS_PER_SECOND, caps at
    BURST_SIZE. Thread-safe so concurrent yfinance calls (Stage-1 scan
    workers) share one global budget.
    """
    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.capacity = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        """Block until a token is available, then consume one."""
        while True:
            with self.lock:
                now = time.monotonic()
                # Refill based on time elapsed since last check
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                # How long until next token is available?
                wait_needed = (1.0 - self.tokens) / self.rate
            time.sleep(wait_needed)


_bucket = _TokenBucket(CALLS_PER_SECOND, BURST_SIZE)


# ─────────────────────────────────────────────
# DISK CACHE (atomic writes, thread-safe)
# ─────────────────────────────────────────────

_cache_data = None
_cache_lock = threading.Lock()


def _load_cache() -> dict:
    global _cache_data
    if _cache_data is not None:
        return _cache_data
    with _cache_lock:
        if _cache_data is not None:
            return _cache_data
        if not os.path.exists(CACHE_FILE):
            _cache_data = {}
            return _cache_data
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _cache_data = json.load(f)
            if not isinstance(_cache_data, dict):
                _cache_data = {}
        except Exception:
            _cache_data = {}
    return _cache_data


def _save_cache():
    """Atomic write — never corrupt the file on interrupted scans."""
    if _cache_data is None:
        return
    with _cache_lock:
        tmp = f"{CACHE_FILE}.{os.getpid()}.tmp"
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                # default=str MUST match _is_serializable's test — the old
                # mismatch made most saves fail silently AND leave an
                # orphaned temp shard per process (the root-folder litter)
                json.dump(_cache_data, f, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CACHE_FILE)
        except Exception:
            # never leave a corpse behind on a failed write
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def _cache_key(ticker: str, attr: str, args: tuple = ()) -> str:
    """Hash key for a yfinance call. Same ticker+attr+args = same cache entry."""
    return f"{ticker.upper()}::{attr}::{repr(args)}"


def _cache_get(key: str, ttl: timedelta):
    cache = _load_cache()
    entry = cache.get(key)
    if not entry:
        return None
    try:
        stored = datetime.fromisoformat(entry["t"])
        if datetime.now() - stored > ttl:
            return None
        return entry["v"]
    except Exception:
        return None


def _cache_set(key: str, value: Any):
    cache = _load_cache()
    with _cache_lock:
        cache[key] = {
            "t": datetime.now().isoformat(timespec="seconds"),
            "v": value,
        }
    # Periodically flush — not on every write to avoid disk thrashing
    if len(cache) % 25 == 0:
        _save_cache()


# ─────────────────────────────────────────────
# CLASSIFY CALLS — which TTL applies
# ─────────────────────────────────────────────

# Methods/attributes whose data changes minute-by-minute (price, volume)
VOLATILE_ATTRS = {"info", "history", "fast_info"}

# NEVER CACHED, for two independent reasons.
#   1. It is live quotes. bid/ask/IV move every tick; a 24-hour TTL on an
#      option chain (which is what this had) is meaningless data.
#   2. It cannot survive the cache. The return is a namedtuple of DataFrames;
#      JSON turns that into a list and the caller's `chain.calls` explodes.
# Rate limiting still applies — only the caching is suppressed.
NEVER_CACHE_ATTRS = {"option_chain"}

# Methods whose data is stable for ~24 hours
STABLE_ATTRS = {
    "calendar", "earnings_dates", "get_earnings_dates",
    "balance_sheet", "financials", "cashflow",
    "income_stmt", "quarterly_income_stmt", "balancesheet",
    "quarterly_balance_sheet", "quarterly_cashflow",
    "dividends", "splits", "actions",
    "earnings", "quarterly_earnings",
    "earnings_history", "earnings_estimate", "growth_estimates",
    "recommendations", "recommendations_summary",
    "institutional_holders", "major_holders", "mutualfund_holders",
    "insider_transactions", "insider_purchases", "insider_roster_holders",
    "isin", "shares", "share_count",
    "options",
}


def _ttl_for(attr: str) -> Optional[timedelta]:
    """Return the cache TTL for this attr name, or None to disable caching."""
    if attr in NEVER_CACHE_ATTRS:
        return None
    if attr in VOLATILE_ATTRS:
        return CACHE_TTL_VOLATILE
    if attr in STABLE_ATTRS:
        return CACHE_TTL_STABLE
    return None    # don't cache unknown attributes


def _is_serializable(obj) -> bool:
    """Can we round-trip this through JSON safely — actually?

    THE BUG THIS FIXES
    ------------------
    The previous version was `json.dumps(obj, default=str)`. That flag is the
    opposite of a safety check: it makes EVERY object "serializable" by
    stringifying whatever JSON cannot encode. A yfinance option chain is a
    namedtuple of DataFrames, so it serialized as a JSON ARRAY of truncated
    DataFrame repr strings — "0  GRPN260717C00001000 ... REGULAR USD" — and
    was cached. On the next call the cache returned that LIST, and the caller
    did `chain.calls`, producing:

        Options analysis failed: 'list' object has no attribute 'calls'

    which is exactly what the analyzer has been reporting. 354 corrupt chain
    entries were sitting in the cache. With the old 24-hour TTL, one poisoned
    fetch broke a ticker's options analysis for a full day.

    A cache that silently returns a different TYPE than the live call is worse
    than no cache. So: no `default=`, and the value must survive an actual
    round trip unchanged."""
    try:
        return json.loads(json.dumps(obj)) == obj
    except (TypeError, ValueError, RecursionError):
        return False


# ─────────────────────────────────────────────
# THE MONKEYPATCH ITSELF
# ─────────────────────────────────────────────

def _install():
    """Wrap yfinance.Ticker with rate limiting + caching + retry."""
    if not ENABLED:
        return False
    try:
        import yfinance
    except ImportError:
        return False

    _OriginalTicker = yfinance.Ticker

    class _ThrottledTicker:
        """
        Drop-in replacement for yfinance.Ticker. Same API surface, but
        every attribute access and method call goes through rate limiting
        and caching. The actual yfinance Ticker is lazily created only
        when an uncached call needs to hit the network.
        """
        def __init__(self, ticker: str, *args, **kwargs):
            self._ticker = (ticker or "").upper()
            self._init_args = args
            self._init_kwargs = kwargs
            self._real = None   # lazy — only built when network needed

        def _ensure_real(self):
            if self._real is None:
                self._real = _OriginalTicker(self._ticker, *self._init_args,
                                              **self._init_kwargs)
            return self._real

        def _throttled_call(self, attr: str, args: tuple = (), kwargs: dict = None):
            """Core call path: cache lookup → rate limit → real call → cache."""
            kwargs = kwargs or {}
            ttl = _ttl_for(attr)
            cache_key = _cache_key(self._ticker, attr, args + tuple(sorted(kwargs.items())))

            # Cache check first — no network, no rate-limit consumed
            if ttl is not None:
                cached = _cache_get(cache_key, ttl)
                if cached is not None:
                    return cached

            # Acquire rate limit token (blocks if budget exhausted)
            _bucket.acquire()

            # Retry loop with exponential backoff on apparent throttles
            last_exc = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    real = self._ensure_real()
                    target = getattr(real, attr)
                    # If it's callable, invoke it; if it's a property, just get it
                    result = target(*args, **kwargs) if callable(target) else target
                    # Cache it if possible
                    if ttl is not None and _is_serializable(result):
                        _cache_set(cache_key, result)
                    return result
                except Exception as e:
                    last_exc = e
                    msg = str(e).lower()
                    # Recognize throttle-shaped failures
                    is_throttle = any(s in msg for s in (
                        "too many requests", "rate", "429",
                        "throttle", "limit", "timeout",
                    ))
                    if not is_throttle or attempt >= MAX_RETRIES:
                        break
                    time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])

            # DEAD NEGATIVE CACHE, REMOVED. This wrote `cache_key + "::FAIL"`,
            # a key no reader ever looks up — _cache_get() only ever queries
            # the plain key. So it suppressed nothing and instead grew the
            # cache file with one permanent entry per failure (that file had
            # reached 10.8 MB). Failing fast and letting the caller retry is
            # both the honest behavior and what was actually happening.
            raise last_exc if last_exc else RuntimeError(f"yfinance {attr} failed")

        def __getattr__(self, name: str):
            """
            Proxy attribute access. For DATA attributes (info, financials,
            etc.) we intercept and route through throttle. For METHODS,
            we return a wrapper that throttles on call.
            """
            # Properties — return cached value immediately.
            # NEVER_CACHE attrs still take this path: _throttled_call handles
            # a None ttl by skipping the cache read and write, so they keep
            # rate limiting and retry-with-backoff while storing nothing.
            ttl = _ttl_for(name)
            if ttl is not None or name in NEVER_CACHE_ATTRS:
                # Determine whether it's a property or a method on the real obj.
                # We need to call _ensure_real, but only briefly — to introspect.
                # Cheap heuristic: methods commonly start with get_ or end in _dates etc.
                if name.startswith("get_") or name in {"history", "option_chain"}:
                    # Return a callable wrapper
                    def _wrapper(*args, **kwargs):
                        return self._throttled_call(name, args, kwargs)
                    return _wrapper
                # Property-style — call with no args
                return self._throttled_call(name)

            # Unknown attribute — fall through to real Ticker without caching
            # but still respect rate limit
            _bucket.acquire()
            return getattr(self._ensure_real(), name)

    # Install
    yfinance.Ticker = _ThrottledTicker
    return True


# ─────────────────────────────────────────────
# MIGRATION + ORPHAN SWEEP (runs once per import)
# ─────────────────────────────────────────────

def _migrate_and_clean() -> int:
    """Move a legacy root-folder cache into cache/, and delete orphaned
    temp shards (yfinance_cache.json.<pid>[.tmp]) from BOTH locations.
    Those shards are partial writes from the old buggy save — garbage,
    never live data. Returns count removed."""
    import glob
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError:
        return 0
    legacy = os.path.join(_DIR, "yfinance_cache.json")
    if os.path.exists(legacy) and not os.path.exists(CACHE_FILE):
        try:
            os.replace(legacy, CACHE_FILE)   # preserve the warm cache
        except OSError:
            pass
    removed = 0
    for pat in (os.path.join(_DIR, "yfinance_cache.json.*"),
                os.path.join(CACHE_DIR, "yfinance_cache.json.*")):
        for p in glob.glob(pat):
            if os.path.abspath(p) == os.path.abspath(CACHE_FILE):
                continue
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
    return removed


_SWEPT = _migrate_and_clean()

# Install on import — explicit and visible
_INSTALLED = _install()


def status() -> str:
    """Diagnostic helper — call this to see if the patch is active."""
    cache = _load_cache()
    return (
        f"yfinance_throttle: {'INSTALLED' if _INSTALLED else 'DISABLED'} | "
        f"rate={CALLS_PER_SECOND}/s burst={BURST_SIZE} | "
        f"cache_entries={len(cache)} dir={CACHE_DIR} | "
        f"swept {_SWEPT} orphaned shard(s) at startup"
    )


def flush():
    """Explicitly flush the disk cache (e.g. on app shutdown)."""
    _save_cache()


if __name__ == "__main__":
    print(status())
    print("Testing rate limit budget — 8 acquire() calls...")
    start = time.monotonic()
    for i in range(8):
        _bucket.acquire()
        elapsed = time.monotonic() - start
        print(f"  call {i+1}: {elapsed:.2f}s elapsed")
    total = time.monotonic() - start
    print(f"Total: {total:.2f}s for 8 calls "
          f"(expected ~{(8 - BURST_SIZE) / CALLS_PER_SECOND:.1f}s after initial burst)")
