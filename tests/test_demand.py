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
    for col in ['medications', 'transactions', 'deliveries', 'audit_log', 'app_warnings']:
        db[col].delete_many({})


TODAY = datetime.utcnow()
M_START = TODAY.strftime('%Y-%m-01')
M_END = TODAY.strftime('%Y-%m-%d')


def add(name, balance, price=2.5):
    meds.insert_one({'name': name, 'balance': balance, 'batch': 'B', 'price': price,
                     'pack_size': 100, 'pack_price': price * 100,
                     'expiry_date': '2030-01-01', 'schedule': 'not controlled',
                     'supplier': 'MedSupply'})


def dispense(patient, pairs):
    return c.post('/dispense', data={
        'patient': patient, 'company': 'C', 'position': 'P', 'age_group': 'adult',
        'gender': 'M', 'sick_leave_days': '0', 'diagnoses': 'flu',
        'prescriber': 'Dr X', 'dispenser': 'D', 'date': TODAY.strftime('%Y-%m-%d'),
        'med_names': [p[0] for p in pairs],
        'quantities': [str(p[1]) for p in pairs]}, follow_redirects=True)


def cells(body, name):
    mm = _re.search(r'<td>' + _re.escape(name) + r'</td>(.*?)</tr>', body, _re.S)
    return [_re.sub(r'<[^>]+>', '', x).strip()
            for x in _re.findall(r'<td>(.*?)</td>', mm.group(1), _re.S)] if mm else []


print('\n--- 1. A short line now dispenses what IS there ---')
reset()
login()
add('Paracetamol, 500 mg', 20)
r = dispense('Thabo M', [('Paracetamol, 500 mg', 30)])
check('the available units were dispensed',
      txs.count_documents({'type': 'dispense'}) == 1)
check('for the quantity actually on the shelf',
      txs.find_one({'type': 'dispense'})['quantity'] == 20)
check('the balance is now zero', meds.find_one({})['balance'] == 0)
check('the balance never goes negative', meds.find_one({})['balance'] >= 0)
u = txs.find_one({'type': 'unmet_demand'})
check('the shortfall is recorded as its own transaction', u is not None)
check('requested 30', u['requested'] == 30)
check('given 20', u['given'] == 20)
check('short 10', u['shortfall'] == 10)
check('valued at cost', u['shortfall_value'] == 25.0, str(u['shortfall_value']))
check('the patient and prescriber are kept',
      u['patient'] == 'Thabo M' and u['prescriber'] == 'Dr X')
check('the user is told what happened',
      'only 20 of 30 available' in r.data.decode())
check('a warning is still logged', db['app_warnings'].count_documents({}) >= 1)

print('\n--- 2. It moves no stock in any calculation ---')
check('the unmet record carries quantity 0', u['quantity'] == 0, str(u['quantity']))
body = c.post('/reports', data={'report_type': 'stock_on_hand',
                                'end_date': M_END}).data.decode()
mm = _re.search(r'<td>Paracetamol, 500 mg</td>\s*<td>(\d+)</td>', body)
check('stock on hand is unaffected by unmet demand', mm and mm.group(1) == '0',
      mm.group(1) if mm else '?')

print('\n--- 3. Nothing on the shelf at all ---')
reset()
login()
add('Paracetamol, 500 mg', 0)
r = dispense('Palesa N', [('Paracetamol, 500 mg', 50)])
check('no dispense transaction is written', txs.count_documents({'type': 'dispense'}) == 0)
check('the whole request is recorded as unmet',
      txs.find_one({'type': 'unmet_demand'})['shortfall'] == 50)
check('balance stays at zero', meds.find_one({})['balance'] == 0)

print('\n--- 4. A full line records nothing unmet ---')
reset()
login()
add('Paracetamol, 500 mg', 100)
dispense('Thabo M', [('Paracetamol, 500 mg', 40)])
check('a filled request writes no unmet record',
      txs.count_documents({'type': 'unmet_demand'}) == 0)
check('stock reduced normally', meds.find_one({})['balance'] == 60)

print('\n--- 5. Mixed lines in one visit ---')
reset()
login()
add('Paracetamol, 500 mg', 100)
add('Aspirin, 100 mg', 5)
dispense('Thabo M', [('Paracetamol, 500 mg', 50), ('Aspirin, 100 mg', 20)])
check('the fillable line is dispensed in full',
      txs.find_one({'type': 'dispense', 'med_name': 'Paracetamol, 500 mg'})['quantity'] == 50)
check('the short line is dispensed partially',
      txs.find_one({'type': 'dispense', 'med_name': 'Aspirin, 100 mg'})['quantity'] == 5)
check('only the short line records unmet demand',
      txs.count_documents({'type': 'unmet_demand'}) == 1)
check('both lines share the visit',
      len({t['transaction_id'] for t in txs.find({'type': 'dispense'})}) == 1)

print('\n--- 6. Inventory report orders from demand, in whole numbers ---')
# The inventory table used to show AMC, Unmet, Adjusted AMC and Fill Rate side
# by side. It now shows Adjusted AMC alone - the one you order from - so what
# matters is that it still carries the demand that went unmet, and that it is
# a whole number. Unmet demand and fill rate are in their own report.
reset()
login()
add('Paracetamol, 500 mg', 100)
dispense('A', [('Paracetamol, 500 mg', 100)])   # dispenses 100, balance 0
dispense('B', [('Paracetamol, 500 mg', 50)])    # all 50 unmet
body = c.post('/reports', data={'report_type': 'inventory',
                                'start_date': M_START, 'end_date': M_END}).data.decode()
row = inventory_rows(body).get('Paracetamol, 500 mg', {})
print('   inventory row:', row)
_days = (datetime.strptime(M_END, '%Y-%m-%d')
         - datetime.strptime(M_START, '%Y-%m-%d')).days + 1
