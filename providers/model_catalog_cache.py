from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
CATALOG_CACHE_TTL_SECONDS = 6 * 60 * 60
_DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
CATALOG_CACHE_PATH = _DATA_DIR / "model_catalog_cache.json"

_refresh_lock = threading.Lock()
_refresh_state_lock = threading.Lock()
_refresh_state: dict[str, Any] = {
    "running": False,
    "started_at": 0.0,
    "finished_at": 0.0,
    "reason": "",
    "last_result": None,
}


@dataclass(frozen=True)
class CatalogCacheSnapshot:
    version: int
    generated_at: float
    cloud_cache: dict[str, dict[str, Any]]
    ollama_rows: list[dict[str, Any]]
    provider_status: dict[str, dict[str, Any]]
    warnings: tuple[str, ...]
    reason: str = ""

    @property
    def age_seconds(self) -> float:
        if self.generated_at <= 0:
            return float("inf")
        return max(0.0, time.time() - self.generated_at)

    @property
    def is_empty(self) -> bool:
        return not self.cloud_cache and not self.ollama_rows

    @property
    def is_stale(self) -> bool:
        return self.is_empty or self.age_seconds >= CATALOG_CACHE_TTL_SECONDS

    @property
    def total_rows(self) -> int:
        return len(self.cloud_cache) + len(self.ollama_rows)


def empty_catalog_cache() -> CatalogCacheSnapshot:
    return CatalogCacheSnapshot(
        version=CACHE_VERSION,
        generated_at=0.0,
        cloud_cache={},
        ollama_rows=[],
        provider_status={},
        warnings=(),
        reason="empty",
    )


def read_model_catalog_cache(path: pathlib.Path | None = None) -> CatalogCacheSnapshot:
    cache_path = path or CATALOG_CACHE_PATH
    try:
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            snapshot = _snapshot_from_payload(payload)
            if snapshot.version == CACHE_VERSION:
                return snapshot
            logger.warning("Ignoring model catalog cache with unsupported version: %s", snapshot.version)
    except Exception:
        logger.warning("Failed to load model catalog cache from %s", cache_path, exc_info=True)
    return _bootstrap_snapshot_from_runtime()


def write_model_catalog_cache(snapshot: CatalogCacheSnapshot, path: pathlib.Path | None = None) -> None:
    cache_path = path or CATALOG_CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(_snapshot_to_payload(snapshot), indent=2), encoding="utf-8")
    tmp_path.replace(cache_path)


def build_cached_model_catalog_rows(
    *,
    defaults: dict[str, str] | None = None,
    quick_choices: Iterable[dict[str, Any]] | None = None,
    snapshot: CatalogCacheSnapshot | None = None,
):
    from providers.model_catalog import build_model_catalog_rows

    snap = snapshot or read_model_catalog_cache()
    return build_model_catalog_rows(
        cloud_cache=snap.cloud_cache,
        ollama_rows=snap.ollama_rows,
        defaults=defaults,
        quick_choices=quick_choices,
    )


