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
keys = db['key_register']
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


def login(role='admin', name='L. Hlaoli'):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['key_register', 'audit_log']:
        db[col].delete_many({})


_real_local_now = A.local_now


def at(y, m, d, hh, mm):
    """Pin the pharmacy wall clock."""
    A.local_now = lambda: datetime(y, m, d, hh, mm)


def unpin():
    A.local_now = _real_local_now


TODAY = A.local_now().date()


OFFICER = 'Mathuto Kutoane'      # seeded, in charge
OFFICER2 = 'Mapalo Mapesela'     # seeded, in charge
NURSE_ASSISTANT = 'Mamosa Seetsa'  # seeded, explicitly NOT in charge


def issue(name=OFFICER, pharmacist='', comments=''):
    return c.post('/key-register', data={
        'action': 'issue_key', 'recipient_name': name,
        'pharmacist': pharmacist, 'comments': comments}, follow_redirects=True)


def officers():
    return db['key_officers']


def open_id():
    d = keys.find_one({'status': 'Out'})
    return str(d['_id']) if d else ''


def give_back(returning='', receiving='', pharmacist='', comments=''):
    return c.post('/key-register', data={
        'action': 'return_key', 'entry_id': open_id(),
        'returning_officer': returning, 'receiving_officer': receiving,
        'return_pharmacist': pharmacist, 'comments': comments},
        follow_redirects=True)


def view(**kw):
    qs = '&'.join(f'{k}={v}' for k, v in kw.items())
    return c.get('/key-register' + ('?' + qs if qs else '')).data.decode()


def text(html):
    return _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', ' ', html))


def register_table(body):
    """Just the register table.

    The state banner above it is a standing statement of where the key is and
    is deliberately NOT filtered, so an assertion about filtering has to look
    at the table alone.
    """
    i = body.index('<thead>')
    return body[i:body.index('</table>', i)]


def headers(body):
    # Scoped to the table: <thead> matches a naive <th[^>]*> pattern, and the
    # page's own stylesheet can mention a header cell in prose.
    return [h.strip() for h in
            _re.findall(r'<th(?:\s[^>]*)?>(.*?)</th>',
                        register_table(body), _re.S)]


def state_class(body):
    """The class on the rendered state banner div, not the stylesheet."""
    m = _re.search(r'<div class="key-state ([a-z-]*)"', body)
    return m.group(1) if m else None


def row_classes(body):
    return _re.findall(r'<tr class="([a-z-]*)"', register_table(body))


