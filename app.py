import os
from flask import Flask, request, render_template_string, jsonify, redirect
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from datetime import datetime, timedelta

app = Flask(__name__)

# MongoDB connection function (lazy initialization for fork-safety)
def get_mongo_client():
    mongouri = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
    return MongoClient(mongouri, serverSelectionTimeoutMS=5000)

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
    .nav-links {
        text-align: center;
        margin-bottom: 20px;
        font-size: 16px;
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
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 15px;
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
        grid-column: 1 / -1;
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
        .dispense-form,
        .receive-form,
        .add-medication-form {
            grid-template-columns: 1fr; /* Single column on mobile */
        }
        table th, table td {
            font-size: 14px;
            padding: 8px;
        }
    }
</style>
"""


# ✅ Updated Dispense Template with dropdowns for Doctors, Issuers, and Age Groups
DISPENSE_TEMPLATE = CSS_STYLE + """
<h1>Dispensing</h1>
{{ nav_links|safe }}

{% if message %}
    <p class="message {% if 'successfully' in message|lower %}success{% else %}error{% endif %}">{{ message }}</p>
{% endif %}

<h2>Dispense Medication</h2>

<form method="POST" action="{{ url_for('dispense') }}" class="dispense-form">
    <div>
        <label>Medication:</label><input name="med_name" id="med_name" list="med_suggestions" required>
        <datalist id="med_suggestions"></datalist>
    </div>

    <div>
        <label>Quantity:</label>
        <input name="quantity" type="number" min="1" required>
    </div>

    <div>
        <label>Patient:</label>
        <input name="patient" type="text" required>
    </div>

    <div>
        <label for="company">Company:</label>
        <select id="company" name="company" required>
          <option value="">-- Select Company --</option>
          <option>Eminence</option>
          <option>BLW</option>
          <option>Mendi</option>
          <option>BUSY BEE</option>
          <option>CMS</option>
          <option>PLATO</option>
          <option>LD</option>
          <option>LISELO</option>
          <option>LMPS</option>
          <option>MGC</option>
          <option>MINOPEX</option>
          <option>NMC</option>
          <option>Public</option>
          <option>Enaex</option>
          <option>TOMRA</option>
          <option>IFS</option>
          <option>UL4</option>
          <option>UNITRANS</option>
          <option>THOLO</option>
          <option>Other</option>
          <option>Government</option>
          <option>Consulmet</option>
          <option>Other</option>
        </select>
    </div>

    <div>
        <label for="position">Position:</label>
        <select id="position" name="position" required>
          <option value="">-- Select Position --</option>
          <option>Operator</option>
          <option>Supervisor</option>
          <option>Manager</option>
          <option>Cleaner</option>
          <option>Drivers</option>
          <option>Plant Operator</option>
          <option>Maintenance</option>
          <option>Hse</option>
          <option>Storekeeper</option>
          <option>Mechanics</option>
          <option>Administration</option>
          <option>Electricians</option>
          <option>Fitters</option>
          <option>Recovery</option>
          <option>Geologist</option>
          <option>Workshop Cleaners</option>
          <option>Kitchen</option>
          <option>Controller</option>
          <option>Management</option>
          <option>Emergency Coordinator</option>
          <option>Security</option>
          <option>Medical Doctor</option>
          <option>Nurse</option>
          <option>PHC</option>
          <option>X-Ray Technologist</option>
          <option>Lab Technologist</option>
          <option>Boiler Maker</option>
          <option>Housekeeping</option>
          <option>Pharmacist</option>
          <option>Process</option>
          <option>Mining</option>
          <option>General Worker</option>
          <option>Blasting</option>
          <option>Chef</option>
          <option>Food Service Attendant</option>
          <option>Other</option>
          <option>Drilling</option>
          <option>Treatment</option>
          <option>Sorting</option>
          <option>Diesel Attendant</option>
          <option>Welder</option>
          <option>Water Works</option>
          <option>Intern</option>
          <option>CI</option>
          <option>Finance</option>
          <option>Procurement</option>
          <option>Metallurgy</option>
          <option>Tyreman</option>
          <option>Training</option>
          <option>Artisan</option>
          <option>IT</option>
          <option>Production</option>
          <option>Survey</option>
          <option>Visitor</option>
          <option>Environmnet</option>
          <option>Tourist</option>
          <option>Police</option>
          <option>Public</option>
          <option>Director</option>
          <option>Technician</option>
          <option>Other</option>
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
        <label>Diagnosis:</label>
        <input name="diagnosis" type="text" required>
    </div>

    <div>
        <label>Prescriber (Doctor):</label>
        <select name="prescriber" required>
            <option value="">-- Select Doctor --</option>
            <option>Dr. T. Khothatso</option>
            <option>Mamosa Seetsa</option>
            <option>Mapalo Mapesela</option>
            <option>Mathuto Kutoane</option>
            <option>Mamosaase Nqosa</option>
            <option>Malesoetsa Leohla</option>
            <option>Locum</option>
            <option>Thapelo Mphole</option>
        </select>
    </div>

    <div>
        <label>Dispenser (Issuer):</label>
        <select name="dispenser" required>
            <option value="">-- Select Issuer --</option>
            <option>Letlotlo Hlaoli</option>
            <option>Mamosa Seetsa</option>
            <option>Mapalo Mapesela</option>
            <option>Mathuto Kutoane</option>
            <option>Mamosaase Nqosa</option>
            <option>Malesoetsa Leohla</option>
            <option>Locum</option>
            <option>Thapelo Mphole</option>
        </select>
    </div>

    <div>
        <label>Date:</label>
        <input name="date" type="date" required>
    </div>

    <div class="form-buttons">
        <input type="submit" value="Dispense">
        <button type="button" onclick="document.querySelector('form').reset();">Clear Form</button>
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
            <th>Diagnosis</th>
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
            <td>{{ t.diagnosis }}</td>
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
// Clear form after successful dispense
{% if message and 'successfully' in message|lower %}
    document.querySelector('form').reset();
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
    <div>
        <label>Medication:</label>
        <input name="med_name" id="med_name" list="med_suggestions" required>
        <datalist id="med_suggestions"></datalist>
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
    <div>
        <label>Medication Name:</label>
        <input name="med_name" id="med_name" list="med_suggestions" required>
        <datalist id="med_suggestions"></datalist>
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
            <th>Stock Receiver</th>
            <th>Order Number</th>
            <th>Supplier</th>
            <th>Invoice Number</th>
        </tr>
    </thead>
    <tbody>
    {% for med in stock_data %}
        <tr>
            <td>{{ med.name }}</td>
            <td>{{ med.balance }}</td>
            <td>{{ med.expiry_date }}</td>
            <td>{{ med.batch }}</td>
            <td>${{ "%.2f"|format(med.price) }}</td>
            <td>{{ med.stock_receiver }}</td>
            <td>{{ med.order_number }}</td>
            <td>{{ med.supplier }}</td>
            <td>{{ med.invoice_number }}</td>
        </tr>
    {% else %}
        <tr><td colspan="9">No medications in stock.</td></tr>
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
        </tr>
    {% else %}
        <tr><td colspan="5">No data for this period.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'dispense_list' and dispense_list %}
