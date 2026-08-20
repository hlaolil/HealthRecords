import os
import requests
from flask import Flask, request, render_template_string, jsonify, redirect, url_for, session, flash, get_flashed_messages
from functools import wraps
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from collections import defaultdict
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import escape  # FIX: used to prevent XSS in nav links

load_dotenv()

from error_logger import init_error_logging, write_app_warning
from bson import ObjectId
from bson.errors import InvalidId

app = Flask(__name__)
init_error_logging(app)
app.secret_key = os.getenv('SECRET_KEY')

# FIX: fail loudly at startup if SECRET_KEY is not set, rather than silently
# using a weak dev key in production.
if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. "
        "Set it to a long random string before starting the server."
    )

ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')

# -----------------------------------------------------------------------
# NEW: 30-minute inactivity session timeout.
#
# PERMANENT_SESSION_LIFETIME sets the ceiling for how long a "permanent"
# session cookie is valid. We mark sessions permanent at login (see
# login() below) and refresh the expiry on every request via the
# before_request hook further down, so the 30 minutes counts from the
# user's LAST activity, not from login time — true inactivity timeout,
# not a fixed session length.
# -----------------------------------------------------------------------
SESSION_TIMEOUT_MINUTES = 60
app.permanent_session_lifetime = timedelta(minutes=SESSION_TIMEOUT_MINUTES)

# NEW: passwords expire after this many days; checked at login time.
PASSWORD_MAX_AGE_DAYS = 365

# NEW: accounts expire after this many days (~2 years) and must be
# re-registered by an admin; checked at login time, before the password
# expiry check.
ACCOUNT_MAX_AGE_DAYS = 730

# -----------------------------------------------------------------------
# FIX (Performance): MongoDB module-level client with connection pooling.
# Previously a new MongoClient was created and closed on every request,
# which is slow and defeats PyMongo's built-in connection pool.
# -----------------------------------------------------------------------
_mongo_client = None

def get_mongo_client():
    global _mongo_client
    if _mongo_client is None:
        mongouri = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
        _mongo_client = MongoClient(mongouri, serverSelectionTimeoutMS=120000)
    return _mongo_client


# -----------------------------------------------------------------------
# NEW: Explicit collection + index initialization.
#
# Previously audit_log (written by audit_logger.py) and error_logs (written
# by error_logger.py) were only created implicitly on first insert. That
# meant: (a) the /audit page couldn't distinguish "nothing happened yet"
# from "collection doesn't exist", and (b) there were no indexes, so
# sort('timestamp', -1) would do a full collection scan as these grow.
#
# This runs once at process startup (module import time + again defensively
# in __main__) and is safe to call repeatedly — create_collection raises
# CollectionInvalid if it already exists, which we just swallow, and
# create_index is idempotent by definition.
# -----------------------------------------------------------------------
def init_db_collections():
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        existing = db.list_collection_names()

        if 'audit_log' not in existing:
            db.create_collection('audit_log')
        if 'error_logs' not in existing:
            db.create_collection('error_logs')
        if 'app_warnings' not in existing:
            db.create_collection('app_warnings')

        # audit_log: /audit page sorts by timestamp desc and filters by
        # action/target_type/target_id/user via $or regex, plus an exact
        # lookup pattern (target_type + target_id) for "history of this record".
        db['audit_log'].create_index([('timestamp', -1)])
        db['audit_log'].create_index([('action', 1)])
        db['audit_log'].create_index([('target_type', 1), ('target_id', 1)])
        db['audit_log'].create_index([('user', 1)])

        # error_logs: /audit page sorts by timestamp desc and filters by
        # path/endpoint/method.
        db['error_logs'].create_index([('timestamp', -1)])
        db['error_logs'].create_index([('endpoint', 1)])

        # app_warnings: handled business-logic issues (not found, insufficient
        # stock, etc.) written by error_logger.write_app_warning(). /audit
        # page sorts by timestamp desc and filters by warning_type/user.
        db['app_warnings'].create_index([('timestamp', -1)])
        db['app_warnings'].create_index([('warning_type', 1)])
        db['app_warnings'].create_index([('user', 1)])

        # Also index the collections the rest of the app queries heavily,
        # since dispense/receive/reports all filter or sort on these.
        db['transactions'].create_index([('timestamp', -1)])
        db['transactions'].create_index([('transaction_id', 1)])
        db['transactions'].create_index([('type', 1), ('timestamp', -1)])
        db['transactions'].create_index([('med_name', 1)])
        db['medications'].create_index([('name', 1)], unique=True)

        app.logger.info("DB collections and indexes initialized (audit_log, error_logs, app_warnings, transactions, medications)")
        print("✅ DB collections and indexes initialized (audit_log, error_logs, app_warnings, transactions, medications)")
    except Exception as e:
        # Don't crash app startup over index creation — log and continue.
        # Collections will still be created implicitly on first write if this
        # fails (e.g. Mongo briefly unreachable at boot).
        app.logger.warning(f"init_db_collections failed (will retry implicitly on first write): {e}")
        print(f"⚠️  init_db_collections failed (will retry implicitly on first write): {e}")


# Run at import time so this also applies under `flask run` / gunicorn,
# not just `python app.py`.
init_db_collections()

# Diagnosis options
DIAGNOSES_OPTIONS = [
    'ARDS', 'Abscess', 'Acne', 'Acute Bronchitis', 'Acute Gastroenteritis (AGE)',
    'Acute appendicitis', 'Acute otitis media', 'Acute sinusitis', 'Acute stress disorder',
    'Acute tonsilitis /pharyngitis', 'Alcohol intoxication', 'Allergic conjunctivitis',
    'Allergic rhinitis', 'Allergic skin reaction', 'Allergies', 'Anaemia',
    'Anal fissure', 'Angina', 'Antiphspholipid syndrome', 'Anxiety disorder/ panic disorder',
    'Aphthous ulcers/oral lessions', 'Aquagenic pruritus', 'Arthralgia', 'Asthma',
    'Awating PCR results', 'Bacterial conjunctivitis', 'Bipolar disorder', 'Boutoner deformity',
    'Bowel obstruction', 'Brachial plexus compression', 'Breast lump', 'Bulous skin lessions',
    'Burns', 'Bursitis', 'CCF', 'CNS_PNS', 'COPD', 'COVID_19', 'CVS_Immunological',
    'Calcaneous spur', 'Candidiasis(oral/esophageal)', 'Cardiac dysrythmia', 'Cardiomegally',
    'Cataract', 'Cellulitis', 'Chelazion', 'Chemical conjunctivitis', 'Chemical pneumonitis',
    'Chronic Suppurative Otits media (CSOM)', 'Chronic fatique syndrome', 'Chronic sinusitis',
    'Circumcission', 'Common Cold', 'Constipation', 'Costochondritis', 'Crush syndrome',
    'DVT', 'Dental', 'Dental abscess', 'Dental caries', 'Dental decay', 'Depressive disorder',
    'Dermatitis/Eczema', 'Dermatological', 'Diarrhoea', 'Disc Hernia', 'Dislocation',
    'Dog bite', 'Dry Eyes', 'Dysentery', 'ENT', 'Ear wax impaction', 'Emphysema',
    'Endocrinological', 'Epidermoid cyst', 'Epilepsy / Seizure disorder', 'Epistaxis',
    'Eyelid infection', 'Feet corns/calluses', 'Foreign body', 'Foreign body (in soft tissue)',
    'Foreign body ear', 'Fractures', 'Fungal infections/Tineas/ Dermatophyte',
    'GERD / Esophageal sphincter dysfunction', 'GIT', 'GUT_Urological_Gynae',
    'Ganglion cyst', 'Gingivitis', "Golfer's elbow", 'Gout', 'Grief/bereavement',
    'HIV', 'HIV -Associated vasculitis', 'HTN', 'HTN/DM', 'Hairy leucoplakia',
    'Hallus valgus deformity', 'Head injury',
    'Headaches (tension, migrane, cluster etc)', 'Hepatitis',
    'Herpes labialis (cold sore)', 'Herpes zoster', 'Hiccups', 'High altitude syndrome',
    'Hypercholesterolaemia', 'Hypertriglyceridaemia', 'Hypotension', 'I & D',
    'Illio-tibial band syndrome', 'Indigestion', 'Inflamatory bowel disease / IBS',
    'Influenza', 'Ingrown', 'Injury', 'Insect bite', 'Insomnia',
    'Insufficient sleep syndrome', 'Internal/external haemorrhoids', 'Kaposis Sarcoma',
    'LRTI', 'Laryngitis', 'Lightening injury', 'Ligmament sprain',
    'Lipoma', 'Loose teeth', 'Lower GI bleed',
    'Lymphadenitis', 'Mechanical low back pain', 'Medication induced cough(ACE-I etc)',
    'Medication side effects', 'Mucus hypersecretion', 'Muscle strain',
    'Musculoskeletal', 'Myalgia/muscle tension/ spasm', 'Myocardiac infaction/ ACS',
    'Nasal lession/infection', 'Nasal polyp', 'Nausea and Vomiting', 'Negative on AgRDT',
    'Neuralgia / neuritis', 'Non specific spinal pain',
    'Obstructive sleep apnea', 'Opthalmological', 'Oral lession(s)', 'Osteoarthritis',
    'Otitis externa', 'PICA', 'PJP', 'PUD/gastritis', 'Pancreatitis', 'Papule, pastules',
    'Parasthesia', 'Peri-orbital lession', 'Perianal abscess', 'Periodontitis',
    'Peripheral neuropathy', 'Peripheral vascular disease', 'Pinguecula', 'Planta fascitis',
    'Pleural effusion', 'Pleuritic chest pain/pleuritis', 'Pneumonia', 'Polyarthralgia',
    'Poor vision', 'Positive on AgRDT', 'Post covid hyperactive airway DX',
    'Preseptal cellulitis', 'Psychiatric', 'Psychosomatic Disorder', 'Pterygium',
    'Radiculopathy', 'Respiratory', 'Rheumatoid arthritis',
    'Rheumatological_Ortho', 'Rynaulds phenomenon', 'Scabies', 'Sceptic nasal piercing',
    'Sciatica', 'Smoke inhilation injury', 'Spinal abnormality',
    'Spondylolisthesis', 'Spondylosis', 'Spondylosis/spondylolisthesis', 'Stye /',
    'Surgical', 'Surgical site infection', 'Swan neck deformity',
    'Syncope', 'TB', 'TB Meningitis', 'TIA / CVA',
    'Temporal arteritis', 'Tendon injury', 'Tendonitis', 'Tennis elbow', 'Thoracic back pain',
    'Tinnitus', 'Toothache', 'Torticolis', 'Traumatic conjunctivitis',
    'Tumour', 'Typhoid', 'URTI', 'Ulcer',
    'Upper GI bleed', 'Urticaria', 'Venous insufficiency', 'Viral conjunctivitis',
    'Viral rhinitis/ common cold', 'Warts', 'Worm infestation', 'faecal incontinence',
    'hordeolum', 'injuries', 'injuries on duty', 'injury (RTA)',
    'STI (MUS, VDS, Herpes genitalia, syphilis etc)', 'Post HIV exposure', 'Post operative adhessions',
    'Vaginal candidiasis', 'Cervixitis', 'UTI', 'Pelvic hypertension', 'Dysmenorrhoea', 'PID',
    'Hormonal imbalance', 'PCOS', 'Ovarian cyst', 'Pelvic mass',
    'Fibroid uterus', 'Pregnancy', 'Supplementation', 'Normal mentrual period', 'Menorrhagia',
    'Miscarriage', 'Secondary ammenorrhoea',
    'Post menopausal syndrome', 'Post menauposal bleeding', 'Dysfunctional uterine beeding',
    'Inhibited sexual desire', 'Erectile dysfunction', 'Epididimorchitis',
    'Urine Incontinence', 'Hydrocele', 'Acute urinary retension', 'Urinary catheter blockage',
    'BPH', 'Acute kidney injury', 'Chronic kidney disease',
    'Drug induced kidney injury', 'Urethral stricture/Urinary outlet obstruction', 'Kidney stone',
    'Bladder stone', 'Warts', 'DM', 'Hyperglycaemia', 'Hypoglycaemia', 'DKA', 'HHS'
]

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function


# NEW: enforce the 30-minute inactivity timeout on every request, before
# any route handler runs. This has to be a before_request hook rather than
# logic inside login_required, because it needs to actively clear an expired
# session (not just check membership) — otherwise a stale 'user' key would
# still satisfy login_required's `if 'user' not in session` check even
# though the user has been inactive well past the timeout.
@app.before_request
def enforce_session_timeout():
    if 'user' not in session:
        return  # not logged in, nothing to enforce

    now = datetime.now(timezone.utc)
    last_active_str = session.get('last_active')

    if last_active_str:
        try:
            last_active = datetime.fromisoformat(last_active_str)
        except ValueError:
            last_active = None
        if last_active and (now - last_active) > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
            session.clear()
            flash('You were logged out due to 30 minutes of inactivity. Please log in again.', 'error')
            return redirect('/login')

    # Still within the window (or this is the first request after login) —
    # refresh the activity timestamp and make sure the cookie is marked
    # permanent so permanent_session_lifetime actually applies to it.
    session['last_active'] = now.isoformat()
    session.permanent = True

    # NEW: if this user's password has expired, block every route except
    # the change-password page itself and logout — otherwise a user could
    # just type /dispense into the address bar and skip the requirement
    # entirely, since they're still a normally-authenticated session.
    if session.get('must_change_password') and request.endpoint not in (
        'change_expired_password', 'logout', 'static'
    ):
        return redirect('/change-expired-password')

# FIX (Security/XSS): escape user-controlled values before inserting into nav HTML.
# Previously the user's display name came straight from the DB and was rendered
# with |safe, allowing a name like <script>alert(1)</script> to execute.
def get_nav_links():
    if 'user' in session:
        user = session['user']
        is_admin = user.get('role') == 'admin'
        # escape() returns a Markup object that Jinja will not double-escape,
        # but any HTML characters in the name are neutralised.
        name = escape(user.get('name', user.get('login', 'User')))
        add_med_link = '<a href="/add-medication">Add Medication</a> | ' if is_admin else ''
        audit_link = '<a href="/audit">Audit Log</a> | ' if is_admin else ''
        return f"""
        <p class="nav-links"><strong>Navigate:</strong>
            <a href="/dispense">Dispensing</a> |
            <a href="/receive">Receiving</a> |
            {add_med_link}
            <a href="/reports">Reports</a> |
            {audit_link}
            <span>Welcome, {name}! <a href="/logout">Logout</a></span>
        </p>
        """
    else:
        return """
        <p class="nav-links"><strong>Navigate:</strong>
            <a href="/login">Login</a> | <a href="/register">Register</a>
        </p>
        """

