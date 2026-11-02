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
from test_support import arm_csrf
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
    for col in ['medications', 'transactions', 'deliveries', 'audit_log']:
        db[col].delete_many({})


def add(name, balance=10, pack_size=1, pack_price=10.0, supplier='MedSupply',
        schedule='not controlled'):
    meds.insert_one({'name': name, 'balance': balance, 'batch': 'B',
                     'price': pack_price / pack_size, 'pack_size': pack_size,
                     'pack_price': pack_price, 'expiry_date': '2030-01-01',
                     'schedule': schedule, 'supplier': supplier})


def consume(name, per_month):
    """Three months of even consumption inside the 90-day AMC window."""
    for d in (10, 40, 70):
        txs.insert_one({'type': 'dispense', 'med_name': name, 'quantity': per_month,
                        'transaction_id': f'TX-{name}-{d}', 'patient': 'P', 'user': 'u',
                        'timestamp': datetime.utcnow() - timedelta(days=d)})


def first_col(body, after):
    seg = body[body.index(after):]
    seg = seg[:seg.find('</table>')]
    return [_re.sub(r'<[^>]+>', '', x).strip()
            for x in _re.findall(r'<tr[^>]*>\s*<td>(.*?)</td>', seg, _re.S)]


MIXED = ['Zinc Sulphate, 20 mg', 'amoxicillin Cap, 250 mg', 'Paracetamol, 500 mg',
         'ibuprofen, 400 mg', 'Benzyl Benzoate', 'aspirin, 100 mg']
EXPECTED = ['amoxicillin Cap, 250 mg', 'aspirin, 100 mg', 'Benzyl Benzoate',
            'ibuprofen, 400 mg', 'Paracetamol, 500 mg', 'Zinc Sulphate, 20 mg']

# =====================================================================
print('\n--- 1. Names sort the way a person reads them ---')
reset()
login()
for n in MIXED:
    add(n)
today = datetime.utcnow().strftime('%Y-%m-%d')
month_start = datetime.utcnow().strftime('%Y-%m-01')

body = c.post('/reports', data={'report_type': 'stock_on_hand',
                                'end_date': today}).data.decode()
got = first_col(body, 'Stock on Hand')
print('   stock on hand:', got)
check('stock on hand is alphabetical regardless of case', got == EXPECTED, str(got))
check('lower-case names are not dumped after Z',
      got.index('amoxicillin Cap, 250 mg') < got.index('Zinc Sulphate, 20 mg'))

body = c.post('/reports', data={'report_type': 'inventory',
                                'start_date': month_start, 'end_date': today}).data.decode()
check('inventory report is alphabetical', first_col(body, 'Inventory Report') == EXPECTED,
      str(first_col(body, 'Inventory Report')))

body = c.get('/dashboard?sort=name&limit=0').data.decode()
check('dashboard name sort is alphabetical',
      first_col(body, 'Consumption by Month') == EXPECTED,
      str(first_col(body, 'Consumption by Month')))

print('\n--- 2. Stray whitespace does not jump to the top ---')
reset()
login()
for n in ['  Ibuprofen, 400 mg', 'Amoxycillin Cap, 250 mg', 'Zinc, 20 mg']:
    add(n)
got = first_col(c.post('/reports', data={'report_type': 'stock_on_hand',
                                         'end_date': today}).data.decode(), 'Stock on Hand')
check('a leading space no longer forces an item to the front',
      got[0].strip() == 'Amoxycillin Cap, 250 mg', str(got))

print('\n--- 3. New names are stripped on entry ---')
reset()
login()
c.post('/add-medication', data={'med_name': '  Paracetamol, 500 mg  ', 'pack_size': '100',
                                'pack_price': '250', 'schedule': 'not controlled',
                                'initial_balance': '0', 'batch': '', 'expiry_date': '',
                                'stock_receiver': ''}, follow_redirects=True)
check('a name typed with spaces is stored trimmed',
      meds.find_one({})['name'] == 'Paracetamol, 500 mg',
      repr(meds.find_one({})['name']))

print('\n--- 4. Controlled register and stock take are sorted too ---')
reset()
login()
for n in ['Zolpidem, 10 mg', 'diazepam, 5 mg', 'Morphine, 10 mg']:
    add(n, schedule='controlled')
    consume(n, 10)
