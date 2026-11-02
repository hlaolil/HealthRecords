"""Backups, one staff roster, and finding the visits that were recorded twice.

These came out of a scan for the same classes of fault already found in this
app: a list kept in two places drifts, and a record nobody can get out of the
database is a record that can be lost.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, json, csv, io, zipfile, re as _re
from datetime import datetime, timedelta, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
db = _f['pharmacy_db']
meds, txs = db['medications'], db['transactions']
officers = db['key_officers']
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
    for col in db.list_collection_names():
        db[col].delete_many({})


NOW = datetime.utcnow()

print('\n--- 1. The whole register comes out in one file ---')
# Every other download answers one question. None of them is the register.
# Years of dispensing and the controlled drug movements live in one cluster,
# and an Atlas free tier takes no backups of its own.
reset()
login()
meds.insert_one({'name': "TDF/3TC/DTG 300/300/50mg 30's", 'balance': 40,
                 'price': 0.0, 'expiry_date': '2028-04-30', 'batch': 'N/A'})
txs.insert_one({'type': 'dispense', 'patient': "O'Brien", 'quantity': 10,
                'med_name': 'Paracetamol tabs, 500 mg', 'price': 0.18,
                'diagnoses': ['URTI', 'Fever'], 'timestamp': datetime(2026, 3, 4, 17, 5),
                'nested': {'a': [1, {'b': 2}]}})
r = c.get('/backup')
check('it downloads', r.headers.get('Content-Type') == 'application/zip',
      r.headers.get('Content-Type'))
check('named for the day and time it was taken',
      _re.search(r'nmc-pharmacy-backup-\d{8}-\d{4}\.zip',
                 r.headers.get('Content-Disposition', '')) is not None,
      r.headers.get('Content-Disposition'))
z = zipfile.ZipFile(io.BytesIO(r.data))
names = z.namelist()
for col in ['medications', 'transactions', 'temperature_logs', 'key_register',
            'loans', 'audit_log', 'users']:
    check(f'{col} is in the archive', f'{col}.jsonl' in names, str(names))
check('with a README for whoever has to use it', 'README.txt' in names)
check('and a manifest saying what is in it', 'manifest.json' in names)

man = json.loads(z.read('manifest.json'))
check('the manifest counts the records', man['counts']['transactions'] == 1,
      str(man['counts']))
check('and says who took it', man['taken_by'] == 'Letlotlo Hlaoli')

print('\n--- 2. A backup that cannot be read back is not a backup ---')
lines = [l for l in z.read('transactions.jsonl').decode().splitlines() if l.strip()]
check('every line is a whole record on its own', len(lines) == 1)
doc = json.loads(lines[0])
check('an apostrophe in a name survives', doc['patient'] == "O'Brien",
      str(doc['patient']))
check('a date comes back as a date, not a string of today',
      doc['timestamp'] == {'$date': '2026-03-04T17:05:00Z'}, str(doc['timestamp']))
check('the id is kept so a record can be restored in place',
      isinstance(doc.get('_id'), dict) and '$oid' in doc['_id'], str(doc.get('_id')))
check('nested structures are not flattened',
      doc['nested'] == {'a': [1, {'b': 2}]}, str(doc['nested']))
check('the README says how to put it back',
      'mongoimport' in z.read('README.txt').decode())
check('and warns that it holds patient names',
      'confidential' in z.read('README.txt').decode().lower())

print('\n--- 3. Who may take one, and what it records ---')
check('taking a backup is itself audited',
      db['audit_log'].count_documents({'target_type': 'database_backup'}) == 1)
login(role='user', name='Mamosa Seetsa')
r = c.get('/backup', follow_redirects=True)
check('a non-admin cannot download the whole database',
      not r.headers.get('Content-Type', '').startswith('application/zip'),
      r.headers.get('Content-Type'))
check('and is told why', 'administrator' in r.data.decode())
login()

print('\n--- 4. One roster, not four lists of the same people ---')
# The same people were listed four times: prescribers and dispensers written
# into the dispensing page, the key register roster, and the login accounts.
# They already disagreed.
reset()
login()
c.get('/key-register')
check('the roster seeds itself', officers.count_documents({}) == len(A.STAFF_SEED),
      str(officers.count_documents({})))
pres = A._staff_named_for(db, 'prescriber')
disp = A._staff_named_for(db, 'dispenser')
# Exactly the people the two hardcoded lists held, so nothing changed about
# who may do what — only where it is written down.
check('the prescribers are who they were', pres == [
    'Dr. T. Khothatso', 'Locum Doctor', 'Locum Nurse', 'Malesoetsa Leohla',
    'Mamosa Seetsa', 'Mamosaase Nqosa', 'Mapalo Mapesela', 'Mathuto Kutoane',
    'Thapelo Mphole'], str(pres))
check('and the dispensers are who they were', disp == [
    'Letlotlo Hlaoli', 'Locum Nurse', 'Locum Pharmacist', 'Malesoetsa Leohla',
    'Mamosa Seetsa', 'Mamosaase Nqosa', 'Mapalo Mapesela', 'Mathuto Kutoane',
    'Thapelo Mphole'], str(disp))
check('a doctor prescribes but does not dispense',
      'Dr. T. Khothatso' in pres and 'Dr. T. Khothatso' not in disp)
check('a pharmacist dispenses but does not prescribe',
      'Letlotlo Hlaoli' in disp and 'Letlotlo Hlaoli' not in pres)
check('the names are no longer written into the page',
      "{% set prescribers = [" not in page_source('dispense.html')
      and "{% set dispensers = [" not in page_source('dispense.html'))

print('\n--- 5. Adding somebody once puts her everywhere ---')
r = c.post('/key-register', data={
    'action': 'add_officer', 'officer_name': 'Nthabiseng Moerane',
    'officer_role': 'Nursing Officer', 'in_charge': 'yes',
    'prescriber': 'yes', 'dispenser': 'yes'}, follow_redirects=True)
check('she is added', 'added to the roster' in r.data.decode())
check('and may prescribe', 'Nthabiseng Moerane' in A._staff_named_for(db, 'prescriber'))
check('and dispense', 'Nthabiseng Moerane' in A._staff_named_for(db, 'dispenser'))
check('and hold the key',
      'Nthabiseng Moerane' in A._officer_names(officers))
body = c.get('/dispense').data.decode()
_pres_box = _re.search(r'name="prescriber".*?</select>', body, _re.S).group(0)
check('she appears on the dispensing form without any code change',
      'Nthabiseng Moerane' in _pres_box)

oid = officers.find_one({'name': 'Nthabiseng Moerane'})['_id']
c.post('/key-register', data={
    'action': 'set_officer', 'officer_id': str(oid), 'in_charge': 'no',
    'prescriber': 'no', 'dispenser': 'no', 'active': 'no'},
    follow_redirects=True)
check('marking her inactive takes her out of every list at once',
      'Nthabiseng Moerane' not in A._staff_named_for(db, 'prescriber')
      and 'Nthabiseng Moerane' not in A._staff_named_for(db, 'dispenser'))
check('the change is audited',
      db['audit_log'].count_documents(
          {'target_type': 'key_officer', 'action': 'UPDATE'}) >= 1)

print('\n--- 6. A roster already in use is not overwritten ---')
# This runs on a pharmacy that has been using the app for months. Seeding
# must never undo a decision an administrator has already made.
reset()
officers.insert_one({'name': 'Malesoetsa Leohla', 'role': 'Nursing Officer',
                     'in_charge': False, 'active': False})
A._seed_key_officers(officers)
again = officers.find_one({'name': 'Malesoetsa Leohla'})
check('somebody set inactive stays inactive', again['active'] is False,
      str(again))
check('and not in charge stays not in charge', again['in_charge'] is False)
check('but the new job flags are filled in', 'prescriber' in again)
check('the people who were missing are added',
      officers.count_documents({}) == len(A.STAFF_SEED))
A._seed_key_officers(officers)
check('seeding twice changes nothing',
      officers.count_documents({}) == len(A.STAFF_SEED))

print('\n--- 7. Visits that look like one visit recorded twice ---')
# A partial dispense used to leave the whole visit in the form, so pressing
# Dispense again — the natural thing when a form looks unsubmitted —
# dispensed the same medicines a second time.
reset()
login()
meds.insert_one({'name': 'Paracetamol tabs, 500 mg', 'balance': 100,
                 'price': 0.18, 'expiry_date': '2029-03-31', 'batch': 'B',
                 'category': 'general', 'pack_size': 1, 'pack_price': 0.18})


def visit(tid, patient, qty, when, med='Paracetamol tabs, 500 mg'):
    txs.insert_one({'type': 'dispense', 'transaction_id': tid, 'patient': patient,
                    'med_name': med, 'quantity': qty, 'price': 0.18,
                    'line_value': qty * 0.18, 'dispenser': 'Letlotlo Hlaoli',
                    'outcome': 'Outpatient', 'timestamp': when})


visit('V-1', 'Thabo Mokoena', 10, NOW - timedelta(minutes=32))
visit('V-2', 'Thabo Mokoena', 10, NOW - timedelta(minutes=30))   # the duplicate
visit('V-3', 'Palesa Nkoe', 5, NOW - timedelta(minutes=20))
visit('V-3', 'Palesa Nkoe', 5, NOW - timedelta(minutes=20))      # one prescription
visit('V-4', 'Lineo Phiri', 7, NOW - timedelta(days=3))
visit('V-5', 'Lineo Phiri', 7, NOW - timedelta(days=1))          # a real repeat
visit('V-6', 'Mpho Letsie', 4, NOW - timedelta(minutes=9))
visit('V-7', 'Mpho Letsie', 6, NOW - timedelta(minutes=8))       # different dose

found = A._duplicate_visits(txs)
check('the double-dispense is found', len(found) == 1,
      str([(d['patient'], d['minutes']) for d in found]))
if found:
    d = found[0]
    check('it names the patient', d['patient'] == 'Thabo Mokoena', d['patient'])
    check('and how far apart the two were', d['minutes'] == 2.0, str(d['minutes']))
    check('and what it cost in stock', d['units'] == 10 and round(d['value'], 2) == 1.8,
          f"{d['units']} units, {d['value']}")
check('two lines of ONE prescription are not a duplicate',
      not any(d['patient'] == 'Palesa Nkoe' for d in found))
check('a genuine repeat visit days later is not a duplicate',
      not any(d['patient'] == 'Lineo Phiri' for d in found))
check('the same patient at a different dose is not a duplicate',
      not any(d['patient'] == 'Mpho Letsie' for d in found))

body = c.post('/reports', data={'report_type': 'duplicate_visits'}).data.decode()
check('the report renders', 'Possible Double-Dispensing' in body)
check('it names the visit to check', 'Thabo Mokoena' in body)
check('it says what it would cost if they are all duplicates',
      'counted out of stock' in body)
check('and it does not pretend to be certain', 'it is not a verdict' in body)
check('it says how to correct one', 'find the later' in body)

r = c.post('/reports', data={'report_type': 'duplicate_visits', 'export': 'csv'})
check('and it downloads', r.headers.get('Content-Type', '').startswith('text/csv'))
rows = list(csv.reader(io.StringIO(r.data.decode())))
check('with the columns a pharmacist needs',
      rows[0] == ['Patient', 'Medication', 'Quantity', 'First recorded',
                  'Recorded again', 'Minutes apart', 'Units counted twice',
                  'Value counted twice', 'Dispenser'], str(rows[0]))

print('\n--- 8. A clean record says so ---')
reset()
login()
check('no duplicates found when there are none', A._duplicate_visits(txs) == [])
body = c.post('/reports', data={'report_type': 'duplicate_visits'}).data.decode()
check('and the page says that is the answer you want',
      'the answer you want' in body)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