def banner(body):
    m = _re.search(r'<p class="message ([a-z]*)"[^>]*>(.*?)</p>', body, _re.S)
    return (m.group(1), _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', '', m.group(2))).strip()) \
        if m else (None, None)


print('--- 1. The page and the form it reproduces ---')
reset()
login()
body = view()
check('the page renders', 'Pharmacy Store Key Register' in body)
check('it carries the document reference', 'LD-HSE/NMC/PSKR/6.1.3.3' in body)
check('it carries the revision', 'Revision 2 of 2018' in body)
check('it names who is responsible', 'Responsible: SNO' in body)
check('it is in the nav', '/key-register' in body)
check('the register has the form\'s fourteen columns in order',
      headers(body)[:14]
      == ['Date Key given', 'Time', 'Pharmacist', 'Pharmacist signature',
          "Recipient's Name", "Recipient's signature", 'Date returned', 'Time',
          "Returning officer's name", "Returning officer's signature",
          'Receiving officer', "Receiving officer's signature",
          'Pharmacist signature', 'Comments'],
      str(headers(body)[:14]))
check('it prints landscape A4', 'size: A4 landscape' in body)
check('a sign-off line is provided', 'Checked by' in text(body))

print('\n--- 2. Issuing the key ---')
reset()
login()
at(2026, 10, 19, 8, 15)
r = issue(OFFICER)
check('an entry is created', keys.count_documents({}) == 1)
d = keys.find_one({})
check('the recipient is recorded', d['recipient_name'] == OFFICER)
check('the date comes from the clock, not typed',
      d['issued_date'] == datetime(2026, 10, 19), str(d['issued_date']))
check('the time comes from the clock', d['issued_time'] == '08:15',
      str(d.get('issued_time')))
check('the issuing pharmacist defaults to whoever is logged in',
      d['pharmacist'] == 'L. Hlaoli', str(d.get('pharmacist')))
check('it is open', d['status'] == 'Out')
check('nothing is returned yet', d['returned_date'] is None)
check('it has a reference', d['ref'].startswith('KEY-'))
check('it is audited',
      db['audit_log'].count_documents({'target_type': 'store_key',
                                       'action': 'CREATE'}) == 1)
cls, msg = banner(r.data.decode())
check('the banner confirms it', cls == 'success', f'{cls}: {msg}')
check('and names the time', '08:15' in msg, msg)

print('\n--- 3. The state banner says where the key is ---')
body = view()
check('the page says the key is OUT', 'is <strong>OUT</strong>' in body)
check('it names who has it', OFFICER in body)
check('it says how long it has been held', 'ago' in text(body))
check('the issue form is replaced by the return form',
      'value="return_key"' in body and 'value="issue_key"' not in body)

print('\n--- 4. One key cannot go to two people ---')
r = issue('P. Mokete')
check('a second issue is refused', keys.count_documents({}) == 1)
cls, msg = banner(r.data.decode())
check('and says who already holds it', OFFICER in msg, msg)
check('and says to record its return first', 'return before issuing' in msg
      or 'Record its return' in msg, msg)
check('the first holder still holds it',
      keys.find_one({'status': 'Out'})['recipient_name'] == OFFICER)

print('\n--- 5. A nameless issue is refused ---')
reset()
login()
at(2026, 10, 19, 8, 15)
r = c.post('/key-register', data={'action': 'issue_key',
                                  'recipient_name': '   '},
           follow_redirects=True)
check('the key is not issued to nobody', keys.count_documents({}) == 0)
check('and the refusal explains why', 'traceable' in text(r.data.decode()))

print('\n--- 6. Returning the key ---')
reset()
login()
at(2026, 10, 19, 8, 15)
issue(OFFICER)
at(2026, 10, 19, 12, 35)
r = give_back()
d = keys.find_one({})
check('the entry is closed', d['status'] == 'Returned')
check('the return date is recorded',
      d['returned_date'] == datetime(2026, 10, 19), str(d['returned_date']))
check('the return time comes from the clock', d['returned_time'] == '12:35')
check('the returning officer defaults to whoever took it',
      d['returning_officer'] == OFFICER, str(d.get('returning_officer')))
check('the receiving officer defaults to whoever is logged in',
      d['receiving_officer'] == 'L. Hlaoli')
check('the pharmacist column is filled too',
      d['return_pharmacist'] == 'L. Hlaoli')
check('the return is audited',
      db['audit_log'].count_documents({'target_type': 'store_key',
                                       'action': 'UPDATE'}) == 1)
cls, msg = banner(r.data.decode())
check('the banner reports how long it was out', '4h 20m' in msg, msg)
body = view()
check('the page now says the key is IN', 'is <strong>IN</strong>' in body)
check('and reports the last return', 'Last returned' in text(body))
check('the issue form is back', 'value="issue_key"' in body)

print('\n--- 7. Somebody else can bring it back ---')
reset()
login()
at(2026, 10, 19, 8, 0)
issue(OFFICER)
at(2026, 10, 19, 17, 0)
give_back(returning='T. Nkoe', receiving='A. Nurse', pharmacist='L. Hlaoli',
          comments='handed over at shift change')
d = keys.find_one({})
check('the returning officer can differ from the recipient',
      d['returning_officer'] == 'T. Nkoe' and d['recipient_name'] == OFFICER,
      f"{d.get('returning_officer')} / {d.get('recipient_name')}")
check('the receiving officer is recorded as given',
      d['receiving_officer'] == 'A. Nurse')
check('the comment is kept', 'shift change' in d['comments'])

print('\n--- 8. A key cannot be returned twice ---')
r = c.post('/key-register', data={'action': 'return_key',
                                  'entry_id': str(keys.find_one({})['_id'])},
           follow_redirects=True)
check('the second return is refused', 'already closed' in text(r.data.decode())
      or 'already recorded as returned' in text(r.data.decode()))
check('the first return is unchanged',
      keys.find_one({})['returning_officer'] == 'T. Nkoe')
r = c.post('/key-register', data={'action': 'return_key', 'entry_id': 'rubbish'},
           follow_redirects=True)
check('a malformed id is handled', r.status_code == 200)

print('\n--- 9. Held overnight on a weekday is flagged ---')
reset()
login()
at(2026, 10, 14, 16, 0)          # Wednesday
issue('Kept Overnight')
at(2026, 10, 15, 9, 0)           # Thursday morning, still out
body = view(start_date='2026-10-01', end_date='2026-10-19')
check('the banner turns red', state_class(body) == 'key-late', str(state_class(body)))
check('it says the due time has passed',
      'the key is overdue' in text(body))
check('the row is highlighted', 'key-overdue' in row_classes(body),
      str(row_classes(body)))
check('the held-for figure counts the hours across the night',
      '17h 0m ago' in text(body),
      text(body)[text(body).find('ago') - 60:][:80])
check('the register marks it as still out', 'still out' in text(body))
check('and counts it', 'not returned' in text(body))

print('\n--- 10. A key issued today is not flagged ---')
reset()
login()
at(2026, 10, 14, 8, 0)           # Wednesday
issue('Same Day')
at(2026, 10, 14, 15, 0)
body = view(start_date='2026-10-01', end_date='2026-10-19')
check('the banner is amber, not red', state_class(body) == 'key-out',
      str(state_class(body)))
check('the row is not highlighted',
      'key-overdue' not in row_classes(body), str(row_classes(body)))

print('\n--- 10b. The weekend handover is expected, not an alarm ---')
# He leaves Friday late morning and is back Monday midday. The key stays with
# whoever locks up. Flagging that every week would make the red meaningless.
check('a Friday issue is due back Monday midday',
      A._key_due_back(datetime(2026, 10, 16, 14, 0)) == datetime(2026, 10, 19, 12, 0),
      str(A._key_due_back(datetime(2026, 10, 16, 14, 0))))
check('a Saturday issue is due the same Monday',
      A._key_due_back(datetime(2026, 10, 17, 9, 0)) == datetime(2026, 10, 19, 12, 0))
check('a Sunday issue is due the same Monday',
      A._key_due_back(datetime(2026, 10, 18, 9, 0)) == datetime(2026, 10, 19, 12, 0))
for d, lbl in ((14, 'Wednesday'), (15, 'Thursday'), (19, 'Monday')):
    check(f'a {lbl} issue is due back the same day',
          A._key_due_back(datetime(2026, 10, d, 9, 0)).date()
          == datetime(2026, 10, d).date())
check('Friday, Saturday and Sunday count as a weekend hold',
      all(A._key_is_weekend_hold(datetime(2026, 10, d, 9, 0))
          for d in (16, 17, 18)))
check('Monday to Thursday do not',
      not any(A._key_is_weekend_hold(datetime(2026, 10, d, 9, 0))
              for d in (12, 13, 14, 15)))

reset()
login()
at(2026, 10, 16, 11, 25)         # Friday, as he is leaving
r = issue(OFFICER)
cls, msg = banner(r.data.decode())
check('the issue is called a weekend handover', 'weekend handover' in msg, msg)
check('and names when it is due back', 'Monday' in msg, msg)
check('the stored entry is marked a weekend hold',
      keys.find_one({})['weekend_hold'] is True)
check('and carries its due time',
      keys.find_one({})['due_back'] == datetime(2026, 10, 19, 12, 0),
      str(keys.find_one({}).get('due_back')))

at(2026, 10, 18, 10, 0)          # Sunday, still out
body = view(start_date='2026-10-01', end_date='2026-10-19')
check('over the weekend it is NOT red', state_class(body) == 'key-weekend',
      str(state_class(body)))
check('the banner calls it a weekend handover', 'weekend handover' in text(body))
check('the row is not flagged overdue',
      'key-overdue' not in row_classes(body), str(row_classes(body)))

at(2026, 10, 19, 11, 0)          # Monday, before he is back
body = view(start_date='2026-10-01', end_date='2026-10-19')
check('still not red before he returns', state_class(body) == 'key-weekend',
      str(state_class(body)))

at(2026, 10, 19, 12, 30)         # Monday, after he is back
body = view(start_date='2026-10-01', end_date='2026-10-19')
check('once he is back and it is still out, it turns red',
      state_class(body) == 'key-late', str(state_class(body)))
check('and the row is flagged', 'key-overdue' in row_classes(body))

print('\n--- 11. Signature columns print empty ---')
reset()
login()
at(2026, 10, 19, 8, 0)
issue(OFFICER)
at(2026, 10, 19, 12, 0)
give_back()
body = view(start_date='2026-10-01', end_date='2026-10-19')
row = [r for r in _re.findall(r'<tr[^>]*>.*?</tr>', body, _re.S)
       if OFFICER in r][0]
cells = _re.findall(r'<td[^>]*>(.*?)</td>', row, _re.S)
# Five of them now: the pharmacist signs the issue (4) and the return (13),
# which is what column 13 is for — it is not a second name column.
check('all five signature cells are blank',
      all(_re.sub(r'<[^>]+>|&nbsp;|\s', '', cells[i]) == ''
          for i in (3, 5, 9, 11, 12)),
      str([cells[i][:30] for i in (3, 5, 9, 11, 12)]))
check('the pharmacist signs the return as well as the issue',
      headers(body)[3] == 'Pharmacist signature'
      and headers(body)[12] == 'Pharmacist signature',
      str(headers(body)[3]) + ' / ' + str(headers(body)[12]))
check('and is named once, on the issue side',
      headers(body)[2] == 'Pharmacist'
      and 'Pharmacist' not in headers(body)[12:14])
check('who oversaw the return is still kept, on screen only',
      'return overseen by' in body)
check('the page says the pharmacist signs twice',
      'signs twice' in text(body))
check('the name cells either side are not blank',
      'L. Hlaoli' in cells[2] and OFFICER in cells[4])
check('the page says why they are blank',
      'does not record a signature it never saw' in text(body))
check('the signature tint is on body cells only, so headers stay readable',
      'td.key-sign { background' in body and '\n  .key-sign { background' not in body,
      'a tint on <th> would overpaint the header and hide its white text')

print('\n--- 12. Period filter and search ---')
reset()
login()
for day, who in ((5, 'Early Bird'), (12, 'Midmonth'), (19, 'Recent')):
    at(2026, 10, day, 8, 0)
    issue(who)
    at(2026, 10, day, 12, 0)
    give_back()
at(2026, 10, 19, 14, 0)
body = view(start_date='2026-10-10', end_date='2026-10-19')
check('entries before the period are excluded',
      'Early Bird' not in register_table(body))
check('entries inside it are shown',
      'Midmonth' in register_table(body) and 'Recent' in register_table(body))
body = view(start_date='2026-10-01', end_date='2026-10-19', q='Midmonth')
_t = register_table(body)
check('search narrows by name', 'Midmonth' in _t and 'Recent' not in _t)
check('but the state banner is not filtered away',
      'Recent' in body,
      'the banner states where the key is, whatever the filter says')
body = view(start_date='2026-10-01', end_date='2026-10-19',
            q='O%27Brien+%28snr%29')
check('a search with regex characters does not crash', '<table' in body)
body = view(start_date='nonsense', end_date='also-nonsense')
check('malformed dates fall back instead of crashing', '<table' in body)
body = view(start_date='2026-10-19', end_date='2026-10-01')
check('a reversed period is put the right way round',
      'Early Bird' in register_table(body)
      and 'Recent' in register_table(body))

print('\n--- 13. Access control ---')
reset()
login('admin')
at(2026, 10, 14, 8, 0)
issue(OFFICER)
# A rostered officer in charge who is not an admin: ordinary counter work.
login('pharmacist', OFFICER2)
check('a rostered officer can open the register',
      'Pharmacy Store Key Register' in view())
at(2026, 10, 14, 12, 0)
give_back()
check('a rostered officer can record a return',
      keys.find_one({})['status'] == 'Returned')
check('and is recorded as the receiving officer',
      keys.find_one({})['receiving_officer'] == OFFICER2,
      str(keys.find_one({}).get('receiving_officer')))
at(2026, 10, 14, 13, 0)
issue(OFFICER)
check('a rostered officer can issue the key', keys.count_documents({}) == 2)
check('a non-admin sees no delete control', 'delete_entry' not in view())
r = c.post('/key-register', data={'action': 'delete_entry',
                                  'entry_id': str(keys.find_one({'status': 'Out'})['_id'])},
           follow_redirects=True)
check('and cannot delete by posting directly', keys.count_documents({}) == 2)
check('the refusal is explained', 'Only admins' in text(r.data.decode()))
r = c.post('/key-register', data={'action': 'edit_entry',
                                  'entry_id': str(keys.find_one({'status': 'Out'})['_id']),
                                  'recipient_name': 'Someone Else'},
           follow_redirects=True)
check('and cannot correct an entry either',
      keys.find_one({'status': 'Out'})['recipient_name'] == OFFICER)
check('the refusal is explained', 'Only admins' in text(r.data.decode()))

login('admin')
check('an admin sees the delete control', 'delete_entry' in view())
r = c.post('/key-register', data={'action': 'delete_entry',
                                  'entry_id': str(keys.find_one({'status': 'Out'})['_id'])},
           follow_redirects=True)
check('an admin can delete an entry', keys.count_documents({}) == 1)
check('the deletion is audited',
      db['audit_log'].count_documents({'target_type': 'store_key',
                                       'action': 'DELETE'}) == 1)
check('deleting the open entry frees the key',
      'value="issue_key"' in view())

print('\n--- 13b. The Nurse Assistant may record but is not in charge ---')
reset()
login('admin')
at(2026, 10, 14, 8, 0)
check('she is on the roster', officers().find_one({'name': NURSE_ASSISTANT}) is not None)
check('but not as an officer in charge',
      officers().find_one({'name': NURSE_ASSISTANT})['in_charge'] is False)
check('so she is not offered as a key holder',
      NURSE_ASSISTANT not in A._officer_names(officers()))
check('while the officers in charge are',
      OFFICER in A._officer_names(officers()))

login('pharmacist', NURSE_ASSISTANT)
body = view()
check('she can open the register', 'Pharmacy Store Key Register' in body)
check('the page tells her why her name is not offered',
      'the roster does not have you as one' in text(body))
# Her name is on the page in the nav greeting, so look at the dropdown itself.
_sel = body[body.index('id="key-recipient"'):]
_sel = _sel[:_sel.index('</select>')]
check('her own name is not in the issue dropdown',
      NURSE_ASSISTANT not in _sel, _sel[:200])
check('the officers in charge are', OFFICER in _sel)

# Posting her own name as the issuing pharmacist must not take.
r = c.post('/key-register', data={'action': 'issue_key',
                                  'recipient_name': OFFICER,
                                  'pharmacist': ''}, follow_redirects=True)
check('issuing without naming an officer in charge is refused',
      keys.count_documents({}) == 0)
check('and says her own name cannot be used',
      'your own name cannot be used' in text(r.data.decode()).lower()
      or 'cannot be used' in text(r.data.decode()), text(r.data.decode())[:200])

# She CAN record it, naming the officer who actually handed it over.
r = c.post('/key-register', data={'action': 'issue_key',
                                  'recipient_name': OFFICER,
                                  'pharmacist': OFFICER2}, follow_redirects=True)
d = keys.find_one({})
check('she can record the issue by naming the real officer',
      d is not None and d['pharmacist'] == OFFICER2,
      str(d.get('pharmacist') if d else None))
check('and the entry records that SHE typed it',
      d['issued_by_user'] == NURSE_ASSISTANT, str(d.get('issued_by_user')))
check('who typed it is shown on screen', NURSE_ASSISTANT in view())
check('but it is not one of the printed columns',
      'Entered by' in view() and 'no-print' in view())

at(2026, 10, 14, 16, 0)
r = c.post('/key-register', data={'action': 'return_key',
                                  'entry_id': open_id(),
                                  'returning_officer': OFFICER,
                                  'receiving_officer': '',
                                  'return_pharmacist': ''},
           follow_redirects=True)
check('a return without an officer in charge is refused too',
      keys.find_one({})['status'] == 'Out')
r = c.post('/key-register', data={'action': 'return_key',
                                  'entry_id': open_id(),
                                  'returning_officer': OFFICER,
                                  'receiving_officer': OFFICER2,
                                  'return_pharmacist': OFFICER2},
           follow_redirects=True)
check('naming the officer lets the return through',
      keys.find_one({})['status'] == 'Returned')
check('and she is never recorded as the one in charge',
      NURSE_ASSISTANT not in (keys.find_one({})['receiving_officer'],
                              keys.find_one({})['return_pharmacist'],
                              keys.find_one({})['pharmacist']))

print('\n--- 13c. An admin corrects an entry ---')
reset()
login('admin')
at(2026, 10, 14, 8, 0)
issue(OFFICER)
at(2026, 10, 14, 12, 0)
give_back()
eid = str(keys.find_one({})['_id'])
r = c.post('/key-register', data={
    'action': 'edit_entry', 'entry_id': eid,
    'recipient_name': OFFICER2, 'comments': 'corrected after review'},
    follow_redirects=True)
d = keys.find_one({})
check('the name is corrected', d['recipient_name'] == OFFICER2)
check('the comment is stored', d['comments'] == 'corrected after review')
check('who corrected it is recorded', d['corrected_by'] == 'L. Hlaoli')
check('the correction is audited',
      db['audit_log'].count_documents(
          {'target_type': 'store_key', 'action': 'UPDATE'}) >= 1)
entry = db['audit_log'].find_one({'target_type': 'store_key', 'action': 'UPDATE'},
                                 sort=[('timestamp', -1)])
check('the audit records what it replaced',
      OFFICER in entry['changes'] and OFFICER2 in entry['changes'],
      entry['changes'][:160])
check('a correction that changes nothing says so',
      'Nothing was changed' in text(c.post('/key-register', data={
          'action': 'edit_entry', 'entry_id': eid,
          'recipient_name': OFFICER2}, follow_redirects=True).data.decode()))
r = c.post('/key-register', data={'action': 'edit_entry', 'entry_id': 'rubbish',
                                  'recipient_name': 'X'}, follow_redirects=True)
check('a malformed id is handled', r.status_code == 200)
body = view(edit=eid, start_date='2026-10-01', end_date='2026-10-19')
check('the edit panel opens from the register', 'Correct Entry' in body)
check('it is pre-filled with what is there', f'value="{OFFICER2}"' in body)

print('\n--- 13d. Off-roster holders are recorded but flagged ---')
reset()
login('admin')
at(2026, 10, 14, 8, 0)
r = issue('Visiting Locum')
d = keys.find_one({})
check('the key can still be issued to someone off the roster',
      d is not None and d['recipient_name'] == 'Visiting Locum')
check('the entry is flagged', d['recipient_off_roster'] is True)
cls, msg = banner(r.data.decode())
check('the banner is amber, not green', cls == 'partial', f'{cls}: {msg}')
check('and says they are not on the roster',
      'not on the roster' in msg, msg)
body = view(start_date='2026-10-01', end_date='2026-10-19')
check('the row is marked for review',
      'key-off-roster' in row_classes(body), str(row_classes(body)))
check('the banner carries the flag too', 'not on the roster as in charge' in text(body))
check('issuing to a rostered officer is NOT flagged',
      (reset() or login() or at(2026, 10, 14, 8, 0) or issue(OFFICER)) is not None
      and keys.find_one({})['recipient_off_roster'] is False)

print('\n--- 13e. The roster is admin-editable ---')
reset()
login('admin')
at(2026, 10, 14, 8, 0)
before = officers().count_documents({})
c.post('/key-register', data={'action': 'add_officer',
                              'officer_name': 'New Officer',
                              'officer_role': 'Nursing Officer',
                              'in_charge': 'yes'}, follow_redirects=True)
check('an admin can add someone', officers().count_documents({}) == before + 1)
check('and they become selectable', 'New Officer' in A._officer_names(officers()))
r = c.post('/key-register', data={'action': 'add_officer',
                                  'officer_name': 'New Officer'},
           follow_redirects=True)
check('a duplicate is refused', officers().count_documents({}) == before + 1)
check('and says so', 'already on the roster' in text(r.data.decode()))
oid = str(officers().find_one({'name': 'New Officer'})['_id'])
c.post('/key-register', data={'action': 'set_officer', 'officer_id': oid,
                              'in_charge': 'no', 'active': 'yes'},
       follow_redirects=True)
check('an admin can take someone out of charge',
      officers().find_one({'name': 'New Officer'})['in_charge'] is False)
check('and they stop being offered',
      'New Officer' not in A._officer_names(officers()))
check('the roster change is audited',
      db['audit_log'].count_documents({'target_type': 'key_officer'}) >= 2)
login('pharmacist', OFFICER2)
check('a non-admin does not see the roster editor',
      'add_officer' not in view())
r = c.post('/key-register', data={'action': 'add_officer',
                                  'officer_name': 'Sneaky'},
           follow_redirects=True)
check('and cannot change it by posting directly',
      officers().find_one({'name': 'Sneaky'}) is None)

print('\n--- 15. The register writes itself from the logins ---')
# Nobody fills a form in: the pharmacist logs out, whoever logs in next is
# holding the key, and he clicks Returned when he is back. These tests use
# real logins rather than an injected session, because the login IS the event.
from werkzeug.security import generate_password_hash

users = db['users']


def make_user(username, name, role):
    users.update_one({'username': username}, {'$set': {
        'username': username, 'name': name, 'role': role,
        'password_hash': generate_password_hash('pw'),
        'created_at': datetime.now(timezone.utc),
        'password_set_at': datetime.now(timezone.utc)}}, upsert=True)


def real_login(username):
    return c.post('/login', data={'username': username, 'password': 'pw'},
                  follow_redirects=False)


def real_logout():
    return c.get('/logout', follow_redirects=False)


def full_reset():
    for col in ['key_register', 'key_control', 'audit_log']:
        db[col].delete_many({})


make_user('pharm', 'L. Hlaoli', 'admin')
make_user('nurse1', OFFICER, 'employee')
make_user('nurse2', OFFICER2, 'employee')
make_user('na', NURSE_ASSISTANT, 'employee')

full_reset()
at(2026, 10, 16, 11, 25)         # Friday, he is leaving
real_login('pharm')
check('his own login does not issue the key to anybody',
      keys.count_documents({}) == 0, str(keys.count_documents({})))
real_logout()
check('logging out arms the handover',
      db['key_control'].find_one({'_id': 'state'})['armed'] is True)

at(2026, 10, 16, 11, 40)
real_login('nurse1')
d = keys.find_one({})
check('the next login writes the row by itself', d is not None)
check('that person is recorded as holding the key',
      d and d['recipient_name'] == OFFICER, str(d.get('recipient_name') if d else None))
check('the pharmacist who logged out is recorded as handing it over',
      d['pharmacist'] == 'L. Hlaoli', str(d.get('pharmacist')))
check('the time comes from the clock', d['issued_time'] == '11:40')
check('it is marked as having come from a login',
      d['auto_source'] == 'login', str(d.get('auto_source')))
check('it knows it is a weekend hold', d['weekend_hold'] is True)
check('and is due back when he returns',
      d['due_back'] == datetime(2026, 10, 19, 12, 0), str(d.get('due_back')))
check('the handover is disarmed once taken',
      db['key_control'].find_one({'_id': 'state'})['armed'] is False)
check('it is audited', db['audit_log'].count_documents(
    {'target_type': 'store_key', 'action': 'CREATE'}) == 1)

at(2026, 10, 16, 14, 0)
real_login('nurse1')
check('the same person logging in again changes nothing',
      keys.count_documents({}) == 1, str(keys.count_documents({})))

print('\n--- 15b. One row covers the whole absence ---')
at(2026, 10, 17, 8, 0)           # Saturday, second nurse comes on duty
real_login('nurse2')
check('the row is NOT split', keys.count_documents({}) == 1,
      str(keys.count_documents({})))
check('it stays open', keys.count_documents({'status': 'Out'}) == 1)
check('the key stays recorded to whoever took it when he left',
      keys.find_one({})['recipient_name'] == OFFICER,
      str(keys.find_one({}).get('recipient_name')))
check('nothing is closed off', keys.find_one({})['returned_date'] is None)
check('but the second person is noted against the same row',
      keys.find_one({})['also_on_duty'] == [OFFICER2],
      str(keys.find_one({}).get('also_on_duty')))
check('and that is audited', db['audit_log'].count_documents(
    {'target_type': 'store_key', 'action': 'UPDATE'}) >= 1)

at(2026, 10, 17, 14, 0)
real_login('nurse2')
check('the same person logging in twice is noted once',
      keys.find_one({})['also_on_duty'] == [OFFICER2],
      str(keys.find_one({}).get('also_on_duty')))
at(2026, 10, 18, 9, 0)
real_login('na')
check('a third person is added to the same list',
      keys.find_one({})['also_on_duty'] == [OFFICER2, NURSE_ASSISTANT],
      str(keys.find_one({}).get('also_on_duty')))
check('and still only one row exists', keys.count_documents({}) == 1)
login('admin')
body = view(start_date='2026-10-01', end_date='2026-10-19')
check('the register shows who else was on duty',
      'also on duty' in text(body) and OFFICER2 in text(body))

print('\n--- 15c. He returns and clicks once ---')
at(2026, 10, 19, 12, 5)          # Monday, he is back
real_login('pharm')
check('his login does NOT close the row by itself',
      keys.count_documents({'status': 'Out'}) == 1,
      'the key is his again when he says so, not when he opens the app')
body = c.get('/key-register').data.decode()
check('the page offers a single Returned button', 'Key Returned' in body)
check('with everything already filled in',
      f'value="{OFFICER}"' in body and 'value="L. Hlaoli"' in body)
r = c.post('/key-register', data={
    'action': 'return_key', 'entry_id': open_id(),
    'returning_officer': OFFICER, 'receiving_officer': 'L. Hlaoli',
    'return_pharmacist': 'L. Hlaoli'}, follow_redirects=True)
check('one click closes it', keys.count_documents({'status': 'Out'}) == 0)
check('the key reads as in', 'is <strong>IN</strong>' in c.get('/key-register').data.decode())
check('the whole weekend is one row on the register',
      keys.count_documents({}) == 1, str(keys.count_documents({})))
_row = keys.find_one({})
check('opened when he left', _row['issued_date'] == datetime(2026, 10, 16))
check('and closed when he got back',
      _row['returned_date'] == datetime(2026, 10, 19))
check('with the others still noted on it',
      OFFICER2 in (_row.get('also_on_duty') or []))

print('\n--- 15d. A forgotten logout is caught by the away window ---')
full_reset()
at(2026, 10, 16, 11, 45)         # Friday afternoon, he just closed the browser
check('nothing is armed', db['key_control'].find_one({'_id': 'state'}) is None)
real_login('nurse1')
check('the login is still recorded as taking the key',
      keys.count_documents({}) == 1, str(keys.count_documents({})))
check('and marked as having come from the away window',
      keys.find_one({})['auto_source'] == 'away-window')
check('the window is open on Friday afternoon',
      A._key_in_away_window(datetime(2026, 10, 16, 11, 30)) is True)
check('but not on Friday morning',
      A._key_in_away_window(datetime(2026, 10, 16, 11, 29)) is False)
check('it is open all Saturday and Sunday',
      A._key_in_away_window(datetime(2026, 10, 17, 3, 0))
      and A._key_in_away_window(datetime(2026, 10, 18, 23, 0)))
check('it closes when he gets back on Monday',
      A._key_in_away_window(datetime(2026, 10, 19, 11, 59)) is True
      and A._key_in_away_window(datetime(2026, 10, 19, 12, 0)) is False)
check('it is shut midweek',
      not any(A._key_in_away_window(datetime(2026, 10, d, 10, 0))
              for d in (13, 14, 15)))

full_reset()
at(2026, 10, 14, 9, 50)          # ordinary Wednesday, he is at work
real_login('pharm')
c.get('/key-register')           # and using the app
at(2026, 10, 14, 10, 0)
real_login('nurse1')
check('a nurse logging in midweek while he is working does NOT take the key',
      keys.count_documents({}) == 0,
      'he is there holding it')

print('\n--- 15d2. The weekday gap: he closed the browser instead of logging out ---')
# Reported: "Thapelo Mphole logged in but there was no record of exchange of
# keys." On a weekday nothing but the Logout button handed the key over, and
# closing the browser is not the Logout button.
full_reset()
at(2026, 10, 27, 7, 0)           # Tuesday morning
real_login('pharm')
c.get('/dispense')
_state = db['key_control'].find_one({'_id': 'state'})
check('his use of the app is noted', _state and _state.get('pharmacist_seen_at')
      == datetime(2026, 10, 27, 7, 0), str(_state))
check('with his name', _state.get('pharmacist_seen_by') == 'L. Hlaoli')

# Twenty-nine minutes later: he may just be at the shelf.
at(2026, 10, 27, 7, 29)
real_login('nurse1')
check('a login 29 minutes after he was last seen does not take the key',
      keys.count_documents({}) == 0, str(keys.count_documents({})))

# Thirty-five minutes later, and he never pressed Logout.
at(2026, 10, 27, 7, 35)
real_login('nurse2')
d = keys.find_one({})
check('a login after he has been idle 30 minutes takes the key', d is not None)
check('to the person who logged in',
      d and d['recipient_name'] == OFFICER2, str(d and d.get('recipient_name')))
check('from him, the pharmacist last seen',
      d and d['pharmacist'] == 'L. Hlaoli', str(d and d.get('pharmacist')))
check('marked as coming from his being idle, so it can be told apart',
      d and d['auto_source'] == 'pharmacist-idle', str(d and d.get('auto_source')))
check('the time is the login time', d and d['issued_time'] == '07:35')
check('it is audited', db['audit_log'].count_documents(
    {'target_type': 'store_key', 'action': 'CREATE'}) == 1)
login('admin')
check('the register says it came from their login',
      'from their login' in text(view(start_date='2026-10-01', end_date='2026-10-31')))

# He comes back. His login does not close it; one click does, as before.
at(2026, 10, 27, 9, 0)
real_login('pharm')
check('his return does not close the row by itself',
      keys.count_documents({'status': 'Out'}) == 1)

print('\n--- 15d3. The idle rule needs to know he was there ---')
full_reset()
at(2026, 10, 27, 7, 35)
real_login('nurse1')
check('with no record of him at all, a weekday login takes nothing',
      keys.count_documents({}) == 0,
      'the record only starts once he has used this version; until then '
      'the app does not know, and an unasked-for key row is worse than the gap')
# The noting is throttled to once a minute through his own session.
full_reset()
at(2026, 10, 27, 8, 0)
real_login('pharm')
_writes = []
_orig_note = A._key_note_pharmacist_seen
A._key_note_pharmacist_seen = lambda n: (_writes.append(n), _orig_note(n))
for _ in range(5):
    c.get('/dispense')
A._key_note_pharmacist_seen = _orig_note
check('five requests in the same minute write his activity at most once',
      len(_writes) <= 1, str(len(_writes)))

print('\n--- 15e. The Nurse Assistant case ---')
full_reset()
at(2026, 10, 16, 11, 25)
real_login('pharm')
real_logout()
at(2026, 10, 16, 11, 50)
real_login('na')
d = keys.find_one({})
check('her login records her as holding the key', d['recipient_name'] == NURSE_ASSISTANT)
check('because the register records what happened, not what should have',
      d['status'] == 'Out')
check('but the row is flagged for review', d['recipient_off_roster'] is True)
login('admin')
body = view(start_date='2026-10-01', end_date='2026-10-19')
check('and tinted in the register', 'key-off-roster' in row_classes(body),
      str(row_classes(body)))
check('the flag is visible on screen', 'not in charge' in text(body))
eid = str(d['_id'])
c.post('/key-register', data={'action': 'edit_entry', 'entry_id': eid,
                              'recipient_name': OFFICER,
                              'comments': 'key actually held by the officer on duty'},
       follow_redirects=True)
check('an admin can put it right', keys.find_one({})['recipient_name'] == OFFICER)
check('and the flag clears', keys.find_one({})['recipient_off_roster'] is False)
check('the correction is on the audit trail',
      db['audit_log'].count_documents({'target_type': 'store_key',
                                       'action': 'UPDATE'}) >= 1)

print('\n--- 15f. The register can never block a login ---')
full_reset()
_broken = A._key_on_login


def _explode(*a, **k):
    raise RuntimeError('database on fire')


A._key_on_login = _explode
at(2026, 10, 16, 12, 0)
r = real_login('nurse1')
check('a failure in the key hook does not stop anybody logging in',
      r.status_code in (302, 303), str(r.status_code))
with c.session_transaction() as sess:
    check('and the session is still established',
          sess.get('user', {}).get('name') == OFFICER,
          str(sess.get('user')))
A._key_on_login = _broken
unpin()

print('\n--- 14. Odds and ends ---')
reset()
login()
check('an empty register renders', 'Pharmacy Store Key Register' in view())
check('it says the key is in', 'is <strong>IN</strong>' in view())
check('it says there are no movements', 'No key movements' in text(view()))
r = c.post('/key-register', data={'action': 'nonsense'}, follow_redirects=True)
check('an unknown action is handled', r.status_code == 200)
check('the elapsed helper reads in minutes',
      A._held_for(datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 1, 8, 45)) == '45m')
