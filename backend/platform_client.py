"""Thin client for the LingoScribe platform's /v1/deduct endpoint.

The platform endpoint is dual-purpose:
  - Empty body  → validate key + read balance (no charge).
  - {minutes:N} → validate + charge (or 403 if insufficient).

This module wraps both flows and adds a 60-second in-process cache for
validation-only calls (so the cache-hit re-view path stays fast).

NOTE: The cache is per-process. Multi-worker deployments (gunicorn -w N)
should swap this for Redis or similar. Single-worker uvicorn (current dev
setup) is fine.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Optional

import requests

PLATFORM_BASE_URL = os.getenv("PLATFORM_BASE_URL", "http://localhost:5000")
DEDUCT_ENDPOINT = f"{PLATFORM_BASE_URL.rstrip('/')}/v1/deduct"
DEDUCT_TIMEOUT = float(os.getenv("PLATFORM_TIMEOUT_SECONDS", "10"))

API_KEY_HASH_PEPPER = os.getenv("API_KEY_HASH_PEPPER", "dev-pepper-change-in-prod")
VALIDATION_TTL_SECONDS = 60


@dataclass
class DeductResult:
    """Outcome of a /v1/deduct call. Maps directly to platform response cases."""
    valid: bool
    balance_minutes: Optional[float]   # None if call errored or key invalid
    quota_exceeded: bool = False       # True iff platform returned 403
    error: Optional[str] = None        # Human-readable for logs / debugging
    raw_status: Optional[int] = None


def hash_api_key(api_key: str) -> str:
    """SHA-256(pepper + ':' + api_key). Used as the user identifier in our DB
    so a leak doesn't expose raw platform credentials."""
    payload = f"{API_KEY_HASH_PEPPER}:{api_key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# -------------------------------------------------------------------------
# Validation cache (positive AND negative results, both 60s TTL)
# -------------------------------------------------------------------------

_validation_cache: dict[str, tuple[float, DeductResult]] = {}
_cache_lock = Lock()


def _cache_get(api_key_hash: str) -> Optional[DeductResult]:
    now = time.time()
    with _cache_lock:
        entry = _validation_cache.get(api_key_hash)
        if entry and entry[0] > now:
            return entry[1]
        if entry:
            _validation_cache.pop(api_key_hash, None)
    return None


def _cache_put(api_key_hash: str, result: DeductResult) -> None:
    with _cache_lock:
        _validation_cache[api_key_hash] = (time.time() + VALIDATION_TTL_SECONDS, result)


def invalidate_cache(api_key: str) -> None:
    """Drop the cached validation for this key. Call after any successful
    deduction so the cached balance doesn't lie about the real balance."""
    h = hash_api_key(api_key)
    with _cache_lock:
        _validation_cache.pop(h, None)


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------

def _post_deduct(api_key: str, minutes: Optional[float]) -> DeductResult:
    """Single HTTP call to /v1/deduct. Used by both validate and charge paths."""
    body = {"minutes": minutes} if minutes is not None else {}
    try:
        resp = requests.post(
            DEDUCT_ENDPOINT,
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=DEDUCT_TIMEOUT,
        )
    except requests.RequestException as e:
        return DeductResult(valid=False, balance_minutes=None,
                            error=f"platform unreachable: {e}", raw_status=None)

    if resp.status_code == 401:
        return DeductResult(valid=False, balance_minutes=None,
                            error="invalid/revoked key", raw_status=401)
    if resp.status_code == 400:
        return DeductResult(valid=False, balance_minutes=None,
                            error=f"bad request: {resp.text[:200]}", raw_status=400)
    if resp.status_code == 403:
        # Quota exceeded. Body includes valid:true + balance.
        try:
            data = resp.json()
            details = data.get("details") or data
            balance = details.get("balance_minutes")
        except Exception:
            balance = None
        return DeductResult(valid=True, balance_minutes=balance,
                            quota_exceeded=True, raw_status=403)
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            return DeductResult(valid=False, balance_minutes=None,
                                error="non-json 200 response", raw_status=200)
        return DeductResult(
            valid=bool(data.get("valid", True)),
            balance_minutes=data.get("balance_minutes"),
            raw_status=200,
        )

    return DeductResult(valid=False, balance_minutes=None,
                        error=f"unexpected status {resp.status_code}: {resp.text[:200]}",
                        raw_status=resp.status_code)


def validate(api_key: str, *, use_cache: bool = True) -> DeductResult:
    """Validate-only call (empty body). No deduction. Cached for 60s."""
    if not api_key:
        return DeductResult(valid=False, balance_minutes=None, error="empty key")

    h = hash_api_key(api_key)
    if use_cache:
        cached = _cache_get(h)
        if cached is not None:
            return cached

    result = _post_deduct(api_key, minutes=None)
    # Cache both positive AND negative results (negatives at shorter TTL would
    # be ideal, but a single 60s window is fine for dev).
    _cache_put(h, result)
    return result


def deduct(api_key: str, minutes: float) -> DeductResult:
    """Charge `minutes` from the user's balance. Bypasses cache (the cache is
    for validation only — every charge hits the platform). Invalidates the
    validation cache so subsequent reads see fresh balance."""
    if not api_key:
        return DeductResult(valid=False, balance_minutes=None, error="empty key")
    if minutes is None or minutes <= 0:
        return DeductResult(valid=False, balance_minutes=None,
                            error=f"invalid minutes: {minutes}")

    print(f"[Platform] 💰 Deducting {minutes:.2f} min from key ...{api_key[-6:]}")
    result = _post_deduct(api_key, minutes=minutes)
    print(f"[Platform]    └─ result: status={result.raw_status} valid={result.valid} "
          f"balance={result.balance_minutes} quota_exceeded={result.quota_exceeded}")

    # Whatever the outcome, the cached validation is now stale. Drop it.
    invalidate_cache(api_key)
    return result
