import os
import requests
from flask import Flask, request, render_template_string, jsonify, redirect, url_for, session, flash
from functools import wraps
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from collections import defaultdict
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
load_dotenv() # Loads .env into os.environ
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
# Diagnosis options
DIAGNOSES_OPTIONS = [
    'ARDS', 'Abscess', 'Acne (Moderate to severe)', 'Acute Bronchitis', 'Acute Gastroenteritis (AGE)',
    'Acute appendicitis', 'Acute otitis media', 'Acute sinusitis', 'Acute stress disorder',
    'Acute tonsilitis /pharyngitis', 'Alcohol intoxication', 'Allergic conjunctivitis',
    'Allergic rhinitis', 'Allergic skin reaction (unspecified)', 'Allergies', 'Anaemia',
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
    'Hallus valgus deformity', 'Head injury (TBI) (mild , moderate, Severe)',
    'Headaches (tension, migrane, cluster etc)', 'Hepatitis (alcohol induced, viral etc)',
    'Herpes labialis (cold sore)', 'Herpes zoster', 'Hiccups', 'High altitude syndrome',
    'Hypercholesterolaemia', 'Hypertriglyceridaemia', 'Hypotension', 'I & D',
    'Illio-tibial band syndrome', 'Indigestion', 'Inflamatory bowel disease / IBS',
    'Influenza', 'Ingrown', 'Injury', 'Insect bite', 'Insomnia',
    'Insufficient sleep syndrome', 'Internal/external haemorrhoids', 'Kaposis Sarcoma',
    'LRTI (unspecified)', 'Laryngitis', 'Lightening injury', 'Ligmament (unspecified) sprain',
    'Lipoma', 'Loose teeth (unspecified cause)', 'Lower GI bleed (unspecified)',
    'Lymphadenitis', 'Mechanical low back pain', 'Medication induced cough(ACE-I etc)',
    'Medication side effects', 'Mucus hypersecretion', 'Muscle (unspecified) strain',
    'Musculoskeletal', 'Myalgia/muscle tension/ spasm', 'Myocardiac infaction/ ACS',
    'Nasal lession/infection', 'Nasal polyp', 'Nausea and Vomiting', 'Negative on AgRDT',
    'Neuralgia / neuritis', 'Non specific spinal pain (cervicalgia, thoracalgia, lumbalgia)',
    'Obstructive sleep apnea', 'Opthalmological', 'Oral lession(s)', 'Osteoarthritis',
    'Otitis externa', 'PICA', 'PJP', 'PUD/gastritis', 'Pancreatitis', 'Papule, pastules',
    'Parasthesia', 'Peri-orbital lession (undefined)', 'Perianal abscess', 'Periodontitis',
    'Peripheral neuropathy', 'Peripheral vascular disease', 'Pinguecula', 'Planta fascitis',
    'Pleural effusion', 'Pleuritic chest pain/pleuritis', 'Pneumonia', 'Polyarthralgia',
    'Poor vision', 'Positive on AgRDT', 'Post covid hyperactive airway DX',
    'Preseptal cellulitis', 'Psychiatric', 'Psychosomatic Disorder', 'Pterygium',
    'Radiculopathy (cervical, thoracic, lumber)', 'Respiratory', 'Rheumatoid arthritis',
    'Rheumatological_Ortho', 'Rynaulds phenomenon', 'Scabies', 'Sceptic nasal piercing',
    'Sciatica', 'Smoke inhilation injury', 'Spinal abnormality (scoliosis, kyphosis etc)',
    'Spondylolisthesis', 'Spondylosis', 'Spondylosis/spondylolisthesis', 'Stye /',
    'Surgical', 'Surgical site infection (post ganglion cyst removal)', 'Swan neck deformity',
    'Syncope', 'TB (lungs, pleura, meningeal, spine etc)', 'TB Meningitis', 'TIA / CVA',
    'Temporal arteritis', 'Tendon injury', 'Tendonitis', 'Tennis elbow', 'Thoracic back pain',
    'Tinnitus', 'Toothache (no obvious decay/caries)', 'Torticolis', 'Traumatic conjunctivitis',
    'Tumour (mass) undefined', 'Typhoid', 'URTI (Unspecified)', 'Ulcer',
    'Upper GI bleed', 'Urticaria', 'Venous insufficiency', 'Viral conjunctivitis',
    'Viral rhinitis/ common cold', 'Warts', 'Worm infestation', 'faecal incontinence',
    'hordeolum', 'injuries', 'injuries on duty', 'injury (RTA)', 'nail',
    'STI (MUS, VDS, Herpes genitalia, syphilis etc)', 'Post HIV exposure', 'Post operative adhessions',
    'Vaginal candidiasis', 'Cervixitis', 'UTI', 'Pelvic hypertension', 'Dysmenorrhoea', 'PID',
    'Hormonal imbalance', 'PCOS', 'Ovarian cyst (simple/complex)', 'Pelvic mass (undefined)',
    'Fibroid uterus', 'Pregnancy', 'Supplementation', 'Normal mentrual period', 'Menorrhagia',
    'Miscarriage (threatened, incomplete, complete etc)', 'Secondary ammenorrhoea(unspecified)',
    'Post menopausal syndrome', 'Post menauposal bleeding', 'Dysfunctional uterine beeding',
    'Inhibited sexual desire (female)', 'Erectile dysfunction (male)', 'Epididimorchitis',
    'Urine Incontinence', 'Hydrocele', 'Acute urinary retension', 'Urinary catheter blockage',
    'BPH', 'Acute kidney injury (unspecified)', 'Chronic kidney disease (CKD)',
    'Drug induced kidney injury', 'Urethral stricture/Urinary outlet obstruction', 'Kidney stone',
    'Bladder stone', 'Warts', 'DM', 'Hyperglycaemia', 'Hypoglycaemia', 'DKA', 'HHS'
]
# MongoDB connection function (lazy initialization for fork-safety)
def get_mongo_client():
    monguri = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
    return MongoClient(monguri, serverSelectionTimeoutMS=120000)
# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function
# Navigation links (updated to include user info and logout)
def get_nav_links():
    if 'user' in session:
        user = session['user']
        is_admin = user.get('role') == 'admin'
        name = user.get('name', user.get('login', 'User'))
        add_med_link = '<a href="/add-medication">Add Medication</a> | ' if is_admin else ''
        return f"""
        <p class="nav-links"><strong>Navigate:</strong>
            <a href="/dispense">Dispensing</a> |
            <a href="/receive">Receiving</a> |
            {add_med_link}
            <a href="/reports">Reports</a> |
            <span>Welcome, {name}! <a href="/logout">Logout</a></span>
        </p>
        """
    else:
        return """
        <p class="nav-links"><strong>Navigate:</strong>
            <a href="/login">Login</a> | <a href="/register">Register</a>
        </p>
        """
# CSS for all templates (same colors, improved button design)
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

    /* === Improved Buttons (keeping original colors) === */
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

    /* === Action Buttons (Edit / Delete / View) === */
    .action-buttons {
        padding: 6px 10px;
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
    /* === Override Delete Button inside forms === */
    form button.delete-btn {
        background-color: #dc3545 !important;
        color: #fff !important;
        padding: 0 !important; /* smaller size */
        font-size: 13px !important;
        border-radius: 2px !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1) !important;
        transition: all 0.2s ease !important;
        margin: 0 !important;
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
        body {
            padding: 10px;
        }
        form, table {
            max-width: 100%;
        }
        .common-section {
            grid-template-columns: 1fr;
        }
        #medications, #diagnoses {
            grid-template-columns: 1fr;
        }
        .med-row, .diag-row {
            grid-template-columns: 1fr;
        }
        table th, table td {
            font-size: 14px;
            padding: 8px;
        }
        .filter-section {
            grid-template-columns: 1fr;
        }
    }
</style>
"""


DISPENSE_TEMPLATE = CSS_STYLE + """
<h1>Dispensing</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
{{ nav_links|safe }}
{% if message %}
    <p class="message {% if 'successfully' in message|lower or 'updated' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}
<h2>{% if tx_data %}Edit Dispense{% else %}Dispense Medication{% endif %}</h2>
<form method="POST" action="{{ url_for('dispense') }}" class="dispense-form">
    <input type="hidden" name="transaction_id" value="{{ tx_data.transaction_id if tx_data else '' }}">
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
            <input name="sick_leave_days" type="number" min="0" value="{{ tx_data.sick_leave_days if tx_data else '' }}" required>
        </div>
        <div>
            <label>Prescriber (Doctor):</label>
            {% set prescribers = ['Dr. T. Khothatso', 'Locum', 'Malesoetsa Leohla', 'Mamosa Seetsa', 'Mamosaase Nqosa', 'Mapalo Mapesela', 'Mathuto Kutoane', 'Thapelo Mphole'] %}
            <select name="prescriber" {% if not tx_data %}required{% endif %}>
                <option value="">-- Select Doctor --</option>
                {% for p in prescribers %}
                    <option value="{{ p }}" {% if tx_data and tx_data.prescriber == p %}selected{% endif %}>{{ p }}</option>
                {% endfor %}
            </select>
        </div>
        <div>
            <label>Dispenser (Issuer):</label>
            {% set dispensers = ['Letlotlo Hlaoli', 'Locum', 'Malesoetsa Leohla', 'Mamosa Seetsa', 'Mamosaase Nqosa', 'Mapalo Mapesela', 'Mathuto Kutoane', 'Thapelo Mphole'] %}
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
        {% set tx_number = namespace(value=1) %}
        {% for t in tx_list %}
            {% if loop.first or t.transaction_id != tx_list[loop.index0 - 1].transaction_id %}
                <tr style="border-top: 3px double #0056b3;">
                    <td rowspan="{{ tx_list|selectattr('transaction_id', 'equalto', t.transaction_id)|list|length }}"
                        style="vertical-align: middle; font-weight: bold; font-size: 1.1em; color: #0056b3;">
                        {{ tx_number.value }}.
                    </td>
                    {% set tx_number.value = tx_number.value + 1 %}
            {% else %}
                <tr>
            {% endif %}
                    <td>{{ t.date }}</td>
                    <td>{{ t.patient }}</td>
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
                        <a href="{{ url_for('dispense',
                                            edit=t.transaction_id,
                                            start_date=start_date,
                                            end_date=end_date,
                                            search=search) }}">
                            <button type="button" class="edit-btn">Edit</button>
                        </a>
                        {% if session['user']['role'] == 'admin' %}
                        <form method="POST"
                              action="{{ url_for('delete_dispense') }}"
                              style="display:inline-block;"
                              onsubmit="return confirm('Permanently delete this dispense transaction?\nStock will be restored.');">
                            <input type="hidden" name="transaction_id" value="{{ t.transaction_id }}">
                            <input type="hidden" name="start_date" value="{{ start_date or '' }}">
                            <input type="hidden" name="end_date" value="{{ end_date or '' }}">
                            <input type="hidden" name="search" value="{{ search or '' }}">
                            <button type="submit" class="delete-btn">Delete</button>
                        </form>
                        {% endif %}
                    </td>
                </tr>
        {% else %}
        <tr><td colspan="16">No dispense transactions.</td></tr>
        {% endfor %}
    </tbody>
