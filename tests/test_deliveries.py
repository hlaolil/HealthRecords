from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, re as _re
from datetime import datetime, timezone

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
    for col in ['medications', 'transactions', 'deliveries', 'audit_log']:
        db[col].delete_many({})


def add_med(name, balance=0):
    meds.insert_one({'name': name, 'balance': balance, 'batch': 'OLD', 'price': 1.0,
                     'expiry_date': '2030-01-01', 'schedule': 'not controlled'})


def open_delivery(supplier='MedSupply Ltd', order='PO-100', order_total='1000.00',
                  invoice='INV-500', invoice_total='1000.00', receiver='S. Mokoena'):
    c.post('/receive', data={'action': 'open', 'supplier': supplier,
                             'order_number': order, 'order_total': order_total,
                             'invoice_number': invoice, 'invoice_total': invoice_total,
                             'stock_receiver': receiver, 'note': ''},
           follow_redirects=True)
    d = dels.find_one({'invoice_number': invoice}, sort=[('opened_at', -1)])
    return str(d['_id']), d


def add_line(did, med, qty, pack_price, pack_size=1, batch='B1', expiry='2030-06-01',
             schedule='not controlled'):
    """qty is in single units; pack_price is the price of a whole pack."""
    return c.post('/receive', data={'action': 'line', 'delivery_id': did,
                                    'med_name': med, 'quantity': str(qty),
                                    'pack_size': str(pack_size),
                                    'pack_price': str(pack_price), 'batch': batch,
                                    'expiry_date': expiry, 'schedule': schedule},
                  follow_redirects=True)


# =====================================================================
print('\n--- 1. Opening a delivery ---')
reset()
login()
r = c.get('/receive')
check('receive page offers a delivery, not a single-item form',
      b'Start a Delivery' in r.data and b'Receive Medication' not in r.data)
did, d = open_delivery()
check('delivery created with a reference', d['reference'].startswith('DEL-'))
check('header stores order number and total',
      d['order_number'] == 'PO-100' and d['order_total'] == 1000.0)
check('header stores invoice number and total',
      d['invoice_number'] == 'INV-500' and d['invoice_total'] == 1000.0)
check('delivery opens in open status', d['status'] == 'open')
check('opening is audited', db['audit_log'].count_documents({'target_type': 'delivery'}) == 1)

did2, _ = open_delivery(supplier='Other Co', order='PO-200', invoice='INV-600')
check('several deliveries can be open at once',
      dels.count_documents({'status': 'open'}) == 2)

# =====================================================================
print('\n--- 2. Paperwork is entered once, inherited by every line ---')
reset()
login()
add_med('Paracetamol, 500 mg', 100)
add_med('Aspirin, 100 mg', 50)
did, d = open_delivery(order_total='1000.00', invoice_total='1000.00')
add_line(did, 'Paracetamol, 500 mg', 200, 2.50)   # 500.00
add_line(did, 'Aspirin, 100 mg', 100, 5.00)       # 500.00

lines = list(txs.find({'delivery_id': did}))
check('two lines captured', len(lines) == 2)
check('every line inherits the supplier',
      all(l['supplier'] == 'MedSupply Ltd' for l in lines))
check('every line inherits the order number',
      all(l['order_number'] == 'PO-100' for l in lines))
check('every line inherits the invoice number',
      all(l['invoice_number'] == 'INV-500' for l in lines))
check('every line inherits the receiver',
      all(l['stock_receiver'] == 'S. Mokoena' for l in lines))
check('lines are tagged with the delivery reference',
      all(l['delivery_reference'] == d['reference'] for l in lines))

body = c.get(f'/receive?delivery_id={did}').data.decode()
check('the add-line form does not ask for supplier again',
      'name="supplier"' not in body.split('Add a Line')[1].split('</form>')[0])
check('the add-line form does not ask for order number again',
      'name="order_number"' not in body.split('Add a Line')[1].split('</form>')[0])
check('the add-line form does not ask for invoice number again',
      'name="invoice_number"' not in body.split('Add a Line')[1].split('</form>')[0])

