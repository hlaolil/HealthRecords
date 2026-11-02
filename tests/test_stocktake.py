from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'

import mongomock
import pymongo
from datetime import datetime, timedelta, timezone
import csv as _csv
import io as _io
import re as _re

_fake = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _fake

import app as A
# Bound by VALUE on purpose. As a closure over the name, any later
# assignment to that name anywhere in this file would silently replace
# the database for the rest of the run.
A.get_mongo_client = lambda _client=_fake: _client
A.init_db_collections()

db = _fake['pharmacy_db']
meds = db['medications']
txs = db['transactions']

client = A.app.test_client()



# The app refuses a POST without its CSRF token; this makes the

# test client send it, so these checks exercise the real path.

from test_support import arm_csrf

arm_csrf(client)
def login_as_admin():
    with client.session_transaction() as s:
        s['user'] = {'name': 'Admin User', 'login': 'admin', 'role': 'admin'}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for c in ['medications', 'transactions', 'stock_takes',
              'stock_take_counts', 'audit_log']:
        db[c].delete_many({})


def seed(name, opening, received, dispensed, day_base):
    """Create a med whose balance is the result of the movements below."""
    meds.insert_one({'name': name, 'balance': opening + received - dispensed,
                     'batch': 'B1', 'price': 1.0,
                     'expiry_date': '2030-01-01', 'schedule': 'not controlled'})
    if received:
        txs.insert_one({'type': 'receive', 'med_name': name, 'quantity': received,
                        'user': 'u', 'timestamp': day_base + timedelta(days=2)})
    if dispensed:
        txs.insert_one({'type': 'dispense', 'med_name': name, 'quantity': dispensed,
                        'user': 'u', 'timestamp': day_base + timedelta(days=3)})


PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label + (('  ->  ' + detail) if detail and not cond else ''))


# =====================================================================
print('\n--- 1. Access control ---')
reset()
with client.session_transaction() as s:
    s['user'] = {'name': 'Pharm', 'login': 'p', 'role': 'user'}
    s['last_activity'] = datetime.now(timezone.utc).isoformat()
r = client.get('/stock-take', follow_redirects=False)
check('non-admin is redirected away from /stock-take', r.status_code == 302)

login_as_admin()
r = client.get('/stock-take')
check('admin can open /stock-take', r.status_code == 200)
check('nav shows Stock Take link for admin', b'/stock-take' in r.data)

# =====================================================================
print('\n--- 2. Opening a stock take ---')
r = client.post('/stock-take', data={'action': 'open', 'note': 'August count'},
                follow_redirects=True)
st = db['stock_takes'].find_one({})
check('session created and open', st is not None and st['status'] == 'open')
check('reference generated', st and st['reference'].startswith('ST-'))
sid = str(st['_id'])

r = client.post('/stock-take', data={'action': 'open', 'note': 'second'},
                follow_redirects=True)
check('cannot open a second concurrent stock take',
      db['stock_takes'].count_documents({}) == 1)

# =====================================================================
print('\n--- 3. Blind counting ---')
meds.insert_one({'name': 'Paracetamol, 500 mg', 'balance': 100, 'batch': 'B1',
                 'price': 1.0, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled'})
r = client.get(f'/stock-take?stock_take_id={sid}')
body = r.data.decode()
check('counting sheet lists the medication name', 'Paracetamol, 500 mg' in body)
# The sheet used to be blind. It shows the system figure now, at the
# pharmacist's request, so a stock take short of time can act only where the
# shelf disagrees. These say what is true now, rather than passing by luck:
# the old check looked for "<td>100</td>" and the figure sits in a cell with
# a class, so it would have kept passing with the balance on the screen.
check('the sheet shows what the system has',
      '<td class="st-system">100</td>' in body)
check('but never pre-fills the count, so a count is still typed',
      'name="counted__0" type="number" min="0"' in body
      and not _re.search(r'name="counted__0"[^>]*value=', body))

# =====================================================================
print('\n--- 4. Count with shortfall: issued, not recorded ---')
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Paracetamol, 500 mg',
                                 'counted': '93', 'note': 'shelf A'},
            follow_redirects=True)
m = meds.find_one({'name': 'Paracetamol, 500 mg'})
check('balance corrected immediately to counted figure', m['balance'] == 93,
      f"balance={m['balance']}")
c = db['stock_take_counts'].find_one({'med_name': 'Paracetamol, 500 mg'})
check('variance recorded as -7', c['variance'] == -7, str(c['variance']))
check('classified as issued_not_recorded',
      c['discrepancy_type'] == 'issued_not_recorded', c['discrepancy_type'])