check('the elapsed helper reads in hours',
      A._held_for(datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 1, 12, 30)) == '4h 30m')
check('the elapsed helper reads in days',
      A._held_for(datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 3, 11, 0)) == '2 days 3h')
check('a clock that runs backwards does not give a negative time',
      A._held_for(datetime(2026, 1, 2, 8, 0), datetime(2026, 1, 1, 8, 0)) == '0m')
unpin()

print('\n--- 16. The pharmacist can take his own key back ---')
# Reported from the live pharmacy: "I am trying to receive the key and it
# wants me to enter who is receiving it and my name is not in the options."
#
# The register had one flag, in_charge, meaning the nursing officer who holds
# the store key while the pharmacist is away. The pharmacist is correctly NOT
# one of those — he is the one handing it over — so the roster has him with
# the flag off. But the return form used that same flag to decide who could be
# named as RECEIVING the key back, which is exactly his job. He was locked out
# of his own register by a correct entry on the roster: no one-click button,
# and a dropdown of nursing officers with his own name missing.
#
# Everything here passed throughout, because every other check in this file
# signs in as "L. Hlaoli" — a name that is not on the roster, so the fallback
# to the admin role applied and the flag was never consulted. The real
# pharmacist is on it. So these use the seeded name, deliberately.
PHARMACIST = 'Letlotlo Hlaoli'          # seeded as Pharmacist, in_charge False
reset()
login('admin')
at(2026, 10, 14, 8, 0)
_doc = officers().find_one({'name': PHARMACIST})
check('the pharmacist is on the roster', _doc is not None)
check('and is NOT an officer in charge, which is right',
      _doc['in_charge'] is False)
