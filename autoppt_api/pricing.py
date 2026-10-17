"""Estimates from the active ``pricing_rules`` row.

Estimate = base (intake + planning) + slides x tier price + add-ons, split by
phase so each phase settles its own share.  The hold uses the upper end of the
slide range.  Until P1 measurements exist the default table is provisional and
must not be published as a price.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoppt_api.models import PricingRule

DEFAULT_RULES: dict[str, Any] = {
    "provisional": True,
    "base": 20,
    "per_slide": {"planning_package": 0, "image_deck": 6, "native_editable": 14},
    "vectorize_per_slide": 12,
    "addons": {"speaker_notes": 10, "anticipated_questions": 10},
    "redo_slide": 4,
    "extra_round": 5,
    "free_rounds": 2,
    # Credit packages (price in CNY cents) and the value of one credit for margin reports.
    "packages": [
        {"id": "p100", "credits": 100, "price_cents": 3000, "label": "100 积分"},
        {"id": "p300", "credits": 300, "price_cents": 8500, "label": "300 积分"},
        {"id": "p1000", "credits": 1000, "price_cents": 26000, "label": "1000 积分"},
    ],
    "credit_value_cents": 28,
    # Cost side of the margin report (CNY cents).  Codex account-pool tokens are a fixed cost: 0 per token.
    "cost_rates": {
        "input_per_million_cents": 0,
        "cached_input_per_million_cents": 0,
        "output_per_million_cents": 0,
        "office_minute_cents": 5,
        "storage_gb_month_cents": 15,
    },
}


class EstimateError(ValueError):
    pass


def active_rules(session: Session) -> tuple[int, dict[str, Any]]:
    rule = session.scalars(select(PricingRule).where(PricingRule.active.is_(True)).order_by(PricingRule.version.desc())).first()
    if rule is None:
        rule = PricingRule(version=1, active=True, rules=DEFAULT_RULES, note="provisional defaults before P1 measurements")
        session.add(rule)
        session.flush()
    return rule.version, rule.rules


def estimate(normalized: dict[str, Any], derived: dict[str, Any], rules: dict[str, Any], *, version: int) -> dict[str, Any]:
    tier = normalized["delivery"]["tier"]
    slides = derived.get("resolved_slides")
    if normalized["service"] == "vectorize":
        if not (normalized.get("slides") or {}).get("count"):
            raise EstimateError("矢量化需要填写要转换的页数（slides.mode=fixed, count）")
        count = normalized["slides"]["count"]
        phases = {"intake": 0, "planning": 0, "masters": 0, "reconstruct": rules["vectorize_per_slide"] * count, "package": 0}
        phases["intake"] = rules["base"]
    else:
        if not slides:
            raise EstimateError("缺少页数")
        count = slides["max"]
        image = rules["per_slide"]["image_deck"] * count if tier != "planning_package" else 0
        native = (rules["per_slide"]["native_editable"] - rules["per_slide"]["image_deck"]) * count if tier == "native_editable" else 0
        phases = {"intake": 0, "planning": rules["base"], "masters": image, "reconstruct": native, "package": 0}
    addons = {name: rules["addons"][name] for name in derived.get("addons", []) if name in rules["addons"]}
    phases["package"] += sum(addons.values())
    total = sum(phases.values())
    return {
        "pricing_version": version,
        "provisional": bool(rules.get("provisional")),
        "slides_for_hold": count,
        "phases": phases,
        "addons": addons,
        "total": total,
        "hold": total,
        "redo_slide": rules["redo_slide"],
        "extra_round": rules["extra_round"],
        "free_rounds": rules["free_rounds"],
    }