adj = txs.find_one({'type': 'adjustment', 'med_name': 'Paracetamol, 500 mg'})
check('signed adjustment posted to the ledger', adj is not None and adj['quantity'] == -7,
      str(adj))
check('audit_log entry written',
      db['audit_log'].count_documents({'target_type': 'stock_take_count'}) == 1)

# =====================================================================
print('\n--- 5. Count with surplus: recorded, not issued ---')
meds.insert_one({'name': 'Amoxycillin Cap, 250 mg', 'balance': 40, 'batch': 'B2',
                 'price': 2.0, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled'})
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Amoxycillin Cap, 250 mg',
                                 'counted': '46'}, follow_redirects=True)
c = db['stock_take_counts'].find_one({'med_name': 'Amoxycillin Cap, 250 mg'})
check('variance +6 classified as recorded_not_issued',
      c['variance'] == 6 and c['discrepancy_type'] == 'recorded_not_issued')
check('balance now 46', meds.find_one({'name': 'Amoxycillin Cap, 250 mg'})['balance'] == 46)

# =====================================================================
print('\n--- 6. Count that agrees writes no adjustment ---')
meds.insert_one({'name': 'Aspirin, 100 mg', 'balance': 20, 'batch': 'B3',
                 'price': 0.5, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled'})
before_adj = txs.count_documents({'type': 'adjustment'})
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Aspirin, 100 mg',
                                 'counted': '20'}, follow_redirects=True)
check('no adjustment posted when the count agrees',
      txs.count_documents({'type': 'adjustment'}) == before_adj)
check('the agreeing count is still logged',
      db['stock_take_counts'].count_documents({'med_name': 'Aspirin, 100 mg'}) == 1)

# =====================================================================
print('\n--- 7. Partial counting across sittings ---')
check('only 3 of 3 counted so far, session still open',
      db['stock_take_counts'].count_documents({'stock_take_id': sid}) == 3
      and db['stock_takes'].find_one({'_id': A.ObjectId(sid)})['status'] == 'open')
meds.insert_one({'name': 'Later Item, 10 mg', 'balance': 5, 'batch': 'B4',
                 'price': 1.0, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled'})
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Later Item, 10 mg',
                                 'counted': '4'}, follow_redirects=True)
check('an item counted later joins the same session',
      db['stock_take_counts'].count_documents({'stock_take_id': sid}) == 4)

# =====================================================================
print('\n--- 8. Bad input ---')
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Does Not Exist',
                                 'counted': '5'}, follow_redirects=True)
check('unknown medication is rejected, not created',
      meds.count_documents({'name': 'Does Not Exist'}) == 0)
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Aspirin, 100 mg',
                                 'counted': 'abc'}, follow_redirects=True)
check('non-numeric count rejected',
      meds.find_one({'name': 'Aspirin, 100 mg'})['balance'] == 20)
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Aspirin, 100 mg',
                                 'counted': '-3'}, follow_redirects=True)
check('negative count rejected',
      meds.find_one({'name': 'Aspirin, 100 mg'})['balance'] == 20)

# =====================================================================
print('\n--- 9. Closing ---')
client.post('/stock-take', data={'action': 'close', 'stock_take_id': sid},
            follow_redirects=True)
st = db['stock_takes'].find_one({'_id': A.ObjectId(sid)})
check('session closed with who/when', st['status'] == 'closed'
      and st['closed_by'] == 'Admin User' and st['closed_at'] is not None)
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Aspirin, 100 mg',
                                 'counted': '11'}, follow_redirects=True)
check('no counts accepted after close',
      meds.find_one({'name': 'Aspirin, 100 mg'})['balance'] == 20)
client.post('/stock-take', data={'action': 'open', 'note': 'next'},
            follow_redirects=True)
check('a new stock take can open once the previous is closed',
      db['stock_takes'].count_documents({'status': 'open'}) == 1)

# =====================================================================
print('\n--- 10. Inventory report reconciles ---')
reset()
base = datetime(2026, 8, 1)
seed('Paracetamol, 500 mg', opening=100, received=50, dispensed=30, day_base=base)
# a stock take mid-period finds 7 missing
txs.insert_one({'type': 'adjustment', 'med_name': 'Paracetamol, 500 mg',
                'quantity': -7, 'user': 'Admin User',
                'timestamp': base + timedelta(days=5)})
meds.update_one({'name': 'Paracetamol, 500 mg'}, {'$inc': {'balance': -7}})

login_as_admin()
r = client.post('/reports', data={'report_type': 'inventory',
                                  'start_date': '2026-08-01',
                                  'end_date': '2026-08-31'})
