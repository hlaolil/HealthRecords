# audit_logger.py
# --------------------------------------------------------------
# Drop this file in the root folder (same level as app.py)
# It will automatically monkey-patch the Flask routes that
# perform edits / deletes and store an immutable audit trail.
# --------------------------------------------------------------

import os
import uuid
from datetime import datetime, timezone
from functools import wraps
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from flask import request, session, current_app, g

# ------------------------------------------------------------------
# Configuration – change only if you want a different DB / collection
# ------------------------------------------------------------------
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
DB_NAME     = 'pharmacy_db'
COLLECTION  = 'audit_log'      # <-- audit records go here
# ------------------------------------------------------------------

def get_mongo_client():
    """Lazy client – safe for forks."""
    return MongoClient(MONGODB_URI, serverSelectionTimeoutMS=120000)

def write_audit(action, target_type, target_id, changes, user):
    """Persist a single audit entry."""
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        coll = db[COLLECTION]

        doc = {
            'audit_id'     : str(uuid.uuid4()),
            'timestamp'    : datetime.now(timezone.utc),
            'action'       : action,          # CREATE / UPDATE / DELETE
            'target_type'  : target_type,     # dispense / medication
            'target_id'    : target_id,       # transaction_id or med_name
            'changes'      : changes,         # dict of old→new or list of meds
            'user'         : user,
            'ip'           : request.remote_addr,
            'user_agent'   : request.headers.get('User-Agent'),
        }
        coll.insert_one(doc)
    except ServerSelectionTimeoutError:
        current_app.logger.error("Audit log failed – DB unavailable")
    finally:
        client.close()


# ------------------------------------------------------------------
# Helper decorators – they wrap the original view functions
# ------------------------------------------------------------------
def audit_dispense_edit(original_func):
    """Wraps the POST handling in /dispense when editing."""
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        # Detect edit mode
        if request.form.get('transaction_id'):
            tx_id = request.form['transaction_id']
            # Grab the *old* rows before they are deleted
            client = get_mongo_client()
            db = client[DB_NAME]
            old_rows = list(db['transactions'].find(
                {'transaction_id': tx_id, 'type': 'dispense'}
            ))
            client.close()

            old_meds = [
                {'med_name': r['med_name'], 'quantity': r['quantity']}
                for r in old_rows
            ]

            # Let the original view run (it will delete + re-insert)
            response = original_func(*args, **kwargs)

            # After success – record the change
            user = session['user']['name']
            write_audit(
                action='UPDATE',
                target_type='dispense',
                target_id=tx_id,
                changes={'old_meds': old_meds,
                         'new_meds': [
                             {'med_name': n, 'quantity': int(q)}
                             for n, q in zip(
                                 request.form.getlist('med_names'),
                                 request.form.getlist('quantities')
                             ) if n.strip()
                         ]},
                user=user
            )
            return response

        # Normal (create) path – just continue
        return original_func(*args, **kwargs)
    return wrapper


def audit_dispense_delete(original_func):
    """Wraps /delete-dispense."""
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        tx_id = request.form.get('transaction_id')
        if not tx_id:
            return original_func(*args, **kwargs)

        # Capture the rows that are about to be removed
        client = get_mongo_client()
        db = client[DB_NAME]
        rows = list(db['transactions'].find(
            {'transaction_id': tx_id, 'type': 'dispense'}
        ))
        client.close()

        meds = [
            {'med_name': r['med_name'], 'quantity': r['quantity']}
            for r in rows
        ]

        response = original_func(*args, **kwargs)

        # Log the deletion
        user = session['user']['name']
        write_audit(
            action='DELETE',
            target_type='dispense',
            target_id=tx_id,
            changes={'removed_meds': meds},
            user=user
        )
        return response
    return wrapper


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