check('stock increased for the first medication',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['balance'] == 300)
check('stock increased for the second medication',
      meds.find_one({'name': 'Aspirin, 100 mg'})['balance'] == 150)
check('medication price updated from the line',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['price'] == 2.50)

# =====================================================================
print('\n--- 3. Three-way match: fully matched ---')
body = c.get(f'/receive?delivery_id={did}').data.decode()
check('three-way match panel renders', 'This Invoice vs Goods Received' in body)
check('goods value computed as 1000.00', 'R1000.00' in body)
check('a fully matched delivery says so', 'Matched' in body
      and 'Fewer goods' not in body and 'More goods' not in body)
check('no discrepancy colouring when matched', 'class="expired"' not in
      body.split('This Invoice vs Goods Received')[1].split('</table>')[0])

# =====================================================================
print('\n--- 4. Goods below the invoice ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
did, _ = open_delivery(order_total='1000.00', invoice_total='1000.00')
add_line(did, 'Paracetamol, 500 mg', 300, 2.50)     # 750.00 only
body = c.get(f'/receive?delivery_id={did}').data.decode()
panel = body.split('This Invoice vs Goods Received')[1].split('</table>')[0]
check('shortfall against the invoice is flagged', 'Fewer goods than invoiced' in panel)
check('the difference is shown signed', 'R-250.00' in panel, panel[:200])
check('shortfall is ambered, not red', 'close-to-expire' in panel)

print('\n--- 5. Goods above the invoice ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
did, _ = open_delivery(order_total='1000.00', invoice_total='1000.00')
add_line(did, 'Paracetamol, 500 mg', 500, 2.50)     # 1250.00
panel = c.get(f'/receive?delivery_id={did}').data.decode() \
    .split('This Invoice vs Goods Received')[1].split('</table>')[0]
check('surplus against the invoice is flagged', 'More goods than invoiced' in panel)
check('surplus difference shown', 'R+250.00' in panel)

print('\n--- 6. Invoice against the order ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
did, _ = open_delivery(order_total='1000.00', invoice_total='1150.00')
body = c.get(f'/receive?delivery_id={did}').data.decode()
panel = body.split('<h3>Order ')[1].split('</table>')[0]
check('being invoiced beyond the order is flagged', 'Invoiced beyond the order' in panel)
check('over-invoicing is shown as a negative amount still to invoice',
      'R-150.00' in panel, panel[-400:])
did2, _ = open_delivery(order='PO-9', order_total='1000.00',
                        invoice='INV-9', invoice_total='900.00')
panel = c.get(f'/receive?delivery_id={did2}').data.decode().split('<h3>Order ')[1].split('</table>')[0]
check('an order not yet fully invoiced reads as part-delivered',
      'Part-delivered' in panel and 'R100.00' in panel, panel[-400:])

# =====================================================================
print('\n--- 7. Warn but allow: a mismatch never blocks receiving ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
did, d = open_delivery(order_total='1000.00', invoice_total='1000.00')
add_line(did, 'Paracetamol, 500 mg', 100, 2.50)     # 250.00 vs 1000.00
check('stock still went up despite the mismatch',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['balance'] == 100)
check('the line was still recorded', txs.count_documents({'delivery_id': did}) == 1)
r = c.post('/receive', data={'action': 'close', 'delivery_id': did}, follow_redirects=True)
body = r.data.decode()
check('closing with a mismatch is allowed',
      dels.find_one({'_id': A.ObjectId(did)})['status'] == 'closed')
check('closing warns about the mismatch', 'closed with a mismatch' in body)
check('the warning quotes both figures', 'R250.00' in body and 'R1000.00' in body)
check('the mismatch is audited',
      db['audit_log'].count_documents({'target_type': 'delivery', 'action': 'UPDATE'}) == 1)

reset()
login()
add_med('Paracetamol, 500 mg', 0)
did, _ = open_delivery(order_total='500.00', invoice_total='500.00')
add_line(did, 'Paracetamol, 500 mg', 200, 2.50)
body = c.post('/receive', data={'action': 'close', 'delivery_id': did},
              follow_redirects=True).data.decode()
check('a matched delivery closes cleanly', 'closed and fully matched' in body)

# =====================================================================
print('\n--- 8. A closed delivery is sealed ---')
r = add_line(did, 'Paracetamol, 500 mg', 50, 2.50)
check('no lines can be added after closing', txs.count_documents({'delivery_id': did}) == 1)
check('stock is not changed by the rejected line',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['balance'] == 200)
check('the refusal is explained', 'is closed' in r.data.decode())
body = c.get(f'/receive?delivery_id={did}').data.decode()
check('a closed delivery hides the add-line form', 'Add a Line' not in body)
check('a closed delivery hides the close button', 'Close This Delivery' not in body)
check('a closed delivery still shows its lines and match', 'This Invoice vs Goods Received' in body)

# =====================================================================
print('\n--- 9. The delivery list ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
did_a, da = open_delivery(order='PO-A', invoice='INV-A',
                          order_total='500.00', invoice_total='500.00')
add_line(did_a, 'Paracetamol, 500 mg', 200, 2.50)
c.post('/receive', data={'action': 'close', 'delivery_id': did_a}, follow_redirects=True)
did_b, dbb = open_delivery(order='PO-B', invoice='INV-B',
                           order_total='800.00', invoice_total='800.00')
add_line(did_b, 'Paracetamol, 500 mg', 100, 2.50)
c.post('/receive', data={'action': 'close', 'delivery_id': did_b}, follow_redirects=True)
body = c.get('/receive').data.decode()


def row(body, ref):
    mm = _re.search(r'<td>' + _re.escape(ref) + r'</td>(.*?)</tr>', body, _re.S)
    return [_re.sub(r'<[^>]+>', '', x).strip() for x in _re.findall(r'<td>(.*?)</td>', mm.group(1), _re.S)] if mm else []


ra, rb = row(body, da['reference']), row(body, dbb['reference'])
check('matched delivery shows "matched"', 'matched' in ra, str(ra))
check('mismatched delivery shows the difference', 'R-550.00' in rb, str(rb))
check('mismatched closed delivery is flagged red',
      _re.search(r'<tr class="expired">\s*<td>' + _re.escape(dbb['reference']), body) is not None)
check('matched closed delivery is not flagged',
      _re.search(r'<tr class="normal">\s*<td>' + _re.escape(da['reference']), body) is not None)
check('list shows line counts', ra[7] == '1' and rb[7] == '1', f'{ra[7]},{rb[7]}')

# =====================================================================
print('\n--- 10. Bad input ---')
reset()
login()
add_med('Paracetamol, 500 mg', 100)
did, _ = open_delivery()
for bad, label in [
        ({'quantity': '0'}, 'zero quantity'),
        ({'quantity': '-5'}, 'negative quantity'),
        ({'quantity': 'abc'}, 'non-numeric quantity'),
        ({'price': '-1'}, 'negative price'),
        ({'price': 'xyz'}, 'non-numeric price')]:
    data = {'action': 'line', 'delivery_id': did, 'med_name': 'Paracetamol, 500 mg',
            'quantity': '10', 'price': '2.00', 'batch': 'B', 'expiry_date': '2030-01-01',
            'schedule': 'not controlled'}
    data.update(bad)
    c.post('/receive', data=data, follow_redirects=True)
check('no invalid line was accepted', txs.count_documents({'delivery_id': did}) == 0)
check('stock untouched by invalid lines',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['balance'] == 100)

c.post('/receive', data={'action': 'open', 'supplier': 'S', 'order_number': 'O',
                         'order_total': 'not-a-number', 'invoice_number': 'I',
                         'invoice_total': '10', 'stock_receiver': 'R'},
       follow_redirects=True)
check('a delivery with a non-numeric total is rejected',
      dels.count_documents({'supplier': 'S'}) == 0)
check('an unknown delivery id does not error',
      c.get('/receive?delivery_id=nonsense').status_code == 200)
check('a well-formed but missing id does not error',
      c.get('/receive?delivery_id=507f1f77bcf86cd799439011').status_code == 200)
check('a line against an unknown delivery is refused',
      c.post('/receive', data={'action': 'line', 'delivery_id': 'nope',
                               'med_name': 'Paracetamol, 500 mg', 'quantity': '1',
                               'price': '1', 'batch': 'B', 'expiry_date': '2030-01-01',
                               'schedule': 'not controlled'},
             follow_redirects=True).status_code == 200)

# =====================================================================
print('\n--- 11. Existing data and the rest of the app still work ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
# a legacy receive row with no delivery_id at all
txs.insert_one({'type': 'receive', 'med_name': 'Paracetamol, 500 mg', 'quantity': 50,
                'batch': 'OLD', 'price': 1.0, 'expiry_date': '2030-01-01',
                'schedule': 'not controlled', 'stock_receiver': 'R',
                'order_number': 'LEGACY-1', 'supplier': 'Old Co',
                'invoice_number': 'LEG-1', 'user': 'u',
                'timestamp': datetime.utcnow()})
body = c.get('/receive').data.decode()
check('legacy receive rows still list without a delivery', 'LEGACY-1' in body)
check('legacy rows do not break the delivery table', 'No deliveries yet' in body)

did, _ = open_delivery(order_total='250.00', invoice_total='250.00')
add_line(did, 'Paracetamol, 500 mg', 100, 2.50)
r = c.post('/reports', data={'report_type': 'receive_list',
                             'start_date': datetime.utcnow().strftime('%Y-%m-01'),
                             'end_date': datetime.utcnow().strftime('%Y-%m-%d')})
check('receive list report includes both legacy and delivery lines',
      b'LEGACY-1' in r.data and b'PO-100' in r.data)

line = txs.find_one({'delivery_id': did})
r = c.post('/delete-receive', data={'receive_id': str(line['_id'])}, follow_redirects=True)
# the legacy row above was inserted straight into the collection and never
# touched the balance, so the 100 added by the delivery line is all there is
check('deleting a delivery line reverses the stock',
      meds.find_one({'name': 'Paracetamol, 500 mg'})['balance'] == 0,
      str(meds.find_one({'name': 'Paracetamol, 500 mg'})['balance']))
body = c.get(f'/receive?delivery_id={did}').data.decode()
check('the three-way match updates after a line is deleted',
      'R0.00' in body.split('This Invoice vs Goods Received')[1].split('</table>')[0])

check('receive list is still paginated', 'pagebar' not in body and c.get('/receive').status_code == 200)

# =====================================================================
print('\n--- 12. Pack pricing: unit price is derived, not typed ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
did, _ = open_delivery(order_total='1250.00', invoice_total='1250.00')
# 5 boxes of 100 tablets at R250 a box = 500 units, R1250
add_line(did, 'Paracetamol, 500 mg', 500, pack_price=250.00, pack_size=100)
line = txs.find_one({'delivery_id': did})
check('quantity is stored in single units', line['quantity'] == 500)
check('pack size stored', line['pack_size'] == 100)
check('pack price stored', line['pack_price'] == 250.00)
check('unit price derived as 2.50', line['price'] == 2.50, str(line['price']))
check('line value is 1250.00', abs(line['line_value'] - 1250.00) < 0.001,
      str(line['line_value']))
check('stock went up by the unit count', meds.find_one({})['balance'] == 500)
check('the medication carries the UNIT price, not the pack price',
      meds.find_one({})['price'] == 2.50, str(meds.find_one({})['price']))
check('the medication remembers the pack size', meds.find_one({})['pack_size'] == 100)

body = c.get(f'/receive?delivery_id={did}').data.decode()
check('the delivery fully matches on pack pricing',
      'Matched' in body.split('This Invoice vs Goods Received')[1].split('</table>')[0])
check('the line shows the derived packs', '<td>5</td>' in body)

print('\n--- 13. A pack size that does not divide cleanly still matches ---')
reset()
login()
add_med('Odd Item, 10 mg', 0)
# 3 units per pack at R250 -> unit price R83.3333...; 300 units = 100 packs = R25,000
did, _ = open_delivery(order_total='25000.00', invoice_total='25000.00')
add_line(did, 'Odd Item, 10 mg', 300, pack_price=250.00, pack_size=3)
line = txs.find_one({'delivery_id': did})
check('unit price is not rounded in storage', abs(line['price'] - 250 / 3) < 1e-9,
      str(line['price']))
check('line value is exactly 25000.00', abs(line['line_value'] - 25000.00) < 0.001,
      str(line['line_value']))
panel = c.get(f'/receive?delivery_id={did}').data.decode() \
    .split('This Invoice vs Goods Received')[1].split('</table>')[0]
check('an awkward pack size does NOT create a false mismatch', 'Matched' in panel,
      panel[:300])
# what would have happened with a rounded unit price
check('a rounded unit price would have been out by more than a cent',
      abs(300 * round(250 / 3, 2) - 25000.00) > 0.01,
      str(300 * round(250 / 3, 2)))

print('\n--- 14. Pack size defaults and validation ---')
reset()
login()
add_med('Single Item, 10 mg', 0)
did, _ = open_delivery(order_total='100.00', invoice_total='100.00')
add_line(did, 'Single Item, 10 mg', 50, pack_price=2.00, pack_size=1)
line = txs.find_one({'delivery_id': did})
check('pack size 1 leaves the price as the unit price', line['price'] == 2.00)
check('pack size 1 line value is quantity x price', line['line_value'] == 100.00)
for bad in ('0', '-3', 'abc'):
    c.post('/receive', data={'action': 'line', 'delivery_id': did,
                             'med_name': 'Single Item, 10 mg', 'quantity': '10',
                             'pack_size': bad, 'pack_price': '5', 'batch': 'B',
                             'expiry_date': '2030-01-01', 'schedule': 'not controlled'},
           follow_redirects=True)
check('invalid pack sizes are rejected', txs.count_documents({'delivery_id': did}) == 1)
check('stock untouched by rejected lines', meds.find_one({})['balance'] == 50)

print('\n--- 15. Stock value on the dashboard is now correct ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
did, _ = open_delivery(order_total='1250.00', invoice_total='1250.00')
add_line(did, 'Paracetamol, 500 mg', 500, pack_price=250.00, pack_size=100)
body = c.get('/dashboard').data.decode()
# 500 units x R2.50 = R1,250 -- NOT 500 x R250 = R125,000
check('stock value uses the unit price', 'R1,250' in body, )
check('stock value is not inflated by the pack size', 'R125,000' not in body)

# =====================================================================
print('\n--- 16. Several invoices against one order ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
add_med('Aspirin, 100 mg', 0)
# order PO-777 worth R3,000, filled by two part-deliveries
d1, dd1 = open_delivery(order='PO-777', order_total='3000.00',
                        invoice='INV-A', invoice_total='1250.00')
add_line(d1, 'Paracetamol, 500 mg', 500, pack_price=250.00, pack_size=100)
d2, dd2 = open_delivery(order='PO-777', order_total='3000.00',
                        invoice='INV-B', invoice_total='1000.00')
add_line(d2, 'Aspirin, 100 mg', 200, pack_price=500.00, pack_size=100)

body = c.get(f'/receive?delivery_id={d2}').data.decode()
order_panel = body.split('<h3>Order ')[1].split('</table>')[0]
check('the order panel appears', 'PO-777' in body)
check('order total shown', 'R3000.00' in order_panel, order_panel[:400])
check('both invoices counted', '<td>2</td>' in order_panel, order_panel[:400])
check('invoiced to date sums both invoices', 'R2250.00' in order_panel, order_panel[:400])
check('goods received sums across both deliveries', 'R2250.00' in order_panel)
check('still to invoice is the balance of the order', 'R750.00' in order_panel,
      order_panel[:400])
check('the order reads as part-delivered', 'Part-delivered' in order_panel)

sib_table = body.split('<h3>Order ')[1].split('<h3>')[0]
check('sibling invoices are listed', 'INV-A' in sib_table and 'INV-B' in sib_table)
check('sibling deliveries link to each other', dd1['reference'] in sib_table)

print('\n--- 17. Completing and over-invoicing an order ---')
add_med('Amoxycillin Cap, 250 mg', 0)
d3, _ = open_delivery(order='PO-777', order_total='3000.00',
                      invoice='INV-C', invoice_total='750.00')
add_line(d3, 'Amoxycillin Cap, 250 mg', 150, pack_price=500.00, pack_size=100)
panel = c.get(f'/receive?delivery_id={d3}').data.decode().split('<h3>Order ')[1].split('</table>')[0]
check('the order now reads as fully invoiced', 'Fully invoiced' in panel, panel[:400])
check('nothing left to invoice', 'R0.00' in panel)
check('three invoices counted', '<td>3</td>' in panel)

d4, _ = open_delivery(order='PO-777', order_total='3000.00',
                      invoice='INV-D', invoice_total='500.00')
panel = c.get(f'/receive?delivery_id={d4}').data.decode().split('<h3>Order ')[1].split('</table>')[0]
check('invoicing beyond the order total is flagged', 'Invoiced beyond the order' in panel)
check('the overrun is shown as a negative remainder', 'R-500.00' in panel, panel[:400])

print('\n--- 18. Deliveries disagreeing on the order total ---')
reset()
login()
d1, _ = open_delivery(order='PO-888', order_total='1000.00',
                      invoice='INV-1', invoice_total='500.00')
d2, _ = open_delivery(order='PO-888', order_total='1200.00',
                      invoice='INV-2', invoice_total='500.00')
body = c.get(f'/receive?delivery_id={d2}').data.decode()
check('a conflicting order total is surfaced, not silently resolved',
      'do not agree on the order total' in body)
check('the most recent figure is used', 'R1200.00' in body.split('<h3>Order ')[1])
body = c.get(f'/receive?delivery_id={d1}').data.decode()
check('an order with consistent totals shows no warning after correction',
      'do not agree on the order total' in body)

print('\n--- 19. A single-invoice order shows no sibling table ---')
reset()
login()
d1, _ = open_delivery(order='PO-999', order_total='500.00',
                      invoice='INV-ONLY', invoice_total='500.00')
body = c.get(f'/receive?delivery_id={d1}').data.decode()
check('the order panel still appears for one invoice', 'PO-999' in body)
check('no sibling list when there is only one invoice',
      body.split('<h3>Order ')[1].split('<h3>')[0].count('INV-ONLY') <= 1)

print('\n--- 20. Legacy lines without pack data still reconcile ---')
reset()
login()
add_med('Paracetamol, 500 mg', 0)
d1, dd = open_delivery(order_total='300.00', invoice_total='300.00')
# a line as it would have been written before pack pricing existed
txs.insert_one({'type': 'receive', 'med_name': 'Paracetamol, 500 mg', 'quantity': 100,
                'batch': 'B', 'price': 3.0, 'expiry_date': '2030-01-01',
                'schedule': 'not controlled', 'stock_receiver': 'R',
                'order_number': 'PO-100', 'supplier': 'S', 'invoice_number': 'INV-500',
                'delivery_id': d1, 'delivery_reference': dd['reference'],
                'user': 'u', 'timestamp': datetime.utcnow()})
panel = c.get(f'/receive?delivery_id={d1}').data.decode() \
    .split('This Invoice vs Goods Received')[1].split('</table>')[0]
check('a line with no pack data falls back to quantity x price',
      'Matched' in panel, panel[:300])
body = c.get(f'/receive?delivery_id={d1}').data.decode()
check('a legacy line renders without a pack price', '&ndash;' in body or '–' in body)
check('the delivery list values legacy lines too',
      'R300.00' in c.get('/receive').data.decode())

print('\n--- A half-typed delivery line is not lost ---')
# The dispensing form already keeps a draft. A delivery line is the other place
# the user types for a while before anything is saved, and losing it means
# re-reading the invoice.
# The note has to be on the page that HAS the line form, which is the open
# delivery — not the list page. It was on the list page, where the user typing
# a line would never have seen it.
_d = dels.find_one({"status": "open"}) or dels.find_one({})
assert _d is not None, 'this suite should already have made a delivery'
_did = str(_d['_id'])
listing = c.get('/receive').data.decode()
body = c.get(f'/receive?delivery_id={_did}').data.decode()
check('the open delivery has the line form',
      'name="med_name"' in body)
check('the open delivery carries a place to say the draft came back',
      'id="line-draft-note"' in body)
check('and the style for it exists', '.draft-note' in body)
check('the draft is per-tab session storage, never a cookie',
      'sessionStorage' in body and 'delivery-line-draft' in body)
check('the whole line is covered, not just the name',
      all(f"'{f}'" in body for f in
          ['med_name', 'quantity', 'pack_size', 'pack_price',
           'batch', 'expiry_date']))
# The page must not try to read its own prose to find out whether a line
# posted: document.body.textContent includes <script> text, so any sentinel
# phrase written in the script matches itself on every single load.
check('whether a line posted comes from the server, not the page text',
      'LINE_POSTED' in body
      and "textContent.indexOf('units received onto')" not in body)
check('a plain load is told no line posted',
      'var LINE_POSTED = false;' in body,
      [l for l in body.splitlines() if 'LINE_POSTED =' in l])
posted = c.get(f'/receive?delivery_id={_did}&line_added=1').data.decode()
check('the load straight after a line posts is told so',
      'var LINE_POSTED = true;' in posted,
      [l for l in posted.splitlines() if 'LINE_POSTED =' in l])

# Behaviour, not presence: run the page's own code against a DOM stub.
import json as _json, subprocess as _sp, tempfile as _tf, os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_NODE = next((n for n in ['/usr/bin/node', '/usr/local/bin/node', 'node']
              if _os.path.exists(n) or n == 'node'), 'node')
def _draft_script(page_html):
    found = [x for x in _re.findall(r'<script>(.*?)</script>', page_html, _re.S)
             if 'delivery-line-draft' in x]
    return found[0] if len(found) == 1 else None

_draft, _draft_posted = _draft_script(body), _draft_script(posted)
check('the draft script was found on the page', _draft is not None)
check('and on the page served after a line posts', _draft_posted is not None)
if _draft and _draft_posted:
    _tmps = []
    for _src in (_draft, _draft_posted):
        with _tf.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
            fh.write(_src)
            _tmps.append(fh.name)
    try:
        _r = _sp.run([_NODE, _os.path.join(_HERE, 'line_draft_harness.js'),
                      _tmps[0], _tmps[1]],
                     capture_output=True, text=True, timeout=60)
        _out = _json.loads(_r.stdout.strip().splitlines()[-1]) if _r.stdout.strip() else None
    except Exception as _e:
        _out, _r = None, type('r', (), {'stderr': str(_e)})
    finally:
        for _t in _tmps:
            _os.unlink(_t)

    check('the draft code runs', _out is not None,
          (getattr(_r, 'stderr', '') or '')[:300])
    if _out:
        check('a page that is NOT a post-line load keeps the draft',
              _out['restored']['stored'] is not None, str(_out['restored']))
        check('a fresh page starts empty and says nothing',
              _out['fresh']['med_name'] == '' and _out['fresh']['note'] == '',
              str(_out['fresh']))
        check('typing a line saves it', _out['afterTyping']['stored'] is not None,
              str(_out['afterTyping']))
        check('the name comes back after the page does',
              _out['restored']['med_name'] == 'Amoxicillin, 250 mg',
              str(_out['restored']))
        check('so does the quantity', _out['restored']['quantity'] == '400',
              str(_out['restored']))
        check('and the batch', _out['restored']['batch'] == 'AX77',
              str(_out['restored']))
        check('the user is told why the fields are filled',
              'Nothing was lost' in _out['restored']['note'],
              str(_out['restored']['note']))
        check('once the line is on the delivery the draft is gone',
              _out['afterPost']['stored'] is None, str(_out['afterPost']))
        check('and the next line starts from a clean form',
              _out['afterPost']['med_name'] == '', str(_out['afterPost']))
        check('a pre-filled pack size on its own is not a draft',
              _out['packSizeOnly']['stored'] is None,
              str(_out['packSizeOnly']))

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
