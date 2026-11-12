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
      _re.search(r'healing-hub-backup-\d{8}-\d{4}\.zip',
                 r.headers.get('Content-Disposition', '')) is not None,
      r.headers.get('Content-Disposition'))
z = zipfile.ZipFile(io.BytesIO(r.data))
names = z.namelist()
for col in ['medications', 'transactions', 'sales', 'temperature_logs',
            'audit_log', 'users']:
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

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