def refresh_model_catalog_cache(
    *,
    reason: str = "manual",
    force: bool = False,
    provider_id: str | None = None,
) -> CatalogCacheSnapshot:
    """Refresh model catalog metadata synchronously.

    This is intended to run in a worker thread. It always preserves the
    previous cache if refresh work fails before a usable snapshot is produced.
    """
    previous = read_model_catalog_cache()
    if not force and not previous.is_stale:
        logger.info(
            "model catalog cache refresh skipped; cache fresh age=%.0fs reason=%s",
            previous.age_seconds,
            reason,
        )
        return previous

    started = time.perf_counter()
    warnings: list[str] = []
    provider_status: dict[str, dict[str, Any]] = {}
    cloud_cache = dict(previous.cloud_cache)
    ollama_rows = list(previous.ollama_rows)

    try:
        cloud_cache, cloud_status = _refresh_cloud_cache(provider_id=provider_id)
        provider_status.update(cloud_status)
    except Exception as exc:
        warnings.append(f"Cloud providers refresh failed: {exc}")
        logger.warning("Cloud model catalog refresh failed; preserving previous cloud cache", exc_info=True)

    should_refresh_ollama = provider_id in {None, "", "ollama", "ollama_cloud"}
    if should_refresh_ollama:
        try:
            ollama_rows = _refresh_ollama_rows()
            provider_status["ollama"] = {"status": "ok", "count": len(ollama_rows)}
        except Exception as exc:
            warnings.append(f"Ollama catalog refresh failed: {exc}")
            logger.warning("Ollama model catalog refresh failed; preserving previous Ollama rows", exc_info=True)

    snapshot = CatalogCacheSnapshot(
        version=CACHE_VERSION,
        generated_at=time.time(),
        cloud_cache=cloud_cache,
        ollama_rows=ollama_rows,
        provider_status=provider_status,
        warnings=tuple(warnings),
        reason=reason,
    )
    if snapshot.is_empty and not previous.is_empty:
        logger.warning("Model catalog refresh produced an empty cache; preserving previous last-known-good cache")
        return previous
    write_model_catalog_cache(snapshot)
    logger.info(
        "perf: model catalog cache refreshed in %.3fs rows=%d warnings=%d reason=%s provider=%s",
        time.perf_counter() - started,
        snapshot.total_rows,
        len(snapshot.warnings),
        reason,
        provider_id or "all",
    )
    return snapshot


def start_model_catalog_refresh_background(
    *,
    reason: str = "manual",
    force: bool = False,
    provider_id: str | None = None,
) -> bool:
    """Start a coalesced daemon-thread refresh. Returns False if one is already running."""
    with _refresh_state_lock:
        if _refresh_state.get("running"):
            return False
        _refresh_state.update({
            "running": True,
            "started_at": time.time(),
            "finished_at": 0.0,
            "reason": reason,
            "last_result": None,
        })

    def _runner() -> None:
        result: dict[str, Any]
        if not _refresh_lock.acquire(blocking=False):
            result = {"ok": False, "coalesced": True, "message": "Refresh already running"}
        else:
            try:
                snapshot = refresh_model_catalog_cache(reason=reason, force=force, provider_id=provider_id)
                result = {
                    "ok": True,
                    "rows": snapshot.total_rows,
                    "warnings": list(snapshot.warnings),
                    "generated_at": snapshot.generated_at,
                    "reason": reason,
                }
            except Exception as exc:
                logger.warning("Background model catalog refresh failed", exc_info=True)
                result = {"ok": False, "message": str(exc), "reason": reason}
            finally:
                _refresh_lock.release()
        with _refresh_state_lock:
            _refresh_state.update({
                "running": False,
                "finished_at": time.time(),
                "last_result": result,
            })

    thread = threading.Thread(target=_runner, name="model-catalog-refresh", daemon=True)
    thread.start()
    return True


def model_catalog_refresh_state() -> dict[str, Any]:
    with _refresh_state_lock:
        return dict(_refresh_state)


def is_model_catalog_refresh_running() -> bool:
    return bool(model_catalog_refresh_state().get("running"))


