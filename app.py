# Every annotation in this file is a string at runtime because of this line,
# so none of them is ever evaluated when the app starts. That matters for a
# deployment: a name that only exists for the type checker cannot bring the
# pharmacy down, and the TYPE_CHECKING imports below cost nothing at import.
from __future__ import annotations

import os
import re
import csv
import hmac
import io
import json
import secrets
import zipfile
import requests
from flask import Flask, request, render_template, jsonify, redirect, url_for, session, flash, get_flashed_messages, make_response
from functools import wraps
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ServerSelectionTimeoutError, DuplicateKeyError
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence, cast
from uuid import uuid4

if TYPE_CHECKING:                       # imported for the checker, never at run time
    from flask import Response
    from flask.typing import ResponseReturnValue
    from pymongo.collection import Collection
    from pymongo.database import Database

# What comes out of Mongo, and what goes into it as a filter. Naming them
# says the useful part — that these are documents and queries, not any old
# dictionary — without pretending the shapes are fixed, which they are not:
# a transaction written in 2024 has fewer fields than one written today, and
# the code has to keep reading both.
Doc = dict[str, Any]
Docs = list[Doc]
Query = dict[str, Any]
from collections import defaultdict
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import escape  # FIX: used to prevent XSS in nav links

load_dotenv()

from error_logger import init_error_logging, write_app_warning
from bson import ObjectId
from bson.errors import InvalidId

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES = os.path.join(_HERE, 'templates')
app = Flask(__name__, template_folder=_TEMPLATES)

# The pages are files in templates/ now, so that folder has to be deployed
# alongside this one. Said plainly at startup, because the alternative is
# every page answering 500 with a Jinja TemplateNotFound in the logs, which
# is a much longer way round to the same sentence.
#
# The path is pinned to this file's own directory rather than left to Flask's
# default, which is relative to the working directory: a serverless handler
# does not necessarily run from the directory its code is in.
if not os.path.isdir(_TEMPLATES):
    raise RuntimeError(
        f"The templates folder is missing: {_TEMPLATES}\n"
        "Every page of this app is a file in it. Deploy the templates/ "
        "directory next to app.py — on Vercel, make sure it is committed "
        "and, if the project uses includeFiles, that templates/** is listed."
    )

init_error_logging(app)
app.secret_key = os.getenv('SECRET_KEY')

# FIX: fail loudly at startup if SECRET_KEY is not set, rather than silently
# using a weak dev key in production.
if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. "
        "Set it to a long random string before starting the server."
    )

# ---------------------------------------------------------------------
# The session cookie itself.
#
# HttpOnly keeps it out of reach of any script on the page. SameSite=Lax
# means a form on somebody else's site cannot post to this one carrying the
# pharmacist's session — which is the whole shape of a cross-site request
# forgery. Secure means it is never sent over plain HTTP.
#
# Secure is switched on by the VERCEL variable, which Vercel sets for you,
# so the deployed app is protected and a local copy served over http still
# works. COOKIE_SECURE=1 forces it on anywhere.
# ---------------------------------------------------------------------
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.environ.get('VERCEL')
                               or os.environ.get('COOKIE_SECURE') == '1'),
)

# ---------------------------------------------------------------------
# Cross-site request forgery.
#
# Every one of the 23 POST routes in this file changed data on the word of
# a session cookie alone. A page on another site could therefore put up a
# form aimed at this one — write off stock, delete a visit, add a user —
# and a pharmacist who happened to be logged in would submit it by clicking
# anything on that page. Nothing in the audit log would look unusual,
# because as far as this app was concerned the pharmacist did it.
#
# The fix is the standard one: a random token, kept in the session, which
# every state-changing request must carry back. An attacker's page can make
# the browser send the cookie, but cannot read the token to send with it.
#
# The token is put into every POST form automatically, after the page is
# rendered (see _add_csrf_field). That is deliberate: there are 43 forms
# across 17 templates, and a scheme that depends on remembering to add a
# line to each one is a scheme that protects 42 forms.
# ---------------------------------------------------------------------
CSRF_FIELD_NAME = '_csrf_token'
CSRF_SESSION_KEY = '_csrf_token'
CSRF_HEADER = 'X-CSRF-Token'
CSRF_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
_POST_FORM_TAG = re.compile(
    rb'(<form\b[^>]*\bmethod\s*=\s*["\']post["\'][^>]*>)', re.I)


def csrf_token() -> str:
    """The token for this session, made on first use."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


@app.before_request
def _require_csrf_token() -> tuple[str, int] | None:
    """Refuse a state-changing request that does not carry the token.

    Checked only when the session actually holds a token. A session that has
    expired holds nothing, and those requests are better answered by the
    login redirect than by a bare rejection: the pharmacist gets the login
    page and the browser draft puts their half-finished visit back. A fresh
    visitor posting to /login is in the same position.
    """
    if request.method not in CSRF_METHODS:
        return None
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        return None
    sent = (request.form.get(CSRF_FIELD_NAME)
            or request.headers.get(CSRF_HEADER) or '')
    if hmac.compare_digest(str(sent), str(expected)):
        return None
    app.logger.warning(
        f'CSRF token missing or wrong on {request.method} {request.path} '
        f'from {request.remote_addr}')
    return render_template('csrf_failed.html'), 400


app.jinja_env.globals['csrf_token'] = csrf_token


@app.after_request
def _add_csrf_field(response: Response) -> Response:
    """Put the token into every POST form on the way out.

    Rendered HTML rather than the templates, so no form can be forgotten and
    a form added later is covered the day it is written.
    """
    try:
        if response.direct_passthrough:
            return response
        if not (response.content_type or '').startswith('text/html'):
            return response
        body = response.get_data()
        if b'<form' not in body:
            return response
        field = ('<input type="hidden" name="%s" value="%s">'
                 % (CSRF_FIELD_NAME, csrf_token())).encode()
        patched = _POST_FORM_TAG.sub(lambda m: m.group(1) + field, body)
        if patched != body:
            response.set_data(patched)
    except Exception as e:
        # A page that renders without the field is caught loudly by the
        # check above; a page that does not render at all helps nobody.
        app.logger.warning(f'CSRF field not added: {e}')
    return response


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

# -----------------------------------------------------------------------
# Local time at the pharmacy.
#
# The server runs in UTC. Lesotho keeps South African Standard Time, UTC+2
# all year with no daylight saving, so a fixed offset is exact rather than an
# approximation and needs no timezone database.
#
# This matters for anything that reads a wall clock. A check done at 13:30
# local is 11:30 UTC, so deciding "morning or afternoon" from UTC would file
# every early-afternoon reading as a morning one — silently, and on a
# compliance record.
# -----------------------------------------------------------------------
PHARMACY_UTC_OFFSET_HOURS = float(os.environ.get('PHARMACY_UTC_OFFSET', '2'))
PHARMACY_TZ = timezone(timedelta(hours=PHARMACY_UTC_OFFSET_HOURS))


def local_now() -> datetime:
    """Wall-clock time at the pharmacy, naive, to match stored datetimes."""
    return datetime.now(timezone.utc).astimezone(PHARMACY_TZ).replace(tzinfo=None)


def local_session() -> str:
    """Which round a check done right now belongs to."""
    return 'Morning' if local_now().hour < TEMP_NOON_HOUR else 'Afternoon'

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
_mongo_client: MongoClient | None = None

def get_mongo_client() -> MongoClient:
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
def init_db_collections() -> None:
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
        # NEW (Stock Take): a stock take is a long-running session that stays
        # open across days while the admin counts items whenever convenient.
        # stock_takes holds the session header; stock_take_counts holds one
        # document per counted line.
        if 'stock_takes' not in existing:
            db.create_collection('stock_takes')
        if 'stock_take_counts' not in existing:
            db.create_collection('stock_take_counts')
        # NEW (Deliveries): header document carrying supplier, order number +
        # order total, invoice number + invoice total for a consignment. Its
        # line items are ordinary 'receive' transactions tagged with delivery_id.
        if 'deliveries' not in existing:
            db.create_collection('deliveries')
        # NEW (Temperature log): one document per location per date per session
        # (morning/afternoon), holding the max-min thermometer's three figures.
        if 'temperature_logs' not in existing:
            db.create_collection('temperature_logs')
        # NEW (Equipment loans): crutches, moon boots and arm slings are lent
        # out and come back, so they are NOT stock. A consumable leaves and is
        # gone; a loaned item is still owned by the pharmacy while it is off
        # the shelf. Holding them in `medications` would give them a balance
        # that drops on issue, an AMC, a reorder level and an expiry date, all
        # of which are meaningless here and would corrupt the stock reports.
        # loan_items is the asset register, one document per physical item;
        # loans is the lending ledger, one document per issue.
        if 'loan_items' not in existing:
            db.create_collection('loan_items')
        if 'loans' not in existing:
            db.create_collection('loans')
        # NEW (Store key register, LD-HSE/NMC/PSKR/6.1.3.3): one document per
        # issue of the pharmacy store key, carrying both sides of the paper
        # form — who took it and when, who brought it back and who received it.
        if 'key_register' not in existing:
            db.create_collection('key_register')
        # NEW: who may hold the key. A Nurse Assistant may well be the person
        # at the counter entering the record, but is not an officer in charge,
        # so the two things are kept apart by an explicit flag rather than by
        # anyone remembering.
        if 'key_officers' not in existing:
            db.create_collection('key_officers')
        # NEW: one small document recording that the key is up for handover,
        # set when the pharmacist logs out and cleared when somebody takes it.
        if 'key_control' not in existing:
            db.create_collection('key_control')

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

        # NEW (Stock Take): the stock-take page lists open sessions first and
        # sorts by start time; the audit view sorts counts by timestamp desc
        # and filters by med_name/user/discrepancy_type.
        db['stock_takes'].create_index([('status', 1), ('started_at', -1)])
        db['stock_takes'].create_index([('reference', 1)], unique=True)
        db['stock_take_counts'].create_index([('timestamp', -1)])
        db['stock_take_counts'].create_index([('stock_take_id', 1), ('timestamp', -1)])
        db['stock_take_counts'].create_index([('med_name', 1)])
        db['stock_take_counts'].create_index([('discrepancy_type', 1)])
        # The register is read a month at a time for one location, and a
        # reading is uniquely identified by location + date + session — which
        # the unique index enforces, so a second entry corrects the first
        # instead of silently creating a duplicate row on the chart.
        db['temperature_logs'].create_index([('location', 1), ('date', 1)])
        db['temperature_logs'].create_index(
            [('location', 1), ('date', 1), ('session', 1)], unique=True)

        # loan_items: the asset register. item_code is how staff refer to a
        # physical item ("CR-003"), so it must be unique or two crutch pairs
        # share an identity and the loan history becomes unreadable.
        db['loan_items'].create_index([('item_code', 1)], unique=True)
        db['loan_items'].create_index([('item_type', 1), ('status', 1)])

        # loans: the lending ledger. The hot query is "what is still out",
        # which is status + due_date for the overdue list, and item_code for
        # the double-issue guard and an item's own history.
        db['loans'].create_index([('status', 1), ('due_date', 1)])
        db['loans'].create_index([('item_code', 1), ('issued_date', -1)])
        db['loans'].create_index([('patient_name', 1)])
        db['loans'].create_index([('issued_date', -1)])
        db['loans'].create_index([('loan_ref', 1)], unique=True)

        # key_register: the hot query is "is the key out right now", which is
        # status alone; the register itself reads newest-first by issue time.
        db['key_register'].create_index([('status', 1)])
        db['key_register'].create_index([('issued_at', -1)])
        db['key_register'].create_index([('recipient_name', 1)])
        db['key_register'].create_index([('ref', 1)], unique=True)
        db['key_officers'].create_index([('name', 1)], unique=True)
        db['key_officers'].create_index([('active', 1), ('in_charge', 1)])
        db['deliveries'].create_index([('status', 1), ('opened_at', -1)])
        db['deliveries'].create_index([('reference', 1)], unique=True)
        db['deliveries'].create_index([('invoice_number', 1)])
        db['deliveries'].create_index([('order_number', 1)])
        db['transactions'].create_index([('delivery_id', 1)])

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
# The clinical vocabulary.
#
# Standardised 20 October 2026, in three passes.
#
# The first corrected 79 entries that carried a spelling or punctuation error
# ("Chelazion", "Rynaulds phenomenon", "Myocardiac infaction"), removed 14
# that were body-system headings rather than diagnoses ("ENT", "Respiratory",
# "Surgical"), and merged three that said the same thing twice.
#
# The second split six entries that put two DIFFERENT diseases on one line —
# worst of them "Inflammatory bowel disease / IBS", which is Crohn's and
# ulcerative colitis filed under irritable bowel — and added 23 conditions the
# pharmacy plainly treats but could not record. Those were found by reading
# the medicine cupboard against this list: Anti Snake Bite Serum with no snake
# bite, Tetanus Toxoid with no immunisation, four contraceptives with no
# family planning, Adrenalin with no anaphylaxis.
#
# The third took the naming discipline of ICD-10's tabular list, but only
# where it removes a real ambiguity. A diagnosis of "Ulcer" does not say
# whether it is gastric, oral, venous or corneal; "Injury" does not say what
# was injured. ICD will not let a title be that vague, so those 12 now carry
# the qualifier, named as "unspecified" rather than left silent. The other 16
# were joined with a slash, which ICD does not use: it says "and" for a shared
# title and gives separate entries otherwise. The abbreviations the pharmacy
# speaks in — HTN, DM, TB — are deliberately left alone: they are not
# ambiguous here, they are already findable by their full name, and spelling
# them out would only make the register wider.
#
# Records already written keep whatever was entered at the time — a clinical
# record is not rewritten after the fact — so every wording this list has ever
# carried lives on in DIAGNOSIS_SEARCH_TERMS and still finds its entry, and
# DIAGNOSIS_RENAMES below keeps the reports counting it as one condition.
DIAGNOSES_OPTIONS = [
    'Abrasion', 'Abscess, site unspecified', 'Acne', 'Acute appendicitis',
    'Acute bronchitis', 'Acute gastroenteritis (AGE)', 'Acute kidney injury',
    'Acute myocardial infarction', 'Acute nasopharyngitis [common cold]',
    'Acute otitis media', 'Acute sinusitis', 'Acute stress disorder',
    'Acute tonsillitis and pharyngitis', 'Acute urinary retention',
    'Alcohol intoxication', 'Allergic conjunctivitis', 'Allergic rhinitis',
    'Allergic skin reaction', 'Allergy, unspecified', 'Anaemia',
    'Anal fissure', 'Anaphylaxis', 'Angina', 'Angio-oedema',
    'Antiphospholipid syndrome', 'Anxiety disorder',
    'Aphthous ulcer and other oral lesions', 'Aquagenic pruritus', 'ARDS',
    'Arthralgia', 'Asthma', 'Awaiting PCR results',
    'Bacterial conjunctivitis', 'Bereavement reaction', 'Bipolar disorder',
    'Bladder stone', 'Boutonniere deformity', 'Bowel obstruction', 'BPH',
    'Brachial plexus compression', 'Breast lump', 'Bullous skin lesions',
    'Burn, site and degree unspecified', 'Bursitis', 'Calcaneal spur',
    'Candidiasis of mouth or oesophagus', 'Cardiac dysrhythmia',
    'Cardiomegaly', 'Cataract', 'CCF', 'Cellulitis', 'Cervixitis',
    'Chalazion', 'Chemical burn', 'Chemical conjunctivitis',
    'Chemical pneumonitis', 'Chronic fatigue syndrome',
    'Chronic kidney disease', 'Chronic sinusitis',
    'Chronic suppurative otitis media (CSOM)', 'Circumcision', 'Common cold',
    'Constipation', 'Contusion', 'COPD', 'Corneal abrasion',
    'Corns and callosities', 'Costochondritis', 'COVID-19', 'Crush syndrome',
    'CVA', 'Dehydration', 'Dental abscess', 'Dental caries',
    'Depressive disorder', 'Dermatitis and eczema', 'Dermatophytosis [tinea]',
    'Diarrhoea', 'Disc herniation', 'Dislocation, site unspecified', 'DKA',
    'DM', 'Dog bite', 'Drug-induced kidney injury', 'Dry eyes', 'DVT',
    'Dysentery', 'Dysfunctional uterine bleeding', 'Dysmenorrhoea',
    'Ear wax impaction', 'Emergency contraception', 'Emphysema',
    'Epidermoid cyst', 'Epididymo-orchitis', 'Epilepsy', 'Epistaxis',
    'Erectile dysfunction', 'Eyelid infection', 'Faecal incontinence',
    'Family planning', 'Fibroid uterus', 'Fitness to work examination',
    'Foreign body (in soft tissue)', 'Foreign body ear',
    'Foreign body, site unspecified', 'Fracture, site unspecified',
    'Ganglion cyst', 'Gastritis', 'Gastro-oesophageal reflux disease [GERD]',
    'Gingivitis', "Golfer's elbow", 'Gout', 'Haemorrhoids',
    'Hairy leucoplakia', 'Hallux valgus deformity', 'Head injury',
    'Headache (tension, migraine, cluster etc)', 'Heat exhaustion',
    'Heat stroke', 'Hepatitis, unspecified', 'Herpes labialis (cold sore)',
    'Herpes zoster', 'HHS', 'Hiccups', 'High altitude syndrome', 'HIV',
    'HIV-associated vasculitis', 'Hormonal imbalance', 'HTN', 'Hydrocele',
    'Hypercholesterolaemia', 'Hyperglycaemia', 'Hypertriglyceridaemia',
    'Hypoglycaemia', 'Hypotension', 'I & D', 'Iliotibial band syndrome',
    'Immunisation', 'Indigestion', 'Inflammatory bowel disease', 'Influenza',
    'Ingrown toenail', 'Inhibited sexual desire', 'Injury (RTA)',
    'Injury on duty', 'Injury, site unspecified', 'Insect bite', 'Insomnia',
    'Insufficient sleep syndrome', 'Irritable bowel syndrome',
    "Kaposi's sarcoma", 'Kidney stone', 'Laceration', 'Laryngitis',
    'Ligament sprain', 'Lightning injury', 'Lipoma', 'Loose teeth',
    'Lower GI bleed', 'LRTI', 'Lymphadenitis', 'Mechanical low back pain',
    'Medication side effects', 'Medication-induced cough (ACE-I etc)',
    'Menorrhagia', 'Miscarriage', 'Mucus hypersecretion', 'Muscle strain',
    'Myalgia and muscle spasm', 'Nasal lesion and infection', 'Nasal polyp',
    'Nausea and vomiting', 'Negative on AgRDT',
    'Neoplasm, nature unspecified', 'Neuralgia and neuritis, unspecified',
    'Noise-induced hearing loss', 'Non-specific spinal pain',
    'Normal menstrual period', 'Obstructive sleep apnoea', 'Oral lesion(s)',
    'Osteoarthritis', 'Otitis externa', 'Ovarian cyst', 'Overdose',
    'Pancreatitis', 'Panic disorder', 'Papules, pustules', 'Paraesthesia',
    'PCOS', 'Pelvic congestion syndrome', 'Pelvic mass',
    'Peri-orbital lesion', 'Perianal abscess', 'Periodontitis',
    'Peripheral neuropathy', 'Peripheral vascular disease', 'Pica', 'PID',
    'Pinguecula', 'PJP', 'Plantar fasciitis', 'Pleural effusion', 'Pleurisy',
    'Pneumoconiosis', 'Pneumonia', 'Poisoning', 'Polyarthralgia',
    'Positive on AgRDT', 'Post HIV exposure',
    'Post-COVID hyperactive airway disease', 'Post-menopausal bleeding',
    'Post-menopausal syndrome', 'Post-operative adhesions', 'Pregnancy',
    'Preseptal cellulitis', 'Psychosomatic disorder', 'Pterygium', 'PUD',
    'Rabies exposure', 'Radiculopathy', "Raynaud's phenomenon",
    'Rheumatoid arthritis', 'Scabies', 'Sciatica', 'Secondary amenorrhoea',
    'Seizure, unspecified', 'Septic nasal piercing',
    'Smoke inhalation injury', 'Snake bite', 'Spinal abnormality',
    'Spondylolisthesis', 'Spondylosis',
    'STI (MUS, VDS, herpes genitalia, syphilis etc)', 'Stye (hordeolum)',
    'Supplementation', 'Surgical site infection', 'Swan neck deformity',
    'Syncope', 'TB', 'TB meningitis', 'Temporal arteritis', 'Tendon injury',
    'Tendonitis', 'Tennis elbow', 'Tetanus prophylaxis', 'Thoracic back pain',
    'TIA', 'Tinnitus', 'Toothache', 'Torticollis', 'Traumatic conjunctivitis',
    'Typhoid', 'Ulcer, site unspecified', 'Upper GI bleed',
    'Urethral stricture and bladder outlet obstruction',
    'Urinary catheter blockage', 'Urinary incontinence', 'URTI', 'Urticaria',
    'UTI', 'Vaginal candidiasis', 'Venous insufficiency',
    'Viral conjunctivitis', 'Visual impairment, unspecified', 'Warts',
    'Welding flash (arc eye)', 'Worm infestation', 'Wound care (dressing)',
]

# What each renamed entry used to be called.
#
# Renaming a diagnosis splits its own history: every visit before today says
# "Injury" and every visit after says "Injury, site unspecified", so a count
# of either is half the truth. The records are not touched — they are clinical
# records — so the reports are told that these pairs are one condition.
DIAGNOSIS_RENAMES = {
    'Abscess':
        'Abscess, site unspecified',
    'Acute tonsillitis / pharyngitis':
        'Acute tonsillitis and pharyngitis',
    'Allergies':
        'Allergy, unspecified',
    'Aphthous ulcers / oral lesions':
        'Aphthous ulcer and other oral lesions',
    'Burns':
        'Burn, site and degree unspecified',
    'Candidiasis (oral / oesophageal)':
        'Candidiasis of mouth or oesophagus',
    'Dermatitis / eczema':
        'Dermatitis and eczema',
    'Dislocation':
        'Dislocation, site unspecified',
    'Feet corns / calluses':
        'Corns and callosities',
    'Foreign body':
        'Foreign body, site unspecified',
    'Fractures':
        'Fracture, site unspecified',
    'Fungal infection / tinea / dermatophyte':
        'Dermatophytosis [tinea]',
    'GERD / oesophageal sphincter dysfunction':
        'Gastro-oesophageal reflux disease [GERD]',
    'Grief / bereavement':
        'Bereavement reaction',
    'Hepatitis':
        'Hepatitis, unspecified',
    'Injury':
        'Injury, site unspecified',
    'Internal / external haemorrhoids':
        'Haemorrhoids',
    'Myalgia / muscle tension / spasm':
        'Myalgia and muscle spasm',
    'Myocardial infarction / ACS':
        'Acute myocardial infarction',
    'Nasal lesion / infection':
        'Nasal lesion and infection',
    'Neuralgia / neuritis':
        'Neuralgia and neuritis, unspecified',
    'Pleuritic chest pain / pleuritis':
        'Pleurisy',
    'Poor vision':
        'Visual impairment, unspecified',
    'Seizure':
        'Seizure, unspecified',
    'Tumour':
        'Neoplasm, nature unspecified',
    'Ulcer':
        'Ulcer, site unspecified',
    'Urethral stricture / urinary outlet obstruction':
        'Urethral stricture and bladder outlet obstruction',
    'Viral rhinitis / common cold':
        'Acute nasopharyngitis [common cold]',
}

# Extra words that find an entry without being part of its name: the long
# form of an abbreviation, the everyday word for it, and every spelling,
# joined-up phrasing and old title this list has ever carried. Typing
# "hypertension" found NOTHING before this, because the entry is called HTN;
# so did "diabetes", "stroke", "shingles" and "nosebleed". A standard list
# only standardises anything if it is easier to hit than typing something of
# your own.
DIAGNOSIS_SEARCH_TERMS = {
    'ARDS': [
        'acute respiratory distress syndrome',],
    'Abrasion': [
        'graze', 'scrape',],
    'Abscess, site unspecified': [
        'abscess',],
    'Acute gastroenteritis (AGE)': [
        'age', 'diarrhoea vomiting', 'stomach bug',],
    'Acute myocardial infarction': [
        'acs', 'acute coronary syndrome', 'heart attack', 'mi',
        'myocardiac infaction/ acs', 'myocardial infarction / acs',],
    'Acute nasopharyngitis [common cold]': [
        'cold', 'common cold', 'viral rhinitis',
        'viral rhinitis / common cold', 'viral rhinitis/ common cold',],
    'Acute otitis media': [
        'ear infection', 'earache',],
    'Acute tonsillitis and pharyngitis': [
        'acute tonsilitis /pharyngitis', 'acute tonsillitis / pharyngitis',
        'sore throat', 'tonsils',],
    'Acute urinary retention': [
        'acute urinary retension',],
    'Allergic conjunctivitis': [
        'pink eye', 'red eye',],
    'Allergy, unspecified': [
        'allergies',],
    'Anaemia': [
        'anemia', 'low blood',],
    'Anaphylaxis': [
        'anaphylactic', 'severe allergic reaction',],
    'Angio-oedema': [
        'angioedema', 'facial swelling', 'swelling of the lips',],
    'Antiphospholipid syndrome': [
        'antiphspholipid syndrome',],
    'Anxiety disorder': [
        'anxiety', 'anxiety disorder / panic disorder',],
    'Aphthous ulcer and other oral lesions': [
        'aphthous ulcers / oral lesions', 'aphthous ulcers/oral lessions',],
    'Awaiting PCR results': [
        'awating pcr results',],
    'BPH': [
        'benign prostatic hyperplasia', 'enlarged prostate', 'prostate',],
    'Bacterial conjunctivitis': [
        'pink eye', 'red eye',],
    'Bereavement reaction': [
        'bereavement', 'grief', 'grief / bereavement', 'grief/bereavement',],
    'Boutonniere deformity': [
        'boutoner deformity', 'boutonniere',],
    'Bullous skin lesions': [
        'bulous skin lessions',],
    'Burn, site and degree unspecified': [
        'burn', 'burns',],
    'CCF': [
        'congestive cardiac failure', 'heart failure',],
    'COPD': [
        'chronic obstructive pulmonary disease', 'emphysema bronchitis',],
    'COVID-19': [
        'covid_19',],
    'CVA': [
        'cerebrovascular accident', 'stroke', 'tia / cva',],
    'Calcaneal spur': [
        'calcaneous spur',],
    'Candidiasis of mouth or oesophagus': [
        'candida', 'candidiasis (oral / oesophageal)',
        'candidiasis(oral/esophageal)', 'oral candidiasis', 'thrush',],
    'Cardiac dysrhythmia': [
        'cardiac dysrythmia',],
    'Cardiomegaly': [
        'cardiomegally', 'enlarged heart',],
    'Chalazion': [
        'chelazion', 'eyelid lump',],
    'Chemical burn': [
        'acid burn', 'caustic',],
    'Chronic fatigue syndrome': [
        'chronic fatique syndrome',],
    'Chronic suppurative otitis media (CSOM)': [
        'chronic suppurative otits media (csom)', 'csom', 'discharging ear',],
    'Circumcision': [
        'circumcission',],
    'Contusion': [
        'bruise', 'bruising',],
    'Corneal abrasion': [
        'cornea', 'scratched eye',],
    'Corns and callosities': [
        'callus', 'calluses', 'corn', 'feet corns', 'feet corns / calluses',
        'feet corns/calluses',],
    'DKA': [
        'diabetic ketoacidosis',],
    'DM': [
        'diabetes', 'diabetes and hypertension', 'diabetes mellitus',
        'htn / dm', 'sugar',],
    'DVT': [
        'clot', 'deep vein thrombosis',],
    'Dehydration': [
        'dehydrated', 'fluid loss',],
    'Dental caries': [
        'cavity', 'dental decay', 'tooth decay',],
    'Dermatitis and eczema': [
        'dermatitis / eczema', 'dermatitis/eczema',],
    'Dermatophytosis [tinea]': [
        "athlete's foot", 'dermatophyte', 'fungal infection',
        'fungal infection / tinea / dermatophyte',
        'fungal infections/tineas/ dermatophyte', 'ringworm', 'tinea',],
    'Diarrhoea': [
        'diarrhea', 'loose stools',],
    'Disc herniation': [
        'disc hernia',],
    'Dislocation, site unspecified': [
        'dislocation',],
    'Drug-induced kidney injury': [
        'drug induced kidney injury',],
    'Dysfunctional uterine bleeding': [
        'dysfunctional uterine beeding',],
    'Dysmenorrhoea': [
        'dysmenorrhea', 'period pain',],
    'Ear wax impaction': [
        'cerumen', 'wax',],
    'Emergency contraception': [
        'emergency pill', 'morning after',],
    'Epididymo-orchitis': [
        'epididimorchitis',],
    'Epilepsy': [
        'epilepsy / seizure disorder', 'fits',],
    'Epistaxis': [
        'nose bleed', 'nosebleed',],
    'Family planning': [
        'contraception', 'contraceptive', 'depo', 'implant',],
    'Fitness to work examination': [
        'fitness to work', 'medical examination', 'periodic medical',
        'pre-employment',],
    'Foreign body, site unspecified': [
        'foreign body',],
    'Fracture, site unspecified': [
        'broken bone', 'fracture', 'fractures',],
    'Gastritis': [
        'pud / gastritis',],
    'Gastro-oesophageal reflux disease [GERD]': [
        'gastro-oesophageal reflux', 'gerd',
        'gerd / esophageal sphincter dysfunction',
        'gerd / oesophageal sphincter dysfunction', 'heartburn', 'reflux',],
    "Golfer's elbow": [
        'medial epicondylitis',],
    'HHS': [
        'hyperosmolar hyperglycaemic state',],
    'HIV': [
        'human immunodeficiency virus', 'retroviral',],
    'HIV-associated vasculitis': [
        'hiv -associated vasculitis',],
    'HTN': [
        'blood pressure', 'bp', 'high blood pressure', 'htn / dm',
        'hypertension', 'hypertension and diabetes',],
    'Haemorrhoids': [
        'external haemorrhoids', 'hemorrhoids',
        'internal / external haemorrhoids', 'internal haemorrhoids',
        'internal/external haemorrhoids', 'piles',],
    'Hallux valgus deformity': [
        'bunion', 'hallus valgus deformity',],
    'Headache (tension, migraine, cluster etc)': [
        'headaches (tension, migrane, cluster etc)',],
    'Heat exhaustion': [
        'heat', 'overheating',],
    'Heat stroke': [
        'heatstroke',],
    'Hepatitis, unspecified': [
        'hepatitis',],
    'Herpes labialis (cold sore)': [
        'cold sore', 'fever blister',],
    'Herpes zoster': [
        'shingles',],
    'Hypercholesterolaemia': [
        'cholesterol', 'high cholesterol',],
    'Hyperglycaemia': [
        'high sugar', 'hyperglycemia',],
    'Hypertriglyceridaemia': [
        'triglycerides',],
    'Hypoglycaemia': [
        'hypoglycemia', 'low sugar',],
    'I & D': [
        'incision and drainage',],
    'Iliotibial band syndrome': [
        'illio-tibial band syndrome',],
    'Immunisation': [
        'immunization', 'vaccination', 'vaccine',],
    'Inflammatory bowel disease': [
        "crohn's", 'crohns', 'ibd', 'inflammatory bowel disease / ibs',
        'ulcerative colitis',],
    'Ingrown toenail': [
        'ingrowing toenail', 'ingrown', 'onychocryptosis',],
    'Injury (RTA)': [
        'car accident', 'mva', 'road traffic accident',],
    'Injury on duty': [
        'injuries on duty', 'iod', 'work injury', 'workplace injury',],
    'Injury, site unspecified': [
        'injuries', 'injury',],
    'Irritable bowel syndrome': [
        'ibs', 'inflammatory bowel disease / ibs', 'irritable bowel',],
    "Kaposi's sarcoma": [
        'kaposi', 'kaposis sarcoma',],
    'LRTI': [
        'chest infection', 'lower respiratory tract infection',],
    'Laceration': [
        'cut', 'open wound',],
    'Ligament sprain': [
        'ligmament sprain',],
    'Lightning injury': [
        'lightening injury',],
    'Mechanical low back pain': [
        'back pain', 'backache',],
    'Medication-induced cough (ACE-I etc)': [
        'medication induced cough(ace-i etc)',],
    'Myalgia and muscle spasm': [
        'myalgia / muscle tension / spasm', 'myalgia/muscle tension/ spasm',],
    'Nasal lesion and infection': [
        'nasal lesion / infection', 'nasal lession/infection',],
    'Neoplasm, nature unspecified': [
        'growth', 'mass', 'tumor', 'tumour',],
    'Neuralgia and neuritis, unspecified': [
        'neuralgia / neuritis',],
    'Noise-induced hearing loss': [
        'deafness', 'hearing loss', 'nihl', 'noise induced',],
    'Non-specific spinal pain': [
        'non specific spinal pain',],
    'Normal menstrual period': [
        'normal mentrual period',],
    'Obstructive sleep apnoea': [
        'obstructive sleep apnea', 'sleep apnea', 'snoring',],
    'Oral lesion(s)': [
        'oral lession(s)',],
    'Osteoarthritis': [
        'arthritis', 'oa',],
    'Overdose': [
        'od', 'opioid overdose',],
    'PCOS': [
        'polycystic ovary syndrome',],
    'PID': [
        'pelvic inflammatory disease',],
    'PJP': [
        'pneumocystis pneumonia',],
    'PUD': [
        'peptic ulcer disease', 'pud / gastritis', 'stomach ulcer',],
    'Panic disorder': [
        'anxiety disorder / panic disorder', 'panic attack',],
    'Papules, pustules': [
        'papule, pastules',],
    'Paraesthesia': [
        'numbness', 'parasthesia', 'paresthesia', 'pins and needles',],
    'Pelvic congestion syndrome': [
        'pelvic congestion', 'pelvic hypertension', 'pelvic varices',],
    'Peri-orbital lesion': [
        'peri-orbital lession',],
    'Pica': [
        'pica',],
    'Plantar fasciitis': [
        'heel pain', 'planta fascitis',],
    'Pleurisy': [
        'pleuritic chest pain', 'pleuritic chest pain / pleuritis',
        'pleuritic chest pain/pleuritis', 'pleuritis',],
    'Pneumoconiosis': [
        'dust disease', 'dust exposure', 'occupational lung', 'silicosis',],
    'Poisoning': [
        'ingestion', 'swallowed',],
    'Post-COVID hyperactive airway disease': [
        'post covid hyperactive airway dx',],
    'Post-menopausal bleeding': [
        'post menauposal bleeding',],
    'Post-menopausal syndrome': [
        'post menopausal syndrome',],
    'Post-operative adhesions': [
        'post operative adhessions',],
    'Rabies exposure': [
        'animal bite', 'rabid', 'rabies',],
    "Raynaud's phenomenon": [
        'raynaud', 'raynauds', 'rynaulds phenomenon',],
    'Rheumatoid arthritis': [
        'arthritis', 'ra',],
    'STI (MUS, VDS, herpes genitalia, syphilis etc)': [
        'discharge', 'gonorrhoea', 'sexually transmitted infection', 'std',
        'syphilis',],
    'Scabies': [
        'itch mite',],
    'Secondary amenorrhoea': [
        'secondary ammenorrhoea',],
    'Seizure, unspecified': [
        'convulsion', 'epilepsy / seizure disorder', 'fit', 'seizure',],
    'Septic nasal piercing': [
        'sceptic nasal piercing',],
    'Smoke inhalation injury': [
        'smoke inhilation injury',],
    'Snake bite': [
        'snake', 'snakebite',],
    'Spondylolisthesis': [
        'spondylosis / spondylolisthesis',],
    'Spondylosis': [
        'spondylosis / spondylolisthesis',],
    'Stye (hordeolum)': [
        'eyelid', 'hordeolum', 'stye /',],
    'TB': [
        'tuberculosis',],
    'TB meningitis': [
        'tuberculous meningitis',],
    'TIA': [
        'mini stroke', 'tia / cva', 'transient ischaemic attack',],
    'Tennis elbow': [
        'lateral epicondylitis',],
    'Tetanus prophylaxis': [
        'tetanus', 'tetanus toxoid',],
    'Torticollis': [
        'torticolis',],
    'URTI': [
        'cold', 'flu', 'upper respiratory tract infection',],
    'UTI': [
        'bladder infection', 'urinary tract infection',],
    'Ulcer, site unspecified': [
        'ulcer',],
    'Urethral stricture and bladder outlet obstruction': [
        'urethral stricture / urinary outlet obstruction',
        'urethral stricture/urinary outlet obstruction',],
    'Urinary incontinence': [
        'urine incontinence',],
    'Urticaria': [
        'hives',],
    'Vaginal candidiasis': [
        'candida', 'thrush',],
    'Viral conjunctivitis': [
        'pink eye', 'red eye',],
    'Visual impairment, unspecified': [
        'bad eyesight', 'blurred vision', 'poor vision',],
    'Welding flash (arc eye)': [
        'arc eye', 'photokeratitis', 'welder', 'welding flash',],
    'Worm infestation': [
        'helminth', 'worms',],
    'Wound care (dressing)': [
        'dressing', 'dressing change', 'wound care',],
}


_DIAGNOSIS_CANONICAL = {old.strip().lower(): new
                        for old, new in DIAGNOSIS_RENAMES.items()}


def _canonical_diagnosis(name: str) -> str:
    """The name this diagnosis is called NOW.

    An old record says "Injury"; the list calls it "Injury, site unspecified".
    Counting those as two conditions would halve both figures and break the
    trend on the dashboard, which is the one place a diagnosis is counted at
    all. Anything not renamed comes back exactly as it was recorded.
    """
    if not name:
        return name
    return _DIAGNOSIS_CANONICAL.get(name.strip().lower(), name.strip())

# Login required decorator
def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
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
def enforce_session_timeout() -> ResponseReturnValue | None:
    if 'user' not in session:
        return None            # not logged in, nothing to enforce

    now = datetime.now(timezone.utc)
    last_active_str = session.get('last_active')

    if last_active_str:
        try:
            last_active = datetime.fromisoformat(last_active_str)
        except ValueError:
            last_active = None
        if last_active and (now - last_active) > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
            session.clear()
            flash(f'You were logged out after {SESSION_TIMEOUT_MINUTES} minutes '
                  f'of inactivity. Please log in again.', 'error')
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

    return None               # nothing to enforce: carry on to the route


# FIX (Security/XSS): escape user-controlled values before inserting into nav HTML.
# Previously the user's display name came straight from the DB and was rendered
# with |safe, allowing a name like <script>alert(1)</script> to execute.

# ---------------------------------------------------------------------------
# Session guard, rendered on every signed-in page via get_nav_links().
#
# Built as a plain string and concatenated rather than interpolated into an
# f-string: JavaScript is full of braces, and an f-string would need every one
# of them doubled. __TIMEOUT__ is substituted at render time.
# ---------------------------------------------------------------------------
SESSION_GUARD_JS = """
<div id="session-warn" class="session-warn"></div>
<script>
(function () {
    var TIMEOUT_MS = __TIMEOUT__ * 60 * 1000;
    var WARN_MS = 3 * 60 * 1000;      // warn with three minutes to go
    var MIN_PING_MS = 60 * 1000;      // at most one ping a minute
    var last = Date.now();            // last real interaction
    var lastPing = Date.now();
    var expired = false;
    var box = document.getElementById('session-warn');

    function show(msg, cls) {
        if (!box) return;
        box.textContent = msg;
        box.className = 'session-warn shown ' + (cls || '');
    }
    function hide() { if (box) box.className = 'session-warn'; }

    function ping() {
        lastPing = Date.now();
        var tokenMeta = document.querySelector('meta[name="csrf-token"]');
        fetch('/api/session-ping', {
            method: 'POST',
            headers: tokenMeta ? {'X-CSRF-Token': tokenMeta.content} : {}
        })
            .then(function (r) { if (!r.ok) markExpired(); })
            .catch(function () { /* offline: leave the countdown alone */ });
    }

    function markExpired() {
        expired = true;
        show('Your session has expired. Open the login page in another tab, '
             + 'sign in, then come back and save — what you have typed here '
             + 'is kept.', 'session-expired');
    }

    // Only genuine interaction counts. A page left open on an empty counter
    // makes none of these, so an unattended terminal still times out.
    ['keydown', 'click', 'input', 'change'].forEach(function (evt) {
        document.addEventListener(evt, function () {
            last = Date.now();
            if (expired) return;
            if (Date.now() - lastPing > MIN_PING_MS) ping();
        }, true);
    });

    setInterval(function () {
        if (expired) return;
        var idle = Date.now() - last;
        if (idle >= TIMEOUT_MS) {
            markExpired();
        } else if (idle >= TIMEOUT_MS - WARN_MS) {
            var mins = Math.max(1, Math.ceil((TIMEOUT_MS - idle) / 60000));
            show('You will be signed out in about ' + mins + ' minute'
                 + (mins === 1 ? '' : 's') + '. Type or click anywhere to stay '
                 + 'signed in.', 'session-soon');
        } else {
            hide();
        }
    }, 15000);
})();
</script>
"""

def get_nav_links() -> str:
    if 'user' in session:
        user = session['user']
        is_admin = user.get('role') == 'admin'
        # escape() returns a Markup object that Jinja will not double-escape,
        # but any HTML characters in the name are neutralised.
        name = escape(user.get('name', user.get('login', 'User')))
        add_med_link = '<a href="/add-medication">Add Medication</a> | ' if is_admin else ''
        audit_link = '<a href="/audit">Audit Log</a> | ' if is_admin else ''
        # NEW (Stock Take): admin-only physical count page.
        stock_take_link = '<a href="/stock-take">Stock Take</a> | ' if is_admin else ''
        # NEW (Equipment loans): the lending desk is counter work, so everyone
        # reaches it; the asset register is an admin decision.
        loan_items_link = '<a href="/loan-items">Equipment Register</a> | ' if is_admin else ''
        return f"""
        <p class="nav-links"><strong>Navigate:</strong>
            <a href="/dashboard">Dashboard</a> |
            <a href="/dispense">Dispensing</a> |
            <a href="/receive">Receiving</a> |
            <a href="/internal-issue">Internal Issues</a> |
            <a href="/temperature">Temperature</a> |
            <a href="/loans">Loans</a> |
            <a href="/key-register">Store Key</a> |
            {add_med_link}
            {loan_items_link}
            <a href="/reports">Reports</a> |
            {stock_take_link}
            {audit_link}
            <span>Welcome, {name}! <a href="/logout">Logout</a></span>
        </p>
        """ + SESSION_GUARD_JS.replace('__TIMEOUT__', str(SESSION_TIMEOUT_MINUTES))
    else:
        return """
        <p class="nav-links"><strong>Navigate:</strong>
            <a href="/login">Login</a> | <a href="/register">Register</a>
        </p>
        """

# ============================================================
# Templates
# ============================================================
#
# The pages live in templates/ as real .html files, and every one of them
# extends base.html — which holds the document head, the stylesheet, the
# viewport line and the inactivity guard that all of them share.
#
# They used to be Python strings in this file: a page was CSS_STYLE + the
# body, so the 18kB head was pasted into all seventeen of them and anything
# that had to change in the shared part had to be found in one string and
# could not be checked anywhere. That was 4,883 lines of HTML in the middle
# of the application code, and about 289,000 characters of it were the same
# 18kB repeated.
#
# render_template() looks them up by name; the folder is pinned to this
# file's own directory below, because a serverless handler does not
# necessarily run with its working directory anywhere near the code.

# ============================================================
# Routes
# ============================================================

@app.route('/', methods=['GET'])
@login_required
def home() -> ResponseReturnValue:
    return redirect('/reports')


@app.route('/login', methods=['GET', 'POST'])
def login() -> ResponseReturnValue:
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
                # Keep the store key register in step with who is on duty.
                # Guarded here as well as inside: this is the login path, and
                # no change to the key register should ever be able to lock
                # everybody out of the app.
                try:
                    _key_on_login(session['user']['name'],
                                  session['user'].get('role') == 'admin')
                except Exception as e:
                    app.logger.warning(f'key register login hook failed: {e}')

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
    return render_template('login.html', error=error)


@app.route('/change-expired-password', methods=['GET', 'POST'])
@login_required
def change_expired_password() -> ResponseReturnValue:
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

    return render_template('change_expired_password.html', error=error)


@app.route('/register', methods=['GET', 'POST'])
def register() -> ResponseReturnValue:
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
        return render_template('register_password.html', error=error)
    else:
        return render_template('register.html', error=error, message=message)


@app.route('/logout', methods=['GET'])
def logout() -> ResponseReturnValue:
    # The pharmacist logging out IS the key handover: whoever logs in next is
    # holding it. Arming here means nobody has to record anything.
    user = session.get('user') or {}
    if user.get('role') == 'admin':
        try:
            _key_arm_handover(user.get('name', ''))
        except Exception as e:
            app.logger.warning(f'key handover arm failed: {e}')
    session.pop('user', None)
    session.pop('admin_access', None)
    return redirect('/login')


@app.route('/dispense', methods=['GET', 'POST'])
@login_required
def dispense() -> ResponseReturnValue:
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        # Read any flashed message from a preceding redirect (e.g. from
        # delete_dispense success/error, or access-denied from other routes).
        # with_categories so the banner colour is decided by the route, which
        # knows WHY the dispense was partial, rather than by the template
        # guessing from words in the message.
        message_class, message = _flashed_banner()
        start_date = request.values.get('start_date')
        end_date = request.values.get('end_date')
        search = request.values.get('search')
        current_user = session['user']['name']
        # FIX (Performance): default to the current month so an unfiltered
        # visit never asks for the whole history. A search bypasses this and
        # looks across all time.
        start_date, end_date, window_defaulted = _default_window(start_date, end_date, search)
        page = _requested_page()

        base_query: Query = {'type': 'dispense'}
        date_query: Query = {}
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
                {'outcome': {'$regex': search, '$options': 'i'}},
                {'gender':    {'$regex': search, '$options': 'i'}},
                {'prescriber':{'$regex': search, '$options': 'i'}},
                {'dispenser': {'$regex': search, '$options': 'i'}},
                {'date':      {'$regex': search, '$options': 'i'}},
                {'diagnoses.0':{'$regex': search, '$options': 'i'}},
            ]

        # The download is the whole filtered record, not the page on screen.
        if request.values.get('export') == 'csv':
            made = _dispense_csv(transactions, base_query)
            if made:
                filename, body = made
                resp = make_response(body)
                resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
                resp.headers['Content-Disposition'] = \
                    f'attachment; filename="{filename}"'
                return resp
            flash(f'That is more than {DISPENSE_CSV_MAX_LINES:,} lines to '
                  f'download at once. Narrow the date range and try again.')
            return redirect(url_for('dispense', start_date=start_date or '',
                                    end_date=end_date or '', search=search or ''))

        # FIX (Performance): page over VISITS, not lines. Paging the raw
        # transaction rows would split a patient's three medicines across a
        # page break, so the distinct transaction_ids are paged first and then
        # every line belonging to that page's visits is fetched. A search that
        # matches one line still shows the whole visit it belongs to, which is
        # what someone looking up a record actually wants.
        visit_ids, total_visits = [], 0
        try:
            counted = list(transactions.aggregate([
                {'$match': base_query},
                {'$group': {'_id': '$transaction_id'}},
                {'$count': 'n'},
            ]))
            total_visits = counted[0]['n'] if counted else 0
            pager = _pager(page, DISPENSE_PAGE_SIZE, total_visits)
            visit_ids = [g['_id'] for g in transactions.aggregate([
                {'$match': base_query},
                {'$group': {'_id': '$transaction_id', 'ts': {'$max': '$timestamp'}}},
                {'$sort': {'ts': -1}},
                {'$skip': (pager['page'] - 1) * DISPENSE_PAGE_SIZE},
                {'$limit': DISPENSE_PAGE_SIZE},
            ])]
        except Exception as e:
            # Never let a paging failure turn back into an unbounded fetch.
            app.logger.warning(f"Dispense paging aggregation unavailable: {e}")
            seen, capped = [], transactions.find(base_query).sort('timestamp', -1).limit(
                DISPENSE_PAGE_SIZE * 40)
            for t in capped:
                if t.get('transaction_id') not in seen:
                    seen.append(t.get('transaction_id'))
            total_visits = len(seen)
            pager = _pager(page, DISPENSE_PAGE_SIZE, total_visits)
            start = (pager['page'] - 1) * DISPENSE_PAGE_SIZE
            visit_ids = seen[start:start + DISPENSE_PAGE_SIZE]

        raw_tx = list(transactions.find(
            {'type': 'dispense', 'transaction_id': {'$in': visit_ids}}
        ).sort('timestamp', -1)) if visit_ids else []

        # FIX (UX/Correctness): group rows by transaction_id in Python so the
        # template can use reliable rowspan values regardless of sort order or
        # shared timestamps.  Ordered by the paged visit order.
        # BUG: t['transaction_id'] was a direct key access. A dispense record
        # without that field groups under None, which $in then matches, and the
        # KeyError took the WHOLE dispensing page down with a 500 — no form, no
        # history, nothing. One malformed old record must not be able to do
        # that, so the field is read defensively and such rows are grouped
        # under None, which no visit id matches, so they simply do not list.
        grouped: dict[Any, Docs] = {}
        for t in raw_tx:
            grouped.setdefault(t.get('transaction_id'), []).append(t)
        tx_groups = [(tid, grouped[tid]) for tid in visit_ids if tid in grouped]
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
                # Stored as the year itself; age is derived at report time so it
                # never goes stale.
                # Stored as the first year of the decade (1980 means 1980-1989),
                # so the range can always be re-derived and never goes stale the
                # way a recorded age band would.
                try:
                    birth_decade = int(request.form.get('birth_decade') or 0) or None
                except (TypeError, ValueError):
                    birth_decade = None
                prescriber    = request.form['prescriber']
                dispenser     = request.form['dispenser']
                date_str      = request.form['date']
                gender        = request.form['gender']
                outcome = (request.form.get('outcome') or '').strip() or 'Outpatient'
                try:
                    outcome_days = int(request.form.get('outcome_days') or 0) or None
                except (TypeError, ValueError):
                    outcome_days = None
                # Days belong to sick leave only. The field is hidden for every
                # other outcome, but hiding it does not clear it: pick Sick
                # leave, type 3, change to Referral, and the browser still
                # submits 3. Enforced here as well so a referral can never
                # carry days, whatever the form sends or whether its script ran.
                if outcome != 'Sick leave':
                    outcome_days = None
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
                        # NEW: the two partial outcomes mean different things and
                        # need different urgency. Running short is routine — the
                        # shortfall is captured as unmet demand and the order
                        # request will act on it. A medication that is NOT ON FILE
                        # is a data fault: nothing was recorded against it at all,
                        # and it stays broken until someone fixes the item. Showing
                        # both in the same amber banner buries the second.
                        missing_msgs = []
                        short_msgs = []
                        dispensed_meds = []
                        for med_name, quantity in zip(med_names, quantities):
                            med = medications.find_one({'name': med_name})
                            if not med:
                                missing_msgs.append(f'"{med_name}" is not on file')
                                write_app_warning(
                                    'medication_not_found',
                                    f'Medication "{med_name}" not found during dispense.',
                                    {'med_name': med_name, 'requested_quantity': quantity,
                                     'patient': patient, 'transaction_id': tx_id}
                                )
                            else:
                                # NEW (Unmet demand): a short line used to be
                                # refused ENTIRELY — not even the units actually on
                                # the shelf were recorded. In practice the
                                # pharmacist still hands over what they have, so the
                                # software was manufacturing the very
                                # "issued, not recorded" discrepancies the stock
                                # take exists to find. Now the available units are
                                # dispensed and the shortfall is recorded as demand
                                # that could not be met.
                                available = med.get('balance', 0) or 0
                                given     = min(quantity, available)
                                short     = quantity - given
                                if short > 0:
                                    unit_price = med.get('price', 0) or 0
                                    transactions.insert_one({
                                        'type': 'unmet_demand',
                                        'transaction_id': tx_id,
                                        'med_name': med_name,
                                        # quantity is 0 so this NEVER moves stock in
                                        # any balance calculation; the demand figures
                                        # live in their own fields.
                                        'quantity': 0,
                                        'requested': quantity, 'given': given,
                                        'shortfall': short,
                                        'price': unit_price,
                                        'shortfall_value': short * unit_price,
                                        'patient': patient, 'prescriber': prescriber,
                                        'dispenser': dispenser, 'date': date_str,
                                        'user': current_user,
                                        'timestamp': datetime.utcnow(),
                                    })
                                    write_app_warning(
                                        'insufficient_stock',
                                        f'Insufficient stock for "{med_name}" during dispense.',
                                        {'med_name': med_name, 'requested_quantity': quantity,
                                         'available_quantity': available,
                                         'dispensed_quantity': given,
                                         'patient': patient, 'transaction_id': tx_id}
                                    )
                                    short_msgs.append(
                                        f'{med_name}: only {given} of {quantity} available, '
                                        f'{short} recorded as unmet demand')
                                if given <= 0:
                                    continue
                                quantity = given
                                medications.update_one({'name': med_name}, {'$inc': {'balance': -quantity}})
                                transactions.insert_one({
                                    'type': 'dispense',
                                    'transaction_id': tx_id,
                                    'patient': patient,
                                    'company': company,
                                    'position': position,
                                    'birth_decade': birth_decade,
                                    'gender': gender,
                                    'outcome': outcome,
                                    'outcome_days': outcome_days,
                                    'diagnoses': diagnoses,
                                    'prescriber': prescriber,
                                    'dispenser': dispenser,
                                    'user': current_user,
                                    'date': date_str,
                                    'med_name': med_name,
                                    'quantity': quantity,
                                    # NEW (Costing): freeze the unit price AT THE
                                    # MOMENT OF DISPENSING. Costing historical
                                    # activity by looking up today's price would
                                    # silently rewrite last year's figures every
                                    # time a supplier changed a rate — a report
                                    # run twice would not agree with itself.
                                    'price': med.get('price', 0) or 0,
                                    'pack_size': med.get('pack_size'),
                                    'line_value': quantity * (med.get('price', 0) or 0),
                                    'timestamp': datetime.utcnow()
                                })
                                dispensed_meds.append(med_name)

                        parts = []
                        if dispensed_meds:
                            parts.append(f'{message_prefix}: {", ".join(dispensed_meds)}')
                        if short_msgs:
                            parts.append('Short: ' + '; '.join(short_msgs))
                        if missing_msgs:
                            parts.append('NOT ON FILE: ' + '; '.join(missing_msgs)
                                         + ' — nothing was recorded for these. '
                                           'Add the item under Add Medication, then dispense it.')

                        if missing_msgs:
                            # Red: needs someone to fix the data before this
                            # prescription is complete.
                            severity = 'error'
                            head = ('Partial — item not on file' if dispensed_meds
                                    else 'Not dispensed — item not on file')
                        elif short_msgs:
                            severity = 'partial'
                            head = ('Partial — insufficient stock' if dispensed_meds
                                    else 'Not dispensed — no stock')
                        elif dispensed_meds:
                            severity = 'success'
                            head = f'{message_prefix} successfully'
                        else:
                            severity = 'error'
                            head = f'No medications {message_prefix.lower()}'
                        message = head + ('. ' + ' | '.join(parts) if parts else '.')

                        # FIX (Bug): this used to fall through and re-render with
                        # the transaction list that was fetched BEFORE the insert,
                        # so a dispense you had just recorded did not appear until
                        # the next one pushed it into view.
                        #
                        # It also left the browser sitting on a POST, so a refresh
                        # re-submitted the whole dispense and deducted the stock a
                        # second time. Redirecting after the write fixes both.
                        # BUG: the form was cleared only if the message said
                        # "successfully". A partial dispense says "Partial —
                        # insufficient stock", so the form stayed loaded with
                        # the whole visit in it — and the items that DID
                        # dispense were already recorded and already deducted.
                        # Pressing Dispense again, which is the natural thing
                        # to do when a form looks unsubmitted, dispensed them a
                        # second time. Whether the visit was written is a fact
                        # the server knows; it must not be inferred from the
                        # wording of a message.
                        #   dispensed_meds -> dispense records written
                        #   short_msgs     -> unmet demand records written
                        #   missing_msgs alone -> nothing written, so the form
                        #                         stays for the user to fix.
                        session['dispense_saved'] = bool(dispensed_meds or short_msgs)
                        flash(message, severity)
                        return redirect(url_for('dispense'))
            except ValueError as e:
                message = f'Invalid input: {str(e)}'

        return render_template(
            'dispense.html',
            tx_list=tx_list,
            tx_groups=tx_groups,
            nav_links=get_nav_links(),
            message=message,
            start_date=start_date,
            end_date=end_date,
            search=search,
            tx_data=tx_data, current_year=datetime.utcnow().year,
            message_class=message_class,
            form_saved=session.pop('dispense_saved', False),
            is_admin=session['user'].get('role') == 'admin',
            prescribers=_staff_named_for(db, 'prescriber'),
            dispensers=_staff_named_for(db, 'dispenser'),
            today=local_now().strftime('%Y-%m-%d'),
            current_user=session['user']['name'],
            form_defaults=_new_visit_defaults(session['user']['name']),
            birth_decades=_birth_decades(),
            pager=pager, window_defaulted=window_defaulted, unit='visits'
        )
    except ServerSelectionTimeoutError:
        return render_template(
            'dispense.html',
            tx_list=[], tx_groups=[],
            nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            start_date='', end_date='', search='', tx_data=None,
            current_year=datetime.utcnow().year, birth_decades=_birth_decades(),
            today=local_now().strftime('%Y-%m-%d'),
            current_user=session.get('user', {}).get('name', ''),
            form_defaults=_new_visit_defaults(session.get('user', {}).get('name', '')),
            message_class='error', form_saved=False,
            is_admin=session.get('user', {}).get('role') == 'admin',
            # The database is unreachable on this path, so the roster falls
            # back to the seed rather than leaving both dropdowns empty.
            prescribers=_staff_named_for(None, 'prescriber'),
            dispensers=_staff_named_for(None, 'dispenser'),
            pager={'page': 1, 'pages': 1, 'total': 0, 'page_size': 0, 'has_prev': False, 'has_next': False, 'first': 0, 'last': 0}, window_defaulted=False, unit='visits'
        ), 500


def _load_delivery(deliveries: Collection, raw_id: str | None) -> Doc | None:
    if not raw_id:
        return None
    try:
        return deliveries.find_one({'_id': ObjectId(raw_id)})
    except (InvalidId, TypeError):
        return None


def _line_value(line: Doc) -> float:
    """Value of one receive line.

    NEW (Pack pricing): suppliers price by the pack but stock is counted in
    single units, so a line stores the pack figures as entered and the unit
    price is DERIVED from them. Reconciliation uses the stored line_value,
    computed from the pack price directly, because multiplying a rounded unit
    price back out by the quantity drifts: a pack of 3 at R250 gives a unit
    price of R83.3333..., and 3 x R83.33 is R249.99, which would fail an
    otherwise perfect invoice match by a cent.

    Falls back to quantity x price for rows written before pack pricing
    existed, and for lines edited through the single-line edit form.
    """
    if line.get('line_value') is not None:
        return line['line_value']
    return (line.get('quantity', 0) or 0) * (line.get('price', 0) or 0)


def _unit_price(pack_price: float, pack_size: float | None) -> float:
    """Price of a single countable unit, from the pack price and pack size."""
    pack_size = pack_size or 1
    return pack_price / pack_size if pack_size else pack_price


def _delivery_goods_value(transactions: Collection, delivery_id: Any) -> float:
    return sum(_line_value(l) for l in
               transactions.find({'type': 'receive', 'delivery_id': delivery_id},
                                 {'_id': 0, 'quantity': 1, 'price': 1, 'line_value': 1}))


def _order_rollup(deliveries: Collection, transactions: Collection, order_number: str | None) -> Doc | None:
    """NEW (Multiple invoices per order): one purchase order is normally
    fulfilled across several part-deliveries, each with its own invoice. The
    order-level position is therefore the SUM of the invoices raised against
    that order number, not any single one.

    There is no separate order record — deliveries are grouped by the order
    number typed on each invoice — so the order total is taken from the most
    recently opened delivery for that order, and a disagreement between
    deliveries is surfaced rather than silently resolved.
    """
    sibs = list(deliveries.find({'order_number': order_number}).sort('opened_at', -1))
    if not sibs:
        return None
    totals = {round(d.get('order_total', 0) or 0, 2) for d in sibs}
    order_total = sibs[0].get('order_total', 0) or 0
    invoiced = sum(d.get('invoice_total', 0) or 0 for d in sibs)
    goods = sum(_line_value(l) for l in transactions.find(
        {'type': 'receive', 'delivery_id': {'$in': [str(d['_id']) for d in sibs]}},
        {'_id': 0, 'quantity': 1, 'price': 1, 'line_value': 1}))
    outstanding = order_total - invoiced
    return {
        'order_number': order_number, 'order_total': order_total,
        'invoice_count': len(sibs), 'invoiced': invoiced, 'goods': goods,
        'outstanding': outstanding,
        'over_invoiced': invoiced - order_total > 0.005,
        'fully_invoiced': abs(outstanding) < 0.01,
        'conflicting_totals': len(totals) > 1,
        'siblings': [{'reference': d['reference'], 'invoice_number': d.get('invoice_number'),
                      'invoice_total': d.get('invoice_total', 0) or 0,
                      'status': d.get('status'), 'id': str(d['_id'])} for d in sibs],
    }


def _delivery_values(transactions: Collection, delivery_ids: Sequence[Any]) -> dict[Any, tuple[float, int]]:
    """delivery_id -> (goods value, line count), in one aggregation."""
    if not delivery_ids:
        return {}
    pipeline = [
        {'$match': {'type': 'receive', 'delivery_id': {'$in': delivery_ids}}},
        {'$group': {'_id': '$delivery_id',
                    'value': {'$sum': {'$ifNull': ['$line_value',
                                                   {'$multiply': ['$quantity', '$price']}]}},
                    'lines': {'$sum': 1}}},
    ]
    try:
        return {r['_id']: (r.get('value', 0) or 0, r.get('lines', 0)) for r in
                transactions.aggregate(pipeline)}
    except Exception as e:
        app.logger.warning(f"Delivery value aggregation unavailable: {e}")
        out: dict[Any, tuple[float, int]] = {}
        for l in transactions.find({'type': 'receive', 'delivery_id': {'$in': delivery_ids}},
                                   {'_id': 0, 'delivery_id': 1, 'quantity': 1,
                                    'price': 1, 'line_value': 1}):
            v, n = out.get(l['delivery_id'], (0.0, 0))
            out[l['delivery_id']] = (v + _line_value(l), n + 1)
        return out


def _reconcile_delivery(delivery: Doc, goods_value: float) -> Doc:
    """Three-way match: order vs invoice vs goods actually received.

    Differences are reported, never enforced. Stock physically standing on the
    shelf has to be recordable whatever the paperwork says; the job of this
    panel is to make sure nobody can record it without seeing the discrepancy.
    """
    order_total   = delivery.get('order_total', 0) or 0
    invoice_total = delivery.get('invoice_total', 0) or 0
    order_diff    = invoice_total - order_total
    goods_diff    = goods_value - invoice_total

    def band(diff, over, under):
        if abs(diff) < 0.01:
            return 'normal', 'matched', 'Matched'
        return ('expired' if diff > 0 else 'close-to-expire',
                f'R{diff:+.2f}', over if diff > 0 else under)

    o_class, o_label, o_note = band(
        order_diff, 'Invoiced above the order', 'Invoiced below the order')
    g_class, g_label, g_note = band(
        goods_diff, 'More goods than invoiced', 'Fewer goods than invoiced')
    return {'goods_value': goods_value,
            'order_class': o_class, 'order_diff_label': o_label, 'order_note': o_note,
            'goods_class': g_class, 'goods_diff_label': g_label, 'goods_note': g_note,
            'matched': abs(goods_diff) < 0.01 and abs(order_diff) < 0.01}


@app.route('/receive', methods=['GET', 'POST'])
@login_required
def receive() -> ResponseReturnValue:
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        deliveries = db['deliveries']
        # Read any flashed message from a preceding redirect (e.g. from
        # delete_receive success/error, or access-denied from other routes).
        flashed = get_flashed_messages()
        message = flashed[0] if flashed else None
        start_date = request.values.get('start_date')
        end_date = request.values.get('end_date')
        search = request.values.get('search')
        current_user = session['user']['name']
        # FIX (Performance): default to the current month so an unfiltered
        # visit never asks for the whole history. A search bypasses this and
        # looks across all time.
        start_date, end_date, window_defaulted = _default_window(start_date, end_date, search)
        page = _requested_page()

        base_query: Query = {'type': 'receive'}
        date_query: Query = {}
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

        # FIX (Performance): a flat list, so ordinary skip/limit paging.
        total_lines = transactions.count_documents(base_query)
        pager = _pager(page, RECEIVE_PAGE_SIZE, total_lines)
        tx_list = list(transactions.find(base_query)
                       .sort('timestamp', -1)
                       .skip((pager['page'] - 1) * RECEIVE_PAGE_SIZE)
                       .limit(RECEIVE_PAGE_SIZE))

        # FIX (Bug): the original GET-based edit path used {'_id': edit_id} (a raw
        # string) which never matched a MongoDB ObjectId and always returned None.
        # The edit-receive route handles this properly, so the receive route now
        # simply never tries to load rx_data inline — editing always goes via
        # /edit-receive/<id>.
        rx_data = None

        # ---------------- Delivery actions ----------------------------
        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'open':
                try:
                    reference = f"DEL-{datetime.utcnow().strftime('%Y%m%d')}-{uuid4().hex[:4].upper()}"
                    new_id = deliveries.insert_one({
                        'reference': reference,
                        'supplier': request.form['supplier'].strip(),
                        'order_number': request.form['order_number'].strip(),
                        'order_total': float(request.form['order_total']),
                        'invoice_number': request.form['invoice_number'].strip(),
                        'invoice_total': float(request.form['invoice_total']),
                        'stock_receiver': request.form['stock_receiver'].strip(),
                        'note': (request.form.get('note') or '').strip(),
                        'status': 'open',
                        'received_by': current_user,
                        'opened_at': datetime.utcnow(),
                        'closed_by': None, 'closed_at': None,
                    }).inserted_id
                    write_audit_entry('CREATE', 'delivery', reference,
                                      f'Delivery {reference} opened by {current_user}')
                    flash(f'Delivery {reference} opened.')
                    return redirect(url_for('receive', delivery_id=str(new_id)))
                except (ValueError, KeyError) as e:
                    flash(f'Invalid delivery details: {e}')
                    return redirect(url_for('receive'))

            if action == 'line':
                d = _load_delivery(deliveries, request.form.get('delivery_id'))
                if not d:
                    flash('Delivery not found.')
                    return redirect(url_for('receive'))
                if d['status'] != 'open':
                    flash(f"Delivery {d['reference']} is closed. No further lines can be added.")
                    return redirect(url_for('receive', delivery_id=str(d['_id'])))
                try:
                    med_name    = request.form['med_name'].strip()
                    quantity    = int(request.form['quantity'])       # single units
                    pack_size   = int(request.form.get('pack_size') or 1)
                    pack_price  = float(request.form['pack_price'])
                    batch       = request.form['batch'].strip()
                    expiry_date = request.form['expiry_date']
                    if quantity < 1:
                        raise ValueError('quantity must be at least 1')
                    if pack_size < 1:
                        raise ValueError('pack size must be at least 1')
                    if pack_price < 0:
                        raise ValueError('price cannot be negative')
                except (ValueError, KeyError) as e:
                    flash(f'Invalid line: {e}')
                    return redirect(url_for('receive', delivery_id=str(d['_id'])))

                # NEW (Pack pricing): stock is counted in single units but
                # suppliers price by the pack, so the unit price is derived
                # rather than typed. Line value is computed from the pack price
                # directly so that a unit price which does not divide cleanly
                # (a pack of 3, say) cannot introduce a rounding mismatch
                # against the invoice.
                price      = _unit_price(pack_price, pack_size)
                line_value = quantity * pack_price / pack_size

                # FIX (Data integrity): this used to upsert, so a typo in the
                # medication name silently CREATED a new medication and put the
                # stock there, leaving the real item's balance untouched and
                # nobody any the wiser. Receiving now refuses an item that has
                # not been defined, exactly as the stock take does.
                existing_med = medications.find_one({'name': med_name})
                if not existing_med:
                    write_app_warning(
                        'medication_not_found',
                        f'Medication "{med_name}" not found while receiving on '
                        f'{d["reference"]}.',
                        {'med_name': med_name, 'delivery': d['reference']})
                    flash(f'Medication "{med_name}" is not on file. Add it under '
                          f'Add Medication first, then receive it here.')
                    return redirect(url_for('receive', delivery_id=str(d['_id'])))

                # Classification comes from the medication record, never from
                # the form — see the note on the add-line template.
                schedule = existing_med.get('schedule', 'not controlled')

                # Supplier / order / invoice are INHERITED from the delivery
                # header — that is the whole point: they are typed once per
                # delivery, not once per medication, so they cannot drift
                # between lines of the same consignment.
                medications.update_one(
                    {'name': med_name},
                    {'$inc': {'balance': quantity},
                     '$set': {'batch': batch, 'price': price, 'expiry_date': expiry_date,
                              'pack_size': pack_size, 'pack_price': pack_price,
                              'stock_receiver': d['stock_receiver'],
                              'order_number': d['order_number'], 'supplier': d['supplier'],
                              'invoice_number': d['invoice_number']}}
                )
                transactions.insert_one({
                    'type': 'receive',
                    'med_name': med_name, 'quantity': quantity, 'batch': batch,
                    'price': price, 'pack_size': pack_size, 'pack_price': pack_price,
                    'line_value': line_value,
                    'expiry_date': expiry_date, 'schedule': schedule,
                    'stock_receiver': d['stock_receiver'],
                    'order_number': d['order_number'], 'supplier': d['supplier'],
                    'invoice_number': d['invoice_number'],
                    'delivery_id': str(d['_id']), 'delivery_reference': d['reference'],
                    'user': current_user, 'timestamp': datetime.utcnow(),
                })
                packs = quantity / pack_size
                flash(f'{med_name}: {quantity} units received onto {d["reference"]} '
                      f'({packs:g} x pack of {pack_size} at R{pack_price:.2f} = '
                      f'R{line_value:.2f}; unit price R{price:.4f}).')
                # line_added tells the page the line really posted, so the
                # half-typed draft can be dropped. The page must not try to
                # work this out by reading its own text.
                return redirect(url_for('receive', delivery_id=str(d['_id']),
                                        line_added=1))

            if action == 'close':
                d = _load_delivery(deliveries, request.form.get('delivery_id'))
                if not d:
                    flash('Delivery not found.')
                    return redirect(url_for('receive'))
                if d['status'] != 'open':
                    flash(f"Delivery {d['reference']} is already closed.")
                    return redirect(url_for('receive', delivery_id=str(d['_id'])))
                goods = _delivery_goods_value(transactions, str(d['_id']))
                deliveries.update_one(
                    {'_id': d['_id'], 'status': 'open'},
                    {'$set': {'status': 'closed', 'closed_by': current_user,
                              'closed_at': datetime.utcnow(),
                              'goods_value_at_close': goods}})
                write_audit_entry('UPDATE', 'delivery', d['reference'],
                                  f"closed by {current_user}; goods R{goods:.2f} "
                                  f"vs invoice R{d['invoice_total']:.2f}")
                # Warn but allow: a mismatch is surfaced, never blocking.
                if abs(goods - d['invoice_total']) >= 0.01:
                    flash(f"Delivery {d['reference']} closed with a mismatch: goods received "
                          f"R{goods:.2f} against invoice R{d['invoice_total']:.2f} "
                          f"(difference R{goods - d['invoice_total']:+.2f}).")
                else:
                    flash(f"Delivery {d['reference']} closed and fully matched.")
                return redirect(url_for('receive', delivery_id=str(d['_id'])))

            flash('Unknown receiving action.')
            return redirect(url_for('receive'))

        # ---------------- Delivery views ------------------------------
        delivery, delivery_lines, recon, delivery_rows = None, [], {}, []
        order_summary = None
        delivery = _load_delivery(deliveries, request.values.get('delivery_id'))
        if delivery:
            delivery_lines = list(transactions.find(
                {'type': 'receive', 'delivery_id': str(delivery['_id'])}
            ).sort('timestamp', 1).limit(500))
            recon = _reconcile_delivery(delivery,
                                        sum(_line_value(l) for l in delivery_lines))
            order_summary = _order_rollup(deliveries, transactions,
                                          delivery.get('order_number'))
        elif not rx_data:
            delivery_rows = list(deliveries.find().sort([('status', 1), ('opened_at', -1)]).limit(200))
            values = _delivery_values(transactions, [str(d['_id']) for d in delivery_rows])
            for d in delivery_rows:
                goods_value, line_count = values.get(str(d['_id']), (0.0, 0))
                d['goods_value'], d['line_count'] = goods_value, line_count
                diff = d['goods_value'] - d.get('invoice_total', 0)
                matched = abs(diff) < 0.01
                d['diff_label'] = 'matched' if matched else f'R{diff:+.2f}'
                d['row_class'] = ('close-to-expire' if d['status'] == 'open'
                                  else ('normal' if matched else 'expired'))

        return render_template(
            'receive.html',
            tx_list=tx_list, nav_links=get_nav_links(),
            message=message, start_date=start_date, end_date=end_date,
            search=search, rx_data=rx_data,
            current_user=session['user']['name'],
            delivery=delivery, delivery_lines=delivery_lines, recon=recon,
            deliveries=delivery_rows, order_summary=order_summary,
            pager=pager, window_defaulted=window_defaulted, unit='receipts'
        )
    except ServerSelectionTimeoutError:
        return render_template(
            'receive.html', tx_list=[], nav_links=get_nav_links(),
            delivery=None, delivery_lines=[], recon={}, deliveries=[], order_summary=None,
            message="Database connection failed.", start_date='', end_date='', search='', rx_data=None,
            current_user=session.get('user', {}).get('name', ''),
            pager={'page': 1, 'pages': 1, 'total': 0, 'page_size': 0, 'has_prev': False, 'has_next': False, 'first': 0, 'last': 0}, window_defaulted=False, unit='receipts'
        ), 500


@app.route('/add-medication', methods=['GET', 'POST'])
@login_required
def add_medication() -> ResponseReturnValue:
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
                med_name       = request.form['med_name'].strip()
                # NEW (Pack pricing): the unit price is derived here too, so an
                # item defined on this page values its stock correctly from the
                # moment it is created rather than only after it is first
                # received on a delivery.
                category       = request.form.get('category') or CATEGORY_PHARM
                if category not in CATEGORIES:
                    category = CATEGORY_PHARM
                pack_size      = int(request.form.get('pack_size') or 1)
                pack_price     = float(request.form['pack_price'])
                schedule       = request.form['schedule']
                initial_balance= int(request.form.get('initial_balance') or 0)
                batch          = (request.form.get('batch') or '').strip()
                expiry_date    = (request.form.get('expiry_date') or '').strip()
                stock_receiver = (request.form.get('stock_receiver') or '').strip()
                if pack_size < 1:
                    raise ValueError('pack size must be at least 1')
                if pack_price < 0:
                    raise ValueError('price cannot be negative')
                if initial_balance < 0:
                    raise ValueError('opening balance cannot be negative')

                if medications.find_one({'name': med_name}):
                    message = f'Medication "{med_name}" already exists. Use Receiving to add stock.'
                    return render_template('add_medication.html', nav_links=get_nav_links(), message=message,
                     current_user=session.get('user', {}).get('name', ''),
                                                  categories=CATEGORIES)

                price = _unit_price(pack_price, pack_size)
                medications.insert_one({
                    'name': med_name, 'category': category,
                    'balance': initial_balance, 'batch': batch,
                    'price': price, 'pack_size': pack_size, 'pack_price': pack_price,
                    'expiry_date': expiry_date, 'schedule': schedule,
                    'stock_receiver': stock_receiver,
                })

                # An opening load is stock that was already on the shelf, not a
                # supplier delivery. It is written as its own transaction type so
                # that it can never be mistaken for one: it has no invoice to
                # reconcile against, it must not appear in the receive list or
                # the delivery totals, and it must not be read as a stock-take
                # correction either. Reports that back-calculate balances treat
                # it exactly like a receipt, which is what it is in stock terms.
                if initial_balance > 0:
                    transactions.insert_one({
                        'type': 'opening_balance',
                        'med_name': med_name, 'quantity': initial_balance,
                        'batch': batch, 'price': price,
                        'pack_size': pack_size, 'pack_price': pack_price,
                        'line_value': initial_balance * pack_price / pack_size,
                        'expiry_date': expiry_date, 'schedule': schedule,
                        'stock_receiver': stock_receiver,
                        'user': current_user, 'timestamp': datetime.utcnow(),
                    })
                    write_audit_entry('CREATE', 'opening_balance', med_name,
                                      f'opening stock of {initial_balance} units '
                                      f'recorded by {current_user}')
                    message = (f'Medication added with an opening balance of '
                               f'{initial_balance} units (unit price R{price:.4f}).')
                else:
                    message = 'Medication added successfully! Receive stock against a delivery.'
                return render_template('add_medication.html', nav_links=get_nav_links(), message=message,
                     current_user=session.get('user', {}).get('name', ''),
                                                  categories=CATEGORIES)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template('add_medication.html', nav_links=get_nav_links(), message=message,
                     current_user=session.get('user', {}).get('name', ''),
                                                  categories=CATEGORIES)

        return render_template('add_medication.html', nav_links=get_nav_links(), message=message,
                     current_user=session.get('user', {}).get('name', ''),
                                                  categories=CATEGORIES)
    except ServerSelectionTimeoutError:
        return render_template(
            'add_medication.html', nav_links=get_nav_links(),
            message="Database connection failed. Please try again later."
        ), 500


@app.route('/edit-medication', methods=['GET', 'POST'])
@login_required
def edit_medication() -> ResponseReturnValue:
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
            return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES,
                                          message=message, med_data=None, med_name=None)

        med = medications.find_one({'name': med_name})
        if not med:
            message = f'Medication "{med_name}" not found.'
            write_app_warning(
                'medication_not_found',
                f'Medication "{med_name}" not found when attempting to edit.',
                {'med_name': med_name}
            )
            return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES,
                                          message=message, med_data=None, med_name=med_name)

        med_data = med
        if request.method == 'POST':
            try:
                batch       = request.form['batch']
                # NEW (Pack pricing): the unit price is derived here as well, so
                # a pack size can be corrected without waiting for the item to be
                # received again — which is what the order request needs in order
                # to ask for whole packs.
                category    = request.form.get('category') or CATEGORY_PHARM
                if category not in CATEGORIES:
                    category = CATEGORY_PHARM
                pack_size   = int(request.form.get('pack_size') or 1)
                pack_price  = float(request.form['pack_price'])
                if pack_size < 1:
                    raise ValueError('pack size must be at least 1')
                if pack_price < 0:
                    raise ValueError('price cannot be negative')
                price       = _unit_price(pack_price, pack_size)
                expiry_date = request.form['expiry_date']
                schedule    = request.form['schedule']
                medications.update_one(
                    {'name': med_name},
                    # balance deliberately absent — see the note in the template
                    {'$set': {'batch': batch, 'price': price, 'category': category,
                              'pack_size': pack_size, 'pack_price': pack_price,
                              'expiry_date': expiry_date, 'schedule': schedule}}
                )
                message = 'Medication updated successfully!'
                med_data = medications.find_one({'name': med_name})
                return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES,
                                              message=message, med_data=med_data, med_name=med_name)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES,
                                              message=message, med_data=med_data, med_name=med_name)

        return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES,
                                      message=message, med_data=med_data, med_name=med_name)
    except ServerSelectionTimeoutError:
        # FIX: med_name may not be assigned yet if get_mongo_client() itself
        # raised this before reaching that line — read it defensively here
        # rather than risk an UnboundLocalError masking the real DB error.
        fallback_med_name = (request.form.get('med_name') if request.method == 'POST'
                             else request.args.get('med_name'))
        return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES,
                                      message="Database connection failed.", med_data=None, med_name=fallback_med_name), 500


@app.route('/delete-medication', methods=['POST'])
@login_required
def delete_medication() -> ResponseReturnValue:
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


def _demand_by_med(transactions: Collection, time_filter: Query,
                   med_names: list[str] | None = None) -> dict[str, Doc]:
    """med_name -> {'shortfall', 'events'} — demand that could not be met.

    Kept apart from _movement_by_med on purpose: unmet demand moves no stock,
    so it must never touch a balance calculation. It exists only to correct the
    demand signal, which consumption alone understates precisely when an item
    is stocking out — the case where an accurate figure matters most.
    """
    match = {'type': 'unmet_demand', 'timestamp': time_filter}
    if med_names is not None:
        match['med_name'] = {'$in': med_names}
    try:
        return {r['_id']: {'shortfall': r.get('shortfall', 0) or 0,
                           'events': r.get('events', 0) or 0}
                for r in transactions.aggregate([
                    {'$match': match},
                    {'$group': {'_id': '$med_name',
                                'shortfall': {'$sum': '$shortfall'},
                                'events': {'$sum': 1}}}])}
    except Exception as e:
        app.logger.warning(f"Demand aggregation unavailable, falling back: {e}")
        out: dict[str, Doc] = {}
        for t in transactions.find(match, {'_id': 0, 'med_name': 1, 'shortfall': 1}):
            b = out.setdefault(t.get('med_name'), {'shortfall': 0, 'events': 0})
            b['shortfall'] += t.get('shortfall', 0) or 0
            b['events'] += 1
        return out


_ZERO_DEMAND = {'shortfall': 0, 'events': 0}


ACTIVE_ONLY = {'archived': {'$ne': True}}

# NEW (Categories): items are either pharmaceuticals or medical supplies. This is
# a field on the item, NOT a second collection — deliveries, stock takes,
# write-offs, internal issues, ordering and the dashboard then work on supplies
# with no new code and no second set of bugs to keep in step.
CATEGORY_PHARM = 'Pharmaceutical'
CATEGORY_SUPPLY = 'Medical Supply'
CATEGORY_DONOR = 'Donor Funded'
CATEGORIES = [CATEGORY_PHARM, CATEGORY_SUPPLY, CATEGORY_DONOR]
CATEGORY_LABELS = {'pharmaceutical': 'Pharmaceuticals',
                   'supply': 'Medical Supplies',
                   'donor': 'Donor Funded',
                   'both': 'All Categories'}


def _category_filter(choice: str | None) -> Query:
    """Query fragment for the chosen category view.

    Items recorded before categories existed have no field at all, so
    'pharmaceutical' must match a MISSING value too — otherwise every existing
    medication would vanish from every report the moment this shipped.
    """
    if choice == 'supply':
        return {'category': CATEGORY_SUPPLY}
    if choice == 'donor':
        return {'category': CATEGORY_DONOR}
    if choice == 'both':
        return {}
    # Pharmaceuticals is expressed as "neither of the others" so that records
    # written before categories existed — which have no field at all — still
    # appear. Asking for category == 'Pharmaceutical' would hide every item
    # already in the database.
    return {'category': {'$nin': [CATEGORY_SUPPLY, CATEGORY_DONOR]}}


def _category_choice() -> str:
    c = (request.values.get('category') or 'pharmaceutical').strip()
    return c if c in ('pharmaceutical', 'supply', 'donor', 'both') else 'pharmaceutical'


def _cat_of(med: Doc) -> str:
    return med.get('category') or CATEGORY_PHARM


def _active(base: Query | None = None) -> Query:
    """Add the not-archived condition to a medications query.

    Archived items keep every transaction they ever had, so historical reports
    still reconcile — they simply stop appearing in the lists you work from.
    """
    q = dict(base or {})
    q['archived'] = {'$ne': True}
    return q


def _name_key(name: str | None) -> str:
    """Sort key for medication names.

    FIX (Bug): lists were sorted with MongoDB's default ordering, which
    compares raw bytes. Every capitalised name therefore sorted ahead of every
    lower-case one — "amoxicillin" landed after "Zinc" — and a name with a
    stray leading space jumped to the very top. Neither looks alphabetical to
    a person reading the report.
    """
    return (name or '').strip().lower()


def _by_name(docs: Iterable[Doc]) -> Docs:
    """Sort medication documents the way a human reads a list."""
    return sorted(docs, key=lambda d: _name_key(d.get('name')))



@app.route('/edit-loss', methods=['POST'])
@login_required
def edit_loss() -> ResponseReturnValue:
    """Correct the quantity or unit price on a recorded loss.

    A loss already moved stock, so changing its quantity has to move stock by
    the DIFFERENCE — not by the new figure. Writing off 60 when it should have
    been 40 means 20 units are still on the shelf and must go back; the
    reverse takes 20 more off. Overwriting the quantity without that
    adjustment would leave the balance permanently wrong while the ledger
    looked correct.

    The price is different: it never moved stock, so correcting it only
    revalues the loss.
    """
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can edit a recorded loss.')
        return redirect('/reports')

    start_date = request.form.get('start_date') or ''
    end_date = request.form.get('end_date') or ''

    def back():
        return redirect(url_for('reports', report_type='expiry_writeoffs',
                                start_date=start_date, end_date=end_date))

    try:
        db = get_mongo_client()['pharmacy_db']
        try:
            tx = db['transactions'].find_one(
                {'_id': ObjectId(request.form.get('loss_id', '')),
                 'type': {'$in': ['expiry_removal', 'damage']}})
        except (InvalidId, TypeError):
            tx = None
        if not tx:
            flash('That loss record was not found.')
            return back()

        try:
            new_qty = int(request.form.get('quantity', ''))
            new_price = float(request.form.get('price', ''))
        except (TypeError, ValueError):
            flash('Quantity must be a whole number and price a number.')
            return back()
        if new_qty < 1:
            flash('Quantity must be at least 1. To cancel a loss entirely, '
                  'reverse it by recording the stock back in.')
            return back()
        if new_price < 0:
            flash('Price cannot be negative.')
            return back()
        # Only touch remarks when the form actually carried the field; an edit
        # posted without it must not silently clear what is there.
        remarks_given = 'remarks' in request.form
        new_remarks = (request.form.get('remarks') or '').strip()

        med_name = tx.get('med_name')
        med = db['medications'].find_one({'name': med_name})
        if not med:
            flash(f'Medication "{med_name}" is no longer on file, so this loss '
                  f'cannot be edited.')
            return back()

        old_qty = abs(tx.get('quantity', 0) or 0)
        old_price = tx.get('price', 0) or 0
        delta = new_qty - old_qty          # >0 means MORE is being written off
        balance = med.get('balance', 0) or 0
        if delta > balance:
            flash(f'Cannot increase this loss to {new_qty}: that needs '
                  f'{delta} more unit(s) and only {balance} are on hand.')
            return back()

        if delta:
            db['medications'].update_one({'name': med_name},
                                         {'$inc': {'balance': -delta}})
        db['transactions'].update_one(
            {'_id': tx['_id']},
            {'$set': {'quantity': -new_qty, 'price': new_price,
                      'line_value': -new_qty * new_price,
                      'edited_by': session['user']['name'],
                      'edited_at': datetime.utcnow()}})

        changes = []
        if old_qty != new_qty:
            changes.append(f'quantity from {old_qty} to {new_qty} '
                           f'(stock on hand from {balance} to {balance - delta})')
        if abs(old_price - new_price) >= 0.0001:
            changes.append(f'unit price from R{old_price:.4f} to R{new_price:.4f}')
        old_remarks = tx.get('remarks') or tx.get('reason') or ''
        if remarks_given and new_remarks != old_remarks:
            db['transactions'].update_one({'_id': tx['_id']},
                                          {'$set': {'remarks': new_remarks}})
            changes.append('remarks updated')
        if not changes:
            flash('Nothing was changed.')
            return back()
        write_audit_entry('UPDATE', 'stock_loss',
                          f'{med_name} ({tx.get("type")})', '; '.join(changes))
        flash(f'{med_name}: loss corrected — {"; ".join(changes)}. '
              f'Value now R{new_qty * new_price:.2f}.')
    except ServerSelectionTimeoutError:
        flash('Database connection failed. Please try again later.')
    return back()


@app.route('/remove-expired', methods=['POST'])
@login_required
def remove_expired() -> ResponseReturnValue:
    """Write a stated number of expired units off the shelf.

    A quantity rather than an all-or-nothing button, because batches expire at
    different times and the balance on hand is often a mix — writing off the
    whole balance when one batch expired would destroy good stock on paper.
    """
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can remove expired stock.')
        return redirect('/reports')
    report_type = request.form.get('report_type', 'expired_list')
    try:
        db = get_mongo_client()['pharmacy_db']
        med_name = (request.form.get('med_name') or '').strip()
        try:
            quantity = int(request.form.get('quantity', ''))
        except (TypeError, ValueError):
            flash('Quantity must be a whole number.')
            return redirect(url_for('reports', report_type=report_type))
        med = db['medications'].find_one({'name': med_name})
        if not med:
            flash(f'Medication "{med_name}" not found.')
            return redirect(url_for('reports', report_type=report_type))
        balance = med.get('balance', 0) or 0
        if quantity < 1:
            flash('Quantity must be at least 1.')
            return redirect(url_for('reports', report_type=report_type))
        if quantity > balance:
            flash(f'Cannot remove {quantity} units of "{med_name}" — only {balance} on hand.')
            return redirect(url_for('reports', report_type=report_type))

        price = med.get('price', 0) or 0
        db['medications'].update_one({'name': med_name}, {'$inc': {'balance': -quantity}})
        # Its own transaction type: this is stock leaving the shelf, but it is
        # neither consumption nor a counting error, and every report that
        # back-calculates a balance has to unwind it as its own thing.
        db['transactions'].insert_one({
            'type': 'expiry_removal',
            'med_name': med_name,
            'quantity': -quantity,               # signed, like an adjustment
            'price': price, 'line_value': -quantity * price,
            'batch': med.get('batch'), 'expiry_date': med.get('expiry_date'),
            'schedule': med.get('schedule'),
            'reason': (request.form.get('reason') or '').strip(),
            'balance_before': balance,
            'user': session['user']['name'], 'timestamp': datetime.utcnow(),
        })
        write_audit_entry('UPDATE', 'expiry_removal', med_name,
                          f'{quantity} unit(s) written off as expired '
                          f'(balance {balance} -> {balance - quantity}, '
                          f'value R{quantity * price:.2f})')
        flash(f'{med_name}: {quantity} unit(s) removed as expired. '
              f'Balance is now {balance - quantity}. Value written off R{quantity * price:.2f}.')
    except ServerSelectionTimeoutError:
        flash('Database connection failed. Please try again later.')
    return redirect(url_for('reports', report_type=report_type))


@app.route('/remove-expired-bulk', methods=['POST'])
@login_required
def remove_expired_bulk() -> ResponseReturnValue:
    """Write off several expired items in one pass.

    The app already knows exactly what has expired, so clearing a month's
    worth should be one review and one button, not two interactions per item.

    Each line keeps its own quantity, pre-filled with the balance but
    editable, because batches expire at different times and writing off a
    whole balance when one batch went out of date destroys good stock on
    paper. Unticking a line leaves it alone.

    A line that cannot be written off does not stop the rest: it is skipped
    and named in the result, so one bad figure never costs the whole pass.
    """
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can remove expired stock.')
        return redirect('/reports')
    report_type = request.form.get('report_type', 'expired_list')
    reason = (request.form.get('bulk_reason') or '').strip()
    try:
        db = get_mongo_client()['pharmacy_db']
        selected = [n.strip() for n in request.form.getlist('sel') if n.strip()]
        if not selected:
            flash('Nothing was ticked, so nothing was removed.')
            return redirect(url_for('reports', report_type=report_type))

        removed, skipped, total_value, total_units = [], [], 0.0, 0
        for med_name in selected:
            raw = (request.form.get(f'qty__{med_name}') or '').strip()
            try:
                quantity = int(raw)
            except (TypeError, ValueError):
                skipped.append(f'{med_name} (quantity "{raw}" is not a number)')
                continue
            med = db['medications'].find_one({'name': med_name})
            if not med:
                skipped.append(f'{med_name} (not found)')
                continue
            balance = med.get('balance', 0) or 0
            if quantity < 1:
                skipped.append(f'{med_name} (quantity must be at least 1)')
                continue
            if quantity > balance:
                skipped.append(f'{med_name} (only {balance} on hand)')
                continue

            price = med.get('price', 0) or 0
            db['medications'].update_one({'name': med_name},
                                         {'$inc': {'balance': -quantity}})
            db['transactions'].insert_one({
                'type': 'expiry_removal',
                'med_name': med_name,
                'quantity': -quantity,
                'price': price, 'line_value': -quantity * price,
                'batch': med.get('batch'), 'expiry_date': med.get('expiry_date'),
                'schedule': med.get('schedule'),
                'reason': reason,
                'balance_before': balance,
                'user': session['user']['name'], 'timestamp': datetime.utcnow(),
            })
            write_audit_entry('UPDATE', 'expiry_removal', med_name,
                              f'{quantity} unit(s) written off as expired in a '
                              f'bulk pass (balance {balance} -> '
                              f'{balance - quantity}, '
                              f'value R{quantity * price:.2f})')
            removed.append(med_name)
            total_units += quantity
            total_value += quantity * price

        parts = []
        if removed:
            parts.append(f"{len(removed)} item{'' if len(removed) == 1 else 's'} "
                         f'written off, {total_units} unit'
                         f"{'' if total_units == 1 else 's'}, "
                         f'value R{total_value:.2f}')
        if skipped:
            parts.append(f"not removed: {'; '.join(skipped)}")
        flash('. '.join(parts) + '.' if parts else 'Nothing was removed.',
              'error' if skipped and not removed else
              ('partial' if skipped else 'success'))
    except ServerSelectionTimeoutError:
        flash('Database connection failed. Please try again later.')
    return redirect(url_for('reports', report_type=report_type))


@app.route('/correct-expiry', methods=['POST'])
@login_required
def correct_expiry() -> ResponseReturnValue:
    """Fix a wrongly recorded expiry date. Moves no stock, by design."""
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can correct expiry dates.')
        return redirect('/reports')
    report_type = request.form.get('report_type', 'expired_list')
    try:
        db = get_mongo_client()['pharmacy_db']
        med_name = (request.form.get('med_name') or '').strip()
        new_date = (request.form.get('expiry_date') or '').strip()
        try:
            datetime.strptime(new_date, '%Y-%m-%d')
        except ValueError:
            flash('Expiry date must be a valid date (YYYY-MM-DD).')
            return redirect(url_for('reports', report_type=report_type))
        med = db['medications'].find_one({'name': med_name})
        if not med:
            flash(f'Medication "{med_name}" not found.')
            return redirect(url_for('reports', report_type=report_type))
        old_date = med.get('expiry_date')
        if old_date == new_date:
            flash(f'{med_name}: expiry date is already {new_date}.')
            return redirect(url_for('reports', report_type=report_type))
        db['medications'].update_one({'name': med_name},
                                     {'$set': {'expiry_date': new_date}})
        write_audit_entry('UPDATE', 'medication', med_name,
                          f'expiry date corrected: {old_date} -> {new_date}')
        flash(f'{med_name}: expiry date corrected from {old_date} to {new_date}. '
              f'No stock was moved.')
    except ServerSelectionTimeoutError:
        flash('Database connection failed. Please try again later.')
    return redirect(url_for('reports', report_type=report_type))


@app.route('/reports', methods=['GET', 'POST'])
@login_required
def reports() -> ResponseReturnValue:
    is_admin = session['user'].get('role') == 'admin'
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']

        cat = _category_choice()
        cat_q = _category_filter(cat)
        cat_label = CATEGORY_LABELS[cat]

        report_data = []
        receive_list = []
        stock_data: Docs = []
        controlled_register = []
        order_request = None
        monthly = None
        writeoffs: Docs = []
        writeoff_totals = {'item_count': 0, 'units': 0, 'value': 0.0, 'by_med': []}
        unmet_list = []
        duplicates = []
        unmet_totals: Doc = {'item_count': 0, 'shortfall': 0, 'value': 0.0,
                            'fill_rate': None, 'by_med': []}
        issues = []
        issue_totals = {'lines': 0, 'units': 0, 'value': 0.0, 'dept_count': 0, 'by_dept': [], 'by_med': []}
        sick_leave: Docs = []
        sick_leave_totals = {'patients': 0, 'days': 0, 'average': '-', 'longest': 0, 'repeat': []}
        report_type = None
        start_date = None
        end_date = None
        total_transactions = 0
        message_class = None
        message = session.pop('message', None)
        # Also read any flashed message from a preceding redirect (e.g.
        # access-denied from add/edit/delete medication routes).
        # Honour the category the route set rather than guessing severity
        # from the wording — that read a partly-successful bulk write-off,
        # which names what it skipped, as an outright error.
        if not message:
            message_class, message = _flashed_banner()
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

        # NEW: also honour a GET with a report_type, so the expiry actions can
        # return the admin to the list they were working in rather than dumping
        # them back at the report menu after every removal.
        if request.method == 'POST' or request.args.get('report_type'):
            report_type = request.values.get('report_type')
            start_date  = request.values.get('start_date')
            end_date    = request.values.get('end_date')
            search      = request.values.get('search')

            if report_type:
                try:
                    if start_date:
                        start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    if end_date:
                        end_dt = (datetime.strptime(end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                                  + timedelta(days=1) - timedelta(seconds=1))

                    if report_type in stock_report_types:
                        # NEW: default to today rather than refusing. "What is
                        # expired?" is a question about now, and the expiry
                        # actions redirect back here without a date.
                        if not end_date:
                            end_date = datetime.utcnow().strftime('%Y-%m-%d')
                            end_dt = (datetime.strptime(end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                                      + timedelta(days=1) - timedelta(seconds=1))
                        report_date     = datetime.strptime(end_date, '%Y-%m-%d').date()
                        threshold_date  = report_date + timedelta(days=30)
                        now_dt          = datetime.now(timezone.utc)
                        med_filter      = {'name': {'$regex': search or '', '$options': 'i'}} if search else {}
                        all_meds        = _by_name(medications.find(_active({**med_filter, **cat_q}), {'_id': 0}))
                        stock_data = []
                        # FIX (Performance): one aggregation for every medication
                        # instead of one per medication. Scoped by name only when a
                        # search has narrowed the set, so the $in list stays small.
                        movement = _movement_by_med(
                            transactions, {'$gt': end_dt, '$lte': now_dt},
                            [m['name'] for m in all_meds] if search else None)
                        for med in all_meds:
                            med_name = med['name']
                            current_balance = med.get('balance', 0)
                            # NEW (Stock Take): 'adjustment' transactions are
                            # signed and must be unwound too, or a count taken
                            # after the report date would distort the balance
                            # reported as at that date.
                            mv = movement.get(med_name, _ZERO_MOVEMENT)
                            balance_at_date = max(0, current_balance - mv['received']
                                                  + mv['dispensed'] + mv['issued']
                                                  - mv['adjusted'] - mv['expired']
                                                  - mv['damaged'])

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
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        med_filter   = {'name': {'$regex': search or '', '$options': 'i'}} if search else {}
                        # FIX (Bug): the .limit(300) that used to be here silently
                        # dropped every medication past the 300th from the report —
                        # no message, no indication that anything was missing. It
                        # only existed to bound the per-medication query loop below,
                        # which no longer exists, so the cap goes with it.
                        meds         = _by_name(medications.find(_active({**med_filter, **cat_q}),
                                                 {'_id': 0, 'name': 1, 'balance': 1, 'category': 1}))
                        days_in_period = max(1, (end_dt.date() - start_dt.date()).days + 1)
                        # FIX (Performance): one aggregation for the whole report
                        # instead of one per medication.
                        movement = _movement_by_med(
                            transactions, {'$gte': start_dt, '$lte': end_dt},
                            [m['name'] for m in meds] if search else None)
                        demand = _demand_by_med(
                            transactions, {'$gte': start_dt, '$lte': end_dt},
                            [m['name'] for m in meds] if search else None)

                        def _tidy(v):
                            return int(v) if isinstance(v, float) and v.is_integer() else round(v, 2)

                        for med in meds:
                            med_name = med['name']
                            mv = movement.get(med_name, _ZERO_MOVEMENT)
                            dispensed = mv['dispensed']
                            received  = mv['received']
                            # NEW (Stock Take): signed net of every physical-count
                            # correction posted in the period. Unwinding it here is
                            # what keeps the row internally consistent:
                            #   Beginning + Received - Dispensed + Adjustment = Current
                            adjustment        = mv['adjusted']
                            # NEW (Expiry): written-off stock is its own movement —
                            # neither consumption nor a counting error — so it is
                            # unwound separately or the beginning balance is wrong
                            # for every period containing a write-off.
                            expired           = mv['expired']
                            # NEW: stock issued to a department is CONSUMED — it
                            # left the store and has to be replaced — whereas
                            # damage is LOST. Both reduce stock, only one is
                            # demand, so they are tracked separately.
                            issued            = mv['issued']
                            damaged           = mv['damaged']
                            current_balance   = med.get('balance', 0)
                            beginning_balance = max(0, current_balance - received + dispensed
                                                    + issued - adjustment - expired - damaged)
                            # Consumption is everything issued out of the store,
                            # to a patient or to a department. Excluding internal
                            # issues would understate demand for exactly the items
                            # the wards rely on most.
                            consumption       = dispensed + issued
                            avg_daily         = consumption / days_in_period
                            # AMC (Average Monthly Consumption): consumption in the
                            # period scaled to a 30-day month, so the figure stays
                            # comparable whatever period length is selected.
                            amc               = avg_daily * 30
                            # NEW (Unmet demand): true demand is what was asked
                            # for, not what happened to be on the shelf. AMC is
                            # left as consumption — the recognised figure — and
                            # the demand-based version sits beside it, so the
                            # gap between the two is visible rather than buried.
                            unmet             = demand.get(med_name, _ZERO_DEMAND)['shortfall']
                            adjusted_amc      = (consumption + unmet) / days_in_period * 30
                            lead_time_stock   = avg_daily * 60
                            amount_to_order   = max(0, amc - current_balance + lead_time_stock)

                            report_data.append({
                                'med_name': med_name, 'category': _cat_of(med),
                                'beginning_balance': beginning_balance,
                                'dispensed': dispensed, 'received': received,
                                'adjustment': adjustment, 'expired': expired,
                                'issued': issued, 'damaged': damaged,
                                # NEW: non-admins don't see the Adjustment column,
                                # so their Dispensed figure has to absorb it or the
                                # row won't add up. Folding it in here isn't a fudge
                                # — it is literally what the two discrepancy types
                                # mean. A shortfall (negative adjustment) is stock
                                # that WAS issued but never recorded, so it belongs
                                # in Dispensed. A surplus (positive adjustment) is
                                # an issue that was recorded but never happened, so
                                # it comes back out of Dispensed. Beginning balance
                                # stays truthful either way, and
                                #   Beginning + Received - Dispensed = Current
                                # holds for the non-admin view.
                                'dispensed_effective': dispensed - adjustment - expired - damaged,
                                'current_balance': current_balance,
                                'amc': _tidy(amc),
                                'unmet': unmet, 'adjusted_amc': _tidy(adjusted_amc),
                                'fill_rate': (round(consumption / (consumption + unmet) * 100, 1)
                                              if (consumption + unmet) else None),
                                'amount_to_order': _tidy(amount_to_order)
                            })

                    elif report_type == 'receive_list':
                        base_query: Query = {'type': 'receive'}
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

                    elif report_type == 'sick_leave':
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        q: Query = {'type': 'dispense', 'outcome': 'Sick leave',
                                    'timestamp': {'$gte': start_dt, '$lte': end_dt}}
                        if search:
                            q['$or'] = [
                                {'patient':    {'$regex': search, '$options': 'i'}},
                                {'company':    {'$regex': search, '$options': 'i'}},
                                {'prescriber': {'$regex': search, '$options': 'i'}},
                                {'diagnoses':  {'$regex': search, '$options': 'i'}},
                            ]
                        raw = list(transactions.find(q).sort('timestamp', -1).limit(3000))
                        # One line per VISIT. A dispense writes one document per
                        # medication, so listing rows would print the same
                        # certificate once for every item on the prescription.
                        seen, sick_leave = set(), []
                        per_patient: dict[str, Doc] = {}
                        for t in raw:
                            tid = t.get('transaction_id')
                            if tid in seen:
                                continue
                            seen.add(tid)
                            days = t.get('outcome_days') or 0
                            dx = t.get('diagnoses')
                            sick_leave.append({
                                'date': t.get('date') or t['timestamp'].strftime('%Y-%m-%d'),
                                'patient': t.get('patient', ''),
                                'company': t.get('company'),
                                'days': days,
                                'diagnoses': ', '.join(dx) if isinstance(dx, list) else (dx or ''),
                                'prescriber': t.get('prescriber'),
                                'dispenser': t.get('dispenser'),
                            })
                            entry = per_patient.setdefault(t.get('patient', ''),
                                                       {'count': 0, 'days': 0})
                            entry['count'] += 1
                            entry['days'] += days
                        total_days = sum(l['days'] for l in sick_leave)
                        sick_leave_totals = {
                            'patients': len(per_patient),
                            'days': total_days,
                            'average': (f'{total_days / len(sick_leave):.1f}'
                                        if sick_leave else '-'),
                            'longest': max((l['days'] for l in sick_leave), default=0),
                            'repeat': sorted(
                                ({'patient': k, **v} for k, v in per_patient.items()
                                 if v['count'] > 1),
                                key=lambda r: -r['days']),
                        }
                        report_title = 'Sick Leave Register'

                    elif report_type == 'internal_issues':
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        q = {'type': 'internal_issue',
                             'timestamp': {'$gte': start_dt, '$lte': end_dt}}
                        if search:
                            q['$or'] = [
                                {'med_name':   {'$regex': search, '$options': 'i'}},
                                {'department': {'$regex': search, '$options': 'i'}},
                                {'issued_to':  {'$regex': search, '$options': 'i'}},
                                {'user':       {'$regex': search, '$options': 'i'}},
                            ]
                        issues = list(transactions.find(q).sort('timestamp', -1).limit(2000))
                        by_dept: dict[str, Doc] = {}
                        by_med: dict[str, Doc] = {}
                        for i_ in issues:
                            v = i_.get('line_value')
                            if v is None:
                                v = (i_.get('quantity', 0) or 0) * (i_.get('price', 0) or 0)
                            dept = by_dept.setdefault(i_.get('department') or 'Unspecified',
                                                   {'lines': 0, 'units': 0, 'value': 0.0})
                            dept['lines'] += 1
                            dept['units'] += i_.get('quantity', 0) or 0
                            dept['value'] += v
                            m_ = by_med.setdefault(i_.get('med_name', ''),
                                                   {'lines': 0, 'units': 0, 'value': 0.0})
                            m_['lines'] += 1
                            m_['units'] += i_.get('quantity', 0) or 0
                            m_['value'] += v
                        issue_totals = {
                            'lines': len(issues),
                            'units': sum(v['units'] for v in by_dept.values()),
                            'value': sum(v['value'] for v in by_dept.values()),
                            'dept_count': len(by_dept),
                            'by_dept': [{'name': k, **v} for k, v in
                                        sorted(by_dept.items(), key=lambda kv: -kv[1]['value'])],
                            'by_med': [{'name': k, **v} for k, v in
                                       sorted(by_med.items(), key=lambda kv: -kv[1]['value'])][:20],
                        }
                        report_title = 'Internal Issues'

                    elif report_type == 'duplicate_visits':
                        # No date range: this is a hunt through the record for
                        # something that should not be there, not a report on
                        # a period. It looks back six months by default.
                        duplicates = _duplicate_visits(transactions)
                        if search:
                            sl = search.lower()
                            duplicates = [d for d in duplicates
                                          if sl in (d['patient'] or '').lower()
                                          or sl in (d['med_name'] or '').lower()]
                        report_title = 'Possible double-dispensing'

                    elif report_type == 'unmet_demand':
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        q = {'type': 'unmet_demand',
                             'timestamp': {'$gte': start_dt, '$lte': end_dt}}
                        if search:
                            q['$or'] = [
                                {'med_name':   {'$regex': search, '$options': 'i'}},
                                {'patient':    {'$regex': search, '$options': 'i'}},
                                {'prescriber': {'$regex': search, '$options': 'i'}},
                                {'user':       {'$regex': search, '$options': 'i'}},
                            ]
                        unmet_list = list(transactions.find(q).sort('timestamp', -1).limit(2000))
                        disp = _movement_by_med(transactions, {'$gte': start_dt, '$lte': end_dt})
                        by_med = {}
                        for u in unmet_list:
                            entry = by_med.setdefault(u.get('med_name', ''),
                                                  {'count': 0, 'shortfall': 0, 'value': 0.0})
                            entry['count'] += 1
                            entry['shortfall'] += u.get('shortfall', 0) or 0
                            entry['value'] += u.get('shortfall_value', 0) or 0
                        rows_by_med = []
                        for k, v in sorted(by_med.items(), key=lambda kv: -kv[1]['shortfall']):
                            d = disp.get(k, _ZERO_MOVEMENT)['dispensed']
                            total = d + v['shortfall']
                            rows_by_med.append({'med_name': k, **v, 'dispensed': d,
                                                'fill_rate': round(d / total * 100, 1) if total else None})
                        tot_short = sum(v['shortfall'] for v in by_med.values())
                        tot_disp = sum(r['dispensed'] for r in rows_by_med)
                        unmet_totals = {
                            'item_count': len(by_med),
                            'shortfall': tot_short,
                            'value': sum(v['value'] for v in by_med.values()),
                            'fill_rate': (round(tot_disp / (tot_disp + tot_short) * 100, 1)
                                          if (tot_disp + tot_short) else None),
                            'by_med': rows_by_med,
                        }
                        report_title = 'Unmet Demand'

                    elif report_type == 'expiry_writeoffs':
                        # NEW: the record of what was actually removed, as
                        # opposed to expired_list, which is a work list of what
                        # still needs removing. Once stock is written off it
                        # leaves that list entirely, so without this report the
                        # loss would only survive as an audit-log line.
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        q = {'type': {'$in': ['expiry_removal', 'damage']},
                             'timestamp': {'$gte': start_dt, '$lte': end_dt}}
                        if search:
                            q['$or'] = [
                                {'med_name': {'$regex': search, '$options': 'i'}},
                                {'reason':   {'$regex': search, '$options': 'i'}},
                                {'user':     {'$regex': search, '$options': 'i'}},
                                {'batch':    {'$regex': search, '$options': 'i'}},
                            ]
                        raw = list(transactions.find(q).sort('timestamp', -1).limit(2000))
                        # Records written before the expiry date was captured on
                        # damage fall back to the item's recorded expiry.
                        med_expiry = {m['name']: m.get('expiry_date') for m in
                                      medications.find({}, {'_id': 0, 'name': 1,
                                                            'expiry_date': 1})}
                        writeoffs, by_med = [], {}
                        for w in raw:
                            units = abs(w.get('quantity', 0) or 0)
                            value = abs(w.get('line_value') if w.get('line_value') is not None
                                        else units * (w.get('price', 0) or 0))
                            writeoffs.append({
                                'timestamp': w.get('timestamp'), 'med_name': w.get('med_name'),
                                'units': units, 'price': w.get('price', 0) or 0,
                                'value': value, 'batch': w.get('batch'),
                                'expiry_date': (w.get('expiry_date')
                                                or med_expiry.get(w.get('med_name'))),
                                'reason': w.get('reason'),
                                # Free text the admin writes. Falls back to the
                                # recorded reason so reformatting to the register
                                # columns drops nothing already captured.
                                'remarks': w.get('remarks') or w.get('reason') or '',
                                'id': str(w.get('_id')),
                                'kind': 'Damage' if w.get('type') == 'damage' else 'Expiry',
                                'user': w.get('user'),
                            })
                            entry = by_med.setdefault(w.get('med_name', ''),
                                                  {'count': 0, 'units': 0, 'value': 0.0})
                            entry['count'] += 1
                            entry['units'] += units
                            entry['value'] += value
                        writeoff_totals = {
                            # NOT 'items': Jinja resolves attribute access before
                            # subscript, so writeoff_totals.items would render the
                            # dict's .items METHOD instead of this value.
                            'item_count': len(by_med),
                            'units': sum(w['units'] for w in writeoffs),
                            'value': sum(w['value'] for w in writeoffs),
                            'by_med': [{'med_name': k, **v} for k, v in
                                       sorted(by_med.items(), key=lambda kv: -kv[1]['value'])],
                        }
                        report_title = 'Stock Losses'

                    elif report_type == 'monthly_report':
                        # NEW (Costing): a month at cost. Everything is valued
                        # from the price stored ON each transaction, so the
                        # report is reproducible — re-running it after a price
                        # change gives the same answer, which a lookup against
                        # today's price would not.
                        if not start_dt:
                            raise ValueError('A start date is required; the report covers the period containing it.')
                        # NEW: the same report over any period. The start date
                        # picks a point and the whole surrounding period is
                        # covered, so nobody has to work out where a week or a
                        # quarter begins.
                        period_kind = request.values.get('period', 'month')
                        if period_kind not in ('day', 'week', 'month', 'quarter', 'year'):
                            period_kind = 'month'
                        anchor = start_dt
                        if period_kind == 'day':
                            m_start = datetime(anchor.year, anchor.month, anchor.day)
                            m_end = m_start + timedelta(days=1) - timedelta(seconds=1)
                            label = m_start.strftime('%A, %d %B %Y')
                        elif period_kind == 'week':
                            wd = datetime(anchor.year, anchor.month, anchor.day)
                            m_start = wd - timedelta(days=wd.weekday())      # Monday
                            m_end = m_start + timedelta(days=7) - timedelta(seconds=1)
                            label = (f"Week of {m_start.strftime('%d %b')} to "
                                     f"{(m_end).strftime('%d %b %Y')}")
                        elif period_kind == 'quarter':
                            quarter = (anchor.month - 1) // 3
                            m_start = datetime(anchor.year, quarter * 3 + 1, 1)
                            m_end = (datetime(anchor.year + (quarter == 3), 1 if quarter == 3 else quarter * 3 + 4, 1)
                                     - timedelta(seconds=1))
                            label = f'Q{quarter + 1} {anchor.year}'
                        elif period_kind == 'year':
                            m_start = datetime(anchor.year, 1, 1)
                            m_end = datetime(anchor.year + 1, 1, 1) - timedelta(seconds=1)
                            label = str(anchor.year)
                        else:
                            m_start = datetime(anchor.year, anchor.month, 1)
                            m_end = (datetime(m_start.year + (m_start.month == 12),
                                              1 if m_start.month == 12 else m_start.month + 1, 1)
                                     - timedelta(seconds=1))
                            label = m_start.strftime('%B %Y')
                        period = {'$gte': m_start, '$lte': m_end}

                        lines = list(transactions.find(
                            {'timestamp': period,
                             'type': {'$in': ['dispense', 'receive', 'adjustment',
                                              'opening_balance', 'expiry_removal',
                                              'internal_issue', 'damage']}}))
                        cur_price = {m['name']: (m.get('price', 0) or 0)
                                     for m in medications.find({}, {'_id': 0, 'name': 1, 'price': 1})}

                        def valued(l):
                            """Prefer the price frozen on the transaction."""
                            if l.get('line_value') is not None:
                                return l['line_value'], True
                            if l.get('price'):
                                return (l.get('quantity', 0) or 0) * l['price'], True
                            return (l.get('quantity', 0) or 0) * cur_price.get(l.get('med_name'), 0), False

                        dispensed_value = received_value = opening_load_value = 0.0
                        adjustment_value = expired_value = 0.0
                        issued_value = damaged_value = 0.0
                        issued_lines = damaged_lines = 0
                        by_dept = {}
                        items_dispensed = receipt_lines = adjustment_lines = 0
                        expired_lines = 0
                        estimated_lines = 0
                        visits, patients = set(), set()
                        per_item: dict[str, Doc] = {}
                        for l in lines:
                            v, exact = valued(l)
                            t = l.get('type')
                            if t == 'dispense':
                                dispensed_value += v
                                items_dispensed += 1
                                if not exact:
                                    estimated_lines += 1
                                if l.get('transaction_id'):
                                    visits.add(l['transaction_id'])
                                if l.get('patient'):
                                    patients.add(l['patient'])
                                entry = per_item.setdefault(l.get('med_name', ''), {'units': 0, 'value': 0.0})
                                entry['units'] += l.get('quantity', 0) or 0
                                entry['value'] += v
                            elif t == 'receive':
                                received_value += v
                                receipt_lines += 1
                            elif t == 'opening_balance':
                                opening_load_value += v
                            elif t == 'expiry_removal':
                                expired_value += v
                                expired_lines += 1
                            elif t == 'internal_issue':
                                issued_value += v
                                issued_lines += 1
                                dept = by_dept.setdefault(l.get('department', 'Unspecified'),
                                                       {'lines': 0, 'value': 0.0})
                                dept['lines'] += 1
                                dept['value'] += v
                            elif t == 'damage':
                                damaged_value += v
                                damaged_lines += 1
                            else:
                                adjustment_value += v
                                adjustment_lines += 1

                        # Deliveries and orders, counted by the month the invoice landed.
                        month_dels = list(db['deliveries'].find({'opened_at': period}))
                        invoice_total = sum(d.get('invoice_total', 0) or 0 for d in month_dels)
                        orders: dict[Any, float] = {}
                        for d in month_dels:
                            orders.setdefault(d.get('order_number'), d.get('order_total', 0) or 0)

                        # Closing stock at today's prices; opening derived from it.
                        closing_value = sum((m.get('balance', 0) or 0) * (m.get('price', 0) or 0)
                                            for m in medications.find({}, {'_id': 0, 'balance': 1, 'price': 1}))
                        after = _movement_by_med(transactions, {'$gt': m_end, '$lte': datetime.utcnow()})
                        opening_value = closing_value
                        for m in medications.find({}, {'_id': 0, 'name': 1, 'balance': 1, 'price': 1}):
                            mv = after.get(m['name'], _ZERO_MOVEMENT)
                            p = m.get('price', 0) or 0
                            bal_at_end = max(0, (m.get('balance', 0) or 0)
                                             - mv['received'] + mv['dispensed'] - mv['adjusted'])
                            opening_value += (bal_at_end - (m.get('balance', 0) or 0)) * p
                        # roll back through this month's own movements too
                        month_mv = _movement_by_med(transactions, period)
                        for m in medications.find({}, {'_id': 0, 'name': 1, 'price': 1}):
                            mv = month_mv.get(m['name'], _ZERO_MOVEMENT)
                            p = m.get('price', 0) or 0
                            opening_value -= (mv['received'] - mv['dispensed'] + mv['adjusted']) * p
                        opening_value = max(0.0, opening_value)

                        top = sorted(per_item.items(), key=lambda kv: -kv[1]['value'])[:15]
                        monthly = {
                            'label': label, 'period_kind': period_kind,
                            'starts': m_start.strftime('%Y-%m-%d'),
                            'ends': m_end.strftime('%Y-%m-%d'),
                            'opening_value': opening_value,
                            'closing_value': closing_value,
                            'received_value': received_value,
                            'opening_load_value': opening_load_value,
                            'dispensed_value': dispensed_value,
                            'adjustment_value': adjustment_value,
                            'adjustment_lines': adjustment_lines,
                            'expired_value': expired_value,
                            'expired_lines': expired_lines,
                            'issued_value': issued_value, 'issued_lines': issued_lines,
                            'damaged_value': damaged_value, 'damaged_lines': damaged_lines,
                            'by_dept': [{'name': k, **v} for k, v in
                                        sorted(by_dept.items(), key=lambda kv: -kv[1]['value'])],
                            'receipt_lines': receipt_lines,
                            'delivery_count': len(month_dels),
                            'invoice_count': len(month_dels),
                            'invoice_total': invoice_total,
                            'order_count': len(orders),
                            'order_total': sum(orders.values()),
                            'invoice_gap': invoice_total - received_value,
                            'visits': len(visits), 'patients': len(patients),
                            'items_dispensed': items_dispensed,
                            'items_per_visit': f'{items_dispensed / len(visits):.1f}' if visits else '-',
                            'cost_per_visit': dispensed_value / len(visits) if visits else 0.0,
                            # Cost per PATIENT is not cost per visit: a patient who
                            # attends three times in the period is one patient and
                            # three visits, so the two answer different questions —
                            # what a visit costs to serve, versus what a person
                            # costs to care for over the period.
                            'cost_per_patient': dispensed_value / len(patients) if patients else 0.0,
                            'visits_per_patient': (f'{len(visits) / len(patients):.1f}'
                                                   if patients else '-'),
                            'cost_per_item': dispensed_value / items_dispensed if items_dispensed else 0.0,
                            'estimated_lines': estimated_lines,
                            'top_items': [
                                {'med_name': k, 'units': v['units'], 'value': v['value'],
                                 'share': round(v['value'] / dispensed_value * 100, 1)
                                 if dispensed_value else 0}
                                for k, v in top],
                        }
                        report_title = f'Period Report — {label}'

                    elif report_type == 'order_request':
                        # NEW (Order Request): turns "amount to order" into an
                        # actual requisition. Two things make it usable rather
                        # than merely arithmetic:
                        #   1. quantities round UP to whole packs, because that
                        #      is how the supplier sells — asking for 137
                        #      tablets when they come in boxes of 100 is not an
                        #      order anyone can fill;
                        #   2. items with no recorded consumption are left out.
                        #      Without an AMC there is no basis for a quantity,
                        #      and inventing one is worse than leaving the call
                        #      to the pharmacist.
                        try:
                            cover_months = int(request.form.get('cover_months', 3))
                        except (TypeError, ValueError):
                            cover_months = 3
                        if cover_months not in (2, 3, 4, 6):
                            cover_months = 3

                        # AMC over the last 90 days, matching the basis the
                        # inventory report and dashboard already use.
                        window_end   = datetime.utcnow()
                        window_start = window_end - timedelta(days=90)
                        req_filter = ({'name': {'$regex': search or '', '$options': 'i'}}
                                      if search else {})
                        req_meds  = _by_name(medications.find(_active({**req_filter, **cat_q}), {'_id': 0}))
                        movement  = _movement_by_med(
                            transactions, {'$gte': window_start, '$lte': window_end},
                            [m['name'] for m in req_meds] if search else None)
                        # Ordering is precisely where demand matters more than
                        # consumption: an item that ran out dispensed less than
                        # was asked for, and ordering to that lower figure is how
                        # a stockout becomes permanent.
                        demand    = _demand_by_med(
                            transactions, {'$gte': window_start, '$lte': window_end},
                            [m['name'] for m in req_meds] if search else None)

                        by_supplier: dict[str, Docs] = {}
                        unpriced, unsized = 0, 0
                        for med in req_meds:
                            name    = med.get('name', '')
                            mvd      = movement.get(name, _ZERO_MOVEMENT)
                            consumed = mvd['dispensed'] + mvd['issued']
                            unmet    = demand.get(name, _ZERO_DEMAND)['shortfall']
                            plain_amc = consumed / 3.0         # 90 days -> per month
                            amc      = (consumed + unmet) / 3.0
                            if amc <= 0:
                                continue
                            balance = med.get('balance', 0) or 0
                            mos     = balance / amc
                            if mos >= cover_months:
                                continue
                            shortfall = amc * cover_months - balance
                            # An item whose pack size has never been recorded is
                            # NOT the same as one that genuinely comes in singles.
                            # Treating them alike produced a plausible-looking
                            # request ("order 300 packs") that a supplier could
                            # not fill, with nothing on the page to show it was a
                            # guess. Quantities still fall back to singles, but
                            # the row says so.
                            pack_known = med.get('pack_size') is not None
                            pack_size  = med.get('pack_size') or 1
                            packs     = int(-(-shortfall // pack_size))   # ceiling
                            units     = packs * pack_size
                            pack_price = med.get('pack_price')
                            if pack_price is None:
                                unpriced += 1
                            if not pack_known:
                                unsized += 1
                            line_cost = packs * (pack_price or 0)
                            supplier  = med.get('supplier') or 'Supplier not recorded'
                            by_supplier.setdefault(supplier, []).append({
                                'med_name': name, 'amc': round(amc, 1),
                                'plain_amc': round(plain_amc, 1),
                                # expressed per month, like the AMC figures it
                                # sits between — a 90-day total in that row would
                                # have read as a monthly rate
                                'unmet': round(unmet / 3.0, 1), 'unmet_units': unmet,
                                'balance': balance,
                                'mos_label': 'Out of stock' if balance == 0 else f'{mos:.1f}',
                                'shortfall': int(round(shortfall)),
                                'pack_size': pack_size, 'pack_known': pack_known,
                                'packs': packs, 'units': units,
                                'pack_price': pack_price, 'line_cost': line_cost,
                                'status': ('expired' if balance == 0 or mos < 1
                                           else ('close-to-expire' if mos < 2 else 'normal')),
                            })

                        groups = [{'supplier': sup,
                                   'lines': sorted(lines, key=lambda l: _name_key(l['med_name'])),
                                   'total': sum(l['line_cost'] for l in lines)}
                                  for sup, lines in sorted(by_supplier.items(),
                                                           key=lambda kv: kv[0].lower())]
                        order_request = {
                            'reference': f"REQ-{datetime.utcnow().strftime('%Y%m%d')}-{uuid4().hex[:4].upper()}",
                            'raised_on': datetime.utcnow().strftime('%Y-%m-%d'),
                            'raised_by': session['user']['name'],
                            'cover_months': cover_months,
                            'groups': groups,
                            'line_count': sum(len(g['lines']) for g in groups),
                            'total_packs': sum(l['packs'] for g in groups for l in g['lines']),
                            'grand_total': sum(g['total'] for g in groups),
                            'unpriced': unpriced,
                            'unsized': unsized,
                            'demand_adjusted': sum(1 for g in by_supplier.values()
                                                   for l in g if l['unmet_units']),
                        }
                        report_title = 'Order Request'

                    elif report_type == 'controlled_drug_register':
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        # FIX (Performance): fetch the controlled medications once
                        # and index them by name, instead of a find_one per drug
                        # inside the loop below. This was the last query loop in
                        # the app; the arithmetic is untouched.
                        controlled_docs = {m['name']: m for m in
                                           medications.find({'schedule': 'controlled'}, {'_id': 0})}
                        controlled_meds = list(controlled_docs)
                        if controlled_meds:
                            # NEW (Stock Take): adjustments are included so the
                            # register's running balance still lands on the
                            # current balance after a physical count.
                            all_tx = list(transactions.find({
                                'med_name': {'$in': controlled_meds},
                                'type': {'$in': ['receive', 'dispense', 'adjustment',
                                                 'opening_balance', 'expiry_removal',
                                                 'internal_issue', 'damage']},
                                'timestamp': {'$gte': start_dt, '$lte': end_dt}
                            }).sort('timestamp', 1).limit(10000))

                            tx_by_med = defaultdict(list)
                            for tx in all_tx:
                                tx_by_med[tx['med_name']].append(tx)

                            for med_name in sorted(controlled_meds, key=_name_key):
                                found = controlled_docs.get(med_name)
                                if not found:
                                    continue
                                med = found
                                try:
                                    current_balance    = med.get('balance', 0)
                                    med_txs            = tx_by_med[med_name]
                                    # Every movement, by its own direction —
                                    # issues to a department and write-offs
                                    # included. Leaving those out is what let
                                    # the register disagree with the shelf.
                                    deltas             = [_controlled_delta(t) for t in med_txs]
                                    received_in_period = sum(d for d in deltas if d > 0)
                                    issued_in_period   = sum(-d for d in deltas if d < 0)
                                    # Worked backwards from the shelf, so the
                                    # register and the balance cannot disagree.
                                    beginning_balance  = current_balance - sum(deltas)
                                    running_bal        = beginning_balance
                                    running_entries    = []
                                    for tx, delta in zip(med_txs, deltas):
                                        running_bal += delta
                                        tx_copy = tx.copy()
                                        tx_copy['balance_after'] = running_bal
                                        tx_copy['movement'] = CONTROLLED_MOVEMENT_LABEL.get(
                                            tx.get('type'), tx.get('type', ''))
                                        # Shown in an In or an Out column, so
                                        # the figure itself never needs a
                                        # minus sign to be understood.
                                        tx_copy['in_qty'] = delta if delta > 0 else None
                                        tx_copy['out_qty'] = -delta if delta < 0 else None
                                        running_entries.append(tx_copy)
                                    controlled_register.append({
                                        'med_name': med_name,
                                        'beginning_balance': beginning_balance,
                                        'ending_balance': current_balance,
                                        'received': received_in_period,
                                        'dispensed': issued_in_period,
                                        # The opening balance is worked
                                        # backwards from the shelf, so the
                                        # column always adds up — what it
                                        # cannot do is start below zero. That
                                        # means more left the cupboard than
                                        # could ever have been in it, which
                                        # is a movement recorded outside this
                                        # period or a balance edited directly.
                                        'reconciles': beginning_balance >= 0,
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
                    order_request = None
                    monthly = None
                    writeoffs = []
                    writeoff_totals = {'item_count': 0, 'units': 0, 'value': 0.0, 'by_med': []}
                    unmet_list = []
                    duplicates = []
                    unmet_totals = {'item_count': 0, 'shortfall': 0, 'value': 0.0, 'fill_rate': None, 'by_med': []}
                    issues = []
                    issue_totals = {'lines': 0, 'units': 0, 'value': 0.0, 'dept_count': 0, 'by_dept': [], 'by_med': []}
                    sick_leave = []
                    sick_leave_totals = {'patients': 0, 'days': 0, 'average': '-', 'longest': 0, 'repeat': []}
            else:
                message = 'Please select a report type.'

        # The download is the report on screen, built from exactly the same
        # data — never a second query that could disagree with it.
        if request.form.get('export') == 'csv':
            made = _report_csv(report_type, {
                'report_data': report_data, 'stock_data': stock_data,
                'receive_list': receive_list, 'order_request': order_request,
                'controlled_register': controlled_register, 'monthly': monthly,
                'writeoffs': writeoffs, 'unmet_list': unmet_list,
                'issues': issues, 'sick_leave': sick_leave,
                'duplicates': duplicates,
            }, is_admin=is_admin)
            if made:
                filename, body = made
                resp = make_response(body)
                resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
                resp.headers['Content-Disposition'] = \
                    f'attachment; filename="{filename}"'
                return resp
            flash('That report has no table to download.')

        return render_template(
            'reports.html',
            report_type=report_type, report_data=report_data,
            receive_list=receive_list, stock_data=stock_data,
            controlled_register=controlled_register, order_request=order_request,
            category=cat, category_label=cat_label,
            monthly=monthly, writeoffs=writeoffs, writeoff_totals=writeoff_totals,
            unmet_list=unmet_list, unmet_totals=unmet_totals,
            duplicates=duplicates,
            duplicate_window=DUPLICATE_WINDOW_MINUTES,
            issues=issues, issue_totals=issue_totals,
            sick_leave=sick_leave, sick_leave_totals=sick_leave_totals,
            start_date=start_date, end_date=end_date,
            default_start=local_now().replace(day=1).strftime('%Y-%m-%d'),
            default_end=local_now().strftime('%Y-%m-%d'),
            total_transactions=total_transactions,
            nav_links=get_nav_links(), message=message,
            message_class=message_class, current_user=session['user']['name'],
            search=search, report_title=report_title, is_admin=is_admin
        )
    except ServerSelectionTimeoutError:
        return render_template(
            'reports.html', nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            report_type=None, report_data=[], receive_list=[], stock_data=[],
            message_class='error',
            current_user=session.get('user', {}).get('name', ''),
            default_start=local_now().replace(day=1).strftime('%Y-%m-%d'),
            default_end=local_now().strftime('%Y-%m-%d'),
            controlled_register=[], order_request=None, monthly=None,
            category='pharmaceutical', category_label=CATEGORY_LABELS['pharmaceutical'],
            writeoffs=[], writeoff_totals={'item_count': 0, 'units': 0, 'value': 0.0, 'by_med': []},
            unmet_list=[], unmet_totals={'item_count': 0, 'shortfall': 0, 'value': 0.0, 'fill_rate': None, 'by_med': []},
            issues=[], issue_totals={'lines': 0, 'units': 0, 'value': 0.0, 'dept_count': 0, 'by_dept': [], 'by_med': []},
            sick_leave=[], sick_leave_totals={'patients': 0, 'days': 0, 'average': '-', 'longest': 0, 'repeat': []},
            start_date=None, end_date=None,
            total_transactions=0, search=None, report_title=None,
            duplicates=[], duplicate_window=DUPLICATE_WINDOW_MINUTES,
            is_admin=is_admin
        ), 500


# NEW: admin-only audit trail. Gives admins visibility into every edit/delete
# (from audit_logger.py's audit_log collection) and every unhandled exception
# (from error_logger.py's error_logs collection) across the whole app.
# -----------------------------------------------------------------------
# NEW (Dashboard): consumption trend and stock-health overview.
#
# PERFORMANCE NOTE: the reports() route queries transactions once per
# medication in a Python loop, which is fine for one report but would be
# ruinous here — this page needs every medication across several months at
# once. Instead the whole window is fetched in ONE aggregation grouped by
# medication and month, and everything else is computed in memory. Two DB
# round trips total (three for admins), regardless of how many medications
# exist.
# -----------------------------------------------------------------------
DASHBOARD_MONTH_CHOICES = [3, 6, 12]
MONTH_ABBR = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _month_sequence(n: int) -> list[tuple[int, int]]:
    """The last n calendar months as (year, month), oldest first."""
    now = datetime.utcnow()
    y, m = now.year, now.month
    seq = []
    for _ in range(n):
        seq.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    seq.reverse()
    return seq


def _monthly_movement(transactions: Collection, window_start: datetime) -> dict[str, dict[tuple[int, int], Doc]]:
    """med_name -> (year, month) -> {'dispensed','received','adjusted'}."""
    movement: defaultdict[str, dict[tuple[int, int], Doc]] = defaultdict(dict)
    pipeline = [
        {'$match': {'timestamp': {'$gte': window_start},
                    'type': {'$in': ['dispense', 'receive', 'adjustment', 'opening_balance', 'expiry_removal',
                       'internal_issue', 'damage']}}},
        {'$group': {
            '_id': {'med': '$med_name',
                    'y': {'$year': '$timestamp'},
                    'm': {'$month': '$timestamp'}},
            'dispensed': {'$sum': {'$cond': [{'$eq': ['$type', 'dispense']}, '$quantity', 0]}},
            'received':  {'$sum': {'$cond': [{'$in': ['$type', ['receive', 'opening_balance']]},
                                             '$quantity', 0]}},
            'adjusted':  {'$sum': {'$cond': [{'$eq': ['$type', 'adjustment']}, '$quantity', 0]}},
            'expired':   {'$sum': {'$cond': [{'$eq': ['$type', 'expiry_removal']}, '$quantity', 0]}},
            'issued':    {'$sum': {'$cond': [{'$eq': ['$type', 'internal_issue']}, '$quantity', 0]}},
            'damaged':   {'$sum': {'$cond': [{'$eq': ['$type', 'damage']}, '$quantity', 0]}},
        }}
    ]
    try:
        for row in transactions.aggregate(pipeline):
            k = row['_id']
            movement[k['med']][(k['y'], k['m'])] = {
                'dispensed': row.get('dispensed', 0) or 0,
                'received':  row.get('received', 0) or 0,
                'adjusted':  row.get('adjusted', 0) or 0,
                'expired':   row.get('expired', 0) or 0,
                'issued':    row.get('issued', 0) or 0,
                'damaged':   row.get('damaged', 0) or 0,
            }
        return movement
    except Exception as e:
        # Fall back to a client-side pass rather than failing the page — the
        # $year/$month grouping is standard, but this keeps the dashboard
        # working on any backend that doesn't support it.
        app.logger.warning(f"Dashboard aggregation unavailable, falling back: {e}")
        movement = defaultdict(dict)
        cursor = transactions.find(
            {'timestamp': {'$gte': window_start},
             'type': {'$in': ['dispense', 'receive', 'adjustment', 'opening_balance', 'expiry_removal',
                       'internal_issue', 'damage']}},
            {'_id': 0, 'med_name': 1, 'type': 1, 'quantity': 1, 'timestamp': 1}
        )
        for tx in cursor:
            ts = tx.get('timestamp')
            if not isinstance(ts, datetime):
                continue
            bucket = movement[tx.get('med_name')].setdefault(
                (ts.year, ts.month), dict(_ZERO_MOVEMENT))
            qty = tx.get('quantity', 0) or 0
            k = tx.get('type')
            if k == 'dispense':
                bucket['dispensed'] += qty
            elif k in ('receive', 'opening_balance'):
                bucket['received'] += qty
            elif k == 'expiry_removal':
                bucket['expired'] += qty
            elif k == 'internal_issue':
                bucket['issued'] += qty
            elif k == 'damage':
                bucket['damaged'] += qty
            else:
                bucket['adjusted'] += qty
        return movement


# -----------------------------------------------------------------------
# FIX (Performance): movement totals for a whole set of medications in ONE
# aggregation, replacing the per-medication query loop that reports() used to
# run.
#
# The old pattern issued two round trips per medication (a find plus an
# aggregate), so an inventory report over a 260-item formulary meant roughly
# 520 sequential round trips to Atlas — slow enough to risk a serverless
# timeout, and unnecessary, since a single $group by med_name returns exactly
# the same numbers.
#
# It was also a correctness problem: each of those queries sat in its own
# try/except that fell back to zeros, so one flaky query produced a
# plausible-looking row with fabricated figures in it. With a single query a
# failure is visible instead of silent.
# -----------------------------------------------------------------------
_ZERO_MOVEMENT = {'dispensed': 0, 'received': 0, 'adjusted': 0, 'expired': 0,
                  'issued': 0, 'damaged': 0}


def _movement_by_med(transactions: Collection, time_filter: Query,
                     med_names: list[str] | None = None) -> dict[str, Doc]:
    """med_name -> {'dispensed', 'received', 'adjusted'} over a time window.

    'adjusted' is the signed net of stock-take corrections; callers must
    unwind it when back-calculating a balance, or a physical count will
    silently distort every period that contains it.
    """
    # NEW (Opening balances): an opening load is stock arriving, so every
    # back-calculated beginning balance has to account for it exactly as it
    # does a receipt. Omitting it would make every period containing a go-live
    # load report a beginning balance that is too high.
    match = {'timestamp': time_filter,
             'type': {'$in': ['dispense', 'receive', 'adjustment', 'opening_balance', 'expiry_removal',
                       'internal_issue', 'damage']}}
    if med_names is not None:
        match['med_name'] = {'$in': med_names}
    pipeline: list[Doc] = [
        {'$match': match},
        {'$group': {
            '_id': '$med_name',
            'dispensed': {'$sum': {'$cond': [{'$eq': ['$type', 'dispense']}, '$quantity', 0]}},
            'received':  {'$sum': {'$cond': [{'$in': ['$type', ['receive', 'opening_balance']]},
                                             '$quantity', 0]}},
            'adjusted':  {'$sum': {'$cond': [{'$eq': ['$type', 'adjustment']}, '$quantity', 0]}},
            'expired':   {'$sum': {'$cond': [{'$eq': ['$type', 'expiry_removal']}, '$quantity', 0]}},
            'issued':    {'$sum': {'$cond': [{'$eq': ['$type', 'internal_issue']}, '$quantity', 0]}},
            'damaged':   {'$sum': {'$cond': [{'$eq': ['$type', 'damage']}, '$quantity', 0]}},
        }}
    ]
    try:
        return {r['_id']: {'dispensed': r.get('dispensed', 0) or 0,
                           'received':  r.get('received', 0) or 0,
                           'adjusted':  r.get('adjusted', 0) or 0,
                           'expired':   r.get('expired', 0) or 0,
                           'issued':    r.get('issued', 0) or 0,
                           'damaged':   r.get('damaged', 0) or 0}
                for r in transactions.aggregate(pipeline)}
    except Exception as e:
        app.logger.warning(f"Movement aggregation unavailable, falling back: {e}")
        out: dict[str, Doc] = {}
        for tx in transactions.find(match, {'_id': 0, 'med_name': 1, 'type': 1, 'quantity': 1}):
            b = out.setdefault(tx.get('med_name'), dict(_ZERO_MOVEMENT))
            qty = tx.get('quantity', 0) or 0
            k = tx.get('type')
            if k == 'dispense':
                b['dispensed'] += qty
            elif k in ('receive', 'opening_balance'):
                b['received'] += qty
            elif k == 'expiry_removal':
                b['expired'] += qty
            elif k == 'internal_issue':
                b['issued'] += qty
            elif k == 'damage':
                b['damaged'] += qty
            else:
                b['adjusted'] += qty
        return out


def _activity_by_month(transactions: Collection, window_start: datetime) -> dict[tuple[int, int], Doc]:
    """(year, month) -> counts of dispensing/receiving ACTIVITY, not units.

    Summing `quantity` across different medications adds tablets to vials to
    millilitres — arithmetically valid, semantically empty. These are the
    countable events instead: how many visits, how many prescription items,
    how many patients, how many deliveries.

    A dispense writes one document per medication line, all sharing a
    transaction_id for the visit, so lines and visits are different numbers
    and both are worth knowing.
    """
    buckets: defaultdict[tuple[int, int], Doc] = defaultdict(
        lambda: {'visits': set(), 'lines': 0, 'patients': set(),
                                   'receipts': 0, 'deliveries': set(), 'adjusted_lines': 0})
    pipeline = [
        {'$match': {'timestamp': {'$gte': window_start},
                    'type': {'$in': ['dispense', 'receive', 'adjustment']}}},
        {'$group': {
            '_id': {'y': {'$year': '$timestamp'}, 'm': {'$month': '$timestamp'}, 't': '$type'},
            'lines': {'$sum': 1},
            'visits': {'$addToSet': '$transaction_id'},
            'patients': {'$addToSet': '$patient'},
            'orders': {'$addToSet': '$order_number'},
        }}
    ]

    def _clean(vals):
        return {v for v in (vals or []) if v not in (None, '', 'N/A')}

    try:
        rows = list(transactions.aggregate(pipeline))
        for r in rows:
            k = r['_id']
            b = buckets[(k['y'], k['m'])]
            if k['t'] == 'dispense':
                b['lines'] += r.get('lines', 0)
                b['visits'] |= _clean(r.get('visits'))
                b['patients'] |= _clean(r.get('patients'))
            elif k['t'] == 'receive':
                b['receipts'] += r.get('lines', 0)
                b['deliveries'] |= _clean(r.get('orders'))
            else:
                b['adjusted_lines'] += r.get('lines', 0)
        return buckets
    except Exception as e:
        app.logger.warning(f"Activity aggregation unavailable, falling back: {e}")
        cursor = transactions.find(
            {'timestamp': {'$gte': window_start},
             'type': {'$in': ['dispense', 'receive', 'adjustment']}},
            {'_id': 0, 'type': 1, 'timestamp': 1, 'transaction_id': 1,
             'patient': 1, 'order_number': 1})
        for tx in cursor:
            ts = tx.get('timestamp')
            if not isinstance(ts, datetime):
                continue
            b = buckets[(ts.year, ts.month)]
            if tx.get('type') == 'dispense':
                b['lines'] += 1
                b['visits'] |= _clean([tx.get('transaction_id')])
                b['patients'] |= _clean([tx.get('patient')])
            elif tx.get('type') == 'receive':
                b['receipts'] += 1
                b['deliveries'] |= _clean([tx.get('order_number')])
            else:
                b['adjusted_lines'] += 1
        return buckets


# -----------------------------------------------------------------------
# FIX (Performance/Scalability): /dispense and /receive used to fetch the
# ENTIRE transaction history with no limit and render every row into one HTML
# page. Measured at 6,000 transactions that produced a 6.5 MB page; at a few
# thousand dispensing lines a month it reaches tens of megabytes within a
# year, on the page dispensers open every single time they dispense.
#
# Two changes fix it:
#   1. A default date window, so an unfiltered visit shows the current month
#      rather than all history. A search overrides it — searching should look
#      across everything, not just this month, or the box is useless for
#      finding an old record.
#   2. Real pagination, so even a wide window can't produce an unbounded page.
# -----------------------------------------------------------------------
DISPENSE_PAGE_SIZE = 25    # visits per page (a visit may hold several lines)
RECEIVE_PAGE_SIZE  = 50    # receipt lines per page


def _requested_page() -> int:
    try:
        return max(1, int(request.values.get('page', 1)))
    except (TypeError, ValueError):
        return 1


def _default_window(start_date: str | None, end_date: str | None, search: str | None) -> tuple[str | None, str | None, bool]:
    """Fall back to the current month when nothing else narrows the list.

    Returns (start_date, end_date, defaulted). A search is deliberately NOT
    date-limited: someone looking up a patient or an invoice from March should
    find it without first working out which month to ask for.
    """
    if start_date or end_date or search:
        return start_date, end_date, False
    today = datetime.utcnow().date()
    return today.replace(day=1).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d'), True


def _pager(page: int, page_size: int, total: int) -> Doc:
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    return {'page': page, 'pages': pages, 'total': total, 'page_size': page_size,
            'has_prev': page > 1, 'has_next': page < pages,
            'first': 0 if total == 0 else (page - 1) * page_size + 1,
            'last': min(page * page_size, total)}



# ---------------------------------------------------------------------------
# CSV download.
#
# Every report was print-only, so anything that had to leave the pharmacy —
# the requisition above all — was read off the screen and typed again
# somewhere else. The figures are already computed; handing them over as a
# file costs nothing and removes a whole re-keying job.
#
# Each report names its columns explicitly rather than dumping dictionary
# keys, so the file reads the way the printed report does and does not change
# shape whenever an internal field is added.
# ---------------------------------------------------------------------------
# The inventory table is the one report whose COLUMNS change with the role:
# an admin sees Dispensed with Adjustment, Expired and Damaged beside it, while
# a non-admin sees one honest "Issued" figure with those three already folded
# in. The download has to follow that, for two separate reasons. Giving a
# non-admin the admin columns would hand them figures the screen withholds; and
# putting dispensed_effective in the same sheet as the Adjustment, Expired and
# Damaged columns would double-count them, so the row would not reconcile.
INVENTORY_TAIL = [
    ('current_balance', 'Current Balance'), ('amc', 'AMC'),
    ('unmet', 'Unmet'), ('adjusted_amc', 'Adjusted AMC'),
    ('fill_rate', 'Fill Rate %'), ('amount_to_order', 'Amount to Order')]
INVENTORY_HEAD = [
    ('med_name', 'Item'), ('category', 'Category'),
    ('beginning_balance', 'Beginning Balance'), ('received', 'Received')]
INVENTORY_COLUMNS_ADMIN = INVENTORY_HEAD + [
    ('dispensed', 'Dispensed'), ('adjustment', 'Adjustment'),
    ('expired', 'Expired'), ('issued', 'To Departments'),
    ('damaged', 'Damaged')] + INVENTORY_TAIL
INVENTORY_COLUMNS_USER = INVENTORY_HEAD + [
    ('dispensed_effective', 'Issued'),
    ('issued', 'To Departments')] + INVENTORY_TAIL

CSV_REPORTS = {
    'inventory': ('Inventory Report', 'report_data', INVENTORY_COLUMNS_USER),
    'stock_on_hand': ('Stock on Hand', 'stock_data', [
        ('name', 'Medication'), ('balance', 'Balance'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch'), ('price', 'Unit Price')]),
    'expired_list': ('Expired Stock', 'stock_data', [
        ('name', 'Medication'), ('balance', 'Balance'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch'), ('price', 'Unit Price')]),
    'near_expired_list': ('Expiring Soon', 'stock_data', [
        ('name', 'Medication'), ('balance', 'Balance'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch'), ('price', 'Unit Price')]),
    'out_of_stock_list': ('Out of Stock', 'stock_data', [
        ('name', 'Medication'), ('balance', 'Balance'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch')]),
    'sick_leave': ('Sick Leave Register', 'sick_leave', [
        ('date', 'Date'), ('patient', 'Name'), ('company', 'Company'),
        ('days', 'Days Given'), ('diagnoses', 'Diagnosis'),
        ('prescriber', 'Prescriber')]),
    'expiry_writeoffs': ('Losses', 'writeoffs', [
        ('date', 'Date'), ('med_name', 'Item Description'), ('quantity', 'Qty'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch/Lot'),
        ('line_value', 'Total Cost'), ('remarks', 'Remarks')]),
    'internal_issues': ('Internal Issues', 'issues', [
        ('date', 'Date'), ('med_name', 'Medication'), ('quantity', 'Qty'),
        ('department', 'Department'), ('issued_to', 'Received By'),
        ('line_value', 'Value'), ('user', 'Issued By')]),
    'duplicate_visits': ('Possible Double-Dispensing', 'duplicates', [
        ('patient', 'Patient'), ('med_name', 'Medication'),
        ('quantity', 'Quantity'), ('first', 'First recorded'),
        ('second', 'Recorded again'), ('minutes', 'Minutes apart'),
        ('units', 'Units counted twice'), ('value', 'Value counted twice'),
        ('dispenser', 'Dispenser')]),
    'unmet_demand': ('Unmet Demand', 'unmet_list', [
        ('date', 'Date'), ('med_name', 'Medication'),
        ('quantity_requested', 'Requested'), ('patient', 'Patient')]),
    'receive_list': ('Receipts', 'receive_list', [
        ('date', 'Date'), ('med_name', 'Medication'), ('quantity', 'Qty'),
        ('batch', 'Batch'), ('expiry_date', 'Expiry'), ('supplier', 'Supplier'),
        ('invoice_number', 'Invoice'), ('line_value', 'Value')]),
    'controlled_drug_register': ('Controlled Drug Register', 'controlled_register', [
        ('date', 'Date'), ('med_name', 'Medication'), ('movement', 'Movement'),
        ('in_qty', 'In'), ('out_qty', 'Out'), ('balance_after', 'Balance'),
        ('patient', 'Patient'), ('user', 'By')]),
}

ORDER_REQUEST_COLUMNS = [
    ('supplier', 'Supplier'), ('med_name', 'Medication'), ('amc', 'AMC'),
    ('balance', 'Balance on Hand'), ('mos_label', 'Months of Stock'),
    ('shortfall', 'Shortfall (units)'), ('pack_size', 'Pack Size'),
    ('packs', 'Packs to Order'), ('units', 'Units Received'),
    ('pack_price', 'Price per Pack'), ('line_cost', 'Line Cost'),
]


def _csv_value(v: Any) -> str:
    """Flatten anything a report row might hold into one cell."""
    if v is None:
        return ''
    if isinstance(v, (list, tuple)):
        return '; '.join(str(x) for x in v)
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d')
    return str(v)


def _report_csv(report_type: str | None, ctx: Doc, is_admin: bool = False) -> tuple[str, str] | None:
    """(filename, csv text) for the report on screen, or None if it has none."""
    if report_type == 'controlled_drug_register':
        # The register is grouped by medication, each group holding its own
        # movements. Handing those groups straight to the writer produced one
        # blank line per medication and not a single movement — the file had
        # the right headings over nothing at all.
        rows = []
        for group in ctx.get('controlled_register') or []:
            for tx in group.get('transactions') or []:
                row = dict(tx)
                row['med_name'] = group.get('med_name', '')
                ts = tx.get('timestamp')
                if not row.get('date') and ts:
                    row['date'] = ts.strftime('%Y-%m-%d')
                rows.append(row)
        columns = CSV_REPORTS['controlled_drug_register'][2]
        title = 'Controlled Drug Register'
    elif report_type == 'inventory':
        columns = INVENTORY_COLUMNS_ADMIN if is_admin else INVENTORY_COLUMNS_USER
        rows = ctx.get('report_data') or []
        title = 'Inventory Report'
    elif report_type == 'order_request':
        order = ctx.get('order_request') or {}
        rows = []
        for group in order.get('groups') or []:
            for line in group.get('lines') or []:
                row = dict(line)
                row['supplier'] = group.get('supplier', '')
                rows.append(row)
        columns = ORDER_REQUEST_COLUMNS
        title = 'Order Request'
    else:
        spec = CSV_REPORTS.get(report_type or '')
        if not spec:
            return None
        title, key, columns = spec
        rows = ctx.get(key) or []

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([label for _f, label in columns])
    for row in rows:
        writer.writerow([_csv_value(row.get(f)) for f, _lab in columns])
    stamp = local_now().strftime('%Y%m%d')
    name = title.lower().replace(' ', '-')
    return f'{name}-{stamp}.csv', out.getvalue()


# One row per medication line, because that is how a dispense is recorded and
# how a spreadsheet can count it. The visit's own details repeat down its
# lines, and Visit Cost appears once per visit — on its first line, blank on
# the rest — exactly as the screen's merged cell shows it. That way summing
# either Line Cost or Visit Cost gives the right total instead of a figure
# inflated by every extra medicine on a prescription.
DISPENSE_CSV_COLUMNS = [
    ('date', 'Date'), ('timestamp', 'Timestamp'), ('patient', 'Patient'),
    ('company', 'Company'), ('position', 'Position'), ('gender', 'Gender'),
    ('birth_decade', 'Born'), ('outcome', 'Outcome'),
    ('outcome_days', 'Days'), ('diagnoses', 'Diagnoses'),
    ('prescriber', 'Prescriber'), ('dispenser', 'Dispenser'),
    ('med_name', 'Medication'), ('quantity', 'Quantity'),
    ('price', 'Unit Cost'), ('line_cost', 'Line Cost'),
    ('visit_cost', 'Visit Cost'), ('user', 'Recorded By'),
    ('transaction_id', 'Visit ID'),
]
# A download is not a page, so it is not paged — it is everything the filter
# matches. Past this many lines the date range is doing no work, and silently
# handing over a truncated file would be worse than saying so.
DISPENSE_CSV_MAX_LINES = 20000


def _dispense_csv(transactions: Collection, base_query: Query) -> tuple[str, str] | None:
    """(filename, csv text) for the dispensing record the filter matches.

    Returns None if the match is too large to hand over in one file.
    """
    ids = [g['_id'] for g in transactions.aggregate([
        {'$match': base_query},
        {'$group': {'_id': '$transaction_id', 'ts': {'$max': '$timestamp'}}},
        {'$sort': {'ts': -1}},
    ])]
    # A search that matches one line brings its whole visit, the same way the
    # screen does — a record showing two of a patient's three medicines is
    # not the record.
    lines = list(transactions.find(
        {'type': 'dispense', 'transaction_id': {'$in': ids}}))
    if len(lines) > DISPENSE_CSV_MAX_LINES:
        return None

    grouped: dict[Any, Docs] = {}
    for ln in lines:
        grouped.setdefault(ln.get('transaction_id'), []).append(ln)

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([label for _f, label in DISPENSE_CSV_COLUMNS])
    for tid in ids:
        rows = grouped.get(tid) or []
        rows.sort(key=lambda r: (r.get('med_name') or ''))
        visit_cost = sum(_line_value(r) for r in rows)
        for n, r in enumerate(rows):
            row = dict(r)
            row['line_cost'] = f'{_line_value(r):.2f}'
            row['visit_cost'] = f'{visit_cost:.2f}' if n == 0 else ''
            ts = r.get('timestamp')
            row['timestamp'] = ts.strftime('%Y-%m-%d %H:%M:%S') if ts else ''
            if not row.get('date') and ts:
                row['date'] = ts.strftime('%Y-%m-%d')
            writer.writerow([_csv_value(row.get(f))
                             for f, _lab in DISPENSE_CSV_COLUMNS])
    stamp = local_now().strftime('%Y%m%d')
    return f'dispensing-record-{stamp}.csv', out.getvalue()


def _past_receivers(db: Database, limit: int = 40) -> list[str]:
    """People who have taken stock for a department before.

    Offered as suggestions rather than filled in: the right answer changes
    with every transfer, so the app can shorten the typing but must not
    choose.
    """
    try:
        names = db['transactions'].distinct(
            'issued_to', {'type': 'internal_issue',
                          'issued_to': {'$nin': [None, '']}})
        return sorted(names)[:limit]
    except Exception:
        return []


def _new_visit_defaults(current_user: Doc) -> Doc:
    """What a fresh dispensing form starts with.

    Rendered into the form AND handed to the script that restores it after a
    visit is saved, so a default cannot exist in one place and be forgotten in
    the other. Add a field here and both follow.
    """
    return {
        'date': local_now().strftime('%Y-%m-%d'),
        'dispenser': current_user,
        'outcome': 'Outpatient',
    }


def _birth_decades() -> list[int]:
    """Decade options, most recent first — most patients are recent births."""
    this = (datetime.utcnow().year // 10) * 10
    return list(range(this, 1919, -10))


def _clinical_by_month(transactions: Collection, window_start: datetime,
                       months: list[tuple[int, int]]) -> Doc:
    """Outcomes and diagnoses per month, plus a period total per diagnosis.

    Counted per VISIT, not per dispensed line: a visit that produced three
    medications is one referral, not three, and one episode of a diagnosis. The
    dispense collection stores one document per medication line, so counting
    rows here would multiply every clinical figure by the prescription size.
    """
    idx = {ym: i for i, ym in enumerate(months)}
    n = len(months)
    outcomes: dict[str, Doc] = {}
    dx_month: dict[str, list[int]] = {}
    dx_total: dict[str, int] = {}
    seen, patients = set(), set()
    cursor = transactions.find(
        {'type': 'dispense', 'timestamp': {'$gte': window_start}},
        {'_id': 0, 'timestamp': 1, 'transaction_id': 1, 'outcome': 1,
         'outcome_days': 1, 'diagnoses': 1, 'patient': 1})
    for t in cursor:
        ts = t.get('timestamp')
        if not isinstance(ts, datetime):
            continue
        i = idx.get((ts.year, ts.month))
        if i is None:
            continue
        tid = t.get('transaction_id')
        if tid in seen:
            continue
        seen.add(tid)
        if t.get('patient'):
            patients.add(t['patient'])
        oc = t.get('outcome')
        if oc:
            row = outcomes.setdefault(oc, {'counts': [0] * n, 'days': 0})
            row['counts'][i] += 1
            row['days'] += t.get('outcome_days') or 0
        for d in (t.get('diagnoses') or []):
            # Count under the name the diagnosis is called NOW. A visit from
            # last month says "Injury"; the list calls it "Injury, site
            # unspecified". Counted apart, both figures are half the truth and
            # the trend below breaks in two. The record itself is untouched.
            d = _canonical_diagnosis((d or '').strip())
            if not d:
                continue
            dx_month.setdefault(d, [0] * n)[i] += 1
            dx_total[d] = dx_total.get(d, 0) + 1
    top = sorted(dx_total.items(), key=lambda kv: -kv[1])[:12]
    return {
        'outcomes': [{'name': k, 'counts': v['counts'], 'total': sum(v['counts']),
                      'days': v['days']}
                     for k, v in sorted(outcomes.items(), key=lambda kv: -sum(kv[1]['counts']))],
        'diagnoses': [{'name': k, 'counts': dx_month[k], 'total': v,
                       'trend': _dx_trend(dx_month[k])} for k, v in top],
        'episodes': len(seen),
        'patients': patients,
    }


def _dx_trend(counts: Sequence[int]) -> str:
    """Compare the last completed month against the mean of the ones before."""
    if len(counts) < 3:
        return '-'
    body, last = counts[:-2], counts[-2]
    base = sum(body) / len(body) if body else 0
    if base == 0:
        return 'new' if last else '-'
    pct = (last - base) / base * 100
    if abs(pct) < 20:
        return 'steady'
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.0f}%"


def _parse_expiry(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).split('T')[0], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


DEPARTMENTS = ['Emergency Department', 'Emergency Trolley', 'PHC', 'OHC',
               'Lab', 'Testing Room', 'Tx Room', 'Doctor Consultation',
               'Campaigns', 'Environment', 'Other']

DAMAGE_REASONS = ['Broken / spilled', 'Contaminated', 'Cold chain failure',
                  'Recalled by supplier', 'Lost / unaccounted', 'Other']


@app.route('/internal-issue', methods=['GET', 'POST'])
@login_required
def internal_issue() -> ResponseReturnValue:
    """Stock leaving the store other than to a patient.

    Two actions, deliberately distinct:
      internal_issue — CONSUMED by a department. Counts toward AMC; the order
                       request replaces it.
      damage         — LOST. Excluded from AMC, exactly like expiry, so a
                       breakage never causes the app to reorder more.
    """
    current_user = session['user']['name']
    flashed = get_flashed_messages()
    message = flashed[0] if flashed else None
    try:
        db = get_mongo_client()['pharmacy_db']
        medications, transactions = db['medications'], db['transactions']

        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'issue':
                dept = (request.form.get('department') or '').strip()
                if dept == 'Other':
                    dept = (request.form.get('department_other') or '').strip() or 'Other'
                issued_to = (request.form.get('issued_to') or '').strip()
                note = (request.form.get('note') or '').strip()
                if not dept or not issued_to:
                    flash('Department and the person receiving are both required.')
                    return redirect(url_for('internal_issue'))

                names = request.form.getlist('med_name')
                qtys = request.form.getlist('quantity')
                issue_id = f"ISS-{datetime.utcnow().strftime('%Y%m%d')}-{uuid4().hex[:4].upper()}"
                done, problems = [], []
                for name, raw in zip(names, qtys):
                    name = (name or '').strip()
                    raw = (raw or '').strip()
                    if not name or not raw:
                        continue
                    try:
                        qty = int(raw)
                    except ValueError:
                        problems.append(f'{name}: quantity must be a whole number')
                        continue
                    if qty < 1:
                        continue
                    med = medications.find_one({'name': name})
                    if not med:
                        problems.append(f'"{name}" is not on file')
                        continue
                    available = med.get('balance', 0) or 0
                    given = min(qty, available)
                    if given <= 0:
                        problems.append(f'{name}: nothing on hand')
                        continue
                    if given < qty:
                        problems.append(f'{name}: only {given} of {qty} available')
                    price = med.get('price', 0) or 0
                    medications.update_one({'name': name}, {'$inc': {'balance': -given}})
                    transactions.insert_one({
                        'type': 'internal_issue', 'issue_id': issue_id,
                        'med_name': name, 'quantity': given,
                        'price': price, 'line_value': given * price,
                        'department': dept, 'issued_to': issued_to, 'note': note,
                        'batch': med.get('batch'), 'schedule': med.get('schedule'),
                        'user': current_user, 'timestamp': datetime.utcnow(),
                    })
                    done.append(f'{name} x{given}')
                if done:
                    write_audit_entry('UPDATE', 'internal_issue', issue_id,
                                      f'issued to {dept} ({issued_to}): {", ".join(done)}')
                    msg = f'Issue to {dept} recorded: {", ".join(done)}.'
                else:
                    msg = 'Nothing was issued.'
                if problems:
                    msg += ' Problems: ' + '; '.join(problems)
                flash(msg)
                return redirect(url_for('internal_issue'))

            if action == 'damage':
                name = (request.form.get('med_name') or '').strip()
                try:
                    qty = int(request.form.get('quantity', ''))
                except (TypeError, ValueError):
                    flash('Quantity must be a whole number.')
                    return redirect(url_for('internal_issue'))
                reason = (request.form.get('reason') or '').strip()
                if qty < 1 or not reason:
                    flash('A quantity of at least 1 and a reason are both required.')
                    return redirect(url_for('internal_issue'))
                med = medications.find_one({'name': name})
                if not med:
                    flash(f'Medication "{name}" is not on file.')
                    return redirect(url_for('internal_issue'))
                balance = med.get('balance', 0) or 0
                if qty > balance:
                    flash(f'Cannot write off {qty} of "{name}" — only {balance} on hand.')
                    return redirect(url_for('internal_issue'))
                price = med.get('price', 0) or 0
                medications.update_one({'name': name}, {'$inc': {'balance': -qty}})
                transactions.insert_one({
                    'type': 'damage', 'med_name': name,
                    'quantity': -qty,                    # signed, like expiry
                    'price': price, 'line_value': -qty * price,
                    'reason': reason, 'note': (request.form.get('note') or '').strip(),
                    'batch': med.get('batch'),
                    # The register reports an expiry date against every line, so
                    # capture it here too — expiry removals already did.
                    'expiry_date': med.get('expiry_date'),
                    'balance_before': balance,
                    'user': current_user, 'timestamp': datetime.utcnow(),
                })
                write_audit_entry('UPDATE', 'damage', name,
                                  f'{qty} unit(s) written off as {reason} '
                                  f'(balance {balance} -> {balance - qty}, '
                                  f'value R{qty * price:.2f})')
                flash(f'Loss recorded: {name} x{qty} ({reason}). '
                      f'Balance is now {balance - qty}. Value R{qty * price:.2f}.')
                return redirect(url_for('internal_issue'))

            flash('Unknown action.')
            return redirect(url_for('internal_issue'))

        recent = list(transactions.find(
            {'type': {'$in': ['internal_issue', 'damage']}}
        ).sort('timestamp', -1).limit(100))
        return render_template(
            'internal_issue.html', nav_links=get_nav_links(), message=message,
            current_user=session['user']['name'],
            past_receivers=_past_receivers(db),
            departments=DEPARTMENTS, damage_reasons=DAMAGE_REASONS, recent=recent)
    except ServerSelectionTimeoutError:
        return render_template(
            'internal_issue.html', nav_links=get_nav_links(),
            message='Database connection failed. Please try again later.',
            current_user=session.get('user', {}).get('name', ''),
            past_receivers=[],
            departments=DEPARTMENTS, damage_reasons=DAMAGE_REASONS, recent=[]), 500


# =======================================================================
# NEW (Temperature log): monthly max-min thermometer register.
#
# A max-min thermometer gives THREE figures at each check — the highest and
# lowest it has seen since it was last reset, and the temperature right now.
# All three are recorded, morning and afternoon, for each monitored location,
# and the month's chart plots them as max (red), min (blue) and actual
# (black), which is the form an inspector expects to see.
# =======================================================================
TEMP_LOCATIONS = ['Dispensary', 'Store 1', 'Store 2', 'Fridge']
TEMP_SESSIONS = ['Morning', 'Afternoon']
# Each room keeps its own instrument permanently, so the number is a property
# of the location and never has to be typed. If an instrument is ever replaced
# by one with a different number, change it here rather than on every reading.
TEMP_THERMOMETERS = {'Dispensary': '1', 'Store 1': '2',
                     'Store 2': '3', 'Fridge': '4'}
# Before noon is the morning round, noon onwards is the afternoon round.
TEMP_NOON_HOUR = 12

# Acceptable storage range per location, and the "cold spot"/"hot spot" warning
# bands the official chart names in its legend box.
TEMP_LIMITS = {
    'Dispensary': (15.0, 25.0),
    'Store 1': (15.0, 25.0),
    'Store 2': (15.0, 25.0),
    'Fridge': (2.0, 8.0),
}
TEMP_SPOTS = {
    'Dispensary': ((15.0, 17.5), (23.0, 25.0)),
    'Store 1': ((15.0, 17.5), (23.0, 25.0)),
    'Store 2': ((15.0, 17.5), (23.0, 25.0)),
    'Fridge': ((2.0, 3.0), (7.0, 8.0)),
}
TEMP_MIN_PLAUSIBLE, TEMP_MAX_PLAUSIBLE = -50.0, 80.0

# The temperature axis of SR-TC-001 is a stack of 14 cells with only five of
# them labelled: +30, +26, +22, +18, +15. Note the spacing is NOT uniform —
# the first three gaps span 4 degrees over three rows, the last spans 3. That
# is deliberate on the form: +15 is the lower acceptable limit, so it was put
# on a row line rather than left to fall between two. Positions are therefore
# interpolated BETWEEN the printed labels, which puts a reading exactly where a
# person reading the same labels would place it by hand.
#
# The store-room scale is the official one. The fridge scale is the same grid
# re-labelled for 2-8 degree storage, since the reference chart covers store
# rooms only.
TEMP_SCALES = {
    'room': {'anchors': [(0, 30.0), (3, 26.0), (6, 22.0), (9, 18.0),
                         (12, 15.0), (13, 14.0)],
             'labelled': {0: '+30°C', 3: '+26°C', 6: '+22°C',
                          9: '+18°C', 12: '+15°C'}},
    'fridge': {'anchors': [(0, 10.0), (3, 8.0), (6, 6.0), (9, 4.0),
                           (12, 2.0), (13, 1.0)],
               'labelled': {0: '+10°C', 3: '+8°C', 6: '+6°C',
                            9: '+4°C', 12: '+2°C'}},
}
TEMP_ROWS = 14

# The chart's own convention, printed in its legend: actual in black pen,
# minimum in blue, maximum in red. Each series also carries its own marker
# shape — these charts are photocopied, and once the colour is gone red and
# blue are the same grey. Shape survives that; colour does not.
TEMP_SERIES = [
    ('max', 'Maximum', '#cc0000', 'circle', 'Red'),
    ('min', 'Minimum', '#0047b3', 'square', 'Blue'),
    ('actual', 'Actual', '#000000', 'triangle', 'Black'),
]
# The legend box is worded and ordered as the printed form has it.
TEMP_LEGEND_ORDER = ['actual', 'min', 'max']
# What each pen actually records at this pharmacy: one check in the morning and
# one in the afternoon, with the day's average plotted between them. The form
# only has three pens, so they are used this way.
TEMP_PEN_MEANING = {'min': '= morning check',
                    'max': '= afternoon check',
                    'actual': '= average'}


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime, int]:
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end, (end - start).days


def _temp_scale(location: str) -> dict[str, Any]:
    return TEMP_SCALES['fridge' if location == 'Fridge' else 'room']


def _temp_row(value: float, anchors: Sequence[tuple[int, float]]) -> float:
    """Row position (fractional, 0 at the top) for a temperature.

    Interpolates between the printed labels, so a value lands where a person
    reading the same labels would put it. Out-of-scale values clamp to the end
    rows rather than being dropped — a reading off the top of the chart is
    exactly the one that must not go missing.
    """
    if value >= anchors[0][1]:
        return 0.0
    if value <= anchors[-1][1]:
        return float(anchors[-1][0])
    for (r1, t1), (r2, t2) in zip(anchors, anchors[1:]):
        if t2 <= value <= t1:
            if t1 == t2:
                return float(r1)
            return r1 + (t1 - value) / (t1 - t2) * (r2 - r1)
    return float(anchors[-1][0])


def _temp_marker(shape: str, x: float, y: float, colour: str) -> str:
    if shape == 'circle':
        return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{colour}" '
                f'stroke="#ffffff" stroke-width="1"/>')
    if shape == 'square':
        return (f'<rect x="{x - 2.9:.1f}" y="{y - 2.9:.1f}" width="5.8" height="5.8" '
                f'fill="{colour}" stroke="#ffffff" stroke-width="1"/>')
    return (f'<polygon points="{x:.1f},{y - 3.8:.1f} {x + 3.4:.1f},{y + 2.5:.1f} '
            f'{x - 3.4:.1f},{y + 2.5:.1f}" fill="{colour}" '
            f'stroke="#ffffff" stroke-width="1"/>')


def _reading_temp(r: Doc | None) -> float | None:
    """The single temperature on a reading, or None.

    One figure is recorded per check. Readings saved under the earlier
    three-figure shape (max / min / actual per session) fall back to their
    'actual' value, which was the reading on the dial at check time and so
    means the same thing — otherwise anything already entered would vanish
    from the chart the moment this changed.
    """
    if not r:
        return None
    v = r.get('temp')
    if v is None:
        v = r.get('actual')
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _temperature_chart(readings: Iterable[Doc], year: int, month: int, location: str,
                       thermometer: str = '') -> str:
    """SR-TC-001 reproduced as SVG, already filled in from the recorded data.

    Server-rendered with no JavaScript, so the printed sheet is exactly what
    the screen shows. Everything interpolated into the output is either a
    number, a fixed constant, or escaped.
    """
    start, _, days_in_month = _month_bounds(year, month)
    lo_lim, hi_lim = TEMP_LIMITS.get(location, (15.0, 25.0))
    cold, hot = TEMP_SPOTS.get(location, ((15.0, 17.5), (23.0, 25.0)))
    scale = _temp_scale(location)
    anchors, labelled = scale['anchors'], scale['labelled']

    by_slot = {}
    for r in readings:
        idx = (r['date'].day - 1) * 2 + (1 if r.get('session') == 'Afternoon' else 0)
        by_slot[idx] = r

    W, H = 1190, 648
    M = 8                      # page margin
    LBL = 104                  # left label column
    DAYS, COLS = 31, 62        # the form always prints 31 days
    gx, gw = M + LBL, W - 2 * M - LBL
    cw = gw / COLS
    head_y, head_h = 96, 22    # "Days of the month" strip
    ti_h = 15                  # "Time interval" strip
    row_h = 26
    grid_top = head_y + head_h + ti_h
    grid_bot = grid_top + TEMP_ROWS * row_h
    init_y, time_y = grid_bot, grid_bot + 20
    table_bot = time_y + 20

    def cx(i):
        return gx + (i + 0.5) * cw

    def ry(row):
        return grid_top + (row + 0.5) * row_h

    def ty(value):
        return ry(_temp_row(value, anchors))

    o = [f'<svg viewBox="0 0 {W} {H}" width="100%" '
         f'role="img" aria-label="Store room temperature chart" '
         f'style="background:#ffffff;font-family:Arial,Helvetica,sans-serif">',
         f'<rect x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>']

    def box(x, y, w, h, sw=1):
        o.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                 f'fill="none" stroke="#000000" stroke-width="{sw}"/>')

    def txt(x, y, s, size=10, anchor='start', weight='normal', fill='#000000'):
        o.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
                 f'text-anchor="{anchor}" font-weight="{weight}" fill="{fill}">'
                 f'{escape(str(s))}</text>')

    # ---- title block, matching the form's own header --------------------
    box(M, 8, W - 2 * M, 56)
    o.append(f'<line x1="{M + 86}" y1="8" x2="{M + 86}" y2="64" '
             f'stroke="#000000" stroke-width="1"/>')
    o.append(f'<line x1="{W - M - 196}" y1="8" x2="{W - M - 196}" y2="64" '
             f'stroke="#000000" stroke-width="1"/>')
    o.append(f'<line x1="{W - M - 196}" y1="36" x2="{W - M}" y2="36" '
             f'stroke="#000000" stroke-width="1"/>')
    txt(M + 43, 40, 'Lesotho', 8, 'middle', fill='#666666')
    txt(M + 43, 50, 'Ministry of Health', 7, 'middle', fill='#666666')
    txt(M + 98, 32, 'STORE ROOM TEMPERATURE CHART', 16, weight='bold')
    txt(M + 98, 54, f'Month: {start:%B}', 11)
    txt(M + 260, 54, f'Year: {year}', 11)
    txt(M + 400, 54, f'Location: {location}', 11, weight='bold')
    txt(M + 660, 40, 'M – Morning', 10)
    txt(M + 660, 54, 'A – Afternoon', 10)
    txt(W - M - 188, 26, 'Document no: SR-TC-001', 9.5)
    txt(W - M - 188, 54, 'Effective date: 12/2018', 9.5)

    # ---- out-of-range tint, so a breach is visible at a glance ----------
    # Very light: it must not survive a photocopy as anything that could be
    # mistaken for a mark.
    top_lim_y, bot_lim_y = ty(hi_lim), ty(lo_lim)
    o.append(f'<rect x="{gx:.1f}" y="{grid_top:.1f}" width="{gw:.1f}" '
             f'height="{top_lim_y - grid_top:.1f}" fill="#fdf0f0"/>')
    o.append(f'<rect x="{gx:.1f}" y="{bot_lim_y:.1f}" width="{gw:.1f}" '
             f'height="{grid_bot - bot_lim_y:.1f}" fill="#eef3fb"/>')

    # ---- the grid -------------------------------------------------------
    box(M, head_y, W - 2 * M, table_bot - head_y)
    for y in (head_y + head_h, grid_top, grid_bot, init_y + 20):
        o.append(f'<line x1="{M}" y1="{y:.1f}" x2="{W - M}" y2="{y:.1f}" '
                 f'stroke="#000000" stroke-width="1"/>')
    o.append(f'<line x1="{gx:.1f}" y1="{head_y}" x2="{gx:.1f}" '
             f'y2="{table_bot:.1f}" stroke="#000000" stroke-width="1"/>')
    for row in range(1, TEMP_ROWS):
        y = grid_top + row * row_h
        o.append(f'<line x1="{M}" y1="{y:.1f}" x2="{W - M}" y2="{y:.1f}" '
                 f'stroke="#9e9e9e" stroke-width="0.5"/>')

    txt(M + 4, head_y + 10, 'Days of the', 8.5)
    txt(M + 4, head_y + 19, 'month', 8.5)
    txt(M + 4, grid_top - 4, 'Time interval', 8.5)
    txt(M + 4, init_y + 14, 'Initials', 9)
    txt(M + 4, time_y + 14, 'Write time', 9)

    for row, label in labelled.items():
        txt(M + 4, ry(row) + 4, label, 9.5)

    for d in range(1, DAYS + 1):
        i = (d - 1) * 2
        x0, x1 = gx + i * cw, gx + (i + 2) * cw
        real = d <= days_in_month
        if not real:
            # The form always has 31 columns; a short month greys the surplus
            # so nobody reads a blank column as a missed check.
            o.append(f'<rect x="{x0:.1f}" y="{head_y:.1f}" width="{2 * cw:.1f}" '
                     f'height="{table_bot - head_y:.1f}" fill="#f0f0f0"/>')
        o.append(f'<line x1="{x1:.1f}" y1="{head_y}" x2="{x1:.1f}" '
                 f'y2="{table_bot:.1f}" stroke="#000000" stroke-width="1"/>')
        o.append(f'<line x1="{gx + (i + 1) * cw:.1f}" y1="{head_y + head_h:.1f}" '
                 f'x2="{gx + (i + 1) * cw:.1f}" y2="{table_bot:.1f}" '
                 f'stroke="#9e9e9e" stroke-width="0.5"/>')
        txt((x0 + x1) / 2, head_y + 16, d, 9.5, 'middle', 'bold',
            '#000000' if real else '#999999')
        if real:
            txt(cx(i), grid_top - 4, 'm', 8, 'middle')
            txt(cx(i + 1), grid_top - 4, 'a', 8, 'middle')

    # ---- the plotted readings -------------------------------------------
    # One reading is taken each morning and each afternoon. The form has three
    # pens, so they are used the way this pharmacy uses them: the morning
    # reading is marked in blue in that day's 'm' column, the afternoon
    # reading in red in the 'a' column, and the day's average in black
    # between the two.
    #
    # Morning is always blue even on a day the afternoon turns out cooler, so
    # the red line dips under the blue one. That crossing is left visible on
    # purpose: it is a real and unusual day, and sorting the two readings by
    # size to keep the lines apart would hide it.
    #
    # Lines break wherever a check is missing. Joining across a gap would draw
    # readings that were never taken, and a missed check is precisely what
    # this chart exists to show. The average needs BOTH readings, so a single
    # missed check breaks the black line for that day too.
    def _series_points(key):
        pts = []
        for d in range(days_in_month):
            m = _reading_temp(by_slot.get(d * 2))
            a = _reading_temp(by_slot.get(d * 2 + 1))
            if key == 'min':
                v, x = m, cx(d * 2)
            elif key == 'max':
                v, x = a, cx(d * 2 + 1)
            else:
                v = None if (m is None or a is None) else (m + a) / 2.0
                x = gx + (d * 2 + 1) * cw        # midway between m and a
            pts.append((x, v))
        return pts

    for key, _label, colour, shape, _pen in TEMP_SERIES:
        # One line per series, drawn straight through every reading there is.
        # A missed check is not marked on the line itself — it shows up as a
        # blank column in the Initials and Write time rows underneath, and as
        # a dash in the register table below the chart.
        plotted = [(x, ty(v)) for x, v in _series_points(key) if v is not None]
        if len(plotted) > 1:
            pts_attr = ' '.join(f'{x:.1f},{y:.1f}' for x, y in plotted)
            o.append(f'<polyline points="{pts_attr}" fill="none" '
                     f'stroke="{colour}" stroke-width="1.3" '
                     f'stroke-linejoin="round"/>')
        for x, y in plotted:
            o.append(_temp_marker(shape, x, y, colour))

    # ---- initials and time of check, as the form provides for -----------
    for i in range(days_in_month * 2):
        slot = by_slot.get(i)
        if not slot:
            continue
        if slot.get('initials'):
            txt(cx(i), init_y + 14, str(slot['initials'])[:4], 7.5, 'middle')
        if slot.get('check_time'):
            # Rotated: at 62 columns two horizontal times per day overlap.
            tx_, ty_ = cx(i), time_y + 17
            o.append(f'<text x="{tx_:.1f}" y="{ty_:.1f}" font-size="7" '
                     f'text-anchor="start" transform="rotate(-90 {tx_:.1f} {ty_:.1f})" '
                     f'fill="#000000">{escape(str(slot["check_time"])[:5])}</text>')

    # ---- footer: signing block and the form's two legend boxes ----------
    fy = table_bot + 6
    box(M, fy, W - 2 * M, H - fy - 6)
    o.append(f'<line x1="{W - M - 560}" y1="{fy:.1f}" x2="{W - M - 560}" '
             f'y2="{H - 6}" stroke="#000000" stroke-width="1"/>')
    o.append(f'<line x1="{W - M - 230}" y1="{fy:.1f}" x2="{W - M - 230}" '
             f'y2="{H - 6}" stroke="#000000" stroke-width="1"/>')
    txt(M + 8, fy + 20, f'Thermometer Number: {thermometer or "____________________"}', 10)
    txt(M + 300, fy + 20, f'Month: {start:%B}', 10)
    txt(M + 430, fy + 20, f'Year: {year}', 10)
    txt(M + 8, fy + 44, 'Reviewed by: ______________________________', 10)
    txt(M + 300, fy + 44, 'Date: ____________________', 10)

    # The form's own legend wording, kept as printed. What each pen records
    # here is spelled out beside it, because the sheet is filed on its own and
    # "Minimum" meaning "the morning check" is not self-evident off the page.
    lx = W - M - 552
    series_by_key = {k: (lbl, col, sh, pen) for k, lbl, col, sh, pen in TEMP_SERIES}
    for n, key in enumerate(TEMP_LEGEND_ORDER):
        label, colour, shape, pen = series_by_key[key]
        ly = fy + 17 + n * 15
        txt(lx, ly, f'{label}....', 9.5, weight='bold')
        txt(lx + 66, ly, 'use', 9.5)
        txt(lx + 88, ly, f'{pen} pen....', 9.5, fill=colour)
        o.append(_temp_marker(shape, lx + 164, ly - 3, colour))
        txt(lx + 178, ly, TEMP_PEN_MEANING[key], 8, fill='#555555')

    rx = W - M - 222
    txt(rx, fy + 15, f'Acceptable Range: {lo_lim:g} °C to {hi_lim:g} °C', 9)
    txt(rx, fy + 29, f'Cold spot + {cold[0]:g}°C to + {cold[1]:g}°C', 9)
    txt(rx, fy + 43, f'Hot spot + {hot[0]:g}°C to + {hot[1]:g}°C', 9)

    o.append('</svg>')
    return '\n'.join(o)


@app.route('/temperature', methods=['GET', 'POST'])
@login_required
def temperature() -> ResponseReturnValue:
    current_user = session['user']['name']
    message_class, message = _flashed_banner()
    is_admin = session['user'].get('role') == 'admin'
    today = datetime.utcnow().date()

    def chosen_location(src):
        loc = (src.get('location') or '').strip()
        return loc if loc in TEMP_LOCATIONS else TEMP_LOCATIONS[0]

    try:
        db = get_mongo_client()['pharmacy_db']
        logs = db['temperature_logs']

        if request.method == 'POST':
            action = request.form.get('action')
            loc = chosen_location(request.form)

            if action == 'delete':
                if not is_admin:
                    flash('Access denied. Only admins can delete a reading.')
                    return redirect(url_for('temperature'))
                try:
                    doc = logs.find_one({'_id': ObjectId(request.form.get('reading_id', ''))})
                except (InvalidId, TypeError):
                    doc = None
                if not doc:
                    flash('That reading was not found.')
                else:
                    logs.delete_one({'_id': doc['_id']})
                    write_audit_entry(
                        'DELETE', 'temperature_reading',
                        f"{doc.get('location')} {doc['date']:%Y-%m-%d} {doc.get('session')}",
                        f"removed reading high {doc.get('max')} low {doc.get('min')} "
                        f"actual {doc.get('actual')}")
                    flash(f"Reading deleted for {doc.get('location')} on "
                          f"{doc['date']:%Y-%m-%d} ({doc.get('session')}).", 'success')
                return redirect(url_for('temperature', location=loc,
                                        month=request.form.get('month') or ''))

            if action == 'record':
                sess = (request.form.get('session') or '').strip()
                if sess not in TEMP_SESSIONS:
                    flash('Choose morning or afternoon.')
                    return redirect(url_for('temperature', location=loc))
                try:
                    day = datetime.strptime(request.form['date'], '%Y-%m-%d')
                except (KeyError, ValueError):
                    flash('Enter a valid date.')
                    return redirect(url_for('temperature', location=loc))
                if day.date() > today:
                    flash('A reading cannot be dated in the future.')
                    return redirect(url_for('temperature', location=loc))
                try:
                    temp = float(request.form['temp'])
                except (KeyError, ValueError):
                    flash('The temperature must be a number.')
                    return redirect(url_for('temperature', location=loc))

                if not (TEMP_MIN_PLAUSIBLE <= temp <= TEMP_MAX_PLAUSIBLE):
                    flash(f'{temp:g}°C is outside the range a room thermometer '
                          f'can read — check for a mistyped figure.')
                    return redirect(url_for('temperature', location=loc))

                prior = logs.find_one({'location': loc, 'date': day, 'session': sess})
                doc = {
                    'location': loc, 'date': day, 'session': sess,
                    'temp': temp,
                    # The official chart has an Initials row and a Write time
                    # row under the grid, so both are captured per check.
                    'initials': (request.form.get('initials') or '').strip()[:4],
                    'check_time': (request.form.get('check_time') or '').strip()[:5],
                    'thermometer': (request.form.get('thermometer') or '').strip(),
                    'note': (request.form.get('note') or '').strip(),
                    'recorded_by': current_user, 'timestamp': datetime.utcnow(),
                }
                if prior:
                    # Drop the superseded three-figure fields, so a corrected
                    # reading cannot leave a stale 'actual' behind for the
                    # backward-compatible reader to pick up instead.
                    logs.update_one({'_id': prior['_id']},
                                    {'$set': doc,
                                     '$unset': {'max': '', 'min': '', 'actual': ''}})
                    write_audit_entry(
                        'UPDATE', 'temperature_reading',
                        f'{loc} {day:%Y-%m-%d} {sess}',
                        f'{_reading_temp(prior)}°C -> {temp}°C')
                    verb = 'corrected'
                else:
                    logs.insert_one(doc)
                    write_audit_entry('CREATE', 'temperature_reading',
                                      f'{loc} {day:%Y-%m-%d} {sess}',
                                      f'{temp}°C')
                    verb = 'recorded'

                lo_lim, hi_lim = TEMP_LIMITS.get(loc, (15.0, 25.0))
                if temp > hi_lim or temp < lo_lim:
                    flash(f'{loc} {day:%Y-%m-%d} {sess.lower()} {verb} at '
                          f'{temp:g}°C, outside the acceptable '
                          f'{lo_lim:g}–{hi_lim:g}°C range.', 'partial')
                else:
                    flash(f'{loc} {day:%Y-%m-%d} {sess.lower()} {verb} at '
                          f'{temp:g}°C.', 'success')
                return redirect(url_for('temperature', location=loc,
                                        month=f'{day.year:04d}-{day.month:02d}'))

            if action == 'record_round':
                # The whole round in one submission. Date, session, time,
                # thermometer and initials are all derived, so the only thing
                # typed is the temperatures themselves.
                now = local_now()
                sess = local_session()
                day = datetime(now.year, now.month, now.day)
                stamp = now.strftime('%H:%M')
                recorder_initials = ''.join(
                    w[0] for w in current_user.split() if w)[:4].upper()

                saved, corrected, breaches, rejected = [], [], [], []
                for idx, loc_name in enumerate(TEMP_LOCATIONS):
                    raw = (request.form.get(f'temp_{idx}') or '').strip()
                    if not raw:
                        continue          # not checked: record nothing
                    try:
                        temp = float(raw)
                    except ValueError:
                        rejected.append(f'{loc_name} ("{raw}" is not a number)')
                        continue
                    if not (TEMP_MIN_PLAUSIBLE <= temp <= TEMP_MAX_PLAUSIBLE):
                        rejected.append(f'{loc_name} ({temp:g}°C is not a '
                                        f'reading a thermometer can give)')
                        continue

                    prior = logs.find_one({'location': loc_name, 'date': day,
                                           'session': sess})
                    doc = {
                        'location': loc_name, 'date': day, 'session': sess,
                        'temp': temp,
                        'initials': recorder_initials,
                        'check_time': stamp,
                        'thermometer': TEMP_THERMOMETERS.get(loc_name, ''),
                        'note': '',
                        'recorded_by': current_user,
                        'timestamp': datetime.utcnow(),
                    }
                    if prior:
                        logs.update_one(
                            {'_id': prior['_id']},
                            {'$set': doc,
                             '$unset': {'max': '', 'min': '', 'actual': ''}})
                        write_audit_entry(
                            'UPDATE', 'temperature_reading',
                            f'{loc_name} {day:%Y-%m-%d} {sess}',
                            f'{_reading_temp(prior)}°C -> {temp}°C (round entry)')
                        corrected.append(f'{loc_name} {temp:g}°C')
                    else:
                        logs.insert_one(doc)
                        write_audit_entry(
                            'CREATE', 'temperature_reading',
                            f'{loc_name} {day:%Y-%m-%d} {sess}',
                            f'{temp}°C (round entry)')
                        saved.append(f'{loc_name} {temp:g}°C')
                    lo_lim, hi_lim = TEMP_LIMITS.get(loc_name, (15.0, 25.0))
                    if temp > hi_lim or temp < lo_lim:
                        breaches.append(f'{loc_name} {temp:g}°C')

                parts = []
                if saved:
                    parts.append(f"{sess} round recorded: {', '.join(saved)}")
                if corrected:
                    parts.append(f"corrected: {', '.join(corrected)}")
                if rejected:
                    parts.append(f"not recorded: {'; '.join(rejected)}")
                if not parts:
                    flash('Nothing was entered, so nothing was recorded.')
                    tone = None
                else:
                    if breaches:
                        parts.append('OUTSIDE RANGE: ' + ', '.join(breaches))
                    tone = 'error' if rejected else (
                        'partial' if breaches else 'success')
                    flash('. '.join(parts) + '.', tone)
                return redirect(url_for('temperature',
                                        location=chosen_location(request.form),
                                        month=request.form.get('month') or ''))

            flash('Unknown action.')
            return redirect(url_for('temperature'))

        # ----- GET: one location, one calendar month --------------------
        location = chosen_location(request.args)
        raw_month = (request.args.get('month') or '').strip()
        try:
            year, month = int(raw_month[:4]), int(raw_month[5:7])
            datetime(year, month, 1)
        except (ValueError, IndexError):
            year, month = today.year, today.month
        start, end, days_in_month = _month_bounds(year, month)

        readings = list(logs.find({'location': location,
                                   'date': {'$gte': start, '$lt': end}})
                        .sort([('date', 1), ('session', 1)]))
        lo_lim, hi_lim = TEMP_LIMITS.get(location, (15.0, 25.0))

        per_day: dict[int, Doc] = {}
        breach_count = 0
        for r in readings:
            slot = per_day.setdefault(r['date'].day, {})
            slot[r.get('session')] = r
            v = _reading_temp(r)
            if v is not None and (v > hi_lim or v < lo_lim):
                breach_count += 1

        # Each room keeps its own instrument, so the sheet's thermometer number
        # comes from the location rather than from whatever was last typed.
        # Readings entered before that mapping existed fall back to their own
        # stored number, so an older month still prints what it was checked on.
        thermometer = TEMP_THERMOMETERS.get(location, '')
        if not thermometer:
            for r in sorted(readings, key=lambda r: r['timestamp'], reverse=True):
                if r.get('thermometer'):
                    thermometer = r['thermometer']
                    break
        default_initials = ''.join(w[0] for w in current_user.split() if w)[:4].upper()

        # ----- the round panel: one box per room, everything else derived ---
        now_local = local_now()
        round_session = local_session()
        round_day = datetime(now_local.year, now_local.month, now_local.day)
        already = {d['location']: _reading_temp(d) for d in logs.find(
            {'date': round_day, 'session': round_session})}
        round_rows = []
        for loc_name in TEMP_LOCATIONS:
            lo_l, hi_l = TEMP_LIMITS.get(loc_name, (15.0, 25.0))
            round_rows.append({
                'location': loc_name,
                'thermometer': TEMP_THERMOMETERS.get(loc_name, '-'),
                'lo': lo_l, 'hi': hi_l,
                'existing': already.get(loc_name),
            })

        rows, days_complete = [], 0
        for d in range(1, days_in_month + 1):
            slot = per_day.get(d, {})
            am, pm = slot.get('Morning'), slot.get('Afternoon')
            if am and pm:
                days_complete += 1
            breach, ids, notes, by = False, [], [], []
            inits, times = [], []
            # Morning, afternoon, and the average the black line plots. The
            # average is shown only when both checks were done — averaging one
            # reading with nothing would invent a figure for a missed check.
            am_t, pm_t = _reading_temp(am), _reading_temp(pm)
            cells = [am_t, pm_t,
                     None if (am_t is None or pm_t is None)
                     else round((am_t + pm_t) / 2.0, 1)]
            for r in (am, pm):
                v = _reading_temp(r)
                if r:
                    if v is not None and (v > hi_lim or v < lo_lim):
                        breach = True
                    if r.get('note'):
                        notes.append(r['note'])
                    if r.get('recorded_by') and r['recorded_by'] not in by:
                        by.append(r['recorded_by'])
                    if r.get('initials'):
                        inits.append(r['initials'])
                    if r.get('check_time'):
                        times.append(r['check_time'])
            for label, r in (('Morning', am), ('Afternoon', pm)):
                if r:
                    ids.append((label, str(r['_id'])))
            rows.append({'day': d, 'cells': cells, 'breach': breach, 'ids': ids,
                         'note': '; '.join(notes), 'by': ', '.join(by),
                         'initials': ' / '.join(inits), 'times': ' / '.join(times)})

        if request.values.get('export') == 'csv':
            # One row per reading rather than the chart's grid: a grid is for
            # looking at, a row per reading is what a spreadsheet can count.
            return _register_csv_response('temperature', readings)

        # Every room's chart for the month, on one page. Choosing a room one
        # at a time meant four page loads to see whether anything had drifted
        # out of range anywhere, and the question asked of this page is
        # almost always "is everything all right?" — which is a question
        # about all of them at once. One query for the month, split by room.
        month_readings = list(logs.find({'date': {'$gte': start, '$lt': end}})
                              .sort([('date', 1), ('session', 1)]))
        charts: Docs = []
        for loc_name in TEMP_LOCATIONS:
            loc_readings = [r for r in month_readings
                            if r.get('location') == loc_name]
            lo_l, hi_l = TEMP_LIMITS.get(loc_name, (15.0, 25.0))
            loc_breaches = sum(
                1 for r in loc_readings
                if (v := _reading_temp(r)) is not None and (v > hi_l or v < lo_l))
            loc_thermo = TEMP_THERMOMETERS.get(loc_name, '')
            if not loc_thermo:
                for r in sorted(loc_readings, key=lambda r: r['timestamp'],
                                reverse=True):
                    if r.get('thermometer'):
                        loc_thermo = r['thermometer']
                        break
            charts.append({
                'location': loc_name, 'lo': lo_l, 'hi': hi_l,
                'recorded': len(loc_readings), 'breaches': loc_breaches,
                'thermometer': loc_thermo,
                'svg': _temperature_chart(loc_readings, year, month,
                                          loc_name, loc_thermo),
            })

        return render_template(
            'temperature.html', nav_links=get_nav_links(), message=message,
            message_class=message_class, is_admin=is_admin,
            locations=TEMP_LOCATIONS, sessions=TEMP_SESSIONS,
            location=location, today=today.strftime('%Y-%m-%d'),
            month_value=f'{year:04d}-{month:02d}',
            month_label=start.strftime('%B %Y'),
            limit_lo=lo_lim, limit_hi=hi_lim,
            days_in_month=days_in_month, days_complete=days_complete,
            recorded_count=len(readings), expected_count=days_in_month * 2,
            breach_count=breach_count,
            rows=rows, thermometer=thermometer, default_initials=default_initials,
            round_rows=round_rows, round_session=round_session,
            round_time=now_local.strftime('%H:%M'),
            round_date_label=now_local.strftime('%A %d %B %Y'),
            charts=charts,
            chart=_temperature_chart(readings, year, month, location, thermometer))

    except ServerSelectionTimeoutError:
        return render_template(
            'temperature.html', nav_links=get_nav_links(),
            message='Database connection failed. Please try again later.',
            message_class='error', is_admin=is_admin,
            locations=TEMP_LOCATIONS, sessions=TEMP_SESSIONS,
            location=TEMP_LOCATIONS[0], today=today.strftime('%Y-%m-%d'),
            month_value=f'{today.year:04d}-{today.month:02d}',
            month_label=today.strftime('%B %Y'),
            limit_lo=15.0, limit_hi=25.0, days_in_month=30, days_complete=0,
            recorded_count=0, expected_count=60, breach_count=0,
            rows=[], thermometer='', default_initials='',
            round_rows=[], round_session=local_session(),
            round_time=local_now().strftime('%H:%M'),
            round_date_label=local_now().strftime('%A %d %B %Y'),
            charts=[], chart=''), 500


# ============================================================================
# EQUIPMENT LOANS — crutches, moon boots, arm slings
# ============================================================================
# These are NOT stock. A dispensed tablet is gone; a loaned crutch is still
# owned by the pharmacy while it sits in a patient's house. That difference
# drives every design decision here:
#
#   * Items live in their own register (loan_items), one document per physical
#     item, so "who has CR-003 and since when" is answerable. A pool count
#     cannot answer it, and a pool count is useless the moment something is
#     six weeks overdue and you need to go and find it.
#
#   * Availability is DERIVED, never stored. loan_items.status holds only the
#     shelf states (Available / Damaged / Retired / Lost). Whether an item is
#     out is decided by the loans ledger alone. Storing an "on loan" flag as
#     well would give two sources of truth that drift the first time a request
#     fails halfway, and the register would start lying about what is on the
#     shelf. This is the same rule the stock ledger follows.

LOAN_ITEM_TYPES = ['Crutches', 'Moon Boot', 'Arm Sling',
                   'Walking Frame', 'Wheelchair', 'Other']
# Prefix for auto-generated item codes, so a code reads as what it is.
LOAN_TYPE_PREFIX = {'Crutches': 'CR', 'Moon Boot': 'MB', 'Arm Sling': 'AS',
                    'Walking Frame': 'WF', 'Wheelchair': 'WC', 'Other': 'EQ'}
LOAN_CONDITIONS = ['Good', 'Fair', 'Damaged']
# Shelf states. "On loan" is deliberately absent — see the note above.
LOAN_SHELF_STATUSES = ['Available', 'Damaged', 'Retired', 'Lost']
LOAN_STATUS_OUT, LOAN_STATUS_RETURNED = 'Out', 'Returned'
LOAN_STATUS_LOST = 'Lost'
LOAN_DEFAULT_DAYS = 14          # typical fracture follow-up interval
LOAN_MAX_DAYS = 365


def _loan_next_code(items: Collection, item_type: str) -> str:
    """Next free code for a type, e.g. CR-004.

    Scans existing codes rather than keeping a counter, so a deleted or
    manually-typed code can never cause a collision with a generated one.
    """
    prefix = LOAN_TYPE_PREFIX.get(item_type, 'EQ')
    used = set()
    for doc in items.find({'item_code': {'$regex': f'^{prefix}-'}},
                          {'item_code': 1}):
        tail = str(doc.get('item_code', '')).split('-', 1)[-1]
        if tail.isdigit():
            used.add(int(tail))
    n = 1
    while n in used:
        n += 1
    return f'{prefix}-{n:03d}'


def _open_loans_by_code(loans: Collection) -> dict[str, Doc]:
    """{item_code: open loan} — the single source of truth for 'is it out'."""
    return {doc['item_code']: doc
            for doc in loans.find({'status': LOAN_STATUS_OUT})}


def _loan_rows(items: Collection, loans: Collection, today: date) -> Docs:
    """The asset register with availability folded in.

    One pass over each collection, so the page cost does not grow with the
    number of items the way a per-item lookup would.
    """
    open_by_code = _open_loans_by_code(loans)
    rows = []
    for it in items.find().sort('item_code', 1):
        code = it.get('item_code', '')
        loan = open_by_code.get(code)
        shelf = it.get('status', 'Available')
        if loan:
            due = loan.get('due_date')
            overdue = bool(due and due.date() < today)
            state = 'Overdue' if overdue else 'On loan'
        else:
            state = shelf
            overdue = False
        rows.append({
            'code': code,
            'item_type': it.get('item_type', ''),
            'description': it.get('description', ''),
            'size': it.get('size', ''),
            'condition': it.get('condition', ''),
            'shelf': shelf,
            'state': state,
            'overdue': overdue,
            'loan': loan,
            'note': it.get('note', ''),
            'id': str(it['_id']),
        })
    return rows


def _loan_available(items: Collection, loans: Collection) -> Docs:
    """Items that can be lent right now: on the shelf AND not already out."""
    out = set(_open_loans_by_code(loans))
    return [it for it in items.find({'status': 'Available'}).sort('item_code', 1)
            if it.get('item_code') not in out]


def _loan_activity(loans: Collection, months: int = 6) -> Docs:
    """Loans issued / returned / still out, by month, in one aggregation."""
    today = datetime.utcnow().date()
    first = today.replace(day=1)
    span = []
    y, m = first.year, first.month
    for _ in range(months):
        span.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    span.reverse()
    start = datetime(span[0][0], span[0][1], 1)

    issued: dict[tuple[int, int], int] = {}
    returned: dict[tuple[int, int], int] = {}
    days_out: dict[tuple[int, int], list[int]] = {}
    for doc in loans.find({'issued_date': {'$gte': start}}):
        d = doc.get('issued_date')
        if not d:
            continue
        key = (d.year, d.month)
        issued[key] = issued.get(key, 0) + 1
    for doc in loans.find({'returned_date': {'$gte': start}}):
        d = doc.get('returned_date')
        if not d:
            continue
        key = (d.year, d.month)
        returned[key] = returned.get(key, 0) + 1
        iss = doc.get('issued_date')
        if iss:
            days_out.setdefault(key, []).append((d - iss).days)

    rows = []
    for key in span:
        spans = days_out.get(key, [])
        rows.append({
            'label': datetime(key[0], key[1], 1).strftime('%b %Y'),
            'issued': issued.get(key, 0),
            'returned': returned.get(key, 0),
            'avg_days': round(sum(spans) / len(spans), 1) if spans else None,
        })
    return rows


@app.route('/loan-items', methods=['GET', 'POST'])
@login_required
def loan_items() -> ResponseReturnValue:
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can change the equipment register.')
        return redirect(url_for('loans'))

    message_class, message = _flashed_banner()
    today = datetime.utcnow().date()

    try:
        db = get_mongo_client()['pharmacy_db']
        items, loans_col = db['loan_items'], db['loans']

        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'add_item':
                item_type = (request.form.get('item_type') or '').strip()
                if item_type not in LOAN_ITEM_TYPES:
                    flash('Choose a valid item type.')
                    return redirect(url_for('loan_items'))
                condition = (request.form.get('condition') or 'Good').strip()
                if condition not in LOAN_CONDITIONS:
                    condition = 'Good'
                code = (request.form.get('item_code') or '').strip().upper()
                if not code:
                    code = _loan_next_code(items, item_type)
                # A duplicate code would give two physical items one identity,
                # and every loan against it would be ambiguous from then on.
                if items.find_one({'item_code': code}):
                    flash(f'Item code {code} is already in the register.')
                    return redirect(url_for('loan_items'))
                items.insert_one({
                    'item_code': code,
                    'item_type': item_type,
                    'size': (request.form.get('size') or '').strip(),
                    'condition': condition,
                    'status': 'Damaged' if condition == 'Damaged' else 'Available',
                    'note': (request.form.get('note') or '').strip(),
                    'created_by': session['user']['name'],
                    'created_at': datetime.utcnow(),
                })
                write_audit_entry('CREATE', 'loan_item', code,
                                  f'added {item_type} condition {condition}')
                flash(f'{item_type} {code} added to the register.', 'success')
                return redirect(url_for('loan_items'))

            if action == 'set_status':
                try:
                    doc = items.find_one({'_id': ObjectId(request.form.get('item_id', ''))})
                except (InvalidId, TypeError):
                    doc = None
                if not doc:
                    flash('That item was not found.')
                    return redirect(url_for('loan_items'))
                new_status = (request.form.get('status') or '').strip()
                if new_status not in LOAN_SHELF_STATUSES:
                    flash('Choose a valid status.')
                    return redirect(url_for('loan_items'))
                # An item that is physically with a patient cannot be retired
                # or marked available — its state is the loan's to decide until
                # it comes back.
                if loans_col.find_one({'item_code': doc['item_code'],
                                       'status': LOAN_STATUS_OUT}):
                    flash(f"{doc['item_code']} is out on loan. Take it back in "
                          f'first, then change its status.')
                    return redirect(url_for('loan_items'))
                was = doc.get('status', 'Available')
                items.update_one({'_id': doc['_id']},
                                 {'$set': {'status': new_status}})
                write_audit_entry('UPDATE', 'loan_item', doc['item_code'],
                                  f'status {was} -> {new_status}')
                flash(f"{doc['item_code']} set to {new_status}.", 'success')
                return redirect(url_for('loan_items'))

            flash('Unknown action.')
            return redirect(url_for('loan_items'))

        return render_template(
            'loan_items.html',
            nav_links=get_nav_links(),
            message=message, message_class=message_class,
            rows=_loan_rows(items, loans_col, today),
            item_types=LOAN_ITEM_TYPES,
            conditions=LOAN_CONDITIONS,
            shelf_statuses=LOAN_SHELF_STATUSES,
        )
    except Exception as e:
        app.logger.error(f'loan_items failed: {e}')
        flash('Could not load the equipment register.')
        return redirect(url_for('loans'))


@app.route('/loans', methods=['GET', 'POST'])
@login_required
def loans() -> ResponseReturnValue:
    current_user = session['user']['name']
    message_class, message = _flashed_banner()
    is_admin = session['user'].get('role') == 'admin'
    today = datetime.utcnow().date()

    try:
        db = get_mongo_client()['pharmacy_db']
        items, loans_col = db['loan_items'], db['loans']

        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'issue':
                code = (request.form.get('item_code') or '').strip().upper()
                item = items.find_one({'item_code': code})
                if not item:
                    flash(f'Item code {code} is not in the equipment register.')
                    return redirect(url_for('loans'))
                if item.get('status') != 'Available':
                    flash(f"{code} is marked {item.get('status')} and cannot "
                          f'be lent out.')
                    return redirect(url_for('loans'))
                # The double-issue guard. Without it a refreshed POST, or two
                # staff at two screens, lends one physical crutch pair to two
                # patients and the register can never be reconciled again.
                if loans_col.find_one({'item_code': code, 'status': LOAN_STATUS_OUT}):
                    flash(f'{code} is already out on loan. Take it back in first.')
                    return redirect(url_for('loans'))

                patient = (request.form.get('patient_name') or '').strip()
                if not patient:
                    flash('A patient name is required — the loan has to be '
                          'traceable to someone.')
                    return redirect(url_for('loans'))
                try:
                    issued = datetime.strptime(request.form['issued_date'], '%Y-%m-%d')
                    due = datetime.strptime(request.form['due_date'], '%Y-%m-%d')
                except (KeyError, ValueError):
                    flash('Enter valid issue and due dates.')
                    return redirect(url_for('loans'))
                if issued.date() > today:
                    flash('A loan cannot be dated in the future.')
                    return redirect(url_for('loans'))
                if due < issued:
                    flash('The due date cannot be before the issue date.')
                    return redirect(url_for('loans'))
                if (due - issued).days > LOAN_MAX_DAYS:
                    flash(f'A loan longer than {LOAN_MAX_DAYS} days is probably '
                          f'a typing error. Shorten it or write the item off.')
                    return redirect(url_for('loans'))

                condition_out = (request.form.get('condition_out') or 'Good').strip()
                if condition_out not in LOAN_CONDITIONS:
                    condition_out = 'Good'
                ref = f"LN-{datetime.utcnow():%Y%m%d}-{uuid4().hex[:4].upper()}"
                loans_col.insert_one({
                    'loan_ref': ref,
                    'item_code': code,
                    'item_type': item.get('item_type', ''),
                    'patient_name': patient,
                    'patient_contact': (request.form.get('patient_contact') or '').strip(),
                    'patient_id': (request.form.get('patient_id') or '').strip(),
                    'issued_date': issued,
                    'due_date': due,
                    'issued_by': current_user,
                    'condition_out': condition_out,
                    'returned_date': None,
                    'returned_to': None,
                    'condition_in': None,
                    'status': LOAN_STATUS_OUT,
                    'note': (request.form.get('note') or '').strip(),
                    'created_at': datetime.utcnow(),
                })
                write_audit_entry('CREATE', 'loan', ref,
                                  f'{code} issued to {patient}, due {due:%Y-%m-%d}')
                flash(f'{code} issued to {patient}. Due back {due:%Y-%m-%d}. '
                      f'Reference {ref}.', 'success')
                return redirect(url_for('loans'))

            if action == 'return':
                try:
                    loan = loans_col.find_one({'_id': ObjectId(request.form.get('loan_id', ''))})
                except (InvalidId, TypeError):
                    loan = None
                if not loan:
                    flash('That loan was not found.')
                    return redirect(url_for('loans'))
                if loan.get('status') != LOAN_STATUS_OUT:
                    flash(f"That loan is already closed ({loan.get('status')}).")
                    return redirect(url_for('loans'))
                condition_in = (request.form.get('condition_in') or 'Good').strip()
                if condition_in not in LOAN_CONDITIONS:
                    condition_in = 'Good'
                now = datetime.utcnow()
                loans_col.update_one({'_id': loan['_id']}, {'$set': {
                    'returned_date': now,
                    'returned_to': current_user,
                    'condition_in': condition_in,
                    'status': LOAN_STATUS_RETURNED,
                }})
                # An item that comes back broken must not fall straight back
                # into the available pool, or it gets lent to the next patient.
                shelf = 'Damaged' if condition_in == 'Damaged' else 'Available'
                items.update_one({'item_code': loan['item_code']},
                                 {'$set': {'status': shelf,
                                           'condition': condition_in}})
                days = (now - loan['issued_date']).days
                write_audit_entry('UPDATE', 'loan', loan['loan_ref'],
                                  f"returned by {loan['patient_name']} after "
                                  f'{days} days, condition {condition_in}')
                extra = (' It is marked damaged and will not be offered for '
                         'loan until you clear it.' if condition_in == 'Damaged' else '')
                flash(f"{loan['item_code']} returned after {days} days.{extra}",
                      'success' if condition_in != 'Damaged' else 'partial')
                return redirect(url_for('loans'))

            if action == 'mark_lost':
                if not is_admin:
                    flash('Access denied. Only admins can write an item off as lost.')
                    return redirect(url_for('loans'))
                try:
                    loan = loans_col.find_one({'_id': ObjectId(request.form.get('loan_id', ''))})
                except (InvalidId, TypeError):
                    loan = None
                if not loan:
                    flash('That loan was not found.')
                    return redirect(url_for('loans'))
                if loan.get('status') != LOAN_STATUS_OUT:
                    flash(f"That loan is already closed ({loan.get('status')}).")
                    return redirect(url_for('loans'))
                now = datetime.utcnow()
                loans_col.update_one({'_id': loan['_id']}, {'$set': {
                    'returned_date': None,
                    'status': LOAN_STATUS_LOST,
                    'closed_date': now,
                    'closed_by': current_user,
                }})
                items.update_one({'item_code': loan['item_code']},
                                 {'$set': {'status': 'Lost'}})
                write_audit_entry('UPDATE', 'loan', loan['loan_ref'],
                                  f"written off as lost with {loan['patient_name']}")
                flash(f"{loan['item_code']} written off as lost.", 'partial')
                return redirect(url_for('loans'))

            flash('Unknown action.')
            return redirect(url_for('loans'))

        # ---- GET ----------------------------------------------------------
        q = (request.args.get('q') or '').strip()
        status_filter = (request.args.get('status') or '').strip()

        rows = _loan_rows(items, loans_col, today)
        out_rows = []
        for doc in loans_col.find({'status': LOAN_STATUS_OUT}).sort('due_date', 1):
            due = doc.get('due_date')
            overdue = bool(due and due.date() < today)
            out_rows.append({
                'id': str(doc['_id']),
                'loan_ref': doc.get('loan_ref', ''),
                'item_code': doc.get('item_code', ''),
                'item_type': doc.get('item_type', ''),
                'patient_name': doc.get('patient_name', ''),
                'patient_contact': doc.get('patient_contact', ''),
                'patient_id': doc.get('patient_id', ''),
                # Guarded for the same reason as the dispensing register: one
                # old record without this field must not take the page down.
                'issued_date': doc.get('issued_date'),
                'due_date': due,
                'issued_by': doc.get('issued_by', ''),
                'days_out': (datetime.utcnow() - doc['issued_date']).days,
                'days_over': (today - due.date()).days if overdue else 0,
                'overdue': overdue,
            })

        query: Query = {}
        if status_filter in (LOAN_STATUS_OUT, LOAN_STATUS_RETURNED, LOAN_STATUS_LOST):
            query['status'] = status_filter
        if q:
            rx = {'$regex': re.escape(q), '$options': 'i'}
            query['$or'] = [{'patient_name': rx}, {'item_code': rx},
                            {'patient_id': rx}, {'loan_ref': rx}]
        history = []
        for doc in loans_col.find(query).sort('issued_date', -1).limit(300):
            ret = doc.get('returned_date')
            history.append({
                'loan_ref': doc.get('loan_ref', ''),
                'item_code': doc.get('item_code', ''),
                'patient_name': doc.get('patient_name', ''),
                'issued_date': doc.get('issued_date'),
                'due_date': doc.get('due_date'),
                'returned_date': ret,
                'days': ((ret - doc['issued_date']).days
                         if ret and doc.get('issued_date') else None),
                'condition_out': doc.get('condition_out'),
                'condition_in': doc.get('condition_in'),
                'status': doc.get('status', ''),
                'issued_by': doc.get('issued_by', ''),
                'note': doc.get('note', ''),
            })

        tiles = {
            'available': sum(1 for r in rows if r['state'] == 'Available'),
            'out': sum(1 for r in rows if r['loan']),
            'overdue': sum(1 for r in rows if r['overdue']),
            'unusable': sum(1 for r in rows
                            if not r['loan'] and r['shelf'] in ('Damaged', 'Retired', 'Lost')),
        }

        if request.values.get('export') == 'csv':
            # history already holds every loan the filter matches, out and
            # returned alike; out_rows is a subset of it shown separately on
            # screen, so adding both listed the open loans twice.
            return _register_csv_response('loans', history)

        return render_template(
            'loans.html',
            nav_links=get_nav_links(),
            message=message, message_class=message_class,
            is_admin=is_admin,
            today=today.strftime('%Y-%m-%d'),
            default_due=(today + timedelta(days=LOAN_DEFAULT_DAYS)).strftime('%Y-%m-%d'),
            available=_loan_available(items, loans_col),
            conditions=LOAN_CONDITIONS,
            out_rows=out_rows,
            overdue_count=tiles['overdue'],
            history=history,
            activity=_loan_activity(loans_col),
            tiles=tiles,
            q=q, status_filter=status_filter,
        )
    except Exception as e:
        app.logger.error(f'loans failed: {e}')
        flash('Could not load the loans page.')
        return redirect('/reports')


# ---------------------------------------------------------------------------
# The pharmacist is away from Friday late morning until Monday midday, and the
# store key stays with whichever nursing officer locks up. That is the normal
# arrangement, not an exception, so the register must not raise an alarm every
# single weekend — an alarm that cries wolf weekly is one nobody reads.
#
# Any key issued on a Friday, Saturday or Sunday is therefore expected back
# when he returns, and only counts as overdue after that. The cost of this is
# that a Friday-MORNING issue is not chased until Monday either; that is the
# deliberate trade, because he is still on the premises until he leaves and
# would see it himself, whereas a false alarm every weekend costs the flag its
# meaning. Public holidays are not modelled.
# ---------------------------------------------------------------------------
# Seeded on first use so the roster is usable immediately. Everyone here is
# marked an officer in charge EXCEPT the Nurse Assistant, who may operate the
# app but is not in charge. These names were taken from the staff already
# named elsewhere in the app, so the roles are a starting guess — an admin
# edits them on the page.
# How each kind of movement affects a controlled drug balance.
#
# This used to be inferred from the SIGN stored on the transaction, and the
# signs are not consistent: a dispense and an internal issue are stored
# positive, while a damage and an expiry removal are stored negative. The
# register added anything that was not a receipt or a dispense, so issuing
# morphine to a department made the register's running balance go UP. Checked
# against a seeded month, the register ended at 85 where the shelf said 55.
#
# A controlled drug register is a legal document. The direction of a movement
# is therefore stated here once, by type, and the quantity is taken as a
# magnitude — so no change to a storage convention anywhere else in this file
# can silently invert a morphine balance again.
CONTROLLED_DIRECTION = {
    'opening_balance': +1,
    'receive': +1,
    'dispense': -1,
    'internal_issue': -1,
    'damage': -1,
    'expiry_removal': -1,
    'adjustment': 0,      # genuinely either way: the sign IS the information
}

# What a register has to call these in words. "expiry_removal" is a column in
# a database, not an entry in a register someone may have to answer for.
CONTROLLED_MOVEMENT_LABEL = {
    'opening_balance': 'Opening balance',
    'receive': 'Received',
    'dispense': 'Dispensed to patient',
    'internal_issue': 'Issued to department',
    'damage': 'Damaged — removed',
    'expiry_removal': 'Expired — removed',
    'adjustment': 'Stock-take adjustment',
}


def _controlled_delta(tx: Doc) -> float:
    """What this movement does to the balance: negative means it left."""
    direction = CONTROLLED_DIRECTION.get(tx.get('type') or '', 0)
    qty = tx.get('quantity', 0) or 0
    if direction == 0:
        return qty              # an adjustment carries its own sign
    return direction * abs(qty)


# One roster, one person, three jobs.
#
# The same people were listed in FOUR places: a prescribers list and a
# dispensers list written into the dispensing page, this key-register roster,
# and the login accounts. They already disagreed — the roster had seven names
# and the other two had nine each — and adding a nurse meant editing all four.
# Miss one and someone who can log in cannot be selected as the dispenser, so
# their visit is recorded under another person's name or not at all.
#
# The flags below are what the old lists contained, person for person, so
# nothing changes about who may do what. The collection is still called
# key_officers because that is what it is called in every pharmacy database
# already running; renaming it would be a migration for no gain.
#
#   name, role, in charge of the key, may prescribe, may dispense
STAFF_SEED = [
    ('Dr. T. Khothatso', 'Doctor', False, True, False),
    ('Letlotlo Hlaoli', 'Pharmacist', False, False, True),
    ('Locum Doctor', 'Doctor', False, True, False),
    ('Locum Nurse', 'Nursing Officer', True, True, True),
    ('Locum Pharmacist', 'Pharmacist', False, False, True),
    ('Malesoetsa Leohla', 'Nursing Officer', True, True, True),
    ('Mamosa Seetsa', 'Nurse Assistant', False, True, True),
    ('Mamosaase Nqosa', 'Nursing Officer', True, True, True),
    ('Mapalo Mapesela', 'Nursing Officer', True, True, True),
    ('Mathuto Kutoane', 'Nursing Officer', True, True, True),
    ('Thapelo Mphole', 'Nursing Officer', True, True, True),
]
# Kept for the tests and anything that still refers to it.
KEY_OFFICER_SEED = [(n, r, c) for n, r, c, _p, _d in STAFF_SEED]


def _seed_key_officers(officers: Collection) -> None:
    """Put the roster in place, and fill in anything it has not got yet.

    This runs on a pharmacy that has been using the app for months, so it
    must never overwrite a decision an administrator has already made. It
    adds people who are missing, and sets a job flag only where that flag
    has never been set at all. Somebody marked inactive stays inactive.
    """
    try:
        existing = {o.get('name'): o for o in officers.find(
            {}, {'name': 1, 'prescriber': 1, 'dispenser': 1})}
    except Exception as e:
        app.logger.warning(f'staff roster unavailable: {e}')
        return
    to_add, to_set = [], []
    for name, role, in_charge, prescriber, dispenser in STAFF_SEED:
        found = existing.get(name)
        if found is None:
            to_add.append({'name': name, 'role': role, 'in_charge': in_charge,
                           'prescriber': prescriber, 'dispenser': dispenser,
                           'active': True, 'created_at': datetime.utcnow(),
                           'seeded': True})
            continue
        fill = {}
        if 'prescriber' not in found:
            fill['prescriber'] = prescriber
        if 'dispenser' not in found:
            fill['dispenser'] = dispenser
        if fill:
            to_set.append((found['_id'], fill))
    try:
        if to_add:
            officers.insert_many(to_add)
        for _id, fill in to_set:
            officers.update_one({'_id': _id}, {'$set': fill})
    except Exception as e:
        app.logger.warning(f'staff roster could not be completed: {e}')


def _staff_named_for(db: Database | None, job: str) -> list[str]:
    """Everyone on the roster who may do this job, in name order.

    Asked of the roster rather than written into the page, so adding a nurse
    is one entry in one place and she appears everywhere she should.
    """
    try:
        if db is None:
            raise ServerSelectionTimeoutError('no database on this path')
        officers = db['key_officers']
        _seed_key_officers(officers)
        return sorted(
            (o['name'] for o in officers.find(
                {'active': True, job: True}, {'name': 1})
             if o.get('name')),
            key=str.lower)
    except Exception as e:
        app.logger.warning(f'staff list for {job} unavailable: {e}')
        # Better a usable form with the roster as it was last known than a
        # dispensing page nobody can fill in.
        return sorted((n for n, _r, _c, p, d in STAFF_SEED
                       if (p if job == 'prescriber' else d)), key=str.lower)


def _officer_names(officers: Collection, in_charge_only: bool = True) -> list[str]:
    q = {'active': True}
    if in_charge_only:
        q['in_charge'] = True
    return [d['name'] for d in officers.find(q).sort('name', 1)]


def _is_in_charge(officers: Collection, name: str | None) -> bool:
    """Is this person an officer in charge? Unknown names are not."""
    if not name:
        return False
    doc = officers.find_one({'name': name, 'active': True})
    return bool(doc and doc.get('in_charge'))


def _user_in_charge(officers: Collection, name: str | None, is_admin: bool) -> bool:
    """Whether the logged-in user may be named as the officer in charge.

    The roster decides for anybody on it — that is the point of the flag, and
    it must hold even if such a person also has an admin login. Somebody not
    on the roster at all falls back to their role, so the pharmacist and
    managers are not locked out of their own key register for want of a
    roster entry.
    """
    doc = officers.find_one({'name': name, 'active': True})
    if doc:
        return bool(doc.get('in_charge'))
    return bool(is_admin)


# Taking the key and taking it back are not the same job, and for a while the
# app asked the same question for both.
#
# "In charge" means the nursing officer who holds the store key while the
# pharmacist is away. The pharmacist is deliberately not one of those — he is
# the one handing it over — so he is on the roster with the flag off, which is
# right. But the return form used that same flag to decide who could be named
# as RECEIVING the key back, and receiving it back is precisely the
# pharmacist's job. The result was that the pharmacist could not find his own
# name anywhere on the return, and the one-click button was hidden from him:
# locked out of his own register by a correct entry on the roster.
#
# So receiving is its own question. Anyone who may hold the key may also take
# it back — an officer in charge can receive it from another over a long
# weekend — and so may a pharmacist. Nobody who could name themselves before
# has lost anything; the pharmacist has been added.
def _is_pharmacist(doc: Doc | None) -> bool:
    return bool(doc) and 'pharmacist' in str((doc or {}).get('role', '')).lower()


def _key_receiver_names(officers: Collection) -> list[str]:
    """Who may be named as receiving the key back, or as the pharmacist on it."""
    return sorted(
        {d['name'] for d in officers.find({'active': True})
         if d.get('name') and (d.get('in_charge') or _is_pharmacist(d))},
        key=str.lower)


def _user_may_receive(officers: Collection, name: str | None,
                      is_admin: bool) -> bool:
    """Whether the logged-in user may be named as receiving the key back."""
    doc = officers.find_one({'name': name, 'active': True})
    if doc:
        return bool(doc.get('in_charge')) or _is_pharmacist(doc)
    return bool(is_admin)


KEY_AWAY_WEEKDAY = 4          # Friday (Monday = 0)
KEY_RETURN_WEEKDAY = 0        # Monday
KEY_AWAY_TIME = (11, 30)      # he leaves
KEY_RETURN_TIME = (12, 0)     # he is back


def _key_due_back(issued_at: datetime) -> datetime:
    """When an issue is expected back."""
    wd = issued_at.weekday()
    if wd in (KEY_AWAY_WEEKDAY, 5, 6):           # Fri, Sat, Sun
        days_ahead = {KEY_AWAY_WEEKDAY: 3, 5: 2, 6: 1}[wd]
        monday = issued_at + timedelta(days=days_ahead)
        return monday.replace(hour=KEY_RETURN_TIME[0], minute=KEY_RETURN_TIME[1],
                              second=0, microsecond=0)
    # Any other day: back before the day is out.
    return issued_at.replace(hour=23, minute=59, second=59, microsecond=0)


def _key_is_weekend_hold(issued_at: datetime) -> bool:
    """True when this issue spans the pharmacist's absence."""
    return issued_at.weekday() in (KEY_AWAY_WEEKDAY, 5, 6)


def _held_for(since: datetime, now: datetime) -> str:
    """Plain-language gap, e.g. '4h 20m' or '2 days 3h'."""
    secs = max(0, int((now - since).total_seconds()))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days} day{'' if days == 1 else 's'} {hours}h"
    if hours:
        return f'{hours}h {mins}m'
    return f'{mins}m'


# ---------------------------------------------------------------------------
# The register writes itself from who is logged in.
#
# In practice nobody fills this form in. The pharmacist logs out on Friday,
# whoever logs in next is holding the key, and when he comes back he clicks
# Returned. So the login and logout are the handover, and the register simply
# records what they already say.
#
# Everything here is best-effort and wrapped: a key register that could stop
# somebody logging in would be a far worse problem than a missing row, so a
# failure in here is logged and swallowed.
# ---------------------------------------------------------------------------
def _key_control(db: Database) -> Collection:
    return db['key_control']


def _key_arm_handover(user_name: str) -> None:
    """The pharmacist has logged out, so the key is up for handover."""
    try:
        db = get_mongo_client()['pharmacy_db']
        if db['key_register'].find_one({'status': 'Out'}):
            return                      # already with somebody
        _key_control(db).update_one(
            {'_id': 'state'},
            {'$set': {'armed': True, 'armed_by': user_name,
                      'armed_at': local_now()}},
            upsert=True)
    except Exception as e:
        app.logger.warning(f'key handover arm failed: {e}')


def _key_in_away_window(now: datetime) -> bool:
    """Between the pharmacist leaving on Friday and returning on Monday.

    A safety net for the Friday he closes the browser instead of logging out,
    which will happen. Without it the weekend handover simply would not be
    recorded, which is the one it most matters for.
    """
    wd = now.weekday()
    if wd == KEY_AWAY_WEEKDAY:
        return (now.hour, now.minute) >= KEY_AWAY_TIME
    if wd in (5, 6):
        return True
    if wd == KEY_RETURN_WEEKDAY:
        return (now.hour, now.minute) < KEY_RETURN_TIME
    return False


def _key_open_issue(keys: Collection, officers: Collection, holder: str, pharmacist: str,
                    now: datetime, source: str) -> str:
    """Start a row for `holder`, filled in from what the login already says."""
    ref = f"KEY-{now:%Y%m%d}-{uuid4().hex[:4].upper()}"
    keys.insert_one({
        'ref': ref,
        'issued_date': datetime(now.year, now.month, now.day),
        'issued_time': now.strftime('%H:%M'),
        'issued_at': now,
        'pharmacist': pharmacist,
        'recipient_name': holder,
        'returned_date': None, 'returned_time': None, 'returned_at': None,
        'returning_officer': None, 'receiving_officer': None,
        'return_pharmacist': None,
        'comments': '',
        'status': 'Out',
        'issued_by_user': holder,
        'due_back': _key_due_back(now),
        'weekend_hold': _key_is_weekend_hold(now),
        'recipient_off_roster': not _is_in_charge(officers, holder),
        'auto_source': source,
        'created_at': datetime.utcnow(),
    })
    write_audit_entry('CREATE', 'store_key', ref,
                      f'key recorded to {holder} automatically ({source}) '
                      f'at {now:%Y-%m-%d %H:%M}')
    return ref


def _key_on_login(user_name: str, is_admin: bool) -> None:
    """Called once per successful login. Keeps the register in step."""
    try:
        db = get_mongo_client()['pharmacy_db']
        keys, officers = db['key_register'], db['key_officers']
        _seed_key_officers(officers)
        now = local_now()
        open_doc = keys.find_one({'status': 'Out'})

        if open_doc:
            holder = open_doc.get('recipient_name')
            if holder == user_name:
                return                  # same person, still holding it
            if is_admin:
                # He is back. The key is not his again until he says so, so
                # the row stays open and he closes it with one click.
                return
            # Somebody else has come on duty. One issue covers the whole
            # absence, so the row stays as it is and is NOT split: the key
            # was handed over once, when the pharmacist left.
            #
            # Their name is still noted against that row, though. The app
            # knows they were on duty, and quietly dropping it would leave
            # the register asserting that one person held the key for three
            # days when it knows others were there too.
            already = open_doc.get('also_on_duty') or []
            if user_name not in already:
                keys.update_one(
                    {'_id': open_doc['_id']},
                    {'$addToSet': {'also_on_duty': user_name}})
                write_audit_entry(
                    'UPDATE', 'store_key', open_doc.get('ref', ''),
                    f'{user_name} also on duty during this issue '
                    f'(key remains recorded to {holder})')
            return

        # Key is not out.
        state = _key_control(db).find_one({'_id': 'state'}) or {}
        if is_admin:
            # He is here, so he has it; nothing is outstanding.
            if state.get('armed'):
                _key_control(db).update_one({'_id': 'state'},
                                            {'$set': {'armed': False}})
            return
        if state.get('armed') or _key_in_away_window(now):
            _key_open_issue(keys, officers, user_name,
                            state.get('armed_by') or _key_last_pharmacist(keys),
                            now, 'login' if state.get('armed') else 'away-window')
            _key_control(db).update_one({'_id': 'state'},
                                        {'$set': {'armed': False}}, upsert=True)
    except Exception as e:
        app.logger.warning(f'key register login hook failed: {e}')


def _key_last_pharmacist(keys: Collection) -> str:
    """Whoever last handed the key over, for filling the pharmacist column."""
    last = keys.find_one({'pharmacist': {'$nin': [None, '']}},
                         sort=[('issued_at', -1)])
    return (last or {}).get('pharmacist', '')


@app.route('/key-register', methods=['GET', 'POST'])
@login_required
def key_register() -> ResponseReturnValue:
    current_user = session['user']['name']
    message_class, message = _flashed_banner()
    is_admin = session['user'].get('role') == 'admin'

    try:
        db = get_mongo_client()['pharmacy_db']
        keys = db['key_register']
        officers = db['key_officers']
        _seed_key_officers(officers)
        # Whoever is logged in may be entering the record without being an
        # officer in charge. Their name is kept as the person who typed it,
        # but it is never dropped into a field that asserts authority.
        user_in_charge = _user_in_charge(officers, current_user, is_admin)
        # Receiving the key back is a different question from holding it.
        user_may_receive = _user_may_receive(officers, current_user, is_admin)

        if request.method == 'POST':
            action = request.form.get('action')
            now = local_now()

            if action == 'issue_key':
                # There is ONE physical key. If it is out, it cannot be given
                # to a second person — and a register that allowed it would be
                # unreconcilable afterwards, with two people each believing
                # they held it. This is the whole control.
                open_doc = keys.find_one({'status': 'Out'})
                if open_doc:
                    flash(f"The key is already out with "
                          f"{open_doc.get('recipient_name')} since "
                          f"{open_doc['issued_date']:%Y-%m-%d} "
                          f"{open_doc.get('issued_time')}. Record its return "
                          f"before issuing it again.")
                    return redirect(url_for('key_register'))

                recipient = (request.form.get('recipient_name') or '').strip()
                if recipient == '__other__':
                    recipient = (request.form.get('recipient_other') or '').strip()
                if not recipient:
                    flash('A recipient name is required — the key has to be '
                          'traceable to someone.')
                    return redirect(url_for('key_register'))
                # Only default the pharmacist to the logged-in user when that
                # user is actually an officer in charge. Otherwise the field
                # must be filled in explicitly, so a Nurse Assistant entering
                # the record is never recorded as the one in charge of it.
                pharmacist = (request.form.get('pharmacist') or '').strip()
                if not pharmacist:
                    if user_in_charge:
                        pharmacist = current_user
                    else:
                        flash('Name the officer in charge who handed the key '
                              'over — your own name cannot be used, because '
                              'you are not on the roster as in charge.')
                        return redirect(url_for('key_register'))
                off_roster = not _is_in_charge(officers, recipient)

                ref = f"KEY-{now:%Y%m%d}-{uuid4().hex[:4].upper()}"
                keys.insert_one({
                    'ref': ref,
                    'issued_date': datetime(now.year, now.month, now.day),
                    'issued_time': now.strftime('%H:%M'),
                    'issued_at': now,
                    'pharmacist': pharmacist,
                    'recipient_name': recipient,
                    'returned_date': None, 'returned_time': None,
                    'returned_at': None,
                    'returning_officer': None, 'receiving_officer': None,
                    'return_pharmacist': None,
                    'comments': (request.form.get('comments') or '').strip(),
                    'status': 'Out',
                    'issued_by_user': current_user,
                    'due_back': _key_due_back(now),
                    'weekend_hold': _key_is_weekend_hold(now),
                    'recipient_off_roster': off_roster,
                    'created_at': datetime.utcnow(),
                })
                write_audit_entry('CREATE', 'store_key', ref,
                                  f'key issued to {recipient} by {pharmacist} '
                                  f'at {now:%Y-%m-%d %H:%M}'
                                  + (f' [entered by {current_user}]'
                                     if current_user != pharmacist else ''))
                note = ''
                if off_roster:
                    note = (f' {recipient} is not on the roster as an officer '
                            f'in charge — this entry is flagged for review.')
                if _key_is_weekend_hold(now):
                    due = _key_due_back(now)
                    flash(f'Key issued to {recipient} at {now:%H:%M} as a '
                          f'weekend handover, due back {due:%A} '
                          f'{due:%H:%M}.{note}',
                          'partial' if off_roster else 'success')
                else:
                    flash(f'Key issued to {recipient} at {now:%H:%M}.{note}',
                          'partial' if off_roster else 'success')
                return redirect(url_for('key_register'))

            if action == 'return_key':
                try:
                    doc = keys.find_one({'_id': ObjectId(request.form.get('entry_id', ''))})
                except (InvalidId, TypeError):
                    doc = None
                if not doc:
                    flash('That key register entry was not found.')
                    return redirect(url_for('key_register'))
                if doc.get('status') != 'Out':
                    flash('That entry is already closed — the key was already '
                          'recorded as returned.')
                    return redirect(url_for('key_register'))

                returning = (request.form.get('returning_officer') or '').strip() \
                    or doc.get('recipient_name', '')
                # Same rule as on issue: the logged-in user is only defaulted
                # into an officer field when they are an officer in charge.
                receiving = (request.form.get('receiving_officer') or '').strip()
                ret_pharm = (request.form.get('return_pharmacist') or '').strip()
                if not receiving or not ret_pharm:
                    if user_in_charge:
                        receiving = receiving or current_user
                        ret_pharm = ret_pharm or current_user
                    else:
                        flash('Name the officer in charge receiving the key — '
                              'your own name cannot be used, because you are '
                              'not on the roster as in charge.')
                        return redirect(url_for('key_register'))
                extra = (request.form.get('comments') or '').strip()
                comments = '; '.join(x for x in (doc.get('comments'), extra) if x)

                keys.update_one({'_id': doc['_id']}, {'$set': {
                    'returned_date': datetime(now.year, now.month, now.day),
                    'returned_time': now.strftime('%H:%M'),
                    'returned_at': now,
                    'returning_officer': returning,
                    'receiving_officer': receiving,
                    'return_pharmacist': ret_pharm,
                    'comments': comments,
                    'status': 'Returned',
                    'returned_by_user': current_user,
                }})
                held = _held_for(doc.get('issued_at') or doc['issued_date'], now)
                write_audit_entry('UPDATE', 'store_key', doc.get('ref', ''),
                                  f'key returned by {returning}, received by '
                                  f'{receiving} after {held}')
                flash(f'Key returned by {returning} at {now:%H:%M}, '
                      f'out for {held}.', 'success')
                return redirect(url_for('key_register'))

            if action == 'edit_entry':
                # An entry made by whoever was at the counter may need the
                # officer names put right afterwards. Admin only, and the
                # before-and-after goes to the audit log so the correction is
                # itself on the record.
                if not is_admin:
                    flash('Access denied. Only admins can correct a register entry.')
                    return redirect(url_for('key_register'))
                try:
                    doc = keys.find_one({'_id': ObjectId(request.form.get('entry_id', ''))})
                except (InvalidId, TypeError):
                    doc = None
                if not doc:
                    flash('That key register entry was not found.')
                    return redirect(url_for('key_register'))

                fields = ['pharmacist', 'recipient_name', 'returning_officer',
                          'receiving_officer', 'return_pharmacist', 'comments']
                updates: Doc = {}
                changes: list[str] = []
                for f in fields:
                    if f not in request.form:
                        continue
                    new_val = (request.form.get(f) or '').strip()
                    old_val = doc.get(f) or ''
                    if new_val != old_val:
                        updates[f] = new_val or None
                        changes.append(f"{f}: '{old_val}' -> '{new_val}'")
                for f, label in (('issued_time', 'issued time'),
                                 ('returned_time', 'returned time')):
                    if f not in request.form:
                        continue
                    new_val = (request.form.get(f) or '').strip()[:5]
                    old_val = doc.get(f) or ''
                    if new_val != old_val:
                        updates[f] = new_val or None
                        changes.append(f"{label}: '{old_val}' -> '{new_val}'")

                if not updates:
                    flash('Nothing was changed.')
                    return redirect(url_for('key_register'))
                if 'recipient_name' in updates:
                    updates['recipient_off_roster'] = not _is_in_charge(
                        officers, updates['recipient_name'])
                updates['corrected_by'] = current_user
                updates['corrected_at'] = datetime.utcnow()
                keys.update_one({'_id': doc['_id']}, {'$set': updates})
                write_audit_entry('UPDATE', 'store_key', doc.get('ref', ''),
                                  'entry corrected — ' + '; '.join(changes))
                flash(f"Entry corrected: {'; '.join(changes)}.", 'success')
                return redirect(url_for('key_register'))

            if action == 'add_officer':
                if not is_admin:
                    flash('Access denied. Only admins can change the roster.')
                    return redirect(url_for('key_register'))
                name = (request.form.get('officer_name') or '').strip()
                if not name:
                    flash('A name is required.')
                    return redirect(url_for('key_register'))
                if officers.find_one({'name': name}):
                    flash(f'{name} is already on the roster.')
                    return redirect(url_for('key_register'))
                in_charge = request.form.get('in_charge') == 'yes'
                prescriber = request.form.get('prescriber') == 'yes'
                dispenser = request.form.get('dispenser') == 'yes'
                officers.insert_one({
                    'name': name,
                    'role': (request.form.get('officer_role') or '').strip()
                            or ('Nursing Officer' if in_charge else 'Nurse Assistant'),
                    'in_charge': in_charge,
                    'prescriber': prescriber, 'dispenser': dispenser,
                    'active': True,
                    'created_by': current_user, 'created_at': datetime.utcnow(),
                })
                write_audit_entry(
                    'CREATE', 'key_officer', name,
                    f"added, in charge: {'yes' if in_charge else 'no'}, "
                    f"prescribes: {'yes' if prescriber else 'no'}, "
                    f"dispenses: {'yes' if dispenser else 'no'}")
                jobs = [j for j, on in (('hold the key', in_charge),
                                        ('prescribe', prescriber),
                                        ('dispense', dispenser)) if on]
                flash(f"{name} added to the roster"
                      + (f" &mdash; may {', '.join(jobs)}." if jobs
                         else ', with no jobs ticked yet.'),
                      'success')
                return redirect(url_for('key_register'))

            if action == 'set_officer':
                if not is_admin:
                    flash('Access denied. Only admins can change the roster.')
                    return redirect(url_for('key_register'))
                try:
                    doc = officers.find_one({'_id': ObjectId(request.form.get('officer_id', ''))})
                except (InvalidId, TypeError):
                    doc = None
                if not doc:
                    flash('That person was not found on the roster.')
                    return redirect(url_for('key_register'))
                in_charge = request.form.get('in_charge') == 'yes'
                active = request.form.get('active') == 'yes'
                prescriber = request.form.get('prescriber') == 'yes'
                dispenser = request.form.get('dispenser') == 'yes'
                officers.update_one({'_id': doc['_id']},
                                    {'$set': {'in_charge': in_charge,
                                              'prescriber': prescriber,
                                              'dispenser': dispenser,
                                              'active': active}})
                write_audit_entry(
                    'UPDATE', 'key_officer', doc['name'],
                    f"in charge {doc.get('in_charge')} -> {in_charge}, "
                    f"prescribes {doc.get('prescriber')} -> {prescriber}, "
                    f"dispenses {doc.get('dispenser')} -> {dispenser}, "
                    f"active {doc.get('active')} -> {active}")
                flash(f"{doc['name']} updated.", 'success')
                return redirect(url_for('key_register'))

            if action == 'delete_entry':
                if not is_admin:
                    flash('Access denied. Only admins can delete a register entry.')
                    return redirect(url_for('key_register'))
                try:
                    doc = keys.find_one({'_id': ObjectId(request.form.get('entry_id', ''))})
                except (InvalidId, TypeError):
                    doc = None
                if not doc:
                    flash('That key register entry was not found.')
                else:
                    keys.delete_one({'_id': doc['_id']})
                    write_audit_entry(
                        'DELETE', 'store_key', doc.get('ref', ''),
                        f"removed entry: key to {doc.get('recipient_name')} on "
                        f"{doc['issued_date']:%Y-%m-%d}")
                    flash(f"Entry for {doc.get('recipient_name')} deleted.", 'success')
                return redirect(url_for('key_register'))

            flash('Unknown action.')
            return redirect(url_for('key_register'))

        # ----- GET --------------------------------------------------------
        now = local_now()
        today = now.date()
        raw_start = (request.args.get('start_date') or '').strip()
        raw_end = (request.args.get('end_date') or '').strip()
        try:
            start = datetime.strptime(raw_start, '%Y-%m-%d')
        except ValueError:
            start = datetime(today.year, today.month, 1)
        try:
            end = datetime.strptime(raw_end, '%Y-%m-%d')
        except ValueError:
            end = datetime(today.year, today.month, today.day)
        if end < start:
            start, end = end, start
        q = (request.args.get('q') or '').strip()

        query: Query = {'issued_date': {'$gte': start, '$lt': end + timedelta(days=1)}}
        if q:
            rx = {'$regex': re.escape(q), '$options': 'i'}
            query['$or'] = [{'recipient_name': rx}, {'pharmacist': rx},
                            {'returning_officer': rx}, {'receiving_officer': rx},
                            {'ref': rx}]

        entries, still_out_count = [], 0
        for doc in keys.find(query).sort('issued_at', -1).limit(500):
            out = doc.get('status') == 'Out'
            # Overdue is measured against when the key was actually expected
            # back, which over a weekend is Monday midday, not the same night.
            issued_at = doc.get('issued_at') or doc['issued_date']
            due = doc.get('due_back') or _key_due_back(issued_at)
            weekend = doc.get('weekend_hold')
            if weekend is None:
                weekend = _key_is_weekend_hold(issued_at)
            overdue = out and now > due
            if out:
                still_out_count += 1
            entries.append({
                'id': str(doc['_id']),
                'ref': doc.get('ref', ''),
                'issued_date': doc['issued_date'],
                'issued_time': doc.get('issued_time', ''),
                'pharmacist': doc.get('pharmacist', ''),
                'recipient_name': doc.get('recipient_name', ''),
                'returned_date': doc.get('returned_date'),
                'returned_time': doc.get('returned_time'),
                'returning_officer': doc.get('returning_officer'),
                'receiving_officer': doc.get('receiving_officer'),
                'return_pharmacist': doc.get('return_pharmacist'),
                'comments': doc.get('comments', ''),
                'status': doc.get('status', ''),
                'overdue': overdue,
                'weekend': weekend,
                'due_back': due,
                'entered_by': doc.get('issued_by_user', ''),
                'auto_source': doc.get('auto_source', ''),
                'auto_closed': doc.get('auto_closed', ''),
                'also_on_duty': doc.get('also_on_duty') or [],
                'off_roster': doc.get('recipient_off_roster', False),
                'corrected_by': doc.get('corrected_by', ''),
                'issued_time_raw': doc.get('issued_time', ''),
                'returned_time_raw': doc.get('returned_time', ''),
            })

        open_doc = keys.find_one({'status': 'Out'})
        open_entry = None
        if open_doc:
            since = open_doc.get('issued_at') or open_doc['issued_date']
            due = open_doc.get('due_back') or _key_due_back(since)
            weekend = open_doc.get('weekend_hold')
            if weekend is None:
                weekend = _key_is_weekend_hold(since)
            open_entry = {
                'id': str(open_doc['_id']),
                'recipient_name': open_doc.get('recipient_name', ''),
                'issued_date': open_doc['issued_date'],
                'issued_time': open_doc.get('issued_time', ''),
                'pharmacist': open_doc.get('pharmacist', ''),
                'held_for': _held_for(since, now),
                'overdue': now > due,
                'weekend': weekend,
                'due_back': due,
                'off_roster': open_doc.get('recipient_off_roster', False),
            }

        last_return = ''
        if not open_doc:
            last = keys.find_one({'status': 'Returned'}, sort=[('returned_at', -1)])
            if last and last.get('returned_date'):
                last_return = (f"{last['returned_date']:%a %d %b} at "
                               f"{last.get('returned_time', '')} by "
                               f"{last.get('returning_officer', '')}")

        edit_entry = None
        raw_edit = (request.args.get('edit') or '').strip()
        if raw_edit and is_admin:
            edit_entry = next((e for e in entries if e['id'] == raw_edit), None)

        if request.values.get('export') == 'csv':
            return _register_csv_response('key_register', entries)

        return render_template(
            'key_register.html', nav_links=get_nav_links(),
            message=message, message_class=message_class, is_admin=is_admin,
            current_user=current_user, user_in_charge=user_in_charge,
            user_may_receive=user_may_receive,
            officer_names=_officer_names(officers),
            receiver_names=_key_receiver_names(officers),
            roster=list(officers.find().sort([('in_charge', -1), ('name', 1)])),
            open_entry=open_entry, last_return=last_return,
            entries=entries, still_out_count=still_out_count,
            edit_entry=edit_entry,
            start_date=start.strftime('%Y-%m-%d'),
            end_date=end.strftime('%Y-%m-%d'), q=q)

    except Exception as e:
        app.logger.error(f'key_register failed: {e}')
        flash('Could not load the key register.')
        return redirect('/reports')


@app.route('/dashboard', methods=['GET'])
@login_required
def dashboard() -> ResponseReturnValue:
    is_admin = session['user'].get('role') == 'admin'
    message = None

    # --- request options ------------------------------------------------
    try:
        months_back = int(request.args.get('months', 6))
    except (TypeError, ValueError):
        months_back = 6
    if months_back not in DASHBOARD_MONTH_CHOICES:
        months_back = 6

    sort_by = request.args.get('sort', 'urgency')
    if sort_by not in ('urgency', 'amc', 'trend', 'name'):
        sort_by = 'urgency'

    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    if limit not in (0, 25, 50, 100):
        limit = 50

    search = (request.args.get('search') or '').strip()
    cat = _category_choice()

    months        = _month_sequence(months_back)
    month_labels  = [f"{MONTH_ABBR[m]} {y}" for y, m in months]
    month_labels[-1] += ' (MTD)'
    window_start  = datetime(months[0][0], months[0][1], 1)
    now           = datetime.utcnow()
    today         = now.date()
    current_ym    = (now.year, now.month)
    # The current month is only part-elapsed, so including it would drag every
    # AMC down. It is still shown as a column, just excluded from the average.
    amc_months    = [ym for ym in months if ym != current_ym] or months

    empty = {'val_dispensed': ['0'] * len(months), 'val_received': ['0'] * len(months),
             'val_adjusted': ['0'] * len(months)}
    act_empty = {'visits': [0] * len(months), 'lines': [0] * len(months),
                 'per_visit': ['-'] * len(months), 'patients': [0] * len(months),
                 'receipts': [0] * len(months), 'deliveries': [0] * len(months),
                 'adjusted_lines': [0] * len(months)}
    st_empty: dict[str, list[Any]] = {'counted': [0] * len(months), 'agreed': [0] * len(months),
                'short_units': [0] * len(months), 'over_units': [0] * len(months),
                'accuracy': ['-'] * len(months)}

    try:
        db = get_mongo_client()['pharmacy_db']
        movement = _monthly_movement(db['transactions'], window_start)
        all_meds = _by_name(db['medications'].find(_active(_category_filter(cat)), {'_id': 0}))

        rows = []
        # Units are only meaningful per medication, so the cross-medication
        # totals are kept as VALUE (money adds up across dosage forms; units
        # do not) plus the countable activity figures fetched below.
        n_m = len(months)
        totals: dict[str, list[Any]] = {'val_dispensed': [0.0] * n_m, 'val_received': [0.0] * n_m,
                  'val_adjusted': [0.0] * n_m}
        activity = _activity_by_month(db['transactions'], window_start)
        demand = _demand_by_med(db['transactions'], {'$gte': window_start})
        clinical = _clinical_by_month(db['transactions'], window_start, months)

        for med in all_meds:
            name    = med.get('name', '')
            by_month = movement.get(name, {})
            monthly  = [by_month.get(ym, {}).get('dispensed', 0) for ym in months]
            for i, ym in enumerate(months):
                b = by_month.get(ym, {})
                # Valued at the medication's CURRENT unit price — transactions
                # don't store a price at dispense time, so this is an
                # approximation, and it is labelled as one on the page.
                unit = med.get('price', 0) or 0
                totals['val_dispensed'][i] += b.get('dispensed', 0) * unit
                totals['val_received'][i]  += b.get('received', 0) * unit
                totals['val_adjusted'][i]  += b.get('adjusted', 0) * unit

            complete = [by_month.get(ym, {}).get('dispensed', 0) for ym in amc_months]
            amc      = sum(complete) / len(complete) if complete else 0.0
            balance  = med.get('balance', 0) or 0
            price    = med.get('price', 0) or 0

            # Months of stock: how long today's balance lasts at the recent rate.
            mos = (balance / amc) if amc > 0 else None
            if balance == 0:
                status, mos_label = 'expired', 'Out of stock'
            elif mos is None:
                status, mos_label = 'normal', 'No recent use'
            elif mos < 1:
                status, mos_label = 'expired', f'{mos:.1f}'
            elif mos < 2:
                status, mos_label = 'close-to-expire', f'{mos:.1f}'
            else:
                status, mos_label = 'normal', f'{mos:.1f}'

            # Trend: last completed month against AMC.
            last_complete = by_month.get(amc_months[-1], {}).get('dispensed', 0) if amc_months else 0
            if amc <= 0:
                trend_label = '-'
            else:
                pct = (last_complete - amc) / amc * 100
                if abs(pct) < 10:
                    trend_label = 'steady'
                else:
                    trend_label = f"{'up' if pct > 0 else 'down'} {abs(pct):.0f}%"

            # Pattern: how erratic demand has been. An item with the same AMC
            # but lumpy demand needs a bigger buffer, which AMC alone hides.
            if amc > 0 and len(complete) >= 3:
                cv = _stdev(complete) / amc
                pattern = 'Steady' if cv < 0.25 else ('Variable' if cv < 0.75 else 'Erratic')
            else:
                pattern = '-'

            # Same target as the inventory report: one month's cover plus two
            # months' lead time.
            suggested = max(0, amc * 3 - balance) if amc > 0 else 0
            peak      = monthly.index(max(monthly)) if any(monthly) else None

            unmet_units = demand.get(name, _ZERO_DEMAND)['shortfall']
            rows.append({
                'med_name': name, 'monthly': monthly, 'unmet': unmet_units,
                'fill_rate': (round(sum(monthly) / (sum(monthly) + unmet_units) * 100)
                              if (sum(monthly) + unmet_units) else None),
                'amc': round(amc, 1), 'amc_raw': amc,
                'trend_label': trend_label,
                'trend_abs': abs((last_complete - amc) / amc) if amc > 0 else -1,
                'pattern': pattern, 'balance': balance,
                'mos': mos, 'mos_label': mos_label, 'status': status,
                'suggested_order': int(round(suggested)),
                'peak': peak, 'value': balance * price,
                'expiry': _parse_expiry(med.get('expiry_date')),
                'expiry_raw': med.get('expiry_date') or '-',
                'total_consumption': sum(monthly),
            })

        # --- activity rows ------------------------------------------------
        act: Doc = {'visits': [], 'lines': [], 'per_visit': [], 'patients': [],
               'receipts': [], 'deliveries': [], 'adjusted_lines': []}
        for ym in months:
            b = activity.get(ym) or {'visits': set(), 'lines': 0, 'patients': set(),
                                     'receipts': 0, 'deliveries': set(), 'adjusted_lines': 0}
            visits = len(b['visits'])
            act['visits'].append(visits)
            act['lines'].append(b['lines'])
            act['per_visit'].append(f"{b['lines'] / visits:.1f}" if visits else '-')
            act['patients'].append(len(b['patients']))
            act['receipts'].append(b['receipts'])
            act['deliveries'].append(len(b['deliveries']))
            act['adjusted_lines'].append(b['adjusted_lines'])
        # Distinct patients across the whole window, so a patient attending in
        # two months is counted once — the same basis as the period report.
        patient_total = len(clinical.get('patients') or ())
        dispensed_money = sum(totals['val_dispensed'])
        cost_per_patient_label = (f'R{dispensed_money / patient_total:,.2f}'
                                  if patient_total else '-')
        totals['val_dispensed'] = [f"{v:,.0f}" for v in totals['val_dispensed']]
        totals['val_received']  = [f"{v:,.0f}" for v in totals['val_received']]
        totals['val_adjusted']  = [f"{v:,.0f}" for v in totals['val_adjusted']]

        # --- tiles (whole pharmacy, never narrowed by the search box) ----
        out_of_stock = [r for r in rows if r['balance'] == 0]
        under_one    = [r for r in rows if r['balance'] > 0 and r['mos'] is not None and r['mos'] < 1]
        expired_now  = [r for r in rows if r['expiry'] and r['expiry'] < today and r['balance'] > 0]
        expiring_90  = [r for r in rows if r['expiry'] and today <= r['expiry'] <= today + timedelta(days=90)
                        and r['balance'] > 0]
        stock_value  = sum(r['value'] for r in rows)
        at_risk      = sum(r['value'] for r in expiring_90)

        total_disp = sum(sum(r['monthly']) for r in rows)
        total_unmet = sum(r['unmet'] for r in rows)
        fill = (total_disp / (total_disp + total_unmet) * 100) if (total_disp + total_unmet) else None
        short_items = [r for r in rows if r['unmet']]
        tiles = [
            {'label': 'Items tracked', 'value': len(rows), 'sub': '', 'bg': '#eef2f7', 'fg': '#1a3a5c'},
            {'label': 'Fill rate', 'value': f'{fill:.1f}%' if fill is not None else '-',
             'sub': f'{total_unmet:,} unit(s) asked for and not supplied',
             'bg': ('#f8d7da' if fill is not None and fill < 90
                    else ('#fff3cd' if fill is not None and fill < 98 else '#eef2f7')),
             'fg': ('#721c24' if fill is not None and fill < 90
                    else ('#856404' if fill is not None and fill < 98 else '#1a3a5c'))},
            {'label': 'Items that ran short', 'value': len(short_items),
             'sub': 'demand could not be met',
             'bg': '#f8d7da' if short_items else '#eef2f7',
             'fg': '#721c24' if short_items else '#1a3a5c'},
            {'label': 'Out of stock', 'value': len(out_of_stock), 'sub': 'balance is zero',
             'bg': '#f8d7da' if out_of_stock else '#eef2f7', 'fg': '#721c24' if out_of_stock else '#1a3a5c'},
            {'label': 'Under 1 month of stock', 'value': len(under_one), 'sub': 'reorder now',
             'bg': '#f8d7da' if under_one else '#eef2f7', 'fg': '#721c24' if under_one else '#1a3a5c'},
            {'label': 'Expired on shelf', 'value': len(expired_now), 'sub': 'remove from stock',
             'bg': '#f8d7da' if expired_now else '#eef2f7', 'fg': '#721c24' if expired_now else '#1a3a5c'},
            {'label': 'Expiring in 90 days', 'value': len(expiring_90), 'sub': f'R{at_risk:,.2f} at risk',
             'bg': '#fff3cd' if expiring_90 else '#eef2f7', 'fg': '#856404' if expiring_90 else '#1a3a5c'},
            {'label': 'Stock value', 'value': f'R{stock_value:,.0f}', 'sub': 'balance x unit price',
             'bg': '#eef2f7', 'fg': '#1a3a5c'},
            {'label': 'Cost per patient', 'value': cost_per_patient_label,
             'sub': f'{patient_total:,} patient(s) over the period',
             'bg': '#eef2f7', 'fg': '#1a3a5c'},
        ]

        # --- expiring panel ---------------------------------------------
        expiring = []
        for r in sorted(expiring_90, key=lambda r: r['expiry']):
            days_left = (r['expiry'] - today).days
            # What will still be sitting there at expiry if it keeps moving
            # at its AMC — the quantity actually at risk of write-off.
            projected_use = r['amc_raw'] * (days_left / 30.0)
            expiring.append({
                'med_name': r['med_name'], 'expiry_date': r['expiry_raw'],
                'days_left': days_left, 'balance': r['balance'], 'value': r['value'],
                'amc': r['amc'], 'likely_unused': max(0, int(round(r['balance'] - projected_use))),
                'status': 'expired' if days_left <= 30 else 'close-to-expire',
            })

        # --- table: search, sort, limit ---------------------------------
        table_rows = rows
        if search:
            needle = search.lower()
            table_rows = [r for r in table_rows if needle in r['med_name'].lower()]
        total_items = len(table_rows)

        if sort_by == 'name':
            table_rows = sorted(table_rows, key=lambda r: _name_key(r['med_name']))
        elif sort_by == 'amc':
            table_rows = sorted(table_rows, key=lambda r: -r['amc_raw'])
        elif sort_by == 'trend':
            table_rows = sorted(table_rows, key=lambda r: -r['trend_abs'])
        else:  # urgency — emptiest shelves first, items with no demand last
            table_rows = sorted(
                table_rows,
                key=lambda r: (0 if r['balance'] == 0 else 1,
                               r['mos'] if r['mos'] is not None else float('inf'),
                               -r['amc_raw'])
            )
        if limit:
            table_rows = table_rows[:limit]

        # --- stock take panels (admin only) -----------------------------
        st_totals, top_discrepancies, never_counted = st_empty, [], []
        if is_admin:
            counts = list(db['stock_take_counts'].find(
                {'timestamp': {'$gte': window_start}},
                {'_id': 0, 'med_name': 1, 'variance': 1, 'timestamp': 1}))
            idx = {ym: i for i, ym in enumerate(months)}
            st_totals = {'counted': [0] * len(months), 'agreed': [0] * len(months),
                         'short_units': [0] * len(months), 'over_units': [0] * len(months),
                         'accuracy': ['-'] * len(months)}
            net_by_med: defaultdict[str, Doc] = defaultdict(
                lambda: {'net': 0, 'count': 0, 'last': None})
            for c in counts:
                ts = c.get('timestamp')
                v = c.get('variance', 0) or 0
                if isinstance(ts, datetime) and (ts.year, ts.month) in idx:
                    i = idx[(ts.year, ts.month)]
                    st_totals['counted'][i] += 1
                    if v == 0:
                        st_totals['agreed'][i] += 1
                    elif v < 0:
                        st_totals['short_units'][i] += -v
                    else:
                        st_totals['over_units'][i] += v
                if v != 0:
                    e = net_by_med[c.get('med_name', '')]
                    e['net'] += v
                    e['count'] += 1
                    if isinstance(ts, datetime) and (e['last'] is None or ts > e['last']):
                        e['last'] = ts
            for i in range(len(months)):
                total = st_totals['counted'][i]
                st_totals['accuracy'][i] = f"{st_totals['agreed'][i] / total * 100:.0f}%" if total else '-'

            top_discrepancies = sorted(
                ({'med_name': k, 'net': v['net'], 'count': v['count'],
                  'label': DISCREPANCY_LABELS[classify_variance(v['net'])],
                  'last_counted': v['last'].strftime('%Y-%m-%d') if v['last'] else '-'}
                 for k, v in net_by_med.items() if v['net'] != 0),
                key=lambda d: -abs(d['net'])
            )[:15]

            ever_counted = set(db['stock_take_counts'].distinct('med_name'))
            never_counted = sorted(
                ({'med_name': r['med_name'], 'balance': r['balance'], 'value': r['value']}
                 for r in rows if r['balance'] > 0 and r['med_name'] not in ever_counted),
                key=lambda n: -n['value']
            )[:25]

        return render_template(
            'dashboard.html', nav_links=get_nav_links(), message=message,
            is_admin=is_admin, month_labels=month_labels, months_back=months_back,
            month_choices=DASHBOARD_MONTH_CHOICES, sort_by=sort_by, limit=limit,
            search=search, category=cat, category_label=CATEGORY_LABELS[cat],
            tiles=tiles, totals=totals, act=act, clinical=clinical,
            rows=table_rows,
            total_items=total_items, expiring=expiring, st_totals=st_totals,
            top_discrepancies=top_discrepancies, never_counted=never_counted,
        )

    except ServerSelectionTimeoutError:
        return render_template(
            'dashboard.html', nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            is_admin=is_admin, month_labels=month_labels, months_back=months_back,
            month_choices=DASHBOARD_MONTH_CHOICES, sort_by=sort_by, limit=limit,
            search=search, category='pharmaceutical',
            category_label=CATEGORY_LABELS['pharmaceutical'],
            tiles=[], totals=empty, act=act_empty, rows=[], total_items=0,
            clinical={'outcomes': [], 'diagnoses': [], 'episodes': 0, 'patients': set()},
            expiring=[], st_totals=st_empty, top_discrepancies=[], never_counted=[],
        ), 500


# -----------------------------------------------------------------------
# NEW (Stock Take): physical count with immediate, per-item correction.
#
# A discrepancy between the shelf and the system is always one of exactly two
# things, and the sign of the variance tells you which:
#
#   counted < system  -> stock left the shelf but was never recorded
#                        ("issued, not recorded")
#   counted > system  -> an issue was recorded that never actually happened
#                        ("recorded, not issued")
#
# so the type is derived, never asked for.
# -----------------------------------------------------------------------
DISCREPANCY_ISSUED_NOT_RECORDED = 'issued_not_recorded'
DISCREPANCY_RECORDED_NOT_ISSUED = 'recorded_not_issued'
DISCREPANCY_NONE = 'none'

DISCREPANCY_LABELS = {
    DISCREPANCY_ISSUED_NOT_RECORDED: 'Issued, not recorded',
    DISCREPANCY_RECORDED_NOT_ISSUED: 'Recorded, not issued',
    DISCREPANCY_NONE: 'Agrees',
}


def classify_variance(variance: float) -> str:
    """variance = physical count - system balance."""
    if variance < 0:
        return DISCREPANCY_ISSUED_NOT_RECORDED
    if variance > 0:
        return DISCREPANCY_RECORDED_NOT_ISSUED
    return DISCREPANCY_NONE


def _flashed_banner() -> tuple[str | None, str | None]:
    """(css_class, message) for the banner, with Flask's default normalised.

    flash('text') with no category stores the category as the literal string
    'message'. The templates then render class="message message", which is
    truthy, so the {% else %}error{% endif %} fallback never fired and every
    plain error banner rendered unstyled instead of red. Mapping the default
    onto 'error' here fixes it once for every page rather than in each
    template, and routes that pass a category keep it.
    """
    flashed = get_flashed_messages(with_categories=True)
    if not flashed:
        return None, None
    category, text = cast('list[tuple[str, str]]', flashed)[0]
    return ('error' if category in (None, '', 'message') else category), text


def write_audit_entry(action: str, target_type: str, target_id: Any, changes: Any) -> None:
    """Write straight to audit_log in the shape AUDIT_TEMPLATE renders.

    audit_logger.py's decorators only wrap the dispense/receive/medication
    routes, so stock-take corrections would otherwise never appear under
    'Edits & Deletes' even though they change a balance. Best-effort: an audit
    write must never be the reason a count fails to save.
    """
    try:
        db = get_mongo_client()['pharmacy_db']
        db['audit_log'].insert_one({
            'timestamp': datetime.utcnow(),
            'action': action,
            'target_type': target_type,
            'target_id': target_id,
            'user': session.get('user', {}).get('name', 'unknown'),
            'ip': request.remote_addr,
            'changes': changes,
        })
    except Exception as e:
        app.logger.warning(f"Failed to write audit entry for {target_type}/{target_id}: {e}")


@app.route('/stock-take', methods=['GET', 'POST'])
@login_required
def stock_take() -> ResponseReturnValue:
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can perform a stock take.')
        return redirect('/reports')

    current_user = session['user']['name']
    flashed = get_flashed_messages()
    message = flashed[0] if flashed else None

    try:
        db = get_mongo_client()['pharmacy_db']
        medications  = db['medications']
        transactions = db['transactions']
        stock_takes  = db['stock_takes']
        counts_col   = db['stock_take_counts']

        stock_take_id = request.values.get('stock_take_id')

        if request.method == 'POST':
            action = request.form.get('action')

            # --- Open a new stock take -----------------------------------
            if action == 'open':
                if stock_takes.find_one({'status': 'open'}):
                    flash('A stock take is already open. Continue or close it first.')
                    return redirect(url_for('stock_take'))
                reference = f"ST-{datetime.utcnow().strftime('%Y%m%d')}-{uuid4().hex[:4].upper()}"
                try:
                    new_id = stock_takes.insert_one({
                        'reference': reference,
                        'status': 'open',
                        'note': (request.form.get('note') or '').strip(),
                        'started_by': current_user,
                        'started_at': datetime.utcnow(),
                        'closed_by': None,
                        'closed_at': None,
                    }).inserted_id
                except DuplicateKeyError:
                    flash('Could not generate a unique reference. Please try again.')
                    return redirect(url_for('stock_take'))
                write_audit_entry('CREATE', 'stock_take', reference,
                                  f"Stock take {reference} opened by {current_user}")
                flash(f'Stock take {reference} opened.')
                return redirect(url_for('stock_take', stock_take_id=str(new_id)))

            # --- Post a whole count sheet at once -------------------------
            if action == 'count_sheet':
                st = _load_stock_take(stock_takes, request.form.get('stock_take_id'))
                if not st:
                    flash('Stock take not found.')
                    return redirect(url_for('stock_take'))
                if st['status'] != 'open':
                    flash(f"Stock take {st['reference']} is closed.")
                    return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

                names = request.form.getlist('med_name')
                posted = agreed = short = over = skipped = 0
                details_only = detail_changes = bad_dates = 0
                for i, name in enumerate(names):
                    raw = (request.form.get(f'counted__{i}') or '').strip()
                    expiry = (request.form.get(f'expiry__{i}') or '').strip()
                    batch = (request.form.get(f'batch__{i}') or '').strip()

                    # A date the app cannot read is dropped rather than
                    # written, and said out loud. The count on the same row
                    # still posts: losing a counted quantity over a mistyped
                    # date would be the worse trade.
                    if expiry and not _parse_expiry(expiry):
                        bad_dates += 1
                        expiry = ''

                    if raw == '' and not expiry and not batch:
                        continue          # blank row: nothing entered, leave it

                    counted: int | None = None
                    if raw != '':
                        try:
                            counted = int(raw)
                        except ValueError:
                            skipped += 1
                            continue
                        if counted < 0:
                            skipped += 1
                            continue

                    outcome = _record_count(
                        medications, transactions, counts_col, st, name, counted,
                        (request.form.get(f'note__{i}') or '').strip(), current_user,
                        expiry=expiry or None, batch=batch or None)
                    if outcome is None:
                        skipped += 1
                        continue
                    detail_changes += outcome['detail_changes']
                    if not outcome['counted']:
                        details_only += 1
                        continue
                    posted += 1
                    if outcome['variance'] == 0:
                        agreed += 1
                    elif outcome['variance'] < 0:
                        short += 1
                    else:
                        over += 1

                if posted:
                    msg = (f'{posted} count(s) posted to {st["reference"]}: '
                           f'{agreed} agreed, {short} short, {over} over.')
                elif details_only:
                    msg = f'No counts were entered in {st["reference"]}.'
                else:
                    msg = 'No counts were entered.'
                if details_only:
                    msg += (f' {details_only} row(s) had an expiry or batch '
                            f'but no count, so their balances were left alone.')
                if detail_changes:
                    msg += f' {detail_changes} expiry/batch detail(s) updated.'
                if bad_dates:
                    msg += (f' {bad_dates} expiry date(s) were not in '
                            f'YYYY-MM-DD form and were not saved.')
                if skipped:
                    msg += f' {skipped} row(s) skipped as invalid or unknown.'
                flash(msg)
                return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

            # --- Record one physical count -------------------------------
            if action == 'count':
                st = _load_stock_take(stock_takes, request.form.get('stock_take_id'))
                if not st:
                    flash('Stock take not found.')
                    return redirect(url_for('stock_take'))
                if st['status'] != 'open':
                    flash(f"Stock take {st['reference']} is closed. No further counts can be added.")
                    return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

                med_name = (request.form.get('med_name') or '').strip()
                raw = (request.form.get('counted') or '').strip()
                expiry = (request.form.get('expiry_date') or '').strip()
                batch = (request.form.get('batch') or '').strip()

                counted = None
                if raw != '':
                    try:
                        counted = int(raw)
                    except ValueError:
                        flash('Physical count must be a whole number.')
                        return redirect(url_for('stock_take',
                                                stock_take_id=str(st['_id'])))
                    if counted < 0:
                        flash('Physical count cannot be negative.')
                        return redirect(url_for('stock_take',
                                                stock_take_id=str(st['_id'])))
                if counted is None and not expiry and not batch:
                    flash('Enter a count, or an expiry date or batch number.')
                    return redirect(url_for('stock_take',
                                            stock_take_id=str(st['_id'])))
                if expiry and not _parse_expiry(expiry):
                    flash('The expiry date must be in YYYY-MM-DD form.')
                    return redirect(url_for('stock_take',
                                            stock_take_id=str(st['_id'])))

                # One implementation, shared with the count sheet. This used
                # to be a second copy of the same ninety lines, which is how
                # the two could have drifted apart without anything failing.
                outcome = _record_count(
                    medications, transactions, counts_col, st, med_name, counted,
                    (request.form.get('note') or '').strip(), current_user,
                    expiry=expiry or None, batch=batch or None)
                if outcome is None:
                    flash(f'Medication "{med_name}" not found. '
                          f'Add it first, then count it.')
                    return redirect(url_for('stock_take',
                                            stock_take_id=str(st['_id'])))

                bits = []
                if outcome['counted']:
                    variance = outcome['variance']
                    if variance == 0:
                        bits.append(f'counted {counted}. Agrees with the system')
                    else:
                        dtype = classify_variance(variance)
                        bits.append(
                            f'counted {counted}, system had '
                            f'{counted - variance:g} ({variance:+g}). Recorded as '
                            f'"{DISCREPANCY_LABELS[dtype]}" and the balance is '
                            f'now {counted}')
                if outcome['detail_changes']:
                    bits.append(f"{outcome['detail_changes']} detail(s) corrected")
                elif not outcome['counted']:
                    bits.append('expiry and batch recorded; the balance was '
                                'left as it was')
                flash(f"{med_name}: {'; '.join(bits)}.")
                return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

            # --- Close a stock take --------------------------------------
            if action == 'close':
                st = _load_stock_take(stock_takes, request.form.get('stock_take_id'))
                if not st:
                    flash('Stock take not found.')
                    return redirect(url_for('stock_take'))
                if st['status'] != 'open':
                    flash(f"Stock take {st['reference']} is already closed.")
                    return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))
                stock_takes.update_one(
                    {'_id': st['_id'], 'status': 'open'},
                    {'$set': {'status': 'closed', 'closed_by': current_user,
                              'closed_at': datetime.utcnow()}}
                )
                write_audit_entry('UPDATE', 'stock_take', st['reference'],
                                  f"Stock take {st['reference']} closed by {current_user}")
                flash(f"Stock take {st['reference']} closed.")
                return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

            flash('Unknown stock take action.')
            return redirect(url_for('stock_take'))

        # ----- GET ------------------------------------------------------
        active = _load_stock_take(stock_takes, stock_take_id) if stock_take_id else None

        if active:
            counts = list(counts_col.find({'stock_take_id': str(active['_id'])})
                          .sort('timestamp', -1).limit(2000))
            for c in counts:
                c['discrepancy_label'] = DISCREPANCY_LABELS.get(c.get('discrepancy_type'), '-')
            # Only the names are sent to the counting sheet — never the
            # balances. The count has to be blind.
            med_names = [m['name'] for m in _by_name(medications.find(_active(), {'_id': 0, 'name': 1}))]
            # Items already counted in THIS stock take drop off the sheet, so
            # the list shrinks as the work is done and nothing is counted twice
            # by accident.
            done = set(counts_col.distinct(
                'med_name',
                {'stock_take_id': str(active['_id']), 'counted': {'$ne': None}}))
            uncounted = [m for m in med_names if m not in done]
            if request.values.get('export') == 'csv':
                return _register_csv_response('stock_take', counts)

            return render_template(
                'stock_take.html', nav_links=get_nav_links(), message=message,
                active=active, counts=counts, med_names=med_names,
                uncounted=uncounted, sessions=[],
                agreed_count=sum(1 for c in counts if c.get('variance') == 0),
                issued_not_recorded=sum(1 for c in counts
                                        if (c.get('variance') or 0) < 0),
                recorded_not_issued=sum(1 for c in counts
                                        if (c.get('variance') or 0) > 0),
            )

        sessions = list(stock_takes.find().sort([('status', 1), ('started_at', -1)]).limit(200))
        for s in sessions:
            sid = str(s['_id'])
            s['counted_lines']     = counts_col.count_documents(
                {'stock_take_id': sid, 'counted': {'$ne': None}})
            s['discrepancy_lines'] = counts_col.count_documents(
                {'stock_take_id': sid, 'variance': {'$nin': [0, None]}})
        return render_template(
            'stock_take.html', nav_links=get_nav_links(), message=message,
            active=None, sessions=sessions, counts=[], med_names=[], uncounted=[],
            agreed_count=0, issued_not_recorded=0, recorded_not_issued=0,
        )

    except ServerSelectionTimeoutError:
        return render_template(
            'stock_take.html', nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            active=None, sessions=[], counts=[], med_names=[], uncounted=[],
            agreed_count=0, issued_not_recorded=0, recorded_not_issued=0,
        ), 500


def _record_count(medications: Collection, transactions: Collection,
                  counts_col: Collection, st: Doc, med_name: str,
                  counted: float | None, note: str, user: str,
                  expiry: str | None = None,
                  batch: str | None = None) -> Doc | None:
    """Apply one line of a stock take. None if the medication is unknown.

    The one place a count is recorded. The single-item path and the count
    sheet both come through here, so the sheet cannot drift into being a
    second, subtly different implementation.

    The quantity is optional, because a stock take also turns up an expiry
    date or a batch number that does not match what is on file, and noticing
    that is worth recording even when the shelf is not being recounted. A
    line with no quantity corrects the detail and leaves the balance exactly
    as it was: no adjustment is posted, and it does not count as an item
    counted.

    Whatever is given replaces what is on file — the shelf is the truth, and
    that is what a stock take is for — but the value it replaced is kept on
    the line, so the register shows the change rather than quietly absorbing
    it.
    """
    updates: Doc = {}
    if counted is not None:
        updates['balance'] = counted
    if expiry:
        updates['expiry_date'] = expiry
    if batch:
        updates['batch'] = batch
    if not updates:
        return None                     # nothing was entered on this line

    before = medications.find_one_and_update(
        {'name': med_name}, {'$set': updates},
        return_document=ReturnDocument.BEFORE)
    if not before:
        write_app_warning(
            'medication_not_found',
            f'Medication "{med_name}" not found during stock take {st["reference"]}.',
            {'med_name': med_name, 'stock_take': st['reference']})
        return None

    now = datetime.utcnow()
    system_balance = before.get('balance', 0)
    variance: float | None = None
    dtype: str | None = None
    balance_change = ''
    if counted is not None:
        variance = counted - system_balance
        dtype = classify_variance(variance)
        balance_change = (f'balance: {system_balance} -> {counted} '
                          f'(variance {variance:+g}, '
                          f'{DISCREPANCY_LABELS[dtype].lower()})')

    # The correction is posted to the ledger as its own signed type. Every
    # report back-calculates a beginning balance from the current balance and
    # the movements in between, so a balance corrected without a matching
    # entry would break the arithmetic for every period containing the count.
    # A detail-only line moves nothing, so it posts nothing.
    if variance:
        transactions.insert_one({
            'type': 'adjustment', 'med_name': med_name, 'quantity': variance,
            'batch': before.get('batch'), 'price': before.get('price'),
            'expiry_date': before.get('expiry_date'),
            'schedule': before.get('schedule'),
            'system_balance': system_balance, 'counted': counted,
            'discrepancy_type': dtype,
            'stock_take_id': str(st['_id']), 'stock_take_reference': st['reference'],
            'user': user, 'timestamp': now,
        })

    line: Doc = {
        'stock_take_id': str(st['_id']), 'reference': st['reference'],
        'med_name': med_name, 'system_balance': system_balance,
        'counted': counted, 'variance': variance, 'discrepancy_type': dtype,
        'note': note, 'counted_by': user, 'timestamp': now,
    }
    changes = []
    if expiry:
        line['expiry_before'] = before.get('expiry_date')
        line['expiry_after'] = expiry
        if str(before.get('expiry_date') or '') != expiry:
            changes.append(f"expiry: {before.get('expiry_date') or '(none)'} -> {expiry}")
    if batch:
        line['batch_before'] = before.get('batch')
        line['batch_after'] = batch
        if str(before.get('batch') or '') != batch:
            changes.append(f"batch: {before.get('batch') or '(none)'} -> {batch}")
    counts_col.insert_one(line)

    if balance_change:
        changes.insert(0, balance_change)
    write_audit_entry('UPDATE', 'stock_take_count',
                      f"{st['reference']} / {med_name}", '; '.join(changes))
    return {'variance': variance, 'counted': counted is not None,
            'detail_changes': len(changes) - (1 if counted is not None else 0)}


def _load_stock_take(stock_takes: Collection, raw_id: str | None) -> Doc | None:
    """Resolve a stock take by its string _id, tolerating a malformed value."""
    if not raw_id:
        return None
    try:
        return stock_takes.find_one({'_id': ObjectId(raw_id)})
    except (InvalidId, TypeError):
        return None


@app.route('/audit', methods=['GET'])
@login_required
def audit_log() -> ResponseReturnValue:
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

        date_query: Query = {}
        if start_date:
            date_query['$gte'] = datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
            date_query['$lte'] = end_dt

        if view == 'errors':
            query: Query = {}
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
        elif view == 'stocktake':
            # NEW (Stock Take): every physical count and the correction it
            # produced. ?only=discrepancies hides the lines that agreed.
            query = {}
            if date_query:
                query['timestamp'] = date_query
            if request.args.get('only') == 'discrepancies':
                query['variance'] = {'$ne': 0}
            if search:
                query['$or'] = [
                    {'med_name':         {'$regex': search, '$options': 'i'}},
                    {'reference':        {'$regex': search, '$options': 'i'}},
                    {'counted_by':       {'$regex': search, '$options': 'i'}},
                    {'discrepancy_type': {'$regex': search, '$options': 'i'}},
                    {'note':             {'$regex': search, '$options': 'i'}},
                ]
            entries = list(
                db['stock_take_counts'].find(query).sort('timestamp', -1).limit(500)
            )
            for e in entries:
                e['discrepancy_label'] = DISCREPANCY_LABELS.get(e.get('discrepancy_type'), '-')
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

        if request.values.get('export') == 'csv':
            return _register_csv_response('audit', entries)

        return render_template(
            'audit.html',
            nav_links=get_nav_links(),
            message=message,
            view=view,
            entries=entries,
            start_date=start_date,
            end_date=end_date,
            search=search
        )
    except ServerSelectionTimeoutError:
        return render_template(
            'audit.html',
            nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            view=view, entries=[], start_date=start_date, end_date=end_date, search=search
        ), 500


@app.route('/delete-dispense', methods=['POST'])
@login_required
def delete_dispense() -> ResponseReturnValue:
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
def edit_receive(receive_id: str) -> ResponseReturnValue:
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        transactions = db['transactions']
        medications  = db['medications']

        start_date = request.values.get('start_date')
        end_date   = request.values.get('end_date')
        search     = request.values.get('search')
        current_user = session['user']['name']

        base_query: Query = {'type': 'receive'}
        date_query: Query = {}
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
        # FIX (Performance): this route renders the same list template, so it
        # carried the same unbounded fetch. It only ever shows the first page
        # behind the edit form, so it is capped outright.
        page = _requested_page()
        total_lines = transactions.count_documents(base_query)
        pager = _pager(page, RECEIVE_PAGE_SIZE, total_lines)
        tx_list = list(transactions.find(base_query)
                       .sort('timestamp', -1)
                       .skip((pager['page'] - 1) * RECEIVE_PAGE_SIZE)
                       .limit(RECEIVE_PAGE_SIZE))

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
                    med_name       = request.form['med_name'].strip()
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
                         # FIX (Bug): schedule deliberately excluded here too —
                         # editing a receive line must not be able to reclassify
                         # a controlled drug. Change it under Edit Medication.
                         #
                         # pack_price is recomputed from the edited unit price and
                         # the pack size already on file, so the three figures
                         # cannot drift apart and leave the order request costing
                         # packs at a stale rate.
                         '$set': {'batch': batch, 'price': price, 'expiry_date': expiry_date,
                                  'pack_price': price * (medications.find_one(
                                      {'name': med_name}, {'_id': 0, 'pack_size': 1}
                                  ) or {}).get('pack_size', 1),
                                  'stock_receiver': stock_receiver,
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

        return render_template(
            'receive.html',
            tx_list=tx_list, nav_links=get_nav_links(),
            message=message, start_date=start_date, end_date=end_date,
            search=search, rx_data=rx_data,
            current_user=session['user']['name'],
            delivery=None, delivery_lines=[], recon={}, deliveries=[], order_summary=None,
            pager=pager, window_defaulted=False, unit='receipts'
        )
    except ServerSelectionTimeoutError:
        return "Database connection failed.", 500


@app.route('/delete-receive', methods=['POST'])
@login_required
def delete_receive() -> ResponseReturnValue:
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

DIAGNOSIS_SUGGESTION_LIMIT = 10


def _diagnosis_matches(query: str, limit: int = DIAGNOSIS_SUGGESTION_LIMIT) -> list[str]:
    """The standard diagnoses worth offering for what has been typed.

    Ranked, because the old version returned the first ten entries in list
    order that contained the text anywhere — so typing "cold" offered
    "Alcohol intoxication" before "Common cold", and the standard term was
    further away than typing one of your own.

    Order: an entry whose name starts with what you typed, then one where a
    WORD starts with it, then one that merely contains it, then the same
    three again for its other search terms. Shorter names win ties, because
    the short name is nearly always the everyday one.
    """
    q = (query or '').strip().lower()
    if not q:
        return []
    # One or two letters match far too much in the middle of a word: "tb"
    # offered GERD, because one of its search terms is "heartburn". For a
    # query this short, only the start of a word counts.
    loose = len(q) >= 3
    scored = []
    for label in DIAGNOSES_OPTIONS:
        best = None
        terms = [(label.lower(), 0)] + [
            (t, 1) for t in DIAGNOSIS_SEARCH_TERMS.get(label, ())]
        for text, offset in terms:
            if text.startswith(q):
                rank = 0
            elif re.search(r'\b' + re.escape(q), text):
                rank = 1
            elif loose and q in text:
                rank = 2
            else:
                continue
            rank += offset
            best = rank if best is None else min(best, rank)
        if best is not None:
            scored.append((best, len(label), label.lower(), label))
    scored.sort()
    return [label for _r, _n, _l, label in scored[:limit]]


# Every register answers the same question on paper: "can I have this as a
# file?" The reports page and the dispensing page already could. These are
# the rest of them, through one helper so they behave identically and a sixth
# register costs four lines rather than forty.
REGISTER_CSV = {
    'audit': ('Audit Log', [
        ('timestamp', 'When'), ('user', 'User'), ('action', 'Action'),
        ('target_type', 'Record type'), ('target_id', 'Record'),
        ('changes', 'What changed'), ('ip', 'From')]),
    'key_register': ('Key Register', [
        ('ref', 'Ref'), ('issued_date', 'Issued'), ('issued_time', 'Time out'),
        ('recipient_name', 'Issued to'), ('recipient_role', 'Role'),
        ('purpose', 'Purpose'), ('also_on_duty', 'Also on duty'),
        ('returned_date', 'Returned'), ('returned_time', 'Time in'),
        ('returning_officer', 'Returned by'),
        ('receiving_officer', 'Received by'), ('pharmacist', 'Pharmacist'),
        ('comment', 'Comment'), ('user', 'Recorded by')]),
    'temperature': ('Temperature Register', [
        ('date', 'Date'), ('session', 'Round'), ('location', 'Room'),
        ('thermometer', 'Thermometer'), ('temp', 'Temperature C'),
        ('initials', 'Initials'), ('check_time', 'Write time'),
        ('user', 'Recorded by')]),
    'loans': ('Equipment Loans', [
        ('loan_ref', 'Ref'), ('item_code', 'Item'), ('item_type', 'Type'),
        ('patient_name', 'Patient'), ('patient_id', 'Patient ID'),
        ('patient_contact', 'Contact'), ('issued_date', 'Issued'),
        ('due_date', 'Due back'), ('returned_date', 'Returned'),
        ('days', 'Days out'), ('status', 'Status'),
        ('condition_out', 'Condition out'), ('condition_in', 'Condition back'),
        ('issued_by', 'Issued by'), ('note', 'Note')]),
    'stock_take': ('Stock Take', [
        ('med_name', 'Medication'), ('system_balance', 'On system'),
        ('counted', 'Counted'), ('variance', 'Variance'),
        ('expiry_before', 'Expiry was'), ('expiry_after', 'Expiry now'),
        ('batch_before', 'Batch was'), ('batch_after', 'Batch now'),
        ('counted_by', 'Counted by'), ('timestamp', 'When'),
        ('note', 'Note')]),
}


def _register_csv(kind: str, rows: Iterable[Doc]) -> tuple[str, str]:
    """(filename, csv text) for one of the registers."""
    title, columns = REGISTER_CSV[kind]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([label for _f, label in columns])
    for row in rows or []:
        writer.writerow([_csv_value(row.get(f) if hasattr(row, 'get') else None)
                         for f, _lab in columns])
    stamp = local_now().strftime('%Y%m%d')
    return f"{title.lower().replace(' ', '-')}-{stamp}.csv", out.getvalue()


def _register_csv_response(kind: str, rows: Iterable[Doc]) -> Response:
    """The download itself.

    An empty register still hands over the file, with its column headings and
    no rows. The first version refused and flashed a message instead — but
    these pages do not all render flashed messages, so on the audit log the
    button did nothing at all and said nothing about why. A file that says
    "no rows" is honest; a button that silently does nothing is not.
    """
    filename, body = _register_csv(kind, rows)
    resp = make_response(body)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


# ---------------------------------------------------------------------
# Visits that look like the same visit recorded twice.
#
# Until today a partial dispense left the whole visit sitting in the form —
# the message said "Partial", not "successfully", and only the word
# "successfully" cleared it. The medicines that HAD dispensed were already
# recorded and their stock already deducted, so pressing Dispense again, the
# natural thing to do when a form looks unsubmitted, dispensed them a second
# time. The form is fixed; the records it made are still there, with the
# stock counted out twice.
#
# Matching is deliberately narrow: same patient, same medication, same
# quantity, minutes apart. A patient genuinely coming back the same day for
# the same drug at the same dose within a few minutes does not happen.
# ---------------------------------------------------------------------
DUPLICATE_WINDOW_MINUTES = 10


def _duplicate_visits(transactions: Collection, days: int = 180, limit: int = 200) -> Docs:
    """Visits that are probably one visit recorded twice.

    Returns a list of {patient, med_name, quantity, first, second, minutes,
    units, value, ids} — never a verdict. The pharmacist decides; this only
    says where to look.
    """
    since = datetime.utcnow() - timedelta(days=days)
    try:
        rows = list(transactions.find(
            {'type': 'dispense', 'timestamp': {'$gte': since}},
            {'patient': 1, 'med_name': 1, 'quantity': 1, 'price': 1,
             'line_value': 1, 'timestamp': 1, 'transaction_id': 1,
             'dispenser': 1}).sort('timestamp', 1))
    except Exception as e:
        app.logger.warning(f'duplicate scan unavailable: {e}')
        return []

    by_key: dict[tuple[Any, ...], Docs] = {}
    for r in rows:
        key = ((r.get('patient') or '').strip().lower(),
               (r.get('med_name') or '').strip().lower(),
               r.get('quantity'))
        by_key.setdefault(key, []).append(r)

    out = []
    window = timedelta(minutes=DUPLICATE_WINDOW_MINUTES)
    for (_pat, _med, _qty), group in by_key.items():
        if len(group) < 2:
            continue
        for a, b in zip(group, group[1:]):
            ta, tb = a.get('timestamp'), b.get('timestamp')
            if not ta or not tb or (tb - ta) > window:
                continue
            # Two lines of the SAME visit are not a duplicate: a prescription
            # can legitimately repeat a medicine across its own lines.
            if a.get('transaction_id') and a.get('transaction_id') == b.get('transaction_id'):
                continue
            out.append({
                'patient': b.get('patient', ''),
                'med_name': b.get('med_name', ''),
                'quantity': b.get('quantity', 0),
                'first': ta, 'second': tb,
                'minutes': round((tb - ta).total_seconds() / 60, 1),
                'dispenser': b.get('dispenser', ''),
                'units': b.get('quantity', 0) or 0,
                'value': _line_value(b),
                'ids': [a.get('transaction_id'), b.get('transaction_id')],
            })
    out.sort(key=lambda d: d['second'], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------
# Getting everything out.
#
# Every other download in this app answers one question — a month's
# dispensing, a stock-on-hand list. None of them is the REGISTER. Years of
# dispensing, the controlled drug movements, the key register and the
# temperature record live in one database in one cluster, and a MongoDB
# Atlas free tier takes no backups of its own. If that cluster is lost, or
# the account lapses, the register goes with it and there is nothing on
# paper to go back to.
#
# So: one button that writes the whole thing to a file the pharmacy keeps.
# JSON Lines rather than CSV, because this is a backup and has to come back
# in — one document per line, the shape Mongo itself reads with
# `mongoimport`. The archive also carries a plain-English README, because
# the person who needs it will be having a bad day and may not be you.
# ---------------------------------------------------------------------
BACKUP_COLLECTIONS = [
    'medications', 'transactions', 'deliveries', 'stock_takes',
    'stock_take_counts', 'temperature_logs', 'loan_items', 'loans',
    'key_register', 'key_officers', 'key_control', 'users', 'audit_log',
    'app_warnings', 'error_logs',
]


def _json_safe(value: Any) -> Any:
    """Anything Mongo stores, written so it can be read back."""
    if isinstance(value, ObjectId):
        return {'$oid': str(value)}
    if isinstance(value, datetime):
        # Stored naive, in UTC, like everything else this app writes.
        return {'$date': value.replace(microsecond=0).isoformat() + 'Z'}
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


BACKUP_README = """NMC Pharmacy — full database backup
===================================

Taken : {when}
By    : {who}
Source: {db}

WHAT THIS IS
    Every record the pharmacy system holds, at the moment above. One file
    per collection. Each line of a .jsonl file is one record.

    This is the whole register: dispensing history, the controlled drug
    movements, deliveries, stock takes, the temperature record, the key
    register, equipment loans and the audit log.

WHAT IS IN IT
{counts}

KEEP IT SOMEWHERE ELSE
    A backup on the same machine as the thing it is backing up is not a
    backup. Put a copy somewhere that would survive losing this computer.

    Passwords are stored only as one-way hashes and cannot be read out of
    this file, but it still holds patient names and their diagnoses. Treat
    it exactly as you would the paper register: it is confidential.

PUTTING IT BACK
    Each file restores with mongoimport, one collection at a time:

        mongoimport --uri "<your connection string>" \
                    --collection transactions \
                    --file transactions.jsonl

    Restore into an EMPTY database first and check it before pointing the
    live app at it. --drop replaces a collection outright, so only reach
    for it when you are certain.
"""


def _backup_archive(db: Database, taken_by: str) -> tuple[str, bytes]:
    """(filename, bytes) — the whole database as a dated zip."""
    buf = io.BytesIO()
    counts: dict[str, Any] = {}
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in BACKUP_COLLECTIONS:
            lines = []
            try:
                for doc in db[name].find():
                    lines.append(json.dumps(_json_safe(doc),
                                            ensure_ascii=False))
            except Exception as e:
                # One unreadable collection must not cost the pharmacy the
                # other fourteen. Say so in the archive rather than failing.
                app.logger.warning(f'backup: {name} unavailable: {e}')
                zf.writestr(f'{name}.FAILED.txt',
                            f'This collection could not be read: {e}\n')
                counts[name] = 'could not be read'
                continue
            counts[name] = len(lines)
            zf.writestr(f'{name}.jsonl', '\n'.join(lines) + ('\n' if lines else ''))
        when = local_now().strftime('%Y-%m-%d %H:%M')
        width = max((len(n) for n in counts), default=10)
        rows = '\n'.join(f'    {n.ljust(width)}  {counts[n]}'
                          for n in BACKUP_COLLECTIONS if n in counts)
        zf.writestr('README.txt', BACKUP_README.format(
            when=when, who=taken_by, db='pharmacy_db', counts=rows))
        zf.writestr('manifest.json', json.dumps({
            'taken_at': when, 'taken_by': taken_by,
            'database': 'pharmacy_db', 'counts': counts,
            'format': 'one JSON document per line (mongoimport)',
        }, indent=1))
    stamp = local_now().strftime('%Y%m%d-%H%M')
    return f'nmc-pharmacy-backup-{stamp}.zip', buf.getvalue()


@app.route('/backup', methods=['GET'])
@login_required
def backup_download() -> ResponseReturnValue:
    """The whole register, in one file, for the pharmacy to keep."""
    if session['user'].get('role') != 'admin':
        flash('Only an administrator can download the full backup.')
        return redirect(url_for('reports'))
    try:
        db = get_mongo_client()['pharmacy_db']
        taken_by = session['user']['name']
        filename, blob = _backup_archive(db, taken_by)
    except ServerSelectionTimeoutError:
        flash('Database connection failed, so no backup was taken. '
              'Nothing was changed.')
        return redirect(url_for('reports'))
    write_audit_entry('EXPORT', 'database_backup', filename,
                      f'Full database backup downloaded by {taken_by}.')
    resp = make_response(blob)
    resp.headers['Content-Type'] = 'application/zip'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


# The companies whose staff the pharmacy serves, and the jobs they do.
# These SEED the suggestions; the live list is whatever has actually been
# recorded, so a new contractor appears the first time one of its employees
# is seen rather than when someone remembers to edit this file.
COMPANY_SEED = [
    'BLW', 'BUSY BEE', 'CMS', 'Consulmet', 'Eminence', 'Enaex', 'ER24',
    'Government', 'IFS', 'LD', 'LISELO', 'LMPS', 'Mendi', 'MGC', 'MINOPEX',
    'NMC', 'Other', 'PLATO', 'Public', 'THOLO', 'TOMRA', 'UL4', 'UNITRANS',
]
POSITION_SEED = [
    'Administration', 'Artisan', 'Blasting', 'Boiler Maker', 'Chef', 'CI',
    'Cleaner', 'Controller', 'Diesel Depo', 'Director', 'Drilling', 'Drivers',
    'Electricians', 'Emergency Coordinator', 'Environmnet', 'Finance',
    'Fitters', 'Food Service Attendant', 'General Worker', 'Geologist', 'Hse',
    'Housekeeping', 'Intern', 'IT', 'Kitchen', 'Lab Technologist',
    'Maintenance', 'Management', 'Manager', 'Mechanics', 'Medical Doctor',
    'Metallurgy', 'Mining', 'Nurse', 'Operator', 'Other', 'Pharmacist', 'PHC',
    'Plant Operator', 'Police', 'Procurement', 'Process', 'Production',
    'Public', 'Recovery', 'Rope Access', 'Security', 'Sorting', 'Storekeeper',
    'Supervisor', 'Survey', 'Technician', 'Tourist', 'Training', 'Treatment',
    'Tyreman', 'UNITRANS', 'Visitor', 'Water Works', 'Welder',
    'Workshop Cleaners', 'X-Ray Technologist',
]
PEOPLE_OPTION_LIMIT = 400


def _recorded_values(transactions: Collection, field: str, seed: Iterable[str]) -> list[str]:
    """What has been recorded in this field, plus the standing list.

    Case-folded so "Unitrans" and "UNITRANS" do not both appear: the first
    spelling seen wins, which keeps whatever the pharmacy actually writes
    rather than imposing a house style on their own records.
    """
    out, seen = [], set()
    for name in sorted(seed, key=str.lower):
        if name.strip() and name.strip().lower() not in seen:
            seen.add(name.strip().lower())
            out.append(name.strip())
    try:
        for value in transactions.distinct(field, {'type': 'dispense'}):
            v = (value or '').strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
    except Exception as e:
        app.logger.warning(f'{field} suggestions unavailable: {e}')
    return sorted(out, key=str.lower)[:PEOPLE_OPTION_LIMIT]


@app.route('/api/people-options', methods=['GET'])
@login_required
def people_options() -> ResponseReturnValue:
    """Companies and positions to suggest, as JSON."""
    try:
        transactions = get_mongo_client()['pharmacy_db']['transactions']
        return jsonify({
            'companies': _recorded_values(transactions, 'company', COMPANY_SEED),
            'positions': _recorded_values(transactions, 'position', POSITION_SEED),
        })
    except Exception as e:
        app.logger.warning(f'people options unavailable: {e}')
        return jsonify({'companies': sorted(COMPANY_SEED, key=str.lower),
                        'positions': sorted(POSITION_SEED, key=str.lower)})


@app.route('/api/diagnoses', methods=['GET'])
@login_required
def get_diagnosis_suggestions() -> ResponseReturnValue:
    return jsonify(_diagnosis_matches(request.args.get('query', '')))


# These names SEED an empty database. They are not the suggestion list any
# more: that comes from the medications collection, which already knows what
# is on file (see _medication_names). Keeping a hand-edited list as the
# suggestions meant editing this file whenever stock changed, and when the two
# drifted apart the app offered names that did not exist — so a prescription
# could be typed out in full and then fail with "item not on file". This list
# is therefore only a fallback for a pharmacy with nothing on file yet, and it
# does not need updating as stock changes.
MEDICATION_SEED_NAMES = [
    # Taken 2026-10-20 from the pharmacy's own Stock on Hand listing.
    '3HP 300/300mg',
    'ABC/3TC 600/300mg',
    'Acetylsalisylic Acid, 100 mg',
    'Acetylsalisylic Acid, 300 mg',
    'Activated Charcoal, 050 g',
    'Actrapid, 100 IU',
    'Acyclovir Cre 5 Perc, 010 mg',
    'Acyclovir, 800 mg',
    'Adalat, 030 mg',
    'Adcorectic, 050 mg',
    'Adenosine, 006 mg',
    'Adrenalin Hcl Inj, 001 mg',
    'Alcohol Swab',
    'Alcophyllex Syrup',
    'Alcophyllin Syrup',
    'Allopurinol, 100 mg',
    'Aminophyllin Injection, 250 mg',
    'Amiodarone, 006 mg',
    'Amitryptyline, 025 mg',
    'Amlodipine, 010 mg',
    'Amoxycillin Cap, 250 mg',
    'Amoxyclav Injection, 1200 mg',
    'Amoxyclav, 375 mg',
    'Amoxyclav, 625 mg',
    'Anti Haemorrhoidal Suppositories, 100 mg',
    'Anti Snake Bite Serum, 010 ml',
    'Antirubbies, 2.5 IU',
    'Arachis Oil, 020 ml',
    'Asccorbic Acid Tab - Chewable, 250 mg',
    'Atenolol, 050 mg',
    'Atenolol, 100 mg',
    'Atorvastatin, 010 mg',
    'Atropine Injection, 0.5 mg',
    'Azithromycin, 500 mg',
    'AZT/3TC 300/150mg',
    'Baclofen, 010 mg',
    'Beclomethasone Inhaler, 200 MID',
    'Benzathine Pen, 2.4 MU',
    'Benzoic Salicylic Ointment (Whitfield), 500 g',
    'Benzyl Benzoate, 100 ml',
    'Betamethasone Cream, 500 g',
    'Bisacodyl Tab, 005 mg',
    'Calamine Lotion, 100 ml',
    'Calcium Gluconate Tabs, 300 mg',
    'Captopril Tab, 050 mg',
    'Carbamazepine, 200 mg',
    'Carvedilol, 12.5mg',
    'Ceftriaxone Injection, 1000 mg',
    'Ceftriaxone, 250 mg',
    'Celebrex, 200 mg',
    'Cetrizine, 010 mg',
    'Chlopromazine, 025 mg',
    'Chloramphenicol Caps, 250 mg',
    'Chloramphenicol Eye Drops, 010 ml',
    'Chloramphenicol Eye Oint, 005 g',
    'Chlorhexide Mouth Wash, 100 ml',
    'Chloro Ear Drops, 020 ml',
    'Chlorpheniramine Tabs, 004 mg',
    'Cimetidine Injection, 200 mg',
    'Cimetidine Tabs, 200 mg',
    'Cimetidine tabs, 400 mg',
    'Cipro Eye Drops, 010 ml',
    'Ciprofloxacin, 500 mg',
    'Clarythromycin, 500 mg',
    'Clinical Thermometer',
    'Clinical Thermometer Digita',
    'Clopidogrel, 075 mg',
    'Clotrimazole Cre Vaginal, 010 mg',
    'Clotrimazole Pess, 100 mg',
    'Clotrimazole Topical Cre 1%, 020 g',
    'Cloxacillin Caps, 250 mg',
    'Cloxacillin Injection, 250 mg',
    'Colchicine Tabs, 0.5 mg',
    'Combined Pill',
    'Cotrimoxazole, 480 mg',
    'Cotrimoxazole, 960 mg',
    'Cotton Buds',
    'Cotton Wool',
    'Crepe Bandage 150mm',
    'Crepe Bandage 50mm',
    'Crepe Bandage 75mm',
    'CTX 960mg',
    'Cyproheptadine, 004 mg',
    'Deep Freeze Spray, 050 ml',
    'Dexaethasone Eye Drops, 002 ml',
    'Dexamethasone Injection, 004 mg',
    'Dextrose Injection, 050 %',
    'Diazepam Injection, 010 mg',
    'Diazepam Tabs, 005 mg',
    'Diclofenac Gel, 050 g',
    'Diclofenac Injection, 075 mg',
    'Diclofenac Tab, 025 mg',
    'Diclofenac Tab, 050 mg',
    'Digoxin, 0.250 mg',
    'Diphenhydramine Syrup, 100 ml',
    'Dopamine Injection, 010 mg',
    'Doxycycline Tabs, 100 mg',
    'Dressing Pack',
    'DTG 50mg',
    'Dynexan Oral Gel, 010 mg',
    'Elasto-Plast',
    'Emergency Pill',
    'Emergency Pill, 002 mg',
    'Enalapril tabs, 005 mg',
    'Enalapril tabs, 010 mg',
    'Enalapril tabs, 20 mg',
    'Ergometrine Injection, 0.5 mg',
    'Erythromycin tabs, 250 mg',
    'Examination Gloves L',
    'Examination Gloves M',
    'Fentanyl, 100 mcg',
    'Ferrous Sulphate Tabs, 200 mg',
    'Fertomid, 050 mg',
    'Flagyl Injection, 400 mg',
    'Flu Stat, 200 mg',
    'Fluconazole, 200 mg',
    'FluoxetineCaps, 020 mg',
    'Fml Neo Opd, 005 ml',
    'Folic Acid Tabs, 005 mg',
    'Freestyle Optium Test Strip',
    'Furosemide Injection, 020 mg',
    'Furosemide Tabs, 040 mg',
    'Gabapentine, 100 mg',
    'Gauze Swab',
    'Gentamycin Injection, 040 mg',
    'Glibenclamide Tabs, 005 mg',
    'Gliclazide, 080 mg',
    'Glucose Powder, 500 g',
    'Glycerine Supp, 100 mg',
    'Griseofulvin Tabs, 500 mg',
    'Guafenesin Xl 60, 100 ml',
    'Gv Paint, 020 ml',
    'Haloperidol Injection, 002 mg',
    'Haloperidol Tabs, 1.5 mg',
    'Heparine, 1000 SIU',
    'Histacon Caps, 200 mg /2mg',
    'Hydralazine Hcl Tabs, 010 mg',
    'Hydralazine Hcl Tabs, 050 mg',
    'Hydrochlorothiazide Tabs, 025 mg',
    'Hydrocortisone Cream, 500 g',
    'Hydrocortisone Injection, 100 mg',
    'Hyoscine Injection, 020 mg',
    'Hyoscine Tabs, 010 mg',
    'Ibuprofen Tab, 200 mg',
    'Ibuprofen Tab, 400 mg',
    'Ichthammol Ointment, 500 g',
    'Imipramine, 010 mg',
    'Indapamide Tabs, 0.5 mg',
    'Indomethacin Caps, 025 mg',
    'Insulin Hm Injection, 100 U 10 Ml',
    'Isosorbide Trinitrate, 005 mg',
    'Ketamine Injection, 050 mg (Ml)',
    'Keteconazole, 200 mg Tabs',
    'Labetolol, 5mg',
    'Lactulose, 150 ml',
    'Lancets',
    'Levonorgestrel 0.03mg',
    'Levonorgestrel 75mg Implants',
    'Lignocaine Injection, 002 %',
    'Lignocaine Spray, 050 ml',
    'Linen Saver',
    'Liquid Paraffin, 100 ml',
    'Loperamide Tabs, 002 mg',
    'Loratadine, 010 mg',
    'Losartan, 050 mg',
    'Losartan, 100 mg',
    'LPVr 200/50mg',
    'Lubrucating Gel, 050 g',
    'Magnesium Suphate injection, 010 mg',
    'Male Condoms',
    'Mannitol, 020 %',
    'Mayogel Suspension',
    'Mebendazole, 100 mg Tabs',
    'Medicine Bottles',
    'Medroxyprogesterone 150mg',
    'Mefenamic Acid, 250 mg',
    'Mepyramine Cream, 025 g',
    'Mercurochrome Paint, 020 ml',
    'Metformin Tabs, 500 mg',
    'Metformin Tabs, 850 mg',
    'Methotrexate, 005 mg',
    'Methylcellulose Eye Drops, 010 ml',
    'Methylprednisone Injection, 040 mg',
    'Methylsal Ointment, 500 mg',
    'Metoclopramide Injection, 010 mg',
    'Metoclopramide Tabs, 010 mg',
    'Metronidazole tabs, 400 mg',
    'Miconazol Oral Gel, 030 g',
    'Miconazole Cream, 002 %',
    'Midazolam, 010 mg',
    'Migril, 002 mg',
    'Mist Alba Susp, 100 ml',
    'Mmt, 250 mg',
    'Morphine Injection, 010 mg',
    'Morphine tabs, 10mg',
    'Multivitamin Tabs, 0.25 mg',
    'Mybulen, 200 mg',
    'N95 Masks',
    'Naloxone, 0.4 mg',
    'Nasal Drops- Oxymetazoline, 005 ml',
    'Needles 21g',
    'Neurobion Tabs, 200 mg',
    'Nifedipine, 005 mg',
    'Nifedipine, 010 mg',
    'Nitrofurantoin, 100 mg',
    'Nitrofurazone Ointment, 500 g',
    'Nitrolingual Spray, 020 ml',
    'Norflex Co Tabs, 375 mg',
    'Normal Saline 100ml',
    'Normal Saline 1L',
    'Nystatin Ointment, 020 g',
    'Nystatin Oral Susp, 1000 u',
    'Nystatin Vaginal Pess, 100 mg',
    'Ointment Jars',
    'Omeprazole Tabs, 020 mg',
    'Oral Rehydration Salts, 002 g',
    'Osteoeze Gold, 200 mg',
    'Oxytocin Injection, 010 mg',
    'Pain Relief Gel, 020 g',
    'PanaCod Tab, 500 mg',
    'Paper Bags L',
    'Paracetamol tabs, 500 mg',
    'Pen Light Torch',
    'Pen Vk Tab, 250 mg',
    'Pentaprazole Injection, 040 mg',
    'Perfulgan, 001 g',
    'Pethedine Injection, 050 mg',
    'Pethedine Injection, 100 mg',
    'Phenytoin Injection, 200 mg',
    'Phernobabitol tabs, 020 mg',
    'Plasma Expander',
    'Podophylline Paint, 020 ml',
    'POP 75mm',
    'Potassium Chloride Injection',
    'Potassium Chloride tabs, 600 mg',
    'Potassium Citrate, 100 ml',
    'Povidone Iodine Solution',
    'Povidone Ointment, 500 mg',
    'Pravastatin tabs, 020 mg',
    'Prednisone Tab, 005 mg',
    'Probanthine Tabs, 015 mg',
    'Prochlorperazine Injection, 005 mg',
    'Prochlorperazine Tabs, 005 mg',
    'Promethazine Injection, 050 mg',
    'Promethazine Tabs, 025 mg',
    'Propranolol Tabs, 010 mg',
    'Propranolol Tabs, 040 mg',
    'Pyridoxine, 025 mg',
    'Pyridoxine, 25 mg Donor-Funded',
    'Ranitidine, 150 mg',
    'RH 150/75mg',
    'RHE 150/75/275mg',
    'RHZE 150/75/400/275mg',
    'Rocuronium injection, 010 mg',
    'Salbutamol Inhaler, 200 MID',
    'Salbutamol, 004 mg Tablets',
    'Sanitary Pads',
    'Savlon',
    'Selenium Tab, 100 mg',
    'Sharps Container',
    'Sildenafil, 050 mg',
    'Simvastatin, 020 mg',
    'Sinucon Tab, 200 mg',
    'Sodium Bicarbonate, 050 ml',
    'Sodium Valproate, 200 mg',
    'Spersallerg Opd, 010 ml',
    'Spironolactone tabs, 025 mg',
    'Sterile Trocar',
    'Suppositories Indocid (Arthrexin), 100 mg',
    'Surgical Masks',
    'Suxamethonium injection, 010 mg',
    'Syringes 3ml',
    'TDF/3TC 300/300mg',
    "TDF/3TC/DTG 300/300/50mg 30's",
    "TDF/3TC/DTG 300/300/50mg 90's",
    'Tetanus Toxoid Vaccine, 010 mg',
    'Tetracycline Ointment, 003 % 25G',
    'Tetracycline Opthal Ointment, 020 g',
    'Thiamine Injection',
    'Throat Lozenges, 250 mg',
    'Thymol Glycerine, 100 ml',
    'Tongue Depressors',
    'Tramadol Injection, 100 mg',
    'Tramadol Tabs, 050 mg',
    'Tranexamic Acid Injection, 500 mg',
    'Tranexamic Acid tabs, 500 mg',
    'Trifen Adult, 100 ml',
    'Tumsulosin, 0.5 mg',
    'Urirex K, 050 mg',
    'Venteze Resp.Sol, 005 mg 20ml',
    'Vitamin B Co Injection, 001 mg',
    'Vitamin B Co Tablets, 001 mg',
    'Vitamin B12, 002 mg',
    'Vitamin E Cream, 500 g',
    'Vitamin K Injection (Konakion), 001 mg',
    'Warfarin Tabs, 005 mg',
    'Water For Injection, 010 ml',
    'Zinc Oxide Ointment, 030 mg',
    'Zinc Tablets, 020 mg',
    'ZnO Plater 100mm',
    'ZnO Plater 75mm',
    'Zuvamor, 040 mg',
]

@app.route('/api/session-ping', methods=['POST'])
@login_required
def session_ping() -> ResponseReturnValue:
    """Tell the server the user is still here.

    The timeout is meant to measure INACTIVITY, but the only activity it
    could see was a page request. Filling in a long dispensing form makes no
    requests at all, so twenty minutes of typing looked exactly like twenty
    minutes of an empty room — and the submit at the end landed on the login
    page with the work lost.

    The page calls this only when the user actually interacts with it, so an
    unattended terminal still times out as intended. The before_request hook
    has already refreshed the activity stamp by the time this runs; all this
    has to do is answer.
    """
    return jsonify({'ok': True, 'timeout_minutes': SESSION_TIMEOUT_MINUTES})


@app.route('/api/patient-lookup', methods=['GET'])
@login_required
def patient_lookup() -> ResponseReturnValue:
    """What this patient's last visit recorded about them.

    Company, position, birth decade and gender are facts about a person, not
    about a visit, so a returning patient should not have to supply them
    again. Matched on the exact name, case-insensitively: anything looser
    would attach one person's employer to another's record, which is worse
    than typing it out.

    The date of the visit it came from is returned too, because two people
    genuinely can share a name. The form says where the details came from so
    a wrong match is visible rather than silently believed.
    """
    name = (request.args.get('name') or '').strip()
    if not name:
        return jsonify({})
    try:
        db = get_mongo_client()['pharmacy_db']
        doc = db['transactions'].find_one(
            {'type': 'dispense',
             'patient': {'$regex': f'^{re.escape(name)}$', '$options': 'i'}},
            {'_id': 0, 'patient': 1, 'company': 1, 'position': 1,
             'birth_decade': 1, 'gender': 1, 'timestamp': 1, 'date': 1},
            sort=[('timestamp', -1)])
        if not doc:
            return jsonify({})
        when = doc.get('date') or ''
        if not when and doc.get('timestamp'):
            when = doc['timestamp'].strftime('%Y-%m-%d')
        return jsonify({
            'patient': doc.get('patient', ''),
            'company': doc.get('company', ''),
            'position': doc.get('position', ''),
            'birth_decade': doc.get('birth_decade', ''),
            'gender': doc.get('gender', ''),
            'last_visit': when,
        })
    except Exception as e:
        app.logger.warning(f'patient lookup failed: {e}')
        return jsonify({})


@app.route('/api/medication-defaults', methods=['GET'])
@login_required
def get_medication_defaults() -> ResponseReturnValue:
    """Pack size and schedule for each stocked item, keyed by name.

    These describe the PRODUCT, so they are the same on every delivery and
    there is no reason to retype them. Price is deliberately not included:
    that describes this particular purchase, and pre-filling it would invite
    somebody to accept last month's figure without reading the invoice.

    Leaving pack size to be retyped was not merely tedious — the field resets
    to 1, and a pack size of 1 values a pack-priced item at the pack price per
    unit, which is the inflation this app was already fixed for once.
    """
    try:
        db = get_mongo_client()['pharmacy_db']
        out = {}
        for m in db['medications'].find(
                {}, {'_id': 0, 'name': 1, 'pack_size': 1, 'schedule': 1}):
            name = m.get('name')
            if not name:
                continue
            out[name] = {'pack_size': m.get('pack_size') or '',
                         'schedule': m.get('schedule') or ''}
        return jsonify(out)
    except Exception as e:
        app.logger.warning(f'medication defaults failed: {e}')
        return jsonify({})


def _medication_names() -> list[str]:
    """Every medication on file, for the name suggestions.

    Asked of the medications collection rather than kept in a list here, so
    that adding an item under Add Medication makes it suggestable everywhere
    at once and nothing has to be edited by hand. A name that is suggested is
    therefore a name that exists, which is the whole point: the old list could
    offer "Morpine tabs, 10mg" long after the register had been corrected to
    "Morphine", and the dispense only failed at the end.

    Zero-balance items are included on purpose. Dispensing one records the
    shortfall as unmet demand, which is a figure the pharmacy orders against —
    hiding those names would quietly stop that being recorded.
    """
    try:
        db = get_mongo_client()['pharmacy_db']
        names = db['medications'].distinct('name')
        out = sorted((n.strip() for n in names if n and str(n).strip()),
                     key=lambda t: t.lower())
        # Only while nothing is on file: a brand-new pharmacy gets something
        # to type against rather than a blank box.
        return out or sorted(MEDICATION_SEED_NAMES, key=lambda t: t.lower())
    except Exception as e:
        app.logger.warning(f'medication name list unavailable: {e}')
        # Free typing still works, and the server checks the name either way.
        return []


@app.route('/api/medications', methods=['GET'])
@login_required
def get_medication_options() -> ResponseReturnValue:
    """The medication names to suggest, as JSON.

    Templates fetch this once per page load and cache it in JS module scope,
    so a medication added in one tab appears in the next page that loads.
    """
    return jsonify(_medication_names())


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
