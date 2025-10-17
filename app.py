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
                <option>Malesoetsa Leohla</option>
                <option>Mamosa Seetsa</option>
                <option>Mamosaase Nqosa</option>
                <option>Mapalo Mapesela</option>
                <option>Mathuto Kutoane</option>
                <option>Thapelo Mphole</option>
            </select>
        </div>

        <div>
            <label>Dispenser (Issuer):</label>
            <select name="dispenser" required>
                <option value="">-- Select Issuer --</option>
                <option>Letlotlo Hlaoli</option>
                <option>Locum</option>
                <option>Malesoetsa Leohla</option>
                <option>Mamosa Seetsa</option>
                <option>Mamosaase Nqosa</option>
                <option>Mapalo Mapesela</option>
                <option>Mathuto Kutoane</option>
                <option>Thapelo Mphole</option>
            </select>
        </div>

        <div>
            <label>Date:</label>
            <input name="date" type="date" required>
        </div>
    </div>

    <div class="diag-section">
        <h3>Diagnoses (up to 3)</h3>
        <div id="diagnoses">
            <div class="diag-row">
                <div>
                    <label>Diagnosis:</label>
                    <input name="diagnoses[]" type="text" class="diag-input" required>
                </div>
                <div>
                    <button type="button" onclick="removeDiagRow(this)">Remove</button>
                </div>
            </div>
        </div>
        <button type="button" onclick="addDiagRow()">Add Diagnosis</button>
    </div>

    <div class="med-section">
        <h3>Medications (up to 12)</h3>
        <datalist id="med_suggestions"></datalist>
        <div id="medications">
            <div class="med-row">
                <div>
                    <label>Medication:</label>
                    <input name="med_names[]" list="med_suggestions" class="med-input" required>
                </div>
                <div>
                    <label>Quantity:</label>
                    <input name="quantities[]" type="number" min="1" required>
                </div>
                <div>
                    <button type="button" onclick="removeRow(this)">Remove</button>
                </div>
            </div>
        </div>
        <button type="button" onclick="addRow()">Add Medication</button>
    </div>

    <div class="form-buttons">
        <input type="submit" value="Dispense">
        <button type="button" onclick="clearForm()">Clear Form</button>
    </div>
</form>

<hr>