</table>
<script>
let medRowCount = {{ (tx_data.meds|length if tx_data else 1) }};
let diagRowCount = {{ (tx_data.diags|length if tx_data else 1) }};
// Company options array for autocomplete
const companyOptions = [
    "BLW",
    "BUSY BEE",
    "CMS",
    "Consulmet",
    "Enaex",
    "Eminence",
    "ER24",
    "Government",
    "IFS",
    "LD",
    "LISELO",
    "LMPS",
    "Mendi",
    "MGC",
    "MINOPEX",
    "NMC",
    "Other",
    "PLATO",
    "Public",
    "THOLO",
    "TOMRA",
    "UL4",
    "UNITRANS"
];
// Position options array for autocomplete
const positionOptions = [
    "Administration",
    "Artisan",
    "Blasting",
    "Boiler Maker",
    "Chef",
    "CI",
    "Cleaner",
    "Controller",
    "Director",
    "Drilling",
    "Drivers",
    "Electricians",
    "Emergency Coordinator",
    "Environmnet",
    "Finance",
    "Fitters",
    "Food Service Attendant",
    "General Worker",
    "Geologist",
    "Hse",
    "Housekeeping",
    "IT",
    "Intern",
    "Kitchen",
    "Lab Technologist",
    "Maintenance",
    "Management",
    "Manager",
    "Mechanics",
    "Medical Doctor",
    "Metallurgy",
    "Mining",
    "Nurse",
    "Operator",
    "Other",
    "PHC",
    "Pharmacist",
    "Plant Operator",
    "Police",
    "Procurement",
    "Process",
    "Production",
    "Public",
    "Recovery",
    "Rope Access",
    "Security",
    "Sorting",
    "Storekeeper",
    "Supervisor",
    "Survey",
    "Technician",
    "Training",
    "Tourist",
    "Treatment",
    "Tyreman",
    "UNITRANS",
    "Visitor",
    "Water Works",
    "Welder",
    "Workshop Cleaners",
    "X-Ray Technologist"
];
// Medication options array for autocomplete
const medicationOptions = [
    "Acetylsalisylic Acid, 100 mg",
    "Acetylsalisylic Acid, 300 mg",
    "Activated Charcoal, 050 g",
    "Actrapid, 100 IU",
    "Acyclovir Cre 5 Perc, 010 mg",
    "Acyclovir Tab, 200 mg",
    "Acyclovir, 800 mg",
    "Adalat, 030 mg",
    "Adalat, 060 mg",
    "Adcodol, 500 mg",
    "Adcorectic, 050 mg",
    "Adenosine, 006 mg",
    "Adrenalin Hcl Inj, 001 mg",
    "Alcophyllin Syrup, 100 ml",
    "Alcophyllex Syrup, 100 ml",
    "Allopurinol, 100 mg",
    "Aminophyllin Injection, 250 mg",
    "Aminophyllin, 100 mg",
    "Amiodarone, 006 mg",
    "Amitryptyline, 025 mg",
    "Amlodipine, 010 mg",
    "Amoxycillin Cap, 250 mg",
    "Amoxyclav Injection, 1200 mg",
    "Amoxyclav, 625 mg",
    "Ampicillin Caps, 250 mg",
    "Ampiclox Caps, 500 mg",
    "Ampjicillin Injection, 500 mg",
    "Anti Haemorrhoidal Suppositories, 100 mg",
    "Anti Snake Bite Serum, 010 ml",
    "Antirubbies, 2.5 IU",
    "Anusol Ointment, 2500 mg",
    "Arachis Oil, 020 ml",
    "Atorvastatin, 010 mg",
    "Atorvastatin, 020 mg",
    "Asccorbic Acid Tab - Chewable, 250 mg",
    "Atenolol, 050 mg",
    "Atenolol, 100 mg",
    "Atropine Injection, 0.5 mg",
    "Azithromycin, 500 mg",
    "Baclofen, 010 mg",
    "Beclomethasone Inhaler, 200 MID",
    "Benzathine Pen, 2.4 MU",
    "Benzoic Salicylic Ointment (Whitfield), 500 g",
    "Benzyl Benzoate, 100 ml",
    "Benzyl Pen Injection, 005 MU",
    "Betamethasone Cream, 500 g",
    "Bisacodyl Tab, 005 mg",
    "Calamine Lotion, 100 ml",
    "Calcium Gluconate Tabs, 300 mg",
    "Captopril Tab, 050 mg",
    "Carbamazepine, 200 mg",
    "Carvedilol, 12.5mg",
    "Cefotaxime Injection, 001 g",
    "Ceftriaxone Injection, 1000 mg",
    "Ceftriaxone, 250 mg",
    "Celebrex, 200 mg",
    "Cetrizine, 010 mg",
    "Chlopromazine, 025 mg",
    "Chloramphenicol Caps, 250 mg",
    "Chloramphenicol Eye Drops, 010 ml",
    "Chloramphenicol Eye Oint, 005 g",
    "Chlorhexide Mouth Wash, 100 ml",
    "Chloro Ear Drops, 020 ml",
    "Chlorpheniramine Tabs, 004 mg",
    "Cimetidine Tabs, 200 mg",
    "Cimetidine tabs, 400 mg",
    "Cimetidine Injection, 200 mg",
    "Cipro Eye Drops, 010 ml",
    "Ciprofloxacin, 500 mg",
    "Clarythromycin, 500 mg",
    "Cloxacillin Injection, 250 mg",
    "Clopidogrel, 075 mg",
    "Clotrimazole Cre Vaginal, 010 mg",
    "Clotrimazole Pess, 100 mg",
    "Clotrimazole Topical Cre 1%, 020 g",
    "Cloxacillin Caps, 250 mg",
    "Colchicine Tabs, 0.5 mg",
    "Cotrimoxazole, 480 mg",
    "Cotrimoxazole, 960 mg",
    "Cyproheptadine, 004 mg",
    "Deep Freeze Spray, 050 ml",
    "Dexamethasone Eye Drops, 002 ml",
    "Dexamethasone Injection, 004 mg",
    "Dextrose Injection, 050 %",
    "Diazepam Injection, 010 mg",
    "Diazepam Tabs, 005 mg",
    "Diclofenac Injection, 075 mg",
    "Diclofenac Tab, 025 mg",
    "Diclofenac Tab, 050 mg",
    "Diclofenac Gel, 050 g",
    "Digoxin, 0.250 mg",
    "Diphenhydramine Syrup, 100 ml",
    "Dopamine Injection, 010 mg",
    "Doxycycline Tabs, 100 mg",
    "Dynexan Oral Gel, 010 mg",
    "Emergency Pill, 002 mg",
    "Enalapril tabs, 005 mg",
    "Enalapril tabs, 010 mg",
    "Ergometrine Injection, 0.5 mg /ml",
    "Erythromycin tabs, 250 mg",
    "Fentanyl, 100 mcg",
    "Ferrous Sulphate Tabs, 200 mg",
    "Fertomid, 050 mg",
    "Flagyl Injection, 400 mg",
    "Flu Stat, 200 mg",
    "Fluconazole, 200 mg",
    "FluoxetineCaps, 020 mg",
    "Fml Neo Opd, 005 ml",
    "Folic Acid Tabs, 005 mg",
    "Furosemide Injection, 020 mg",
    "Furosemide Tabs, 040 mg",
    "Gabapentine, 100 mg",
    "Gentamycin Injection, 040 mg",
    "Glibenclamide Tabs, 005 mg",
    "Gliclazide, 080 mg",
    "Glucose Powder, 500 g",
    "Glycerine Supp, 100 mg",
    "Griseofulvin Tabs, 500 mg",
    "Guafenesin Xl 60, 100 ml",
    "Gv Paint, 020 ml",
    "Haloperidol Injection, 002 mg",
    "Haloperidol Tabs, 1.5 mg",
    "Heparine, 1000 SIU",
    "Histacon Caps, 200 mg",
    "Hydalazine Injection, 020 mg",
    "Hydralazine Hcl Tabs , 010 mg",
    "Hydralazine Hcl Tabs, 050 mg",
    "Hydrochlorothiazide Tabs, 025 mg",
    "Hydrocortisone Cream, 500 g",
    "Hydrocortisone Injection, 100 mg",
    "Hyoscine Injection, 020 mg",
    "Hyoscine Tabs, 010 mg",
    "Ibuprofen Tab, 200 mg",
    "Ibuprofen Tab, 400 mg",
    "Ichthammol Ointment, 500 g",
    "Imipramine, 010 mg",
    "Indapamide Tabs, 0.5 mg",
    "Indomethacin Caps, 025 mg",
    "Insulin Hm Injection, 100 U 10 Ml",
    "Isosorbide Trinitrate, 005 mg",
    "Ketamine Injection, 050 mg (Ml)",
    "Keteconazole, 200 mg Tabs",
    "Lactulose, 150 ml",
    "Lignocaine Injection, 002 %",
    "Lignocaine Spray, 050 ml",
    "Liquid Paraffin , 100 ml",
    "Lisinopril, 020, mg",
    "Loperamide Tabs, 002 mg",
    "Loratadine, 010 mg",
    "Lorsatan, 050 mg",
    "Lorsatan, 100 mg",
    "Lubrucating Gel, 050 g",
    "Magasil Suspension, 100 ml",
    "Magnesium Suphate injection, 010 mg",
    "Mannitol, 020 %",
    "Mayogel suspension, 100 ml",
    "Mebendazole, 100 mg Tabs",
    "Medigel Suspension, 100 ml",
    "Mefenamic Acid, 250 mg",
    "Mepyramine Cream, 025 g",
    "Mercurochrome Paint, 020 ml",
    "Metformin Tabs, 500 mg",
    "Metformin Tabs, 850 mg",
    "Methotrexate, 005 mg",
    "Methylprednisone Injection, 040 mg",
    "Methylsal Ointment, 500 mg",
    "Metoclopramide Injection, 010 mg",
    "Metoclopramide Tabs, 010 mg",
    "Metronidazole tabs, 400 mg",
    "Miconazol Oral Gel, 030 g",
    "Miconazole Cream, 002 %",
    "Midazolam, 010 mg",
    "Migril, 002 mg",
    "Mist Alba Susp, 100 ml",
    "Mmt, 250 mg",
    "Morphine Injection, 010 mg",
    "Multivitamin Tabs, 0.25 mg",
    "Mybulen, 200 mg",
    "Naloxone, 0.4 mg",
    "Nasal Drops- Oxymetazoline, 005 ml",
    "Neurobion Tabs, 200 mg",
    "Nifedipine, 005 mg",
    "Nifedipine, 010 mg",
    "Nitrofurantoin, 100 mg",
    "Nitrofurazone Ointment, 500 g",
    "Nitrolingual Spray, 020 ml",
    "Methylcellulose Eye Drops, 010 ml",
    "Norflex Co Tabs, 375 mg",
    "Nystatin Ointment, 020 g",
    "Nystatin Oral Susp, 1000 u",
    "Nystatin Vaginal Pess, 100 mg",
    "Omeprazole Tabs, 020 mg",
    "Oral Rehydration Salts, 002 g",
    "Osteoeze Gold, 200 mg",
    "Oxytocin Injection, 010 mg",
    "Pain Relief Gel, 020 g",
    "PanaCod Tab, 500 mg",
    "Paracetamol tabs, 500 mg",
    "Pen Vk Tab, 250 mg",
    "Pentaprazole Injection, 040 mg",
    "Perfulgan, 001 g",
    "Pethedine Injection, 050 mg",
    "Pethedine Injection, 100 mg",
    "Phenytoin Injection, 200 mg",
    "Phernobabitol tabs, 020 mg",
    "Podophylline Paint, 020 ml",
    "Potassium Chloride tabs, 600 mg",
    "Potassium Citrate, 100 ml",
    "Povidone Ointment, 500 mg",
    "Pravastatin tabs, 020 mg",
    "Prednisone Tab, 005 mg",
    "Probanthine Tabs, 015 mg",
    "Prochlorperazine Tabs, 005 mg",
    "Projchlorperazine Injection, 005 mg",
    "Promethazine Injection, 050 mg",
    "Promethazine Tabs, 025 mg",
    "Propranolol Tabs, 010 mg",
    "Propranolol Tabs, 040 mg",
    "Pyridoxine, 025 mg",
    "Ranitidine, 150 mg",
    "Rocuronium injection, 010 mg",
    "Salbutamol Inhaler, 200 MID",
    "Salbutamol, 004 mg Tablets",
    "Selenium Tab, 100 mg",
    "Sildenafil, 050 mg",
    "Simvastatin, 020 mg",
    "Sinucon Tab, 200 mg",
    "Sodium Bicarbonate, 050 ml",
    "Sodium Valproate, 200 mg",
    "Spersallerg Opd, 010 ml",
    "Spironolactone tabs, 025 mg",
    "Suppositories Indocid (Arthrexin), 100 mg",
    "Suxamethonium injection, 010 mg",
    "Tetanus Toxoid Vaccine, 010 mg",
    "Tetracycline Ointment, 003 % 25G",
    "Tetracycline Opthal Ointment, 020 g",
    "Throat Lozenges, 250 mg",
    "Thymol Glycerine, 100 ml",
    "Tranexamic Acid Injection, 500 mg",
    "Tramadol Injection, 100 mg",
    "Tramadol Tabs, 050 mg",
    "Tranexamic Acid tabs, 500 mg",
    "Trifen Adult, 100 ml",
    "Tumsulosin, 0.5 mg",
    "Urirex K, 050 mg",
    "Venteze Resp.Sol, 005 mg/20ml",
    "Vitamin B Co Tablets, 001 mg",
    "Vitamin B12, 002 mg",
    "Vitamin E Cream, 500 g",
    "Vitamin B Co Injection, 001 mg",
    "Vitamin K Injection (Konakion), 001 mg",
    "Warfarin Tabs, 005 mg",
    "Water For Injection, 010 ml",
    "Zinc Oxide Ointment, 030 mg",
    "Zinc Tablets, 020 mg",
    "Zuvamor, 040 mg",
    "Amoxyl, 500 mg",
    "Labetolol, 5mg",
    "Morpine tabs , 10mg"
];
function addInputListener(input, type) {
    input.addEventListener('input', function() {
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
                datalist = document.getElementById('med_suggestions');
                options = medicationOptions;
                break;
            case 'diagnosis':
                datalist = document.getElementById('diag_suggestions');
                fetch(`/api/diagnoses?query=${encodeURIComponent(query)}`)
                    .then(response => response.json())
                    .then(suggestions => {
                        if (suggestions.error) {
                            console.error(suggestions.error);
                            return;
                        }
                        datalist.innerHTML = '';
                        suggestions.forEach(sugg => {
                            const option = document.createElement('option');
                            option.value = sugg;
                            datalist.appendChild(option);
                        });
                    })
                    .catch(error => console.error('Error fetching diagnoses:', error));
                return;
            default:
                return;
        }
        datalist.innerHTML = '';
        if (query.length < 1) return;
        const filtered = options.filter(option => option.toLowerCase().includes(query));
        filtered.forEach(sugg => {
            const option = document.createElement('option');
            option.value = sugg;
            datalist.appendChild(option);
        });
    });
}
function addRow() {
    if (medRowCount >= 12) {
        alert('Maximum 12 medications allowed.');
        return;
    }
    medRowCount++;
    const container = document.getElementById('medications');
    const newRow = document.createElement('div');
    newRow.className = 'med-row';
    newRow.innerHTML = `
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
    `;
    container.appendChild(newRow);
    const newInput = newRow.querySelector('.med-input');
    addInputListener(newInput, 'medication');
}
function removeRow(btn) {
    btn.closest('.med-row').remove();
    medRowCount--;
}
function addDiagRow() {
    if (diagRowCount >= 3) {
        alert('Maximum 3 diagnoses allowed.');
        return;
    }
    diagRowCount++;
    const container = document.getElementById('diagnoses');
    const newRow = document.createElement('div');
    newRow.className = 'diag-row';
    newRow.innerHTML = `
        <div>
            <label>Diagnosis:</label>
            <input name="diagnoses" list="diag_suggestions" type="text" class="diag-input">
        </div>
        <div>
            <button type="button" onclick="removeDiagRow(this)">Remove</button>
        </div>
    `;
    container.appendChild(newRow);
    const newInput = newRow.querySelector('.diag-input');
    addInputListener(newInput, 'diagnosis');
}
function removeDiagRow(btn) {
    btn.closest('.diag-row').remove();
    diagRowCount--;
}
function clearForm() {
    document.querySelector('.common-section').querySelectorAll('input, select').forEach(el => el.value = '');
    const diagContainer = document.getElementById('diagnoses');
    while (diagContainer.children.length > 1) {
        diagContainer.removeChild(diagContainer.lastChild);
    }
    const firstDiagRow = diagContainer.firstChild;
    firstDiagRow.querySelectorAll('input').forEach(el => el.value = '');
    diagRowCount = 1;
    const medsContainer = document.getElementById('medications');
    while (medsContainer.children.length > 1) {
        medsContainer.removeChild(medsContainer.lastChild);
    }
    const firstMedRow = medsContainer.firstChild;
    firstMedRow.querySelectorAll('input').forEach(el => el.value = '');
    medRowCount = 1;
    document.getElementById('med_suggestions').innerHTML = '';
    document.getElementById('diag_suggestions').innerHTML = '';
    document.getElementById('company_suggestions').innerHTML = '';
    document.getElementById('position_suggestions').innerHTML = '';
}
// Initialize listeners for existing inputs
document.addEventListener('DOMContentLoaded', function() {
    const companyInput = document.getElementById('company');
    if (companyInput) addInputListener(companyInput, 'company');
    const positionInput = document.getElementById('position');
    if (positionInput) addInputListener(positionInput, 'position');
    const existingMedInputs = document.querySelectorAll('.med-input');
    existingMedInputs.forEach(input => addInputListener(input, 'medication'));
    const existingDiagInputs = document.querySelectorAll('.diag-input');
    existingDiagInputs.forEach(input => addInputListener(input, 'diagnosis'));
});
// Clear form after successful dispense
{% if message and ('successfully' in message|lower or 'updated' in message|lower) %}
    {% if not tx_data %}
        clearForm();
    {% endif %}
{% endif %}
</script>
"""
RECEIVE_TEMPLATE = CSS_STYLE + """
<h1>Receiving</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
{{ nav_links|safe }}
{% if message %}
<p class="message {% if 'successfully' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}
<h2>Receive Medication</h2>
<form method="POST" action="/receive" class="receive-form">
    <div class="common-section">
        <div>
            <label>Medication:</label>
            <input name="med_name" id="med_name" list="med_suggestions" required>
        </div>
        <div>
            <label>Quantity:</label>
            <input name="quantity" type="number" min="1" required>
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
        <input type="submit" value="Receive">
        <button type="button" onclick="document.querySelector('form').reset(); document.getElementById('med_suggestions').innerHTML = ''; ">Clear Form</button>
    </div>
    <input type="hidden" name="start_date" value="{{ start_date or '' }}">
    <input type="hidden" name="end_date" value="{{ end_date or '' }}">
    <input type="hidden" name="search" value="{{ search or '' }}">