def schedule_model_catalog_refresh_jobs() -> None:
    """Schedule delayed startup and periodic model catalog refresh jobs."""
    try:
        from tasks import _get_scheduler

        scheduler = _get_scheduler()
        scheduler.add_job(
            lambda: start_model_catalog_refresh_background(reason="startup", force=False),
            trigger="date",
            run_date=datetime.now() + timedelta(seconds=45),
            id="model_catalog_startup_refresh",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            lambda: start_model_catalog_refresh_background(reason="scheduled", force=False),
            trigger="interval",
            hours=6,
            id="model_catalog_periodic_refresh",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("Model catalog cache refresh scheduled (startup delay + every 6 h)")
    except Exception:
        logger.warning("Could not schedule model catalog cache refresh", exc_info=True)


def cache_age_label(snapshot: CatalogCacheSnapshot | None = None) -> str:
    snap = snapshot or read_model_catalog_cache()
    if snap.generated_at <= 0:
        return "Not cached yet"
    age = snap.age_seconds
    if age < 60:
        return "Updated just now"
    if age < 3600:
        return f"Updated {int(age // 60)}m ago"
    if age < 86400:
        return f"Updated {int(age // 3600)}h ago"
    return f"Updated {int(age // 86400)}d ago"


def _refresh_cloud_cache(*, provider_id: str | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    import models

    provider_status: dict[str, dict[str, Any]] = {}
    if provider_id:
        models.fetch_context_catalog()
        with models._cloud_cache_lock:
            retained = {
                model_id: info
                for model_id, info in models._cloud_model_cache.items()
                if not (isinstance(info, dict) and info.get("provider") == provider_id)
            }
            models._cloud_model_cache.clear()
            models._cloud_model_cache.update(retained)
        count = models.fetch_cloud_models(provider_id)
        models._save_cloud_cache()
        provider_status[provider_id] = {"status": "ok", "count": count}
    else:
        count = models.refresh_cloud_models()
        provider_status["cloud"] = {"status": "ok", "count": count}
    with models._cloud_cache_lock:
        cloud_cache = {
            str(model_id): dict(info)
            for model_id, info in models._cloud_model_cache.items()
            if isinstance(info, dict)
        }
    return cloud_cache, provider_status


def _refresh_ollama_rows() -> list[dict[str, Any]]:
    from providers.model_catalog import load_ollama_catalog_rows

    return [dict(row) for row in load_ollama_catalog_rows()]


def _bootstrap_snapshot_from_runtime() -> CatalogCacheSnapshot:
    cloud_cache: dict[str, dict[str, Any]] = {}
    try:
        import models

        with models._cloud_cache_lock:
            cloud_cache = {
                str(model_id): dict(info)
                for model_id, info in models._cloud_model_cache.items()
                if isinstance(info, dict)
            }
    except Exception:
        cloud_cache = {}
    generated_at = time.time() if cloud_cache else 0.0
    return CatalogCacheSnapshot(
        version=CACHE_VERSION,
        generated_at=generated_at,
        cloud_cache=cloud_cache,
        ollama_rows=[],
        provider_status={"bootstrap": {"status": "ok", "count": len(cloud_cache)}} if cloud_cache else {},
        warnings=(),
        reason="bootstrap",
    )


def _snapshot_from_payload(payload: Any) -> CatalogCacheSnapshot:
    if not isinstance(payload, dict):
        return empty_catalog_cache()
    cloud_cache = {
        str(model_id): dict(info)
        for model_id, info in (payload.get("cloud_cache") or {}).items()
        if isinstance(info, dict)
    } if isinstance(payload.get("cloud_cache"), dict) else {}
    ollama_rows = [
        dict(row)
        for row in (payload.get("ollama_rows") or [])
        if isinstance(row, dict)
    ] if isinstance(payload.get("ollama_rows"), list) else []
    provider_status = {
        str(provider): dict(status)
        for provider, status in (payload.get("provider_status") or {}).items()
        if isinstance(status, dict)
    } if isinstance(payload.get("provider_status"), dict) else {}
    warnings = tuple(str(item) for item in payload.get("warnings") or [] if str(item))
    return CatalogCacheSnapshot(
        version=int(payload.get("version") or 0),
        generated_at=float(payload.get("generated_at") or 0.0),
        cloud_cache=cloud_cache,
        ollama_rows=ollama_rows,
        provider_status=provider_status,
        warnings=warnings,
        reason=str(payload.get("reason") or ""),
    )


def _snapshot_to_payload(snapshot: CatalogCacheSnapshot) -> dict[str, Any]:
    return {
        "version": snapshot.version,
        "generated_at": snapshot.generated_at,
        "generated_at_iso": datetime.fromtimestamp(snapshot.generated_at).isoformat() if snapshot.generated_at else "",
        "reason": snapshot.reason,
        "cloud_cache": snapshot.cloud_cache,
        "ollama_rows": snapshot.ollama_rows,
        "provider_status": snapshot.provider_status,
        "warnings": list(snapshot.warnings),
    }
