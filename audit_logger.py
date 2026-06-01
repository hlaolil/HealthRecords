# audit_logger.py
import os
import uuid
from datetime import datetime, timezone
from functools import wraps
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from flask import request, session, current_app

MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
DB_NAME     = 'pharmacy_db'
COLLECTION  = 'audit_log'

def get_mongo_client():
    return MongoClient(MONGODB_URI, serverSelectionTimeoutMS=120000)

def write_audit(action, target_type, target_id, changes, user):
    """Persist audit entry (unchanged)."""
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        coll = db[COLLECTION]
        doc = {
            'audit_id': str(uuid.uuid4()),
            'timestamp': datetime.now(timezone.utc),
            'action': action,
            'target_type': target_type,
            'target_id': target_id,
            'changes': changes,
            'user': user,
            'ip': request.remote_addr,
            'user_agent': request.headers.get('User-Agent'),
        }
        coll.insert_one(doc)
    except ServerSelectionTimeoutError:
        current_app.logger.error("Audit log failed – DB unavailable")
    finally:
        client.close()

def init_audit(app):
    """Monkey-patch only the routes we care about using Flask's view_functions registry.
       Works perfectly with single-file app.py."""
    # Dispense (create + edit)
    if 'dispense' in app.view_functions:
        original_dispense = app.view_functions['dispense']
        app.view_functions['dispense'] = audit_dispense_edit(original_dispense)

    # Delete dispense
    if 'delete_dispense' in app.view_functions:
        original_delete_dispense = app.view_functions['delete_dispense']
        app.view_functions['delete_dispense'] = audit_dispense_delete(original_delete_dispense)

    # Receive delete (we'll keep it simple for now)
    if 'delete_receive' in app.view_functions:
        original_delete_receive = app.view_functions['delete_receive']
        app.view_functions['delete_receive'] = audit_delete_receive(original_delete_receive)

    # Medication CRUD (add/edit/delete)
    if 'add_medication' in app.view_functions:
        app.view_functions['add_medication'] = audit_medication_create(app.view_functions['add_medication'])
    if 'edit_medication' in app.view_functions:   # note: endpoint is usually the function name
        app.view_functions['edit_medication'] = audit_medication_update(app.view_functions['edit_medication'])
    if 'delete_medication' in app.view_functions:
        app.view_functions['delete_medication'] = audit_medication_delete(app.view_functions['delete_medication'])

    app.logger.info("✅ Audit logger attached successfully (single-file mode).")

# Keep your existing wrapper decorators (they still work)
def audit_dispense_edit(original_func):
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        if request.method == 'POST' and request.form.get('transaction_id'):  # edit mode
            tx_id = request.form['transaction_id']
            client = get_mongo_client()
            old_rows = list(client[DB_NAME]['transactions'].find(
                {'transaction_id': tx_id, 'type': 'dispense'}
            ))
            client.close()
            old_meds = [{'med_name': r['med_name'], 'quantity': r['quantity']} for r in old_rows]

            response = original_func(*args, **kwargs)

            user = session.get('user', {}).get('name', 'unknown')
            write_audit('UPDATE', 'dispense', tx_id, {
                'old_meds': old_meds,
                'new_meds': [{'med_name': n, 'quantity': int(q)} 
                             for n, q in zip(request.form.getlist('med_names'), 
                                             request.form.getlist('quantities')) if n.strip()]
            }, user)
            return response
        return original_func(*args, **kwargs)
    return wrapper

def audit_dispense_delete(original_func):
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        tx_id = request.form.get('transaction_id')
        if tx_id:
            client = get_mongo_client()
            rows = list(client[DB_NAME]['transactions'].find(
                {'transaction_id': tx_id, 'type': 'dispense'}
            ))
            client.close()
            meds = [{'med_name': r['med_name'], 'quantity': r['quantity']} for r in rows]

            response = original_func(*args, **kwargs)

            user = session.get('user', {}).get('name', 'unknown')
            write_audit('DELETE', 'dispense', tx_id, {'removed_meds': meds}, user)
            return response
        return original_func(*args, **kwargs)
    return wrapper