# CSS for all templates
CSS_STYLE = """
<style>
    body {
        font-family: Arial, sans-serif;
        max-width: 1200px;
        margin: 0 auto;
        padding: 20px;
        background-color: #f8f9fa;
        color: #333;
    }
    h1 {
        color: #0056b3;
        text-align: center;
        margin-bottom: 20px;
    }
    h2 {
        color: #343a40;
        margin-top: 30px;
    }
    h3 {
        color: #495057;
        margin-top: 10px;
    }
    .nav-links {
        text-align: center;
        margin-bottom: 20px;
        font-size: 16px;
        position: sticky;
        top: 0;
        background-color: #f8f9fa;
        z-index: 100;
        padding: 10px 0;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        border-bottom: 1px solid #dee2e6;
    }
    .nav-links a {
        color: #0056b3;
        text-decoration: none;
        margin: 0 10px;
        font-weight: bold;
    }
    .nav-links a:hover {
        text-decoration: underline;
        color: #003d80;
    }
    form {
        background-color: #fff;
        padding: 20px;
        border-radius: 8px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        max-width: 900px;
        margin: 0 auto 20px;
    }
    /* FIX (Bug): the generic `form` rule above applies to every <form> on
       the page, including the tiny inline delete forms inside table action
       cells (e.g. <form class="delete-btn" style="display:inline;">).
       That gave each delete form a 20px padded card with its own shadow,
       rendering as an oversized red block around the Delete button instead
       of a normal-sized button. Action-cell forms opt back out of all of
       that card styling here. */
    .action-buttons form {
        background-color: transparent;
        padding: 0;
        border-radius: 0;
        box-shadow: none;
        max-width: none;
        margin: 0;
    }
    .dispense-form,
    .receive-form,
    .add-medication-form,
    .edit-medication-form {
        display: block;
    }
    .common-section {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 15px;
        margin-bottom: 20px;
    }
    .med-section, .diag-section {
        margin-bottom: 20px;
    }
    #medications, #diagnoses {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 15px;
    }
    .med-row, .diag-row {
        display: grid;
        grid-template-columns: 1fr 1fr auto;
        gap: 10px;
        margin-bottom: 10px;
        padding: 10px;
        border: 1px solid #ddd;
        border-radius: 4px;
        align-items: end;
    }
    .diag-row > div:first-of-type {
        grid-column: span 2;
    }
    .med-row label, .diag-row label {
        display: block;
        margin: 0 0 5px;
        font-weight: bold;
    }
    .med-row input, .diag-row input {
        width: 100%;
        padding: 8px;
        border: 1px solid #ced4da;
        border-radius: 4px;
        box-sizing: border-box;
    }
    form label {
        display: block;
        margin: 10px 0 5px;
        font-weight: bold;
    }
    form input, form select, form datalist {
        width: 100%;
        padding: 8px;
        margin-bottom: 10px;
        border: 1px solid #ced4da;
        border-radius: 4px;
        box-sizing: border-box;
    }
    .form-buttons {
        text-align: center;
    }
    form input[type="submit"],
    form button {
        background-color: #0056b3;
        color: #fff;
        border: none;
        padding: 12px 26px;
        border-radius: 6px;
        cursor: pointer;
        margin: 10px 8px;
        display: inline-block;
        font-weight: bold;
        font-size: 15px;
        transition: all 0.25s ease-in-out;
        box-shadow: 0 3px 6px rgba(0,0,0,0.15);
    }
    form input[type="submit"]:hover,
    form button:hover {
        background-color: #003d80;
        transform: translateY(-2px);
        box-shadow: 0 5px 10px rgba(0,0,0,0.2);
    }
    form input[type="submit"]:active,
    form button:active {
        transform: scale(0.97);
        box-shadow: 0 2px 4px rgba(0,0,0,0.15);
    }
    .action-buttons {
        /* FIX (UX): this class is applied to the <td> containing Edit/Delete.
           It previously had no layout rules of its own, so the edit <a> and
           the delete <form style="display:inline;"> sat flush against each
           other with their buttons' margin/padding forced to 0 below —
           making accidental delete clicks on a misaimed edit click likely,
           especially on touch. Flex + gap gives consistent, deliberate
           spacing regardless of markup (a/form/span mix). */
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
    }
    .action-buttons a,
    .action-buttons form {
        display: inline-flex;
    }
    .action-buttons button {
        padding: 6px 14px;
        border-radius: 4px;
        font-size: 13px;
        font-weight: 600;
        border: none;
        cursor: pointer;
        transition: all 0.2s ease;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    .action-buttons button:hover {
        transform: translateY(-2px);
        box-shadow: 0 3px 6px rgba(0,0,0,0.2);
    }
    .action-buttons .delete-btn {
        background-color: #dc3545;
        color: #fff;
    }
    .action-buttons .delete-btn:hover {
        background-color: #c82333;
        color: #fff;
    }
    .action-buttons .edit-btn {
        background-color: #ffc107;
        color: #212529;
    }
    .action-buttons .edit-btn:hover {
        background-color: #e0a800;
    }
    .action-buttons .view-btn {
        background-color: #28a745;
        color: white;
    }
    .action-buttons .view-btn:hover {
        background-color: #218838;
    }
    form button.delete-btn {
        /* FIX (UX): no longer zeroes out margin/padding — that was the
           direct cause of the delete button having no spacing buffer
           around it. Sizing now comes from .action-buttons button above;
           this rule only keeps delete's distinct red color/shadow. */
        background-color: #dc3545 !important;
        color: #fff !important;
        font-size: 13px !important;
        border-radius: 4px !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1) !important;
        transition: all 0.2s ease !important;
    }
    form button.delete-btn:hover {
        background-color: #c82333 !important;
        transform: translateY(-2px) !important;
        box-shadow: 0 3px 6px rgba(0,0,0,0.2) !important;
    }
    table {
        width: 100%;
        border-collapse: collapse;
        background-color: #fff;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        margin-top: 20px;
    }
    table th, table td {
        padding: 12px;
        text-align: left;
        border: 1px solid #dee2e6;
    }
    table th {
        background-color: #0056b3;
        color: #fff;
        font-weight: bold;
    }
    table tr:nth-child(even) {
        background-color: #f8f9fa;
    }
    table tr:hover {
        background-color: #e0e7f5;
    }
    .expired {
        background-color: #f8d7da !important;
        color: #721c24 !important;
    }
    .out-of-stock {
        background-color: #e3f2fd !important;
        color: #1976d2 !important;
    }
    .close-to-expire {
        background-color: #fff3cd !important;
        color: #856404 !important;
    }
    .normal {
        background-color: inherit !important;
        color: inherit !important;
    }
    .message {
        padding: 10px;
        margin-bottom: 20px;
        border-radius: 4px;
        text-align: center;
        font-weight: bold;
    }
    .message.success {
        background-color: #d4edda;
        color: #155724;
    }
    .message.partial {
        background-color: #fff3cd;
        color: #856404;
        border: 1px solid #ffeeba;
    }
    .message.error {
        background-color: #f8d7da;
        color: #721c24;
    }
    .filter-form {
        background-color: #fff;
        padding: 15px;
        border-radius: 8px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        margin-bottom: 20px;
    }
    .filter-section {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
        gap: 15px;
        align-items: end;
    }
    .filter-section label {
        display: block;
        font-weight: bold;
        margin-bottom: 5px;
    }
    .filter-section input {
        width: 100%;
        padding: 8px;
        border: 1px solid #ced4da;
        border-radius: 4px;
        box-sizing: border-box;
    }
    .filter-section a {
        color: #0056b3;
        text-decoration: none;
        margin-left: 10px;
    }
    .filter-section a:hover {
        text-decoration: underline;
    }
    .button-div {
        display: flex;
        align-items: end;
        gap: 5px;
    }
    .login-form, .register-form {
        max-width: 400px;
        margin: 100px auto;
        padding: 20px;
        background-color: #fff;
        border-radius: 8px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        text-align: center;
    }
    @media (max-width: 600px) {
        body { padding: 10px; }
        form, table { max-width: 100%; }
        .common-section { grid-template-columns: 1fr; }
        #medications, #diagnoses { grid-template-columns: 1fr; }
        .med-row, .diag-row { grid-template-columns: 1fr; }
        table th, table td { font-size: 14px; padding: 8px; }
        .filter-section { grid-template-columns: 1fr; }
    }
</style>
<script>
// NEW: client-side inactivity auto-logout.
//
// The server already enforces a 30-minute inactivity timeout (see
// enforce_session_timeout() / before_request in app.py), but that only
// gets checked on the NEXT request the browser happens to make. If
// someone leaves a form open and doesn't click anything for 30+ minutes,
// nothing visibly happens until they finally submit — at which point
// they're bounced to login and lose whatever they were typing.
//
// This timer runs entirely in the browser, independent of any server
// request, and redirects to /login the moment 30 minutes of no mouse/
// keyboard/touch activity has passed — so the redirect happens on its
// own, before the user has invested time filling out a form that's
// about to be thrown away.
(function() {
    // Skip entirely on the login page itself — nothing to time out.
    if (window.location.pathname === '/login') return;

    var TIMEOUT_MINUTES = 30;
    var TIMEOUT_MS = TIMEOUT_MINUTES * 60 * 1000;
    var timer = null;
    var lastReset = 0;
    var THROTTLE_MS = 1000; // ignore activity bursts within 1 second of each other

    function goToLogin() {
        window.location.href = '/login';
    }

    function resetTimer() {
        var nowMs = Date.now();
        if (nowMs - lastReset < THROTTLE_MS) return;
        lastReset = nowMs;
        if (timer) clearTimeout(timer);
        timer = setTimeout(goToLogin, TIMEOUT_MS);
    }

    // Any of these count as activity and push the timeout back out,
    // matching how the server-side last_active timestamp is refreshed
    // on every request.
    ['mousedown', 'mousemove', 'keydown', 'scroll', 'touchstart', 'click']
        .forEach(function(evt) {
            document.addEventListener(evt, resetTimer, { passive: true });
        });

    resetTimer();
})();
</script>
"""

# FIX (Performance): medication options are now served from a single endpoint
# /api/medications instead of being duplicated verbatim in three HTML templates.
# Templates reference MEDICATION_OPTIONS_JS which injects a small loader snippet.
MEDICATION_OPTIONS_JS = """
<script>
// FIX: medication list is fetched once from /api/medications instead of being
// duplicated in every template. Results are cached in module scope.
let _medicationOptions = null;

async function getMedicationOptions() {
    if (_medicationOptions) return _medicationOptions;
    try {
        const res = await fetch('/api/medications');
        _medicationOptions = await res.json();
    } catch(e) {
        console.error('Failed to load medication list', e);
        _medicationOptions = [];
    }
    return _medicationOptions;
}
</script>
"""

