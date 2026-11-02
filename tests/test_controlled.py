"""The controlled drug register.

This is the one report in the app that is a legal document, and it was wrong.
Issuing morphine to a department made the register's running balance go UP,
because the direction of a movement was inferred from the sign stored on the
transaction — and those signs are not consistent. A dispense and an internal
issue are stored positive; a damage and an expiry removal are stored negative.
The register added anything that was not a receipt or a dispense.

Seeded with one real month, the register ended at 85 where the shelf said 55:
a thirty-unit discrepancy on a schedule 6 drug, with nothing on screen to say
so.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, csv, io, re as _re
from datetime import datetime, timedelta, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
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
MORPHINE = 'Morphine tabs, 10mg'


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


NOW = datetime.utcnow()


def seed(balance=55):
    reset()
    meds.insert_one({'name': MORPHINE, 'balance': balance, 'price': 126.55,
                     'expiry_date': '2028-03-31', 'batch': '90AUDA340',
                     'category': 'controlled', 'schedule': 'controlled',
                     'pack_size': 10, 'pack_price': 1265.5})


def movement(kind, qty, days_ago, **extra):
    doc = {'type': kind, 'med_name': MORPHINE, 'quantity': qty,
           'price': 126.55, 'user': 'Letlotlo Hlaoli',
           'timestamp': NOW - timedelta(days=days_ago)}
    doc.update(extra)
    txs.insert_one(doc)


def register(days=30):
    return c.post('/reports', data={
        'report_type': 'controlled_drug_register',
        'start_date': (NOW - timedelta(days=days)).strftime('%Y-%m-%d'),
        'end_date': NOW.strftime('%Y-%m-%d')}).data.decode()


print('\n--- 1. Every movement counts in the right direction ---')
# The signs stored on these four are deliberately a mixture, because that is
# what the database actually holds.
check('a receipt adds', A._controlled_delta(
    {'type': 'receive', 'quantity': 100}) == 100)
check('an opening balance adds', A._controlled_delta(
    {'type': 'opening_balance', 'quantity': 100}) == 100)
check('a dispense takes away, though it is stored positive',
      A._controlled_delta({'type': 'dispense', 'quantity': 20}) == -20,
      str(A._controlled_delta({'type': 'dispense', 'quantity': 20})))
check('an issue to a department takes away, though it is stored positive',
      A._controlled_delta({'type': 'internal_issue', 'quantity': 15}) == -15,
      'this is the one that made a morphine balance go UP')
check('an expiry removal takes away, and it is stored negative',
      A._controlled_delta({'type': 'expiry_removal', 'quantity': -10}) == -10,
      str(A._controlled_delta({'type': 'expiry_removal', 'quantity': -10})))
check('a damage write-off takes away',
      A._controlled_delta({'type': 'damage', 'quantity': -5}) == -5)
# An adjustment is the one movement whose sign IS the information.
check('an adjustment keeps its own sign, up',
      A._controlled_delta({'type': 'adjustment', 'quantity': 3}) == 3)
check('and down', A._controlled_delta({'type': 'adjustment', 'quantity': -3}) == -3)
check('the direction is stated by type, not guessed from the sign',
      set(A.CONTROLLED_DIRECTION) >= {'receive', 'dispense', 'internal_issue',
                                      'expiry_removal', 'damage', 'adjustment',
                                      'opening_balance'})

print('\n--- 2. A real month must reconcile with the shelf ---')
# Opening 100, dispensed 20, issued 15 to the Tx Room, 10 expired = 55 left.
seed(balance=55)
login()
movement('opening_balance', 100, 9)
movement('dispense', 20, 6, patient='Thabo Mokoena',
         prescriber='Dr. T. Khothatso', dispenser='Letlotlo Hlaoli')
movement('internal_issue', 15, 4, department='Tx Room', issued_to='Sr. Mokoena')
movement('expiry_removal', -10, 2, reason='Expired')

body = register()
rows = _re.findall(
    r'<tr>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<&]*)[^<]*</td>'
    r'\s*<td>([^<&]*)[^<]*</td>\s*<td>([^<]*)</td>', body)
balances = [r[4].strip() for r in rows]
check('every movement is listed', len(rows) == 4, str(rows))
check('the running balance ends where the shelf is',
      balances and balances[-1] == '55', str(balances))
check('issuing to a department takes the balance DOWN',
      balances[:3] == ['100', '80', '65'], str(balances)
      + ' — it used to read 100, 80, 95')
check('and the write-off takes it down again',
      balances == ['100', '80', '65', '55'], str(balances))
check('the register says it reconciles rather than leaving you to check',
      'do not add up' not in body)

print('\n--- 3. Nothing in the register carries a minus sign ---')
# One Quantity column showed "-10" for an expiry removal and "20" for a
# dispense, when both left the cupboard. In and Out instead.
check('there is an In column and an Out column',
      '<th>In</th>' in body and '<th>Out</th>' in body)
check('and no Quantity column to be ambiguous',
      '<th>Quantity</th>' not in body)
check('no figure in the register is negative',
      not _re.search(r'<td>-\d', body),
      str(_re.findall(r'<td>-\d+</td>', body)))

print('\n--- 4. A register reads in words, not in database types ---')
_table = body[body.index('<th>Movement</th>'):body.index('</table>',
                                                      body.index('<th>Movement</th>'))]
for machine, human in [('expiry_removal', 'Expired — removed'),
                       ('internal_issue', 'Issued to department'),
                       ('opening_balance', 'Opening balance'),
                       ('dispense', 'Dispensed to patient')]:
    check(f'"{machine}" is written as "{human}"',
          f'<td>{human}</td>' in _table and f'<td>{machine}</td>' not in _table,
          _table[:200])

print('\n--- 5. An impossible opening balance is flagged ---')
# The opening balance is worked backwards from the shelf, so the column
# always adds up — by construction. What it cannot do is start below zero:
# that means more left the cupboard than could ever have been in it.
# 100 came in, nothing was recorded going out, and the shelf holds 20. The
# opening balance works out at -80, which cannot have been true: 80 units
# left the cupboard with no record of it.
seed(balance=20)
login()
movement('receive', 100, 9, supplier='NDSO', invoice_number='INV-1')
body = register()
check('an opening balance below zero is flagged',
      'could have been in it' in body,
      body[body.find('Opening balance'):][:220])
check('and it says what to do about it', 'needs explaining' in body)
check('the figure itself is still shown rather than hidden',
      '-80' in body, body[body.find('Opening balance'):][:160])

seed(balance=55)
login()
movement('opening_balance', 100, 9)
movement('dispense', 45, 6, patient='Thabo Mokoena')
check('a register that CAN have happened is not flagged',
      'could have been in it' not in register())

print('\n--- 6. The page does not answer a question nobody asked ---')
# "No controlled drugs found or no data for this period" was the catch-all
# for the whole report chain, so it greeted you on the reports page before
# you had chosen any report at all.
seed()
login()
fresh = c.get('/reports').data.decode()
check('a fresh reports page says nothing about controlled drugs',
      'No controlled drugs' not in fresh)
check('nor about an empty report of any kind',
      'nothing in it for this period' not in fresh)
check('it just offers the report chooser',
      'name="report_type"' in fresh)

print('\n--- 7. A quiet period still shows the drug ---')
# A controlled drug that did not move is not an absent controlled drug.
body = register(days=5)
check('the drug is listed even though nothing moved', MORPHINE in body)
check('with its balance unchanged', 'Opening balance' in body and '55' in body)
check('and says plainly that nothing moved',
      'No transactions in this period' in body)

print('\n--- 8. The download is the register, not a row of blanks ---')
seed(balance=55)
login()
movement('opening_balance', 100, 9)
movement('dispense', 20, 6, patient='Thabo Mokoena')
movement('internal_issue', 15, 4, department='Tx Room', issued_to='Sr. Mokoena')
movement('expiry_removal', -10, 2, reason='Expired')
r = c.post('/reports', data={
    'report_type': 'controlled_drug_register', 'export': 'csv',
    'start_date': (NOW - timedelta(days=30)).strftime('%Y-%m-%d'),
    'end_date': NOW.strftime('%Y-%m-%d')})
check('it downloads', r.headers.get('Content-Type', '').startswith('text/csv'),
      r.headers.get('Content-Type'))
rows = list(csv.reader(io.StringIO(r.data.decode())))
check('the columns match the register on screen',
      rows[0] == ['Date', 'Medication', 'Movement', 'In', 'Out', 'Balance',
                  'Patient', 'By'], str(rows[0]))
body_rows = [x for x in rows[1:] if x and any(cell for cell in x)]
# The register is grouped by medication; handing those groups to the writer
# produced one blank line per medication and not a single movement.
check('there is one row per MOVEMENT, not one blank row per medication',
      len(body_rows) == 4, str(body_rows))
check('every row names its medication',
      all(x[1] == MORPHINE for x in body_rows), str(body_rows))
check('and carries a date', all(x[0] for x in body_rows), str(body_rows))
check('the running balance is in the file too',
      [x[5] for x in body_rows] == ['100', '80', '65', '55'],
      str([x[5] for x in body_rows]))
check('in and out are separate columns there as well',
      [x[3] for x in body_rows] == ['100', '', '', '']
      and [x[4] for x in body_rows] == ['', '20', '15', '10'],
      str([(x[3], x[4]) for x in body_rows]))

print('\n--- 9. The running balance depends on the rows being in time order ---')
# Found in a browser, not here: a register whose rows came back out of order
# read "Balance After: -4" on its first line and still ended on the right
# closing balance, because the column is a running total and the summary is
# not. Minus four of a schedule 6 drug is not a number that can appear on a
# legal document, and the per-row arithmetic is the only thing that catches
# it: the opening and closing figures were both correct.
#
# So these movements are inserted in deliberately scrambled order, and the
# register has to put them back.
seed(balance=55)
login()
movement('expiry_removal', -10, 2, reason='Expired')          # newest first
movement('opening_balance', 100, 9)                           # oldest third
movement('internal_issue', 15, 4, department='Tx Room', issued_to='Sr. Mokoena')
movement('dispense', 20, 6, patient='Thabo Mokoena',
         prescriber='Dr. T. Khothatso', dispenser='Letlotlo Hlaoli')

body = register()
rows = _re.findall(
    r'<tr>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<&]*)[^<]*</td>'
    r'\s*<td>([^<&]*)[^<]*</td>\s*<td>([^<]*)</td>', body)
check('all four movements are listed however they were written',
      len(rows) == 4, str(rows))
dates = [r[0].strip() for r in rows]
check('the register reads oldest first, whatever order they arrived in',
      dates == sorted(dates), str(dates))
check('the opening balance is the first line, not the third',
      rows and 'Opening balance' in rows[0][1], str([r[1] for r in rows]))

# Walk the column the way a pharmacist checking the book would: each line's
# balance must be the one above it, plus what came in, less what went out.
walk, ok_steps, detail = None, True, []
for date, movement_name, in_qty, out_qty, bal in rows:
    got = int(bal.strip())
    came_in = int(in_qty.strip()) if in_qty.strip().isdigit() else 0
    went_out = int(out_qty.strip()) if out_qty.strip().isdigit() else 0
    want = came_in - went_out if walk is None else walk + came_in - went_out
    detail.append(f'{movement_name.strip()}: {walk}+{came_in}-{went_out}'
                  f'={want} shown {got}')
    if want != got:
        ok_steps = False
    walk = got
check('every line is the line above it, plus in, less out', ok_steps,
      ' | '.join(detail))
check('and no line in the walk is negative',
      all(int(r[4].strip()) >= 0 for r in rows),
      str([r[4] for r in rows]))
check('the walk finishes on the balance the shelf says',
      walk == 55, str(walk))

print('\n--- 10. One clock, or the order above cannot be trusted ---')
# The scrambled order in the browser came from a seed that wrote local time
# while the app writes UTC: two hours of difference is enough to sort a
# morning's movements into the wrong sequence, and the register would read
# wrong without anything looking wrong. Every transaction write uses the
# same clock, and this is the check that keeps it that way.
_src = open('app.py').read()
_utc = _src.count("'timestamp': datetime.utcnow()")
_local = _src.count("'timestamp': local_now()")
check('transaction timestamps are written in UTC', _utc >= 10, str(_utc))
check('and not one of them uses the pharmacy wall clock', _local == 0,
      f'{_local} writes use local_now(), which is UTC+2 — a register '
      f'mixing the two sorts rows into the wrong order')
check('the register asks for them oldest first',
      ".sort('timestamp', 1)" in _src)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