<h2>Dispense List for {{ start_date }} to {{ end_date }}</h2>
<table>
    <thead>
        <tr>
            <th>Medication</th>
            <th>Quantity</th>
            <th>Patient</th>
            <th>Company</th>
            <th>Position</th>
            <th>Age</th>
            <th>Diagnosis</th>
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
            <td>{{ t.age }}</td>
            <td>{{ t.diagnosis }}</td>
            <td>{{ t.prescriber }}</td>
            <td>{{ t.dispenser }}</td>
            <td>{{ t.date }}</td>
            <td>{{ t.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
        </tr>
    {% else %}
        <tr><td colspan="11">No dispense transactions in this period.</td></tr>
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
            <th>Stock Receiver</th>
            <th>Order Number</th>
            <th>Supplier</th>
            <th>Invoice Number</th>
        </tr>
    </thead>
    <tbody>
    {% for med in stock_data %}
        <tr>
            <td>{{ med.name }}</td>
            <td>{{ med.balance }}</td>
            <td>{{ med.expiry_date }}</td>
            <td>{{ med.batch }}</td>
            <td>${{ "%.2f"|format(med.price) }}</td>
            <td>{{ med.stock_receiver }}</td>
            <td>{{ med.order_number }}</td>
            <td>{{ med.supplier }}</td>
            <td>{{ med.invoice_number }}</td>
        </tr>
    {% else %}
        <tr><td colspan="9">No medications out of stock.</td></tr>
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
        <tr>
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
                age = int(request.form['age'])
                diagnosis = request.form['diagnosis']
                prescriber = request.form['prescriber']
                dispenser = request.form['dispenser']
                date_str = request.form['date']
                med_name = request.form['med_name']
                quantity = int(request.form['quantity'])
                gender = request.form['gender']
                sick_leave_days = int(request.form['sick_leave_days'])


                med = medications.find_one({'name': med_name})
                if not med:
                    message = f'Medication "{med_name}" not found.'
                elif med['balance'] < quantity:
                    message = f'Insufficient stock for "{med_name}".'
                else:
                    medications.update_one({'name': med_name}, {'$inc': {'balance': -quantity}})
                    transactions.insert_one({
                       'type': 'dispense',
                       'patient': patient,
                        'company': company,
                        'position': position,
                        'age': age,
                        'gender': gender,
                        'sick_leave_days': sick_leave_days,
                        'diagnosis': diagnosis,
                        'prescriber': prescriber,
                        'dispenser': dispenser,
                        'date': date_str,
                        'med_name': med_name,
                        'quantity': quantity,
                        'timestamp': datetime.utcnow()
                    })

                    message = 'Dispensed successfully!'
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
        close_to_expire_days = None

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
                elif report_type == 'out_of_stock':
                    stock_data = list(medications.find({'balance': 0}, {'_id': 0}).sort('name', 1))
                elif report_type == 'inventory':
                    meds = list(medications.find({}, {'_id': 0, 'name': 1, 'balance': 1}).sort('name', 1))
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

                        report_data.append({
                            'med_name': med_name,
                            'beginning_balance': max(0, beginning_balance),
                            'dispensed': dispensed,
                            'received': received,
                            'current_balance': med['balance']
                        })
                elif report_type == 'dispense_list':
                    dispense_list = list(transactions.find({
                        'type': 'dispense',
                        'timestamp': {'$gte': start_dt, '$lte': end_dt}
                    }).sort('timestamp', 1))
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
                            expiry_data.append(med)
                        elif expiry_dt <= threshold_date:
                            med['expiry_status'] = 'Close to Expire'
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
                    close_to_expire_days=None
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
            close_to_expire_days=None
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
