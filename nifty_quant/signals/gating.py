"""Validation-gating: which strategies are allowed to emit signals.

The gate is deliberately explicit and conservative. A strategy is *not*
approved because it was profitable in sample; it is approved only when the
validation report says it passed the declared checks. Manual overrides are not
supported: if a strategy fails validation, the engine emits ``BLOCKED``.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from nifty_quant.validation.report import ValidationReport

# The default approval rule is intentionally narrow. It requires the verdict
# itself to be "supported" and also insists on a few checks that are easy to
# fail by accident (in-sample curve-fitting, too few trades, or a restart that
# disagrees with the sliced holdout). Changing these criteria requires changing
# this dictionary and re-running validation; it cannot be overridden at signal
# generation time.
DEFAULT_APPROVAL_CRITERIA = {
    "required_verdict": "supported",
    "require_enough_trades": True,
    "require_oos_profitable": True,
    "require_strict_split_ok_if_tested": True,
}

CRITICAL_CHECKS = ("enough_trades", "oos_profitable", "breadth", "fdr_significant")


def approved_strategies(
    report: ValidationReport,
    *,
    criteria: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Map each strategy to its approval status and the reason for denial.

    Returns a dict ``strategy_id -> {"approved": bool, "verdict": str,
    "checks": dict, "failed_checks": list, "reason": str}``.
    """
    criteria = {**DEFAULT_APPROVAL_CRITERIA, **(criteria or {})}
    out: dict[str, dict[str, Any]] = {}

    if report is None or report.verdicts.empty:
        return out

    checks = _checks_by_strategy(report.checks)
    for _, row in report.verdicts.iterrows():
        sid = str(row["strategy_id"])
        verdict = str(row.get("verdict", "untested"))
        strategy_checks = checks.get(sid, {})
        failed = [c for c, v in strategy_checks.items() if v is False]

        approved = True
        reasons: list[str] = []

        if verdict != criteria["required_verdict"]:
            approved = False
            reasons.append(f"verdict is {verdict!r} (required: {criteria['required_verdict']!r})")

        if criteria["require_enough_trades"] and not strategy_checks.get("enough_trades"):
            approved = False
            reasons.append("did not pass the enough_trades check")

        if criteria["require_oos_profitable"] and not strategy_checks.get("oos_profitable"):
            approved = False
            reasons.append("did not pass the oos_profitable check")

        if criteria["require_strict_split_ok_if_tested"]:
            ss = report.strict_split
            if ss is not None and not ss.empty and sid in ss["strategy_id"].values:
                tested = ss[(ss["strategy_id"] == sid) & ss["difference"].notna()]
                if not tested.empty and not tested["ok"].all():
                    approved = False
                    reasons.append("held-out restart disagreed with sliced run")

        # Any remaining critical failure is an automatic denial, even if the
        # overall verdict somehow escaped it.
        critical_failed = [c for c in failed if c in CRITICAL_CHECKS]
        if critical_failed:
            approved = False
            reasons.append(f"critical check(s) failed: {', '.join(critical_failed)}")

        out[sid] = {
            "approved": approved,
            "verdict": verdict,
            "checks": strategy_checks,
            "failed_checks": failed,
            "reason": "; ".join(reasons) if reasons else "passed all declared criteria",
        }
    return out


def _checks_by_strategy(checks: pd.DataFrame) -> dict[str, dict[str, bool | None]]:
    """Pivot checks to {strategy_id: {check: passed}}."""
    if checks is None or checks.empty:
        return {}
    out: dict[str, dict[str, bool | None]] = {}
    for _, row in checks.iterrows():
        sid = str(row["strategy_id"])
        check = str(row["check"])
        passed = row["passed"]
        if isinstance(passed, bool):
            value: bool | None = passed
        elif passed is None or (isinstance(passed, float) and pd.isna(passed)):
            value = None
        else:
            value = bool(passed)
        out.setdefault(sid, {})[check] = value
    return out