DISPENSE_TEMPLATE = CSS_STYLE + MEDICATION_OPTIONS_JS + """
<h1>Dispensing</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
{{ nav_links|safe }}
{% if message %}
    <p class="message {% if 'partial success' in message|lower %}partial{% elif 'successfully' in message|lower or 'updated' in message|lower or 'deleted' in message|lower or 'restored' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}
<h2>{% if tx_data %}Edit Dispense{% else %}Dispense Medication{% endif %}</h2>
<form method="POST" action="{{ url_for('dispense') }}" class="dispense-form">
    {# FIX (UX): transaction_id field now has id="transaction_id_field" so
       clearForm() can reset it, preventing a Clear → Submit from accidentally
       updating an existing record instead of creating a new one. #}
    <input type="hidden" name="transaction_id" id="transaction_id_field" value="{{ tx_data.transaction_id if tx_data else '' }}">
    <div class="common-section">
        <div>
            <label>Patient:</label>
            <input name="patient" type="text" value="{{ tx_data.patient if tx_data else '' }}" {% if not tx_data %}required{% endif %}>
        </div>
        <div>
            <label for="company">Company:</label>
            <input id="company" name="company" list="company_suggestions" type="text" value="{{ tx_data.company if tx_data else '' }}" {% if not tx_data %}required{% endif %}>
        </div>
        <div>
            <label for="position">Position:</label>
            <input id="position" name="position" list="position_suggestions" type="text" value="{{ tx_data.position if tx_data else '' }}" {% if not tx_data %}required{% endif %}>
        </div>
        <div>
            <label>Age Group:</label>
            <select name="age_group" {% if not tx_data %}required{% endif %}>
                <option value="">-- Select Age Group --</option>
                {% set age_options = ['18-24', '25-34', '35-45', '45-54', '54-65'] %}
                {% for opt in age_options %}
                    <option value="{{ opt }}" {% if tx_data and tx_data.age_group == opt %}selected{% endif %}>{{ opt }}</option>
                {% endfor %}
            </select>
        </div>
        <div>
            <label>Gender:</label>
            <select name="gender" {% if not tx_data %}required{% endif %}>
                <option value="">-- Select --</option>
                <option value="Male" {% if tx_data and tx_data.gender == 'Male' %}selected{% endif %}>Male</option>
                <option value="Female" {% if tx_data and tx_data.gender == 'Female' %}selected{% endif %}>Female</option>
            </select>
        </div>
        <div>
            <label>Number of Sick Leave Days:</label>
            <input name="sick_leave_days" type="number" min="0" autocomplete="on" value="{{ tx_data.sick_leave_days if tx_data else '' }}" required>
        </div>
        <div>
            <label>Prescriber (Doctor):</label>
            {% set prescribers = ['Dr. T. Khothatso', 'Locum Doctor', 'Locum Nurse', 'Malesoetsa Leohla', 'Mamosa Seetsa', 'Mamosaase Nqosa', 'Mapalo Mapesela', 'Mathuto Kutoane', 'Thapelo Mphole'] %}
            <select name="prescriber" {% if not tx_data %}required{% endif %}>
                <option value="">-- Select Doctor --</option>
                {% for p in prescribers %}
                    <option value="{{ p }}" {% if tx_data and tx_data.prescriber == p %}selected{% endif %}>{{ p }}</option>
                {% endfor %}
            </select>
        </div>
        <div>
            <label>Dispenser (Issuer):</label>
            {% set dispensers = ['Letlotlo Hlaoli', 'Locum Nurse', 'Locum Pharmacist', 'Malesoetsa Leohla', 'Mamosa Seetsa', 'Mamosaase Nqosa', 'Mapalo Mapesela', 'Mathuto Kutoane', 'Thapelo Mphole'] %}
            <select name="dispenser" {% if not tx_data %}required{% endif %}>
                <option value="">-- Select Issuer --</option>
                {% for d in dispensers %}
                    <option value="{{ d }}" {% if tx_data and tx_data.dispenser == d %}selected{% endif %}>{{ d }}</option>
                {% endfor %}
            </select>
        </div>
        <div>
            <label>Date:</label>
            <input name="date" type="date" value="{{ tx_data.date if tx_data else '' }}" {% if not tx_data %}required{% endif %}>
        </div>
    </div>
    <div class="diag-section">
        <h3>Diagnoses (up to 3)</h3>
        <datalist id="diag_suggestions"></datalist>
        <div id="diagnoses">
            {% if tx_data and tx_data.diags %}
                {% for d in tx_data.diags %}
                    <div class="diag-row">
                        <div>
                            <label>Diagnosis:</label>
                            <input name="diagnoses" list="diag_suggestions" type="text" class="diag-input" value="{{ d }}" {% if loop.first %}required{% endif %}>
                        </div>
                        <div>
                            <button type="button" onclick="removeDiagRow(this)">Remove</button>
                        </div>
                    </div>
                {% endfor %}
            {% else %}
                <div class="diag-row">
                    <div>
                        <label>Diagnosis:</label>
                        <input name="diagnoses" list="diag_suggestions" type="text" class="diag-input" required>
                    </div>
                    <div>
                        <button type="button" onclick="removeDiagRow(this)">Remove</button>
                    </div>
                </div>
            {% endif %}
        </div>
        <button type="button" onclick="addDiagRow()">Add Diagnosis</button>
    </div>
    <div class="med-section">
        <h3>Medications (up to 12)</h3>
        <datalist id="med_suggestions"></datalist>
        <div id="medications">
            {% if tx_data and tx_data.meds %}
                {% for med in tx_data.meds %}
                    <div class="med-row">
                        <div>
                            <label>Medication:</label>
                            <input name="med_names" list="med_suggestions" class="med-input" value="{{ med[0] }}">
                        </div>
                        <div>
                            <label>Quantity:</label>
                            <input name="quantities" type="number" min="1" value="{{ med[1] }}">
                        </div>
                        <div>
                            <button type="button" onclick="removeRow(this)">Remove</button>
                        </div>
                    </div>
                {% endfor %}
            {% else %}
                <div class="med-row">
                    <div>
                        <label>Medication:</label>
                        <input name="med_names" list="med_suggestions" class="med-input" required>
                    </div>
                    <div>
                        <label>Quantity:</label>
                        <input name="quantities" type="number" min="1" required>
                    </div>
                    <div>
                        <button type="button" onclick="removeRow(this)">Remove</button>
                    </div>
                </div>
            {% endif %}
        </div>
        <button type="button" onclick="addRow()">Add Medication</button>
    </div>
    <div class="form-buttons">
        <input type="submit" value="{% if tx_data %}Update{% else %}Dispense{% endif %}">
        <button type="button" onclick="clearForm()">Clear Form</button>
    </div>
    <input type="hidden" name="start_date" value="{{ start_date or '' }}">
    <input type="hidden" name="end_date" value="{{ end_date or '' }}">
    <input type="hidden" name="search" value="{{ search or '' }}">
    <datalist id="company_suggestions"></datalist>
    <datalist id="position_suggestions"></datalist>
</form>
<hr>
<h3>Dispense Transactions</h3>
<form method="GET" action="{{ url_for('dispense') }}" class="filter-form">
    <div class="filter-section">
        <div>
            <label>Start Date:</label>
            <input name="start_date" type="date" value="{{ start_date or '' }}">
        </div>
        <div>
            <label>End Date:</label>
            <input name="end_date" type="date" value="{{ end_date or '' }}">
        </div>
        <div>
            <label>Search:</label>
            <input name="search" type="text" value="{{ search or '' }}" placeholder="Search patient, medication, company...">
        </div>
        <div class="button-div">
            <input type="submit" value="Filter">
            <a href="{{ url_for('dispense') }}">Clear</a>
        </div>
    </div>
</form>
<table>
    <thead>
        <tr>
            <th>#</th>
            <th>Date</th>
            <th>Patient</th>
            <th>Company</th>
            <th>Position</th>
            <th>Gender</th>
            <th>Age Group</th>
            <th>Timestamp</th>
            <th>User</th>
            <th>Diagnoses</th>
            <th>Prescriber</th>
            <th>Dispenser</th>
            <th>Sick Leave (Days)</th>
            <th>Medication</th>
            <th>Quantity</th>
            <th>Actions</th>
        </tr>
    </thead>
    <tbody>
        {# FIX (UX/Correctness): Group rows by transaction_id in Python (see route)
           instead of relying on consecutive ordering in the template.
           tx_groups is a list of (transaction_id, [rows]) in display order. #}
        {% set tx_number = namespace(value=1) %}
        {% for group_id, group_rows in tx_groups %}
            {% for t in group_rows %}
                {% if loop.first %}
                <tr style="border-top: 3px double #0056b3;">
                    <td rowspan="{{ group_rows|length }}"
                        style="vertical-align: middle; font-weight: bold; font-size: 1.1em; color: #0056b3;">
                        {{ tx_number.value }}.
                        {% set tx_number.value = tx_number.value + 1 %}
                    </td>
                {% else %}
                <tr>
                {% endif %}
                    <td>{{ t.date }}</td>
                    <td>
                        {% if session['user']['role'] == 'viewer' %}
                            ***HIDDEN***
                        {% else %}
                            {{ t.patient }}
                        {% endif %}
                    </td>
                    <td>{{ t.company }}</td>
                    <td>{{ t.position }}</td>
                    <td>{{ t.gender }}</td>
                    <td>{{ t.age_group }}</td>
                    <td>{{ t.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
                    <td>{{ t.user }}</td>
                    <td>{{ t.diagnoses | join(', ') if t.diagnoses else '' }}</td>
                    <td>{{ t.prescriber }}</td>
                    <td>{{ t.dispenser }}</td>
                    <td>{{ t.sick_leave_days }}</td>
                    <td>{{ t.med_name }}</td>
                    <td>{{ t.quantity }}</td>
                    <td class="action-buttons">
                        {% if loop.first %}
                            {% if session['user']['role'] != 'viewer' %}
                                <a href="{{ url_for('dispense',
                                                    edit=t.transaction_id,
                                                    start_date=start_date,
                                                    end_date=end_date,
                                                    search=search) }}">
                                    <button type="button" class="edit-btn">Edit</button>
                                </a>
                            {% endif %}
                            {% if session['user']['role'] == 'admin' %}
                                <form class="delete-btn" method="POST"
                                      action="{{ url_for('delete_dispense') }}"
                                      style="display:inline;"
                                      onsubmit="return confirm('Permanently delete this dispense transaction?\\nStock will be restored.');">
                                    <input type="hidden" name="transaction_id" value="{{ t.transaction_id }}">
                                    <input type="hidden" name="start_date" value="{{ start_date or '' }}">
                                    <input type="hidden" name="end_date" value="{{ end_date or '' }}">
                                    <input type="hidden" name="search" value="{{ search or '' }}">
                                    <button type="submit" class="delete-btn">Delete</button>
                                </form>
                            {% endif %}
                            {% if session['user']['role'] == 'viewer' %}
                                <span>—</span>
                            {% endif %}
                        {% endif %}
                    </td>
                </tr>
            {% endfor %}
        {% else %}
        <tr><td colspan="16">No dispense transactions.</td></tr>
        {% endfor %}
    </tbody>
</table>
<script>
let medRowCount = {{ (tx_data.meds|length if tx_data else 1) }};
let diagRowCount = {{ (tx_data.diags|length if tx_data else 1) }};

const companyOptions = [
    "BLW","BUSY BEE","CMS","Consulmet","Enaex","Eminence","ER24","Government",
    "IFS","LD","LISELO","LMPS","Mendi","MGC","MINOPEX","NMC","Other","PLATO",
    "Public","THOLO","TOMRA","UL4","UNITRANS"
];
const positionOptions = [
    "Administration","Artisan","Blasting","Boiler Maker","Chef","CI","Cleaner",
    "Controller","Director","Diesel Depo","Drilling","Drivers","Electricians",
    "Emergency Coordinator","Environmnet","Finance","Fitters","Food Service Attendant",
    "General Worker","Geologist","Hse","Housekeeping","IT","Intern","Kitchen",
    "Lab Technologist","Maintenance","Management","Manager","Mechanics","Medical Doctor",
    "Metallurgy","Mining","Nurse","Operator","Other","PHC","Pharmacist","Plant Operator",
    "Police","Procurement","Process","Production","Public","Recovery","Rope Access",
    "Security","Sorting","Storekeeper","Supervisor","Survey","Technician","Training",
    "Tourist","Treatment","Tyreman","UNITRANS","Visitor","Water Works","Welder",
    "Workshop Cleaners","X-Ray Technologist"
];

function addInputListener(input, type) {
    input.addEventListener('input', async function() {
        const query = this.value.toLowerCase();
        let datalist, options;
        switch(type) {
            case 'company':
                datalist = document.getElementById('company_suggestions');
                options = companyOptions;
                break;
            case 'position':
                datalist = document.getElementById('position_suggestions');
                options = positionOptions;
                break;
            case 'medication':
                // FIX (Performance): fetch from API instead of using inline array
                datalist = document.getElementById('med_suggestions');
                options = await getMedicationOptions();
                break;
            case 'diagnosis':
                datalist = document.getElementById('diag_suggestions');
                fetch(`/api/diagnoses?query=${encodeURIComponent(query)}`)
                    .then(r => r.json())
                    .then(suggestions => {
                        if (suggestions.error) return;
                        datalist.innerHTML = '';
                        suggestions.forEach(s => {
                            const o = document.createElement('option');
                            o.value = s;
                            datalist.appendChild(o);
                        });
                    })
                    .catch(e => console.error('Error fetching diagnoses:', e));
                return;
            default:
                return;
        }
        datalist.innerHTML = '';
        if (query.length < 1) return;
        options.filter(o => o.toLowerCase().includes(query)).forEach(s => {
            const o = document.createElement('option');
            o.value = s;
            datalist.appendChild(o);
        });
    });
}

function addRow() {
    if (medRowCount >= 12) { alert('Maximum 12 medications allowed.'); return; }
    medRowCount++;
    const container = document.getElementById('medications');
    const newRow = document.createElement('div');
    newRow.className = 'med-row';
    newRow.innerHTML = `
        <div><label>Medication:</label><input name="med_names" list="med_suggestions" class="med-input" required></div>
        <div><label>Quantity:</label><input name="quantities" type="number" min="1" required></div>
        <div><button type="button" onclick="removeRow(this)">Remove</button></div>
    `;
    container.appendChild(newRow);
    addInputListener(newRow.querySelector('.med-input'), 'medication');
}

function removeRow(btn) {
    btn.closest('.med-row').remove();
    medRowCount--;
}

function addDiagRow() {
    if (diagRowCount >= 3) { alert('Maximum 3 diagnoses allowed.'); return; }
    diagRowCount++;
    const container = document.getElementById('diagnoses');
    const newRow = document.createElement('div');
    newRow.className = 'diag-row';
    newRow.innerHTML = `
        <div><label>Diagnosis:</label><input name="diagnoses" list="diag_suggestions" type="text" class="diag-input"></div>
        <div><button type="button" onclick="removeDiagRow(this)">Remove</button></div>
    `;
    container.appendChild(newRow);
    addInputListener(newRow.querySelector('.diag-input'), 'diagnosis');
}

function removeDiagRow(btn) {
    btn.closest('.diag-row').remove();
    diagRowCount--;
}

function clearForm() {
    // FIX (UX): also reset the hidden transaction_id so a Clear → Submit
    // creates a new record instead of updating the previously-edited one.
    document.getElementById('transaction_id_field').value = '';

    document.querySelector('.common-section').querySelectorAll('input, select').forEach(el => el.value = '');

    const diagContainer = document.getElementById('diagnoses');
    while (diagContainer.children.length > 1) diagContainer.removeChild(diagContainer.lastChild);
    diagContainer.firstElementChild.querySelectorAll('input').forEach(el => el.value = '');
    diagRowCount = 1;

    const medsContainer = document.getElementById('medications');
    while (medsContainer.children.length > 1) medsContainer.removeChild(medsContainer.lastChild);
    medsContainer.firstElementChild.querySelectorAll('input').forEach(el => el.value = '');
    medRowCount = 1;

    ['med_suggestions','diag_suggestions','company_suggestions','position_suggestions']
        .forEach(id => document.getElementById(id).innerHTML = '');
}

document.addEventListener('DOMContentLoaded', function() {
    const companyInput = document.getElementById('company');
    if (companyInput) addInputListener(companyInput, 'company');
    const positionInput = document.getElementById('position');
    if (positionInput) addInputListener(positionInput, 'position');
    document.querySelectorAll('.med-input').forEach(i => addInputListener(i, 'medication'));
    document.querySelectorAll('.diag-input').forEach(i => addInputListener(i, 'diagnosis'));
});

{% if message and ('successfully' in message|lower or 'updated' in message|lower) and 'partial success' not in message|lower %}
    {% if not tx_data %}
        clearForm();
    {% endif %}
{% endif %}
</script>
"""

RECEIVE_TEMPLATE = CSS_STYLE + MEDICATION_OPTIONS_JS + """
<h1>Receiving</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
{{ nav_links|safe }}

{% if message %}
<p class="message {% if 'successfully' in message|lower or 'deleted' in message|lower or 'reduced' in message|lower %}success{% else %}error{% endif %}">
    {{ message }}
</p>
{% endif %}

<h2>{% if rx_data %}Edit Receive Transaction{% else %}Receive Medication{% endif %}</h2>

<form method="POST"
      action="{% if rx_data %}{{ url_for('edit_receive', receive_id=rx_data.receive_id) }}{% else %}/receive{% endif %}"
      class="receive-form">

    {% if rx_data %}
        <input type="hidden" name="receive_id" value="{{ rx_data.receive_id }}">
    {% endif %}

    <div class="common-section">
        <div>
            <label>Medication:</label>
            <input name="med_name" id="med_name" list="med_suggestions"
                   value="{{ rx_data.med_name if rx_data else '' }}" required>
        </div>
        <div>
            <label>Quantity:</label>
            <input name="quantity" type="number" min="1"
                   value="{{ rx_data.quantity if rx_data else '' }}" required>
        </div>
        <div>
            <label>Batch:</label>
            <input name="batch" value="{{ rx_data.batch if rx_data else '' }}" required>
        </div>
        <div>
            <label>Price per Unit:</label>
            <input name="price" type="number" step="0.01" min="0"
                   value="{{ rx_data.price if rx_data else '' }}" required>
        </div>
        <div>
            <label>Expiry Date (YYYY-MM-DD):</label>
            <input name="expiry_date" type="date"
                   value="{{ rx_data.expiry_date if rx_data else '' }}" required>
        </div>
        <div>
            <label>Schedule:</label>
            <select name="schedule" required>
                <option value="">-- Select Schedule --</option>
                {% for opt in ['controlled', 'not controlled'] %}
                    <option value="{{ opt }}"
                            {% if rx_data and rx_data.schedule == opt %}selected{% endif %}>
                        {{ opt|title }}
                    </option>
                {% endfor %}
            </select>
        </div>
        <div>
            <label>Stock Receiver:</label>
            <input name="stock_receiver"
                   value="{{ rx_data.stock_receiver if rx_data else '' }}" required>
        </div>
        <div>
            <label>Order Number:</label>
            <input name="order_number"
                   value="{{ rx_data.order_number if rx_data else '' }}" required>
        </div>
        <div>
            <label>Supplier:</label>
            <input name="supplier"
                   value="{{ rx_data.supplier if rx_data else '' }}" required>
        </div>
        <div>
            <label>Invoice Number:</label>
            <input name="invoice_number"
                   value="{{ rx_data.invoice_number if rx_data else '' }}" required>
        </div>
    </div>

    <datalist id="med_suggestions"></datalist>

    <div class="form-buttons">
        <input type="submit" value="{% if rx_data %}Update Receive{% else %}Receive{% endif %}">
        {% if rx_data %}
            <a href="{{ url_for('receive', start_date=start_date, end_date=end_date, search=search) }}">
                <button type="button">Cancel</button>
            </a>
        {% else %}
            <button type="button"
                    onclick="document.querySelector('form.receive-form').reset();
                             document.getElementById('med_suggestions').innerHTML='';">
                Clear Form
            </button>
        {% endif %}
    </div>

    <input type="hidden" name="start_date" value="{{ start_date or '' }}">
    <input type="hidden" name="end_date"   value="{{ end_date   or '' }}">
    <input type="hidden" name="search"     value="{{ search     or '' }}">
</form>

<hr>

<h2>Receive Transactions</h2>

<form method="GET" action="{{ url_for('receive') }}" class="filter-form">
    <div class="filter-section">
        <div>
            <label>Start Date:</label>
            <input name="start_date" type="date" value="{{ start_date or '' }}">
        </div>
        <div>
            <label>End Date:</label>
            <input name="end_date" type="date" value="{{ end_date or '' }}">
        </div>
        <div>
            <label>Search:</label>
            <input name="search" type="text" value="{{ search or '' }}"
                   placeholder="Search medication, batch, supplier...">
        </div>
        <div class="button-div">
            <input type="submit" value="Filter">
            <a href="{{ url_for('receive') }}">Clear</a>
        </div>
    </div>
</form>

<table>
    <thead>
        <tr>
            <th>Medication</th><th>Quantity</th><th>Batch</th><th>Price</th>
            <th>Expiry Date</th><th>Stock Receiver</th><th>Order Number</th>
            <th>Supplier</th><th>Invoice Number</th><th>User</th>
            <th>Timestamp</th><th>Actions</th>
        </tr>
    </thead>
    <tbody>
        {% for t in tx_list %}
        <tr>
            <td>{{ t.med_name }}</td>
            <td>{{ t.quantity }}</td>
            <td>{{ t.batch }}</td>
            <td>${{ "%.2f"|format(t.price) }}</td>
            <td>{{ t.expiry_date }}</td>
            <td>{{ t.stock_receiver }}</td>
            <td>{{ t.order_number }}</td>
            <td>{{ t.supplier }}</td>
            <td>{{ t.invoice_number }}</td>
            <td>{{ t.user }}</td>
            <td>{{ t.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
            <td class="action-buttons">
                {% if session['user']['role'] != 'viewer' %}
                    <a href="{{ url_for('edit_receive',
                                        receive_id=t._id,
                                        start_date=start_date,
                                        end_date=end_date,
                                        search=search) }}">
                        <button type="button" class="edit-btn">Edit</button>
                    </a>
                {% endif %}
                {% if session['user']['role'] == 'admin' %}
                <form class="delete-btn" method="POST" action="{{ url_for('delete_receive') }}"
                        style="display:inline;"
                        onsubmit="return confirm('Permanently delete this receive entry?\\nStock will be reduced.');">
                    <input type="hidden" name="receive_id" value="{{ t._id }}">
                    <input type="hidden" name="start_date" value="{{ start_date or '' }}">
                    <input type="hidden" name="end_date"   value="{{ end_date   or '' }}">
                    <input type="hidden" name="search"     value="{{ search     or '' }}">
                    <button type="submit" class="delete-btn">Delete</button>
                </form>
                {% endif %}
                {% if session['user']['role'] == 'viewer' %}
                    <span>—</span>
                {% endif %}
            </td>
        </tr>
        {% else %}
        <tr><td colspan="12">No receive transactions.</td></tr>
        {% endfor %}
    </tbody>
</table>

<script>
document.addEventListener('DOMContentLoaded', async () => {
    const input    = document.getElementById('med_name');
    const datalist = document.getElementById('med_suggestions');
    if (!input) return;

    // FIX (Performance): options come from /api/medications, not an inline array
    const options = await getMedicationOptions();
    options.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m;
        datalist.appendChild(opt);
    });

    input.addEventListener('input', () => {
        const q = input.value.toLowerCase();
        Array.from(datalist.options).forEach(opt => {
            opt.style.display = opt.value.toLowerCase().includes(q) ? '' : 'none';
        });
    });
});
</script>
"""

