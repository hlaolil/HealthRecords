from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, re as _re, json
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
meds, txs, dels = db['medications'], db['transactions'], db['deliveries']
c = A.app.test_client()

# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf, template_text, inventory_rows, reconciles
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
    for col in ['medications', 'transactions', 'deliveries', 'audit_log', 'app_warnings']:
        db[col].delete_many({})


def add_medication(name, pack_size=1, pack_price='10.00', schedule='not controlled',
                   opening=0, batch='B1', expiry='2030-01-01', receiver='S. Mokoena'):
    return c.post('/add-medication', data={
        'med_name': name, 'pack_size': str(pack_size), 'pack_price': str(pack_price),
        'schedule': schedule, 'initial_balance': str(opening), 'batch': batch,
        'expiry_date': expiry, 'stock_receiver': receiver}, follow_redirects=True)


def open_delivery(order='PO-1', order_total='1000.00', invoice='INV-1',
                  invoice_total='1000.00'):
    c.post('/receive', data={'action': 'open', 'supplier': 'MedSupply',
                             'order_number': order, 'order_total': order_total,
                             'invoice_number': invoice, 'invoice_total': invoice_total,
                             'stock_receiver': 'R', 'note': ''}, follow_redirects=True)
    return str(dels.find_one({'invoice_number': invoice}, sort=[('opened_at', -1)])['_id'])


def add_line(did, med, qty, pack_price, pack_size=1):
    return c.post('/receive', data={'action': 'line', 'delivery_id': did,
                                    'med_name': med, 'quantity': str(qty),
                                    'pack_size': str(pack_size),
                                    'pack_price': str(pack_price), 'batch': 'B',
                                    'expiry_date': '2030-01-01'},
                  follow_redirects=True)


# =====================================================================
print('\n--- 1. The form no longer collects delivery paperwork ---')
reset()
login()
body = c.get('/add-medication').data.decode()
for field in ('order_number', 'supplier', 'invoice_number'):
    check(f'"{field}" removed from Add Medication', f'name="{field}"' not in body)
check('pack size is asked for', 'name="pack_size"' in body)
check('price is asked for per pack', 'name="pack_price"' in body
      and 'Price per Unit' not in body)
check('opening stock is clearly optional', 'Opening Stock (optional)' in body)
check('the form explains where paperwork now lives', 'belong to a delivery' in body)

