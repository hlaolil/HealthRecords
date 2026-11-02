"""The diagnosis vocabulary.

A standard list only standardises anything if it is easier to hit than typing
something of your own. Before this, typing "hypertension" found nothing at
all — the entry is called HTN — and typing "cold" offered "Alcohol
intoxication" ahead of "Common cold", because matches came back in list order
rather than by how well they matched.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, json, re as _re
from datetime import datetime, timedelta, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
db = _f['pharmacy_db']
c = A.app.test_client()
# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
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
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


DX = A.DIAGNOSES_OPTIONS
SYN = A.DIAGNOSIS_SEARCH_TERMS

print('\n--- 1. The list itself ---')
check('nothing appears twice', len(DX) == len(set(DX)),
      str([x for x in set(DX) if DX.count(x) > 1]))
check('nor under a different case',
      len({x.lower() for x in DX}) == len(DX),
      str([x for x in DX if [y.lower() for y in DX].count(x.lower()) > 1]))
check('it is in alphabetical order, so typing does not jump about',
      DX == sorted(DX, key=str.lower))
check('nothing is blank or padded',
      all(x and x == x.strip() and '  ' not in x for x in DX),
      str([x for x in DX if not x or x != x.strip() or '  ' in x]))
check('every entry starts with a capital',
      all(x[0].isupper() or x[0].isdigit() for x in DX),
      str([x for x in DX if not (x[0].isupper() or x[0].isdigit())]))
check('slashes are spaced consistently',
      not any(_re.search(r'\S/|/\S', x) for x in DX),
      str([x for x in DX if _re.search(r'\S/|/\S', x)]))

print('\n--- 2. Body-system headings are not diagnoses ---')
# "ENT" or "Surgical" in the diagnosis column records nothing clinically.
for heading in ['ENT', 'Dental', 'Respiratory', 'Surgical', 'Psychiatric',
                'GIT', 'Musculoskeletal', 'Dermatological', 'Opthalmological',
                'Ophthalmological', 'CNS_PNS', 'CVS_Immunological',
                'Endocrinological', 'GUT_Urological_Gynae',
                'Rheumatological_Ortho']:
    check(f'"{heading}" is no longer offered as a diagnosis', heading not in DX)
check('nothing is left with an underscore in it',
      not any('_' in x for x in DX), str([x for x in DX if '_' in x]))

print('\n--- 3. The spellings that were wrong ---')
WAS_WRONG = {
    'Chelazion': 'Chalazion',
    'Rynaulds phenomenon': "Raynaud's phenomenon",
    'Myocardiac infaction/ ACS': 'Acute myocardial infarction',
    'Circumcission': 'Circumcision',
    'Ligmament sprain': 'Ligament sprain',
    'Dysfunctional uterine beeding': 'Dysfunctional uterine bleeding',
    'Antiphspholipid syndrome': 'Antiphospholipid syndrome',
    'Awating PCR results': 'Awaiting PCR results',
    'Cardiomegally': 'Cardiomegaly',
    'Chronic fatique syndrome': 'Chronic fatigue syndrome',
    'Smoke inhilation injury': 'Smoke inhalation injury',
    'Torticolis': 'Torticollis',
    'Planta fascitis': 'Plantar fasciitis',
    'Parasthesia': 'Paraesthesia',
    'Secondary ammenorrhoea': 'Secondary amenorrhoea',
    'Epididimorchitis': 'Epididymo-orchitis',
    'Acute urinary retension': 'Acute urinary retention',
    'Kaposis Sarcoma': "Kaposi's sarcoma",
    'Illio-tibial band syndrome': 'Iliotibial band syndrome',
    'Hallus valgus deformity': 'Hallux valgus deformity',
}
for wrong, right in WAS_WRONG.items():
    check(f'"{wrong}" is gone', wrong not in DX)
    check(f'...and "{right}" is there', right in DX)

print('\n--- 4. The old spellings still FIND the new entry ---')
# Records written before today keep the spelling they were written with, so
# the old words have to stay searchable — and a decade of muscle memory with
# them.
login()
for wrong, right in WAS_WRONG.items():
    got = A._diagnosis_matches(wrong)
    check(f'typing "{wrong}" still reaches it', right in got,
          str(got[:3]))

print('\n--- 5. Abbreviations are findable by their full name ---')
# None of these worked at all before: the entry is called HTN, so
# "hypertension" matched nothing.
BY_NAME = {
    'hypertension': 'HTN', 'diabetes': 'DM', 'tuberculosis': 'TB',
    'stroke': 'CVA', 'transient ischaemic attack': 'TIA',
    'deep vein thrombosis': 'DVT',
    'heart failure': 'CCF', 'peptic ulcer': 'PUD',
    'urinary tract infection': 'UTI', 'pelvic inflammatory disease': 'PID',
    'polycystic ovary': 'PCOS', 'benign prostatic hyperplasia': 'BPH',
    'diabetic ketoacidosis': 'DKA',
    'upper respiratory tract infection': 'URTI',
    'lower respiratory tract infection': 'LRTI',
    'chronic obstructive pulmonary disease': 'COPD',
    'sexually transmitted infection':
        'STI (MUS, VDS, herpes genitalia, syphilis etc)',
    'incision and drainage': 'I & D',
}
for typed, expect in BY_NAME.items():
    got = A._diagnosis_matches(typed)
    check(f'"{typed}" finds {expect}', expect in got, str(got[:3]))

print('\n--- 6. And by the everyday word for it ---')
PLAIN = {
    'shingles': 'Herpes zoster', 'nosebleed': 'Epistaxis',
    'hives': 'Urticaria', 'ringworm': 'Dermatophytosis [tinea]',
    'piles': 'Haemorrhoids',
    'heart attack': 'Acute myocardial infarction',
    'bunion': 'Hallux valgus deformity', 'reflux': 'Gastro-oesophageal reflux disease [GERD]',
    'sore throat': 'Acute tonsillitis and pharyngitis',
    'car accident': 'Injury (RTA)', 'pins and needles': 'Paraesthesia',
    'high blood pressure': 'HTN', 'back pain': 'Mechanical low back pain',
}
for typed, expect in PLAIN.items():
    got = A._diagnosis_matches(typed)
    check(f'"{typed}" finds {expect}', expect in got, str(got[:3]))

print('\n--- 7. The best match comes first ---')
check('"cold" offers a cold, not alcohol intoxication',
      'Alcohol intoxication' not in A._diagnosis_matches('cold')[:3],
      str(A._diagnosis_matches('cold')[:4]))
check('"hypertension" puts HTN first, not a pelvic condition',
      A._diagnosis_matches('hypertension')[0] == 'HTN',
      str(A._diagnosis_matches('hypertension')[:3]))
check('"tb" offers TB first', A._diagnosis_matches('tb')[0] == 'TB',
      str(A._diagnosis_matches('tb')[:3]))
check('a two-letter query does not match the middle of a word',
      not any('GERD' in d for d in A._diagnosis_matches('tb')),
      str(A._diagnosis_matches('tb')) + ' — "heartburn" contains "tb"')
check('an exact name is offered first',
      A._diagnosis_matches('Pneumonia')[0] == 'Pneumonia',
      str(A._diagnosis_matches('Pneumonia')[:3]))

print('\n--- 7b. The two entries that were not real diagnoses ---')
# "Ingrown" was ingrown WHAT, and "Pelvic hypertension" is not a term. Both
# were left alone until the pharmacist said what they meant, because a
# clinical record is not somewhere to put a guess.
check('"Ingrown" on its own is gone', 'Ingrown' not in DX)
check('and reads as a diagnosis now', 'Ingrown toenail' in DX)
check('"Pelvic hypertension" is gone', 'Pelvic hypertension' not in DX)
check('replaced by the real term', 'Pelvic congestion syndrome' in DX)
# Visits recorded under the old wording are untouched, so the old wording
# still has to find the entry.
check('the old wording still finds the toenail',
      'Ingrown toenail' in A._diagnosis_matches('ingrown'),
      str(A._diagnosis_matches('ingrown')[:3]))
check('and the old wording still finds the pelvic entry',
      'Pelvic congestion syndrome' in A._diagnosis_matches('pelvic hypertension'),
      str(A._diagnosis_matches('pelvic hypertension')[:3]))
check('"toenail" finds it too, which it never did before',
      'Ingrown toenail' in A._diagnosis_matches('toenail'),
      str(A._diagnosis_matches('toenail')[:3]))

print('\n--- 7c. The same thing is not recordable twice ---')
# Two entries for one condition split its own count in two, so neither
# figure is the figure.
check('"Dental decay" is gone, because it IS "Dental caries"',
      'Dental decay' not in DX and 'Dental caries' in DX)
check('and the old word still finds it',
      'Dental caries' in A._diagnosis_matches('dental decay'),
      str(A._diagnosis_matches('dental decay')[:3]))
check('the joined spine entry is gone',
      'Spondylosis / spondylolisthesis' not in DX)
check('because both halves were already listed',
      'Spondylosis' in DX and 'Spondylolisthesis' in DX)
check('and the old joined wording offers both',
      set(A._diagnosis_matches('spondylosis / spondylolisthesis')[:2])
      == {'Spondylosis', 'Spondylolisthesis'},
      str(A._diagnosis_matches('spondylosis / spondylolisthesis')[:3]))

print('\n--- 7d. Two diseases no longer share one line ---')
# The worst was Crohn's and ulcerative colitis filed under irritable bowel.
SPLITS = {
    'Inflammatory bowel disease / IBS':
        ('Inflammatory bowel disease', 'Irritable bowel syndrome'),
    'Epilepsy / seizure disorder': ('Epilepsy', 'Seizure, unspecified'),
    'TIA / CVA': ('TIA', 'CVA'),
    'PUD / gastritis': ('PUD', 'Gastritis'),
    'Anxiety disorder / panic disorder': ('Anxiety disorder', 'Panic disorder'),
}
for joined, (a, b) in SPLITS.items():
    check(f'"{joined}" is no longer one entry', joined not in DX)
    check(f'...{a} stands on its own', a in DX)
    check(f'...and so does {b}', b in DX)
    got = A._diagnosis_matches(joined)[:3]
    check(f'...and the old wording still offers both', a in got and b in got,
          str(got) + ' — every visit recorded before today says the old thing')

check('"HTN / DM" is gone, since both were already listed',
      'HTN / DM' not in DX and 'HTN' in DX and 'DM' in DX)
check('and the old joined wording offers both',
      set(A._diagnosis_matches('htn / dm')[:2]) == {'HTN', 'DM'},
      str(A._diagnosis_matches('htn / dm')[:3]))
# The obvious way to make that findable broke something else: the phrase
# "hypertension and diabetes" starts with "hypertension", so putting it on DM
# made typing "hypertension" offer DM ahead of HTN.
check('typing "hypertension" still reaches HTN first',
      A._diagnosis_matches('hypertension')[0] == 'HTN',
      str(A._diagnosis_matches('hypertension')[:3]))
check('and "diabetes" still reaches DM first',
      A._diagnosis_matches('diabetes')[0] == 'DM',
      str(A._diagnosis_matches('diabetes')[:3]))

print('\n--- 7e. What the pharmacy treats, it can record ---')
# Found by reading the medicine cupboard against this list: medicines on the
# shelf for conditions with no entry at all.
FROM_STOCK = {
    'Anti Snake Bite Serum': ('snake', 'Snake bite'),
    'Antirubbies': ('rabies', 'Rabies exposure'),
    'Tetanus Toxoid Vaccine': ('tetanus', 'Tetanus prophylaxis'),
    'Combined Pill and implants': ('contraception', 'Family planning'),
    'Emergency Pill': ('morning after', 'Emergency contraception'),
    'Naloxone': ('overdose', 'Overdose'),
    'Activated Charcoal': ('poisoning', 'Poisoning'),
    'Adrenalin injection': ('anaphylaxis', 'Anaphylaxis'),
    'Dressing packs': ('dressing', 'Wound care (dressing)'),
    'Oral Rehydration Salts': ('dehydrated', 'Dehydration'),
}
for med, (typed, expect) in FROM_STOCK.items():
    check(f'{med} has something to record it against', expect in DX)
    check(f'...reached by typing "{typed}"',
          expect in A._diagnosis_matches(typed),
          str(A._diagnosis_matches(typed)[:3]))

print('\n--- 7f. A mine has its own conditions ---')
OCCUPATIONAL = {
    'hearing loss': 'Noise-induced hearing loss',
    'silicosis': 'Pneumoconiosis',
    'heat exhaustion': 'Heat exhaustion',
    'arc eye': 'Welding flash (arc eye)',
    'scratched eye': 'Corneal abrasion',
    'bruise': 'Contusion',
    'acid burn': 'Chemical burn',
    'pre-employment': 'Fitness to work examination',
    'cut': 'Laceration',
    'graze': 'Abrasion',
}
for typed, expect in OCCUPATIONAL.items():
    check(f'"{typed}" finds {expect}',
          expect in A._diagnosis_matches(typed),
          str(A._diagnosis_matches(typed)[:3]))

print('\n--- 7g. Nothing new broke what was already there ---')
check('still in alphabetical order', DX == sorted(DX, key=str.lower))
check('still no duplicates', len(DX) == len(set(DX)))
check('nor any that differ only by case',
      len({x.lower() for x in DX}) == len(DX))
check('every search key still names a real entry', set(SYN) <= set(DX))
# One overlap is deliberate: the pharmacist keeps the cold group as it is.
check('the cold group is left as chosen',
      'Common cold' in DX and 'Acute nasopharyngitis [common cold]' in DX
      and 'URTI' in DX)

print('\n--- 7h. ICD naming, but only where it removes a real ambiguity ---')
# A diagnosis of "Ulcer" does not say whether it is gastric, oral, venous or
# corneal; "Injury" does not say what was injured. ICD will not let a title be
# that vague, and will not join two conditions with a slash.
check('nothing is joined with a slash any more',
      not any(' / ' in d for d in DX), str([d for d in DX if ' / ' in d]))
VAGUE = {
    'Injury': 'Injury, site unspecified',
    'Ulcer': 'Ulcer, site unspecified',
    'Fractures': 'Fracture, site unspecified',
    'Burns': 'Burn, site and degree unspecified',
    'Tumour': 'Neoplasm, nature unspecified',
    'Poor vision': 'Visual impairment, unspecified',
    'Abscess': 'Abscess, site unspecified',
    'Hepatitis': 'Hepatitis, unspecified',
}
for old, new in VAGUE.items():
    check(f'"{old}" now says what it does not know', old not in DX and new in DX,
          f'{old} -> {new}')
SLASHED = {
    'Dermatitis / eczema': 'Dermatitis and eczema',
    'Internal / external haemorrhoids': 'Haemorrhoids',
    'Feet corns / calluses': 'Corns and callosities',
    'Grief / bereavement': 'Bereavement reaction',
    'Pleuritic chest pain / pleuritis': 'Pleurisy',
    'Viral rhinitis / common cold': 'Acute nasopharyngitis [common cold]',
    'Fungal infection / tinea / dermatophyte': 'Dermatophytosis [tinea]',
}
for old, new in SLASHED.items():
    check(f'"{old}" reads as ICD would title it', old not in DX and new in DX,
          f'{old} -> {new}')

check('the abbreviations the pharmacy speaks in are left alone',
      all(a in DX for a in ['HTN', 'DM', 'TB', 'URTI', 'UTI', 'COPD', 'PUD']),
      'spelling them out would only make the register wider')

print('\n--- 7i. A rename must not split the history in two ---')
# Every visit before today says "Injury"; every visit after says "Injury, site
# unspecified". The records are not rewritten — they are clinical records — so
# the reports are told the pair is one condition.
for old, new in list(VAGUE.items()) + list(SLASHED.items()):
    check(f'a record saying "{old}" counts as {new}',
          A._canonical_diagnosis(old) == new,
          A._canonical_diagnosis(old))
    check(f'...and typing "{old}" still finds it',
          new in A._diagnosis_matches(old),
          str(A._diagnosis_matches(old)[:3]))
check('a diagnosis that was never renamed is left exactly as recorded',
      A._canonical_diagnosis('URTI') == 'URTI')
check('and so is something typed by hand that is on no list',
      A._canonical_diagnosis('Sore left knee') == 'Sore left knee')
check('an empty value does not become a condition',
      A._canonical_diagnosis('') == '' and A._canonical_diagnosis(None) is None)
check('matching ignores case and stray spaces',
      A._canonical_diagnosis('  injury  ') == 'Injury, site unspecified',
      A._canonical_diagnosis('  injury  '))
check('every old name maps to an entry that actually exists',
      all(v in DX for v in A.DIAGNOSIS_RENAMES.values()),
      str([v for v in A.DIAGNOSIS_RENAMES.values() if v not in DX]))
check('and no old name is still on the list under its old wording',
      not any(k in DX for k in A.DIAGNOSIS_RENAMES),
      str([k for k in A.DIAGNOSIS_RENAMES if k in DX]))

# The dashboard is the one place a diagnosis is counted, so that is where
# splitting the history would show.
reset_db = db['transactions']
reset_db.delete_many({})
_now = datetime.utcnow()
for n, (dxname, when) in enumerate([
        ('Injury', _now - timedelta(days=40)),          # recorded before
        ('Injury', _now - timedelta(days=35)),
        ('Injury, site unspecified', _now - timedelta(days=2))]):   # and after
    reset_db.insert_one({
        'type': 'dispense', 'transaction_id': f'T-{n}', 'patient': f'P{n}',
        'med_name': 'X', 'quantity': 1, 'diagnoses': [dxname],
        'outcome': 'Outpatient', 'timestamp': when})
_rows = list(reset_db.find({'type': 'dispense'}))
_counted = {}
for t in _rows:
    for d in t.get('diagnoses') or []:
        k = A._canonical_diagnosis(d)
        _counted[k] = _counted.get(k, 0) + 1
check('three visits under two wordings count as one condition, three times',
      _counted == {'Injury, site unspecified': 3}, str(_counted))
# Leave the collection as this block found it, or the next section picks up
# these seeded visits instead of the one it writes for itself.
reset_db.delete_many({'transaction_id': {'$in': ['T-0', 'T-1', 'T-2']}})
check('and this check cleans up after itself',
      reset_db.count_documents({'type': 'dispense'}) == 0,
      str(reset_db.count_documents({'type': 'dispense'})))

print('\n--- 8. The endpoint behind the box ---')
r = c.get('/api/diagnoses?query=hypertension')
check('it answers', r.status_code == 200, str(r.status_code))
got = json.loads(r.data)
check('with the standard entry', 'HTN' in got, str(got))
check('capped so the list stays usable',
      len(json.loads(c.get('/api/diagnoses?query=a').data))
      <= A.DIAGNOSIS_SUGGESTION_LIMIT)
check('an empty query offers nothing rather than everything',
      json.loads(c.get('/api/diagnoses?query=').data) == [])
check('it is behind the login',
      A.app.test_client().get('/api/diagnoses?query=tb').status_code in (302, 401, 403))
check('every search key names a real entry',
      set(SYN) <= set(DX), str(set(SYN) - set(DX)))
check('no search term is blank',
      all(t and t == t.strip() for v in SYN.values() for t in v))

print('\n--- 9. Anything not on the list can still be recorded ---')
# A case that does not fit the vocabulary still has to be recordable; the
# alternative is a register that is tidy and wrong.
db['medications'].insert_one(
    {'name': 'Paracetamol tabs, 500 mg', 'balance': 100, 'price': 0.18,
     'expiry_date': '2029-03-31', 'batch': 'B', 'category': 'general',
     'pack_size': 1, 'pack_price': 0.18})
login()
c.post('/dispense', data={
    'patient': 'Thabo Mokoena', 'company': 'Letseng', 'position': 'Driver',
    'birth_decade': '1980', 'gender': 'Male', 'outcome': 'Outpatient',
    'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
    'date': A.local_now().strftime('%Y-%m-%d'),
    'med_names': 'Paracetamol tabs, 500 mg', 'quantities': '1',
    'diagnoses': 'Something entirely new'}, follow_redirects=True)
saved = db['transactions'].find_one({'type': 'dispense'})
check('a diagnosis that is not on the list is still recorded',
      saved and 'Something entirely new' in (saved.get('diagnoses') or []),
      str(saved.get('diagnoses') if saved else None))

body = c.get('/dispense').data.decode()
_flat = _re.sub(r'\s+', ' ', body)
check('and the form says how to hit the standard list',
      'standard list' in _flat and 'short form' in _flat, _flat[:120])
check('while saying an off-list diagnosis is still allowed',
      'can still be typed' in _flat)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