</form>
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
            <input name="search" type="text" value="{{ search or '' }}" placeholder="Search medication, batch, supplier...">
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
            <th>Medication</th>
            <th>Quantity</th>
            <th>Batch</th>
            <th>Price</th>
            <th>Expiry Date</th>
            <th>Stock Receiver</th>
            <th>Order Number</th>
            <th>Supplier</th>
            <th>Invoice Number</th>
            <th>User</th>
            <th>Timestamp</th>
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
        </tr>
        {% else %}
        <tr><td colspan="11">No receive transactions.</td></tr>
        {% endfor %}
    </tbody>
</table>
<script>
// Medication options array for autocomplete
const medicationOptions = [
    "Acetylsalisylic Acid, 100 mg",
    "Acetylsalisylic Acid, 300 mg",
    "Activated Charcoal, 050 g",
    "Actrapid, 100 IU",
    "Acyclovir Cre 5 Perc, 010 mg",
    "Acyclovir Tab, 200 mg",
    "Acyclovir, 800 mg",
    "Adalat, 030 mg",
    "Adalat, 060 mg",
    "Adcodol, 500 mg",
    "Adcorectic, 050 mg",
    "Adenosine, 006 mg",
    "Adrenalin Hcl Inj, 001 mg",
    "Alcophyllin Syrup, 100 ml",
    "Alcophyllex Syrup, 100 ml",
    "Allopurinol, 100 mg",
    "Aminophyllin Injection, 250 mg",
    "Aminophyllin, 100 mg",
    "Amiodarone, 006 mg",
    "Amitryptyline, 025 mg",
    "Amlodipine, 010 mg",
    "Amoxycillin Cap, 250 mg",
    "Amoxyclav Injection, 1200 mg",
    "Amoxyclav, 625 mg",
    "Ampicillin Caps, 250 mg",
    "Ampiclox Caps, 500 mg",
    "Ampjicillin Injection, 500 mg",
    "Anti Haemorrhoidal Suppositories, 100 mg",
    "Anti Snake Bite Serum, 010 ml",
    "Antirubbies, 2.5 IU",
    "Anusol Ointment, 2500 mg",
    "Arachis Oil, 020 ml",
    "Atorvastatin, 010 mg",
    "Atorvastatin, 020 mg",
    "Asccorbic Acid Tab - Chewable, 250 mg",
    "Atenolol, 050 mg",
    "Atenolol, 100 mg",
    "Atropine Injection, 0.5 mg",
    "Azithromycin, 500 mg",
    "Baclofen, 010 mg",
    "Beclomethasone Inhaler, 200 MID",
    "Benzathine Pen, 2.4 MU",
    "Benzoic Salicylic Ointment (Whitfield), 500 g",
    "Benzyl Benzoate, 100 ml",
    "Benzyl Pen Injection, 005 MU",
    "Betamethasone Cream, 500 g",
    "Bisacodyl Tab, 005 mg",
    "Calamine Lotion, 100 ml",
    "Calcium Gluconate Tabs, 300 mg",
    "Captopril Tab, 050 mg",
    "Carbamazepine, 200 mg",
    "Carvedilol, 12.5mg",
    "Cefotaxime Injection, 001 g",
    "Ceftriaxone Injection, 1000 mg",
    "Ceftriaxone, 250 mg",
    "Celebrex, 200 mg",
    "Cetrizine, 010 mg",
    "Chlopromazine, 025 mg",
    "Chloramphenicol Caps, 250 mg",
    "Chloramphenicol Eye Drops, 010 ml",
    "Chloramphenicol Eye Oint, 005 g",
    "Chlorhexide Mouth Wash, 100 ml",
    "Chloro Ear Drops, 020 ml",
    "Chlorpheniramine Tabs, 004 mg",
    "Cimetidine Tabs, 200 mg",
    "Cimetidine tabs, 400 mg",
    "Cimetidine Injection, 200 mg",
    "Cipro Eye Drops, 010 ml",
    "Ciprofloxacin, 500 mg",
    "Clarythromycin, 500 mg",
    "Cloxacillin Injection, 250 mg",
    "Clopidogrel, 075 mg",
    "Clotrimazole Cre Vaginal, 010 mg",
    "Clotrimazole Pess, 100 mg",
    "Clotrimazole Topical Cre 1%, 020 g",
    "Cloxacillin Caps, 250 mg",
    "Colchicine Tabs, 0.5 mg",
    "Cotrimoxazole, 480 mg",
    "Cotrimoxazole, 960 mg",
    "Cyproheptadine, 004 mg",
    "Deep Freeze Spray, 050 ml",
    "Dexamethasone Eye Drops, 002 ml",
    "Dexamethasone Injection, 004 mg",
    "Dextrose Injection, 050 %",
    "Diazepam Injection, 010 mg",
    "Diazepam Tabs, 005 mg",
    "Diclofenac Injection, 075 mg",
    "Diclofenac Tab, 025 mg",
    "Diclofenac Tab, 050 mg",
    "Diclofenac Gel, 050 g",
    "Digoxin, 0.250 mg",
    "Diphenhydramine Syrup, 100 ml",
    "Dopamine Injection, 010 mg",
    "Doxycycline Tabs, 100 mg",
    "Dynexan Oral Gel, 010 mg",
    "Emergency Pill, 002 mg",
    "Enalapril tabs, 005 mg",
    "Enalapril tabs, 010 mg",
    "Ergometrine Injection, 0.5 mg /ml",
    "Erythromycin tabs, 250 mg",
    "Fentanyl, 100 mcg",
    "Ferrous Sulphate Tabs, 200 mg",
    "Fertomid, 050 mg",
    "Flagyl Injection, 400 mg",
    "Flu Stat, 200 mg",
    "Fluconazole, 200 mg",
    "FluoxetineCaps, 020 mg",
    "Fml Neo Opd, 005 ml",
    "Folic Acid Tabs, 005 mg",
    "Furosemide Injection, 020 mg",
    "Furosemide Tabs, 040 mg",
    "Gabapentine, 100 mg",
    "Gentamycin Injection, 040 mg",
    "Glibenclamide Tabs, 005 mg",
    "Gliclazide, 080 mg",
    "Glucose Powder, 500 g",
    "Glycerine Supp, 100 mg",
    "Griseofulvin Tabs, 500 mg",
    "Guafenesin Xl 60, 100 ml",
    "Gv Paint, 020 ml",
    "Haloperidol Injection, 002 mg",
    "Haloperidol Tabs, 1.5 mg",
    "Heparine, 1000 SIU",
    "Histacon Caps, 200 mg",
    "Hydalazine Injection, 020 mg",
    "Hydralazine Hcl Tabs , 010 mg",
    "Hydralazine Hcl Tabs, 050 mg",
    "Hydrochlorothiazide Tabs, 025 mg",
    "Hydrocortisone Cream, 500 g",
    "Hydrocortisone Injection, 100 mg",
    "Hyoscine Injection, 020 mg",
    "Hyoscine Tabs, 010 mg",
    "Ibuprofen Tab, 200 mg",
    "Ibuprofen Tab, 400 mg",
    "Ichthammol Ointment, 500 g",
    "Imipramine, 010 mg",
    "Indapamide Tabs, 0.5 mg",
    "Indomethacin Caps, 025 mg",
    "Insulin Hm Injection, 100 U 10 Ml",
    "Isosorbide Trinitrate, 005 mg",
    "Ketamine Injection, 050 mg (Ml)",
    "Keteconazole, 200 mg Tabs",
    "Lactulose, 150 ml",
    "Lignocaine Injection, 002 %",
    "Lignocaine Spray, 050 ml",
    "Liquid Paraffin , 100 ml",
    "Lisinopril, 020, mg",
    "Loperamide Tabs, 002 mg",
    "Loratadine, 010 mg",
    "Lorsatan, 050 mg",
    "Lorsatan, 100 mg",
    "Lubrucating Gel, 050 g",
    "Magasil Suspension, 100 ml",
    "Magnesium Suphate injection, 010 mg",
    "Mannitol, 020 %",
    "Mayogel suspension, 100 ml",
    "Mebendazole, 100 mg Tabs",
    "Medigel Suspension, 100 ml",
    "Mefenamic Acid, 250 mg",
    "Mepyramine Cream, 025 g",
    "Mercurochrome Paint, 020 ml",
    "Metformin Tabs, 500 mg",
    "Metformin Tabs, 850 mg",
    "Methotrexate, 005 mg",
    "Methylprednisone Injection, 040 mg",
    "Methylsal Ointment, 500 mg",
    "Metoclopramide Injection, 010 mg",
    "Metoclopramide Tabs, 010 mg",
    "Metronidazole tabs, 400 mg",
    "Miconazol Oral Gel, 030 g",
    "Miconazole Cream, 002 %",
    "Midazolam, 010 mg",
    "Migril, 002 mg",
    "Mist Alba Susp, 100 ml",
    "Mmt, 250 mg",
    "Morphine Injection, 010 mg",
    "Multivitamin Tabs, 0.25 mg",
    "Mybulen, 200 mg",
    "Naloxone, 0.4 mg",
    "Nasal Drops- Oxymetazoline, 005 ml",
    "Neurobion Tabs, 200 mg",
    "Nifedipine, 005 mg",
    "Nifedipine, 010 mg",
    "Nitrofurantoin, 100 mg",
    "Nitrofurazone Ointment, 500 g",
    "Nitrolingual Spray, 020 ml",
    "Methylcellulose Eye Drops, 010 ml",
    "Norflex Co Tabs, 375 mg",
    "Nystatin Ointment, 020 g",
    "Nystatin Oral Susp, 1000 u",
    "Nystatin Vaginal Pess, 100 mg",
    "Omeprazole Tabs, 020 mg",
    "Oral Rehydration Salts, 002 g",
    "Osteoeze Gold, 200 mg",
    "Oxytocin Injection, 010 mg",
    "Pain Relief Gel, 020 g",
    "PanaCod Tab, 500 mg",
    "Paracetamol tabs, 500 mg",
    "Pen Vk Tab, 250 mg",
    "Pentaprazole Injection, 040 mg",
    "Perfulgan, 001 g",
    "Pethedine Injection, 050 mg",
    "Pethedine Injection, 100 mg",
    "Phenytoin Injection, 200 mg",
    "Phernobabitol tabs, 020 mg",
    "Podophylline Paint, 020 ml",
    "Potassium Chloride tabs, 600 mg",
    "Potassium Citrate, 100 ml",
    "Povidone Ointment, 500 mg",
    "Pravastatin tabs, 020 mg",
    "Prednisone Tab, 005 mg",
    "Probanthine Tabs, 015 mg",
    "Prochlorperazine Tabs, 005 mg",
    "Projchlorperazine Injection, 005 mg",
    "Promethazine Injection, 050 mg",
    "Promethazine Tabs, 025 mg",
    "Propranolol Tabs, 010 mg",
    "Propranolol Tabs, 040 mg",
    "Pyridoxine, 025 mg",
    "Ranitidine, 150 mg",
    "Rocuronium injection, 010 mg",
    "Salbutamol Inhaler, 200 MID",
    "Salbutamol, 004 mg Tablets",
    "Selenium Tab, 100 mg",
    "Sildenafil, 050 mg",
    "Simvastatin, 020 mg",
    "Sinucon Tab, 200 mg",
    "Sodium Bicarbonate, 050 ml",
    "Sodium Valproate, 200 mg",
    "Spersallerg Opd, 010 ml",
    "Spironolactone tabs, 025 mg",
    "Suppositories Indocid (Arthrexin), 100 mg",
    "Suxamethonium injection, 010 mg",
    "Tetanus Toxoid Vaccine, 010 mg",
    "Tetracycline Ointment, 003 % 25G",
    "Tetracycline Opthal Ointment, 020 g",
    "Throat Lozenges, 250 mg",
    "Thymol Glycerine, 100 ml",
    "Tranexamic Acid Injection, 500 mg",
    "Tramadol Injection, 100 mg",
    "Tramadol Tabs, 050 mg",
    "Tranexamic Acid tabs, 500 mg",
    "Trifen Adult, 100 ml",
    "Tumsulosin, 0.5 mg",
    "Urirex K, 050 mg",
    "Venteze Resp.Sol, 005 mg/20ml",
    "Vitamin B Co Tablets, 001 mg",
    "Vitamin B12, 002 mg",
    "Vitamin E Cream, 500 g",
    "Vitamin B Co Injection, 001 mg",
    "Vitamin K Injection (Konakion), 001 mg",
    "Warfarin Tabs, 005 mg",
    "Water For Injection, 010 ml",
    "Zinc Oxide Ointment, 030 mg",
    "Zinc Tablets, 020 mg",
    "Zuvamor, 040 mg",
    "Amoxyl, 500 mg",
    "Labetolol, 5mg",
    "Morpine tabs , 10mg"
];
document.addEventListener('DOMContentLoaded', function() {
    const medInput = document.getElementById('med_name');
    medInput.addEventListener('input', function() {
        const query = this.value.toLowerCase();
        const datalist = document.getElementById('med_suggestions');
        datalist.innerHTML = '';
        if (query.length < 1) return;
        const filtered = medicationOptions.filter(option => option.toLowerCase().includes(query));
        filtered.forEach(med => {
            const option = document.createElement('option');
            option.value = med;
            datalist.appendChild(option);
        });
    });
});
</script>
"""
ADD_MED_TEMPLATE = CSS_STYLE + """
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
        <button type="button" onclick="document.querySelector('form').reset(); document.getElementById('med_suggestions').innerHTML = ''; ">Clear Form</button>
    </div>