body = c.post('/reports', data={'report_type': 'controlled_drug_register',
                                'start_date': month_start, 'end_date': today}).data.decode()
order = [m.strip() for m in _re.findall(r'<h3>([^<]+)</h3>', body)]
check('controlled register is alphabetical',
      order == ['diazepam, 5 mg', 'Morphine, 10 mg', 'Zolpidem, 10 mg'], str(order))
c.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
body = c.get(f'/stock-take?stock_take_id={sid}').data.decode()
# the datalist was replaced by a full count sheet — check the sheet's row order
sheet = body[body.index('Count Sheet'):]
rows = _re.findall(r'<td>([^<]+)<input type="hidden" name="med_name"', sheet)
check('stock take count sheet is alphabetical',
      rows == ['diazepam, 5 mg', 'Morphine, 10 mg', 'Zolpidem, 10 mg'], str(rows))

# =====================================================================
print('\n--- 5. Order request: what gets included ---')
reset()
login()
# AMC 100/month each
add('Paracetamol, 500 mg', balance=50, pack_size=100, pack_price=250.0)   # 0.5 months
consume('Paracetamol, 500 mg', 100)
add('aspirin, 100 mg', balance=400, pack_size=100, pack_price=250.0)      # 4 months
consume('aspirin, 100 mg', 100)
add('Zinc Sulphate, 20 mg', balance=0, pack_size=100, pack_price=250.0,
    supplier='PharmaCo')                                                  # out of stock
consume('Zinc Sulphate, 20 mg', 100)
add('Dormant Item, 10 mg', balance=5, pack_size=10, pack_price=10.0)      # no consumption

body = c.post('/reports', data={'report_type': 'order_request',
                                'cover_months': '3'}).data.decode()
check('the report renders', '<h2>Order Request</h2>' in body)
check('a requisition reference is generated', 'REQ-' in body)
check('the raiser is named', 'Admin User' in body)
check('an item below the cover level is included', 'Paracetamol, 500 mg' in body)
check('an out-of-stock item is included', 'Zinc Sulphate, 20 mg' in body)
check('an item with enough cover is excluded', 'aspirin, 100 mg' not in body)
check('an item with no consumption is excluded', 'Dormant Item, 10 mg' not in body)
check('the exclusion of unconsumed items is explained',
      'no recorded demand are not included' in body)


def line(body, name):
    mm = _re.search(r'<td>' + _re.escape(name) + r'</td>(.*?)</tr>', body, _re.S)
    return [_re.sub(r'<[^>]+>', '', x).strip()
            for x in _re.findall(r'<td>(.*?)</td>', mm.group(1), _re.S)] if mm else []


# columns after name: AMC(consumption), unmet/month, AMC used, on hand,
# months left, shortfall, pack size, packs, units, price/pack, line cost
l = line(body, 'Paracetamol, 500 mg')
print('   paracetamol line:', l)
check('AMC is 100', l[0] == '100.0', l[0])
check('on hand 50', l[3] == '50', l[3])
check('months left 0.5', l[4] == '0.5', l[4])
check('shortfall is 3 months cover minus stock (250)', l[5] == '250', l[5])
check('rounded UP to whole packs (3 packs of 100)', l[7] == '3', l[7])
check('units ordered is packs x pack size (300)', l[8] == '300', l[8])
check('line cost is packs x pack price (R750)', l[10] == 'R750.00', l[10])
check('an urgent item is flagged red',
      _re.search(r'<tr class="expired">\s*<td>Paracetamol, 500 mg', body) is not None)

l = line(body, 'Zinc Sulphate, 20 mg')
check('an out-of-stock item reads "Out of stock"', l[4] == 'Out of stock', l[4])

print('\n--- 6. Grouped by supplier, with totals ---')
check('suppliers appear as their own sections',
      'MedSupply &mdash; 1 item(s)' in body and 'PharmaCo &mdash; 1 item(s)' in body)
check('summary section present', '<h3>Summary</h3>' in body)
check('grand total is R1500.00', 'R1500.00' in body, )
check('total packs is 6', '<td>6</td>' in body)
check('a print button is offered', 'window.print()' in body)
check('the estimate is qualified', 'indicative rather than a quotation' in body)

