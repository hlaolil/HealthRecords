import os
from flask import Flask, request, render_template_string, jsonify, redirect
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from datetime import datetime, timedelta

app = Flask(__name__)

# MongoDB connection function (lazy initialization for fork-safety)
def get_mongo_client():
    monguri = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
    return MongoClient(monguri, serverSelectionTimeoutMS=5000)

# Navigation links
NAV_LINKS = """
<p class="nav-links"><strong>Navigate:</strong>
    <a href="/dispense">Dispensing</a> |
    <a href="/receive">Receiving</a> |
    <a href="/add-medication">Add Medication</a> |
    <a href="/reports">Reports</a>
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
        color: #0056b3; /* darker blue */
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
        color: #0056b3; /* darker blue links */
        text-decoration: none;
        margin: 0 10px;
        font-weight: bold;
    }
    .nav-links a:hover {
        text-decoration: underline;
        color: #003d80; /* darker on hover */
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
    .add-medication-form {
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
    form input[type="submit"], form button {
        background-color: #0056b3; /* darker blue button */
        color: #fff;
        border: none;
        padding: 10px 20px;
        border-radius: 4px;
        cursor: pointer;
        margin: 10px 5px;
        display: inline-block;
        font-weight: bold;
    }
    form input[type="submit"]:hover, form button:hover {
        background-color: #003d80; /* even darker on hover */
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
        background-color: #0056b3; /* darker table header */
        color: #fff;
        font-weight: bold;
    }
    table tr:nth-child(even) {
        background-color: #f8f9fa;
    }
    table tr:hover {
        background-color: #e0e7f5; /* subtle blue hover */
    }
    .out-of-stock, .expired {
        background-color: #f8d7da !important;
        color: #721c24 !important;
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
    @media (max-width: 600px) {
        body {
            padding: 10px;
        }
        form, table {
            max-width: 100%;
        }
        .common-section {
            grid-template-columns: 1fr; /* Single column on mobile */
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
    }
</style>
"""


# ✅ Updated Dispense Template with multiple medications and diagnoses support
DISPENSE_TEMPLATE = CSS_STYLE + """
<h1>Dispensing</h1>
{{ nav_links|safe }}

{% if message %}
    <p class="message {% if 'successfully' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}

<h2>Dispense Medication</h2>

<form method="POST" action="{{ url_for('dispense') }}" class="dispense-form">
    <div class="common-section">
        <div>
            <label>Patient:</label>
            <input name="patient" type="text" required>
        </div>

        <div>
            <label for="company">Company:</label>
            <select id="company" name="company" required>
              <option value="">-- Select Company --</option>
              <option>BLW</option>
              <option>BUSY BEE</option>
              <option>CMS</option>
              <option>Consulmet</option>
              <option>Enaex</option>
              <option>Eminence</option>
              <option>Government</option>
              <option>IFS</option>
              <option>LD</option>
              <option>LISELO</option>
              <option>LMPS</option>
              <option>Mendi</option>
              <option>MGC</option>
              <option>MINOPEX</option>
              <option>NMC</option>
              <option>Other</option>
              <option>PLATO</option>
              <option>Public</option>
              <option>THOLO</option>
              <option>TOMRA</option>
              <option>UL4</option>
              <option>UNITRANS</option>
            </select>
        </div>

        <div>
            <label for="position">Position:</label>
            <select id="position" name="position" required>
              <option value="">-- Select Position --</option>
              <option>Administration</option>
              <option>Artisan</option>
              <option>Blasting</option>
              <option>Boiler Maker</option>
              <option>Chef</option>
              <option>CI</option>
              <option>Cleaner</option>
              <option>Controller</option>
              <option>Director</option>
              <option>Drilling</option>
              <option>Drivers</option>
              <option>Electricians</option>
              <option>Emergency Coordinator</option>
              <option>Environmnet</option>
              <option>Finance</option>
              <option>Fitters</option>
              <option>Food Service Attendant</option>
              <option>General Worker</option>
              <option>Geologist</option>
              <option>Hse</option>
              <option>Housekeeping</option>
              <option>IT</option>
              <option>Intern</option>
              <option>Kitchen</option>
              <option>Lab Technologist</option>
              <option>Maintenance</option>
              <option>Management</option>
              <option>Manager</option>
              <option>Mechanics</option>
              <option>Medical Doctor</option>
              <option>Metallurgy</option>
              <option>Mining</option>
              <option>Nurse</option>
              <option>Operator</option>
              <option>Other</option>
              <option>PHC</option>
              <option>Pharmacist</option>
              <option>Plant Operator</option>
              <option>Police</option>
              <option>Procurement</option>
              <option>Process</option>
              <option>Production</option>
              <option>Public</option>
              <option>Recovery</option>
              <option>Security</option>
              <option>Sorting</option>
              <option>Storekeeper</option>
              <option>Survey</option>
              <option>Technician</option>
              <option>Training</option>
              <option>Tourist</option>
              <option>Treatment</option>
              <option>Tyreman</option>
              <option>UNITRANS</option>
              <option>Visitor</option>
              <option>Water Works</option>
              <option>Welder</option>
              <option>Workshop Cleaners</option>
              <option>X-Ray Technologist</option>
            </select>
        </div>

        <div>
            <label>Age Group:</label>
            <select name="age_group" required>
                <option value="">-- Select Age Group --</option>
                <option value="18-24">18-24</option>
                <option value="25-34">25-34</option>
                <option value="35-45">35-45</option>
                <option value="45-54">45-54</option>
                <option value="54-65">54-65</option>
            </select>
        </div>

        <div>
            <label>Gender:</label>
            <select name="gender" required>
                <option value="">-- Select --</option>
                <option value="Male">Male</option>
                <option value="Female">Female</option>
            </select>
        </div>

        <div>
            <label>Number of Sick Leave Days:</label>
            <input name="sick_leave_days" type="number" min="0" required>
        </div>

        <div>
            <label>Prescriber (Doctor):</label>
            <select name="prescriber" required>
                <option value="">-- Select Doctor --</option>
                <option>Dr. T. Khothatso</option>
                <option>Locum</option>
                <option>Maleso
