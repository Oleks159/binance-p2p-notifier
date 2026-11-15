"""Track observed P2P advertisement visibility and annotate historical reports.

A disappearance from API results is not proof the ad was removed from Binance.
The public API may omit ads due to ranking, filtering, or visibility.
"""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")

def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS visibility_sessions (
        id INTEGER PRIMARY KEY, side TEXT NOT NULL, ad_id TEXT NOT NULL,
        first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
        missing_at INTEGER, last_snapshot TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS visibility_by_ad ON visibility_sessions(side,ad_id,last_seen)")
    db.commit()

def observe(db, snapshot, now):
    """Call only after a complete successful market scan; one session per continuous presence."""
    for side in ("BUY", "SELL"):
        seen = set()
        for offer in snapshot[side]:
            seen.add(offer.id)
            previous = db.execute(
                "SELECT id FROM visibility_sessions WHERE side=? AND ad_id=? AND missing_at IS NULL",
                (side, offer.id)).fetchone()
            if previous:
                db.execute("UPDATE visibility_sessions SET last_seen=?,last_snapshot=? WHERE id=?",
                           (now, offer.serialized(), previous[0]))
            else:
                db.execute("""INSERT INTO visibility_sessions
                    (side,ad_id,first_seen,last_seen,missing_at,last_snapshot)
                    VALUES (?,?,?,?,NULL,?)""",
                    (side, offer.id, now, now, offer.serialized()))
        # Only mark absent from THIS scan's returned listings, not definitively deleted.
        for session_id, ad_id in db.execute(
            "SELECT id,ad_id FROM visibility_sessions WHERE side=? AND missing_at IS NULL",
            (side,)).fetchall():
            if ad_id not in seen:
                db.execute("UPDATE visibility_sessions SET missing_at=? WHERE id=?",
                           (now, session_id))

def duration(ms):
    seconds = max(0, int(ms // 1000))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    return f"{seconds//3600}h {(seconds%3600)//60}m"

def extra_lines(db, side, ad_id, at_ms, available=None, price=None):
    """Return honest observation-based metadata for a historical best-price snapshot."""
    lines = []
    if available is not None:
        amount = Decimal(str(available))
        lines.append(f"   Available at recorded price: {amount:,.2f} USDT"
                     + (f" (approx EUR {amount * Decimal(str(price)):,.2f})" if price else ""))
    # New tracking is session-based; old records only contain global first/last seen.
    session = db.execute("""SELECT first_seen,last_seen,missing_at FROM visibility_sessions
        WHERE side=? AND ad_id=? AND first_seen<=? AND last_seen>=?
        ORDER BY first_seen DESC LIMIT 1""", (side, ad_id, at_ms, at_ms)).fetchone()
    if session:
        first, last, missing = session
        first_t = datetime.fromtimestamp(first/1000, TZ)
        last_t = datetime.fromtimestamp(last/1000, TZ)
        lines.append(f"   Observed in API: {first_t:%d.%m %H:%M:%S} – {last_t:%d.%m %H:%M:%S} ({duration(last-first)})")
        lines.append("   Status: no longer returned in scans" if missing is not None
                     else "   Status: present in latest tracked scan")
    else:
        old = db.execute("SELECT first_seen,last_seen FROM offers WHERE side=? AND id=?",
                         (side, ad_id)).fetchone()
        if old and old[0] <= at_ms <= old[1]:
            first, last = old
            first_t = datetime.fromtimestamp(first/1000, TZ)
            last_t = datetime.fromtimestamp(last/1000, TZ)
            lines.append(f"   Legacy first/last seen: {first_t:%d.%m %H:%M} – {last_t:%d.%m %H:%M}")
            lines.append(f"   Legacy observation span: {duration(last-first)} (not continuous availability)")
        else:
            lines.append("   Duration: unknown (insufficient historical observations)")
    return lines
