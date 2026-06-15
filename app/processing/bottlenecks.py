"""Connector to the AI-bottleneck tracker (bottlenecks-app).

The bottlenecks app models the optical/memory supply-chain competitive landscape
— e.g. LITE vs COHR 200G EML rivalry, Broadcom's co-packaged-optics (CPO) threat
to discrete EML, and qualification-timing risk. We read it directly (its services
import cleanly, no Streamlit needed) and turn it into a compact dict the ask layer
can reason over: per-component risk scores, their drivers, the competitor ramps,
and the TRAJECTORY across adverse scenarios (how the landscape worsens over time).

Everything degrades to None on failure — the bottlenecks app is optional.
"""
from __future__ import annotations

import csv
import logging
import sys
import time
from pathlib import Path

from app.config import config

logger = logging.getLogger(__name__)

# Optical names the tracker models (plus anything in its entity table tagged optics).
_OPTICAL = {"LITE", "COHR", "AVGO", "AAOI", "MRVL", "INNOLIGHT", "EOPTOLINK"}
# Baseline + the two scenarios that intensify optical competition over time.
_BASELINE = "TIGHT_DISCIPLINED"
_ADVERSE = ["OPTICAL_QUAL_ACCELERATION", "CPO_FASTER_THAN_EXPECTED"]

_cache: dict[str, tuple[float, dict | None]] = {}
_CACHE_TTL = 3600  # seconds — the tracker's data is seed/expert-tier, slow-moving
_entities: dict[str, dict] | None = None


def _app_dir() -> Path:
    return Path(config.BOTTLENECKS_APP_PATH)


def _ensure_path() -> None:
    p = str(_app_dir())
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_entities() -> dict[str, dict]:
    """ticker(upper) -> {entity_id, name, segment} from the tracker's seed."""
    global _entities
    if _entities is not None:
        return _entities
    _entities = {}
    try:
        with open(_app_dir() / "data" / "seed" / "entities.csv", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                tk = (row.get("ticker") or "").strip().upper()
                if tk:
                    _entities[tk] = {
                        "entity_id": (row.get("entity_id") or "").strip(),
                        "name": (row.get("name") or "").strip(),
                        "segment": (row.get("segment") or "").strip(),
                    }
    except Exception:
        logger.info("bottlenecks: entities.csv not readable")
    return _entities


def _component(comp) -> dict:
    """Flatten a ComponentRiskRange (share_pressure / qualification_timing / ...)."""
    return {
        "base": round(getattr(comp, "base", 0.0), 1),
        "low": round(getattr(comp, "low", 0.0), 1),
        "high": round(getattr(comp, "high", 0.0), 1),
        "label": getattr(comp, "label", None),
        "drivers": list(getattr(comp, "drivers", []) or [])[:4],
    }


def is_covered(ticker: str) -> bool:
    sym = (ticker or "").upper()
    if sym in _OPTICAL:
        return True
    ent = _load_entities().get(sym)
    return bool(ent and ent.get("segment") in {"optics", "memory"})


def get_landscape(ticker: str) -> dict | None:
    """Compact AI-bottleneck competitive landscape for a ticker, or None.

    For optical names: the three risk components (share pressure, qualification
    timing, CPO substitution) at baseline plus their trajectory under the adverse
    scenarios, and the competitor ramps with their impact on LITE.
    """
    sym = (ticker or "").strip().upper()
    if not sym or not is_covered(sym):
        return None
    cached = _cache.get(sym)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    result: dict | None = None
    try:
        _ensure_path()
        ent = _load_entities().get(sym, {})
        segment = ent.get("segment") or ("optics" if sym in _OPTICAL else None)

        if segment == "optics" or sym in _OPTICAL:
            from services.memory_laser.optical_monitor import DEFAULT_RAMPS
            from services.memory_laser.optical_risk_scores import compute_optical_risk_profile

            base = compute_optical_risk_profile(bundle_id=_BASELINE)
            components = {
                "share_pressure": _component(base.share_pressure),
                "qualification_timing": _component(base.qualification_timing),
                "cpo_substitution": _component(base.cpo_substitution),
            }
            # Trajectory: how each component shifts under adverse scenarios — this is
            # the "landscape gets worse over time" signal.
            trajectory = []
            for bundle in _ADVERSE:
                try:
                    p = compute_optical_risk_profile(bundle_id=bundle)
                    trajectory.append({
                        "scenario": bundle,
                        "share_pressure": round(p.share_pressure.base, 1),
                        "qualification_timing": round(p.qualification_timing.base, 1),
                        "cpo_substitution": round(p.cpo_substitution.base, 1),
                        "effects": list(getattr(p, "scenario_effects", []) or [])[:3],
                    })
                except Exception:
                    continue
            entmap = _load_entities()
            id_to_tk = {v["entity_id"]: tk for tk, v in entmap.items()}
            competitors = []
            for r in DEFAULT_RAMPS:
                competitors.append({
                    "supplier": getattr(r, "supplier", None),
                    "ticker": id_to_tk.get(getattr(r, "entity_id", None)),
                    "layer": getattr(r, "layer", None),
                    "lane_speed": getattr(r, "lane_speed", None),
                    "role": getattr(r, "role", None),
                    "lite_impact": getattr(r, "lite_impact", None),
                    "timing": getattr(r, "commercial_timing", None),
                })
            result = {
                "ticker": sym,
                "segment": "optics",
                "scale": "0-10 risk (higher = more competitive pressure / worse for incumbent)",
                "baseline_scenario": _BASELINE,
                "components": components,
                "trajectory": trajectory,
                "competitors": competitors,
                "source": "bottlenecks-app optical monitor (tier-E expert estimates)",
            }
        else:
            # Memory names: lightweight segment tag + any pending bottleneck claims.
            claims = []
            try:
                with open(_app_dir() / "data" / "seed" / "candidate_claims.csv", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        if (row.get("segment") or "").lower() == "memory":
                            claims.append({
                                "metric": row.get("metric"),
                                "range": [row.get("value_low"), row.get("value_base"), row.get("value_high")],
                                "evidence_tier": row.get("evidence_tier"),
                            })
            except Exception:
                pass
            result = {
                "ticker": sym, "segment": "memory",
                "note": "Memory supply/demand bottleneck (DRAM/HBM/NAND) tracked; see gap model.",
                "pending_claims": claims[:4],
                "source": "bottlenecks-app memory monitor",
            }
    except Exception:
        logger.warning("bottlenecks.get_landscape failed for %s", sym, exc_info=True)
        result = None

    _cache[sym] = (time.time(), result)
    return result
