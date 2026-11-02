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
meds, txs, dels = db['medications'], db['transactions'], db['deliveries']
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
    for col in ['medications', 'transactions', 'deliveries', 'audit_log', 'stock_takes',
                'stock_take_counts']:
        db[col].delete_many({})


TODAY = datetime.utcnow()
M_START = TODAY.strftime('%Y-%m-01')
M_END = TODAY.strftime('%Y-%m-%d')


def add_med(name, pack_size=100, pack_price='250.00', opening=0):
    return c.post('/add-medication', data={
        'med_name': name, 'pack_size': str(pack_size), 'pack_price': str(pack_price),
        'schedule': 'not controlled', 'initial_balance': str(opening), 'batch': 'B',
        'expiry_date': '2030-01-01', 'stock_receiver': 'R'}, follow_redirects=True)


def receive(med, units, pack_size=100, pack_price='250.00', order='PO-1',
            order_total='2500.00', invoice='INV-1', invoice_total='2500.00'):
    c.post('/receive', data={'action': 'open', 'supplier': 'MedSupply',
                             'order_number': order, 'order_total': order_total,
                             'invoice_number': invoice, 'invoice_total': invoice_total,
                             'stock_receiver': 'R'}, follow_redirects=True)
    did = str(dels.find_one({'invoice_number': invoice}, sort=[('opened_at', -1)])['_id'])
    c.post('/receive', data={'action': 'line', 'delivery_id': did, 'med_name': med,
                             'quantity': str(units), 'pack_size': str(pack_size),
                             'pack_price': str(pack_price), 'batch': 'B',
                             'expiry_date': '2030-01-01'}, follow_redirects=True)
    return did


def dispense(patient, pairs):
    data = {'patient': patient, 'company': 'C', 'position': 'P', 'age_group': 'adult',
            'gender': 'M', 'sick_leave_days': '0', 'diagnoses': 'flu',
            'prescriber': 'Dr X', 'dispenser': 'D',
            'date': TODAY.strftime('%Y-%m-%d'),
            'med_names': [p[0] for p in pairs],
            'quantities': [str(p[1]) for p in pairs]}
    return c.post('/dispense', data=data, follow_redirects=True)


def monthly(start=None):
    return c.post('/reports', data={'report_type': 'monthly_report',
                                    'start_date': start or M_START,
                                    'end_date': M_END}).data.decode()


def row(body, label):
    mm = _re.search(r'<td>' + _re.escape(label) + r'</td>\s*<td>(.*?)</td>', body, _re.S)
    return _re.sub(r'<[^>]+>', '', mm.group(1)).strip() if mm else None