</form>
<script>
// Medication options array for autocomplete
const medicationOptions = [
    "Acetylsalisylic Acid, 100 mg",
    "Acetylsalisylic Acid, 300 mg",
    "Activated Charcoal, 050 g",
    "Actrapid, 100 IU",
    "Acyclovir Cre 5 Perc, 010 mg",
    "Acyclovir Tab, 200 mg",
    "Acyclovir, 800 mg",
    "Adalat, 030 mg",
    "Adalat, 060 mg",
    "Adcodol, 500 mg",
    "Adcorectic, 050 mg",
    "Adenosine, 006 mg",
    "Adrenalin Hcl Inj, 001 mg",
    "Alcophyllin Syrup, 100 ml",
    "Alcophyllex Syrup, 100 ml",
    "Allopurinol, 100 mg",
    "Aminophyllin Injection, 250 mg",
    "Aminophyllin, 100 mg",
    "Amiodarone, 006 mg",
    "Amitryptyline, 025 mg",
    "Amlodipine, 010 mg",
    "Amoxycillin Cap, 250 mg",
    "Amoxyclav Injection, 1200 mg",
    "Amoxyclav, 625 mg",
    "Ampicillin Caps, 250 mg",
    "Ampiclox Caps, 500 mg",
    "Ampjicillin Injection, 500 mg",
    "Anti Haemorrhoidal Suppositories, 100 mg",
    "Anti Snake Bite Serum, 010 ml",
    "Antirubbies, 2.5 IU",
    "Anusol Ointment, 2500 mg",
    "Arachis Oil, 020 ml",
    "Atorvastatin, 010 mg",
    "Atorvastatin, 020 mg",
    "Asccorbic Acid Tab - Chewable, 250 mg",
    "Atenolol, 050 mg",
    "Atenolol, 100 mg",
    "Atropine Injection, 0.5 mg",
    "Azithromycin, 500 mg",
    "Baclofen, 010 mg",
    "Beclomethasone Inhaler, 200 MID",
    "Benzathine Pen, 2.4 MU",
    "Benzoic Salicylic Ointment (Whitfield), 500 g",
    "Benzyl Benzoate, 100 ml",
    "Benzyl Pen Injection, 005 MU",
    "Betamethasone Cream, 500 g",
    "Bisacodyl Tab, 005 mg",
    "Calamine Lotion, 100 ml",
    "Calcium Gluconate Tabs, 300 mg",
    "Captopril Tab, 050 mg",
    "Carbamazepine, 200 mg",
    "Carvedilol, 12.5mg",
    "Cefotaxime Injection, 001 g",
    "Ceftriaxone Injection, 1000 mg",
    "Ceftriaxone, 250 mg",
    "Celebrex, 200 mg",
    "Cetrizine, 010 mg",
    "Chlopromazine, 025 mg",
    "Chloramphenicol Caps, 250 mg",
    "Chloramphenicol Eye Drops, 010 ml",
    "Chloramphenicol Eye Oint, 005 g",
    "Chlorhexide Mouth Wash, 100 ml",
    "Chloro Ear Drops, 020 ml",
    "Chlorpheniramine Tabs, 004 mg",
    "Cimetidine Tabs, 200 mg",
    "Cimetidine tabs, 400 mg",
    "Cimetidine Injection, 200 mg",
    "Cipro Eye Drops, 010 ml",
    "Ciprofloxacin, 500 mg",
    "Clarythromycin, 500 mg",
    "Cloxacillin Injection, 250 mg",
    "Clopidogrel, 075 mg",
    "Clotrimazole Cre Vaginal, 010 mg",
    "Clotrimazole Pess, 100 mg",
    "Clotrimazole Topical Cre 1%, 020 g",
    "Cloxacillin Caps, 250 mg",
    "Colchicine Tabs, 0.5 mg",
    "Cotrimoxazole, 480 mg",
    "Cotrimoxazole, 960 mg",
    "Cyproheptadine, 004 mg",
    "Deep Freeze Spray, 050 ml",
    "Dexamethasone Eye Drops, 002 ml",
    "Dexamethasone Injection, 004 mg",
    "Dextrose Injection, 050 %",
    "Diazepam Injection, 010 mg",
    "Diazepam Tabs, 005 mg",
    "Diclofenac Injection, 075 mg",
    "Diclofenac Tab, 025 mg",
    "Diclofenac Tab, 050 mg",
    "Diclofenac Gel, 050 g",
    "Digoxin, 0.250 mg",
    "Diphenhydramine Syrup, 100 ml",
    "Dopamine Injection, 010 mg",
    "Doxycycline Tabs, 100 mg",
    "Dynexan Oral Gel, 010 mg",
    "Emergency Pill, 002 mg",
    "Enalapril tabs, 005 mg",
    "Enalapril tabs, 010 mg",
    "Ergometrine Injection, 0.5 mg /ml",
    "Erythromycin tabs, 250 mg",
    "Fentanyl, 100 mcg",
    "Ferrous Sulphate Tabs, 200 mg",
    "Fertomid, 050 mg",
    "Flagyl Injection, 400 mg",
    "Flu Stat, 200 mg",
    "Fluconazole, 200 mg",
    "FluoxetineCaps, 020 mg",
    "Fml Neo Opd, 005 ml",
    "Folic Acid Tabs, 005 mg",
    "Furosemide Injection, 020 mg",
    "Furosemide Tabs, 040 mg",
    "Gabapentine, 100 mg",
    "Gentamycin Injection, 040 mg",
    "Glibenclamide Tabs, 005 mg",
    "Gliclazide, 080 mg",
    "Glucose Powder, 500 g",
    "Glycerine Supp, 100 mg",
    "Griseofulvin Tabs, 500 mg",
    "Guafenesin Xl 60, 100 ml",
    "Gv Paint, 020 ml",
    "Haloperidol Injection, 002 mg",
    "Haloperidol Tabs, 1.5 mg",
    "Heparine, 1000 SIU",
    "Histacon Caps, 200 mg",
    "Hydalazine Injection, 020 mg",
    "Hydralazine Hcl Tabs , 010 mg",
    "Hydralazine Hcl Tabs, 050 mg",
    "Hydrochlorothiazide Tabs, 025 mg",
    "Hydrocortisone Cream, 500 g",
    "Hydrocortisone Injection, 100 mg",
    "Hyoscine Injection, 020 mg",
    "Hyoscine Tabs, 010 mg",
    "Ibuprofen Tab, 200 mg",
    "Ibuprofen Tab, 400 mg",
    "Ichthammol Ointment, 500 g",
    "Imipramine, 010 mg",
    "Indapamide Tabs, 0.5 mg",
    "Indomethacin Caps, 025 mg",
    "Insulin Hm Injection, 100 U 10 Ml",
    "Isosorbide Trinitrate, 005 mg",
    "Ketamine Injection, 050 mg (Ml)",
    "Keteconazole, 200 mg Tabs",
    "Lactulose, 150 ml",
    "Lignocaine Injection, 002 %",
    "Lignocaine Spray, 050 ml",
    "Liquid Paraffin , 100 ml",
    "Lisinopril, 020, mg",
    "Loperamide Tabs, 002 mg",
    "Loratadine, 010 mg",
    "Lorsatan, 050 mg",
    "Lorsatan, 100 mg",
    "Lubrucating Gel, 050 g",
    "Magasil Suspension, 100 ml",
    "Magnesium Suphate injection, 010 mg",
    "Mannitol, 020 %",
    "Mayogel suspension, 100 ml",
    "Mebendazole, 100 mg Tabs",
    "Medigel Suspension, 100 ml",
    "Mefenamic Acid, 250 mg",
    "Mepyramine Cream, 025 g",
    "Mercurochrome Paint, 020 ml",
    "Metformin Tabs, 500 mg",
    "Metformin Tabs, 850 mg",
    "Methotrexate, 005 mg",
    "Methylprednisone Injection, 040 mg",
    "Methylsal Ointment, 500 mg",
    "Metoclopramide Injection, 010 mg",
    "Metoclopramide Tabs, 010 mg",
    "Metronidazole tabs, 400 mg",
    "Miconazol Oral Gel, 030 g",
    "Miconazole Cream, 002 %",
    "Midazolam, 010 mg",
    "Migril, 002 mg",
    "Mist Alba Susp, 100 ml",
    "Mmt, 250 mg",
    "Morphine Injection, 010 mg",
    "Multivitamin Tabs, 0.25 mg",
    "Mybulen, 200 mg",
    "Naloxone, 0.4 mg",
    "Nasal Drops- Oxymetazoline, 005 ml",
    "Neurobion Tabs, 200 mg",
    "Nifedipine, 005 mg",
    "Nifedipine, 010 mg",
    "Nitrofurantoin, 100 mg",
    "Nitrofurazone Ointment, 500 g",
    "Nitrolingual Spray, 020 ml",
    "Methylcellulose Eye Drops, 010 ml",
    "Norflex Co Tabs, 375 mg",
    "Nystatin Ointment, 020 g",
    "Nystatin Oral Susp, 1000 u",
    "Nystatin Vaginal Pess, 100 mg",
    "Omeprazole Tabs, 020 mg",
    "Oral Rehydration Salts, 002 g",
    "Osteoeze Gold, 200 mg",
    "Oxytocin Injection, 010 mg",
    "Pain Relief Gel, 020 g",
    "PanaCod Tab, 500 mg",
    "Paracetamol tabs, 500 mg",
    "Pen Vk Tab, 250 mg",
    "Pentaprazole Injection, 040 mg",
    "Perfulgan, 001 g",
    "Pethedine Injection, 050 mg",
    "Pethedine Injection, 100 mg",
    "Phenytoin Injection, 200 mg",
    "Phernobabitol tabs, 020 mg",
    "Podophylline Paint, 020 ml",
    "Potassium Chloride tabs, 600 mg",
    "Potassium Citrate, 100 ml",
    "Povidone Ointment, 500 mg",
    "Pravastatin tabs, 020 mg",
    "Prednisone Tab, 005 mg",
    "Probanthine Tabs, 015 mg",
    "Prochlorperazine Tabs, 005 mg",
    "Projchlorperazine Injection, 005 mg",
    "Promethazine Injection, 050 mg",
    "Promethazine Tabs, 025 mg",
    "Propranolol Tabs, 010 mg",
    "Propranolol Tabs, 040 mg",
    "Pyridoxine, 025 mg",
    "Ranitidine, 150 mg",
    "Rocuronium injection, 010 mg",
    "Salbutamol Inhaler, 200 MID",
    "Salbutamol, 004 mg Tablets",
    "Selenium Tab, 100 mg",
    "Sildenafil, 050 mg",
    "Simvastatin, 020 mg",
    "Sinucon Tab, 200 mg",
    "Sodium Bicarbonate, 050 ml",
    "Sodium Valproate, 200 mg",
    "Spersallerg Opd, 010 ml",
    "Spironolactone tabs, 025 mg",
    "Suppositories Indocid (Arthrexin), 100 mg",
    "Suxamethonium injection, 010 mg",
    "Tetanus Toxoid Vaccine, 010 mg",
    "Tetracycline Ointment, 003 % 25G",
    "Tetracycline Opthal Ointment, 020 g",
    "Throat Lozenges, 250 mg",
    "Thymol Glycerine, 100 ml",
    "Tranexamic Acid Injection, 500 mg",
    "Tramadol Injection, 100 mg",
    "Tramadol Tabs, 050 mg",
    "Tranexamic Acid tabs, 500 mg",
    "Trifen Adult, 100 ml",
    "Tumsulosin, 0.5 mg",
    "Urirex K, 050 mg",
    "Venteze Resp.Sol, 005 mg/20ml",
    "Vitamin B Co Tablets, 001 mg",
    "Vitamin B12, 002 mg",
    "Vitamin E Cream, 500 g",
    "Vitamin B Co Injection, 001 mg",
    "Vitamin K Injection (Konakion), 001 mg",
    "Warfarin Tabs, 005 mg",
    "Water For Injection, 010 ml",
    "Zinc Oxide Ointment, 030 mg",
    "Zinc Tablets, 020 mg",
    "Zuvamor, 040 mg",
    "Amoxyl, 500 mg",
    "Labetolol, 5mg",
    "Morpine tabs , 10mg"
];
document.addEventListener('DOMContentLoaded', function() {
    const medInput = document.getElementById('med_name');
    medInput.addEventListener('input', function() {
        const query = this.value.toLowerCase();
        const datalist = document.getElementById('med_suggestions');
        datalist.innerHTML = '';
        if (query.length < 1) return;
        const filtered = medicationOptions.filter(option => option.toLowerCase().includes(query));
        filtered.forEach(med => {
            const option = document.createElement('option');
            option.value = med;
            datalist.appendChild(option);
        });
    });
});
</script>
"""
# Edit Medication Template
EDIT_MED_TEMPLATE = CSS_STYLE + """
<h1>Edit Medication</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
{{ nav_links|safe }}
{% if message %}
    <p class="message {% if 'successfully' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}
