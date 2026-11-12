"""Point of sale, receipts, voids, cash-up and the public product feed."""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, re
from datetime import datetime, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
db = _f['pharmacy_db']
meds, txs, sales = db['medications'], db['transactions'], db['sales']
c = A.app.test_client()
from test_support import arm_csrf
arm_csrf(c)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin', name='Letlotlo Hlaoli'):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_active'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in db.list_collection_names():
        db[col].delete_many({})
    meds.insert_many([
        {'name': 'Paracetamol 500mg tabs', 'balance': 100, 'price': 0.20, 'pack_size': 100,
         'selling_price': 0.50, 'rx_only': False, 'schedule': 'not controlled',
         'on_website': True, 'web_category': 'Over-the-counter medicines', 'description': 'Pain and fever'},
        {'name': 'Amlodipine 5mg tabs', 'balance': 60, 'price': 0.30, 'pack_size': 30,
         'selling_price': 1.10, 'rx_only': True, 'schedule': 'not controlled',
         'on_website': True, 'web_category': 'Prescription medicines'},
        {'name': 'Codeine 30mg tabs', 'balance': 20, 'price': 1.00, 'pack_size': 20,
         'selling_price': 3.00, 'rx_only': True, 'schedule': 'controlled', 'on_website': False},
        {'name': 'Vitamin C 1000mg', 'balance': 0, 'price': 2.00, 'pack_size': 10,
         'selling_price': 4.50, 'on_website': True, 'web_category': 'Supplements & cosmetics'},
        {'name': 'Unpriced cream', 'balance': 5, 'price': 10.0, 'pack_size': 1},
    ])


def sell(lines, **extra):
    data = {'med_names': [n for n, _ in lines], 'quantities': [str(q) for _, q in lines],
            'payment_method': 'Cash'}
    data.update(extra)
    return c.post('/sell', data=data)


def bal(name):
    return meds.find_one({'name': name})['balance']


print('\n--- 1. A cash sale ---')
reset(); login()
r = sell([('Paracetamol 500mg tabs', 10)], amount_tendered='10')
check('a sale redirects to its receipt', r.status_code == 302 and '/sales/HH-' in r.headers['Location'])
check('stock drops by what was sold', bal('Paracetamol 500mg tabs') == 90)
s = sales.find_one()
check('receipt total is selling price x quantity', s['total'] == 5.00, str(s.get('total')))
check('change is worked out', s['change'] == 5.00, str(s.get('change')))
check('cost is kept beside the sale price', s['cost_total'] == 2.00, str(s.get('cost_total')))
t = txs.find_one({'type': 'dispense'})
check('the line is an ordinary dispense, so stock reports keep working',
      t and t['quantity'] == 10 and t['transaction_id'] == s['transaction_id'])
check('the line carries cost and selling price apart',
      t['price'] == 0.20 and t['unit_sell_price'] == 0.50 and t['line_total'] == 5.00)
check('receipt numbers are HH-date-sequence', re.fullmatch(r'HH-\d{8}-0001', s['receipt_no']) is not None, s['receipt_no'])
page = c.get('/sales/' + s['receipt_no']).get_data(as_text=True)
check('the receipt page shows the item and total', 'Paracetamol 500mg tabs' in page and '5.00' in page)
r = sell([('Paracetamol 500mg tabs', 1)])
check('the next receipt number follows on', sales.find_one({}, sort=[('timestamp', -1)])['receipt_no'].endswith('-0002'))

print('\n--- 2. What a sale refuses ---')
reset(); login()
r = sell([('Paracetamol 500mg tabs', 200)])
check('more than is in stock is refused', r.status_code == 422 and bal('Paracetamol 500mg tabs') == 100)
check('and nothing at all is sold', sales.count_documents({}) == 0 and txs.count_documents({'type': 'dispense'}) == 0)
check('the shortfall is recorded as unmet demand', txs.count_documents({'type': 'unmet_demand'}) == 1)
r = sell([('Paracetamol 500mg tabs', 2), ('No such thing', 1)])
check('an unknown item stops the whole sale', r.status_code == 422 and bal('Paracetamol 500mg tabs') == 100)
r = sell([('Unpriced cream', 1)])
check('an item with no selling price is refused', r.status_code == 422 and 'selling price' in r.get_data(as_text=True))
r = sell([('Amlodipine 5mg tabs', 30)])
check('a prescription item needs a name and prescription number', r.status_code == 422 and bal('Amlodipine 5mg tabs') == 60)
r = sell([('Amlodipine 5mg tabs', 30)], customer='Mpho M', prescription_no='RX123')
check('with them it sells', r.status_code == 302 and bal('Amlodipine 5mg tabs') == 30)
r = sell([('Codeine 30mg tabs', 5)], customer='Thabo K', prescription_no='RX9')
check('a controlled item also needs the prescriber', r.status_code == 422 and bal('Codeine 30mg tabs') == 20)
r = sell([('Codeine 30mg tabs', 5)], customer='Thabo K', prescription_no='RX9', prescriber='Dr Lebona')
check('with the prescriber it sells', r.status_code == 302 and bal('Codeine 30mg tabs') == 15)
ctl = txs.find_one({'type': 'dispense', 'med_name': 'Codeine 30mg tabs'})
check('and the register line names patient and prescriber',
      ctl['patient'] == 'Thabo K' and ctl['prescriber'] == 'Dr Lebona' and ctl['prescription_no'] == 'RX9')