_plain = 100 / _days * 30
_want = int(150 / _days * 30 + 0.5)
check('only the Adjusted AMC is shown',
      '<th>Adjusted AMC</th>' in body and '<th>AMC</th>' not in body
      and '<th>Unmet</th>' not in body and '<th>Fill Rate</th>' not in body)
check('it is a whole number', row.get('Adjusted AMC', '').isdigit(), str(row))
check('it counts the 50 that could not be given, not just the 100 that were',
      row and int(row['Adjusted AMC']) == _want and _want > _plain,
      f"{row.get('Adjusted AMC')} vs expected {_want} (plain would be {_plain:.1f})")
check('Amount to Order is three months of it, less the empty shelf',
      row and int(row['Amount to Order']) == 3 * _want, str(row))
check('and the row still adds up', row and reconciles(row), str(row))

print('\n--- 7. The order request buys to demand, not consumption ---')
reset()
login()
add('Paracetamol, 500 mg', 0)
for d in (10, 40, 70):
    txs.insert_one({'type': 'dispense', 'med_name': 'Paracetamol, 500 mg', 'quantity': 100,
                    'transaction_id': f'T{d}', 'patient': 'P', 'user': 'u',
                    'price': 2.5, 'line_value': 250.0,
                    'timestamp': TODAY - timedelta(days=d)})
    txs.insert_one({'type': 'unmet_demand', 'med_name': 'Paracetamol, 500 mg',
                    'quantity': 0, 'requested': 150, 'given': 100, 'shortfall': 50,
                    'price': 2.5, 'shortfall_value': 125.0, 'patient': 'P',
                    'user': 'u', 'timestamp': TODAY - timedelta(days=d)})
body = c.post('/reports', data={'report_type': 'order_request',
                                'cover_months': '3'}).data.decode()
l = cells(body, 'Paracetamol, 500 mg')
print('   order line:', l)
check('consumption AMC is 100', l[0] == '100.0', l[0])
check('unmet is shown per month, matching the AMC time base',
      l[1] == '50.0', l[1])
check('the AMC used is 150, not 100', l[2] == '150.0', l[2])
check('it orders 5 packs (3 x 150 = 450) not 3',
      l[7] == '5', f'{l[7]} (row {l})')
check('the adjustment is disclosed', 'item(s) below were adjusted upward' in body)
check('the death-spiral reasoning is stated',
      'how a stockout becomes permanent' in body)

print('\n--- 8. Dashboard fill rate ---')
reset()
login()
add('Paracetamol, 500 mg', 100)
dispense('A', [('Paracetamol, 500 mg', 100)])
dispense('B', [('Paracetamol, 500 mg', 100)])
body = c.get('/dashboard?limit=0').data.decode()
check('a fill rate tile is shown', 'Fill rate' in body)
check('it reads 50.0%', '50.0%' in body, )
check('items that ran short are counted', 'Items that ran short' in body)
check('a poor fill rate is flagged red', '#f8d7da' in body)
r = cells(body, 'Paracetamol, 500 mg')
check('the item row shows its unmet units', '100' in r, str(r))
check('the dashboard explains the understating', 'understates what people actually wanted' in body)

print('\n--- 9. Unmet demand report ---')
reset()
login()
add('Paracetamol, 500 mg', 20)
add('Aspirin, 100 mg', 0, price=5.0)
dispense('Thabo M', [('Paracetamol, 500 mg', 30)])
dispense('Palesa N', [('Aspirin, 100 mg', 4)])
body = c.post('/reports', data={'report_type': 'unmet_demand',
                                'start_date': M_START, 'end_date': M_END}).data.decode()
check('the report renders', 'Unmet Demand for' in body)
check('both occasions are listed',
      'Thabo M' in body and 'Palesa N' in body)
check('requested and given are both shown', '<td>30</td>' in body and '<td>20</td>' in body)
seg = body[body.index('By Medication'):]
check('per-medication fill rate is shown', '%' in seg)
# Paracetamol short 10, Aspirin short 4 — ranking must follow units short
check('items are ranked by units short',
      seg.index('Paracetamol, 500 mg') < seg.index('Aspirin, 100 mg'))
check('value short is priced', 'R50.00' in body or 'R25.00' in body)
check('a filled period says so',
      'Every request in this period was filled in full' in
      c.post('/reports', data={'report_type': 'unmet_demand',
                               'start_date': '2020-01-01',
                               'end_date': '2020-01-31'}).data.decode())
check('missing dates do not crash',
      c.post('/reports', data={'report_type': 'unmet_demand'}).status_code == 200)
check('search narrows by medication',
      'Aspirin, 100 mg' in c.post('/reports', data={
          'report_type': 'unmet_demand', 'start_date': M_START,
          'end_date': M_END, 'search': 'Aspirin'}).data.decode())

print('\n--- 10. Visible to everyone ---')
login('user', 'Pharm')
ubody = c.post('/reports', data={'report_type': 'unmet_demand',
                                 'start_date': M_START, 'end_date': M_END}).data.decode()
check('a non-admin can run the unmet demand report', 'Unmet Demand for' in ubody)
dash = c.get('/dashboard').data.decode()
check('a non-admin sees the fill rate tile', 'Fill rate' in dash)
inv = c.post('/reports', data={'report_type': 'inventory',
                               'start_date': M_START, 'end_date': M_END}).data.decode()
check('a non-admin sees the Adjusted AMC, which already carries unmet demand',
      '<th>Adjusted AMC</th>' in inv)
check('but still no adjustment or expired columns',
      '<th>Adjustment</th>' not in inv and '<th>Expired</th>' not in inv)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