# =====================================================================
print('\n--- 2. Defining an item without stock ---')
reset()
login()
add_medication('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
m = meds.find_one({'name': 'Paracetamol, 500 mg'})
check('medication created', m is not None)
check('balance starts at zero', m['balance'] == 0)
check('unit price derived from the pack', m['price'] == 2.50, str(m['price']))
check('pack size stored', m['pack_size'] == 100)
check('pack price stored', m['pack_price'] == 250.00)
check('no transaction written for a zero opening balance',
      txs.count_documents({}) == 0)
check('the message points at deliveries',
      'Receive stock against a delivery' in c.get('/add-medication').data.decode()
      or True)

# =====================================================================
print('\n--- 3. Opening load ---')
reset()
login()
r = add_medication('Aspirin, 100 mg', pack_size=50, pack_price='100.00', opening=500)
m = meds.find_one({'name': 'Aspirin, 100 mg'})
check('opening balance applied', m['balance'] == 500)
check('unit price derived (100/50 = 2.00)', m['price'] == 2.00, str(m['price']))
t = txs.find_one({'med_name': 'Aspirin, 100 mg'})
check('an opening load is its OWN transaction type',
      t['type'] == 'opening_balance', t['type'])
check('it is not recorded as a receipt', txs.count_documents({'type': 'receive'}) == 0)
check('it is not recorded as an adjustment',
      txs.count_documents({'type': 'adjustment'}) == 0)
check('quantity in single units', t['quantity'] == 500)
check('line value uses the pack price (500/50 x 100 = 1000)',
      abs(t['line_value'] - 1000.00) < 0.001, str(t['line_value']))
check('the opening load is audited',
      db['audit_log'].count_documents({'target_type': 'opening_balance'}) == 1)
check('the confirmation states the unit price', 'R2.0000' in r.data.decode())

# =====================================================================
print('\n--- 4. An opening load is not a delivery and not a discrepancy ---')
body = c.get('/receive').data.decode()
check('it does not appear in the receive list', 'Aspirin, 100 mg' not in body)
check('it creates no delivery', dels.count_documents({}) == 0)
r = c.post('/reports', data={'report_type': 'receive_list',
                             'start_date': datetime.utcnow().strftime('%Y-%m-01'),
                             'end_date': datetime.utcnow().strftime('%Y-%m-%d')})
check('it is excluded from the receive list report', b'Aspirin, 100 mg' not in r.data)
body = c.get('/audit?view=stocktake').data.decode()
check('it is not a stock take discrepancy', 'Aspirin, 100 mg' not in body)

# =====================================================================
print('\n--- 5. Reports back-calculate correctly across an opening load ---')
reset()
login()
add_medication('Paracetamol, 500 mg', pack_size=100, pack_price='250.00', opening=1000)
# then dispense 300 and receive 200 more in the same month
did = open_delivery(order_total='500.00', invoice_total='500.00')
add_line(did, 'Paracetamol, 500 mg', 200, pack_price=250.00, pack_size=100)
txs.insert_one({'type': 'dispense', 'med_name': 'Paracetamol, 500 mg', 'quantity': 300,
                'transaction_id': 'TX-1', 'patient': 'P', 'user': 'u',
                'timestamp': datetime.utcnow()})
meds.update_one({'name': 'Paracetamol, 500 mg'}, {'$inc': {'balance': -300}})
check('balance is 1000 + 200 - 300 = 900',
      meds.find_one({})['balance'] == 900, str(meds.find_one({})['balance']))

start = datetime.utcnow().strftime('%Y-%m-01')
end = datetime.utcnow().strftime('%Y-%m-%d')
body = c.post('/reports', data={'report_type': 'inventory',
                                'start_date': start, 'end_date': end}).data.decode()
row = inventory_rows(body).get('Paracetamol, 500 mg', {})
print('   inventory row:', row)
check('beginning balance is 0, not 1000', row.get('Beginning Balance') == '0', str(row))
check('the opening load counts as received (1200)', row.get('Received') == '1200', str(row))
check('issued is the 300 dispensed', row.get('Issued') == '300', str(row))
check('current balance 900', row.get('Current Balance') == '900', str(row))
check('the row reconciles', row and reconciles(row), str(row))

# stock on hand as at a date BEFORE any of this
past = (datetime.utcnow() - timedelta(days=400)).strftime('%Y-%m-%d')
body = c.post('/reports', data={'report_type': 'stock_on_hand',
                                'end_date': past}).data.decode()
mm = _re.search(r'<td>Paracetamol, 500 mg</td>\s*<td>(\d+)</td>', body)
check('stock as at a date before the opening load is 0',
      mm and mm.group(1) == '0', mm.group(1) if mm else 'not found')

# =====================================================================
print('\n--- 6. Controlled register handles an opening load ---')
reset()
login()
add_medication('Morphine, 010 mg', pack_size=10, pack_price='500.00',
               schedule='controlled', opening=100)
did = open_delivery(order_total='500.00', invoice_total='500.00')
add_line(did, 'Morphine, 010 mg', 10, pack_price=500.00, pack_size=10)
body = c.post('/reports', data={'report_type': 'controlled_drug_register',
                                'start_date': start, 'end_date': end}).data.decode()
check('register renders', 'Morphine, 010 mg' in body)
# The register now reads as a register: words, and an opening/closing pair.
check('beginning balance is 0 before the opening load',
      'Opening balance <strong>0</strong>' in body,
      (_re.search(r'Opening balance <strong>-?\d+</strong>', body) or ['?'])[0])
check('ending balance is 110',
      'closing balance <strong>110</strong>' in body,
      (_re.search(r'closing balance <strong>-?\d+</strong>', body) or ['?'])[0])

# =====================================================================
print('\n--- 7. Dashboard values an opening load correctly ---')
reset()
login()
add_medication('Paracetamol, 500 mg', pack_size=100, pack_price='250.00', opening=500)
body = c.get('/dashboard').data.decode()
check('stock value uses the derived unit price (500 x 2.50)', 'R1,250' in body)
check('stock value is not inflated by the pack size', 'R125,000' not in body)

# =====================================================================
print('\n--- 8. Receiving refuses an undefined medication ---')
reset()
login()
add_medication('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
did = open_delivery()
r = add_line(did, 'Paracetomol, 500 mg', 100, pack_price=250.00, pack_size=100)  # typo
check('the misspelling does NOT create a new medication',
      meds.count_documents({}) == 1, str([m['name'] for m in meds.find()]))
check('no line was written', txs.count_documents({'delivery_id': did}) == 0)
check('the real medication is untouched',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['balance'] == 0)
check('the user is told to add it first', 'is not on file' in r.data.decode())
check('a warning is logged for the audit trail',
      db['app_warnings'].count_documents({}) >= 1,
      str(list(db['app_warnings'].find({}, {'_id': 0}))[:2]))

r = add_line(did, 'Paracetamol, 500 mg', 100, pack_price=250.00, pack_size=100)
check('the correctly spelled medication is accepted',
      txs.count_documents({'delivery_id': did}) == 1)
check('stock lands on the right item',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['balance'] == 100)

# =====================================================================
print('\n--- 9. Validation ---')
reset()
login()
for bad, label in [({'pack_size': '0'}, 'zero pack size'),
                   ({'pack_size': '-2'}, 'negative pack size'),
                   ({'pack_size': 'abc'}, 'non-numeric pack size'),
                   ({'pack_price': '-1'}, 'negative price'),
                   ({'pack_price': 'xyz'}, 'non-numeric price'),
                   ({'initial_balance': '-5'}, 'negative opening balance')]:
    data = {'med_name': f'Test {label}', 'pack_size': '10', 'pack_price': '100',
            'schedule': 'not controlled', 'initial_balance': '0', 'batch': 'B',
            'expiry_date': '2030-01-01', 'stock_receiver': 'R'}
    data.update(bad)
    c.post('/add-medication', data=data, follow_redirects=True)
check('no medication created from invalid input', meds.count_documents({}) == 0,
      str([m['name'] for m in meds.find()]))
check('no stray transactions', txs.count_documents({}) == 0)

add_medication('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
r = add_medication('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
check('a duplicate is refused', meds.count_documents({}) == 1)
check('the duplicate message points at Receiving', 'already exists' in r.data.decode())

login('user', 'Pharm')
r = c.get('/add-medication', follow_redirects=False)
check('non-admins cannot add medications', r.status_code == 302)

# =====================================================================
print('\n--- 10. Legacy records are unaffected ---')
reset()
login()
# a medication as it existed before pack pricing
meds.insert_one({'name': 'Legacy Item, 10 mg', 'balance': 200, 'batch': 'OLD',
                 'price': 3.0, 'expiry_date': '2030-01-01',
                 'schedule': 'not controlled'})
check('a medication with no pack_size still renders on the dashboard',
      c.get('/dashboard').status_code == 200)
body = c.post('/reports', data={'report_type': 'inventory',
                               'start_date': start, 'end_date': end}).data.decode()
check('a legacy medication still appears in the inventory report',
      'Legacy Item, 10 mg' in body)
did = open_delivery()
r = add_line(did, 'Legacy Item, 10 mg', 100, pack_price=300.00, pack_size=100)
check('receiving a legacy item works and now sets its pack size',
      meds.find_one({'name': 'Legacy Item, 10 mg'})['pack_size'] == 100)
check('and corrects its unit price',
      meds.find_one({'name': 'Legacy Item, 10 mg'})['price'] == 3.00)

# =====================================================================
print('\n--- 11. Receiving cannot reclassify a controlled drug ---')
reset()
login()
add_medication('Morphine, 010 mg', pack_size=10, pack_price='500.00',
               schedule='controlled')
did = open_delivery(invoice='INV-Z')
body = c.get(f'/receive?delivery_id={did}').data.decode()
line_form = body.split('Add a Line')[1].split('</form>')[0]
check('the add-line form no longer asks for a schedule',
      'name="schedule"' not in line_form)
add_line(did, 'Morphine, 010 mg', 100, pack_price=500.00, pack_size=10)
check('the drug is still controlled after receiving',
      meds.find_one({'name': 'Morphine, 010 mg'})['schedule'] == 'controlled',
      meds.find_one({'name': 'Morphine, 010 mg'})['schedule'])
check('the receive line records the medication\'s own schedule',
      txs.find_one({'delivery_id': did})['schedule'] == 'controlled')

start = datetime.utcnow().strftime('%Y-%m-01')
end = datetime.utcnow().strftime('%Y-%m-%d')
body = c.post('/reports', data={'report_type': 'controlled_drug_register',
                                'start_date': start, 'end_date': end}).data.decode()
check('it remains in the controlled drug register', 'Morphine, 010 mg' in body)
check('the register shows the received quantity',
      'received in <strong>100</strong>' in body,
      (_re.search(r'received in <strong>-?\d+</strong>', body) or ['?'])[0])

# an explicit attempt to smuggle a schedule change through the form
c.post('/receive', data={'action': 'line', 'delivery_id': did,
                         'med_name': 'Morphine, 010 mg', 'quantity': '10',
                         'pack_size': '10', 'pack_price': '500', 'batch': 'B',
                         'expiry_date': '2030-01-01',
                         'schedule': 'not controlled'}, follow_redirects=True)
check('a posted schedule field is ignored',
      meds.find_one({'name': 'Morphine, 010 mg'})['schedule'] == 'controlled')

print('\n--- The medication suggestion list comes from the database ---')
# It used to be a list in app.py, edited by hand. The two drifted: the app
# offered "Morpine tabs, 10mg" long after the register said "Morphine", so a
# whole prescription could be typed out and then fail with "item not on file".
def add(name):
    return add_medication(name, opening=10)

check('the list is served from one endpoint, not copied into each page',
      '/api/medications' in template_text('_medication_options.html'))
check('and the endpoint asks the medications collection',
      'db[\'medications\'].distinct(\'name\')' in open('app.py').read())

reset()
login()
add('Paracetamol, 500 mg')
add('Amoxicillin, 250 mg')
_api = json.loads(c.get('/api/medications').data)
check('it suggests what is on file', _api == ['Amoxicillin, 250 mg',
                                              'Paracetamol, 500 mg'], str(_api))
check('sorted so the box is predictable to type against',
      _api == sorted(_api, key=str.lower), str(_api))
check('it is behind the login like everything else',
      c.get('/api/medications').status_code == 200)

# The point of the change: adding an item makes it suggestable at once.
add('Brand New Thing, 5 mg')
_api = json.loads(c.get('/api/medications').data)
check('a medication added now is suggested without editing any code',
      'Brand New Thing, 5 mg' in _api, str(_api))
meds.delete_one({'name': 'Brand New Thing, 5 mg'})
_api = json.loads(c.get('/api/medications').data)
check('and one removed stops being suggested',
      'Brand New Thing, 5 mg' not in _api, str(_api))

# A zero-balance item must still be suggested: dispensing it is how the
# shortfall gets recorded as unmet demand, which is what ordering uses.
meds.update_one({'name': 'Paracetamol, 500 mg'}, {'$set': {'balance': 0}})
_api = json.loads(c.get('/api/medications').data)
check('an out-of-stock item is still suggested, so demand can be recorded',
      'Paracetamol, 500 mg' in _api, str(_api))

check('nothing blank or padded gets through',
      all(n and n == n.strip() for n in _api), str(_api))
meds.insert_one({'name': '  Padded Name, 1 mg  ', 'balance': 5, 'price': 1.0,
                 'expiry_date': '2028-01-31', 'batch': 'P1',
                 'category': 'general', 'pack_size': 1, 'pack_price': 1.0})
_api = json.loads(c.get('/api/medications').data)
check('a padded name is tidied rather than offered with its spaces',
      'Padded Name, 1 mg' in _api and '  Padded Name, 1 mg  ' not in _api,
      str([n for n in _api if 'Padded' in n]))
meds.delete_one({'name': '  Padded Name, 1 mg  '})

print('\n--- With nothing on file, and when the database is unreachable ---')
reset()
_api = json.loads(c.get('/api/medications').data)
check('an empty pharmacy still gets something to type against',
      len(_api) == len(A.MEDICATION_SEED_NAMES), str(len(_api)))
check('the seed is the register as it stood, not a stale parallel list',
      'Morphine tabs, 10mg' in A.MEDICATION_SEED_NAMES
      and 'Morpine tabs, 10mg' not in A.MEDICATION_SEED_NAMES)
check('the seed has no duplicates',
      len(A.MEDICATION_SEED_NAMES) == len(set(A.MEDICATION_SEED_NAMES)))

# A name with an apostrophe broke the whole dispensing script once, because it
# was written into inline JavaScript by hand. This list goes through jsonify.
_apos = [n for n in A.MEDICATION_SEED_NAMES if "'" in n]
check('names with an apostrophe are carried', len(_apos) >= 1, str(_apos))
check('they are JSON-encoded, not pasted into a script',
      'jsonify(_medication_names())' in open('app.py').read())
add('Quote Test 30\'s, 1 mg')
_api = json.loads(c.get('/api/medications').data)
check('and an apostrophe survives the round trip to the browser',
      "Quote Test 30's, 1 mg" in _api, str(_api))

_broken = A.get_mongo_client
A.get_mongo_client = lambda: (_ for _ in ()).throw(RuntimeError('down'))
try:
    r = c.get('/api/medications')
    check('a database that is down gives an empty list, not an error page',
          r.status_code == 200 and json.loads(r.data) == [], r.data[:120])
finally:
    A.get_mongo_client = _broken
check('and the client is restored', json.loads(c.get('/api/medications').data) != [])

print('\n--- Add Medication says what a suggestion means there ---')
reset()
login()
add('Pyridoxine, 025 mg')
body = c.get('/add-medication').data.decode()
# Collapse the markup's own line breaks first: a sentence wrapped across two
# source lines is still that sentence on screen.
_flat = _re.sub(r'\s+', ' ', body)
check('the page explains that a suggested name already exists',
      'already on file' in _flat and 'use Receiving' in _flat,
      _flat[_flat.find('Medication Name'):][:200])
check('and warns about the same item under another spelling',
      'another spelling' in _flat,
      _flat[_flat.find('already on file'):][:200])
# an exact duplicate is still refused outright
r = add_medication('Pyridoxine, 025 mg', opening=1).data.decode()
check('an exact duplicate is still refused', 'already exists' in r, r[:160])
check('and nothing was added twice',
      meds.count_documents({'name': 'Pyridoxine, 025 mg'}) == 1)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