r = sell([('Paracetamol 500mg tabs', 2)], amount_tendered='0.50')
check('cash short of the total is refused', r.status_code == 422)
r = sell([('Paracetamol 500mg tabs', 2)], payment_method='Medical aid')
check('medical aid needs scheme and member number', r.status_code == 422)
r = sell([('Paracetamol 500mg tabs', 2)], payment_method='Medical aid', aid_scheme='LMPS', aid_member='123')
check('with them it sells', r.status_code == 302)
r = sell([('Paracetamol 500mg tabs', 0)])
check('a zero quantity is refused', r.status_code == 422)
r = sell([('Paracetamol 500mg tabs', 2), ('Paracetamol 500mg tabs', 3)])
last = sales.find_one({}, sort=[('timestamp', -1)])
check('the same item twice in one basket is one line', len(last['lines']) == 1 and last['lines'][0]['quantity'] == 5)

print('\n--- 3. Voiding ---')
reset(); login()
sell([('Paracetamol 500mg tabs', 10)])
rn = sales.find_one()['receipt_no']
login(role='employee', name='Cashier')
r = c.post('/sales/void', data={'receipt_no': rn, 'reason': 'mistake'})
check('a non-admin cannot void', sales.find_one()['status'] == 'completed' and bal('Paracetamol 500mg tabs') == 90)
login()
r = c.post('/sales/void', data={'receipt_no': rn, 'reason': ''})
check('a void needs a reason', sales.find_one()['status'] == 'completed')
r = c.post('/sales/void', data={'receipt_no': rn, 'reason': 'rung up twice'})
check('an admin void returns the stock', bal('Paracetamol 500mg tabs') == 100)
check('the receipt stays on file, marked voided', sales.find_one()['status'] == 'voided')
check('its stock lines are gone, so reports ignore it', txs.count_documents({'type': 'dispense'}) == 0)
c.post('/sales/void', data={'receipt_no': rn, 'reason': 'again'})
check('voiding twice does not return the stock twice', bal('Paracetamol 500mg tabs') == 100)

print('\n--- 4. Cash-up ---')
reset(); login()
sell([('Paracetamol 500mg tabs', 10)])
sell([('Paracetamol 500mg tabs', 4)], payment_method='M-Pesa')
page = c.get('/sales').get_data(as_text=True)
check("today's cash-up lists cash and M-Pesa", 'Cash' in page and 'M-Pesa' in page)
check('and the day total', '7.00' in page)
csv_body = c.get('/sales?export=csv&start_date=2000-01-01&end_date=2100-01-01').get_data(as_text=True)
check('the CSV has one row per line sold', csv_body.count('Paracetamol 500mg tabs') == 2)

print('\n--- 5. Public product feed ---')
reset()
with c.session_transaction() as s:
    s.clear()
r = c.get('/api/public/products')
check('the feed needs no login', r.status_code == 200)
d = r.get_json()
names = [p['name'] for p in d['products']]
check('only items ticked for the website appear', 'Codeine 30mg tabs' not in names and 'Unpriced cream' not in names
      and 'Paracetamol 500mg tabs' in names)
para = next(p for p in d['products'] if p['name'] == 'Paracetamol 500mg tabs')
amlo = next(p for p in d['products'] if p['name'] == 'Amlodipine 5mg tabs')
vitc = next(p for p in d['products'] if p['name'] == 'Vitamin C 1000mg')
check('an over-the-counter item shows its price', para['price'] == 0.50)
check('a prescription item never shows a price', amlo['price'] is None and amlo['prescription_only'] is True)
check('stock shows only as in / out', para['in_stock'] is True and vitc['in_stock'] is False and 'balance' not in para)
check('no cost price leaks out', all('cost' not in p and 'price_cost' not in p for p in d['products'])
      and '0.2' not in r.get_data(as_text=True))
check('the website may read it from another origin', r.headers.get('Access-Control-Allow-Origin') == '*')
check('and cache it for a few minutes', 'max-age=300' in (r.headers.get('Cache-Control') or ''))

print('\n--- 6. Setting prices ---')
reset(); login()
r = c.post('/edit-medication', data={'med_name': 'Unpriced cream', 'batch': 'B1', 'category': 'Pharmaceutical',
                                     'pack_size': '1', 'pack_price': '10', 'expiry_date': '2028-01-01',
                                     'schedule': 'not controlled', 'selling_price': '14.50',
                                     'on_website': 'on', 'web_category': 'Supplements & cosmetics',
                                     'description': 'Moisturising cream'})
m = meds.find_one({'name': 'Unpriced cream'})
check('editing a product saves the selling price and website fields',
      m.get('selling_price') == 14.5 and m.get('on_website') is True
      and m.get('web_category') == 'Supplements & cosmetics' and m.get('description') == 'Moisturising cream',
      str({k: m.get(k) for k in ('selling_price', 'on_website', 'web_category')}))
check('the edit page shows the selling price field', 'Customer price, each single unit' in
      c.get('/edit-medication?med_name=Unpriced+cream').get_data(as_text=True))
check('the till page loads', c.get('/sell').status_code == 200)
pl = c.get('/api/pos/products').get_json()
check('the till item list carries price, stock and the Rx flag',
      any(p['name'] == 'Amlodipine 5mg tabs' and p['rx_only'] and p['price'] == 1.10 for p in pl))

print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
