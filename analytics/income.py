"""Actual owner-recorded receipts are kept separate from YouTube revenue estimates."""
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from analytics import store

STREAMS = ('ads', 'affiliate', 'campaigns', 'services')


def record(conn, channel, stream, amount, received_on, reference, note=''):
    if stream not in STREAMS or not reference.strip() or len(reference) > 300:
        raise ValueError('Use a supported stream and a unique receipt reference')
    try:
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('.01')):
            raise ValueError('Amount must be nonnegative USD with at most two decimal places')
    except (InvalidOperation, ValueError):
        raise ValueError('Amount must be nonnegative USD with at most two decimal places') from None
    received_on = date.fromisoformat(str(received_on))
    if received_on > date.today():
        raise ValueError('A received payment cannot be in the future')
    store.schema(conn)
    # Match Phase 4 payment references as well, so a receipt is never counted twice.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_events'").fetchone():
        if conn.execute("SELECT 1 FROM campaign_events WHERE kind='payment' AND reference=?", (reference,)).fetchone():
            raise ValueError('This receipt is already recorded in Phase 4 campaigns')
    if conn.execute('SELECT 1 FROM growth_income WHERE reference=?', (reference,)).fetchone():
        raise ValueError('Receipt reference already recorded')
    with conn:
        conn.execute('INSERT INTO growth_income VALUES(?,?,?,?,?,?)',
                     (channel, reference, stream, received_on.isoformat(), int(amount * 100), note))


def summary(conn, channel, month=None):
    month = month or date.today().strftime('%Y-%m')
    date.fromisoformat(month + '-01')
    totals = {s: 0 for s in STREAMS}
    entries = [dict(r) for r in conn.execute('SELECT * FROM growth_income WHERE channel_id=? AND received_on LIKE ?',
                                             (channel, month + '-%'))]
    references = {r['reference'] for r in conn.execute('SELECT reference FROM growth_income')}
    for entry in entries:
        totals[entry['stream']] += entry['cents']
        references.add(entry['reference'])
    # Campaign payments belong to the project's clipping business, not inferred channel ad revenue.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_events'").fetchone():
        for row in conn.execute("SELECT * FROM campaign_events WHERE kind='payment' AND created_at LIKE ?", (month + '-%',)):
            if row['reference'] in references:
                continue
            payload = json.loads(row['payload'])
            cents = int(Decimal(payload['amount_usd']) * 100)
            totals['campaigns'] += cents
            references.add(row['reference'])
            entries.append({'stream': 'campaigns', 'cents': cents, 'reference': row['reference'],
                            'received_on': row['created_at'][:10], 'note': 'Phase 4 project campaign receipt, owner-reported'})
    return {'month': month, 'received_usd': sum(totals.values()) / 100,
            'streams_usd': {k: v / 100 for k, v in totals.items()}, 'entries': entries,
            'basis': 'Logged receipts in USD, owner-reported; no records does not establish zero actual income'}