ADD_MED_TEMPLATE = CSS_STYLE + MEDICATION_OPTIONS_JS + """
<h1>Add New Medication</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
{{ nav_links|safe }}
{% if message %}
    <p class="message {% if 'successfully' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}
<h2>Add Medication</h2>
<form method="POST" action="/add-medication" class="add-medication-form">
    <div class="common-section">
        <div>
            <label>Medication Name:</label>
            <input name="med_name" id="med_name" list="med_suggestions" required>
        </div>
        <div>
            <label>Initial Balance:</label>
            <input name="initial_balance" type="number" min="0" required>
        </div>
        <div>
            <label>Batch:</label>
            <input name="batch" required>
        </div>
        <div>
            <label>Price per Unit:</label>
            <input name="price" type="number" step="0.01" min="0" required>
        </div>
        <div>
            <label>Expiry Date (YYYY-MM-DD):</label>
            <input name="expiry_date" type="date" required>
        </div>
        <div>
            <label>Schedule:</label>
            <select name="schedule" required>
                <option value="">-- Select Schedule --</option>
                <option value="controlled">Controlled</option>
                <option value="not controlled">Not Controlled</option>
            </select>
        </div>
        <div>
            <label>Stock Receiver:</label>
            <input name="stock_receiver" required>
        </div>
        <div>
            <label>Order Number:</label>
            <input name="order_number" required>
        </div>
        <div>
            <label>Supplier:</label>
            <input name="supplier" required>
        </div>
        <div>
            <label>Invoice Number:</label>
            <input name="invoice_number" required>
        </div>
    </div>
    <datalist id="med_suggestions"></datalist>
    <div class="form-buttons">
        <input type="submit" value="Add Medication">
        <button type="button" onclick="document.querySelector('form').reset(); document.getElementById('med_suggestions').innerHTML='';">Clear Form</button>
    </div>
</form>
<script>
document.addEventListener('DOMContentLoaded', async function() {
    const medInput = document.getElementById('med_name');
    const datalist = document.getElementById('med_suggestions');
    // FIX (Performance): fetch from /api/medications
    const options = await getMedicationOptions();
    options.forEach(m => {
        const o = document.createElement('option');
        o.value = m;
        datalist.appendChild(o);
    });
    medInput.addEventListener('input', function() {
        const q = this.value.toLowerCase();
        Array.from(datalist.options).forEach(o => {
            o.style.display = o.value.toLowerCase().includes(q) ? '' : 'none';
        });
    });
});
</script>
"""

EDIT_MED_TEMPLATE = CSS_STYLE + """
<h1>Edit Medication</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
{{ nav_links|safe }}
{% if message %}
    <p class="message {% if 'successfully' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}
<h2>Edit {{ med_data.name if med_data else '' }}</h2>
<form method="POST" action="{{ url_for('edit_medication') }}" class="edit-medication-form">
    <div class="common-section">
        <div>
            <label>Medication Name:</label>
            <input name="med_name" value="{{ med_data.name if med_data else '' }}" readonly>
        </div>
        <div>
            <label>Balance:</label>
            <input name="balance" type="number" min="0" value="{{ med_data.balance if med_data else '' }}" required>
        </div>
        <div>
            <label>Batch:</label>
            <input name="batch" value="{{ med_data.batch if med_data else '' }}" required>
        </div>
        <div>
            <label>Price per Unit:</label>
            <input name="price" type="number" step="0.01" min="0" value="{{ med_data.price if med_data else '' }}" required>
        </div>
        <div>
            <label>Expiry Date (YYYY-MM-DD):</label>
            <input name="expiry_date" type="date" value="{{ med_data.expiry_date if med_data else '' }}" required>
        </div>
        <div>
            <label>Schedule:</label>
            <select name="schedule" required>
                <option value="">-- Select Schedule --</option>
                <option value="controlled" {% if med_data and med_data.schedule == 'controlled' %}selected{% endif %}>Controlled</option>
                <option value="not controlled" {% if med_data and med_data.schedule == 'not controlled' %}selected{% endif %}>Not Controlled</option>
            </select>
        </div>
    </div>
    <div class="form-buttons">
        <input type="submit" value="Update Medication">
        <a href="{{ url_for('reports', report_type='stock_on_hand') }}"><button type="button">Cancel</button></a>
    </div>
</form>
"""

REPORTS_TEMPLATE = CSS_STYLE + """
<h1>Inventory Reports</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
{{ nav_links|safe }}
{% if message %}
    <p class="message {% if 'successfully' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}
<h2>Generate Report</h2>
<form method="POST" action="{{ url_for('reports') }}">
    <label>Report Type:</label>
    <select name="report_type" required>
        <option value="stock_on_hand">Stock on Hand</option>
        <option value="expired_list">Expired Drugs List</option>
        <option value="near_expired_list">Near Expired Drug List</option>
        <option value="out_of_stock_list">Out of Stock List</option>
        <option value="inventory">Inventory Report</option>
        <option value="receive_list">Receive List</option>
        <option value="controlled_drug_register">Controlled Drug Register</option>
    </select><br>
    <label>Start Date (YYYY-MM-DD, if applicable):</label><input name="start_date" type="date"><br>
    <label>End Date (YYYY-MM-DD, if applicable):</label><input name="end_date" type="date"><br>
    <label>Search (optional):</label><input name="search" type="text" placeholder="Filter results by relevant fields"><br>
    <input type="submit" value="Generate Report">
</form>
{% if report_type in ['stock_on_hand', 'expired_list', 'near_expired_list', 'out_of_stock_list'] and stock_data %}
<form method="POST" action="{{ url_for('reports') }}" class="filter-form">
    <input type="hidden" name="report_type" value="{{ report_type }}">
    <div class="filter-section">
        <div>
            <label>Search Medication:</label>
            <input name="search" type="text" value="{{ search or '' }}" placeholder="Filter by medication name">
        </div>
        <div class="button-div">
            <input type="submit" value="Filter">
            <a href="{{ url_for('reports') }}">Back to Menu</a>
        </div>
    </div>
</form>
<h2>{{ report_title }}</h2>
<table>
    <thead>
        <tr>
            <th>Medication</th><th>Balance</th><th>Expiry Date</th><th>Batch</th><th>Price</th><th>Actions</th>
        </tr>
    </thead>
    <tbody>
    {% for med in stock_data %}
        <tr class="{{ med.status }}">
            <td>{{ med.name }}</td>
            <td>{{ med.balance }}</td>
            <td>{{ med.expiry_date }}</td>
            <td>{{ med.batch }}</td>
            <td>${{ "%.2f"|format(med.price) }}</td>
            <td class="action-buttons">
                {% if is_admin %}
                <a href="{{ url_for('edit_medication', med_name=med.name) }}"><button class="edit-btn">Edit</button></a>
                <form class="delete-btn" method="POST" action="{{ url_for('delete_medication') }}" style="display: inline;">
                    <input type="hidden" name="med_name" value="{{ med.name }}">
                    <button type="submit" class="delete-btn" onclick="return confirm('Are you sure you want to delete {{ med.name }}?');">Delete</button>
                </form>
                {% else %}
                <span>-</span>
                {% endif %}
            </td>
        </tr>
    {% else %}
        <tr><td colspan="6">No medications matching the criteria.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'inventory' and report_data %}
<form method="POST" action="{{ url_for('reports') }}" class="filter-form">
    <input type="hidden" name="report_type" value="inventory">
    <div class="filter-section">
        <div><label>Start Date:</label><input name="start_date" type="date" value="{{ start_date or '' }}"></div>
        <div><label>End Date:</label><input name="end_date" type="date" value="{{ end_date or '' }}"></div>
        <div><label>Search Medication:</label><input name="search" type="text" value="{{ search or '' }}" placeholder="Filter by medication name"></div>
        <div class="button-div"><input type="submit" value="Refine"><a href="{{ url_for('reports') }}">Back to Menu</a></div>
    </div>
</form>
<h2>Inventory Report for {{ start_date }} to {{ end_date }}</h2>
<table>
    <thead>
        <tr><th>Medication</th><th>Beginning Balance</th><th>Dispensed</th><th>Received</th><th>Current Balance</th><th>Amount to Order</th></tr>
    </thead>
    <tbody>
    {% for row in report_data %}
        <tr>
            <td>{{ row.med_name }}</td><td>{{ row.beginning_balance }}</td><td>{{ row.dispensed }}</td>
            <td>{{ row.received }}</td><td>{{ row.current_balance }}</td><td>{{ row.amount_to_order }}</td>
        </tr>
    {% else %}
        <tr><td colspan="6">No data for this period.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'receive_list' and receive_list %}
<form method="POST" action="{{ url_for('reports') }}" class="filter-form">
    <input type="hidden" name="report_type" value="receive_list">
    <div class="filter-section">
        <div><label>Start Date:</label><input name="start_date" type="date" value="{{ start_date or '' }}"></div>
        <div><label>End Date:</label><input name="end_date" type="date" value="{{ end_date or '' }}"></div>
        <div><label>Search:</label><input name="search" type="text" value="{{ search or '' }}" placeholder="Search medication, batch, supplier..."></div>
        <div class="button-div"><input type="submit" value="Refine"><a href="{{ url_for('reports') }}">Back to Menu</a></div>
    </div>
</form>
<h2>Receive List for {{ start_date }} to {{ end_date }}</h2>
<table>
    <thead>
        <tr><th>Medication</th><th>Quantity</th><th>Batch</th><th>Price</th><th>Expiry Date</th><th>Stock Receiver</th><th>Order Number</th><th>Supplier</th><th>Invoice Number</th><th>User</th><th>Timestamp</th></tr>
    </thead>
    <tbody>
    {% for t in receive_list %}
        <tr>
            <td>{{ t.med_name }}</td><td>{{ t.quantity }}</td><td>{{ t.batch }}</td>
            <td>${{ "%.2f"|format(t.price) }}</td><td>{{ t.expiry_date }}</td>
            <td>{{ t.stock_receiver }}</td><td>{{ t.order_number }}</td><td>{{ t.supplier }}</td>
            <td>{{ t.invoice_number }}</td><td>{{ t.user }}</td>
            <td>{{ t.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
        </tr>
    {% else %}
        <tr><td colspan="11">No receive transactions in this period.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'controlled_drug_register' and controlled_register %}
<form method="POST" action="{{ url_for('reports') }}" class="filter-form">
    <input type="hidden" name="report_type" value="controlled_drug_register">
    <div class="filter-section">
        <div><label>Start Date:</label><input name="start_date" type="date" value="{{ start_date or '' }}"></div>
        <div><label>End Date:</label><input name="end_date" type="date" value="{{ end_date or '' }}"></div>
        <div><label>Search:</label><input name="search" type="text" value="{{ search or '' }}" placeholder="Search transactions..."></div>
        <div class="button-div"><input type="submit" value="Refine"><a href="{{ url_for('reports') }}">Back to Menu</a></div>
    </div>
</form>
<h2>Controlled Drug Register for {{ start_date }} to {{ end_date }}</h2>
{% for reg in controlled_register %}
    <h3>{{ reg.med_name }} — Beginning: {{ reg.beginning_balance }} | Ending: {{ reg.ending_balance }} | Received: {{ reg.received }} | Dispensed: {{ reg.dispensed }}</h3>
    {% if reg.transactions %}
    <table>
        <thead>
            <tr><th>Date</th><th>Type</th><th>Quantity</th><th>Balance After</th><th>Prescriber</th><th>Issuer/Receiver</th><th>User</th><th>Reference/Patient</th></tr>
        </thead>
        <tbody>
        {% for tx in reg.transactions %}
            <tr>
                <td>{{ tx.get('date', tx.timestamp.strftime('%Y-%m-%d')) }}</td>
                <td>{{ tx.type }}</td><td>{{ tx.quantity }}</td><td>{{ tx.balance_after }}</td>
                <td>{{ tx.get('prescriber', '') }}</td>
                <td>{{ tx.get('dispenser', tx.get('stock_receiver', '')) }}</td>
                <td>{{ tx.get('user', '') }}</td>
                <td>{{ tx.get('patient', tx.get('order_number', tx.get('supplier', ''))) }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <p>No transactions in this period.</p>
    {% endif %}
{% endfor %}
{% else %}
<p>No controlled drugs found or no data for this period.</p>
{% endif %}
"""