# =====================================================================
print('\n--- 1. Cost is frozen at the moment of dispensing ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
receive('Paracetamol, 500 mg', 1000)
dispense('Thabo M', [('Paracetamol, 500 mg', 200)])
t = txs.find_one({'type': 'dispense'})
check('the dispense line stores a unit price', t.get('price') == 2.50, str(t.get('price')))
check('it stores a line value', t.get('line_value') == 500.0, str(t.get('line_value')))

# supplier price doubles
receive('Paracetamol, 500 mg', 100, pack_price='500.00', order='PO-2',
        order_total='500.00', invoice='INV-2', invoice_total='500.00')
check('the medication now carries the new unit price',
      meds.find_one({})['price'] == 5.00, str(meds.find_one({})['price']))
t = txs.find_one({'type': 'dispense'})
check('the historical dispense cost did NOT change', t['line_value'] == 500.0,
      str(t['line_value']))
body = monthly()
check('the month still reports the original cost, not the new price',
      row(body, 'Total cost dispensed') == 'R500.00',
      row(body, 'Total cost dispensed'))
check('a re-run gives the same answer',
      row(monthly(), 'Total cost dispensed') == 'R500.00')

# =====================================================================
print('\n--- 2. Cost per dispensing transaction on the dispense page ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')     # R2.50/unit
add_med('Aspirin, 100 mg', pack_size=100, pack_price='500.00')         # R5.00/unit
receive('Paracetamol, 500 mg', 1000)
receive('Aspirin, 100 mg', 1000, pack_price='500.00', order='PO-A',
        order_total='5000.00', invoice='INV-A', invoice_total='5000.00')
dispense('Thabo M', [('Paracetamol, 500 mg', 20), ('Aspirin, 100 mg', 10)])
body = c.get('/dispense').data.decode()
check('the dispense list shows a unit cost column', '<th>Unit Cost</th>' in body)
check('and a line cost column', '<th>Line Cost</th>' in body)
check('and a visit cost column', '<th>Visit Cost</th>' in body)
check('line cost for 20 @ R2.50 is R50.00', 'R50.00' in body)
check('line cost for 10 @ R5.00 is R50.00', body.count('R50.00') >= 2)
check('the visit total is R100.00', 'R100.00' in body)

# =====================================================================
print('\n--- 3. Monthly report: dispensing figures ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
receive('Paracetamol, 500 mg', 2000, order_total='5000.00', invoice_total='5000.00')
dispense('Thabo M', [('Paracetamol, 500 mg', 100)])      # R250
dispense('Palesa N', [('Paracetamol, 500 mg', 200)])     # R500
dispense('Thabo M', [('Paracetamol, 500 mg', 100)])      # R250, repeat patient
body = monthly()
check('the report renders with the month name', TODAY.strftime('%B %Y') in body)
check('it defaults to a month when no period is chosen', 'Month Report' in body)
check('3 visits', row(body, 'Visits') == '3', row(body, 'Visits'))
check('3 items dispensed', row(body, 'Items dispensed') == '3')
check('2 distinct patients', row(body, 'Patients seen') == '2', row(body, 'Patients seen'))
check('total cost dispensed R1000.00',
      row(body, 'Total cost dispensed') == 'R1000.00', row(body, 'Total cost dispensed'))
check('average cost per visit R333.33',
      row(body, 'Average cost per visit') == 'R333.33', row(body, 'Average cost per visit'))
check('average cost per item R333.33',
      row(body, 'Average cost per item') == 'R333.33', row(body, 'Average cost per item'))

print('\n--- 4. Monthly report: procurement figures ---')
check('1 order raised', row(body, 'Orders raised') == '1', row(body, 'Orders raised'))
check('order total R5000.00', row(body, 'Total ordered') == 'R5000.00',
      row(body, 'Total ordered'))
check('1 invoice', row(body, 'Invoices received') == '1')
check('invoice total R5000.00', row(body, 'Total invoiced') == 'R5000.00')
check('goods received R5000.00', row(body, 'Goods actually received') == 'R5000.00',
      row(body, 'Goods actually received'))
check('no gap between invoiced and received',
      row(body, 'Invoiced less goods received') == 'R+0.00',
      row(body, 'Invoiced less goods received'))

print('\n--- 5. A short delivery shows up as a gap ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
receive('Paracetamol, 500 mg', 800, order_total='2500.00', invoice_total='2500.00')
body = monthly()
check('goods received is R2000 against a R2500 invoice',
      row(body, 'Goods actually received') == 'R2000.00', row(body, 'Goods actually received'))
check('the shortfall is surfaced as +R500',
      row(body, 'Invoiced less goods received') == 'R+500.00',
      row(body, 'Invoiced less goods received'))
check('the gap row is ambered', 'close-to-expire' in body)

print('\n--- 6. Stock value movement reconciles ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
receive('Paracetamol, 500 mg', 1000, order_total='2500.00', invoice_total='2500.00')
dispense('Thabo M', [('Paracetamol, 500 mg', 200)])
body = monthly()
opening = float(row(body, 'Opening stock value').replace('R', ''))
received = float(row(body, 'Received from suppliers').replace('R', ''))
closing = float(row(body, 'Closing stock value').replace('R', ''))
disp = float(row(body, 'Dispensed').replace('R', '').replace('−', '').replace('&minus;', ''))
print(f'   opening {opening} + received {received} - dispensed {disp} = {opening + received - disp}'
      f'  vs closing {closing}')
check('opening is 0 for a brand-new item', opening == 0.0, str(opening))
check('received is R2500', received == 2500.0, str(received))
check('dispensed is R500', disp == 500.0, str(disp))
check('closing is R2000', closing == 2000.0, str(closing))
check('the value column reconciles',
      abs(opening + received - disp - closing) < 0.01,
      str(opening + received - disp - closing))
import re as _re2
check('the differing valuation bases are explained',
      'price effect, not a stock discrepancy' in _re2.sub(r'\s+', ' ', body))

print('\n--- 7. Opening loads are shown separately from supplier receipts ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00', opening=400)
receive('Paracetamol, 500 mg', 200, order_total='500.00', invoice_total='500.00')
body = monthly()
check('the opening load is valued at R1000',
      row(body, 'Opening loads') == 'R1000.00', row(body, 'Opening loads'))
check('it is NOT counted as a supplier receipt',
      row(body, 'Received from suppliers') == 'R500.00',
      row(body, 'Received from suppliers'))

print('\n--- 8. Stock take losses are admin-only ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
receive('Paracetamol, 500 mg', 1000, order_total='2500.00', invoice_total='2500.00')
c.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
c.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                            'med_name': 'Paracetamol, 500 mg', 'counted': '900'},
       follow_redirects=True)
body = monthly()
check('admin sees the adjustment value', 'Stock take adjustments' in body)
check('the loss is valued at -R250', row(body, 'Stock take adjustments') == 'R-250.00',
      row(body, 'Stock take adjustments'))
login('user', 'Pharm')
ubody = monthly()
check('a non-admin does not see the adjustment line',
      'Stock take adjustments' not in ubody)
check('but still sees the rest of the report', 'Total cost dispensed' in ubody)
login()

print('\n--- 9. Highest cost items ---')
reset()
login()
for n, pp in [('Expensive Item, 10 mg', '5000.00'), ('Cheap Item, 10 mg', '100.00')]:
    add_med(n, pack_size=100, pack_price=pp)
receive('Expensive Item, 10 mg', 500, pack_price='5000.00', order='PO-E',
        order_total='25000.00', invoice='INV-E', invoice_total='25000.00')
receive('Cheap Item, 10 mg', 500, pack_price='100.00', order='PO-C',
        order_total='500.00', invoice='INV-C', invoice_total='500.00')
dispense('Thabo M', [('Expensive Item, 10 mg', 100), ('Cheap Item, 10 mg', 100)])
body = monthly()
seg = body[body.index('Highest Cost Items'):]
order = _re.findall(r'<td>([^<]*Item, 10 mg)</td>', seg)
check('items are ranked by cost, not by name or quantity',
      order[0] == 'Expensive Item, 10 mg', str(order))
# 100 x R50 = 5000 vs 100 x R1 = 100 -> 98.0% / 2.0%
check('shares are shown', '98.0' in seg and '2.0' in seg, seg[:400])

print('\n--- 10. Legacy dispense lines are valued and flagged ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
receive('Paracetamol, 500 mg', 1000, order_total='2500.00', invoice_total='2500.00')
# a dispense as written before cost capture: no price, no line_value
txs.insert_one({'type': 'dispense', 'med_name': 'Paracetamol, 500 mg', 'quantity': 100,
                'transaction_id': 'TX-OLD', 'patient': 'Old Patient', 'user': 'u',
                'timestamp': datetime.utcnow()})
body = monthly()
check('a legacy line is still valued at the current price',
      row(body, 'Total cost dispensed') == 'R250.00', row(body, 'Total cost dispensed'))
check('the estimate is disclosed', 'predate cost capture' in body)
check('the count of estimated lines is given', '1\ndispensing line(s)' in body
      or '1 dispensing line(s)' in _re.sub(r'\s+', ' ', body))
dispense('New Patient', [('Paracetamol, 500 mg', 100)])
body = monthly()
check('a mix of exact and estimated lines still totals correctly',
      row(body, 'Total cost dispensed') == 'R500.00', row(body, 'Total cost dispensed'))

print('\n--- 11. Month boundaries and bad input ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
receive('Paracetamol, 500 mg', 1000, order_total='2500.00', invoice_total='2500.00')
dispense('Thabo M', [('Paracetamol, 500 mg', 100)])
# a dispense in a previous month
prev = (TODAY.replace(day=1) - timedelta(days=5))
txs.insert_one({'type': 'dispense', 'med_name': 'Paracetamol, 500 mg', 'quantity': 400,
                'transaction_id': 'TX-PREV', 'patient': 'Last Month', 'user': 'u',
                'price': 2.5, 'line_value': 1000.0, 'timestamp': prev})
body = monthly()
check('this month excludes last month\'s activity',
      row(body, 'Total cost dispensed') == 'R250.00', row(body, 'Total cost dispensed'))
body = monthly(start=prev.strftime('%Y-%m-15'))
check('any date in a month selects that whole month',
      prev.strftime('%B %Y') in body)
check('last month shows only its own activity',
      row(body, 'Total cost dispensed') == 'R1000.00', row(body, 'Total cost dispensed'))
check('a missing start date is reported, not crashed',
      c.post('/reports', data={'report_type': 'monthly_report',
                               'end_date': M_END}).status_code == 200)
r = c.post('/reports', data={'report_type': 'monthly_report',
                             'start_date': 'not-a-date', 'end_date': M_END})
check('a malformed date does not error', r.status_code == 200)

print('\n--- 12. An empty month ---')
reset()
login()
add_med('Paracetamol, 500 mg', pack_size=100, pack_price='250.00')
body = monthly()
check('an empty month renders', 'Report &mdash;' in body and 'Stock Value Movement' in body)
check('zero visits shows a dash for items per visit',
      row(body, 'Items per visit') == '-', row(body, 'Items per visit'))
check('no divide-by-zero on cost per visit',
      row(body, 'Average cost per visit') == 'R0.00')
check('the top items table says nothing was dispensed',
      'Nothing dispensed in this period' in body)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