<h2>Edit {{ med_data.name if med_data else '' }}</h2>
<form method="POST" action="{{ url_for('edit_medication', med_name=med_name) }}" class="edit-medication-form">
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
# Reports Template (updated with nav and user column in tables, and edit button in stock table)
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
            <th>Medication</th>
            <th>Balance</th>
            <th>Expiry Date</th>
            <th>Batch</th>
            <th>Price</th>
            <th>Actions</th>
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
                <a href="{{ url_for('edit_medication', med_name=med.name|urlencode) }}"><button class="edit-btn">Edit</button></a>
                <form method="POST" action="{{ url_for('delete_medication') }}" style="display: inline;">
                    <input type="hidden" name="med_name" value="{{ med.name }}">
                    <button type="submit" class="delete-btn" onclick="return confirm('Are you sure you want to delete {{ med.name }}? This will remove it from the inventory.');">Delete</button>
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
        <div>
            <label>Start Date:</label>
            <input name="start_date" type="date" value="{{ start_date or '' }}">
        </div>
        <div>
            <label>End Date:</label>
            <input name="end_date" type="date" value="{{ end_date or '' }}">
        </div>
        <div>
            <label>Search Medication:</label>
            <input name="search" type="text" value="{{ search or '' }}" placeholder="Filter by medication name">
        </div>
        <div class="button-div">
            <input type="submit" value="Refine">
            <a href="{{ url_for('reports') }}">Back to Menu</a>
        </div>
    </div>