# NEW: admin-only audit trail viewer. Surfaces every CREATE/UPDATE/DELETE
# recorded by audit_logger.py (audit_log collection) and every unhandled
# exception recorded by error_logger.py (error_logs collection) in one place.
AUDIT_TEMPLATE = CSS_STYLE + """
<h1>Audit &amp; Error Log</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
{{ nav_links|safe }}
{% if message %}
    <p class="message {% if 'deleted' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}

<form method="GET" action="{{ url_for('audit_log') }}" class="filter-form">
    <div class="filter-section">
        <div>
            <label>View:</label>
            <select name="view">
                <option value="changes" {% if view == 'changes' %}selected{% endif %}>Edits &amp; Deletes</option>
                <option value="warnings" {% if view == 'warnings' %}selected{% endif %}>Business Warnings (Not Found / Insufficient Stock)</option>
                <option value="errors" {% if view == 'errors' %}selected{% endif %}>Application Errors</option>
            </select>
        </div>
        <div>
            <label>Start Date:</label>
            <input name="start_date" type="date" value="{{ start_date or '' }}">
        </div>
        <div>
            <label>End Date:</label>
            <input name="end_date" type="date" value="{{ end_date or '' }}">
        </div>
        <div>
            <label>Search:</label>
            <input name="search" type="text" value="{{ search or '' }}" placeholder="User, action, target, medication...">
        </div>
        <div class="button-div">
            <input type="submit" value="Filter">
            <a href="{{ url_for('audit_log') }}">Clear</a>
        </div>
    </div>
</form>

{% if view == 'changes' %}
<h2>Edits &amp; Deletes ({{ entries|length }})</h2>
<table>
    <thead>
        <tr>
            <th>Timestamp</th>
            <th>Action</th>
            <th>Record Type</th>
            <th>Target</th>
            <th>User</th>
            <th>IP</th>
            <th>Details</th>
        </tr>
    </thead>
    <tbody>
    {% for e in entries %}
        <tr class="{% if e.action == 'DELETE' %}expired{% elif e.action == 'UPDATE' %}close-to-expire{% else %}normal{% endif %}">
            <td>{{ e.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
            <td>{{ e.action }}</td>
            <td>{{ e.target_type }}</td>
            <td>{{ e.target_id }}</td>
            <td>{{ e.user }}</td>
            <td>{{ e.ip }}</td>
            <td><pre style="white-space: pre-wrap; margin: 0; font-size: 12px;">{{ e.changes }}</pre></td>
        </tr>
    {% else %}
        <tr><td colspan="7">No edit or delete records found.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif view == 'warnings' %}
<h2>Business Warnings ({{ entries|length }})</h2>
<p>Handled conditions that are not bugs — a medication wasn't found, stock was insufficient, or a transaction no longer exists. These don't crash the app, but are worth reviewing.</p>
<table>
    <thead>
        <tr>
            <th>Timestamp</th>
            <th>Type</th>
            <th>Message</th>
            <th>User</th>
            <th>Path</th>
            <th>Details</th>
        </tr>
    </thead>
    <tbody>
    {% for e in entries %}
        <tr class="close-to-expire">
            <td>{{ e.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
            <td>{{ e.warning_type }}</td>
            <td>{{ e.message }}</td>
            <td>{{ e.user }}</td>
            <td>{{ e.path }}</td>
            <td><pre style="white-space: pre-wrap; margin: 0; font-size: 12px;">{{ e.context }}</pre></td>
        </tr>
    {% else %}
        <tr><td colspan="6">No business warnings found.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% else %}
<h2>Application Errors ({{ entries|length }})</h2>
<table>
    <thead>
        <tr>
            <th>Timestamp</th>
            <th>Method</th>
            <th>Path</th>
            <th>Endpoint</th>
            <th>Remote Addr</th>
            <th>Traceback (last line)</th>
        </tr>
    </thead>
    <tbody>
    {% for e in entries %}
        <tr class="expired">
            <td>{{ e.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
            <td>{{ e.method }}</td>
            <td>{{ e.path }}</td>
            <td>{{ e.endpoint }}</td>
            <td>{{ e.remote_addr }}</td>
            <td><pre style="white-space: pre-wrap; margin: 0; font-size: 12px;">{{ e.traceback[-1] if e.traceback else '' }}</pre></td>
        </tr>
    {% else %}
        <tr><td colspan="6">No application errors found.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% endif %}
"""

LOGIN_TEMPLATE = CSS_STYLE + """
<h1>Pharmacy App Login</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
<div class="login-form">
    <h2>Login</h2>
    {% if error %}<p class="message error">{{ error }}</p>{% endif %}
    <form method="POST">
        <label>Username:</label><input type="text" name="username" required><br>
        <label>Password:</label><input type="password" name="password" required><br>
        <input type="submit" value="Login">
    </form>
    <p><a href="/register">Don't have an account? Register here.</a></p>
</div>
"""

# NEW: forced password-change page, shown when a user's password is older
# than PASSWORD_MAX_AGE_DAYS (or has no recorded set-date at all). The user
# is already authenticated at this point (they just entered the correct
# current password to get here) but is blocked from everything else in the
# app until they set a new one — enforced via before_request below.
CHANGE_EXPIRED_PASSWORD_TEMPLATE = CSS_STYLE + """
<h1>Password Expired</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
<div class="login-form">
    <h2>Set a New Password</h2>
    <p>Your password is over a year old and must be changed before you can continue.</p>
    {% if error %}<p class="message error">{{ error }}</p>{% endif %}
    <form method="POST">
        <label>Current Password:</label><input type="password" name="current_password" required><br>
        <label>New Password:</label><input type="password" name="new_password" required><br>
        <label>Confirm New Password:</label><input type="password" name="confirm_password" required><br>
        <input type="submit" value="Change Password">
    </form>
    <p><a href="/logout">Log out instead</a></p>
</div>
"""

REGISTER_PASSWORD_TEMPLATE = CSS_STYLE + """
<h1>Pharmacy App Registration</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
<div class="register-form">
    <h2>Admin Access Required</h2>
    {% if error %}<p class="message error">{{ error }}</p>{% endif %}
    <form method="POST">
        <label>Admin Password:</label><input type="password" name="admin_pass" required><br>
        <input type="submit" value="Access Registration">
    </form>
    <p><a href="/login">Back to Login</a></p>
</div>
"""

REGISTER_TEMPLATE = CSS_STYLE + """
<h1>Pharmacy App Registration</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
<div class="register-form">
    <h2>Register</h2>
    {% if error %}<p class="message error">{{ error }}</p>{% endif %}
    {% if message %}<p class="message success">{{ message }}</p>{% endif %}
    <form method="POST">
        <label>Username:</label><input type="text" name="username" required><br>
        <label>Password:</label><input type="password" name="password" required><br>
        <label>Full Name:</label><input type="text" name="name" required><br>
        <label>Role:</label>
        <select name="role" required>
            <option value="employee">Employee</option>
            <option value="viewer">Viewer (no patient names)</option>
            <option value="admin">Admin</option>
        </select><br>
        <input type="submit" value="Register">
    </form>
    <p><a href="/login">Already have an account? Login here.</a></p>
</div>
"""

# ============================================================
# Routes
# ============================================================

@app.route('/', methods=['GET'])
@login_required
def home():
    return redirect('/reports')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if not username or not password:
            session['error'] = 'Username and password are required.'
            return redirect('/login')
        try:
            client = get_mongo_client()
            db = client['pharmacy_db']
            users = db['users']
            user_doc = users.find_one({'username': username})
            if user_doc and check_password_hash(user_doc['password_hash'], password):
                # NEW: account expiry check (2 years). This runs before the
                # password expiry check and before any session is created —
                # an expired account is blocked from logging in entirely,
                # not given a session that then gets redirected somewhere.
                # Same grandfathering approach as password_set_at: existing
                # users without a recorded account_created_at get it backfilled
                # to now rather than being retroactively expired on day one.
                account_created_at = user_doc.get('account_created_at')
                if not account_created_at:
                    account_created_at = datetime.now(timezone.utc)
                    users.update_one(
                        {'username': username},
                        {'$set': {'account_created_at': account_created_at}}
                    )
                    account_expired = False
                else:
                    if account_created_at.tzinfo is None:
                        account_created_at = account_created_at.replace(tzinfo=timezone.utc)
                    account_age = datetime.now(timezone.utc) - account_created_at
                    account_expired = account_age > timedelta(days=ACCOUNT_MAX_AGE_DAYS)

                if account_expired:
                    session['error'] = (
                        'Your account has expired and must be re-registered by an admin. '
                        'Please contact your administrator.'
                    )
                    return redirect('/login')

                # NEW: password expiry check. Existing users created before
                # this feature won't have a password_set_at field. Rather
                # than mass-expiring every existing account the moment this
                # ships, grandfather them in: backfill password_set_at to
                # now on this first login, so their year starts counting
                # from today. Only accounts with a recorded date older than
                # PASSWORD_MAX_AGE_DAYS are treated as expired going forward.
                password_set_at = user_doc.get('password_set_at')
                if not password_set_at:
                    password_set_at = datetime.now(timezone.utc)
                    users.update_one(
                        {'username': username},
                        {'$set': {'password_set_at': password_set_at}}
                    )
                    password_expired = False
                else:
                    if password_set_at.tzinfo is None:
                        password_set_at = password_set_at.replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - password_set_at
                    password_expired = age > timedelta(days=PASSWORD_MAX_AGE_DAYS)

                session['user'] = {
                    'login': username,
                    'name': user_doc.get('name', username),
                    'role': user_doc.get('role', 'employee')
                }
                session.permanent = True
                session['last_active'] = datetime.now(timezone.utc).isoformat()

                if password_expired:
                    session['must_change_password'] = True
                    flash('Your password has expired and must be changed before continuing.', 'error')
                    return redirect('/change-expired-password')

                return redirect('/dispense')
            else:
                session['error'] = 'Invalid username or password.'
        except ServerSelectionTimeoutError:
            session['error'] = 'Database connection failed. Please try again later.'
        return redirect('/login')

    error = session.pop('error', None)
    return render_template_string(LOGIN_TEMPLATE, error=error)


@app.route('/change-expired-password', methods=['GET', 'POST'])
@login_required
def change_expired_password():
    # If a user reaches this page without actually having an expired
    # password (e.g. they bookmarked it, or navigated back after already
    # changing it), just send them on to the app rather than show a
    # confusing forced form with nothing to enforce.
    if not session.get('must_change_password'):
        return redirect('/dispense')

    error = None
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not current_password or not new_password or not confirm_password:
            error = 'All fields are required.'
        elif new_password != confirm_password:
            error = 'New password and confirmation do not match.'
        elif len(new_password) < 8:
            error = 'New password must be at least 8 characters.'
        elif new_password == current_password:
            error = 'New password must be different from your current password.'
        else:
            try:
                client = get_mongo_client()
                db = client['pharmacy_db']
                users = db['users']
                username = session['user']['login']
                user_doc = users.find_one({'username': username})

                if not user_doc or not check_password_hash(user_doc['password_hash'], current_password):
                    error = 'Current password is incorrect.'
                else:
                    users.update_one(
                        {'username': username},
                        {'$set': {
                            'password_hash': generate_password_hash(new_password),
                            'password_set_at': datetime.now(timezone.utc),
                        }}
                    )
                    session.pop('must_change_password', None)
                    flash('Password changed successfully. Please log in again with your new password.', 'success')
                    session.clear()
                    return redirect('/login')
            except ServerSelectionTimeoutError:
                error = 'Database connection failed. Please try again later.'

    return render_template_string(CHANGE_EXPIRED_PASSWORD_TEMPLATE, error=error)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        if 'admin_pass' in request.form:
            if not ADMIN_PASSWORD:
                session['error'] = 'Admin password not configured. Contact system administrator.'
                return redirect('/register')
            if request.form['admin_pass'] == ADMIN_PASSWORD:
                session['admin_access'] = True
                return redirect('/register')
            else:
                session['error'] = 'Incorrect admin password.'
                return redirect('/register')
        else:
            if 'admin_access' not in session:
                session['error'] = 'Admin access required.'
                return redirect('/register')
            username = request.form.get('username')
            password = request.form.get('password')
            name = request.form.get('name')
            role = request.form.get('role')
            if not username or not password or not name or not role:
                session['error'] = 'All fields are required.'
                return redirect('/register')
            try:
                client = get_mongo_client()
                db = client['pharmacy_db']
                users = db['users']
                existing = users.find_one({'username': username})

                if existing:
                    # NEW: allow re-registration ONLY if this existing
                    # account has actually expired (2+ years old). This is
                    # the admin-driven re-registration flow for expired
                    # accounts — re-registering replaces the password and
                    # resets both the account and password clocks. Active
                    # accounts are still protected from being silently
                    # overwritten by reusing their username.
                    created_at = existing.get('account_created_at')
                    is_expired = False
                    if created_at:
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        is_expired = (datetime.now(timezone.utc) - created_at) > timedelta(days=ACCOUNT_MAX_AGE_DAYS)

                    if not is_expired:
                        session['error'] = 'Username already exists.'
                        return redirect('/register')

                    now = datetime.now(timezone.utc)
                    users.update_one(
                        {'username': username},
                        {'$set': {
                            'password_hash': generate_password_hash(password),
                            'name': name,
                            'role': role,
                            'account_created_at': now,
                            'password_set_at': now,
                        }}
                    )
                    # NEW: log re-registration of an expired account. register()
                    # has no @login_required and no session['user'] to attribute
                    # this to (it's gated by the shared ADMIN_PASSWORD, not a
                    # per-admin login), so this is written directly here rather
                    # than via audit_logger.py's usual decorator pattern.
                    db['audit_log'].insert_one({
                        'audit_id': str(uuid4()),
                        'timestamp': now,
                        'action': 'UPDATE',
                        'target_type': 'user_account',
                        'target_id': username,
                        'changes': {
                            'reason': 'expired account re-registered',
                            'old_account_created_at': str(created_at),
                            'new_name': name,
                            'new_role': role,
                        },
                        'user': 'admin (via /register admin password)',
                        'ip': request.remote_addr,
                        'user_agent': request.headers.get('User-Agent'),
                    })
                    session['message'] = f'Account "{username}" re-registered successfully! Please login.'
                    session.pop('admin_access', None)
                    return redirect('/login')

                now = datetime.now(timezone.utc)
                password_hash = generate_password_hash(password)
                users.insert_one({
                    'username': username,
                    'password_hash': password_hash,
                    'name': name,
                    'role': role,
                    'account_created_at': now,
                    'password_set_at': now,
                })
                session['message'] = 'Registration successful! Please login.'
                session.pop('admin_access', None)
                return redirect('/login')
            except ServerSelectionTimeoutError:
                session['error'] = 'Database connection failed. Please try again later.'
            return redirect('/register')

    error = session.pop('error', None)
    message = session.pop('message', None)
    if 'admin_access' not in session:
        return render_template_string(REGISTER_PASSWORD_TEMPLATE, error=error)
    else:
        return render_template_string(REGISTER_TEMPLATE, error=error, message=message)


@app.route('/logout', methods=['GET'])
def logout():
    session.pop('user', None)
    session.pop('admin_access', None)
    return redirect('/login')


