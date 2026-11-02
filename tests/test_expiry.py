from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, re as _re
from datetime import datetime, timedelta, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
# Bound by VALUE on purpose. As a closure over the name, any later
# assignment to that name anywhere in this file would silently replace
# the database for the rest of the run.
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
db = _f['pharmacy_db']
meds, txs = db['medications'], db['transactions']
c = A.app.test_client()

# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf, inventory_rows, reconciles
arm_csrf(c)
PASS, FAIL = [], []

def _reg_rows(b, name=None):
    """Rows of the losses register: DATE, Item, QTY, EXPIRY, Batch, Cost, Remarks."""
    seg = b[b.index('Stock Losses for'):]
    seg = seg[:seg.index('</table>')]
    out = []
    for m in _re.findall(r'<tr[^>]*>(.*?)</tr>', seg, _re.S):
        cells = [_re.sub(r'<[^>]+>', '', x).strip()
                 for x in _re.findall(r'<td>(.*?)</td>', m, _re.S)]
        if cells and (name is None or cells[1] == name):
            out.append(cells)
    return out



def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin', name='Admin User'):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['medications', 'transactions', 'deliveries', 'audit_log']:
        db[col].delete_many({})


TODAY = datetime.utcnow()
PAST = (TODAY - timedelta(days=30)).strftime('%Y-%m-%d')
FUTURE = (TODAY + timedelta(days=400)).strftime('%Y-%m-%d')
M_START = TODAY.strftime('%Y-%m-01')
M_END = TODAY.strftime('%Y-%m-%d')


def add(name, balance=1000, expiry=PAST, price=2.5):
    meds.insert_one({'name': name, 'balance': balance, 'batch': 'B1', 'price': price,
                     'pack_size': 100, 'pack_price': price * 100,
                     'expiry_date': expiry, 'schedule': 'not controlled'})


def expired_list():
    return c.post('/reports', data={'report_type': 'expired_list'}).data.decode()


def remove(med, qty, reason=''):
    return c.post('/remove-expired', data={'med_name': med, 'quantity': str(qty),
                                           'reason': reason,
                                           'report_type': 'expired_list'},
                  follow_redirects=True)


def correct(med, date):
    return c.post('/correct-expiry', data={'med_name': med, 'expiry_date': date,
                                           'report_type': 'expired_list'},
                  follow_redirects=True)


print('\n--- 1. Both actions appear, and only for admins ---')
reset()
login()
add('Paracetamol, 500 mg')
body = expired_list()
check('the expired list offers Remove Expired', 'Remove Expired' in body)
check('it offers a quantity, not just a button', 'name="quantity"' in body)
check('the quantity defaults to the whole balance', 'value="1000"' in body)
check('it is capped at the balance on hand', 'max="1000"' in body)
check('it offers Correct Date separately', 'Correct Date' in body)
check('the difference between them is explained', 'changes the date and moves no stock' in body)

login('user', 'Pharm')
body = expired_list()
check('a non-admin sees neither action',
      'Remove Expired' not in body and 'Correct Date' not in body)
check('a non-admin cannot remove by posting directly',
      c.post('/remove-expired', data={'med_name': 'Paracetamol, 500 mg',
                                      'quantity': '10'}, follow_redirects=True)
      is not None and meds.find_one({})['balance'] == 1000)
check('a non-admin cannot correct a date by posting directly',
      c.post('/correct-expiry', data={'med_name': 'Paracetamol, 500 mg',
                                      'expiry_date': FUTURE}, follow_redirects=True)
      is not None and meds.find_one({})['expiry_date'] == PAST)
login()

print('\n--- 2. Partial removal ---')
reset()
login()
add('Paracetamol, 500 mg', balance=1000, price=2.5)
r = remove('Paracetamol, 500 mg', 300, reason='batch B1 only')
check('only the stated quantity leaves the shelf',
      meds.find_one({})['balance'] == 700, str(meds.find_one({})['balance']))
