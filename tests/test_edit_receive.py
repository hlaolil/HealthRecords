"""Correcting a receive line.

Asked: "why is it that edit receive has schedule instead of pack size?" The
edit form had been left behind when receiving moved to deliveries. Beyond the
schedule box it re-dated the receipt to the day of the edit, took the old
quantity off the shelf before checking the form, created a new medication
from a mistyped name, let a line's supplier and invoice drift from its
delivery, and kept no audit record. These checks hold it to the add-line rules.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo
from datetime import datetime, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
from test_support import arm_csrf
db = _f['pharmacy_db']
meds, txs, dels, audit = db['medications'], db['transactions'], db['deliveries'], db['audit_log']
c = A.app.test_client()
arm_csrf(c)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


with c.session_transaction() as s:
    s['user'] = {'name': 'Letlotlo Hlaoli', 'login': 'admin', 'role': 'admin'}
    s['last_active'] = datetime.now(timezone.utc).isoformat()

SEPT = datetime(2026, 9, 14, 9, 30)


def reset():
    for col in ('medications', 'transactions', 'deliveries', 'audit_log'):
        db[col].delete_many({})
    meds.insert_many([
        {'name': 'Amoxicillin caps, 500 mg', 'balance': 300, 'schedule': 'not controlled',
         'price': 0.5, 'pack_size': 100, 'pack_price': 50.0, 'batch': 'A1', 'expiry_date': '2028-01-31'},
        {'name': 'Morphine tabs, 10mg', 'balance': 40, 'schedule': 'controlled',
         'price': 2.0, 'pack_size': 10, 'pack_price': 20.0, 'batch': 'M1', 'expiry_date': '2027-06-30'},
    ])
    d = dels.insert_one({'reference': 'DEL-0914', 'supplier': 'NDSO', 'order_number': 'PO-77',
                         'invoice_number': 'INV-9', 'invoice_total': 50.0, 'order_total': 50.0,
                         'stock_receiver': 'L. Hlaoli', 'status': 'closed',
                         'opened_at': SEPT, 'received_by': 'L. Hlaoli'}).inserted_id
    rid = txs.insert_one({'type': 'receive', 'med_name': 'Amoxicillin caps, 500 mg', 'quantity': 100,
                          'batch': 'A1', 'price': 0.5, 'pack_size': 100, 'pack_price': 50.0,
                          'line_value': 50.0, 'expiry_date': '2028-01-31', 'schedule': 'not controlled',
                          'stock_receiver': 'L. Hlaoli', 'order_number': 'PO-77', 'supplier': 'NDSO',
                          'invoice_number': 'INV-9', 'delivery_id': str(d),
                          'delivery_reference': 'DEL-0914', 'user': 'L. Hlaoli',
                          'timestamp': SEPT}).inserted_id
    return str(rid)


def bal(n):
    return meds.find_one({'name': n})['balance']


def save(rid, **kw):
    data = {'med_name': 'Amoxicillin caps, 500 mg', 'quantity': '100', 'pack_size': '100',
            'pack_price': '50.00', 'batch': 'A1', 'expiry_date': '2028-01-31'}
    data.update({k: str(v) for k, v in kw.items()})
    return c.post(f'/edit-receive/{rid}', data=data, follow_redirects=True)


print('\n--- 1. The form matches Add a Line ---')
rid = reset()
page = c.get(f'/edit-receive/{rid}').data.decode()
form = page[page.index('Correct a Receive Line'):page.index('</form>', page.index('Correct a Receive Line'))]
check('it asks for pack size and price per pack', 'name="pack_size"' in form and 'name="pack_price"' in form)
check('filled in from the line', 'value="100" required' in form and 'value="50.00"' in form)
check('it no longer asks for a schedule', 'name="schedule"' not in form)
check('nor for a price per unit', 'name="price"' not in form)
check("a delivery line shows its delivery's details instead of boxes for them",
      'DEL-0914' in page and 'name="supplier"' not in form and 'name="invoice_number"' not in form)

print('\n--- 2. A correction ---')
r = save(rid, quantity=200, pack_price='48.00', batch='A2')
t = txs.find_one({'type': 'receive'})
check('the quantity is corrected and the shelf follows', t['quantity'] == 200 and bal('Amoxicillin caps, 500 mg') == 400)
check('the unit price is worked out from the pack', abs(t['price'] - 0.48) < 1e-9 and abs(t['line_value'] - 96.0) < 1e-9)
check('the date received is kept, not moved to today', t['timestamp'] == SEPT, str(t['timestamp']))
check('who received it is kept', t['user'] == 'L. Hlaoli' and t['edited_by'] == 'Letlotlo Hlaoli')
check('the old values are kept with the line',
      t['corrections'][-1]['changes']['quantity'] == {'from': 100, 'to': 200}
      and t['corrections'][-1]['changes']['batch'] == {'from': 'A1', 'to': 'A2'})
check('and it is in the audit log', audit.find_one({'target_id': 'DEL-0914', 'action': 'UPDATE'}) is not None)
check('the page says it was saved', 'corrected successfully' in r.data.decode())
check('the delivery details are unchanged', t['supplier'] == 'NDSO' and t['invoice_number'] == 'INV-9')
m = meds.find_one({'name': 'Amoxicillin caps, 500 mg'})
check("as the item's latest receipt, its batch and price on file follow", m['batch'] == 'A2' and m['pack_price'] == 48.0)
check('the schedule stays as the medication has it', t['schedule'] == 'not controlled')

print('\n--- 3. Nothing moves when the form is wrong ---')
rid = reset()
for bad, why in [({'quantity': ''}, 'blank quantity'), ({'quantity': 'ten'}, 'quantity in words'),
                 ({'pack_price': ''}, 'no pack price'), ({'expiry_date': '31/06/2027'}, 'bad date'),
                 ({'med_name': 'Amoxicilin caps'}, 'mistyped medication')]:
    r = save(rid, **bad)
    check(f'{why}: refused, and the shelf is untouched',
          bal('Amoxicillin caps, 500 mg') == 300 and txs.find_one({'type': 'receive'})['quantity'] == 100
          and 'Nothing was changed' in r.data.decode(), why)
check('a mistyped name never creates a medication', meds.count_documents({}) == 2)

print('\n--- 4. Moving a line to the right item ---')
rid = reset()
save(rid, med_name='Morphine tabs, 10mg', quantity=100, pack_size=10, pack_price='20.00')
check('the stock leaves the wrong item and lands on the right one',
      bal('Amoxicillin caps, 500 mg') == 200 and bal('Morphine tabs, 10mg') == 140)
check("the line takes the right item's schedule", txs.find_one({'type': 'receive'})['schedule'] == 'controlled')
rid = reset()
meds.update_one({'name': 'Amoxicillin caps, 500 mg'}, {'$set': {'balance': 30}})
r = save(rid, quantity=10)
check('a correction cannot take the shelf below zero',
      bal('Amoxicillin caps, 500 mg') == 30 and 'Nothing was changed' in r.data.decode())

print('\n--- 5. An older line ---')
rid = reset()
txs.insert_one({'type': 'receive', 'med_name': 'Amoxicillin caps, 500 mg', 'quantity': 50, 'batch': 'A9',
                'price': 0.6, 'pack_size': 100, 'pack_price': 60.0, 'expiry_date': '2029-01-31',
                'timestamp': datetime(2026, 10, 20)})
meds.update_one({'name': 'Amoxicillin caps, 500 mg'}, {'$set': {'batch': 'A9', 'pack_price': 60.0}})
save(rid, batch='A1-corrected')
m = meds.find_one({'name': 'Amoxicillin caps, 500 mg'})
check('correcting an old receipt does not put its batch back on file as current', m['batch'] == 'A9')

print('\n--- 6. A line from before deliveries ---')
reset()
lid = str(txs.insert_one({'type': 'receive', 'med_name': 'Morphine tabs, 10mg', 'quantity': 20,
                          'batch': 'M0', 'price': 2.0, 'expiry_date': '2027-01-31', 'schedule': 'controlled',
                          'stock_receiver': 'X', 'order_number': 'O1', 'supplier': 'S1',
                          'invoice_number': 'I1', 'timestamp': datetime(2025, 3, 1)}).inserted_id)
page = c.get(f'/edit-receive/{lid}').data.decode()
check('it keeps its own supplier and invoice boxes', 'name="supplier"' in page and 'name="invoice_number"' in page)
check('its price per pack is worked out from the old unit price', 'value="2.00"' in page)
c.post(f'/edit-receive/{lid}', data={'med_name': 'Morphine tabs, 10mg', 'quantity': '20', 'pack_size': '1',
       'pack_price': '2.00', 'batch': 'M0', 'expiry_date': '2027-01-31', 'stock_receiver': 'X',
       'order_number': 'O1', 'supplier': 'S2', 'invoice_number': 'I1'})
check('and they can be corrected', txs.find_one({'_id': A.ObjectId(lid)})['supplier'] == 'S2')

print(f"\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========")
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
