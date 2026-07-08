"""
Main scanner pipeline — orchestrates all phases for a single daily run.

DISCOVER → PLAN → EXECUTE → VERIFY → ITERATE until STOP CONDITION.
"""
from __future__ import annotations

import logging
import random
from datetime import date, datetime
from uuid import uuid4

from app.scanner.arv import ARVEngine
from app.scanner.config import ScannerConfig, config as default_config
from app.scanner.discovery import PropertyDiscovery, SimulatedPropertySource
from app.scanner.models import (
    CandidateProperty,
    PropertyPipelineResult,
    RejectionReason,
    ScannerRunState,
)
from app.scanner.rehab import RehabEstimator
from app.scanner.scoring import DealCalculator
from app.scanner.verification import PropertyVerifier

logger = logging.getLogger(__name__)


class LeadScannerPipeline:
    """
    Daily distressed property lead scanner.

    Usage:
        pipeline = LeadScannerPipeline()
        state = pipeline.run()
        report = pipeline.get_report(state)
    """

    def __init__(self, cfg: ScannerConfig = None):
        self.cfg = cfg or default_config
        self._rng = random.Random(self.cfg.simulation_seed)

        # Comp source — use real CSV when path is configured, else simulate
        comp_source = None
        if self.cfg.comp_csv_path:
            from app.scanner.comps.csv_source import CsvCompSource
            comp_source = CsvCompSource(self.cfg.comp_csv_path)

        # Sub-engines
        source = SimulatedPropertySource(seed=self.cfg.simulation_seed)
        self.discovery = PropertyDiscovery(source)
        self.arv_engine = ARVEngine(comp_source=comp_source, rng=self._rng)
        self.rehab_estimator = RehabEstimator()
        self.deal_calculator = DealCalculator()

    # ──────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────────────────

    def run(self, run_date: date = None) -> ScannerRunState:
        run_date = run_date or date.today()
        run_id = uuid4()

        state = ScannerRunState(
            run_id=run_id,
            run_date=run_date,
        )

        logger.info(
            "Scanner run started | run_id=%s date=%s | "
            "stop: %d leads OR %d candidates OR %d iterations",
            run_id, run_date,
            self.cfg.max_qualified_leads,
            self.cfg.max_candidates_checked,
            self.cfg.max_iterations,
        )

        verifier = PropertyVerifier(checked_ids=state.checked_property_ids)

        while True:
            state.iteration += 1
            logger.info("── Iteration %d ─────────────────────────────", state.iteration)

            # DISCOVER
            batch = self._discover(state)
            if not batch:
                logger.info("Discovery returned no candidates — stopping.")
                state.stop_reason = "no_more_candidates"
                break

            state.candidates_found += len(batch)

            # EXECUTE — process each candidate through the full pipeline
            for prop in batch:
                result = self._evaluate(prop, state.run_id, verifier)
                state.candidates_checked += 1

                if result.verification and result.verification.passed:
                    state.leads_accepted += 1
                    state.accepted_leads.append(result)
                    logger.info(
                        "  ACCEPTED | %s | score=%.1f | profit=$%.0f",
                        prop.street_address,
                        result.deal_analysis.opportunity_score,
                        result.deal_analysis.estimated_profit,
                    )
                else:
                    reason = result.verification.rejection_reason if result.verification else RejectionReason.BAD_DATA
                    state.record_rejection(reason)
                    state.rejected_leads.append((result, reason))
                    logger.debug(
                        "  ✗ REJECTED | %s | reason=%s | %s",
                        prop.street_address or "<no address>",
                        reason.value,
                        result.verification.rejection_detail if result.verification else "",
                    )

                # CHECK STOP CONDITION after each property
                should_stop, stop_reason = state.should_stop(self.cfg)
                if should_stop:
                    state.stop_reason = stop_reason
                    logger.info("Stop condition hit: %s", stop_reason)
                    break

            else:
                # Batch exhausted; check iteration stop before fetching next
                should_stop, stop_reason = state.should_stop(self.cfg)
                if should_stop:
                    state.stop_reason = stop_reason
                    logger.info("Stop condition hit after batch: %s", stop_reason)
                    break
                continue  # continue to next iteration

            break  # inner break propagated

        state.stop_reason = state.stop_reason or "completed_normally"
        logger.info(
            "Scanner run complete | accepted=%d rejected=%d checked=%d iterations=%d | stop=%s",
            state.leads_accepted,
            state.leads_rejected,
            state.candidates_checked,
            state.iteration,
            state.stop_reason,
        )
        return state

    # ──────────────────────────────────────────────────────────────────────────
    # DISCOVER phase
    # ──────────────────────────────────────────────────────────────────────────

    def _discover(self, state: ScannerRunState) -> list[CandidateProperty]:
        offset = state.candidates_found
        remaining_budget = self.cfg.max_candidates_checked - state.candidates_checked
        batch_size = min(self.cfg.candidate_batch_size, remaining_budget)

        if batch_size <= 0:
            return []

        candidates = self.discovery.discover(batch_size=batch_size, offset=offset)
        logger.info("Discovered %d candidates (offset=%d)", len(candidates), offset)
        return candidates

    # ──────────────────────────────────────────────────────────────────────────
    # PLAN → EXECUTE — full evaluation pipeline for one property
    # ──────────────────────────────────────────────────────────────────────────

    def _evaluate(
        self,
        prop: CandidateProperty,
        run_id,
        verifier: PropertyVerifier,
    ) -> PropertyPipelineResult:
        result = PropertyPipelineResult(property=prop)

        # Quick structural validation before expensive steps
        if not prop.street_address or not prop.street_address.strip():
            from app.scanner.models import VerificationResult
            result.verification = VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.MISSING_ADDRESS,
                rejection_detail="street_address is empty",
            )
            return result

        # Duplicate check (fast, no computation)
        fp = prop.address_fingerprint or prop.street_address.lower()
        if fp in verifier._checked_ids:
            from app.scanner.models import VerificationResult
            result.verification = VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.DUPLICATE,
                rejection_detail=f"Already processed: {fp[:12]}…",
            )
            return result

        # ── ARV calculation ──────────────────────────────────────────
        arv_result = self.arv_engine.calculate(prop, run_id)
        result.arv_result = arv_result

        # ── Rehab estimation ─────────────────────────────────────────
        rehab = self.rehab_estimator.estimate(prop, run_id)
        result.rehab_estimate = rehab

        # ── Deal analysis + scoring ──────────────────────────────────
        deal = None
        if arv_result and arv_result.arv > 0 and rehab:
            deal = self.deal_calculator.analyze(prop, arv_result, rehab, run_id)
        result.deal_analysis = deal

        # ── Verification ─────────────────────────────────────────────
        verification = verifier.verify(prop, arv_result, rehab, deal)
        result.verification = verification
        result.accepted = verification.passed

        return result