</form>
<h2>Inventory Report for {{ start_date }} to {{ end_date }}</h2>
<table>
    <thead>
        <tr>
            <th>Medication</th>
            <th>Beginning Balance</th>
            <th>Dispensed</th>
            <th>Received</th>
            <th>Current Balance</th>
            <th>Amount to Order</th>
        </tr>
    </thead>
    <tbody>
    {% for row in report_data %}
        <tr>
            <td>{{ row.med_name }}</td>
            <td>{{ row.beginning_balance }}</td>
            <td>{{ row.dispensed }}</td>
            <td>{{ row.received }}</td>
            <td>{{ row.current_balance }}</td>
            <td>{{ row.amount_to_order }}</td>
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
            <input name="search" type="text" value="{{ search or '' }}" placeholder="Search medication, batch, supplier...">
        </div>
        <div class="button-div">
            <input type="submit" value="Refine">
            <a href="{{ url_for('reports') }}">Back to Menu</a>
        </div>
    </div>
</form>
<h2>Receive List for {{ start_date }} to {{ end_date }}</h2>
<table>
    <thead>
        <tr>
            <th>Medication</th>
            <th>Quantity</th>
            <th>Batch</th>
            <th>Price</th>
            <th>Expiry Date</th>
            <th>Stock Receiver</th>
            <th>Order Number</th>
            <th>Supplier</th>
            <th>Invoice Number</th>
            <th>User</th>
            <th>Timestamp</th>
        </tr>
    </thead>
    <tbody>
    {% for t in receive_list %}
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
            <input name="search" type="text" value="{{ search or '' }}" placeholder="Search transactions by patient, medication, supplier...">
        </div>
        <div class="button-div">
            <input type="submit" value="Refine">
            <a href="{{ url_for('reports') }}">Back to Menu</a>
        </div>
    </div>
</form>
<h2>Controlled Drug Register for {{ start_date }} to {{ end_date }}</h2>
{% for reg in controlled_register %}
    <h3>{{ reg.med_name }} - Beginning Balance: {{ reg.beginning_balance }} | Ending Balance: {{ reg.ending_balance }} | Received: {{ reg.received }} | Dispensed: {{ reg.dispensed }}</h3>
    {% if reg.transactions %}
    <table>
        <thead>
            <tr>
                <th>Date</th>
                <th>Type</th>
                <th>Quantity</th>
                <th>Balance After</th>
                <th>Prescriber</th>
                <th>Issuer/Receiver</th>
                <th>User</th>
                <th>Reference/Patient</th>
            </tr>
        </thead>
        <tbody>
        {% for tx in reg.transactions %}
            <tr>
                <td>{{ tx.get('date', tx.timestamp.strftime('%Y-%m-%d')) }}</td>
                <td>{{ tx.type }}</td>
                <td>{{ tx.quantity }}</td>
                <td>{{ tx.balance_after }}</td>
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
# Login Template
LOGIN_TEMPLATE = CSS_STYLE + """
<h1>Pharmacy App Login</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
<div class="login-form">
    <h2>Login</h2>
    {% if error %}
        <p class="message error">{{ error }}</p>
    {% endif %}
    <form method="POST">
        <label>Username:</label>
        <input type="text" name="username" required><br>
        <label>Password:</label>
        <input type="password" name="password" required><br>
        <input type="submit" value="Login">
    </form>
    <p><a href="/register">Don't have an account? Register here.</a></p>
</div>
"""
# Register Password Template
REGISTER_PASSWORD_TEMPLATE = CSS_STYLE + """
<h1>Pharmacy App Registration</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
<div class="register-form">
    <h2>Admin Access Required</h2>
    {% if error %}
        <p class="message error">{{ error }}</p>
    {% endif %}
    <form method="POST">
        <label>Admin Password:</label>
        <input type="password" name="admin_pass" required><br>
        <input type="submit" value="Access Registration">
    </form>
    <p><a href="/login">Back to Login</a></p>
</div>
"""
# Register Template
REGISTER_TEMPLATE = CSS_STYLE + """
<h1>Pharmacy App Registration</h1>
<p>LD-HSE/NMC/HRD/6.1.3.3</p>
<div class="register-form">
    <h2>Register</h2>
    {% if error %}
        <p class="message error">{{ error }}</p>
    {% endif %}
    {% if message %}
        <p class="message success">{{ message }}</p>
    {% endif %}
    <form method="POST">
        <label>Username:</label>
        <input type="text" name="username" required><br>
        <label>Password:</label>
        <input type="password" name="password" required><br>
        <label>Full Name:</label>
        <input type="text" name="name" required><br>
        <label>Role:</label>
        <select name="role" required>
            <option value="employee">Employee</option>
            <option value="admin">Admin</option>
        </select><br>
        <input type="submit" value="Register">
    </form>
    <p><a href="/login">Already have an account? Login here.</a></p>
</div>
"""
# Routes
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
                session['user'] = {
                    'login': username,
                    'name': user_doc.get('name', username),
                    'role': user_doc.get('role', 'employee')
                }
                return redirect('/dispense')
            else:
                session['error'] = 'Invalid username or password.'
        except ServerSelectionTimeoutError:
            session['error'] = 'Database connection failed. Please try again later.'
        finally:
            client.close()
        return redirect('/login')
   
    error = session.pop('error', None)
    return render_template_string(LOGIN_TEMPLATE, error=error)
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
                if users.find_one({'username': username}):
                    session['error'] = 'Username already exists.'
                    return redirect('/register')
               
                password_hash = generate_password_hash(password)
                users.insert_one({
                    'username': username,
                    'password_hash': password_hash,
                    'name': name,
                    'role': role
                })
                session['message'] = 'Registration successful! Please login.'
                session.pop('admin_access', None)
                return redirect('/login')
            except ServerSelectionTimeoutError:
                session['error'] = 'Database connection failed. Please try again later.'
            finally:
                client.close()
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
        message = None
        start_date = request.values.get('start_date')
        end_date = request.values.get('end_date')
        search = request.values.get('search')
        current_user = session['user']['name']
        # Build query for tx_list
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
            or_query = [
                {'patient': {'$regex': search, '$options': 'i'}},
                {'med_name': {'$regex': search, '$options': 'i'}},
                {'company': {'$regex': search, '$options': 'i'}},
                {'position': {'$regex': search, '$options': 'i'}},
                {'age_group': {'$regex': search, '$options': 'i'}},
                {'gender': {'$regex': search, '$options': 'i'}},
                {'prescriber': {'$regex': search, '$options': 'i'}},
                {'dispenser': {'$regex': search, '$options': 'i'}},
                {'date': {'$regex': search, '$options': 'i'}},
                {'diagnoses.0': {'$regex': search, '$options': 'i'}},
            ]
            base_query['$or'] = or_query
        tx_list = list(transactions.find(base_query).sort('timestamp', -1))
        tx_data = None
        edit_id = request.args.get('edit')
        if edit_id:
            tx = transactions.find_one({'transaction_id': edit_id, 'type': 'dispense'})
            if tx:
                common = {k: v for k, v in tx.items() if k not in ['_id', 'med_name', 'quantity', 'type', 'timestamp', 'transaction_id', 'user']}
                meds_cursor = transactions.find({'transaction_id': edit_id, 'type': 'dispense'}, {'med_name': 1, 'quantity': 1})
                meds = [(m['med_name'], m['quantity']) for m in meds_cursor]
                common['meds'] = meds
                common['diags'] = common.get('diagnoses', [])
                common['transaction_id'] = edit_id
                tx_data = common
        if request.method == 'POST':
            transaction_id = request.form.get('transaction_id')
            if transaction_id:
                # Edit mode
                old_meds = list(transactions.find({'transaction_id': transaction_id}))
                for old_tx in old_meds:
                    med_name = old_tx['med_name']
                    old_qty = old_tx['quantity']
                    medications.update_one({'name': med_name}, {'$inc': {'balance': old_qty}})
                transactions.delete_many({'transaction_id': transaction_id})
                tx_id = transaction_id
                message_prefix = 'Updated'
            else:
                # New
                tx_id = str(uuid4())
                message_prefix = 'Dispensed'
            try:
                patient = request.form['patient']
                company = request.form['company']
                position = request.form['position']
                age_group = request.form['age_group']
                prescriber = request.form['prescriber']
                dispenser = request.form['dispenser']
                date_str = request.form['date']
                gender = request.form['gender']
                sick_leave_days = int(request.form['sick_leave_days'])
                diagnoses = [d.strip() for d in request.form.getlist('diagnoses') if d.strip()]
                if not diagnoses:
                    message = 'Please provide at least one diagnosis.'
                else:
                    med_names = [name.strip() for name in request.form.getlist('med_names') if name.strip()]
                    quantities_str = request.form.getlist('quantities')
                    quantities = []
                    for q_str in quantities_str:
                        try:
                            qty = int(q_str)
                            if qty > 0:
                                quantities.append(qty)
                        except ValueError:
                            pass
                    if len(med_names) != len(quantities) or not med_names:
                        message = 'Please provide at least one valid medication and quantity.'
                    else:
                        success = True
                        error_msgs = []
                        dispensed_meds = []
                        for med_name, quantity in zip(med_names, quantities):
                            med = medications.find_one({'name': med_name})
                            if not med:
                                error_msgs.append(f'Medication "{med_name}" not found.')
                                success = False
                                continue
                            elif med.get('balance', 0) < quantity:
                                error_msgs.append(f'Insufficient stock for "{med_name}".')
                                success = False
                                continue
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
                        if success and dispensed_meds:
                            message = f'{message_prefix} successfully: {", ".join(dispensed_meds)}'
                        else:
                            message = '; '.join(error_msgs) if error_msgs else f'No medications {message_prefix.lower()}.'
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
        return render_template_string(DISPENSE_TEMPLATE, tx_list=tx_list, nav_links=get_nav_links(), message=message, start_date=start_date, end_date=end_date, search=search, tx_data=tx_data)
    except ServerSelectionTimeoutError:
        return render_template_string(DISPENSE_TEMPLATE, tx_list=[], nav_links=get_nav_links(), message="Database connection failed. Please try again later.", start_date='', end_date='', search='', tx_data=None), 500
    finally:
        client.close()