@app.route('/dispense', methods=['GET', 'POST'])
@login_required
def dispense():
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        # Read any flashed message from a preceding redirect (e.g. from
        # delete_dispense success/error, or access-denied from other routes).
        flashed = get_flashed_messages()
        message = flashed[0] if flashed else None
        start_date = request.values.get('start_date')
        end_date = request.values.get('end_date')
        search = request.values.get('search')
        current_user = session['user']['name']

        base_query = {'type': 'dispense'}
        date_query = {}
        if start_date:
            date_query['$gte'] = datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
            date_query['$lte'] = end_dt
        if date_query:
            base_query['timestamp'] = date_query
        if search:
            base_query['$or'] = [
                {'patient':   {'$regex': search, '$options': 'i'}},
                {'med_name':  {'$regex': search, '$options': 'i'}},
                {'company':   {'$regex': search, '$options': 'i'}},
                {'position':  {'$regex': search, '$options': 'i'}},
                {'age_group': {'$regex': search, '$options': 'i'}},
                {'gender':    {'$regex': search, '$options': 'i'}},
                {'prescriber':{'$regex': search, '$options': 'i'}},
                {'dispenser': {'$regex': search, '$options': 'i'}},
                {'date':      {'$regex': search, '$options': 'i'}},
                {'diagnoses.0':{'$regex': search, '$options': 'i'}},
            ]

        raw_tx = list(transactions.find(base_query).sort('timestamp', -1))

        # FIX (UX/Correctness): group rows by transaction_id in Python so the
        # template can use reliable rowspan values regardless of sort order or
        # shared timestamps.  Use an OrderedDict to preserve display order.
        from collections import OrderedDict
        grouped = OrderedDict()
        for t in raw_tx:
            tid = t['transaction_id']
            grouped.setdefault(tid, []).append(t)
        tx_groups = list(grouped.items())   # [(tx_id, [rows]), ...]
        # Keep flat list for backwards-compat with anything that needs it
        tx_list = raw_tx

        tx_data = None
        edit_id = request.args.get('edit')
        if edit_id:
            tx = transactions.find_one({'transaction_id': edit_id, 'type': 'dispense'})
            if tx:
                common = {k: v for k, v in tx.items()
                          if k not in ['_id', 'med_name', 'quantity', 'type', 'timestamp', 'transaction_id', 'user']}
                meds_cursor = transactions.find(
                    {'transaction_id': edit_id, 'type': 'dispense'},
                    {'med_name': 1, 'quantity': 1}
                )
                common['meds'] = [(m['med_name'], m['quantity']) for m in meds_cursor]
                common['diags'] = common.get('diagnoses', [])
                common['transaction_id'] = edit_id
                tx_data = common

        if request.method == 'POST':
            transaction_id = request.form.get('transaction_id')
            if transaction_id:
                old_meds = list(transactions.find({'transaction_id': transaction_id}))
                for old_tx in old_meds:
                    medications.update_one({'name': old_tx['med_name']}, {'$inc': {'balance': old_tx['quantity']}})
                transactions.delete_many({'transaction_id': transaction_id})
                tx_id = transaction_id
                message_prefix = 'Updated'
            else:
                tx_id = str(uuid4())
                message_prefix = 'Dispensed'

            try:
                patient       = request.form['patient']
                company       = request.form['company']
                position      = request.form['position']
                age_group     = request.form['age_group']
                prescriber    = request.form['prescriber']
                dispenser     = request.form['dispenser']
                date_str      = request.form['date']
                gender        = request.form['gender']
                sick_leave_days = int(request.form['sick_leave_days'])
                diagnoses     = [d.strip() for d in request.form.getlist('diagnoses') if d.strip()]

                if not diagnoses:
                    message = 'Please provide at least one diagnosis.'
                else:
                    med_names = [n.strip() for n in request.form.getlist('med_names') if n.strip()]
                    quantities = []
                    for q_str in request.form.getlist('quantities'):
                        try:
                            qty = int(q_str)
                            if qty > 0:
                                quantities.append(qty)
                        except ValueError:
                            pass

                    if len(med_names) != len(quantities) or not med_names:
                        message = 'Please provide at least one valid medication and quantity.'
                    else:
                        error_msgs = []
                        dispensed_meds = []
                        for med_name, quantity in zip(med_names, quantities):
                            med = medications.find_one({'name': med_name})
                            if not med:
                                error_msgs.append(f'Medication "{med_name}" not found.')
                                write_app_warning(
                                    'medication_not_found',
                                    f'Medication "{med_name}" not found during dispense.',
                                    {'med_name': med_name, 'requested_quantity': quantity,
                                     'patient': patient, 'transaction_id': tx_id}
                                )
                            elif med.get('balance', 0) < quantity:
                                error_msgs.append(f'Insufficient stock for "{med_name}".')
                                write_app_warning(
                                    'insufficient_stock',
                                    f'Insufficient stock for "{med_name}" during dispense.',
                                    {'med_name': med_name, 'requested_quantity': quantity,
                                     'available_quantity': med.get('balance', 0),
                                     'patient': patient, 'transaction_id': tx_id}
                                )
                            else:
                                medications.update_one({'name': med_name}, {'$inc': {'balance': -quantity}})
                                transactions.insert_one({
                                    'type': 'dispense',
                                    'transaction_id': tx_id,
                                    'patient': patient,
                                    'company': company,
                                    'position': position,
                                    'age_group': age_group,
                                    'gender': gender,
                                    'sick_leave_days': sick_leave_days,
                                    'diagnoses': diagnoses,
                                    'prescriber': prescriber,
                                    'dispenser': dispenser,
                                    'user': current_user,
                                    'date': date_str,
                                    'med_name': med_name,
                                    'quantity': quantity,
                                    'timestamp': datetime.utcnow()
                                })
                                dispensed_meds.append(med_name)

                        if dispensed_meds and not error_msgs:
                            message = f'{message_prefix} successfully: {", ".join(dispensed_meds)}'
                        elif dispensed_meds:
                            message = f'Partial success: {", ".join(dispensed_meds)}. Errors: {"; ".join(error_msgs)}'
                        else:
                            message = '; '.join(error_msgs) or f'No medications {message_prefix.lower()}.'
            except ValueError as e:
                message = f'Invalid input: {str(e)}'

        return render_template_string(
            DISPENSE_TEMPLATE,
            tx_list=tx_list,
            tx_groups=tx_groups,
            nav_links=get_nav_links(),
            message=message,
            start_date=start_date,
            end_date=end_date,
            search=search,
            tx_data=tx_data
        )
    except ServerSelectionTimeoutError:
        return render_template_string(
            DISPENSE_TEMPLATE,
            tx_list=[], tx_groups=[],
            nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            start_date='', end_date='', search='', tx_data=None
        ), 500


@app.route('/receive', methods=['GET', 'POST'])
@login_required
def receive():
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        # Read any flashed message from a preceding redirect (e.g. from
        # delete_receive success/error, or access-denied from other routes).
        flashed = get_flashed_messages()
        message = flashed[0] if flashed else None
        start_date = request.values.get('start_date')
        end_date = request.values.get('end_date')
        search = request.values.get('search')
        current_user = session['user']['name']

        base_query = {'type': 'receive'}
        date_query = {}
        if start_date:
            date_query['$gte'] = datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
            date_query['$lte'] = end_dt
        if date_query:
            base_query['timestamp'] = date_query
        if search:
            base_query['$or'] = [
                {'med_name':       {'$regex': search, '$options': 'i'}},
                {'batch':          {'$regex': search, '$options': 'i'}},
                {'supplier':       {'$regex': search, '$options': 'i'}},
                {'stock_receiver': {'$regex': search, '$options': 'i'}},
                {'order_number':   {'$regex': search, '$options': 'i'}},
                {'invoice_number': {'$regex': search, '$options': 'i'}},
                {'expiry_date':    {'$regex': search, '$options': 'i'}},
            ]

        tx_list = list(transactions.find(base_query).sort('timestamp', -1))

        # FIX (Bug): the original GET-based edit path used {'_id': edit_id} (a raw
        # string) which never matched a MongoDB ObjectId and always returned None.
        # The edit-receive route handles this properly, so the receive route now
        # simply never tries to load rx_data inline — editing always goes via
        # /edit-receive/<id>.
        rx_data = None

        if request.method == 'POST':
            try:
                med_name       = request.form['med_name']
                quantity       = int(request.form['quantity'])
                batch          = request.form['batch']
                price          = float(request.form['price'])
                expiry_date    = request.form['expiry_date']
                schedule       = request.form['schedule']
                stock_receiver = request.form['stock_receiver']
                order_number   = request.form['order_number']
                supplier       = request.form['supplier']
                invoice_number = request.form['invoice_number']

                medications.update_one(
                    {'name': med_name},
                    {'$inc': {'balance': quantity},
                     '$set': {
                         'batch': batch, 'price': price, 'expiry_date': expiry_date,
                         'schedule': schedule, 'stock_receiver': stock_receiver,
                         'order_number': order_number, 'supplier': supplier,
                         'invoice_number': invoice_number
                     }},
                    upsert=True
                )
                transactions.insert_one({
                    'type': 'receive',
                    'med_name': med_name, 'quantity': quantity, 'batch': batch,
                    'price': price, 'expiry_date': expiry_date, 'schedule': schedule,
                    'stock_receiver': stock_receiver, 'order_number': order_number,
                    'supplier': supplier, 'invoice_number': invoice_number,
                    'user': current_user, 'timestamp': datetime.utcnow()
                })
                message = 'Received successfully!'
            except ValueError as e:
                message = f'Invalid input: {str(e)}'

        return render_template_string(
            RECEIVE_TEMPLATE,
            tx_list=tx_list, nav_links=get_nav_links(),
            message=message, start_date=start_date, end_date=end_date,
            search=search, rx_data=rx_data
        )
    except ServerSelectionTimeoutError:
        return render_template_string(
            RECEIVE_TEMPLATE, tx_list=[], nav_links=get_nav_links(),
            message="Database connection failed.", start_date='', end_date='', search='', rx_data=None
        ), 500


@app.route('/add-medication', methods=['GET', 'POST'])
@login_required
def add_medication():
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can add new medications.')
        return redirect('/reports')
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        message = None
        current_user = session['user']['name']

        if request.method == 'POST':
            try:
                med_name       = request.form['med_name']
                initial_balance= int(request.form['initial_balance'])
                batch          = request.form['batch']
                price          = float(request.form['price'])
                expiry_date    = request.form['expiry_date']
                schedule       = request.form['schedule']
                stock_receiver = request.form['stock_receiver']
                order_number   = request.form['order_number']
                supplier       = request.form['supplier']
                invoice_number = request.form['invoice_number']

                if medications.find_one({'name': med_name}):
                    message = f'Medication "{med_name}" already exists. Use Receiving to add stock.'
                    return render_template_string(ADD_MED_TEMPLATE, nav_links=get_nav_links(), message=message)

                medications.insert_one({
                    'name': med_name, 'balance': initial_balance, 'batch': batch,
                    'price': price, 'expiry_date': expiry_date, 'schedule': schedule,
                    'stock_receiver': stock_receiver, 'order_number': order_number,
                    'supplier': supplier, 'invoice_number': invoice_number
                })
                transactions.insert_one({
                    'type': 'receive', 'med_name': med_name, 'quantity': initial_balance,
                    'batch': batch, 'price': price, 'expiry_date': expiry_date,
                    'schedule': schedule, 'stock_receiver': stock_receiver,
                    'order_number': order_number, 'supplier': supplier,
                    'invoice_number': invoice_number,
                    'user': current_user, 'timestamp': datetime.utcnow()
                })
                message = 'Medication added successfully!'
                return render_template_string(ADD_MED_TEMPLATE, nav_links=get_nav_links(), message=message)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template_string(ADD_MED_TEMPLATE, nav_links=get_nav_links(), message=message)

        return render_template_string(ADD_MED_TEMPLATE, nav_links=get_nav_links(), message=message)
    except ServerSelectionTimeoutError:
        return render_template_string(
            ADD_MED_TEMPLATE, nav_links=get_nav_links(),
            message="Database connection failed. Please try again later."
        ), 500


@app.route('/edit-medication', methods=['GET', 'POST'])
@login_required
def edit_medication():
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can edit medications.')
        return redirect('/reports')
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        message = None

        # FIX (Bug): med_name used to be a URL path segment
        # (/edit-medication/<med_name>). Path segments containing spaces,
        # commas, etc. require percent-encoding, and different hosting
        # platforms decode that path differently before Werkzeug's router
        # ever sees it — this worked on Render (which passes the raw request
        # straight through to gunicorn/Werkzeug) but broke on Vercel (whose
        # Python runtime sits a proxy/adapter in front that normalizes the
        # path inconsistently, leaving a stray layer of %-encoding no matter
        # how the link was built). Query string values and form POST bodies
        # don't go through that same path-segment handling, so they decode
        # identically everywhere. GET now reads med_name from ?med_name=...,
        # POST reads it from the form body (already used a hidden/readonly
        # input for this, so no template change needed there).
        if request.method == 'POST':
            med_name = request.form.get('med_name')
        else:
            med_name = request.args.get('med_name')

        if not med_name:
            message = 'No medication specified.'
            return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(),
                                          message=message, med_data=None, med_name=None)

        med = medications.find_one({'name': med_name})
        if not med:
            message = f'Medication "{med_name}" not found.'
            write_app_warning(
                'medication_not_found',
                f'Medication "{med_name}" not found when attempting to edit.',
                {'med_name': med_name}
            )
            return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(),
                                          message=message, med_data=None, med_name=med_name)

        med_data = med
        if request.method == 'POST':
            try:
                balance     = int(request.form['balance'])
                batch       = request.form['batch']
                price       = float(request.form['price'])
                expiry_date = request.form['expiry_date']
                schedule    = request.form['schedule']
                medications.update_one(
                    {'name': med_name},
                    {'$set': {'balance': balance, 'batch': batch, 'price': price,
                              'expiry_date': expiry_date, 'schedule': schedule}}
                )
                message = 'Medication updated successfully!'
                med_data = medications.find_one({'name': med_name})
                return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(),
                                              message=message, med_data=med_data, med_name=med_name)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(),
                                              message=message, med_data=med_data, med_name=med_name)

        return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(),
                                      message=message, med_data=med_data, med_name=med_name)
    except ServerSelectionTimeoutError:
        # FIX: med_name may not be assigned yet if get_mongo_client() itself
        # raised this before reaching that line — read it defensively here
        # rather than risk an UnboundLocalError masking the real DB error.
        fallback_med_name = (request.form.get('med_name') if request.method == 'POST'
                             else request.args.get('med_name'))
        return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(),
                                      message="Database connection failed.", med_data=None, med_name=fallback_med_name), 500


@app.route('/delete-medication', methods=['POST'])
@login_required
def delete_medication():
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can delete medications.')
        return redirect('/reports')
    med_name = request.form.get('med_name')
    if not med_name:
        session['message'] = 'No medication specified.'
        return redirect('/reports')
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        med = medications.find_one({'name': med_name})
        if not med:
            session['message'] = f'Medication "{med_name}" not found.'
            write_app_warning(
                'medication_not_found',
                f'Medication "{med_name}" not found when attempting to delete.',
                {'med_name': med_name}
            )
            return redirect('/reports')
        result = medications.delete_one({'name': med_name})
        if result.deleted_count > 0:
            session['message'] = f'Medication "{med_name}" deleted successfully.'
        else:
            session['message'] = f'Failed to delete "{med_name}".'
    except Exception as e:
        session['message'] = f'Error deleting medication: {str(e)}'
    return redirect('/reports')


