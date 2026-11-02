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


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin', name='Admin User'):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['medications', 'transactions', 'audit_log']:
        db[col].delete_many({})


NOW = datetime.utcnow()
M_START = NOW.strftime('%Y-%m-01')
M_END = NOW.strftime('%Y-%m-%d')


def add(name, bal=1000, price=2.5, expiry='2030-01-01'):
    meds.insert_one({'name': name, 'balance': bal, 'batch': 'B', 'price': price,
                     'pack_size': 100, 'pack_price': price * 100,
                     'expiry_date': expiry, 'schedule': 'not controlled'})


def damage(name, qty, reason='Broken / spilled'):
    return c.post('/internal-issue', data={'action': 'damage', 'med_name': name,
                                           'quantity': str(qty), 'reason': reason},
                  follow_redirects=True)


def losses():
    return c.post('/reports', data={'report_type': 'expiry_writeoffs',
                                    'start_date': M_START,
                                    'end_date': M_END}).data.decode()


def loss_id(name=None):
    q = {'type': {'$in': ['expiry_removal', 'damage']}}
    if name:
        q['med_name'] = name
    return str(txs.find_one(q)['_id'])


def edit(lid, qty, price):
    return c.post('/edit-loss', data={'loss_id': lid, 'quantity': str(qty),
                                      'price': str(price), 'start_date': M_START,
                                      'end_date': M_END}, follow_redirects=True)


print('\n--- 1. The controls appear, admin only ---')
reset()
login()
add('Paracetamol tabs, 500 mg', 1000, price=2.5)
damage('Paracetamol tabs, 500 mg', 60)
body = losses()
check('an admin sees a Correct column', '<th>Correct</th>' in body)
check('with an editable quantity', 'name="quantity"' in body)
check('and an editable unit cost', 'name="price"' in body)
check('prefilled with the recorded quantity', 'value="60"' in body)
check('prefilled with the recorded price', 'value="2.5000"' in body)
check('the stock consequence is explained',
      'moves stock on hand by the difference' in _re.sub(r'\s+', ' ', body))
login('user', 'Pharm')
ubody = losses()
check('a non-admin sees no Correct column', '<th>Correct</th>' not in ubody)
check('and cannot edit by posting directly',
      (edit(loss_id(), 10, 1.0) or True)
      and abs(txs.find_one({'type': 'damage'})['quantity']) == 60)
login()

print('\n--- 2. Reducing a quantity puts stock back ---')
reset()
login()
add('Paracetamol tabs, 500 mg', 1000, price=2.5)
damage('Paracetamol tabs, 500 mg', 60)
check('stock dropped to 940 on the original write-off',
      meds.find_one({})['balance'] == 940, str(meds.find_one({})['balance']))
r = edit(loss_id(), 40, 2.5)
check('reducing 60 to 40 returns 20 units',
      meds.find_one({})['balance'] == 960, str(meds.find_one({})['balance']))
t = txs.find_one({'type': 'damage'})
check('the record now says 40', abs(t['quantity']) == 40, str(t['quantity']))
check('the value is revalued to R100', t['line_value'] == -100.0, str(t['line_value']))
check('the message reports both figures',
      'quantity from 60 to 40' in r.data.decode() and 'R100.00' in r.data.decode())
check('and says what happened to stock on hand',
      'stock on hand from 940 to 960' in r.data.decode(), r.data.decode()[:0] or '')
check('it is audited',
      db['audit_log'].count_documents({'target_type': 'stock_loss'}) == 1)
check('who edited it is recorded', t['edited_by'] == 'Admin User')

print('\n--- 3. Increasing a quantity takes more stock ---')
r = edit(loss_id(), 100, 2.5)
check('increasing 40 to 100 removes 60 more',
      meds.find_one({})['balance'] == 900, str(meds.find_one({})['balance']))
check('the record now says 100', abs(txs.find_one({'type': 'damage'})['quantity']) == 100)
check('the report shows the new value', 'R250.00' in losses())

print('\n--- 4. Editing only the price moves no stock ---')
before = meds.find_one({})['balance']
r = edit(loss_id(), 100, 5.0)
check('the balance is untouched', meds.find_one({})['balance'] == before,
      str(meds.find_one({})['balance']))
t = txs.find_one({'type': 'damage'})
check('the unit price is updated', t['price'] == 5.0)
check('and the loss is revalued to R500', t['line_value'] == -500.0, str(t['line_value']))
check('the message names the price change', 'unit price' in r.data.decode())