@app.route('/receive', methods=['GET', 'POST'])
@login_required
def receive():
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        message = None
        start_date = request.values.get('start_date')
        end_date = request.values.get('end_date')
        search = request.values.get('search')
        current_user = session['user']['name']
        # Build query for tx_list
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
            or_query = [
                {'med_name': {'$regex': search, '$options': 'i'}},
                {'batch': {'$regex': search, '$options': 'i'}},
                {'supplier': {'$regex': search, '$options': 'i'}},
                {'stock_receiver': {'$regex': search, '$options': 'i'}},
                {'order_number': {'$regex': search, '$options': 'i'}},
                {'invoice_number': {'$regex': search, '$options': 'i'}},
                {'expiry_date': {'$regex': search, '$options': 'i'}},
            ]
            base_query['$or'] = or_query
        tx_list = list(transactions.find(base_query).sort('timestamp', -1))
        if request.method == 'POST':
            try:
                med_name = request.form['med_name']
                quantity = int(request.form['quantity'])
                batch = request.form['batch']
                price = float(request.form['price'])
                expiry_date = request.form['expiry_date']
                schedule = request.form['schedule']
                stock_receiver = request.form['stock_receiver']
                order_number = request.form['order_number']
                supplier = request.form['supplier']
                invoice_number = request.form['invoice_number']
                medications.update_one(
                    {'name': med_name},
                    {'$inc': {'balance': quantity},
                     '$set': {
                         'batch': batch,
                         'price': price,
                         'expiry_date': expiry_date,
                         'schedule': schedule,
                         'stock_receiver': stock_receiver,
                         'order_number': order_number,
                         'supplier': supplier,
                         'invoice_number': invoice_number
                     }},
                    upsert=True
                )
                transactions.insert_one({
                    'type': 'receive',
                    'med_name': med_name,
                    'quantity': quantity,
                    'batch': batch,
                    'price': price,
                    'expiry_date': expiry_date,
                    'schedule': schedule,
                    'stock_receiver': stock_receiver,
                    'order_number': order_number,
                    'supplier': supplier,
                    'invoice_number': invoice_number,
                    'user': current_user,
                    'timestamp': datetime.utcnow()
                })
                message = 'Received successfully!'
                return render_template_string(RECEIVE_TEMPLATE, tx_list=tx_list, nav_links=get_nav_links(), message=message, start_date=start_date, end_date=end_date, search=search)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template_string(RECEIVE_TEMPLATE, tx_list=tx_list, nav_links=get_nav_links(), message=message, start_date=start_date, end_date=end_date, search=search)
        return render_template_string(RECEIVE_TEMPLATE, tx_list=tx_list, nav_links=get_nav_links(), message=message, start_date=start_date, end_date=end_date, search=search)
    except ServerSelectionTimeoutError:
        return render_template_string(RECEIVE_TEMPLATE, tx_list=[], nav_links=get_nav_links(), message="Database connection failed. Please try again later.", start_date='', end_date='', search=''), 500
    finally:
        client.close()
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
                med_name = request.form['med_name']
                initial_balance = int(request.form['initial_balance'])
                batch = request.form['batch']
                price = float(request.form['price'])
                expiry_date = request.form['expiry_date']
                schedule = request.form['schedule']
                stock_receiver = request.form['stock_receiver']
                order_number = request.form['order_number']
                supplier = request.form['supplier']
                invoice_number = request.form['invoice_number']
                if medications.find_one({'name': med_name}):
                    message = f'Medication "{med_name}" already exists. Use Receiving to add stock.'
                    return render_template_string(ADD_MED_TEMPLATE, nav_links=get_nav_links(), message=message)
                medications.insert_one({
                    'name': med_name,
                    'balance': initial_balance,
                    'batch': batch,
                    'price': price,
                    'expiry_date': expiry_date,
                    'schedule': schedule,
                    'stock_receiver': stock_receiver,
                    'order_number': order_number,
                    'supplier': supplier,
                    'invoice_number': invoice_number
                })
                transactions.insert_one({
                    'type': 'receive',
                    'med_name': med_name,
                    'quantity': initial_balance,
                    'batch': batch,
                    'price': price,
                    'expiry_date': expiry_date,
                    'schedule': schedule,
                    'stock_receiver': stock_receiver,
                    'order_number': order_number,
                    'supplier': supplier,
                    'invoice_number': invoice_number,
                    'user': current_user,
                    'timestamp': datetime.utcnow()
                })
                message = 'Medication added successfully!'
                return render_template_string(ADD_MED_TEMPLATE, nav_links=get_nav_links(), message=message)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template_string(ADD_MED_TEMPLATE, nav_links=get_nav_links(), message=message)
        return render_template_string(ADD_MED_TEMPLATE, nav_links=get_nav_links(), message=message)
    except ServerSelectionTimeoutError:
        return render_template_string(ADD_MED_TEMPLATE, nav_links=get_nav_links(), message="Database connection failed. Please try again later."), 500
    finally:
        client.close()
