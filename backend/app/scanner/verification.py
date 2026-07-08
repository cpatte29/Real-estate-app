"""
VERIFY phase — objective gate that every property must pass before
being accepted as a qualified lead.

Rejection rules (all must pass):
  1. Address completeness     — street_address must not be empty
  2. Minimum comp count       — >= 3 comparable sales found
  3. ARV confidence floor     — confidence_score >= 70.0
  4. Minimum profit threshold — estimated_profit >= $20,000
  5. Repair estimate present  — rehab estimate must exist and cost > 0
  6. Duplicate detection      — not already accepted this run or previously
  7. MAO rule                 — list_price must be <= MAO (deal must work)
"""
from __future__ import annotations

from app.scanner.config import config
from app.scanner.models import (
    ARVResult,
    CandidateProperty,
    DealAnalysis,
    RehabEstimate,
    RejectionReason,
    VerificationResult,
)


class PropertyVerifier:
    def __init__(self, checked_ids: set[str]):
        """
        checked_ids: set of address fingerprints already seen this run
                     (and loaded from persistent state for prior runs).
        """
        self._checked_ids = checked_ids

    def verify(
        self,
        prop: CandidateProperty,
        arv_result: ARVResult | None,
        rehab: RehabEstimate | None,
        deal: DealAnalysis | None,
    ) -> VerificationResult:
        """
        Run all verification rules in priority order.
        Returns on first failure — only one rejection reason per property.
        """

        # ── Rule 1: Address completeness ────────────────────────────
        if not prop.street_address or not prop.street_address.strip():
            return VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.MISSING_ADDRESS,
                rejection_detail="street_address is empty",
            )

        if not prop.city or not prop.state or not prop.zip:
            return VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.MISSING_ADDRESS,
                rejection_detail="city, state, or zip is missing",
            )

        # ── Rule 2: Duplicate detection ─────────────────────────────
        fp = prop.address_fingerprint or prop.street_address.lower()
        if fp in self._checked_ids:
            return VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.DUPLICATE,
                rejection_detail=f"Property fingerprint {fp[:12]}… already processed",
            )

        # ── Rule 3: Minimum comp count ───────────────────────────────
        if arv_result is None or arv_result.comp_count < config.min_comp_count:
            count = arv_result.comp_count if arv_result else 0
            return VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.INSUFFICIENT_COMPS,
                rejection_detail=(
                    f"Only {count} comp(s) found; minimum is {config.min_comp_count}"
                ),
            )

        # ── Rule 4: ARV confidence floor ─────────────────────────────
        if arv_result.confidence_score < config.min_arv_confidence_score:
            return VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.LOW_ARV_CONFIDENCE,
                rejection_detail=(
                    f"ARV confidence score {arv_result.confidence_score:.1f} "
                    f"< threshold {config.min_arv_confidence_score}"
                ),
            )

        # ── Rule 5: Repair estimate present ──────────────────────────
        if rehab is None or rehab.total_cost <= 0:
            return VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.MISSING_REPAIR_ESTIMATE,
                rejection_detail="Rehab estimate is missing or zero",
            )

        # ── Rule 6: Minimum profit threshold ─────────────────────────
        if deal is None or deal.estimated_profit < config.min_estimated_profit:
            profit = deal.estimated_profit if deal else 0
            return VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.INSUFFICIENT_PROFIT,
                rejection_detail=(
                    f"Estimated profit ${profit:,.0f} "
                    f"< minimum ${config.min_estimated_profit:,.0f}"
                ),
            )

        # ── Rule 7: MAO rule — deal must pencil at list price ─────────
        if deal.list_price and deal.list_price > deal.max_allowable_offer * 1.10:
            return VerificationResult(
                passed=False,
                rejection_reason=RejectionReason.FAILED_MAO_RULE,
                rejection_detail=(
                    f"List price ${deal.list_price:,.0f} exceeds "
                    f"MAO ${deal.max_allowable_offer:,.0f} by more than 10%"
                ),
            )

        # ── All rules passed ─────────────────────────────────────────
        self._checked_ids.add(fp)
        return VerificationResult(passed=True)