print('\n--- 7. Months of cover changes the quantity ---')
for months, packs in [('2', '2'), ('3', '3'), ('4', '4'), ('6', '6')]:
    b = c.post('/reports', data={'report_type': 'order_request',
                                 'cover_months': months}).data.decode()
    l = line(b, 'Paracetamol, 500 mg')
    check(f'{months} months cover orders {packs} packs', l[7] == packs, f'{l[7]} (row {l})')
b = c.post('/reports', data={'report_type': 'order_request',
                             'cover_months': '99'}).data.decode()
check('an invalid cover period falls back to 3 months', 'Cover:</strong> 3 months' in b)
b = c.post('/reports', data={'report_type': 'order_request',
                             'cover_months': 'abc'}).data.decode()
check('a non-numeric cover period does not error', b.count('Order Request') >= 1)

print('\n--- 8. Partial packs always round up ---')
reset()
login()
# AMC 100, balance 0, pack of 30 -> needs 300 units -> 10 packs exactly
add('Exact Item, 10 mg', balance=0, pack_size=30, pack_price=90.0)
consume('Exact Item, 10 mg', 100)
# AMC 100, balance 0, pack of 40 -> needs 300 -> 7.5 packs -> 8
add('Partial Item, 10 mg', balance=0, pack_size=40, pack_price=80.0)
consume('Partial Item, 10 mg', 100)
body = c.post('/reports', data={'report_type': 'order_request',
                                'cover_months': '3'}).data.decode()
l = line(body, 'Exact Item, 10 mg')
check('an exact multiple orders exactly that many packs', l[7] == '10', str(l))
check('units match the shortfall exactly', l[8] == '300', str(l))
l = line(body, 'Partial Item, 10 mg')
check('a partial pack rounds up (7.5 -> 8)', l[7] == '8', str(l))
check('units exceed the shortfall as a result', l[8] == '320' and l[5] == '300', str(l))

print('\n--- 9. Missing prices and suppliers ---')
reset()
login()
meds.insert_one({'name': 'Unpriced Item, 10 mg', 'balance': 0, 'batch': 'B',
                 'price': 0, 'expiry_date': '2030-01-01', 'schedule': 'not controlled'})
consume('Unpriced Item, 10 mg', 100)
body = c.post('/reports', data={'report_type': 'order_request',
                                'cover_months': '3'}).data.decode()
check('an item with no pack price is still listed', 'Unpriced Item, 10 mg' in body)
check('its cost is shown as a dash rather than zero', '&ndash;' in body or '–' in body)
check('unpriced items are counted and flagged',
      'no pack price on file' in body)
check('an item with no supplier is grouped as such',
      'Supplier not recorded' in body)

print('\n--- 10. Nothing to order, and access ---')
reset()
login()
add('Well Stocked, 10 mg', balance=10000, pack_size=100, pack_price=250.0)
consume('Well Stocked, 10 mg', 100)
body = c.post('/reports', data={'report_type': 'order_request',
                                'cover_months': '3'}).data.decode()
check('a fully stocked pharmacy says nothing needs ordering',
      'Nothing needs ordering' in body)
check('no summary table when there is nothing to order', '<h3>Summary</h3>' not in body)

reset()
login()
add('Paracetamol, 500 mg', balance=0, pack_size=100, pack_price=250.0)
consume('Paracetamol, 500 mg', 100)
add('Zinc Sulphate, 20 mg', balance=0, pack_size=100, pack_price=250.0)
consume('Zinc Sulphate, 20 mg', 100)
body = c.post('/reports', data={'report_type': 'order_request', 'cover_months': '3',
                                'search': 'Zinc'}).data.decode()
check('search narrows the requisition',
      'Zinc Sulphate, 20 mg' in body and 'Paracetamol, 500 mg' not in body)

login('user', 'Pharm')
body = c.post('/reports', data={'report_type': 'order_request',
                                'cover_months': '3'}).data.decode()
check('a non-admin can also raise a request', 'Order Request' in body)

