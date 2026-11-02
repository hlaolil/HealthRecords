"""Downloading a report instead of retyping it.

The user's complaint was simple: the figures are already on the screen, and
the only way to get them into anything else was to copy them by hand. These
checks are about the download being (a) offered on every report that has a
table, (b) the same numbers as the screen, and (c) a file a spreadsheet will
open without an argument about commas.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, csv, io, json
from datetime import datetime, timedelta, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
# Bound by value — see the note in test_defaults.py.
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
db = _f['pharmacy_db']
meds, txs = db['medications'], db['transactions']
c = A.app.test_client()
# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf, page_source
arm_csrf(c)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin', name='Letlotlo Hlaoli'):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['medications', 'transactions', 'audit_log']:
        db[col].delete_many({})


def download(report_type, **extra):
    """POST the report with the download button pressed."""
    data = {'report_type': report_type, 'export': 'csv'}
    data.update(extra)
    return c.post('/reports', data=data)


def rows_of(resp):
    return list(csv.reader(io.StringIO(resp.data.decode('utf-8'))))


# Quantities below are all POSITIVE: the app records the direction in the
# transaction type, not the sign, and the inventory report's reconciliation
# (beginning + received - issued = current) depends on that convention.
MONTH_START = A.local_now().replace(day=1, hour=8, minute=0,
                                    second=0, microsecond=0)
NOW = A.local_now()


def seed():
    reset()
    # A name with a comma in it, because that is what every medication here
    # looks like and it is exactly what breaks a hand-rolled CSV.
    meds.insert_many([
        {'name': 'Amoxicillin, 250 mg', 'balance': 120, 'price': 1.5,
         'expiry_date': '2027-03-31', 'batch': 'AX1', 'category': 'general',
         'pack_size': 100, 'pack_price': 150.0, 'supplier': 'NDSO'},
        {'name': 'Morphine, 10 mg', 'balance': 8, 'price': 20.0,
         'expiry_date': '2026-11-30', 'batch': 'MO2', 'category': 'controlled',
         'pack_size': 10, 'pack_price': 200.0, 'supplier': 'NDSO'},
        {'name': 'Expired Thing, 5 mg', 'balance': 4, 'price': 2.0,
         'expiry_date': '2020-01-31', 'batch': 'EX3', 'category': 'general',
         'pack_size': 50, 'pack_price': 100.0, 'supplier': 'NDSO'},
        {'name': 'Nothing Left, 1 mg', 'balance': 0, 'price': 1.0,
         'expiry_date': '2028-01-31', 'batch': 'NL4', 'category': 'general',
         'pack_size': 50, 'pack_price': 50.0, 'supplier': 'NDSO'},
    ])
    t = MONTH_START + timedelta(days=1)
    txs.insert_many([
        {'type': 'receive', 'med_name': 'Amoxicillin, 250 mg', 'quantity': 100,
         'price': 1.5, 'line_value': 150.0, 'batch': 'AX1',
         'expiry_date': '2027-03-31', 'supplier': 'NDSO',
         'invoice_number': 'INV-9', 'user': 'Letlotlo Hlaoli', 'timestamp': t},
        {'type': 'dispense', 'med_name': 'Amoxicillin, 250 mg', 'quantity': 30,
         'price': 1.5, 'line_value': 45.0, 'patient': 'Thabo Moeti',
         'outcome': 'Sick leave', 'outcome_days': 3, 'company': 'Letseng',
         'transaction_id': 'T-SL-1',
         'diagnoses': ['URTI', 'Fever'], 'prescriber': 'Dr. Tau',
         'dispenser': 'Letlotlo Hlaoli', 'user': 'Letlotlo Hlaoli',
         'timestamp': t + timedelta(hours=2)},
        {'type': 'internal_issue', 'med_name': 'Amoxicillin, 250 mg',
         'quantity': 10, 'price': 1.5, 'line_value': -15.0,
         'department': 'Occupational Health', 'issued_to': 'Sr. Mokoena',
         'user': 'Letlotlo Hlaoli', 'timestamp': t + timedelta(hours=3)},
        {'type': 'damage', 'med_name': 'Expired Thing, 5 mg', 'quantity': 6,
         'price': 2.0, 'line_value': -12.0, 'batch': 'EX3',
         'expiry_date': '2020-01-31', 'reason': 'Expired',
         'note': 'Found at stock take', 'user': 'Letlotlo Hlaoli',
         'timestamp': t + timedelta(hours=4)},
        {'type': 'unmet_demand', 'med_name': 'Morphine, 10 mg',
         'quantity_requested': 20, 'patient': 'Lineo Phiri',
         'user': 'Letlotlo Hlaoli', 'timestamp': t + timedelta(hours=5)},
        {'type': 'dispense', 'med_name': 'Morphine, 10 mg', 'quantity': 2,
         'price': 20.0, 'line_value': 40.0, 'patient': 'Lineo Phiri',
         'outcome': 'Outpatient', 'dispenser': 'Letlotlo Hlaoli',
         'user': 'Letlotlo Hlaoli', 'timestamp': t + timedelta(hours=6)},
    ])


RANGE = {'start_date': MONTH_START.strftime('%Y-%m-%d'),
         'end_date': NOW.strftime('%Y-%m-%d')}

seed()
login()

print('\n--- 1. The download is offered where there is a table ---')
body = c.post('/reports', data=dict(report_type='inventory', **RANGE)).data.decode()
check('the reports page carries a Download CSV button',
      'name="export" value="csv"' in body)
check('and it does not fight the form validation',
      'formnovalidate' in body)
check('it is a submit button, so it reuses the same filters',
      'type="submit" name="export"' in body)

print('\n--- 2. Inventory downloads as a real CSV ---')
r = download('inventory', **RANGE)
check('the response is a CSV, not a web page',
      r.headers.get('Content-Type', '').startswith('text/csv'),
      r.headers.get('Content-Type'))
cd = r.headers.get('Content-Disposition', '')
check('the browser is told to save it', 'attachment' in cd, cd)
check('the file is named for the report and the day',
      f'inventory-report-{NOW.strftime("%Y%m%d")}.csv' in cd, cd)

rows = rows_of(r)
check('there is a header row', bool(rows) and rows[0][0] == 'Item',
      str(rows[:1]))
check('the download has the columns the screen has', rows[0] == ['Item', 'Category', 'Beginning Balance', 'Received', 'Issued',
    'Current Balance', 'Adjusted AMC', 'Amount to Order'],
      str(rows[0]))
amox = [x for x in rows[1:] if x and x[0] == 'Amoxicillin, 250 mg']
check('the medication name survived its own comma intact', len(amox) == 1,
      str([x[0] for x in rows[1:]]))
if amox:
    row = dict(zip(rows[0], amox[0]))
    check('the comma was quoted, not split into two cells',
          len(amox[0]) == len(rows[0]), str(len(amox[0])))
    check('received matches the delivery', row['Received'] == '100',
          row['Received'])
    # 30 dispensed to patients and 10 sent to a department: one Issued column
    # now, with every outward movement folded in.
    check('issued is the dispensing and the department issue together',
          row['Issued'] == '40', row['Issued'])
    check('current balance is the balance on the shelf',
          row['Current Balance'] == '120', row['Current Balance'])
    recon = (int(row['Beginning Balance']) + int(row['Received'])
             - int(row['Issued']))
    check('the downloaded row still reconciles',
          recon == int(row['Current Balance']),
          f'{recon} != {row["Current Balance"]} in {row}')
    check('Adjusted AMC is a whole number',
          row['Adjusted AMC'].lstrip('-').isdigit(), row['Adjusted AMC'])

print('\n--- 3. The download is the screen, not a second query ---')
screen = c.post('/reports', data=dict(report_type='inventory', **RANGE)).data.decode()
csv_names = sorted(x[0] for x in rows[1:] if x)
on_screen = sorted(m['name'] for m in meds.find({}, {'name': 1})
                   if m['name'] in screen)
check('the same medications appear in both', csv_names == on_screen,
      f'csv={csv_names} screen={on_screen}')

print('\n--- 4. Stock reports ---')
for rt, name, expect in [
        ('stock_on_hand', 'stock-on-hand', 'Amoxicillin, 250 mg'),
        ('expired_list', 'expired-stock', 'Expired Thing, 5 mg'),
        ('out_of_stock_list', 'out-of-stock', 'Nothing Left, 1 mg')]:
    r = download(rt, **RANGE)
    rows = rows_of(r)
    check(f'{rt} downloads', r.headers.get('Content-Type', '').startswith('text/csv'))
    check(f'{rt} is named {name}', name in r.headers.get('Content-Disposition', ''),
          r.headers.get('Content-Disposition'))
    check(f'{rt} lists the item it is about',
          any(x and x[0] == expect for x in rows[1:]),
          str([x[0] for x in rows[1:] if x]))
    check(f'{rt} does not list the ones it is not about',
          not any(x and x[0] == 'Morphine, 10 mg' for x in rows[1:])
          or rt == 'stock_on_hand',
          str([x[0] for x in rows[1:] if x]))

print('\n--- 5. Registers keep the columns the paper form has ---')
r = download('sick_leave', **RANGE)
rows = rows_of(r)
check('sick leave headers match the register',
      rows[0] == ['Date', 'Name', 'Company', 'Days Given', 'Diagnosis',
                  'Prescriber'], str(rows[0]))
sl = [x for x in rows[1:] if x and x[1] == 'Thabo Moeti']
check('the sick leave entry is there', len(sl) == 1, str(rows[1:]))
if sl:
    check('days given came through', sl[0][3] == '3', sl[0][3])
    check('a list of diagnoses became one readable cell',
          sl[0][4] == 'URTI, Fever', sl[0][4])
    check('only sick leave is in the sick leave register',
          not any(x and x[1] == 'Lineo Phiri' for x in rows[1:]),
          str([x[1] for x in rows[1:] if x]))

r = download('internal_issues', **RANGE)
rows = rows_of(r)
check('internal issues name both sides of the transfer',
      rows[0] == ['Date', 'Medication', 'Qty', 'Department', 'Received By',
                  'Value', 'Issued By'], str(rows[0]))
iss = [x for x in rows[1:] if x]
check('the receiver and the issuer are different columns',
      iss and iss[0][4] == 'Sr. Mokoena' and iss[0][6] == 'Letlotlo Hlaoli',
      str(iss[:1]))

r = download('expiry_writeoffs', **RANGE)
rows = rows_of(r)
check('losses carry batch, expiry and cost',
      rows[0] == ['Date', 'Item Description', 'Qty', 'Expiry Date',
                  'Batch/Lot', 'Total Cost', 'Remarks'], str(rows[0]))
check('the written-off item is listed',
      any(x and x[1] == 'Expired Thing, 5 mg' for x in rows[1:]),
      str(rows[1:]))

r = download('unmet_demand', **RANGE)
rows = rows_of(r)
check('unmet demand records what was asked for',
      rows[0] == ['Date', 'Medication', 'Requested', 'Patient'], str(rows[0]))
check('the shortfall is listed',
      any(x and x[1] == 'Morphine, 10 mg' and x[2] == '20' for x in rows[1:]),
      str(rows[1:]))

r = download('receive_list', **RANGE)
rows = rows_of(r)
check('receipts carry the invoice', 'Invoice' in rows[0], str(rows[0]))
check('the delivery is listed',
      any(x and x[1] == 'Amoxicillin, 250 mg' and 'INV-9' in x for x in rows[1:]),
      str(rows[1:]))

r = download('controlled_drug_register', **RANGE)
rows = rows_of(r)
check('the controlled register carries a running balance',
      rows[0] == ['Date', 'Medication', 'Movement', 'In', 'Out', 'Balance',
                  'Patient', 'By'], str(rows[0]))
check('only controlled drugs are in it',
      all(x[1] == 'Morphine, 10 mg' for x in rows[1:] if x),
      str([x[1] for x in rows[1:] if x]))

print('\n--- 6. The order request flattens its groups into rows ---')
r = download('order_request', **RANGE)
check('it downloads', r.headers.get('Content-Type', '').startswith('text/csv'),
      r.headers.get('Content-Type'))
check('it is named for the order', 'order-request' in
      r.headers.get('Content-Disposition', ''),
      r.headers.get('Content-Disposition'))
rows = rows_of(r)
check('the supplier is a column, not a heading the sheet cannot sort on',
      rows[0][0] == 'Supplier', str(rows[0]))
check('packs to order is in there', 'Packs to Order' in rows[0], str(rows[0]))
body_rows = [x for x in rows[1:] if x]
check('each line names its own supplier',
      all(x[0] for x in body_rows) if body_rows else True,
      str(body_rows[:2]))

print('\n--- 7. A report with no table says so instead of sending an empty file ---')
r = c.post('/reports', data={'report_type': 'monthly_summary', 'export': 'csv',
                             **RANGE}, follow_redirects=True)
check('it stays on the page', r.status_code == 200, str(r.status_code))
check('it is not a download pretending to be data',
      not r.headers.get('Content-Type', '').startswith('text/csv'),
      r.headers.get('Content-Type'))

print('\n--- 8. The flattening helper itself ---')
check('None becomes an empty cell, not the word None', A._csv_value(None) == '')
check('a list becomes a readable cell',
      A._csv_value(['a', 'b']) == 'a; b', A._csv_value(['a', 'b']))
check('a datetime becomes a date a sheet will parse',
      A._csv_value(datetime(2026, 3, 4, 17, 5)) == '2026-03-04',
      A._csv_value(datetime(2026, 3, 4, 17, 5)))
check('zero is still zero, not blank', A._csv_value(0) == '0')
check('an unknown report type is refused rather than guessed',
      A._report_csv('not_a_report', {}) is None)
check('every report with a table has a download spec',
      all(isinstance(v, tuple) and len(v) == 3 for v in A.CSV_REPORTS.values()))

print('\n--- 9. A non-admin downloads the non-admin screen ---')
login(role='user', name='Mamosa Seetsa')
r = download('inventory', **RANGE)
check('the download still works for a non-admin',
      r.headers.get('Content-Type', '').startswith('text/csv'),
      r.headers.get('Content-Type'))
rows = rows_of(r)
check('and has the same columns as everybody else', rows[0] == ['Item', 'Category', 'Beginning Balance', 'Received', 'Issued',
    'Current Balance', 'Adjusted AMC', 'Amount to Order'],
      str(rows[0]))
for hidden in ['Adjustment', 'Expired', 'Damaged', 'To Departments']:
    check(f'there is no separate {hidden} column',
          hidden not in rows[0], str(rows[0]))
amox = [x for x in rows[1:] if x and x[0] == 'Amoxicillin, 250 mg']
if amox:
    row = dict(zip(rows[0], amox[0]))
    recon = (int(row['Beginning Balance']) + int(row['Received'])
             - int(row['Issued']))
    check('the non-admin row reconciles with the folded figure',
          recon == int(row['Current Balance']),
          f'{recon} != {row["Current Balance"]} in {row}')
login()

print('\n--- 10. The dispensing record downloads from its own page ---')
reset()
login()
meds.insert_many([
    {'name': 'Paracetamol, 500 mg', 'balance': 500, 'price': 0.4,
     'expiry_date': '2028-01-31', 'batch': 'PC2', 'category': 'general',
     'pack_size': 1000, 'pack_price': 400.0},
    {'name': 'Amoxicillin, 250 mg', 'balance': 200, 'price': 1.5,
     'expiry_date': '2027-03-31', 'batch': 'AX1', 'category': 'general',
     'pack_size': 100, 'pack_price': 150.0},
])
t0 = A.local_now().replace(hour=9, minute=0, second=0, microsecond=0)
# One visit, two medications - the case a flat row-per-line file has to get
# right without inflating the money.
txs.insert_many([
    {'type': 'dispense', 'transaction_id': 'V-1', 'med_name': 'Paracetamol, 500 mg',
     'quantity': 10, 'price': 0.4, 'line_value': 4.0, 'patient': 'Thabo Mokoena',
     'company': 'Letseng Diamonds', 'position': 'Driver', 'gender': 'Male',
     'birth_decade': 1980, 'outcome': 'Sick leave', 'outcome_days': 3,
     'diagnoses': ['URTI', 'Fever'], 'prescriber': 'Dr. T. Khothatso',
     'dispenser': 'Letlotlo Hlaoli', 'user': 'Letlotlo Hlaoli',
     'date': t0.strftime('%Y-%m-%d'), 'timestamp': t0},
    {'type': 'dispense', 'transaction_id': 'V-1', 'med_name': 'Amoxicillin, 250 mg',
     'quantity': 4, 'price': 1.5, 'line_value': 6.0, 'patient': 'Thabo Mokoena',
     'company': 'Letseng Diamonds', 'position': 'Driver', 'gender': 'Male',
     'birth_decade': 1980, 'outcome': 'Sick leave', 'outcome_days': 3,
     'diagnoses': ['URTI', 'Fever'], 'prescriber': 'Dr. T. Khothatso',
     'dispenser': 'Letlotlo Hlaoli', 'user': 'Letlotlo Hlaoli',
     'date': t0.strftime('%Y-%m-%d'), 'timestamp': t0},
    # a second visit, and an older record with no line_value stored
    {'type': 'dispense', 'transaction_id': 'V-2', 'med_name': 'Paracetamol, 500 mg',
     'quantity': 20, 'price': 0.4, 'patient': 'Palesa Nkoe',
     'company': 'Lesotho Highlands', 'position': 'Clerk', 'gender': 'Female',
     'birth_decade': 1990, 'outcome': 'Outpatient',
     'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
     'user': 'Letlotlo Hlaoli', 'date': t0.strftime('%Y-%m-%d'),
     'timestamp': t0 - timedelta(days=1)},
])

body = c.get('/dispense').data.decode()
check('the dispensing page offers the download',
      'name="export" value="csv"' in body)
check('it sits on the filter form, so it obeys the dates and search',
      body.index('name="export"') > body.index('class="filter-form"')
      and body.index('name="export"') < body.index('</form>', body.index('class="filter-form"')))
check('and the page says the download is not just this page',
      'not just this page' in body)

r = c.get('/dispense?export=csv')
check('it comes back as a CSV',
      r.headers.get('Content-Type', '').startswith('text/csv'),
      r.headers.get('Content-Type'))
check('named for the record and the day',
      f'dispensing-record-{A.local_now().strftime("%Y%m%d")}.csv'
      in r.headers.get('Content-Disposition', ''),
      r.headers.get('Content-Disposition'))
rows = rows_of(r)
check('the header matches the record on screen', rows[0] == [
    'Date', 'Timestamp', 'Patient', 'Company', 'Position', 'Gender', 'Born',
    'Outcome', 'Days', 'Diagnoses', 'Prescriber', 'Dispenser', 'Medication',
    'Quantity', 'Unit Cost', 'Line Cost', 'Visit Cost', 'Recorded By',
    'Visit ID'], str(rows[0]))
data = [dict(zip(rows[0], x)) for x in rows[1:] if x]
check('every medication line is a row of its own', len(data) == 3, str(len(data)))
v1 = [x for x in data if x['Visit ID'] == 'V-1']
check('both medicines of the one visit are there', len(v1) == 2, str(v1))
check('the visit details repeat down its rows',
      all(x['Patient'] == 'Thabo Mokoena' and x['Company'] == 'Letseng Diamonds'
          for x in v1), str(v1))
check('the sick leave days come through', all(x['Days'] == '3' for x in v1),
      str([x['Days'] for x in v1]))
check('a list of diagnoses becomes one readable cell',
      all(x['Diagnoses'] == 'URTI; Fever' for x in v1),
      str([x['Diagnoses'] for x in v1]))
check('the medication name keeps its own comma',
      any(x['Medication'] == 'Amoxicillin, 250 mg' for x in v1),
      str([x['Medication'] for x in v1]))

# The money has to be summable. Repeating the visit total on every line of a
# two-medicine visit would double it.
check('line cost is on every row',
      sorted(x['Line Cost'] for x in v1) == ['4.00', '6.00'],
      str([x['Line Cost'] for x in v1]))
check('the visit total appears once per visit, not once per medicine',
      sorted(x['Visit Cost'] for x in v1) == ['', '10.00'],
      str([x['Visit Cost'] for x in v1]))
check('so summing either column gives the same answer',
      round(sum(float(x['Line Cost']) for x in data), 2)
      == round(sum(float(x['Visit Cost']) for x in data if x['Visit Cost']), 2),
      str([(x['Line Cost'], x['Visit Cost']) for x in data]))
v2 = [x for x in data if x['Visit ID'] == 'V-2'][0]
check('a record with no stored line value is still costed',
      v2['Line Cost'] == '8.00', v2['Line Cost'])

print('\n--- 11. The download obeys the filter, and brings whole visits ---')
r = c.get('/dispense?export=csv&search=Amoxicillin')
data = [dict(zip(rows_of(r)[0], x)) for x in rows_of(r)[1:] if x]
check('a search narrows the download',
      all(x['Visit ID'] == 'V-1' for x in data), str(data))
check('but a match on one medicine brings the WHOLE visit',
      len(data) == 2,
      str([x['Medication'] for x in data]) + ' — half a prescription is not '
      'the record')

r = c.get('/dispense?export=csv&start_date='
          + (t0 - timedelta(days=1)).strftime('%Y-%m-%d')
          + '&end_date=' + (t0 - timedelta(days=1)).strftime('%Y-%m-%d'))
data = [dict(zip(rows_of(r)[0], x)) for x in rows_of(r)[1:] if x]
check('a date range narrows it too',
      len(data) == 1 and data[0]['Patient'] == 'Palesa Nkoe', str(data))

print('\n--- 12. Too much to hand over at once is said, not truncated ---')
_was = A.DISPENSE_CSV_MAX_LINES
A.DISPENSE_CSV_MAX_LINES = 1
try:
    r = c.get('/dispense?export=csv', follow_redirects=True)
    check('an oversized download is refused, not quietly cut short',
          not r.headers.get('Content-Type', '').startswith('text/csv'),
          r.headers.get('Content-Type'))
    check('and it says what to do about it',
          'Narrow the date range' in r.data.decode())
finally:
    A.DISPENSE_CSV_MAX_LINES = _was
check('the limit is restored', A.DISPENSE_CSV_MAX_LINES == _was)

print('\n--- 13. The other registers download too ---')
# Reports and dispensing could already. These are the rest, through one
# helper so a sixth register costs four lines rather than forty.
reset()
login()
NOW2 = datetime.utcnow()
db['audit_log'].insert_one({
    'timestamp': NOW2, 'user': 'Letlotlo Hlaoli', 'action': 'CREATE',
    'target_type': 'medication', 'target_id': 'Amoxicillin, 250 mg',
    'changes': 'added with opening balance 100', 'ip': '127.0.0.1'})
db['temperature_logs'].insert_one({
    'date': NOW2, 'session': 'Morning', 'location': 'Dispensary',
    'thermometer': '1', 'temp': 22.4, 'initials': 'LH',
    'check_time': '08:30', 'user': 'Letlotlo Hlaoli', 'timestamp': NOW2})
db['key_register'].insert_one({
    'ref': 'K-001', 'issued_date': NOW2, 'issued_time': '11:30',
    'recipient_name': 'Malesoetsa Leohla', 'recipient_role': 'Nursing Officer',
    'purpose': 'Weekend cover', 'returned_date': NOW2, 'returned_time': '12:00',
    'pharmacist': 'Letlotlo Hlaoli', 'user': 'Letlotlo Hlaoli'})
db['loans'].insert_one({
    'loan_ref': 'L-001', 'item_code': 'CR-01', 'item_type': 'Crutches',
    'patient_name': 'Mpho Letsie', 'issued_date': NOW2,
    'due_date': NOW2 + timedelta(days=14), 'issued_by': 'Letlotlo Hlaoli',
    'status': A.LOAN_STATUS_OUT})

for path, name, must in [
        ('/audit?export=csv', 'audit-log', 'Amoxicillin, 250 mg'),
        ('/temperature?export=csv', 'temperature-register', 'Dispensary'),
        ('/key-register?export=csv', 'key-register', 'Malesoetsa Leohla'),
        ('/loans?export=csv', 'equipment-loans', 'Mpho Letsie')]:
    r = c.get(path)
    check(f'{name} downloads',
          r.headers.get('Content-Type', '').startswith('text/csv'),
          r.headers.get('Content-Type'))
    check(f'{name} is named for the register and the day',
          f'{name}-{A.local_now().strftime("%Y%m%d")}.csv'
          in r.headers.get('Content-Disposition', ''),
          r.headers.get('Content-Disposition'))
    rows = rows_of(r)
    check(f'{name} carries its record', any(must in ','.join(x) for x in rows[1:]),
          str(rows[1:2]))
    check(f'{name} has a header on every column',
          rows and all(h.strip() for h in rows[0]), str(rows[0] if rows else None))

print('\n--- 14. A register with nothing in it says so ---')
reset()
login()
r = c.get('/audit?export=csv')
# The first version refused and flashed a message, but these pages do not all
# render flashed messages — so on the audit log the button did nothing at all
# and said nothing about why.
check('an empty register still hands over the file',
      r.headers.get('Content-Type', '').startswith('text/csv'),
      r.headers.get('Content-Type'))
rows = rows_of(r)
check('with its column headings', rows and rows[0][0] == 'When', str(rows[:1]))
check('and no rows pretending to be records',
      len([x for x in rows[1:] if x]) == 0, str(rows[1:]))

print('\n--- 15. Companies and positions come from the record ---')
# Written into the page, the two lists went stale the moment a new contractor
# arrived: the name was not offered, so it got typed three ways and the
# sick-leave-by-company report split across all three.
reset()
login()
txs.insert_many([
    {'type': 'dispense', 'company': 'Letseng Diamonds', 'position': 'Shift Boss',
     'med_name': 'X', 'quantity': 1, 'timestamp': NOW2},
    {'type': 'dispense', 'company': 'UNITRANS', 'position': 'Drivers',
     'med_name': 'X', 'quantity': 1, 'timestamp': NOW2},
    {'type': 'dispense', 'company': 'unitrans', 'position': 'drivers',
     'med_name': 'X', 'quantity': 1, 'timestamp': NOW2},
])
opts = json.loads(c.get('/api/people-options').data)
check('a company seen once is offered from then on',
      'Letseng Diamonds' in opts['companies'], str(opts['companies'][:5]))
check('and so is a position', 'Shift Boss' in opts['positions'])
check('the standing list is still there',
      'MINOPEX' in opts['companies'] and 'Nurse' in opts['positions'])
check('the same name in another case is not offered twice',
      len([x for x in opts['companies'] if x.lower() == 'unitrans']) == 1,
      str([x for x in opts['companies'] if x.lower() == 'unitrans']))
check('both lists are sorted',
      opts['companies'] == sorted(opts['companies'], key=str.lower)
      and opts['positions'] == sorted(opts['positions'], key=str.lower))
check('the names are no longer written into the page',
      'var companyOptions = [];' in page_source('dispense.html'),
      'the lists should start empty and be fetched')
check('and the page fetches them',
      '/api/people-options' in page_source('dispense.html'))
check('it is behind the login like everything else',
      A.app.test_client().get('/api/people-options').status_code in (302, 401, 403))

print(f"\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========")
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