@app.route('/reports', methods=['GET', 'POST'])
@login_required
def reports():
    is_admin = session['user'].get('role') == 'admin'
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']

        report_data = []
        receive_list = []
        stock_data = []
        controlled_register = []
        report_type = None
        start_date = None
        end_date = None
        total_transactions = 0
        message = session.pop('message', None)
        # Also read any flashed message from a preceding redirect (e.g.
        # access-denied from add/edit/delete medication routes).
        if not message:
            flashed = get_flashed_messages()
            message = flashed[0] if flashed else None
        search = None
        report_title = None
        start_dt = None
        end_dt = None

        def matches_search(tx, search_str):
            if not search_str:
                return True
            sl = search_str.lower()
            for field in ['patient', 'med_name', 'company', 'position', 'prescriber',
                          'dispenser', 'stock_receiver', 'order_number', 'supplier',
                          'invoice_number', 'batch', 'user']:
                if sl in str(tx.get(field, '')).lower():
                    return True
            diagnoses = tx.get('diagnoses', [])
            if isinstance(diagnoses, list):
                if sl in ' '.join(str(d).lower() for d in diagnoses):
                    return True
            return False

        stock_report_types = ['stock_on_hand', 'expired_list', 'near_expired_list', 'out_of_stock_list']

        if request.method == 'POST':
            report_type = request.form.get('report_type')
            start_date  = request.form.get('start_date')
            end_date    = request.form.get('end_date')
            search      = request.form.get('search')

            if report_type:
                try:
                    if start_date:
                        start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    if end_date:
                        end_dt = (datetime.strptime(end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                                  + timedelta(days=1) - timedelta(seconds=1))

                    if report_type in stock_report_types:
                        if not end_date:
                            raise ValueError('End date is required for this report type.')
                        report_date     = datetime.strptime(end_date, '%Y-%m-%d').date()
                        threshold_date  = report_date + timedelta(days=30)
                        now_dt          = datetime.now(timezone.utc)
                        med_filter      = {'name': {'$regex': search or '', '$options': 'i'}} if search else {}
                        all_meds        = list(medications.find(med_filter, {'_id': 0}).sort('name', 1))
                        stock_data      = []
                        for med in all_meds:
                            med_name = med['name']
                            current_balance = med.get('balance', 0)
                            try:
                                period_pipeline = [
                                    {'$match': {'med_name': med_name, 'timestamp': {'$gt': end_dt, '$lte': now_dt}}},
                                    {'$group': {'_id': None,
                                                'dispensed': {'$sum': {'$cond': [{'$eq': ['$type','dispense']}, '$quantity', 0]}},
                                                'received':  {'$sum': {'$cond': [{'$eq': ['$type','receive']},  '$quantity', 0]}}}}
                                ]
                                pr = list(transactions.aggregate(period_pipeline))
                                dispensed_after = pr[0].get('dispensed', 0) if pr else 0
                                received_after  = pr[0].get('received',  0) if pr else 0
                                balance_at_date = max(0, current_balance - received_after + dispensed_after)
                            except Exception as qe:
                                app.logger.error(f"Query failed for med {med_name}: {qe}")
                                balance_at_date = current_balance

                            expiry_str = med.get('expiry_date')
                            expiry_dt = None
                            if expiry_str:
                                try:
                                    date_part = expiry_str.split('T')[0]
                                    expiry_dt = datetime.strptime(date_part, '%Y-%m-%d').date()
                                except ValueError as e:
                                    app.logger.warning(f"Invalid expiry_date '{expiry_str}' for '{med_name}': {e}")

                            if not med.get('batch'):
                                med['batch'] = 'N/A'

                            if balance_at_date == 0:
                                status = 'out-of-stock'
                            elif expiry_dt is None:
                                status = 'normal'
                            elif expiry_dt < report_date:
                                status = 'expired'
                            elif expiry_dt <= threshold_date:
                                status = 'close-to-expire'
                            else:
                                status = 'normal'

                            med_copy = med.copy()
                            med_copy['balance'] = balance_at_date
                            med_copy['status']  = status
                            stock_data.append(med_copy)

                        if report_type == 'stock_on_hand':
                            report_title = f'Stock on Hand as of {end_date}'
                        elif report_type == 'expired_list':
                            stock_data   = [m for m in stock_data if m['status'] == 'expired']
                            report_title = f'Expired Drugs List as of {end_date}'
                        elif report_type == 'near_expired_list':
                            stock_data   = [m for m in stock_data if m['status'] == 'close-to-expire']
                            report_title = f'Near Expired Drug List as of {end_date}'
                        elif report_type == 'out_of_stock_list':
                            stock_data   = [m for m in stock_data if m['status'] == 'out-of-stock']
                            report_title = f'Out of Stock List as of {end_date}'

                    elif report_type == 'inventory':
                        if not start_date or not end_date:
                            raise ValueError('Start and end dates are required for this report type.')
                        med_filter   = {'name': {'$regex': search or '', '$options': 'i'}} if search else {}
                        meds         = list(medications.find(med_filter, {'_id': 0, 'name': 1, 'balance': 1}).sort('name', 1).limit(300))
                        days_in_period = max(1, (end_dt.date() - start_dt.date()).days + 1)
                        for med in meds:
                            med_name = med['name']
                            try:
                                pp = [
                                    {'$match': {'med_name': med_name, 'timestamp': {'$gte': start_dt, '$lte': end_dt}}},
                                    {'$group': {'_id': None,
                                                'dispensed': {'$sum': {'$cond': [{'$eq': ['$type','dispense']}, '$quantity', 0]}},
                                                'received':  {'$sum': {'$cond': [{'$eq': ['$type','receive']},  '$quantity', 0]}}}}
                                ]
                                pr = list(transactions.aggregate(pp))
                                dispensed = pr[0].get('dispensed', 0) if pr else 0
                                received  = pr[0].get('received',  0) if pr else 0
                                current_balance   = med.get('balance', 0)
                                beginning_balance = max(0, current_balance - received + dispensed)
                                avg_daily         = dispensed / days_in_period
                                avg_monthly       = avg_daily * 30
                                lead_time_stock   = avg_daily * 60
                                amount_to_order   = max(0, avg_monthly - current_balance + lead_time_stock)
                                report_data.append({
                                    'med_name': med_name,
                                    'beginning_balance': beginning_balance,
                                    'dispensed': dispensed, 'received': received,
                                    'current_balance': current_balance,
                                    'amount_to_order': int(amount_to_order) if isinstance(amount_to_order, float) and amount_to_order.is_integer() else round(amount_to_order, 2)
                                })
                            except Exception as qe:
                                app.logger.error(f"Query failed for med {med_name}: {qe}")
                                cb = med.get('balance', 0)
                                report_data.append({'med_name': med_name, 'beginning_balance': cb,
                                                    'dispensed': 0, 'received': 0,
                                                    'current_balance': cb, 'amount_to_order': 0})

                    elif report_type == 'receive_list':
                        base_query = {'type': 'receive'}
                        if start_date and end_date:
                            base_query['timestamp'] = {'$gte': start_dt, '$lte': end_dt}
                        if search:
                            base_query['$or'] = [
                                {'med_name':       {'$regex': search, '$options': 'i'}},
                                {'batch':          {'$regex': search, '$options': 'i'}},
                                {'supplier':       {'$regex': search, '$options': 'i'}},
                                {'stock_receiver': {'$regex': search, '$options': 'i'}},
                                {'order_number':   {'$regex': search, '$options': 'i'}},
                                {'invoice_number': {'$regex': search, '$options': 'i'}},
                                {'expiry_date':    {'$regex': search, '$options': 'i'}},
                            ]
                        receive_list = list(transactions.find(base_query).sort('timestamp', 1).limit(10000))

                    elif report_type == 'controlled_drug_register':
                        if not start_date or not end_date:
                            raise ValueError('Start and end dates are required for this report type.')
                        controlled_meds = [m['name'] for m in medications.find({'schedule': 'controlled'}, {'_id': 0, 'name': 1})]
                        if controlled_meds:
                            all_tx = list(transactions.find({
                                'med_name': {'$in': controlled_meds},
                                'type': {'$in': ['receive', 'dispense']},
                                'timestamp': {'$gte': start_dt, '$lte': end_dt}
                            }).sort('timestamp', 1).limit(10000))

                            tx_by_med = defaultdict(list)
                            for tx in all_tx:
                                tx_by_med[tx['med_name']].append(tx)

                            for med_name in sorted(controlled_meds):
                                med = medications.find_one({'name': med_name})
                                if not med:
                                    continue
                                try:
                                    current_balance    = med.get('balance', 0)
                                    med_txs            = tx_by_med[med_name]
                                    received_in_period = sum(t['quantity'] for t in med_txs if t['type'] == 'receive')
                                    dispensed_in_period= sum(t['quantity'] for t in med_txs if t['type'] == 'dispense')
                                    beginning_balance  = max(0, current_balance - received_in_period + dispensed_in_period)
                                    running_bal        = beginning_balance
                                    running_entries    = []
                                    for tx in med_txs:
                                        running_bal += tx['quantity'] if tx['type'] == 'receive' else -tx['quantity']
                                        tx_copy = tx.copy()
                                        tx_copy['balance_after'] = running_bal
                                        running_entries.append(tx_copy)
                                    controlled_register.append({
                                        'med_name': med_name,
                                        'beginning_balance': beginning_balance,
                                        'ending_balance': current_balance,
                                        'received': received_in_period,
                                        'dispensed': dispensed_in_period,
                                        'transactions': [e for e in running_entries if matches_search(e, search)]
                                    })
                                except Exception as qe:
                                    app.logger.error(f"Query failed for controlled med {med_name}: {qe}")
                                    continue

                except ValueError as e:
                    message      = f'Invalid input: {str(e)}'
                    report_type  = None
                    start_date   = end_date = search = None
                    report_data  = receive_list = stock_data = controlled_register = []
                    report_title = None
            else:
                message = 'Please select a report type.'

        return render_template_string(
            REPORTS_TEMPLATE,
            report_type=report_type, report_data=report_data,
            receive_list=receive_list, stock_data=stock_data,
            controlled_register=controlled_register,
            start_date=start_date, end_date=end_date,
            total_transactions=total_transactions,
            nav_links=get_nav_links(), message=message,
            search=search, report_title=report_title, is_admin=is_admin
        )
    except ServerSelectionTimeoutError:
        return render_template_string(
            REPORTS_TEMPLATE, nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            report_type=None, report_data=[], receive_list=[], stock_data=[],
            controlled_register=[], start_date=None, end_date=None,
            total_transactions=0, search=None, report_title=None, is_admin=is_admin
        ), 500


# NEW: admin-only audit trail. Gives admins visibility into every edit/delete
# (from audit_logger.py's audit_log collection) and every unhandled exception
# (from error_logger.py's error_logs collection) across the whole app.
@app.route('/audit', methods=['GET'])
@login_required
def audit_log():
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can view the audit log.', 'error')
        return redirect('/reports')

    view = request.args.get('view', 'changes')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    search = request.args.get('search')
    # FIX (Bug): flash() is used throughout this app but get_flashed_messages()
    # was never called anywhere, so every flash() message silently vanished.
    # Pull any flashed messages into the same `message` variable AUDIT_TEMPLATE
    # already renders.
    flashed = get_flashed_messages()
    message = flashed[0] if flashed else None
    entries = []

    try:
        client = get_mongo_client()
        db = client['pharmacy_db']

        date_query = {}
        if start_date:
            date_query['$gte'] = datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
            date_query['$lte'] = end_dt

        if view == 'errors':
            query = {}
            if date_query:
                query['timestamp'] = date_query
            if search:
                query['$or'] = [
                    {'path':     {'$regex': search, '$options': 'i'}},
                    {'endpoint': {'$regex': search, '$options': 'i'}},
                    {'method':   {'$regex': search, '$options': 'i'}},
                ]
            entries = list(
                db['error_logs'].find(query).sort('timestamp', -1).limit(500)
            )
        elif view == 'warnings':
            query = {}
            if date_query:
                query['timestamp'] = date_query
            if search:
                query['$or'] = [
                    {'warning_type': {'$regex': search, '$options': 'i'}},
                    {'message':      {'$regex': search, '$options': 'i'}},
                    {'user':         {'$regex': search, '$options': 'i'}},
                    {'path':         {'$regex': search, '$options': 'i'}},
                ]
            entries = list(
                db['app_warnings'].find(query).sort('timestamp', -1).limit(500)
            )
        else:
            view = 'changes'  # normalize any unexpected value
            query = {}
            if date_query:
                query['timestamp'] = date_query
            if search:
                query['$or'] = [
                    {'action':      {'$regex': search, '$options': 'i'}},
                    {'target_type': {'$regex': search, '$options': 'i'}},
                    {'target_id':   {'$regex': search, '$options': 'i'}},
                    {'user':        {'$regex': search, '$options': 'i'}},
                ]
            entries = list(
                db['audit_log'].find(query).sort('timestamp', -1).limit(500)
            )

        return render_template_string(
            AUDIT_TEMPLATE,
            nav_links=get_nav_links(),
            message=message,
            view=view,
            entries=entries,
            start_date=start_date,
            end_date=end_date,
            search=search
        )
    except ServerSelectionTimeoutError:
        return render_template_string(
            AUDIT_TEMPLATE,
            nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            view=view, entries=[], start_date=start_date, end_date=end_date, search=search
        ), 500


@app.route('/delete-dispense', methods=['POST'])
@login_required
def delete_dispense():
    if session['user'].get('role') != 'admin':
        flash('Only admins can delete dispense transactions.', 'error')
        return redirect(url_for('dispense'))

    tx_id = request.form.get('transaction_id')
    if not tx_id:
        flash('No transaction selected.', 'error')
        return redirect(url_for('dispense'))

    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        transactions = db['transactions']
        medications  = db['medications']

        tx_rows = list(transactions.find({'transaction_id': tx_id, 'type': 'dispense'}))
        if not tx_rows:
            flash('Transaction not found.', 'error')
            write_app_warning(
                'transaction_not_found',
                f'Dispense transaction "{tx_id}" not found when attempting to delete.',
                {'transaction_id': tx_id}
            )
            return redirect(url_for('dispense'))

        for row in tx_rows:
            medications.update_one({'name': row['med_name']}, {'$inc': {'balance': row['quantity']}})
        transactions.delete_many({'transaction_id': tx_id})
        flash('Dispense transaction deleted – stock restored.', 'success')
    except Exception as e:
        flash(f'Delete failed: {str(e)}', 'error')

    return redirect(url_for('dispense',
                            start_date=request.form.get('start_date'),
                            end_date=request.form.get('end_date'),
                            search=request.form.get('search')))


@app.route('/edit-receive/<receive_id>', methods=['GET', 'POST'])
@login_required
def edit_receive(receive_id):
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        transactions = db['transactions']
        medications  = db['medications']

        start_date = request.values.get('start_date')
        end_date   = request.values.get('end_date')
        search     = request.values.get('search')
        current_user = session['user']['name']

        base_query = {'type': 'receive'}
        date_query = {}
        if start_date:
            date_query['$gte'] = datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
            date_query['$lte'] = end_dt
        if date_query:
            base_query['timestamp'] = date_query
        if search:
            base_query['$or'] = [
                {'med_name':       {'$regex': search, '$options': 'i'}},
                {'batch':          {'$regex': search, '$options': 'i'}},
                {'supplier':       {'$regex': search, '$options': 'i'}},
                {'stock_receiver': {'$regex': search, '$options': 'i'}},
                {'order_number':   {'$regex': search, '$options': 'i'}},
                {'invoice_number': {'$regex': search, '$options': 'i'}},
                {'expiry_date':    {'$regex': search, '$options': 'i'}},
            ]
        tx_list = list(transactions.find(base_query).sort('timestamp', -1))

        rx_data = None
        message = None
        try:
            oid = ObjectId(receive_id)
            rx  = transactions.find_one({'_id': oid, 'type': 'receive'})
            if rx:
                rx_data = rx
                rx_data['receive_id'] = str(rx['_id'])
            else:
                return redirect(url_for('receive', start_date=start_date or '',
                                        end_date=end_date or '', search=search or ''))
        except (InvalidId, TypeError):
            return redirect(url_for('receive', start_date=start_date or '',
                                    end_date=end_date or '', search=search or ''))

        if request.method == 'POST' and rx_data:
            try:
                oid    = ObjectId(receive_id)
                old_rx = transactions.find_one({'_id': oid})
                if not old_rx or old_rx['type'] != 'receive':
                    message = "Transaction not found."
                    write_app_warning(
                        'transaction_not_found',
                        f'Receive transaction "{receive_id}" not found when attempting to edit.',
                        {'receive_id': receive_id}
                    )
                else:
                    medications.update_one({'name': old_rx['med_name']},
                                           {'$inc': {'balance': -old_rx['quantity']}})
                    med_name       = request.form['med_name']
                    quantity       = int(request.form['quantity'])
                    batch          = request.form['batch']
                    price          = float(request.form['price'])
                    expiry_date    = request.form['expiry_date']
                    schedule       = request.form['schedule']
                    stock_receiver = request.form['stock_receiver']
                    order_number   = request.form['order_number']
                    supplier       = request.form['supplier']
                    invoice_number = request.form['invoice_number']

                    medications.update_one(
                        {'name': med_name},
                        {'$inc': {'balance': quantity},
                         '$set': {'batch': batch, 'price': price, 'expiry_date': expiry_date,
                                  'schedule': schedule, 'stock_receiver': stock_receiver,
                                  'order_number': order_number, 'supplier': supplier,
                                  'invoice_number': invoice_number}},
                        upsert=True
                    )
                    transactions.update_one(
                        {'_id': oid},
                        {'$set': {'med_name': med_name, 'quantity': quantity, 'batch': batch,
                                  'price': price, 'expiry_date': expiry_date, 'schedule': schedule,
                                  'stock_receiver': stock_receiver, 'order_number': order_number,
                                  'supplier': supplier, 'invoice_number': invoice_number,
                                  'user': current_user, 'timestamp': datetime.utcnow()}}
                    )
                    return redirect(url_for('receive', start_date=start_date or '',
                                            end_date=end_date or '', search=search or ''))
            except Exception as e:
                message = f"Update failed: {str(e)}"

        return render_template_string(
            RECEIVE_TEMPLATE,
            tx_list=tx_list, nav_links=get_nav_links(),
            message=message, start_date=start_date, end_date=end_date,
            search=search, rx_data=rx_data
        )
    except ServerSelectionTimeoutError:
        return "Database connection failed.", 500


@app.route('/delete-receive', methods=['POST'])
@login_required
def delete_receive():
    if session['user'].get('role') != 'admin':
        flash('Only admins can delete receive transactions.', 'error')
        return redirect(url_for('receive'))

    receive_id = request.form.get('receive_id')
    if not receive_id:
        flash('No transaction selected.', 'error')
        return redirect(url_for('receive'))

    try:
        oid = ObjectId(receive_id)
    except InvalidId:
        flash('Invalid receive ID.', 'error')
        return redirect(url_for('receive'))

    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        transactions = db['transactions']
        medications  = db['medications']

        rx = transactions.find_one({'_id': oid, 'type': 'receive'})
        if not rx:
            flash('Transaction not found.', 'error')
            write_app_warning(
                'transaction_not_found',
                f'Receive transaction "{receive_id}" not found when attempting to delete.',
                {'receive_id': receive_id}
            )
            return redirect(url_for('receive'))

        medications.update_one({'name': rx['med_name']}, {'$inc': {'balance': -rx['quantity']}})
        transactions.delete_one({'_id': oid})
        flash('Receive transaction deleted – stock reduced.', 'success')
    except Exception as e:
        flash(f'Delete failed: {str(e)}', 'error')

    return redirect(url_for('receive',
                            start_date=request.form.get('start_date'),
                            end_date=request.form.get('end_date'),
                            search=request.form.get('search')))


# -----------------------------------------------------------------------
# API endpoints
# -----------------------------------------------------------------------

@app.route('/api/diagnoses', methods=['GET'])
@login_required
def get_diagnosis_suggestions():
    query = request.args.get('query', '').lower()
    matching = [d for d in DIAGNOSES_OPTIONS if query in d.lower()][:10]
    return jsonify(matching)


# FIX (Performance): new endpoint that replaces the three copies of the
# medication list that were previously inlined in HTML templates.
MEDICATION_OPTIONS = [
    "Acetylsalisylic Acid, 100 mg", "Acetylsalisylic Acid, 300 mg",
    "Activated Charcoal, 050 g", "Actrapid, 100 IU",
    "Acyclovir Cre 5 Perc, 010 mg", "Acyclovir Tab, 200 mg", "Acyclovir, 800 mg",
    "Adalat, 030 mg", "Adalat, 060 mg", "Adcodol, 500 mg", "Adcorectic, 050 mg",
    "Adenosine, 006 mg", "Adrenalin Hcl Inj, 001 mg",
    "Alcophyllin Syrup", "Alcophyllex Syrup",
    "Allopurinol, 100 mg", "Aminophyllin Injection, 250 mg", "Aminophyllin, 100 mg",
    "Amiodarone, 006 mg", "Amitryptyline, 025 mg", "Amlodipine, 010 mg",
    "Amoxycillin Cap, 250 mg", "Amoxyclav Injection, 1200 mg", "Amoxyclav, 625 mg",
    "Ampicillin Caps, 250 mg", "Ampiclox Caps, 500 mg", "Ampjicillin Injection, 500 mg",
    "Anti Haemorrhoidal Suppositories, 100 mg", "Anti Snake Bite Serum, 010 ml",
    "Antirubbies, 2.5 IU", "Anusol Ointment, 2500 mg", "Arachis Oil, 020 ml",
    "Atorvastatin, 010 mg", "Atorvastatin, 020 mg",
    "Asccorbic Acid Tab - Chewable, 250 mg",
    "Atenolol, 050 mg", "Atenolol, 100 mg", "Atropine Injection, 0.5 mg",
    "Azithromycin, 500 mg", "Baclofen, 010 mg", "Beclomethasone Inhaler, 200 MID",
    "Benzathine Pen, 2.4 MU", "Benzoic Salicylic Ointment (Whitfield), 500 g",
    "Benzyl Benzoate, 100 ml", "Benzyl Pen Injection, 005 MU",
    "Betamethasone Cream, 500 g", "Bisacodyl Tab, 005 mg",
    "Calamine Lotion, 100 ml", "Calcium Gluconate Tabs, 300 mg",
    "Captopril Tab, 050 mg", "Carbamazepine, 200 mg", "Carvedilol, 12.5mg",
    "Cefotaxime Injection, 001 g", "Ceftriaxone Injection, 1000 mg",
    "Ceftriaxone, 250 mg", "Celebrex, 200 mg", "Cetrizine, 010 mg",
    "Chlopromazine, 025 mg", "Chloramphenicol Caps, 250 mg",
    "Chloramphenicol Eye Drops, 010 ml", "Chloramphenicol Eye Oint, 005 g",
    "Chlorhexide Mouth Wash, 100 ml", "Chloro Ear Drops, 020 ml",
    "Chlorpheniramine Tabs, 004 mg", "Cimetidine Tabs, 200 mg",
    "Cimetidine tabs, 400 mg", "Cimetidine Injection, 200 mg",
    "Cipro Eye Drops, 010 ml", "Ciprofloxacin, 500 mg", "Clarythromycin, 500 mg",
    "Cloxacillin Injection, 250 mg", "Clopidogrel, 075 mg",
    "Clotrimazole Cre Vaginal, 010 mg", "Clotrimazole Pess, 100 mg",
    "Clotrimazole Topical Cre 1%, 020 g", "Cloxacillin Caps, 250 mg",
    "Colchicine Tabs, 0.5 mg", "Cotrimoxazole, 480 mg", "Cotrimoxazole, 960 mg",
    "Cyproheptadine, 004 mg", "Deep Freeze Spray, 050 ml",
    "Dexamethasone Eye Drops, 002 ml", "Dexamethasone Injection, 004 mg",
    "Dextrose Injection, 050 %", "Diazepam Injection, 010 mg", "Diazepam Tabs, 005 mg",
    "Diclofenac Injection, 075 mg", "Diclofenac Tab, 025 mg", "Diclofenac Tab, 050 mg",
    "Diclofenac Gel, 050 g", "Digoxin, 0.250 mg", "Diphenhydramine Syrup, 100 ml",
    "Dopamine Injection, 010 mg", "Doxycycline Tabs, 100 mg", "Dynexan Oral Gel, 010 mg",
    "Emergency Pill, 002 mg", "Enalapril tabs, 005 mg", "Enalapril tabs, 010 mg",
    "Enalapril tabs, 20 mg", "Ergometrine Injection, 0.5 mg /ml",
    "Erythromycin tabs, 250 mg", "Fentanyl, 100 mcg", "Ferrous Sulphate Tabs, 200 mg",
    "Fertomid, 050 mg", "Flagyl Injection, 400 mg", "Flu Stat, 200 mg",
    "Fluconazole, 200 mg", "FluoxetineCaps, 020 mg", "Fml Neo Opd, 005 ml",
    "Folic Acid Tabs, 005 mg", "Furosemide Injection, 020 mg", "Furosemide Tabs, 040 mg",
    "Gabapentine, 100 mg", "Gentamycin Injection, 040 mg", "Glibenclamide Tabs, 005 mg",
    "Gliclazide, 080 mg", "Glucose Powder, 500 g", "Glycerine Supp, 100 mg",
    "Griseofulvin Tabs, 500 mg", "Guafenesin Xl 60, 100 ml", "Gv Paint, 020 ml",
    "Haloperidol Injection, 002 mg", "Haloperidol Tabs, 1.5 mg", "Heparine, 1000 SIU",
    "Histacon Caps, 200 mg", "Hydalazine Injection, 020 mg",
    "Hydralazine Hcl Tabs, 010 mg", "Hydralazine Hcl Tabs, 050 mg",
    "Hydrochlorothiazide Tabs, 025 mg", "Hydrocortisone Cream, 500 g",
    "Hydrocortisone Injection, 100 mg", "Hyoscine Injection, 020 mg",
    "Hyoscine Tabs, 010 mg", "Ibuprofen Tab, 200 mg", "Ibuprofen Tab, 400 mg",
    "Ichthammol Ointment, 500 g", "Imipramine, 010 mg", "Indapamide Tabs, 0.5 mg",
    "Indomethacin Caps, 025 mg", "Insulin Hm Injection, 100 U 10 Ml",
    "Isosorbide Trinitrate, 005 mg", "Ketamine Injection, 050 mg (Ml)",
    "Keteconazole, 200 mg Tabs", "Lactulose, 150 ml", "Lignocaine Injection, 002 %",
    "Lignocaine Spray, 050 ml", "Liquid Paraffin, 100 ml", "Lisinopril, 020, mg",
    "Loperamide Tabs, 002 mg", "Loratadine, 010 mg", "Losartan, 050 mg",
    "Losartan, 100 mg", "Lubrucating Gel, 050 g", "Magasil Suspension, 100 ml",
    "Magnesium Suphate injection, 010 mg", "Mannitol, 020 %",
    "Mayogel Suspension",
    "Mebendazole, 100 mg Tabs", "Medigel Suspension, 100 ml", "Mefenamic Acid, 250 mg",
    "Mepyramine Cream, 025 g", "Mercurochrome Paint, 020 ml",
    "Metformin Tabs, 500 mg", "Metformin Tabs, 850 mg", "Methotrexate, 005 mg",
    "Methylprednisone Injection, 040 mg", "Methylsal Ointment, 500 mg",
    "Metoclopramide Injection, 010 mg", "Metoclopramide Tabs, 010 mg",
    "Metronidazole tabs, 400 mg", "Miconazol Oral Gel, 030 g", "Miconazole Cream, 002 %",
    "Midazolam, 010 mg", "Migril, 002 mg", "Mist Alba Susp, 100 ml", "Mmt, 250 mg",
    "Morphine Injection, 010 mg", "Multivitamin Tabs, 0.25 mg", "Mybulen, 200 mg",
    "Naloxone, 0.4 mg", "Nasal Drops- Oxymetazoline, 005 ml", "Neurobion Tabs, 200 mg",
    "Nifedipine, 005 mg", "Nifedipine, 010 mg", "Nitrofurantoin, 100 mg",
    "Nitrofurazone Ointment, 500 g", "Nitrolingual Spray, 020 ml",
    "Methylcellulose Eye Drops, 010 ml", "Norflex Co Tabs, 375 mg",
    "Nystatin Ointment, 020 g", "Nystatin Oral Susp, 1000 u",
    "Nystatin Vaginal Pess, 100 mg", "Omeprazole Tabs, 020 mg",
    "Oral Rehydration Salts, 002 g", "Osteoeze Gold, 200 mg",
    "Oxytocin Injection, 010 mg", "Pain Relief Gel, 020 g", "PanaCod Tab, 500 mg",
    "Paracetamol tabs, 500 mg", "Pen Vk Tab, 250 mg", "Pentaprazole Injection, 040 mg",
    "Perfulgan, 001 g", "Pethedine Injection, 050 mg", "Pethedine Injection, 100 mg",
    "Phenytoin Injection, 200 mg", "Phernobabitol tabs, 020 mg",
    "Podophylline Paint, 020 ml", "Potassium Chloride tabs, 600 mg",
    "Potassium Citrate, 100 ml", "Povidone Ointment, 500 mg",
    "Pravastatin tabs, 020 mg", "Prednisone Tab, 005 mg", "Probanthine Tabs, 015 mg",
    "Prochlorperazine Tabs, 005 mg", "Projchlorperazine Injection, 005 mg",
    "Promethazine Injection, 050 mg", "Promethazine Tabs, 025 mg",
    "Propranolol Tabs, 010 mg", "Propranolol Tabs, 040 mg", "Pyridoxine, 025 mg",
    "Ranitidine, 150 mg", "Rocuronium injection, 010 mg",
    "Salbutamol Inhaler, 200 MID", "Salbutamol, 004 mg Tablets",
    "Selenium Tab, 100 mg", "Sildenafil, 050 mg", "Simvastatin, 020 mg",
    "Sinucon Tab, 200 mg", "Sodium Bicarbonate, 050 ml", "Sodium Valproate, 200 mg",
    "Spersallerg Opd, 010 ml", "Spironolactone tabs, 025 mg",
    "Suppositories Indocid (Arthrexin), 100 mg", "Suxamethonium injection, 010 mg",
    "Tetanus Toxoid Vaccine, 010 mg", "Tetracycline Ointment, 003 % 25G",
    "Tetracycline Opthal Ointment, 020 g", "Throat Lozenges, 250 mg",
    "Thymol Glycerine, 100 ml", "Tranexamic Acid Injection, 500 mg",
    "Tramadol Injection, 100 mg", "Tramadol Tabs, 050 mg",
    "Tranexamic Acid tabs, 500 mg", "Trifen Adult, 100 ml", "Tumsulosin, 0.5 mg",
    "Urirex K, 050 mg", "Venteze Resp.Sol, 005 mg 20ml",
    "Vitamin B Co Tablets, 001 mg", "Vitamin B12, 002 mg", "Vitamin E Cream, 500 g",
    "Vitamin B Co Injection, 001 mg", "Vitamin K Injection (Konakion), 001 mg",
    "Warfarin Tabs, 005 mg", "Water For Injection, 010 ml",
    "Zinc Oxide Ointment, 030 mg", "Zinc Tablets, 020 mg", "Zuvamor, 040 mg",
    "Amoxyl, 500 mg", "Labetolol, 5mg", "Morpine tabs, 10mg",
]

@app.route('/api/medications', methods=['GET'])
@login_required
def get_medication_options():
    """Return the static medication autocomplete list as JSON.
    Templates fetch this once and cache it in JS module scope."""
    return jsonify(MEDICATION_OPTIONS)


# FIX (Bug, regression): init_audit() must run at module level, not inside
# `if __name__ == '__main__':`. Both gunicorn (Render) and Vercel's Python
# runtime import this module and use the `app` object directly — neither
# executes app.py as a script, so anything gated behind that guard never
# runs in production. Without this, the audit decorators never get attached
# to dispense/receive/medication routes and nothing is ever written to
# audit_log, even though everything looks fine locally with `python app.py`.
try:
    from audit_logger import init_audit
    init_audit(app)
except Exception as e:
    app.logger.error(f"Failed to load audit logger: {e}")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