@app.route('/edit-medication/<med_name>', methods=['GET', 'POST'])
@login_required
def edit_medication(med_name):
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can edit medications.')
        return redirect('/reports')
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        message = None
        med_data = None
        # URL decode med_name if necessary
        med_name = requests.utils.unquote(med_name)
        med = medications.find_one({'name': med_name})
        if not med:
            message = f'Medication "{med_name}" not found.'
            return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(), message=message, med_data=None, med_name=med_name)
        med_data = med
        if request.method == 'POST':
            try:
                balance = int(request.form['balance'])
                batch = request.form['batch']
                price = float(request.form['price'])
                expiry_date = request.form['expiry_date']
                schedule = request.form['schedule']
                # Update the medication
                medications.update_one(
                    {'name': med_name},
                    {'$set': {
                        'balance': balance,
                        'batch': batch,
                        'price': price,
                        'expiry_date': expiry_date,
                        'schedule': schedule
                    }}
                )
                message = 'Medication updated successfully!'
                # Refresh med_data after update
                med_data = medications.find_one({'name': med_name})
                return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(), message=message, med_data=med_data, med_name=med_name)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(), message=message, med_data=med_data, med_name=med_name)
        return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(), message=message, med_data=med_data, med_name=med_name)
    except ServerSelectionTimeoutError:
        message = "Database connection failed. Please try again later."
        return render_template_string(EDIT_MED_TEMPLATE, nav_links=get_nav_links(), message=message, med_data=None, med_name=med_name), 500
    finally:
        client.close()
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
            return redirect('/reports')
        result = medications.delete_one({'name': med_name})
        if result.deleted_count > 0:
            session['message'] = f'Medication "{med_name}" deleted successfully.'
        else:
            session['message'] = f'Failed to delete "{med_name}".'
    except Exception as e:
        session['message'] = f'Error deleting medication: {str(e)}'
    finally:
        client.close()
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
        search = None
        report_title = None
        start_dt = None
        end_dt = None
        def matches_search(tx, search_str):
            if not search_str:
                return True
            search_lower = search_str.lower()
            check_fields = ['patient', 'med_name', 'company', 'position', 'prescriber', 'dispenser', 'stock_receiver', 'order_number', 'supplier', 'invoice_number', 'batch', 'user']
            for field in check_fields:
                val = tx.get(field, '')
                val_str = str(val).lower()
                if search_lower in val_str:
                    return True
            # Handle diagnoses
            diagnoses = tx.get('diagnoses', [])
            if isinstance(diagnoses, list):
                diag_str = ' '.join(str(d).lower() for d in diagnoses)
                if search_lower in diag_str:
                    return True
            return False
        stock_report_types = ['stock_on_hand', 'expired_list', 'near_expired_list', 'out_of_stock_list']
        if request.method == 'POST':
            report_type = request.form.get('report_type')
            start_date = request.form.get('start_date')
            end_date = request.form.get('end_date')
            search = request.form.get('search')
            if report_type:
                try:
                    # Parse dates if provided
                    if start_date:
                        start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    if end_date:
                        end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc) + timedelta(days=1) - timedelta(seconds=1)
                    # now process the report
                    if report_type in stock_report_types:
                        if not end_date:
                            raise ValueError('End date is required for this report type.')
                        report_date = datetime.strptime(end_date, '%Y-%m-%d').date()
                        threshold_date = report_date + timedelta(days=30)
                        now_dt = datetime.now(timezone.utc)
                        med_filter = {'name': {'$regex': search or '', '$options': 'i'}} if search else {}
                        all_meds = list(medications.find(med_filter, {'_id': 0}).sort('name', 1))
                        stock_data = []
                        for med in all_meds:
                            med_name = med['name']
                            current_balance = med.get('balance', 0)
                            try:
                                # Period transactions after the report date (from end_dt+ to now)
                                period_pipeline = [
                                    {'$match': {
                                        'med_name': med_name,
                                        'timestamp': {'$gt': end_dt, '$lte': now_dt}
                                    }},
                                    {'$group': {
                                        '_id': None,
                                        'dispensed': {
                                            '$sum': {
                                                '$cond': [
                                                    {'$eq': ['$type', 'dispense']},
                                                    '$quantity',
                                                    0
                                                ]
                                            }
                                        },
                                        'received': {
                                            '$sum': {
                                                '$cond': [
                                                    {'$eq': ['$type', 'receive']},
                                                    '$quantity',
                                                    0
                                                ]
                                            }
                                        }
                                    }}
                                ]
                                period_result = list(transactions.aggregate(period_pipeline))
                                dispensed_after = period_result[0].get('dispensed', 0) if period_result else 0
                                received_after = period_result[0].get('received', 0) if period_result else 0
                                balance_at_date = current_balance - received_after + dispensed_after
                                balance_at_date = max(0, balance_at_date)
                            except Exception as query_err:
                                app.logger.error(f"Query failed for med {med_name}: {query_err}")
                                # Fallback to current balance
                                balance_at_date = current_balance
                            expiry_str = med.get('expiry_date')
                            expiry_dt = None
                            if expiry_str:
                                try:
                                    if 'T' in expiry_str:
                                        # Full ISO datetime: Extract date part only
                                        date_part = expiry_str.split('T')[0]
                                        expiry_dt = datetime.strptime(date_part, '%Y-%m-%d').date()
                                    else:
                                        # Date-only string
                                        expiry_dt = datetime.strptime(expiry_str, '%Y-%m-%d').date()
                                except ValueError as e:
                                    app.logger.warning(f"Invalid expiry_date '{expiry_str}' for med '{med.get('name', 'unknown')}': {e} - Treating as no expiry.")
                                    expiry_dt = None
                            # Handle missing or empty batch: set to 'N/A'
                            batch_val = med.get('batch')
                            if not batch_val: # Covers None, empty string, or falsy
                                med['batch'] = 'N/A'
                            if balance_at_date == 0:
                                status = 'out-of-stock'
                            else:
                                if expiry_dt is None:
                                    status = 'normal' # Include even without expiry
                                elif expiry_dt < report_date:
                                    status = 'expired'
                                elif expiry_dt <= threshold_date:
                                    status = 'close-to-expire'
                                else:
                                    status = 'normal'
                            med_copy = med.copy()
                            med_copy['balance'] = balance_at_date
                            med_copy['status'] = status
                            stock_data.append(med_copy)
                        date_str = end_date # Use the input string for title
                        if report_type == 'stock_on_hand':
                            report_title = f'Stock on Hand as of {date_str}'
                        elif report_type == 'expired_list':
                            stock_data = [m for m in stock_data if m['status'] == 'expired']
                            report_title = f'Expired Drugs List as of {date_str}'
                        elif report_type == 'near_expired_list':
                            stock_data = [m for m in stock_data if m['status'] == 'close-to-expire']
                            report_title = f'Near Expired Drug List as of {date_str}'
                        elif report_type == 'out_of_stock_list':
                            stock_data = [m for m in stock_data if m['status'] == 'out-of-stock']
                            report_title = f'Out of Stock List as of {date_str}'
                    elif report_type == 'inventory':
                        if not start_date or not end_date:
                            raise ValueError('Start and end dates are required for this report type.')
                        med_filter = {'name': {'$regex': search or '', '$options': 'i'}} if search else {}
                        meds = list(medications.find(med_filter, {'_id': 0, 'name': 1, 'balance': 1}).sort('name', 1).limit(300)) # Temp limit for testing
                        start_date_obj = start_dt.date()
                        end_date_obj = end_dt.date()
                        days_in_period = max(1, (end_date_obj - start_date_obj).days + 1)
                        for med in meds:
                            med_name = med['name']
                            try:
                                # Period transactions (simple $cond per type)
                                period_pipeline = [
                                    {'$match': {
                                        'med_name': med_name,
                                        'timestamp': {'$gte': start_dt, '$lte': end_dt}
                                    }},
                                    {'$group': {
                                        '_id': None,
                                        'dispensed': {
                                            '$sum': {
                                                '$cond': [
                                                    {'$eq': ['$type', 'dispense']},
                                                    '$quantity',
                                                    0
                                                ]
                                            }
                                        },
                                        'received': {
                                            '$sum': {
                                                '$cond': [
                                                    {'$eq': ['$type', 'receive']},
                                                    '$quantity',
                                                    0
                                                ]
                                            }
                                        }
                                    }}
                                ]
                                period_result = list(transactions.aggregate(period_pipeline))
                                dispensed = period_result[0].get('dispensed', 0) if period_result else 0
                                received = period_result[0].get('received', 0) if period_result else 0
                                current_balance = med.get('balance', 0)
                                beginning_balance = current_balance - received + dispensed
                                beginning_balance = max(0, beginning_balance)
                                average_daily = dispensed / days_in_period
                                average_monthly = average_daily * 30
                                lead_time_stock = average_daily * 14
                                amount_to_order = max(0, average_monthly - current_balance + lead_time_stock)
                                report_data.append({
                                    'med_name': med_name,
                                    'beginning_balance': beginning_balance,
                                    'dispensed': dispensed,
                                    'received': received,
                                    'current_balance': current_balance,
                                    'amount_to_order': int(amount_to_order) if amount_to_order.is_integer() else round(amount_to_order, 2)
                                })
                            except Exception as query_err:
                                app.logger.error(f"Query failed for med {med_name}: {query_err}")
                                # Fallback to 0s to avoid crashing the whole report
                                current_balance = med.get('balance', 0)
                                report_data.append({
                                    'med_name': med_name,
                                    'beginning_balance': current_balance,
                                    'dispensed': 0,
                                    'received': 0,
                                    'current_balance': current_balance,
                                    'amount_to_order': 0
                                })
                    elif report_type == 'receive_list':
                        base_query = {'type': 'receive'}
                        if start_date and end_date:
                            base_query['timestamp'] = {'$gte': start_dt, '$lte': end_dt}
                        if search:
                            or_query = [
                                {'med_name': {'$regex': search, '$options': 'i'}},
                                {'batch': {'$regex': search, '$options': 'i'}},
                                {'supplier': {'$regex': search, '$options': 'i'}},
                                {'stock_receiver': {'$regex': search, '$options': 'i'}},
                                {'order_number': {'$regex': search, '$options': 'i'}},
                                {'invoice_number': {'$regex': search, '$options': 'i'}},
                                {'expiry_date': {'$regex': search, '$options': 'i'}},
                            ]
                            base_query['$or'] = or_query
                        # Add limit
                        receive_list = list(transactions.find(base_query).sort('timestamp', 1).limit(10000))
                    elif report_type == 'controlled_drug_register':
                        if not start_date or not end_date:
                            raise ValueError('Start and end dates are required for this report type.')
                        controlled_meds_cursor = medications.find({'schedule': 'controlled'}, {'_id': 0, 'name': 1})
                        controlled_meds = [m['name'] for m in controlled_meds_cursor]
                        if controlled_meds:
                            # Fetch all relevant transactions in period with limit
                            period_query = {
                                'med_name': {'$in': controlled_meds},
                                'type': {'$in': ['receive', 'dispense']},
                                'timestamp': {'$gte': start_dt, '$lte': end_dt}
                            }
                            all_tx = list(transactions.find(period_query).sort('timestamp', 1).limit(10000))
                            tx_by_med = defaultdict(list)
                            for tx in all_tx:
                                tx_by_med[tx['med_name']].append(tx)
                            for med_name in sorted(controlled_meds):
                                med = medications.find_one({'name': med_name})
                                if not med:
                                    continue
                                current_balance = med.get('balance', 0)
                                try:
                                    med_txs = tx_by_med[med_name]
                                    received_in_period = sum(tx['quantity'] for tx in med_txs if tx['type'] == 'receive')
                                    dispensed_in_period = sum(tx['quantity'] for tx in med_txs if tx['type'] == 'dispense')
                                    beginning_balance = current_balance - received_in_period + dispensed_in_period
                                    beginning_balance = max(0, beginning_balance)
                                    ending_balance = current_balance
                                    # Compute running balances
                                    running_current_balance = beginning_balance
                                    running_entries = []
                                    for tx in med_txs:
                                        qty = tx['quantity']
                                        if tx['type'] == 'receive':
                                            running_current_balance += qty
                                        else:
                                            running_current_balance -= qty
                                        tx_copy = tx.copy()
                                        tx_copy['balance_after'] = running_current_balance
                                        running_entries.append(tx_copy)
                                    # Filter transactions
                                    filtered_entries = [e for e in running_entries if matches_search(e, search)]
                                    controlled_register.append({
                                        'med_name': med_name,
                                        'beginning_balance': beginning_balance,
                                        'ending_balance': ending_balance,
                                        'received': received_in_period,
                                        'dispensed': dispensed_in_period,
                                        'transactions': filtered_entries
                                    })
                                except Exception as query_err:
                                    app.logger.error(f"Query failed for controlled med {med_name}: {query_err}")
                                    # Skip this med to avoid crashing
                                    continue
                except ValueError as e:
                    message = f'Invalid input: {str(e)}'
                    report_type = None
                    start_date = None
                    end_date = None
                    search = None
                    report_data = []
                    receive_list = []
                    stock_data = []
                    controlled_register = []
                    total_transactions = 0
                    report_title = None
            else:
                message = 'Please select a report type.'
                report_type = None
                start_date = None
                end_date = None
                search = None
                report_data = []
                receive_list = []
                stock_data = []
                controlled_register = []
                total_transactions = 0
                report_title = None
        return render_template_string(
            REPORTS_TEMPLATE,
            report_type=report_type,
            report_data=report_data,
            receive_list=receive_list,
            stock_data=stock_data,
            controlled_register=controlled_register,
            start_date=start_date,
            end_date=end_date,
            total_transactions=total_transactions,
            nav_links=get_nav_links(),
            message=message,
            search=search,
            report_title=report_title,
            is_admin=is_admin
        )
    except ServerSelectionTimeoutError:
        return render_template_string(
            REPORTS_TEMPLATE,
            nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            report_type=None,
            report_data=[],
            receive_list=[],
            stock_data=[],
            controlled_register=[],
            start_date=None,
            end_date=None,
            total_transactions=0,
            search=None,
            report_title=None,
            is_admin=is_admin
        ), 500
    finally:
        client.close()
# 2. NEW ROUTE – delete a dispense transaction
# -------------------------------------------------
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
        medications = db['medications']

        # 1. Get every medication line for this transaction
        tx_rows = list(transactions.find({'transaction_id': tx_id, 'type': 'dispense'}))
        if not tx_rows:
            flash('Transaction not found.', 'error')
            return redirect(url_for('dispense'))

        # 2. Return stock
        for row in tx_rows:
            med_name = row['med_name']
            qty = row['quantity']
            medications.update_one(
                {'name': med_name},
                {'$inc': {'balance': qty}}
            )

        # 3. Delete all rows belonging to the transaction
        transactions.delete_many({'transaction_id': tx_id})

        flash('Dispense transaction deleted – stock restored.', 'success')
    except Exception as e:
        flash(f'Delete failed: {str(e)}', 'error')
    finally:
        client.close()

    # Preserve any filters the user had
    return redirect(url_for('dispense',
                            start_date=request.form.get('start_date'),
                            end_date=request.form.get('end_date'),
                            search=request.form.get('search')))
@app.route('/api/diagnoses', methods=['GET'])
@login_required
def get_diagnosis_suggestions():
    query = request.args.get('query', '').lower()
    matching = [d for d in DIAGNOSES_OPTIONS if query in d.lower()][:10]
    return jsonify(matching)
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