def audit_delete_receive(original_func):
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        receive_id = request.form.get('receive_id')
        if receive_id:
            client = get_mongo_client()
            rx = client[DB_NAME]['transactions'].find_one({'_id': receive_id, 'type': 'receive'})
            client.close()
            if rx:
                user = session.get('user', {}).get('name', 'unknown')
                write_audit('DELETE', 'receive', str(rx['_id']), 
                           {'removed': {'med_name': rx['med_name'], 'quantity': rx['quantity']}}, user)
        return original_func(*args, **kwargs)
    return wrapper

# Keep the medication audit wrappers exactly as you had them (they don't rely on relative imports)
# ... (copy your original audit_medication_create, audit_medication_update, audit_medication_delete here)


def audit_medication_create(original_func):
    """Wraps /add-medication POST."""
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        response = original_func(*args, **kwargs)
        if request.method == 'POST' and 'Medication added successfully!' in response.get_data(as_text=True):
            user = session['user']['name']
            med_name = request.form['med_name']
            write_audit(
                action='CREATE',
                target_type='medication',
                target_id=med_name,
                changes={'initial_balance': int(request.form['initial_balance'])},
                user=user
            )
        return response
    return wrapper


def audit_medication_update(original_func):
    """Wraps /edit-medication/<med_name> POST."""
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        med_name = kwargs.get('med_name')
        # Capture old values *before* the update
        client = get_mongo_client()
        db = client[DB_NAME]
        old = db['medications'].find_one({'name': med_name})
        client.close()

        response = original_func(*args, **kwargs)

        if request.method == 'POST' and 'Medication updated successfully!' in response.get_data(as_text=True):
            user = session['user']['name']
            changes = {}
            for field in ('balance', 'batch', 'price', 'expiry_date', 'schedule'):
                old_val = old.get(field)
                new_val = request.form.get(field)
                if str(old_val) != new_val:
                    changes[field] = {'old': old_val, 'new': new_val}
            write_audit(
                action='UPDATE',
                target_type='medication',
                target_id=med_name,
                changes=changes,
                user=user
            )
        return response
    return wrapper


def audit_medication_delete(original_func):
    """Wraps /delete-medication POST."""
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        med_name = request.form.get('med_name')
        if not med_name:
            return original_func(*args, **kwargs)

        # Snapshot before deletion
        client = get_mongo_client()
        db = client[DB_NAME]
        med = db['medications'].find_one({'name': med_name})
        client.close()

        response = original_func(*args, **kwargs)

        if 'deleted successfully' in response.get_data(as_text=True):
            user = session['user']['name']
            write_audit(
                action='DELETE',
                target_type='medication',
                target_id=med_name,
                changes={'snapshot': {k: med.get(k) for k in ('balance', 'batch', 'price', 'expiry_date', 'schedule')}},
                user=user
            )
        return response
    return wrapper


# ------------------------------------------------------------------
# Auto-patch the Flask app when this module is imported
# ------------------------------------------------------------------
def init_audit(app):
    """Call this once after you create the Flask app."""
    # Dispense – edit part
    from . import dispense
    dispense.dispense = audit_dispense_edit(dispense.dispense)

    # Dispense – delete
    from . import delete_dispense
    delete_dispense.delete_dispense = audit_dispense_delete(delete_dispense.delete_dispense)

    # Medication CRUD
    from . import add_medication, edit_medication, delete_medication
    add_medication.add_medication = audit_medication_create(add_medication.add_medication)
    edit_medication.edit_medication = audit_medication_update(edit_medication.edit_medication)
    delete_medication.delete_medication = audit_medication_delete(delete_medication.delete_medication)

    app.logger.info("Audit logger attached – edits & deletes are now traced.")