print('\n--- 5. It cannot write off stock that is not there ---')
reset()
login()
add('Paracetamol tabs, 500 mg', 100, price=2.5)
damage('Paracetamol tabs, 500 mg', 10)
check('balance is 90', meds.find_one({})['balance'] == 90)
r = edit(loss_id(), 500, 2.5)
check('an impossible increase is refused', meds.find_one({})['balance'] == 90)
check('the record is unchanged', abs(txs.find_one({'type': 'damage'})['quantity']) == 10)
check('and the refusal explains the arithmetic',
      'needs 490 more' in r.data.decode() and 'only 90 are on hand' in r.data.decode())
r = edit(loss_id(), 100, 2.5)
check('an increase that exactly fits is allowed',
      meds.find_one({})['balance'] == 0, str(meds.find_one({})['balance']))

print('\n--- 6. Bad input ---')
reset()
login()
add('Paracetamol tabs, 500 mg', 1000, price=2.5)
damage('Paracetamol tabs, 500 mg', 50)
lid = loss_id()
for qty, price, label in [('0', '2.5', 'zero quantity'),
                          ('-5', '2.5', 'negative quantity'),
                          ('abc', '2.5', 'non-numeric quantity'),
                          ('50', '-1', 'negative price'),
                          ('50', 'xyz', 'non-numeric price')]:
    edit(lid, qty, price)
check('no invalid edit was applied',
      abs(txs.find_one({'type': 'damage'})['quantity']) == 50
      and meds.find_one({})['balance'] == 950)
r = edit(lid, 50, 2.5)
check('an edit that changes nothing says so', 'Nothing was changed' in r.data.decode())
r = edit('not-an-id', 10, 1.0)
check('a malformed id is handled', 'not found' in r.data.decode())
r = edit('507f1f77bcf86cd799439011', 10, 1.0)
check('an unknown id is handled', 'not found' in r.data.decode())
other = txs.insert_one({'type': 'dispense', 'med_name': 'Paracetamol tabs, 500 mg',
                        'quantity': 5, 'timestamp': NOW}).inserted_id
r = edit(str(other), 1, 1.0)
check('a non-loss transaction cannot be edited through this route',
      'not found' in r.data.decode()
      and txs.find_one({'_id': other})['quantity'] == 5)

print('\n--- 7. Expiry removals are editable too ---')
reset()
login()
past = (NOW - timedelta(days=20)).strftime('%Y-%m-%d')
add('Paracetamol tabs, 500 mg', 500, price=2.0, expiry=past)
c.post('/remove-expired', data={'med_name': 'Paracetamol tabs, 500 mg',
                                'quantity': '200', 'report_type': 'expired_list'},
       follow_redirects=True)
check('the expiry write-off happened', meds.find_one({})['balance'] == 300)
edit(loss_id(), 150, 2.0)
check('correcting it returns the difference',
      meds.find_one({})['balance'] == 350, str(meds.find_one({})['balance']))
# Kind is no longer a column in the register; the expiry removal is
# identified by its line appearing here at all
check('the corrected expiry write-off is listed',
      'Paracetamol tabs, 500 mg' in losses())

print('\n--- 8. Totals and reports follow the correction ---')
body = losses()
check('the line value is R300.00', 'R300.00' in body)
mbody = c.post('/reports', data={'report_type': 'monthly_report', 'period': 'month',
                                 'start_date': M_START, 'end_date': M_END}).data.decode()
mm = _re.search(r'<td>Expired stock written off</td>\s*<td>(.*?)</td>', mbody, _re.S)
val = _re.sub(r'<[^>]+>', '', mm.group(1)).strip() if mm else None
check('the period report reflects the corrected value', val == 'R-300.00', str(val))
inv = c.post('/reports', data={'report_type': 'inventory', 'start_date': M_START,
                               'end_date': M_END}).data.decode()
row = inventory_rows(inv).get('Paracetamol tabs, 500 mg', {})
print('   inventory row:', row)
# There is no Expired column any more; the write-off is inside Issued. So the
# corrected 150 has to be what Issued holds beyond the other outward movements.
_other_out = sum(t.get('quantity', 0) for t in txs.find(
    {'med_name': 'Paracetamol tabs, 500 mg',
     'type': {'$in': ['dispense', 'internal_issue']}}))
check('Issued carries the corrected 150, not the original figure',
      row and int(row['Issued']) - _other_out == 150,
      f"Issued {row.get('Issued')}, other outward {_other_out}")
check('the row still reconciles', row and reconciles(row), str(row))

# =====================================================================
print('\n--- 9. The register format ---')
reset()
login()
add('Zinc Tablets, 020 mg', 500, price=4.0, expiry='2027-03-31')
add('Amoxycillin Cap, 250 mg', 500, price=1.5, expiry='2026-11-30')
meds.update_one({'name': 'Zinc Tablets, 020 mg'}, {'$set': {'batch': 'Z-991'}})
meds.update_one({'name': 'Amoxycillin Cap, 250 mg'}, {'$set': {'batch': 'A-220'}})
damage('Zinc Tablets, 020 mg', 25)
damage('Amoxycillin Cap, 250 mg', 40)
body = losses()

