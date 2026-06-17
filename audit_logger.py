# audit_logger.py - Clean version for single-file app.py
import os
import uuid
from datetime import datetime, timezone
from functools import wraps
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from bson import ObjectId  # FIX: was missing, caused NameError in audit_delete_receive
from flask import request, session, current_app

MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
DB_NAME     = 'pharmacy_db'
COLLECTION  = 'audit_log'

def get_mongo_client():
    return MongoClient(MONGODB_URI, serverSelectionTimeoutMS=120000)

def write_audit(action, target_type, target_id, changes, user):
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
        print(f"✅ AUDIT LOGGED: {action} on {target_type} by {user}")
    except Exception as e:
        current_app.logger.error(f"Audit write failed: {e}")
    finally:
        try:
            client.close()
        except Exception:
            pass

# ====================== DECORATORS ======================

def audit_dispense_edit(original_func):
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        if request.method == 'POST' and request.form.get('transaction_id'):
            tx_id = request.form['transaction_id']
            # Capture old state before the update
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
                'new_meds': [
                    {'med_name': n, 'quantity': int(q or 0)}
                    for n, q in zip(
                        request.form.getlist('med_names'),
                        request.form.getlist('quantities')
                    )
                    if n.strip()
                ]
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
            try:
                # FIX: ObjectId now imported at the top of this file
                client = get_mongo_client()
                rx = client[DB_NAME]['transactions'].find_one(
                    {'_id': ObjectId(receive_id), 'type': 'receive'}
                )
                client.close()
                if rx:
                    user = session.get('user', {}).get('name', 'unknown')
                    write_audit('DELETE', 'receive', receive_id,
                               {'med_name': rx.get('med_name'), 'quantity': rx.get('quantity')}, user)
            except Exception:
                pass
        return original_func(*args, **kwargs)
    return wrapper


def audit_medication_create(original_func):
    @wraps(original_func)
    def wrapper(*args, **kwargs):
        response = original_func(*args, **kwargs)
        # FIX: render_template_string returns a str, not a Response object.
        # Check the string directly; if it's a Response, fall back gracefully.
        if request.method == 'POST':
            try:
                if hasattr(response, 'get_data'):
                    body = response.get_data(as_text=True)
                else:
                    body = str(response)
                if 'successfully' in body.lower():
                    user = session.get('user', {}).get('name', 'unknown')
                    write_audit('CREATE', 'medication', request.form.get('med_name'),
                               {'initial_balance': request.form.get('initial_balance')}, user)
            except Exception as e:
                current_app.logger.error(f"audit_medication_create inspection failed: {e}")
        return response
    return wrapper


# ====================== INIT ======================
def init_audit(app):
    """Attach audit wrappers to routes after the app is fully initialised."""
    try:
        if 'dispense' in app.view_functions:
            app.view_functions['dispense'] = audit_dispense_edit(app.view_functions['dispense'])
        if 'delete_dispense' in app.view_functions:
            app.view_functions['delete_dispense'] = audit_dispense_delete(app.view_functions['delete_dispense'])
        if 'delete_receive' in app.view_functions:
            app.view_functions['delete_receive'] = audit_delete_receive(app.view_functions['delete_receive'])
        if 'add_medication' in app.view_functions:
            app.view_functions['add_medication'] = audit_medication_create(app.view_functions['add_medication'])

        app.logger.info("✅ Audit logger successfully attached!")
        print("✅ Audit logger initialized successfully")
    except Exception as e:
        app.logger.error(f"Failed to attach audit logger: {e}")
        print(f"❌ Audit init error: {e}")