check('so he is not offered as somebody the key is given to',
      PHARMACIST not in A._officer_names(officers()))
check('but he may be named as receiving it back',
      A._user_may_receive(officers(), PHARMACIST, False),
      'receiving the key back is the pharmacist\'s job')
check('and he is in the list of people who can be',
      PHARMACIST in A._key_receiver_names(officers()))
check('the officers in charge are still in that list too',
      OFFICER in A._key_receiver_names(officers()),
      'nobody who could name themselves before has lost the ability')
check('a doctor is in neither list',
      'Dr. T. Khothatso' not in A._key_receiver_names(officers())
      and 'Dr. T. Khothatso' not in A._officer_names(officers()))
check('nor is the nurse assistant',
      NURSE_ASSISTANT not in A._key_receiver_names(officers()))

# The key goes out over a weekend, and he comes back to take it in.
issue(OFFICER, pharmacist=PHARMACIST)
check('the key is out', keys.count_documents({'status': 'Out'}) == 1)

login('admin', PHARMACIST)
body = view()
check('he is offered the one-click return', 'Key Returned' in body,
      'this button was hidden from him entirely')
check('and is not told his name cannot be offered',
      'not offered as receiving officer' not in text(body))
_sel = body[body.index('name="receiving_officer"'):]
_sel = _sel[:_sel.index('</div>')]
check('the receiving officer is filled in with his own name, not a dropdown',
      f'value="{PHARMACIST}"' in _sel and '<select' not in _sel, _sel[:220])