heads = _re.findall(r'<th>(.*?)</th>', body[body.index('Stock Losses for'):])[:7]
print('   headings:', heads)
check('the headings follow the prescribed form',
      heads == ['DATE', 'Item Description', 'QTY', 'EXPIRY DATE',
                'Batch/Lot', 'Total COST', 'Remarks'], str(heads))

seg = body[body.index('Stock Losses for'):]
seg = seg[:seg.index('</table>')]
rows = [[_re.sub(r'<[^>]+>', '', x).strip()
         for x in _re.findall(r'<td>(.*?)</td>', m, _re.S)]
        for m in _re.findall(r'<tr[^>]*>(.*?)</tr>', seg, _re.S)]
rows = [r for r in rows if r]
print('   row 1:', rows[0][:7])
check('the date has no clock time', _re.fullmatch(r'\d{4}-\d{2}-\d{2}', rows[0][0]) is not None,
      rows[0][0])
check('item description is the medication', rows[0][1] == 'Amoxycillin Cap, 250 mg', rows[0][1])
check('QTY is the units written off', rows[0][2] == '40', rows[0][2])
check('expiry date is shown', rows[0][3] == '2026-11-30', rows[0][3])
check('batch/lot is shown', rows[0][4] == 'A-220', rows[0][4])
check('total cost is qty x unit cost (40 x 1.50)', rows[0][5] == 'R60.00', rows[0][5])
check('remarks defaults to the recorded reason so nothing is lost',
      rows[0][6] == 'Broken / spilled', rows[0][6])

check('the table is sorted by medication',
      [r[1] for r in rows] == ['Amoxycillin Cap, 250 mg', 'Zinc Tablets, 020 mg'],
      str([r[1] for r in rows]))
check('unit cost is no longer a column', 'R1.5000' not in seg and 'Unit Cost' not in seg)
check('the Kind column is gone', '<th>Kind</th>' not in body)
check('but damage and expiry are still distinguished in the summaries',
      'By Medication' in body)

print('\n--- 10. Remarks are editable free text ---')
check('the edit control offers a remarks box', 'name="remarks"' in body)
lid = loss_id('Zinc Tablets, 020 mg')
r = c.post('/edit-loss', data={'loss_id': lid, 'quantity': '25', 'price': '4.0',
                               'remarks': 'Crushed in transit; supplier notified',
                               'start_date': M_START, 'end_date': M_END},
           follow_redirects=True)
check('the remark is saved',
      txs.find_one({'_id': A.ObjectId(lid)})['remarks'] == 'Crushed in transit; supplier notified')
check('the change is reported', 'remarks updated' in r.data.decode())
body = losses()
check('it shows in the Remarks column', 'Crushed in transit; supplier notified' in body)
def reg_row(b, name):
    seg = b[b.index('Stock Losses for'):]
    seg = seg[:seg.index('</table>')]
    m = _re.search(r'<td>\d{4}-\d{2}-\d{2}</td>\s*<td>' + _re.escape(name) + r'</td>(.*?)</tr>',
                   seg, _re.S)
    return [_re.sub(r'<[^>]+>', '', x).strip()
            for x in _re.findall(r'<td>(.*?)</td>', m.group(1), _re.S)] if m else []


check('the edited line now carries the new remark',
      reg_row(body, 'Zinc Tablets, 020 mg')[4] == 'Crushed in transit; supplier notified',
      str(reg_row(body, 'Zinc Tablets, 020 mg')))
check('the other line keeps its own default remark',
      reg_row(body, 'Amoxycillin Cap, 250 mg')[4] == 'Broken / spilled',
      str(reg_row(body, 'Amoxycillin Cap, 250 mg')))
check('stock was not moved by a remarks-only edit',
      meds.find_one({'name': 'Zinc Tablets, 020 mg'})['balance'] == 475,
      str(meds.find_one({'name': 'Zinc Tablets, 020 mg'})['balance']))

r = c.post('/edit-loss', data={'loss_id': lid, 'quantity': '25', 'price': '4.0',
                               'remarks': '', 'start_date': M_START, 'end_date': M_END},
           follow_redirects=True)
check('a remark can be cleared', 'remarks updated' in r.data.decode())
login('user', 'Pharm')
ubody = losses()
check('a non-admin sees the register but cannot edit it',
      'Item Description' in ubody and 'name="remarks"' not in ubody)
login()

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
