"""DeepResearch findings engine.

Architecture adapted from QuantMind (github.com/LLMQuant/quant-mind): a two-stage
Knowledge Extraction -> Intelligent Retrieval pipeline, here specialized to the
dashboard's aggregated structured signals and run with Claude instead of the
OpenAI-Agents backend.

Rather than a single LLM pass (which over-trusts the loudest data point), we do
multi-hop reasoning for accuracy:

  Pass 1 — EXTRACT: read the full intelligence packet, surface candidate ideas
           with the specific evidence behind each.
  Pass 2 — VERIFY & REFINE: adversarially check each candidate — is the evidence
           real and corroborated across sources? is it genuinely non-consensus?
           drop weak ideas, sharpen the survivors into a concise ranked top-N.

The verify pass is what raises precision over the old one-shot conviction call.
"""
from __future__ import annotations

import json
import logging

from app.config import config
from app.llm import claude

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = (
    "You are a hedge-fund analyst. You are given an intelligence packet aggregating "
    "many free data sources: classified news signals (with a 0-12 non-consensus alpha "
    "score), insider (Form 4) trades, congressional trades, institutional 13D/13G "
    "stakes, share-dilution filings, unusual-volume tickers, clinical-trial catalysts, "
    "trending themes, prediction-market conflicts, and the macro regime.\n\n"
    "Surface up to 8 CANDIDATE trade ideas the data points to. For each, list the "
    "SPECIFIC evidence items from the packet that support it (quote the numbers). "
    "Favor ideas where MULTIPLE independent sources corroborate. Return ONLY JSON: "
    '{"candidates":[{"title","direction":"long|short|watch","tickers":[],'
    '"evidence":[],"sources_count":<int distinct data sources backing it>}]}'
)

_VERIFY_SYSTEM = (
    "You are the skeptical head of research reviewing an analyst's candidate ideas "
    "against the same intelligence packet. Your job is PRECISION. For each candidate:\n"
    "- Verify every evidence item actually appears in the packet (drop fabricated ones).\n"
    "- Demand corroboration: keep ideas backed by >=2 independent data sources OR one "
    "very strong primary signal (insider cluster, activist 13D, dilution filing).\n"
    "- Confirm it is genuinely NON-CONSENSUS (diverges from analyst/crowd/consensus).\n"
    "- Kill anything thin, obvious, or already priced in.\n\n"
    "Return ONLY the surviving 1-5 ideas, ranked best-first, as JSON: {\"findings\":[{\n"
    '  "rank":<int>, "title":<imperative>, "direction":"long|short|watch",\n'
    '  "conviction":"high|medium|speculative", "tickers":[],\n'
    '  "thesis":<2-3 sentences>, "evidence":[<the verified data points>],\n'
    '  "non_consensus":<why the crowd is offside>, "risk":<the main disconfirmer>,\n'
    '  "corroboration":<which independent sources agree>}]}\n'
    "Be concise and specific. Fewer, higher-quality findings beat more."
)


def deep_research_findings(packet: dict) -> list[dict]:
    """Two-pass extract->verify analysis of the intelligence packet. Returns findings."""
    if not claude.is_configured():
        return []
    packet_json = json.dumps(packet, indent=2, default=str)

    # Pass 1 — extract candidates
    raw1 = claude.complete(
        system=_EXTRACT_SYSTEM, user=packet_json,
        model=config.SONNET_MODEL, max_tokens=2500, cache_system=False,
    )
    candidates = claude.extract_json(raw1).get("candidates", [])
    if not candidates:
        return []

    # Pass 2 — verify & refine against the packet + the candidates
    verify_input = (
        f"INTELLIGENCE PACKET:\n{packet_json}\n\n"
        f"ANALYST CANDIDATES:\n{json.dumps(candidates, indent=2)}"
    )
    raw2 = claude.complete(
        system=_VERIFY_SYSTEM, user=verify_input,
        model=config.SONNET_MODEL, max_tokens=3000, cache_system=False,
    )
    findings = claude.extract_json(raw2).get("findings", [])
    return findings