r = give_back(returning=OFFICER, receiving=PHARMACIST, pharmacist=PHARMACIST)
_rec = keys.find_one({'status': 'Returned'})
check('the return is recorded', _rec is not None)
check('with him as the receiving officer',
      _rec and _rec.get('receiving_officer') == PHARMACIST,
      str(_rec and _rec.get('receiving_officer')))
check('and the officer who held it as the returning officer',
      _rec and _rec.get('returning_officer') == OFFICER)
check('the key is in again', keys.count_documents({'status': 'Out'}) == 0)

# The control that the flag was there for has to survive the fix.
login('pharmacist', NURSE_ASSISTANT)
issue(OFFICER, pharmacist=PHARMACIST)
body = view()
check('the nurse assistant still gets no one-click return',
      'Key Returned' not in body)
# The attribute sits inside the opening tag, so look just before it.
_at = body.index('name="receiving_officer"')
_sel = body[_at - 40:body.index('</select>', _at)]
check('she is still given a list rather than her own name', '<select' in _sel,
      _sel[:120])
check('and her name is not on it', NURSE_ASSISTANT not in _sel, _sel[:200])
check('while the pharmacist is', PHARMACIST in _sel)
unpin()

print('\n--- Default range: a year ---')
# Asked for: "Key register must default at a year range".
from datetime import date as _date
login()
_today = A.local_now().date()
_kp = c.get('/key-register').data.decode()
_want = A._months_back(_today, 12).strftime('%Y-%m-%d')
check('with no dates chosen the register starts a year back',
      f'name="start_date" type="date" value="{_want}"' in _kp,
      _re.findall(r'name="start_date"[^>]*>', _kp)[:1].__str__() if '_re' in dir() else '')
check('and ends today', f'to {_today:%Y-%m-%d}</h2>' in _kp)
_kp2 = c.get('/key-register?start_date=2026-01-05&end_date=2026-02-01').data.decode()
check('dates you choose are still used as given',
      'value="2026-01-05"' in _kp2 and '2026-01-05 to 2026-02-01' in _kp2)
check('months back keeps the day of the month', A._months_back(_date(2026, 11, 2), 12) == _date(2025, 11, 2))
check('a 29 February a year on lands on the 28th', A._months_back(_date(2029, 2, 28), 12) == _date(2028, 2, 28)
      and A._months_back(_date(2025, 2, 28), 12) == _date(2024, 2, 28))
check('and 31 May less three months is the end of February',
      A._months_back(_date(2026, 5, 31), 3) == _date(2026, 2, 28)
      and A._months_back(_date(2028, 5, 31), 3) == _date(2028, 2, 29))
check('across a year end', A._months_back(_date(2026, 1, 15), 3) == _date(2025, 10, 15))

print(f"\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========")
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