t = txs.find_one({'type': 'expiry_removal'})
check('a write-off transaction is recorded', t is not None)
check('the quantity is signed negative', t['quantity'] == -300, str(t['quantity']))
check('it is valued at the unit price', t['line_value'] == -750.0, str(t['line_value']))
check('the reason is kept', t['reason'] == 'batch B1 only')
check('the balance before is kept for the audit trail', t['balance_before'] == 1000)
check('the confirmation states the new balance and value',
      'Balance is now 700' in r.data.decode() and 'R750.00' in r.data.decode())
check('it is written to the audit log',
      db['audit_log'].count_documents({'target_type': 'expiry_removal'}) == 1)

print('\n--- 3. Removal cannot exceed stock or go negative ---')
r = remove('Paracetamol, 500 mg', 900)
check('removing more than is on hand is refused',
      meds.find_one({})['balance'] == 700, str(meds.find_one({})['balance']))
check('the refusal says how much is actually there', 'only 700 on hand' in r.data.decode())
for bad in ('0', '-5', 'abc', ''):
    remove('Paracetamol, 500 mg', bad)
check('invalid quantities are refused', meds.find_one({})['balance'] == 700)
check('no stray write-off transactions',
      txs.count_documents({'type': 'expiry_removal'}) == 1)
r = remove('No Such Item, 10 mg', 5)
check('an unknown medication is refused', 'not found' in r.data.decode())

print('\n--- 4. Correcting a date moves no stock ---')
reset()
login()
add('Paracetamol, 500 mg', balance=1000, expiry=PAST)
r = correct('Paracetamol, 500 mg', FUTURE)
check('the expiry date is updated', meds.find_one({})['expiry_date'] == FUTURE)
check('the balance is untouched', meds.find_one({})['balance'] == 1000)
check('no write-off is created', txs.count_documents({'type': 'expiry_removal'}) == 0)
check('the message says no stock moved', 'No stock was moved' in r.data.decode())
check('the change is audited',
      db['audit_log'].count_documents({'action': 'UPDATE', 'target_type': 'medication'}) == 1)
check('the item drops off the expired list',
      'Paracetamol, 500 mg' not in expired_list())
r = correct('Paracetamol, 500 mg', 'not-a-date')
check('an invalid date is refused', meds.find_one({})['expiry_date'] == FUTURE)
r = correct('Paracetamol, 500 mg', FUTURE)
check('setting the same date is a no-op with a clear message',
      'already' in r.data.decode())

print('\n--- 5. Reports unwind write-offs correctly ---')
reset()
login()
add('Paracetamol, 500 mg', balance=1000, price=2.5, expiry=PAST)
remove('Paracetamol, 500 mg', 200)
body = c.post('/reports', data={'report_type': 'inventory',
                                'start_date': M_START, 'end_date': M_END}).data.decode()
row = inventory_rows(body).get('Paracetamol, 500 mg', {})
print('   admin inventory row:', row)
check('the beginning balance is the pre-write-off figure',
      row.get('Beginning Balance') == '1000', str(row))
check('the 200 written off is inside Issued', row.get('Issued') == '200', str(row))
check('the current balance is 800', row.get('Current Balance') == '800', str(row))
check('the row reconciles', row and reconciles(row), str(row))
check('a write-off is not consumption, so the Adjusted AMC stays 0',
      row.get('Adjusted AMC') == '0', str(row))
check('the reconciliation formula is stated',
      'Beginning + Received &minus; Issued = Current' in body)

print('\n--- 6. Everybody sees the same table ---')
login('user', 'Pharm')
ubody = c.post('/reports', data={'report_type': 'inventory',
                                 'start_date': M_START, 'end_date': M_END}).data.decode()