body = r.data.decode()
check('the inventory report shows the AMC you order from',
      '<th>Adjusted AMC</th>' in body)
check('and folds count corrections into Issued rather than a column',
      '<th>Adjustment</th>' not in body)

from test_support import inventory_rows, reconciles
row = inventory_rows(body).get('Paracetamol, 500 mg', {})
print('   row:', row)
check('beginning balance is the true opening (100)',
      row.get('Beginning Balance') == '100', str(row))
check('received 50', row.get('Received') == '50', str(row))
# 30 dispensed, plus the 7 the count found missing
check('issued is the 30 dispensed and the 7 the count found missing',
      row.get('Issued') == '37', str(row))
check('current balance 113', row.get('Current Balance') == '113', str(row))
check('row reconciles: 100 + 50 - 37 = 113', row and reconciles(row), str(row))
# 30 dispensed over 31 days -> 29.03, as a whole number 29. The 7 missing at
# the count is not consumption and does not raise it.
check('Adjusted AMC is consumption scaled to 30 days, as a whole number (29)',
      row.get('Adjusted AMC') == '29', str(row))

# =====================================================================
print('\n--- 11. Without the adjustment fix the row would NOT reconcile ---')
naive_beginning = 113 - 50 + 30   # the old formula, ignoring adjustments
check('old formula would have reported beginning as 93, not 100',
      naive_beginning == 93, str(naive_beginning))

# =====================================================================
print('\n--- 12. Stock on hand as-of-date unwinds later adjustments ---')
reset()
meds.insert_one({'name': 'Aspirin, 100 mg', 'balance': 45, 'batch': 'B1',
                 'price': 1.0, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled'})
# after 2026-08-15: received 10, dispensed 5, adjustment -3  => balance 45
txs.insert_many([
    {'type': 'receive', 'med_name': 'Aspirin, 100 mg', 'quantity': 10,
     'user': 'u', 'timestamp': datetime(2026, 8, 20)},
    {'type': 'dispense', 'med_name': 'Aspirin, 100 mg', 'quantity': 5,
     'user': 'u', 'timestamp': datetime(2026, 8, 21)},
    {'type': 'adjustment', 'med_name': 'Aspirin, 100 mg', 'quantity': -3,
     'user': 'Admin User', 'timestamp': datetime(2026, 8, 22)},
])
r = client.post('/reports', data={'report_type': 'stock_on_hand',
                                  'end_date': '2026-08-15'})
body = r.data.decode()
mm = _re.search(r'<td>Aspirin, 100 mg</td>\s*<td>(\d+)</td>', body)
got = mm.group(1) if mm else 'not found'
check('balance as at 2026-08-15 is 43 (45 - 10 + 5 + 3)', got == '43', got)

# =====================================================================
print('\n--- 13. Audit view ---')
reset()
login_as_admin()
client.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({})['_id'])
meds.insert_one({'name': 'Paracetamol, 500 mg', 'balance': 100, 'batch': 'B1',
                 'price': 1.0, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled'})
meds.insert_one({'name': 'Aspirin, 100 mg', 'balance': 10, 'batch': 'B1',
                 'price': 1.0, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled'})
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Paracetamol, 500 mg',
                                 'counted': '93'}, follow_redirects=True)
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Aspirin, 100 mg',
                                 'counted': '10'}, follow_redirects=True)
r = client.get('/audit?view=stocktake')
body = r.data.decode()
check('stock take view renders', r.status_code == 200 and 'Stock Take Discrepancies' in body)
check('shows both counts', body.count('Paracetamol, 500 mg') >= 1 and 'Aspirin, 100 mg' in body)
check('shows the human-readable discrepancy type', 'Issued, not recorded' in body)
check('shows the agreeing line as Agrees', 'Agrees' in body)

r = client.get('/audit?view=stocktake&only=discrepancies')
body = r.data.decode()
check('discrepancies-only filter hides agreeing lines',
      'Paracetamol, 500 mg' in body and 'Aspirin, 100 mg' not in body)

r = client.get('/audit?view=stocktake&search=Paracetamol')
check('search filters by medication', 'Paracetamol, 500 mg' in r.data.decode())

with client.session_transaction() as s:
    s['user'] = {'name': 'Pharm', 'login': 'p', 'role': 'user'}
    s['last_activity'] = datetime.now(timezone.utc).isoformat()
r = client.get('/audit?view=stocktake', follow_redirects=False)
check('non-admin blocked from audit', r.status_code == 302)

# =====================================================================
print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)