# =====================================================================
print('\n--- 11. Pack size unknown vs genuinely one ---')
reset()
login()
# never recorded: no pack_size key at all
meds.insert_one({'name': 'Unsized Item, 10 mg', 'balance': 0, 'batch': 'B',
                 'price': 250.0, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled', 'supplier': 'S'})
consume('Unsized Item, 10 mg', 100)
# genuinely sold as singles
add('Single Item, 10 mg', balance=0, pack_size=1, pack_price=5.0, supplier='S')
consume('Single Item, 10 mg', 100)
body = c.post('/reports', data={'report_type': 'order_request',
                                'cover_months': '3'}).data.decode()
lu = line(body, 'Unsized Item, 10 mg')
ls = line(body, 'Single Item, 10 mg')
print('   unsized:', lu)
print('   single :', ls)
check('an unrecorded pack size says so rather than showing 1',
      lu[6] == 'not recorded', lu[6])
check('a genuine pack size of 1 still shows 1', ls[6] == '1', ls[6])
check('quantities still fall back to single units', lu[8] == '300', lu[8])
check('unsized items are counted in the summary', '<td>1</td>' in body)
check('the reader is warned to check against the supplier pack',
      'no pack size on file' in body)
check('the warning explains how it gets fixed',
      'first time an item is received on a delivery' in body)

print('\n--- 12. A delivery teaches the request the right pack size ---')
did = open_del = c.post('/receive', data={
    'action': 'open', 'supplier': 'S', 'order_number': 'PO-1', 'order_total': '250',
    'invoice_number': 'INV-PS', 'invoice_total': '250', 'stock_receiver': 'R'},
    follow_redirects=True)
did = str(db['deliveries'].find_one({'invoice_number': 'INV-PS'})['_id'])
c.post('/receive', data={'action': 'line', 'delivery_id': did,
                         'med_name': 'Unsized Item, 10 mg', 'quantity': '100',
                         'pack_size': '100', 'pack_price': '250', 'batch': 'B',
                         'expiry_date': '2030-01-01'}, follow_redirects=True)
m = meds.find_one({'name': 'Unsized Item, 10 mg'})
check('the delivery records the pack size on the medication', m['pack_size'] == 100)
check('and corrects the unit price', m['price'] == 2.50, str(m['price']))
body = c.post('/reports', data={'report_type': 'order_request',
                                'cover_months': '3'}).data.decode()
l = line(body, 'Unsized Item, 10 mg')
print('   after delivery:', l)
check('the request now shows the real pack size', l[6] == '100', l[6])
check('and asks for whole packs (200 units = 2 packs)', l[7] == '2' and l[8] == '200', str(l))
check('and can now cost it', l[10] == 'R500.00', l[10])
check('the unsized warning is gone', 'no pack size on file' not in body)

print('\n--- 13. Pack size can be set directly, without waiting ---')
reset()
login()
meds.insert_one({'name': 'Unsized Item, 10 mg', 'balance': 0, 'batch': 'B',
                 'price': 250.0, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled', 'supplier': 'S'})
consume('Unsized Item, 10 mg', 100)
body = c.get('/edit-medication?med_name=Unsized Item, 10 mg').data.decode()
check('the edit form offers pack size', 'name="pack_size"' in body)
check('the edit form asks price per pack', 'name="pack_price"' in body
      and 'Price per Unit' not in body)
c.post('/edit-medication', data={'med_name': 'Unsized Item, 10 mg', 'balance': '0',
                                 'batch': 'B', 'pack_size': '100', 'pack_price': '250',
                                 'expiry_date': '2030-01-01',
                                 'schedule': 'not controlled'}, follow_redirects=True)
m = meds.find_one({'name': 'Unsized Item, 10 mg'})
check('editing sets the pack size', m['pack_size'] == 100)
check('editing derives the unit price', m['price'] == 2.50, str(m['price']))
l = line(c.post('/reports', data={'report_type': 'order_request',
                                  'cover_months': '3'}).data.decode(),
         'Unsized Item, 10 mg')
check('the request picks it up immediately', l[6] == '100' and l[7] == '3', str(l))

for bad in ('0', '-1', 'abc'):
    c.post('/edit-medication', data={'med_name': 'Unsized Item, 10 mg', 'balance': '0',
                                     'batch': 'B', 'pack_size': bad, 'pack_price': '250',
                                     'expiry_date': '2030-01-01',
                                     'schedule': 'not controlled'}, follow_redirects=True)
check('an invalid pack size is rejected on edit',
      meds.find_one({'name': 'Unsized Item, 10 mg'})['pack_size'] == 100)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
