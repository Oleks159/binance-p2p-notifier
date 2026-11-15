"""On-demand, bounded diagnostics for public Binance P2P search parameters."""
from market import parse_offer, ServiceError, rank

def diagnose(market, rules, target_id=None):
    if market.methods is None:
        market.discover_methods()
    tests = [
        ("baseline", {}),
        ("page2", {"page": 2}),
        ("order_asc", {"order": "asc"}),
        ("order_desc", {"order": "desc"}),
        ("amount_1000", {"transAmount": "1000"}),
    ]
    lines = ["Binance P2P search diagnostics (on demand)",
             "USDT/EUR | SEPA Instant / Wise | EUR 500–10,000 range as configured",
             "Counts and prices reflect API results, not confirmed tradability.", ""]
    for side in ("BUY", "SELL"):
        lines.append(side + ":")
        for method_id, method_name in market.methods.items():
            baseline_ids = None
            for label, extra in tests:
                params = dict(fiat="EUR", asset="USDT", tradeType=side,
                              limit=20, tradeMethodIdentifiers=method_id)
                params.update(extra)
                try:
                    data = market.get("ad-list", params)
                    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                        raise ServiceError("Unexpected response shape")
                    items = data["items"]
                    offers = [parse_offer(item, side, market.methods) for item in items]
                    ids = {offer.id for offer in offers}
                    eligible = [o for o in offers if method_name in o.methods and o.usable(rules)]
                    top = rank(eligible, rules)
                    best = str(top[0].price) if top else "none"
                    changed = "baseline" if baseline_ids is None else (
                        "same IDs" if ids == baseline_ids else f"{len(ids-baseline_ids)} new IDs")
                    if baseline_ids is None:
                        baseline_ids = ids
                    target = (" | target FOUND" if target_id in ids else " | target absent") if target_id else ""
                    lines.append(f"{method_name} {label}: {len(ids)} ads, {len(eligible)} eligible, "
                                 f"best {best}, {changed}{target}")
                except (ServiceError, ValueError, TypeError) as error:
                    lines.append(f"{method_name} {label}: error ({str(error)[:70]})")
        lines.append("")
    lines.append("Tests use candidate parameters, not guaranteed supported filters. "
                 "A changed result does not prove the parameter caused it; market listings can change.")
    return "\n".join(lines)