u = inventory_rows(ubody).get('Paracetamol, 500 mg', {})
print('   non-admin row:', u)
check('no Expired column', '<th>Expired</th>' not in ubody)
check('no Adjustment column either', '<th>Adjustment</th>' not in ubody)
check('a non-admin sees the same row as an admin', u == row, f'{u} vs {row}')
login()

print('\n--- 7. Stock on hand as at a date before the write-off ---')
body = c.post('/reports', data={'report_type': 'stock_on_hand',
                                'end_date': (TODAY - timedelta(days=400)).strftime('%Y-%m-%d')}).data.decode()
mm = _re.search(r'<td>Paracetamol, 500 mg</td>\s*<td>(\d+)</td>', body)
check('the balance before the write-off is 1000', mm and mm.group(1) == '1000',
      mm.group(1) if mm else 'not found')

print('\n--- 8. The monthly report values the loss ---')
body = c.post('/reports', data={'report_type': 'monthly_report',
                                'start_date': M_START, 'end_date': M_END}).data.decode()
mm = _re.search(r'<td>Expired stock written off</td>\s*<td>(.*?)</td>', body, _re.S)
val = _re.sub(r'<[^>]+>', '', mm.group(1)).strip() if mm else None
check('the write-off appears as a valued line', val == 'R-500.00', str(val))
check('the write-off count is shown', '1 write-off(s)' in body)
login('user', 'Pharm')
ubody = c.post('/reports', data={'report_type': 'monthly_report',
                                 'start_date': M_START, 'end_date': M_END}).data.decode()
check('expired losses are visible to everyone (it is not a discrepancy)',
      'Expired stock written off' in ubody)
check('but stock take adjustments remain admin-only',
      'Stock take adjustments' not in ubody)
login()

print('\n--- 9. Near-expired list offers the same actions ---')
reset()
login()
soon = (TODAY + timedelta(days=20)).strftime('%Y-%m-%d')
add('Soon Item, 10 mg', balance=100, expiry=soon)
body = c.post('/reports', data={'report_type': 'near_expired_list'}).data.decode()
check('near-expired offers Correct Date', 'Correct Date' in body)
check('near-expired offers Remove Expired', 'Remove Expired' in body)
body = c.post('/reports', data={'report_type': 'stock_on_hand'}).data.decode()
check('the plain stock list does NOT offer expiry actions',
      'Remove Expired' not in body)

print('\n--- 10. The admin stays on the list after acting ---')
reset()
login()
add('Paracetamol, 500 mg', balance=1000)
add('Aspirin, 100 mg', balance=500)
r = remove('Paracetamol, 500 mg', 100)
body = r.data.decode()
check('the expired list is re-rendered, not the menu',
      'Expired Drugs List' in body or 'Aspirin, 100 mg' in body, body[:200])
check('the remaining expired item is still listed', 'Aspirin, 100 mg' in body)

# =====================================================================
print('\n--- 11. The two lists answer different questions ---')
reset()
login()
add('Paracetamol, 500 mg', balance=1000, price=2.5, expiry=PAST)
add('Aspirin, 100 mg', balance=200, price=5.0, expiry=PAST)


def writeoffs(start=None, end=None, search=None):
    d = {'report_type': 'expiry_writeoffs', 'start_date': start or M_START,
         'end_date': end or M_END}
    if search:
        d['search'] = search
    return c.post('/reports', data=d).data.decode()


body = writeoffs()
check('the losses report exists', 'Stock Losses for' in body)
check('it starts empty even though stock is expired',
      'No stock was written off in this period' in body)
check('meanwhile the work list shows both items',
      'Paracetamol, 500 mg' in expired_list() and 'Aspirin, 100 mg' in expired_list())
check('the work list explains what it is', 'work list' in expired_list())
check('and points at the write-off report', 'Expiry Write-Offs' in expired_list())

print('\n--- 12. A removal moves an item from one list to the other ---')
remove('Paracetamol, 500 mg', 400, reason='batch B1 perished')
body = writeoffs()
check('the removal now appears in the write-off report',
      'Paracetamol, 500 mg' in body)
