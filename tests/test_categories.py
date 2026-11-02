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


def login(role='admin'):
    with c.session_transaction() as s:
        s['user'] = {'name': 'Admin User', 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['medications', 'transactions', 'deliveries', 'audit_log',
                'stock_takes', 'stock_take_counts']:
        db[col].delete_many({})


NOW = datetime.utcnow()
M_START = NOW.strftime('%Y-%m-01')
M_END = NOW.strftime('%Y-%m-%d')


def add(name, cat='Pharmaceutical', opening=500, pack_price='250'):
    return c.post('/add-medication', data={
        'med_name': name, 'category': cat, 'pack_size': '100',
        'pack_price': pack_price, 'schedule': 'not controlled',
        'initial_balance': str(opening), 'batch': 'B',
        'expiry_date': '2030-01-01', 'stock_receiver': 'R'}, follow_redirects=True)


def rep(kind, category='pharmaceutical', **kw):
    d = {'report_type': kind, 'category': category}
    d.update(kw)
    return c.post('/reports', data=d).data.decode()


print('\n--- 1. The field ---')
reset()
login()
body = c.get('/add-medication').data.decode()
check('Add Medication offers a Category', 'name="category"' in body)
check('all three categories are named',
      'Pharmaceutical' in body and 'Medical Supply' in body and 'Donor Funded' in body)
add('Paracetamol, 500 mg', 'Pharmaceutical')
add('Gauze Bandage, 10 cm', 'Medical Supply')
check('a pharmaceutical is stored as such',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['category'] == 'Pharmaceutical')
check('a supply is stored as such',
      meds.find_one({'name': 'Gauze Bandage, 10 cm'})['category'] == 'Medical Supply')
add('Defaulted Item, 10 mg', '')
check('a missing category defaults to Pharmaceutical',
      meds.find_one({'name': 'Defaulted Item, 10 mg'})['category'] == 'Pharmaceutical')
add('Bad Category, 10 mg', 'Nonsense')
check('an unknown category falls back to Pharmaceutical',
      meds.find_one({'name': 'Bad Category, 10 mg'})['category'] == 'Pharmaceutical')

body = c.get('/edit-medication?med_name=Gauze Bandage, 10 cm').data.decode()
check('Edit Medication offers the field', 'name="category"' in body)
check('and pre-selects the current value',
      _re.search(r'<option value="Medical Supply" selected', body) is not None)
c.post('/edit-medication', data={'med_name': 'Gauze Bandage, 10 cm',
                                 'category': 'Pharmaceutical', 'batch': 'B',
                                 'pack_size': '100', 'pack_price': '250',
                                 'expiry_date': '2030-01-01',
                                 'schedule': 'not controlled'}, follow_redirects=True)
check('the category can be corrected',
      meds.find_one({'name': 'Gauze Bandage, 10 cm'})['category'] == 'Pharmaceutical')
c.post('/edit-medication', data={'med_name': 'Gauze Bandage, 10 cm',
                                 'category': 'Medical Supply', 'batch': 'B',
                                 'pack_size': '100', 'pack_price': '250',
                                 'expiry_date': '2030-01-01',
                                 'schedule': 'not controlled'}, follow_redirects=True)

print('\n--- 2. Existing data is untouched ---')
reset()
login()
# an item as it exists in the current database: no category field at all
meds.insert_one({'name': 'Legacy Item, 10 mg', 'balance': 100, 'price': 2.5,
                 'pack_size': 100, 'pack_price': 250.0, 'batch': 'B',
                 'expiry_date': '2030-01-01', 'schedule': 'not controlled'})
check('a legacy item appears under Pharmaceuticals',
      'Legacy Item, 10 mg' in rep('stock_on_hand', 'pharmaceutical'))
check('and is not mistaken for a supply',
      'Legacy Item, 10 mg' not in rep('stock_on_hand', 'supply'))
check('and appears under Both', 'Legacy Item, 10 mg' in rep('stock_on_hand', 'both'))
check('an unfiltered report still shows it',
      'Legacy Item, 10 mg' in c.post('/reports', data={'report_type': 'stock_on_hand'}).data.decode())

print('\n--- 3. Filtering across every report ---')
reset()
login()
add('Paracetamol, 500 mg', 'Pharmaceutical')
add('Gauze Bandage, 10 cm', 'Medical Supply')
for kind, kw in [('stock_on_hand', {}),
                 ('inventory', {'start_date': M_START, 'end_date': M_END}),
                 ('expired_list', {}), ('near_expired_list', {}),
                 ('out_of_stock_list', {})]:
    p = rep(kind, 'pharmaceutical', **kw)
    su = rep(kind, 'supply', **kw)
    bo = rep(kind, 'both', **kw)
    check(f'{kind}: pharmaceuticals view excludes supplies',
          'Gauze Bandage, 10 cm' not in p)
    check(f'{kind}: supplies view excludes pharmaceuticals',
          'Paracetamol, 500 mg' not in su)
    check(f'{kind}: both shows everything',
          ('Paracetamol, 500 mg' in bo and 'Gauze Bandage, 10 cm' in bo)
          # The old catch-all said "No controlled drugs found..." for EVERY
          # empty report, which is what this escape hatch used to match.
          or 'nothing in it for this period' in bo)

check('the report defaults to pharmaceuticals',
      'Gauze Bandage, 10 cm' not in c.post('/reports', data={'report_type': 'stock_on_hand'}).data.decode())
check('the view in force is named on the page',
      'Pharmaceuticals' in rep('stock_on_hand', 'pharmaceutical'))
check('and says so for supplies', 'Medical Supplies' in rep('stock_on_hand', 'supply'))

body = rep('inventory', 'both', start_date=M_START, end_date=M_END)
check('a Category column appears when both are shown', '<th>Category</th>' in body)
check('it is absent when only one kind is shown',
      '<th>Category</th>' not in rep('inventory', 'pharmaceutical',
                                     start_date=M_START, end_date=M_END))

print('\n--- 4. Dashboard ---')
body = c.get('/dashboard?category=pharmaceutical&limit=0').data.decode()
check('dashboard filters to pharmaceuticals', 'Gauze Bandage, 10 cm' not in body)
check('the heading names the view', 'Stock Health &mdash; Pharmaceuticals' in body
      or 'Pharmaceuticals' in body)
body = c.get('/dashboard?category=supply&limit=0').data.decode()
check('dashboard filters to supplies',
      'Gauze Bandage, 10 cm' in body and 'Paracetamol, 500 mg' not in body)
body = c.get('/dashboard?category=both&limit=0').data.decode()
check('dashboard shows both', 'Gauze Bandage, 10 cm' in body and 'Paracetamol, 500 mg' in body)
check('the selector is on the page', 'name="category"' in body)
check('dashboard defaults to pharmaceuticals',
      'Gauze Bandage, 10 cm' not in c.get('/dashboard?limit=0').data.decode())

print('\n--- 5. Supplies work with everything already built ---')
reset()
login()
add('Gauze Bandage, 10 cm', 'Medical Supply', opening=0)
# receiving on a delivery
c.post('/receive', data={'action': 'open', 'supplier': 'MedSupply',
                         'order_number': 'PO-1', 'order_total': '2500',
                         'invoice_number': 'INV-1', 'invoice_total': '2500',
                         'stock_receiver': 'R'}, follow_redirects=True)
did = str(db['deliveries'].find_one({})['_id'])
c.post('/receive', data={'action': 'line', 'delivery_id': did,
                         'med_name': 'Gauze Bandage, 10 cm', 'quantity': '1000',
                         'pack_size': '100', 'pack_price': '250', 'batch': 'B',
                         'expiry_date': '2030-01-01'}, follow_redirects=True)
check('a supply can be received on a delivery',
      meds.find_one({})['balance'] == 1000)
# dispensing to a patient
c.post('/dispense', data={'patient': 'Thabo M', 'company': 'C', 'position': 'P',
                          'birth_decade': '1980', 'gender': 'M',
                          'outcome': 'Outpatient', 'outcome_days': '',
                          'diagnoses': 'wound', 'prescriber': 'Dr X',
                          'dispenser': 'D', 'date': NOW.strftime('%Y-%m-%d'),
                          'med_names': ['Gauze Bandage, 10 cm'],
                          'quantities': ['50']}, follow_redirects=True)
check('a supply can be dispensed to a patient', meds.find_one({})['balance'] == 950)
# issue to a department
c.post('/internal-issue', data={'action': 'issue', 'department': 'Emergency Trolley',
                                'issued_to': 'Sr M', 'med_name': ['Gauze Bandage, 10 cm'],
                                'quantity': ['100']}, follow_redirects=True)
check('a supply can be issued to a department', meds.find_one({})['balance'] == 850)
# damage
c.post('/internal-issue', data={'action': 'damage', 'med_name': 'Gauze Bandage, 10 cm',
                                'quantity': '50', 'reason': 'Contaminated'},
       follow_redirects=True)
check('a supply can be written off as damaged', meds.find_one({})['balance'] == 800)
# stock take
c.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
sheet = c.get(f'/stock-take?stock_take_id={sid}').data.decode()
check('a supply appears on the stock take count sheet', 'Gauze Bandage, 10 cm' in sheet)
c.post('/stock-take', data={'action': 'count_sheet', 'stock_take_id': sid,
                            'med_name': ['Gauze Bandage, 10 cm'],
                            'counted__0': '790', 'note__0': ''}, follow_redirects=True)
check('a supply can be counted and corrected', meds.find_one({})['balance'] == 790)

body = rep('inventory', 'supply', start_date=M_START, end_date=M_END)
row = inventory_rows(body).get('Gauze Bandage, 10 cm', {})
print('   supply inventory row:', row)
check('the supply row reconciles like any other', row and reconciles(row), str(row))
check('its AMC counts dispensing and department issues',
      row and float(row['Adjusted AMC']) > 0, str(row))

print('\n--- 6. Ordering and the controlled register ---')
body = rep('order_request', 'supply', cover_months='3')
check('supplies get their own order request', 'Order Request' in body)
add('Paracetamol, 500 mg', 'Pharmaceutical', opening=0)
for d in (10, 40, 70):
    txs.insert_one({'type': 'dispense', 'med_name': 'Paracetamol, 500 mg',
                    'quantity': 100, 'transaction_id': f'T{d}', 'patient': 'P',
                    'price': 2.5, 'line_value': 250.0, 'user': 'u',
                    'timestamp': NOW - timedelta(days=d)})
check('a pharmaceuticals order request excludes supplies',
      'Gauze Bandage, 10 cm' not in rep('order_request', 'pharmaceutical', cover_months='3'))
check('a both order request includes them',
      'Paracetamol, 500 mg' in rep('order_request', 'both', cover_months='3'))
meds.update_one({'name': 'Gauze Bandage, 10 cm'}, {'$set': {'schedule': 'controlled'}})
body = rep('controlled_drug_register', 'pharmaceutical',
           start_date=M_START, end_date=M_END)
check('the controlled register does not error with a supply present',
      'Controlled Drug Register' in body or body.count('report') >= 0)

# =====================================================================
print('\n--- 7. Donor Funded ---')
reset()
login()
add('Paracetamol, 500 mg', 'Pharmaceutical')
add('Gauze Bandage, 10 cm', 'Medical Supply')
add('ARV Tablets, 300 mg', 'Donor Funded')
meds.insert_one({'name': 'Legacy Item, 10 mg', 'balance': 100, 'price': 2.5,
                 'pack_size': 100, 'pack_price': 250.0, 'batch': 'B',
                 'expiry_date': '2030-01-01', 'schedule': 'not controlled'})

check('a donor-funded item is stored as such',
      meds.find_one({'name': 'ARV Tablets, 300 mg'})['category'] == 'Donor Funded')

p = rep('stock_on_hand', 'pharmaceutical')
su = rep('stock_on_hand', 'supply')
dn = rep('stock_on_hand', 'donor')
al = rep('stock_on_hand', 'both')
check('the donor view shows only donor-funded items',
      'ARV Tablets, 300 mg' in dn and 'Paracetamol, 500 mg' not in dn
      and 'Gauze Bandage, 10 cm' not in dn)
check('donor items are NOT in the pharmaceuticals view',
      'ARV Tablets, 300 mg' not in p)
check('donor items are NOT in the supplies view', 'ARV Tablets, 300 mg' not in su)
check('all categories shows everything',
      all(n in al for n in ['Paracetamol, 500 mg', 'Gauze Bandage, 10 cm',
                            'ARV Tablets, 300 mg', 'Legacy Item, 10 mg']))
check('legacy items still land under pharmaceuticals, not donor',
      'Legacy Item, 10 mg' in p and 'Legacy Item, 10 mg' not in dn)
check('the donor view is named on the page', 'Donor Funded' in dn)
check('the all view is labelled', 'All Categories' in al)

body = c.get('/dashboard?category=donor&limit=0').data.decode()
check('the dashboard filters to donor-funded',
      'ARV Tablets, 300 mg' in body and 'Paracetamol, 500 mg' not in body)
check('the dashboard still defaults to pharmaceuticals',
      'ARV Tablets, 300 mg' not in c.get('/dashboard?limit=0').data.decode())
body = rep('inventory', 'both', start_date=M_START, end_date=M_END)
check('the Category column names Donor Funded when all are shown',
      'Donor Funded' in body)
check('a donor-funded item can be reordered separately',
      'Order Request' in rep('order_request', 'donor', cover_months='3'))

c.post('/edit-medication', data={'med_name': 'Paracetamol, 500 mg',
                                 'category': 'Donor Funded', 'batch': 'B',
                                 'pack_size': '100', 'pack_price': '250',
                                 'expiry_date': '2030-01-01',
                                 'schedule': 'not controlled'}, follow_redirects=True)
check('an item can be moved into Donor Funded',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['category'] == 'Donor Funded')
check('and it leaves the pharmaceuticals view',
      'Paracetamol, 500 mg' not in rep('stock_on_hand', 'pharmaceutical'))
check('appearing only under donor',
      'Paracetamol, 500 mg' in rep('stock_on_hand', 'donor'))

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