# =====================================================================
print('\n--- 14. Count corrections fold into Issued, for everyone ---')
reset()
base = datetime(2026, 8, 1)
seed('Paracetamol, 500 mg', opening=100, received=50, dispensed=30, day_base=base)
txs.insert_one({'type': 'adjustment', 'med_name': 'Paracetamol, 500 mg',
                'quantity': -7, 'user': 'Admin User',
                'timestamp': base + timedelta(days=5)})
meds.update_one({'name': 'Paracetamol, 500 mg'}, {'$inc': {'balance': -7}})
# a second item where the count found a SURPLUS
seed('Amoxycillin Cap, 250 mg', opening=80, received=0, dispensed=20, day_base=base)
txs.insert_one({'type': 'adjustment', 'med_name': 'Amoxycillin Cap, 250 mg',
                'quantity': 6, 'user': 'Admin User',
                'timestamp': base + timedelta(days=6)})
meds.update_one({'name': 'Amoxycillin Cap, 250 mg'}, {'$inc': {'balance': 6}})

form = {'report_type': 'inventory', 'start_date': '2026-08-01', 'end_date': '2026-08-31'}


def row_cells(body, name):
    mm = _re.search(r'<td>' + _re.escape(name) + r'</td>(.*?)</tr>', body, _re.S)
    return [c.strip() for c in _re.findall(r'<td>(.*?)</td>', mm.group(1))] if mm else []


login_as_admin()
admin_body = client.post('/reports', data=form).data.decode()
with client.session_transaction() as s:
    s['user'] = {'name': 'Pharm', 'login': 'p', 'role': 'user'}
    s['last_activity'] = datetime.now(timezone.utc).isoformat()
user_body = client.post('/reports', data=form).data.decode()

check('nobody sees an Adjustment column',
      '<th>Adjustment</th>' not in admin_body and '<th>Adjustment</th>' not in user_body)
check('both see the Adjusted AMC',
      '<th>Adjusted AMC</th>' in admin_body and '<th>Adjusted AMC</th>' in user_body)
check('no signed +/- figures in the table for anyone',
      '>-7<' not in admin_body and '>+6<' not in admin_body
      and '>-7<' not in user_body and '>+6<' not in user_body)
check('the non-admin page never mentions Adjustment at all',
      'Adjustment' not in user_body)

_ai, _ui = inventory_rows(admin_body), inventory_rows(user_body)
for name, beg, rec, issued, cur in [
        # 30 dispensed + 7 found missing at the count
        ('Paracetamol, 500 mg', 100, 50, 37, 113),
        # 20 dispensed - 6 found EXTRA at the count: the surplus comes off
        # Issued, as decided, so Received still means deliveries only
        ('Amoxycillin Cap, 250 mg', 80, 0, 14, 66)]:
    a, u = _ai.get(name, {}), _ui.get(name, {})
    print(f'   {name}: admin={a}  user={u}')
    check(f'{name}: beginning balance is the TRUE opening ({beg})',
          a.get('Beginning Balance') == str(beg), str(a))
    check(f'{name}: received is deliveries only ({rec})',
          a.get('Received') == str(rec), str(a))
    check(f'{name}: Issued absorbs the count correction ({issued})',
          a.get('Issued') == str(issued), str(a))
    check(f'{name}: current balance {cur}', a.get('Current Balance') == str(cur), str(a))
    check(f'{name}: the row reconciles', a and reconciles(a), str(a))
    check(f'{name}: the same row for admin and non-admin', a == u, f'{a} vs {u}')

print('\n--- 15. Non-admin sees no other stock-take trace ---')
check('no Stock Take nav link for non-admin', '/stock-take' not in user_body)
check('no Audit nav link for non-admin', '/audit' not in user_body)
r = client.get('/stock-take', follow_redirects=False)
check('non-admin still blocked from the stock take page', r.status_code == 302)