check('with the units removed', '<td>400</td>' in body)
check('valued at cost', 'R1000.00' in body)
check('with the reason kept', 'batch B1 perished' in body)
check('and who did it', 'Admin User' in body)
wl = expired_list()
check('the item remains on the work list while units are still expired',
      'Paracetamol, 500 mg' in wl)
check('showing only the units still on the shelf', '>600<' in wl or 'value="600"' in wl)

remove('Paracetamol, 500 mg', 600)
wl = expired_list()
check('once fully removed it drops off the work list',
      'Paracetamol, 500 mg' not in wl)
body = writeoffs()
check('but stays permanently in the write-off record',
      'Paracetamol, 500 mg' in body)
rows = _reg_rows(body, 'Paracetamol, 500 mg')
check('as two separate write-offs', len(rows) == 2, str(rows))
check('of 400 and 600 units', sorted(r[2] for r in rows) == ['400', '600'],
      str([r[2] for r in rows]))
check('the reason is kept on the line it belongs to',
      any(r[6] == 'batch B1 perished' for r in rows), str([r[6] for r in rows]))

print('\n--- 13. Write-off totals ---')
def summary_row(b):
    seg = b[b.index('<h3>Summary</h3>'):]
    seg = seg[:seg.index('</table>')]
    return [_re.sub(r'<[^>]+>', '', x).strip()
            for x in _re.findall(r'<td>(.*?)</td>', seg, _re.S)]


srow = summary_row(body)
print('   summary [write-offs, items, units, value]:', srow)
check('two write-offs recorded', srow[0] == '2', str(srow))
check('one item affected', srow[1] == '1', str(srow))
check('total units lost is 1000', srow[2] == '1000', str(srow))
check('total value lost is R2500.00', srow[3] == 'R2500.00', str(srow))
remove('Aspirin, 100 mg', 200)
body = writeoffs()
check('two items affected now', summary_row(body)[1] == '2', str(summary_row(body)))
seg = body[body.index('By Medication'):]
order = _re.findall(r'<td>([^<]*, \d+ mg)</td>', seg)
check('the by-medication summary ranks by value lost',
      order[0] == 'Paracetamol, 500 mg', str(order))
check('repeat write-offs are counted per item', '<td>2</td>' in seg)
check('the over-ordering interpretation is offered', 'over-ordering signal' in body)

print('\n--- 14. Filtering and date range ---')
check('search narrows by medication',
      'Aspirin, 100 mg' in writeoffs(search='Aspirin')
      and 'Paracetamol, 500 mg' not in writeoffs(search='Aspirin'))
check('search matches the reason text', 'Paracetamol, 500 mg' in writeoffs(search='perished'))
past_month = (TODAY.replace(day=1) - timedelta(days=5))
body = writeoffs(start=past_month.strftime('%Y-%m-01'),
                 end=past_month.strftime('%Y-%m-%d'))
check('a prior month shows nothing', 'No stock was written off' in body)
check('missing dates are reported rather than crashing',
      c.post('/reports', data={'report_type': 'expiry_writeoffs'}).status_code == 200)

print('\n--- 15. Everyone can see losses; the totals agree with the month ---')
login('user', 'Pharm')
ubody = writeoffs()
check('a non-admin can run the losses report', 'Stock Losses for' in ubody)
check('and sees the same value lost', 'R3500.00' in ubody)
login()
mbody = c.post('/reports', data={'report_type': 'monthly_report',
                                 'start_date': M_START, 'end_date': M_END}).data.decode()
mm = _re.search(r'<td>Expired stock written off</td>\s*<td>(.*?)</td>', mbody, _re.S)
val = _re.sub(r'<[^>]+>', '', mm.group(1)).strip() if mm else None
check('the monthly report total matches the write-off report', val == 'R-3500.00', str(val))

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