<h3>Dispense Transactions</h3>
<table>
    <thead>
        <tr>
            <th>Medication</th>
            <th>Quantity</th>
            <th>Patient</th>
            <th>Company</th>
            <th>Position</th>
            <th>Age Group</th>
            <th>Gender</th>
            <th>Sick Leave (Days)</th>
            <th>Diagnoses</th>
            <th>Prescriber</th>
            <th>Dispenser</th>
            <th>Date</th>
            <th>Timestamp</th>
        </tr>
    </thead>
    <tbody>
        {% for t in tx_list %}
        <tr>
            <td>{{ t.med_name }}</td>
            <td>{{ t.quantity }}</td>
            <td>{{ t.patient }}</td>
            <td>{{ t.company }}</td>
            <td>{{ t.position }}</td>
            <td>{{ t.age_group }}</td>
            <td>{{ t.gender }}</td>
            <td>{{ t.sick_leave_days }}</td>
            <td>{{ t.diagnoses | join(', ') if t.diagnoses else '' }}</td>
            <td>{{ t.prescriber }}</td>
            <td>{{ t.dispenser }}</td>
            <td>{{ t.date }}</td>
            <td>{{ t.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
        </tr>
        {% else %}
        <tr><td colspan="13">No dispense transactions.</td></tr>
        {% endfor %}
    </tbody>
</table>

<script>
let medRowCount = 1;
let diagRowCount = 1;

function addInputListener(input) {
    input.addEventListener('input', async function() {
        const query = this.value;
        const datalist = document.getElementById('med_suggestions');
        datalist.innerHTML = '';
        if (query.length < 1) return;

        const response = await fetch(`/api/medications?query=${encodeURIComponent(query)}`);
        const meds = await response.json();
        if (meds.error) {
            console.error(meds.error);
            return;
        }
        meds.forEach(med => {
            const option = document.createElement('option');
            option.value = med;
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
            <input name="med_names[]" list="med_suggestions" class="med-input" required>
        </div>
        <div>
            <label>Quantity:</label>
            <input name="quantities[]" type="number" min="1" required>
        </div>
        <div>
            <button type="button" onclick="removeRow(this)">Remove</button>
        </div>
    `;
    container.appendChild(newRow);
    const newInput = newRow.querySelector('.med-input');
    addInputListener(newInput);
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
            <input name="diagnoses[]" type="text" class="diag-input">
        </div>
        <div>
            <button type="button" onclick="removeDiagRow(this)">Remove</button>
        </div>
    `;
    container.appendChild(newRow);
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
}

// Initialize listeners for existing inputs
document.addEventListener('DOMContentLoaded', function() {
    const existingInputs = document.querySelectorAll('.med-input');
    existingInputs.forEach(addInputListener);
});

// Clear form after successful dispense
{% if message and 'successfully' in message|lower %}
    clearForm();
{% endif %}
</script>
"""
RECEIVE_TEMPLATE = CSS_STYLE + """
<h1>Receiving</h1>{{ nav_links|safe }}

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
        <button type="button" onclick="document.querySelector('form').reset();">Clear Form</button>
    </div>
</form>

<h2>Receive Transactions</h2>
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
            <td>{{ t.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
        </tr>
        {% else %}
        <tr><td colspan="10">No receive transactions.</td></tr>
        {% endfor %}
    </tbody>
</table>

<script>
document.getElementById('med_name').addEventListener('input', async function() {
    const query = this.value;
    const datalist = document.getElementById('med_suggestions');
    datalist.innerHTML = '';
    if (query.length < 1) return;

    const response = await fetch(`/api/medications?query=${encodeURIComponent(query)}`);
    const meds = await response.json();
    if (meds.error) {
        console.error(meds.error);
        return;
    }
    meds.forEach(med => {
        const option = document.createElement('option');
        option.value = med;
        datalist.appendChild(option);
    });
});
</script>
"""

# Add Medication Template
ADD_MED_TEMPLATE = CSS_STYLE + """
<h1>Add New Medication</h1>
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
        <button type="button" onclick="document.querySelector('form').reset();">Clear Form</button>
    </div>
</form>

<script>
document.getElementById('med_name').addEventListener('input', async function() {
    const query = this.value;
    const datalist = document.getElementById('med_suggestions');
    datalist.innerHTML = '';
    if (query.length < 1) return;

    const response = await fetch(`/api/medications?query=${encodeURIComponent(query)}`);
    const meds = await response.json();
    if (meds.error) {
        console.error(meds.error);
        return;
    }
    meds.forEach(med => {
        const option = document.createElement('option');
        option.value = med;
        datalist.appendChild(option);
    });
});
</script>
"""


# Reports Template
REPORTS_TEMPLATE = CSS_STYLE + """
<h1>Inventory Reports</h1>
{{ nav_links|safe }}

{% if message %}
    <p class="message {% if 'successfully' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}

<h2>Generate Report</h2>
<form method="POST" action="/reports">
    <label>Report Type:</label>
    <select name="report_type" required>
        <option value="stock_on_hand">Stock on Hand</option>
        <option value="inventory">Inventory Report</option>
        <option value="dispense_list">Dispense List</option>
        <option value="receive_list">Receive List</option>
        <option value="out_of_stock">Out of Stock</option>
        <option value="expiry">Expired and Close to Expire</option>
    </select><br>
    {% if report_type != 'stock_on_hand' and report_type != 'out_of_stock' %}
    <label>Start Date (YYYY-MM-DD):</label><input name="start_date" type="date" required><br>
    <label>End Date (YYYY-MM-DD):</label><input name="end_date" type="date" required><br>
    {% endif %}
    {% if report_type == 'expiry' %}
    <label>Close to Expire Threshold (days):</label><input name="close_to_expire_days" type="number" min="1" value="30" required><br>
    {% endif %}
    <input type="submit" value="Generate Report">
</form>

{% if report_type == 'stock_on_hand' and stock_data %}
<h2>Stock on Hand</h2>
<table>
    <thead>
        <tr>
            <th>Medication</th>
            <th>Balance</th>
            <th>Expiry Date</th>
            <th>Batch</th>
            <th>Price</th>
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
        </tr>
    {% else %}
        <tr><td colspan="5">No medications in stock.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'inventory' and report_data %}
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
{% elif report_type == 'dispense_list' and dispense_list %}
<h2>Dispense List for {{ start_date }} to {{ end_date }}</h2>
<h3>Total Transactions: {{ total_transactions }}</h3>
<table>
    <thead>
        <tr>
            <th>Medication</th>
            <th>Quantity</th>
            <th>Patient</th>
            <th>Company</th>
            <th>Position</th>
            <th>Age Group</th>
            <th>Gender</th>
            <th>Sick Leave (Days)</th>
            <th>Diagnoses</th>
            <th>Prescriber</th>
            <th>Dispenser</th>
            <th>Date</th>
            <th>Timestamp</th>
        </tr>
    </thead>
    <tbody>
    {% for t in dispense_list %}
        <tr>
            <td>{{ t.med_name }}</td>
            <td>{{ t.quantity }}</td>
            <td>{{ t.patient }}</td>
            <td>{{ t.company }}</td>
            <td>{{ t.position }}</td>
            <td>{{ t.age_group }}</td>
            <td>{{ t.gender }}</td>
            <td>{{ t.sick_leave_days }}</td>
            <td>{{ t.diagnoses | join(', ') if t.diagnoses else '' }}</td>
            <td>{{ t.prescriber }}</td>
            <td>{{ t.dispenser }}</td>
            <td>{{ t.date }}</td>
            <td>{{ t.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
        </tr>
    {% else %}
        <tr><td colspan="13">No dispense transactions in this period.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'receive_list' and receive_list %}
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
            <td>{{ t.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
        </tr>
    {% else %}
        <tr><td colspan="10">No receive transactions in this period.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'out_of_stock' and stock_data %}
<h2>Out of Stock</h2>
<table>
    <thead>
        <tr>
            <th>Medication</th>
            <th>Balance</th>
            <th>Expiry Date</th>
            <th>Batch</th>
            <th>Price</th>
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
        </tr>
    {% else %}
        <tr><td colspan="5">No medications out of stock.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'expiry' and expiry_data %}
<h2>Expired and Close to Expire (within {{ close_to_expire_days }} days)</h2>
<table>
    <thead>
        <tr>
            <th>Medication</th>
            <th>Balance</th>
            <th>Expiry Date</th>
            <th>Expiry Status</th>
            <th>Batch</th>
            <th>Price</th>
            <th>Stock Receiver</th>
            <th>Order Number</th>
            <th>Supplier</th>
            <th>Invoice Number</th>
        </tr>
    </thead>
    <tbody>
    {% for med in expiry_data %}
        <tr class="{{ med.status }}">
            <td>{{ med.name }}</td>
            <td>{{ med.balance }}</td>
            <td>{{ med.expiry_date }}</td>
            <td>{{ med.expiry_status }}</td>
            <td>{{ med.batch }}</td>
            <td>${{ "%.2f"|format(med.price) }}</td>
            <td>{{ med.stock_receiver }}</td>
            <td>{{ med.order_number }}</td>
            <td>{{ med.supplier }}</td>
            <td>{{ med.invoice_number }}</td>
        </tr>
    {% else %}
        <tr><td colspan="10">No medications expired or close to expiry.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% endif %}
"""

# Routes
@app.route('/', methods=['GET'])
def home():
    return redirect('/reports')

@app.route('/dispense', methods=['GET', 'POST'])
def dispense():
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        message = None
        tx_list = list(transactions.find({'type': 'dispense'}).sort('timestamp', -1))

        if request.method == 'POST':
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

                diagnoses = [d.strip() for d in request.form.getlist('diagnoses[]') if d.strip()]
                if not diagnoses:
                    message = 'Please provide at least one diagnosis.'
                else:
                    med_names = [name.strip() for name in request.form.getlist('med_names[]') if name.strip()]
                    quantities_str = request.form.getlist('quantities[]')
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
                            elif med['balance'] < quantity:
                                error_msgs.append(f'Insufficient stock for "{med_name}".')
                                success = False
                                continue
                            else:
                                medications.update_one({'name': med_name}, {'$inc': {'balance': -quantity}})
                                transactions.insert_one({
                                    'type': 'dispense',
                                    'patient': patient,
                                    'company': company,
                                    'position': position,
                                    'age_group': age_group,
                                    'gender': gender,
                                    'sick_leave_days': sick_leave_days,
                                    'diagnoses': diagnoses,
                                    'prescriber': prescriber,
                                    'dispenser': dispenser,
                                    'date': date_str,
                                    'med_name': med_name,
                                    'quantity': quantity,
                                    'timestamp': datetime.utcnow()
                                })
                                dispensed_meds.append(med_name)

                        if success and dispensed_meds:
                            message = f'Dispensed successfully: {", ".join(dispensed_meds)}'
                        else:
                            message = '; '.join(error_msgs) if error_msgs else 'No medications dispensed.'

                tx_list = list(transactions.find({'type': 'dispense'}).sort('timestamp', -1))
                return render_template_string(DISPENSE_TEMPLATE, tx_list=tx_list, nav_links=NAV_LINKS, message=message)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template_string(DISPENSE_TEMPLATE, tx_list=tx_list, nav_links=NAV_LINKS, message=message)
        return render_template_string(DISPENSE_TEMPLATE, tx_list=tx_list, nav_links=NAV_LINKS, message=message)
    except ServerSelectionTimeoutError:
        return render_template_string(DISPENSE_TEMPLATE, tx_list=[], nav_links=NAV_LINKS, message="Database connection failed. Please try again later."), 500
    finally:
        client.close()

@app.route('/receive', methods=['GET', 'POST'])
def receive():
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        message = None
        tx_list = list(transactions.find({'type': 'receive'}).sort('timestamp', -1))

        if request.method == 'POST':
            try:
                med_name = request.form['med_name']
                quantity = int(request.form['quantity'])
                batch = request.form['batch']
                price = float(request.form['price'])
                expiry_date = request.form['expiry_date']
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
                    'stock_receiver': stock_receiver,
                    'order_number': order_number,
                    'supplier': supplier,
                    'invoice_number': invoice_number,
                    'timestamp': datetime.utcnow()
                })
                message = 'Received successfully!'
                tx_list = list(transactions.find({'type': 'receive'}).sort('timestamp', -1))
                return render_template_string(RECEIVE_TEMPLATE, tx_list=tx_list, nav_links=NAV_LINKS, message=message)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template_string(RECEIVE_TEMPLATE, tx_list=tx_list, nav_links=NAV_LINKS, message=message)
        return render_template_string(RECEIVE_TEMPLATE, tx_list=tx_list, nav_links=NAV_LINKS, message=message)
    except ServerSelectionTimeoutError:
        return render_template_string(RECEIVE_TEMPLATE, tx_list=[], nav_links=NAV_LINKS, message="Database connection failed. Please try again later."), 500
    finally:
        client.close()

@app.route('/add-medication', methods=['GET', 'POST'])
def add_medication():
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        message = None

        if request.method == 'POST':
            try:
                med_name = request.form['med_name']
                initial_balance = int(request.form['initial_balance'])
                batch = request.form['batch']
                price = float(request.form['price'])
                expiry_date = request.form['expiry_date']
                stock_receiver = request.form['stock_receiver']
                order_number = request.form['order_number']
                supplier = request.form['supplier']
                invoice_number = request.form['invoice_number']

                if medications.find_one({'name': med_name}):
                    message = f'Medication "{med_name}" already exists. Use Receiving to add stock.'
                    return render_template_string(ADD_MED_TEMPLATE, nav_links=NAV_LINKS, message=message)

                medications.insert_one({
                    'name': med_name,
                    'balance': initial_balance,
                    'batch': batch,
                    'price': price,
                    'expiry_date': expiry_date,
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
                    'stock_receiver': stock_receiver,
                    'order_number': order_number,
                    'supplier': supplier,
                    'invoice_number': invoice_number,
                    'timestamp': datetime.utcnow()
                })
                message = 'Medication added successfully!'
                return render_template_string(ADD_MED_TEMPLATE, nav_links=NAV_LINKS, message=message)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template_string(ADD_MED_TEMPLATE, nav_links=NAV_LINKS, message=message)
        return render_template_string(ADD_MED_TEMPLATE, nav_links=NAV_LINKS, message=message)
    except ServerSelectionTimeoutError:
        return render_template_string(ADD_MED_TEMPLATE, nav_links=NAV_LINKS, message="Database connection failed. Please try again later."), 500
    finally:
        client.close()

@app.route('/reports', methods=['GET', 'POST'])
def reports():
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        report_data = []
        dispense_list = []
        receive_list = []
        stock_data = []
        expiry_data = []
        report_type = None
        start_date = None
        end_date = None
        close_to_expire_days = 30
        total_transactions = 0

        if request.method == 'POST':
            try:
                report_type = request.form['report_type']
                if report_type not in ['stock_on_hand', 'out_of_stock']:
                    start_date = request.form['start_date']
                    end_date = request.form['end_date']
                    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
                    end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
                if report_type == 'expiry':
                    close_to_expire_days = int(request.form.get('close_to_expire_days', 30))

                if report_type == 'stock_on_hand':
                    stock_data = list(medications.find({}, {'_id': 0}).sort('name', 1))
                    today = datetime.utcnow().date()
                    threshold_date = today + timedelta(days=30)
                    for med in stock_data:
                        expiry_dt = datetime.strptime(med['expiry_date'], '%Y-%m-%d').date()
                        if med['balance'] == 0:
                            med['status'] = 'out-of-stock'
                        elif expiry_dt < today:
                            med['status'] = 'expired'
                        elif expiry_dt <= threshold_date:
                            med['status'] = 'close-to-expire'
                        else:
                            med['status'] = 'normal'
                elif report_type == 'out_of_stock':
                    stock_data = list(medications.find({'balance': 0}, {'_id': 0}).sort('name', 1))
                    for med in stock_data:
                        med['status'] = 'out-of-stock'
                elif report_type == 'inventory':
                    meds = list(medications.find({}, {'_id': 0, 'name': 1, 'balance': 1}).sort('name', 1))
                    start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
                    end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()
                    days_in_period = (end_date_obj - start_date_obj).days + 1
                    for med in meds:
                        med_name = med['name']
                        pre_transactions = transactions.find({
                            'med_name': med_name,
                            'timestamp': {'$lt': start_dt}
                        })
                        beginning_balance = 0
                        for tx in pre_transactions:
                            if tx['type'] == 'receive':
                                beginning_balance += tx['quantity']
                            elif tx['type'] == 'dispense':
                                beginning_balance -= tx['quantity']

                        period_transactions = transactions.find({
                            'med_name': med_name,
                            'timestamp': {'$gte': start_dt, '$lte': end_dt}
                        })
                        dispensed = 0
                        received = 0
                        for tx in period_transactions:
                            if tx['type'] == 'dispense':
                                dispensed += tx['quantity']
                            elif tx['type'] == 'receive':
                                received += tx['quantity']

                        average_daily = dispensed / days_in_period if days_in_period > 0 else 0
                        average_monthly = average_daily * 30
                        lead_time_stock = average_daily * 14
                        amount_to_order = max(0, average_monthly - med['balance'] + lead_time_stock)

                        report_data.append({
                            'med_name': med_name,
                            'beginning_balance': max(0, beginning_balance),
                            'dispensed': dispensed,
                            'received': received,
                            'current_balance': med['balance'],
                            'amount_to_order': int(amount_to_order) if amount_to_order.is_integer() else round(amount_to_order, 2)
                        })
                elif report_type == 'dispense_list':
                    dispense_list = list(transactions.find({
                        'type': 'dispense',
                        'timestamp': {'$gte': start_dt, '$lte': end_dt}
                    }).sort('timestamp', 1))
                    total_transactions = len(dispense_list) if dispense_list else 0
                elif report_type == 'receive_list':
                    receive_list = list(transactions.find({
                        'type': 'receive',
                        'timestamp': {'$gte': start_dt, '$lte': end_dt}
                    }).sort('timestamp', 1))
                elif report_type == 'expiry':
                    today = datetime.utcnow().date()
                    threshold_date = today + timedelta(days=close_to_expire_days)
                    expiry_data = []
                    meds = list(medications.find({}, {'_id': 0}).sort('name', 1))
                    for med in meds:
                        expiry_dt = datetime.strptime(med['expiry_date'], '%Y-%m-%d').date()
                        if expiry_dt < today:
                            med['expiry_status'] = 'Expired'
                            med['status'] = 'expired'
                            expiry_data.append(med)
                        elif expiry_dt <= threshold_date:
                            med['expiry_status'] = 'Close to Expire'
                            med['status'] = 'close-to-expire'
                            expiry_data.append(med)
            except ValueError as e:
                return render_template_string(
                    REPORTS_TEMPLATE,
                    nav_links=NAV_LINKS,
                    message=f'Invalid input: {str(e)}',
                    report_type=None,
                    report_data=[],
                    dispense_list=[],
                    receive_list=[],
                    stock_data=[],
                    expiry_data=[],
                    start_date=None,
                    end_date=None,
                    close_to_expire_days=close_to_expire_days,
                    total_transactions=0
                )

        return render_template_string(
            REPORTS_TEMPLATE,
            report_type=report_type,
            report_data=report_data,
            dispense_list=dispense_list,
            receive_list=receive_list,
            stock_data=stock_data,
            expiry_data=expiry_data,
            start_date=start_date,
            end_date=end_date,
            close_to_expire_days=close_to_expire_days,
            total_transactions=total_transactions,
            nav_links=NAV_LINKS
        )
    except ServerSelectionTimeoutError:
        return render_template_string(
            REPORTS_TEMPLATE,
            nav_links=NAV_LINKS,
            message="Database connection failed. Please try again later.",
            report_type=None,
            report_data=[],
            dispense_list=[],
            receive_list=[],
            stock_data=[],
            expiry_data=[],
            start_date=None,
            end_date=None,
            close_to_expire_days=close_to_expire_days,
            total_transactions=0
        ), 500
    finally:
        client.close()

@app.route('/api/medications', methods=['GET'])
def get_medication_suggestions():
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        query = request.args.get('query', '')
        meds = list(medications.find(
            {'name': {'$regex': f'^{query}', '$options': 'i'}},
            {'_id': 0, 'name': 1}
        ).sort('name', 1))
        return jsonify([med['name'] for med in meds])
    except ServerSelectionTimeoutError:
        return jsonify({'error': 'Database connection failed'}), 500
    finally:
        client.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