# =====================================================================
print('\n--- 16. The count sheet, which nothing here had ever posted ---')
# Worth saying plainly: every check above this line posts action=count, the
# one-item path. The page offers the SHEET, and nothing in this file had ever
# posted one. Seventy-six checks passed while the thing the pharmacist
# actually uses went through untested.
reset()
login_as_admin()
meds.insert_many([
    {'name': 'Paracetamol tabs, 500 mg', 'balance': 100, 'batch': 'B1',
     'price': 0.18, 'expiry_date': '2030-01-01', 'schedule': 'not controlled'},
    {'name': 'Amoxicillin caps, 500 mg', 'balance': 40, 'batch': 'AMX7',
     'price': 0.95, 'expiry_date': '2027-06-30', 'schedule': 'not controlled'},
    {'name': 'Morphine tabs, 10mg', 'balance': 20, 'batch': 'M1',
     'price': 126.55, 'expiry_date': '2028-03-31', 'schedule': 'controlled'},
])
client.post('/stock-take', data={'action': 'open', 'note': 'sheet'},
            follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
body = client.get(f'/stock-take?stock_take_id={sid}').data.decode()
sheet_names = _re.findall(r'name="med_name" value="([^"]+)"', body)
check('the sheet lists every medication', len(sheet_names) == 3, str(sheet_names))
check('and the sheet has an expiry box per row',
      'name="expiry__0"' in body and 'name="expiry__2"' in body)
check('and a batch box per row',
      'name="batch__0"' in body and 'name="batch__2"' in body)
check('each row shows its system figure',
      '<td class="st-system">100</td>' in body
      and '<td class="st-system">40</td>' in body)
check('and the expiry and batch on file are already in the boxes',
      'value="2027-06-30"' in body and 'value="AMX7"' in body)


def sheet_post(names, by_name):
    """Build a count-sheet submission from the page's own row order.

    The sheet is listed alphabetically, not in the order the medications were
    created, so a test that hard-codes counted__0 is really testing whichever
    name happens to sort first. Keyed by name instead.
    """
    data = {'action': 'count_sheet', 'stock_take_id': sid, 'med_name': names}
    for i, n in enumerate(names):
        for field, value in (by_name.get(n) or {}).items():
            data[f'{field}__{i}'] = value
    return data


r = client.post('/stock-take', data=sheet_post(sheet_names, {
    'Paracetamol tabs, 500 mg': {'counted': '93', 'note': 'shelf A'},
    'Amoxicillin caps, 500 mg': {'counted': '40', 'expiry': '2027-09-30',
                                 'batch': 'AMX8'},
    'Morphine tabs, 10mg':      {'counted': ''},
}), follow_redirects=True)
check('the sheet posts', r.status_code == 200)
check('the counted balance is corrected',
      meds.find_one({'name': 'Paracetamol tabs, 500 mg'})['balance'] == 93)
check('a count that agrees still records the line',
      db['stock_take_counts'].count_documents(
          {'med_name': 'Amoxicillin caps, 500 mg'}) == 1)
check('the untouched row posts nothing',
      db['stock_take_counts'].count_documents(
          {'med_name': 'Morphine tabs, 10mg'}) == 0)
check('and it stays on the sheet to be counted',
      'Morphine tabs, 10mg' in
      client.get(f'/stock-take?stock_take_id={sid}').data.decode())

# =====================================================================
print('\n--- 17. An expiry and a batch found on the shelf ---')
_amox = meds.find_one({'name': 'Amoxicillin caps, 500 mg'})
check('the expiry on file is corrected to what was found',
      _amox['expiry_date'] == '2027-09-30', str(_amox['expiry_date']))
check('and so is the batch', _amox['batch'] == 'AMX8', str(_amox['batch']))
_line = db['stock_take_counts'].find_one({'med_name': 'Amoxicillin caps, 500 mg'})
check('the line keeps the expiry it replaced',
      _line.get('expiry_before') == '2027-06-30', str(_line.get('expiry_before')))
check('and the batch it replaced',
      _line.get('batch_before') == 'AMX7', str(_line.get('batch_before')))
check('so the register can show the change, not just the new value',
      _line.get('expiry_after') == '2027-09-30'
      and _line.get('batch_after') == 'AMX8')
_audit = db['audit_log'].find_one(
    {'target_type': 'stock_take_count', 'target_id': {'$regex': 'Amoxicillin'}})
check('the audit entry names both old values',
      _audit and '2027-06-30 -> 2027-09-30' in str(_audit.get('changes'))
      and 'AMX7 -> AMX8' in str(_audit.get('changes')),
      str(_audit and _audit.get('changes')))
_body = client.get(f'/stock-take?stock_take_id={sid}').data.decode()
check('and the page shows the old value struck through',
      '<del>2027-06-30</del>' in _body and '<del>AMX7</del>' in _body)
check('a count that agreed posts no adjustment',
      txs.count_documents({'type': 'adjustment',
                           'med_name': 'Amoxicillin caps, 500 mg'}) == 0,
      'nothing moved, so nothing belongs in the ledger')

# =====================================================================
print('\n--- 18. An expiry fixed without recounting ---')
# The case the pharmacist asked for: you notice the expiry is wrong but you
# are not recounting the shelf. The balance must not move, no adjustment may
# be posted, and the item must stay on the sheet - otherwise correcting a
# date would quietly excuse it from the stock take.
_before = meds.find_one({'name': 'Morphine tabs, 10mg'})['balance']
r = client.post('/stock-take', data=sheet_post(['Morphine tabs, 10mg'], {
    'Morphine tabs, 10mg': {'counted': '', 'expiry': '2029-01-31',
                            'batch': 'M2'},
}), follow_redirects=True)
_m = meds.find_one({'name': 'Morphine tabs, 10mg'})
check('the balance is exactly as it was', _m['balance'] == _before,
      f"{_before} -> {_m['balance']}")
check('the expiry is corrected', _m['expiry_date'] == '2029-01-31')
check('and the batch', _m['batch'] == 'M2')
check('no adjustment is posted for a line that moved nothing',
      txs.count_documents({'type': 'adjustment',
                           'med_name': 'Morphine tabs, 10mg'}) == 0)
_line = db['stock_take_counts'].find_one({'med_name': 'Morphine tabs, 10mg'})
check('the line records no count', _line.get('counted') is None,
      str(_line.get('counted')))
check('and no variance to be right or wrong about',
      _line.get('variance') is None)
check('the page says so rather than showing a figure',
      'Detail only' in client.get(f'/stock-take?stock_take_id={sid}').data.decode())
check('it is NOT counted as an item counted',
      'had an expiry or batch but no count' in r.data.decode(),
      r.data.decode()[:300])
check('and it is still on the sheet, waiting to be counted',
      'Morphine tabs, 10mg' in
      client.get(f'/stock-take?stock_take_id={sid}').data.decode(),
      'correcting a date must not excuse an item from the count')

# =====================================================================
print('\n--- 19. A date the app cannot read ---')
_was = meds.find_one({'name': 'Paracetamol tabs, 500 mg'})['expiry_date']
r = client.post('/stock-take', data=sheet_post(['Paracetamol tabs, 500 mg'], {
    'Paracetamol tabs, 500 mg': {'counted': '90', 'expiry': 'next March'},
}), follow_redirects=True)
_p = meds.find_one({'name': 'Paracetamol tabs, 500 mg'})
check('the nonsense date is not written', _p['expiry_date'] == _was,
      str(_p['expiry_date']))
check('but the count on the same row still posts', _p['balance'] == 90,
      'losing a counted quantity over a mistyped date is the worse trade')
check('and the page says the date was not saved',
      'not in YYYY-MM-DD form' in r.data.decode(), r.data.decode()[:300])

# =====================================================================
print('\n--- 20. One item on its own, through the same code ---')
# This path used to be ninety lines duplicated from _record_count, with its
# own copy of the balance correction, the ledger entry and the audit line.
reset()
login_as_admin()
meds.insert_one({'name': 'Ibuprofen tabs, 400 mg', 'balance': 50, 'batch': 'IB1',
                 'price': 0.3, 'expiry_date': '2027-01-31',
                 'schedule': 'not controlled'})
client.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
check('the one-item form is on the page',
      'value="count"' in client.get(f'/stock-take?stock_take_id={sid}').data.decode(),
      'the page had lost it entirely; the route behind it was unreachable')
r = client.post('/stock-take', data={
    'action': 'count', 'stock_take_id': sid,
    'med_name': 'Ibuprofen tabs, 400 mg', 'counted': '47',
    'expiry_date': '2027-04-30', 'batch': 'IB2', 'note': 'shelf C'},
    follow_redirects=True)
_m = meds.find_one({'name': 'Ibuprofen tabs, 400 mg'})
check('the balance is corrected', _m['balance'] == 47)
check('the expiry is corrected', _m['expiry_date'] == '2027-04-30')
check('the batch is corrected', _m['batch'] == 'IB2')
check('the adjustment is posted to the ledger',
      txs.count_documents({'type': 'adjustment',
                           'med_name': 'Ibuprofen tabs, 400 mg'}) == 1)
_adj = txs.find_one({'type': 'adjustment'})
check('signed the way every report expects', _adj['quantity'] == -3,
      str(_adj['quantity']))
_line = db['stock_take_counts'].find_one({'med_name': 'Ibuprofen tabs, 400 mg'})
check('the line keeps what it replaced',
      _line.get('expiry_before') == '2027-01-31'
      and _line.get('batch_before') == 'IB1', str(_line))

r = client.post('/stock-take', data={
    'action': 'count', 'stock_take_id': sid,
    'med_name': 'Ibuprofen tabs, 400 mg', 'counted': '',
    'expiry_date': '2027-05-31'}, follow_redirects=True)
check('a detail-only correction works here too',
      meds.find_one({'name': 'Ibuprofen tabs, 400 mg'})['expiry_date'] == '2027-05-31')
check('and leaves the balance alone',
      meds.find_one({'name': 'Ibuprofen tabs, 400 mg'})['balance'] == 47)
r = client.post('/stock-take', data={
    'action': 'count', 'stock_take_id': sid,
    'med_name': 'Ibuprofen tabs, 400 mg'}, follow_redirects=True)
check('an entirely empty submission is refused',
      'Enter a count, or an expiry date or batch number' in r.data.decode(),
      r.data.decode()[:260])

# =====================================================================
print('\n--- 21. The download carries the figure the variance is against ---')
# Pre-existing: the CSV asked for a field called "expected" that no count
# line has ever held, so "On system" came out blank in every download.
r = client.get(f'/stock-take?stock_take_id={sid}&export=csv')
check('it downloads',
      r.headers.get('Content-Type', '').startswith('text/csv'),
      r.headers.get('Content-Type'))
rows = list(_csv.reader(_io.StringIO(r.data.decode())))
check('On system is a column', 'On system' in rows[0], str(rows[0]))
_i = rows[0].index('On system')
_body_rows = [x for x in rows[1:] if x and any(x)]
check('and it is filled in, not blank',
      all(x[_i] != '' for x in _body_rows),
      str([(x[0], x[_i]) for x in _body_rows]))
for _col in ('Expiry was', 'Expiry now', 'Batch was', 'Batch now'):
    check(f'{_col} is in the download', _col in rows[0], str(rows[0]))
check('and the old expiry is actually in it',
      any('2027-01-31' in x for x in _body_rows), str(_body_rows))

# =====================================================================
print('\n--- 22. Act only where the shelf disagrees ---')
# Asked for: "show system count so that when I don't have time I count only
# where there are discrepancies, and in expiry and batch show the old one so
# I can change only when necessary."
reset()
login_as_admin()
meds.insert_many([
    {'name': 'Amoxicillin caps, 500 mg', 'balance': 40, 'batch': 'AMX7',
     'price': 0.95, 'expiry_date': '2027-06-30', 'schedule': 'not controlled'},
    {'name': 'Ibuprofen tabs, 400 mg', 'balance': 50, 'batch': 'IB1',
     'price': 0.3, 'expiry_date': '2027-01-31T00:00:00', 'schedule': 'not controlled'},
    {'name': 'Morphine tabs, 10mg', 'balance': 20, 'batch': 'M1',
     'price': 126.55, 'expiry_date': '2028-03-31', 'schedule': 'controlled'},
    {'name': 'Paracetamol tabs, 500 mg', 'balance': 100, 'batch': 'B1',
     'price': 0.18, 'expiry_date': '2030-01-01', 'schedule': 'not controlled'},
])
client.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
body = client.get(f'/stock-take?stock_take_id={sid}').data.decode()
names = _re.findall(r'name="med_name" value="([^"]+)"', body)
check('a stored expiry with a time still shows as a plain date',
      'value="2027-01-31"' in body, 'the date box cannot show 2027-01-31T00:00:00')

# The browser sends every box back as it stood. Posting the sheet untouched
# must change nothing at all.
def as_sent(edits):
    """What the browser sends: the prefilled boxes, plus whatever was changed."""
    rows = {}
    for m in meds.find({}, {'_id': 0}):
        rows[m['name']] = {'expiry': A._expiry_iso(m.get('expiry_date')),
                           'batch': m.get('batch') or ''}
    for n, ch in edits.items():
        rows.setdefault(n, {}).update(ch)
    return sheet_post(names, rows)

_audit_before = db['audit_log'].count_documents({})
r = client.post('/stock-take', data=as_sent({}), follow_redirects=True)
check('posting the sheet untouched records nothing',
      db['stock_take_counts'].count_documents({}) == 0,
      str(list(db['stock_take_counts'].find({}, {'_id': 0, 'med_name': 1}))))
check('and changes nothing on file',
      meds.find_one({'name': 'Amoxicillin caps, 500 mg'})['batch'] == 'AMX7'
      and meds.find_one({'name': 'Ibuprofen tabs, 400 mg'})['expiry_date']
          == '2027-01-31T00:00:00',
      'a prefilled box sent back unchanged is not a correction')
check('and writes nothing to the audit log',
      db['audit_log'].count_documents({}) == _audit_before)

# Now the real thing: two shelves agree, one is short, one has a new batch.
r = client.post('/stock-take', data=as_sent({
    'Amoxicillin caps, 500 mg': {'agree': 'on'},
    'Paracetamol tabs, 500 mg': {'agree': 'on', 'batch': 'B2'},
    'Ibuprofen tabs, 400 mg':   {'counted': '47'},
}), follow_redirects=True)
_out = r.data.decode()
check('the ticked rows are recorded as agreeing',
      db['stock_take_counts'].count_documents({'variance': 0}) == 2)
_amox = db['stock_take_counts'].find_one({'med_name': 'Amoxicillin caps, 500 mg'})
check('at the balance the system had', _amox['counted'] == 40, str(_amox))
check('and marked as confirmed, not counted',
      _amox.get('method') == 'confirmed', str(_amox.get('method')))
check('a tick moves no stock and posts nothing to the ledger',
      txs.count_documents({'type': 'adjustment',
                           'med_name': 'Amoxicillin caps, 500 mg'}) == 0
      and meds.find_one({'name': 'Amoxicillin caps, 500 mg'})['balance'] == 40)
check('a tick can still carry a corrected batch',
      meds.find_one({'name': 'Paracetamol tabs, 500 mg'})['batch'] == 'B2')
check('the typed count is recorded as counted',
      db['stock_take_counts'].find_one(
          {'med_name': 'Ibuprofen tabs, 400 mg'}).get('method') == 'counted')
check('and its shortfall is posted',
      txs.find_one({'type': 'adjustment',
                    'med_name': 'Ibuprofen tabs, 400 mg'})['quantity'] == -3)
check('the row left alone stays on the sheet',
      'Morphine tabs, 10mg' in client.get(
          f'/stock-take?stock_take_id={sid}').data.decode())
check('the message says how many were ticked rather than counted',
      'ticked as matching the system' in _out, _out[:300])
_page = client.get(f'/stock-take?stock_take_id={sid}').data.decode()
check('the counted table marks the ticked lines', '(ticked)' in _page)

print('\n--- 23. A tick and a count on the same row ---')
reset()
login_as_admin()
meds.insert_one({'name': 'Amoxicillin caps, 500 mg', 'balance': 40, 'batch': 'AMX7',
                 'price': 0.95, 'expiry_date': '2027-06-30', 'schedule': 'x'})
client.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
client.post('/stock-take', data=sheet_post(['Amoxicillin caps, 500 mg'], {
    'Amoxicillin caps, 500 mg': {'agree': 'on', 'counted': '38'}}),
    follow_redirects=True)
_line = db['stock_take_counts'].find_one({})
check('the number written down wins over the tick',
      _line['counted'] == 38 and _line['variance'] == -2, str(_line))
check('and it is recorded as counted', _line.get('method') == 'counted')

print('\n--- 24. A dispense between looking and posting ---')
# The pharmacist sees 40 and ticks it. Before the sheet is posted, two are
# dispensed: system and shelf both say 38. The tick means "the shelf matches
# the system", so it must not turn into a discrepancy of two.
reset()
login_as_admin()
meds.insert_one({'name': 'Amoxicillin caps, 500 mg', 'balance': 40, 'batch': 'AMX7',
                 'price': 0.95, 'expiry_date': '2027-06-30', 'schedule': 'x'})
client.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
client.get(f'/stock-take?stock_take_id={sid}')
meds.update_one({'name': 'Amoxicillin caps, 500 mg'}, {'$inc': {'balance': -2}})
client.post('/stock-take', data=sheet_post(['Amoxicillin caps, 500 mg'], {
    'Amoxicillin caps, 500 mg': {'agree': 'on'}}), follow_redirects=True)
_line = db['stock_take_counts'].find_one({})
check('the tick agrees with the balance at posting, not at looking',
      _line['counted'] == 38 and _line['variance'] == 0, str(_line))
check('so no false discrepancy is posted',
      txs.count_documents({'type': 'adjustment'}) == 0)

print('\n--- 25. The sheet itself ---')
login_as_admin()
reset()
meds.insert_one({'name': 'Amoxicillin caps, 500 mg', 'balance': 40, 'batch': 'AMX7',
                 'price': 0.95, 'expiry_date': '2027-06-30', 'schedule': 'x'})
client.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
body = client.get(f'/stock-take?stock_take_id={sid}').data.decode()
check('there is a tick box per row', 'name="agree__0" type="checkbox"' in body)
check('and a way to tick a whole shelf at once', 'stTickShown()' in body)
check('which only ticks rows with no count typed',
      "count.value.trim() === ''" in body)
check('a changed expiry or batch is highlighted before posting',
      'st-changed' in body and 'data-orig="2027-06-30"' in body)
check('the page says ticking is weaker than counting for controlled drugs',
      'For controlled drugs, counting is safer than' in body)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
