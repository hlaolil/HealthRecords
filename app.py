import os
from flask import Flask, request, render_template_string, jsonify, redirect
from pymongo import MongoClient
from datetime import datetime, timedelta

app = Flask(__name__)

# MongoDB connection
MONGOURI = os.getenv('MONGOURI', 'mongodb://localhost:27017/')
client = MongoClient(MONGOURI)
db = client['pharmacy_db']
medications = db['medications']    # {name, balance, expiry_date, stock_receiver, batch, price, order_number, supplier, invoice_number}
transactions = db['transactions']   # logs

# Common navigation links
NAV_LINKS = """
<p><strong>Navigate:</strong>
    <a href="/dispense">Dispensing</a> |
    <a href="/receive">Receiving</a> |
    <a href="/add-medication">Add Medication</a> |
    <a href="/reports">Reports</a>
</p>
"""

# Template for dispensing
DISPENSE_TEMPLATE = """
<h1>Dispensing</h1>
{{ nav_links|safe }}

<h2>Dispense Medication</h2>
<form method="POST" action="/dispense">
    Patient Name: <input name="patient" required><br>
    Company: <input name="company" required><br>
    Position: <input name="position" required><br>
    Patient Age: <input name="age" type="number" min="0" required><br>
    Diagnosis: <input name="diagnosis" required><br>
    Prescriber: <input name="prescriber" required><br>
    Dispenser: <input name="dispenser" required><br>
    Date (YYYY-MM-DD): <input name="date" type="date" required><br>
    Medication: <input name="med_name" id="med_name" list="med_suggestions" required><br>
    <datalist id="med_suggestions"></datalist>
    Quantity: <input name="quantity" type="number" min="1" required><br>
    <input type="submit" value="Dispense">
</form>

<h2>Dispense Transactions</h2>
<table border="1" style="margin-top:20px; width:100%; border-collapse:collapse;">
    <thead>
        <tr style="background:#f0f0f0;">
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
    {% for t in tx_list %}
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
        <tr><td colspan="11" style="text-align:center;">No dispense transactions.</td></tr>
    {% endfor %}
    </tbody>
</table>

<script>
document.getElementById('med_name').addEventListener('input', async function() {
    const query = this.value;
    const datalist = document.getElementById('med_suggestions');
    datalist.innerHTML = ''; // Clear previous suggestions
    if (query.length < 1) return; // Don't fetch for empty input

    const response = await fetch(`/api/medications?query=${encodeURIComponent(query)}`);
    const meds = await response.json();
    meds.forEach(med => {
        const option = document.createElement('option');
        option.value = med;
        datalist.appendChild(option);
    });
});
</script>
"""

# Template for receiving
RECEIVE_TEMPLATE = """
<h1>Receiving</h1>
{{ nav_links|safe }}

<h2>Receive Medication</h2>
<form method="POST" action="/receive">
    Medication: <input name="med_name" id="med_name" list="med_suggestions" required><br>
    <datalist id="med_suggestions"></datalist>
    Quantity: <input name="quantity" type="number" min="1" required><br>
    Batch: <input name="batch" required><br>
    Price per Unit: <input name="price" type="number" step="0.01" min="0" required><br>
    Expiry Date (YYYY-MM-DD): <input name="expiry_date" type="date" required><br>
    Stock Receiver: <input name="stock_receiver" required><br>
    Order Number: <input name="order_number" required><br>
    Supplier: <input name="supplier" required><br>
    Invoice Number: <input name="invoice_number" required><br>
    <input type="submit" value="Receive">
</form>

<h2>Receive Transactions</h2>
<table border="1" style="margin-top:20px; width:100%; border-collapse:collapse;">
    <thead>
        <tr style="background:#f0f0f0;">
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
        <tr><td colspan="10" style="text-align:center;">No receive transactions.</td></tr>
    {% endfor %}
    </tbody>
</table>

<script>
document.getElementById('med_name').addEventListener('input', async function() {
    const query = this.value;
    const datalist = document.getElementById('med_suggestions');
    datalist.innerHTML = ''; // Clear previous suggestions
    if (query.length < 1) return; // Don't fetch for empty input

    const response = await fetch(`/api/medications?query=${encodeURIComponent(query)}`);
    const meds = await response.json();
    meds.forEach(med => {
        const option = document.createElement('option');
        option.value = med;
        datalist.appendChild(option);
    });
});
</script>
"""

# Template for adding new medication
ADD_MED_TEMPLATE = """
<h1>Add New Medication</h1>
{{ nav_links|safe }}

<h2>Add Medication</h2>
<form method="POST" action="/add-medication">
    Medication Name: <input name="med_name" id="med_name" list="med_suggestions" required><br>
    <datalist id="med_suggestions"></datalist>
    Initial Balance: <input name="initial_balance" type="number" min="0" required><br>
    Batch: <input name="batch" required><br>
    Price per Unit: <input name="price" type="number" step="0.01" min="0" required><br>
    Expiry Date (YYYY-MM-DD): <input name="expiry_date" type="date" required><br>
    Stock Receiver: <input name="stock_receiver" required><br>
    Order Number: <input name="order_number" required><br>
    Supplier: <input name="supplier" required><br>
    Invoice Number: <input name="invoice_number" required><br>
    <input type="submit" value="Add Medication">
</form>

<script>
document.getElementById('med_name').addEventListener('input', async function() {
    const query = this.value;
    const datalist = document.getElementById('med_suggestions');
    datalist.innerHTML = ''; // Clear previous suggestions
    if (query.length < 1) return; // Don't fetch for empty input

    const response = await fetch(`/api/medications?query=${encodeURIComponent(query)}`);
    const meds = await response.json();
    meds.forEach(med => {
        const option = document.createElement('option');
        option.value = med;
        datalist.appendChild(option);
    });
});
</script>
"""

# Template for reports
REPORTS_TEMPLATE = """
<h1>Inventory Reports</h1>
{{ nav_links|safe }}

<h2>Generate Report</h2>
<form method="POST" action="/reports">
    Report Type: 
    <select name="report_type" required>
        <option value="stock_on_hand">Stock on Hand</option>
        <option value="inventory">Inventory Report</option>
        <option value="dispense_list">Dispense List</option>
        <option value="receive_list">Receive List</option>
        <option value="out_of_stock">Out of Stock</option>
        <option value="expiry">Expired and Close to Expire</option>
    </select><br>
    {% if report_type != 'stock_on_hand' and report_type != 'out_of_stock' %}
    Start Date (YYYY-MM-DD): <input name="start_date" type="date" required><br>
    End Date (YYYY-MM-DD): <input name="end_date" type="date" required><br>
    {% endif %}
    {% if report_type == 'expiry' %}
    Close to Expire Threshold (days): <input name="close_to_expire_days" type="number" min="1" value="30" required><br>
    {% endif %}
    <input type="submit" value="Generate Report">
</form>

{% if report_type == 'stock_on_hand' and stock_data %}
<h2>Stock on Hand</h2>
<table border="1" style="margin-top:20px; width:100%; border-collapse:collapse;">
    <thead>
        <tr style="background:#f0f0f0;">
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
        <tr><td colspan="9" style="text-align:center;">No medications in stock.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'inventory' and report_data %}
<h2>Inventory Report for {{ start_date }} to {{ end_date }}</h2>
<table border="1" style="margin-top:20px; width:100%; border-collapse:collapse;">
    <thead>
        <tr style="background:#f0f0f0;">
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
        <tr><td colspan="5" style="text-align:center;">No data for this period.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'dispense_list' and dispense_list %}
<h2>Dispense List for {{ start_date }} to {{ end_date }}</h2>
<table border="1" style="margin-top:20px; width:100%; border-collapse:collapse;">
    <thead>
        <tr style="background:#f0f0f0;">
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
        <tr><td colspan="11" style="text-align:center;">No dispense transactions in this period.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'receive_list' and receive_list %}
<h2>Receive List for {{ start_date }} to {{ end_date }}</h2>
<table border="1" style="margin-top:20px; width:100%; border-collapse:collapse;">
    <thead>
        <tr style="background:#f0f0f0;">
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
        <tr><td colspan="10" style="text-align:center;">No receive transactions in this period.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'out_of_stock' and stock_data %}
<h2>Out of Stock</h2>
<table border="1" style="margin-top:20px; width:100%; border-collapse:collapse;">
    <thead>
        <tr style="background:#f0f0f0;">
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
        <tr><td colspan="9" style="text-align:center;">No medications out of stock.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% elif report_type == 'expiry' and expiry_data %}
<h2>Expired and Close to Expire (within {{ close_to_expire_days }} days)</h2>
<table border="1" style="margin-top:20px; width:100%; border-collapse:collapse;">
    <thead>
        <tr style="background:#f0f0f0;">
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
        <tr><td colspan="10" style="text-align:center;">No medications expired or close to expiry.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% endif %}
"""

# API endpoint for medication suggestions
@app.route('/api/medications', methods=['GET'])
def get_medication_suggestions():
    query = request.args.get('query', '')
    # Case-insensitive search for names starting with query
    meds = list(medications.find(
        {'name': {'$regex': f'^{query}', '$options': 'i'}},
        {'_id': 0, 'name': 1}
    ).sort('name', 1))
    # Return list of names
    return jsonify([med['name'] for med in meds])

# Routes
@app.route('/', methods=['GET'])
def home():
    return redirect('/reports')  # Redirect to reports page

@app.route('/dispense', methods=['GET', 'POST'])
def dispense():
    if request.method == 'POST':
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

        med = medications.find_one({'name': med_name})
        if not med:
            return f'Medication "{med_name}" not found. <a href="/dispense">Back</a>'
        if med['balance'] < quantity:
            return f'Insufficient stock for "{med_name}". <a href="/dispense">Back</a>'

        medications.update_one({'name': med_name}, {'$inc': {'balance': -quantity}})
        transactions.insert_one({
            'type': 'dispense',
            'patient': patient,
            'company': company,
            'position': position,
            'age': age,
            'diagnosis': diagnosis,
            'prescriber': prescriber,
            'dispenser': dispenser,
            'date': date_str,
            'med_name': med_name,
            'quantity': quantity,
            'timestamp': datetime.utcnow()
        })
        return 'Dispensed successfully! <a href="/dispense">Back</a>'

    tx_list = list(transactions.find({'type': 'dispense'}).sort('timestamp', -1))
    return render_template_string(DISPENSE_TEMPLATE, tx_list=tx_list, nav_links=NAV_LINKS)

@app.route('/receive', methods=['GET', 'POST'])
def receive():
    if request.method == 'POST':
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
        return 'Received successfully! <a href="/receive">Back</a>'

    tx_list = list(transactions.find({'type': 'receive'}).sort('timestamp', -1))
    return render_template_string(RECEIVE_TEMPLATE, tx_list=tx_list, nav_links=NAV_LINKS)

@app.route('/add-medication', methods=['GET', 'POST'])
def add_medication():
    if request.method == 'POST':
        med_name = request.form['med_name']
        initial_balance = int(request.form['initial_balance'])
        batch = request.form['batch']
        price = float(request.form['price'])
        expiry_date = request.form['expiry_date']
        stock_receiver = request.form['stock_receiver']
        order_number = request.form['order_number']
        supplier = request.form['supplier']
        invoice_number = request.form['invoice_number']

        # Check if medication already exists
        if medications.find_one({'name': med_name}):
            return f'Medication "{med_name}" already exists. Use Receiving to add stock. <a href="/add-medication">Back</a>'

        # Insert new medication
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
        # Log as a receive transaction
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
        return 'Medication added successfully! <a href="/add-medication">Back</a>'

    return render_template_string(ADD_MED_TEMPLATE, nav_links=NAV_LINKS)

@app.route('/reports', methods=['GET', 'POST'])
def reports():
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
        report_type = request.form['report_type']
        if report_type not in ['stock_on_hand', 'out_of_stock']:
            start_date = request.form['start_date']
            end_date = request.form['end_date']
            # Convert dates to datetime objects (UTC, midnight)
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)  # End of day
        if report_type == 'expiry':
            close_to_expire_days = int(request.form.get('close_to_expire_days', 30))

        if report_type == 'stock_on_hand':
            # Stock on Hand Report
            stock_data = list(medications.find({}, {'_id': 0}).sort('name', 1))
        elif report_type == 'out_of_stock':
            # Out of Stock Report
            stock_data = list(medications.find({'balance': 0}, {'_id': 0}).sort('name', 1))
        elif report_type == 'inventory':
            # Inventory Report
            meds = list(medications.find({}, {'_id': 0, 'name': 1, 'balance': 1}).sort('name', 1))
            for med in meds:
                med_name = med['name']
                # Calculate beginning balance (before start_date)
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

                # Calculate dispensed and received in date range
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
                    'beginning_balance': max(0, beginning_balance),  # Prevent negative
                    'dispensed': dispensed,
                    'received': received,
                    'current_balance': med['balance']
                })
        elif report_type == 'dispense_list':
            # Dispense List
            dispense_list = list(transactions.find({
                'type': 'dispense',
                'timestamp': {'$gte': start_dt, '$lte': end_dt}
            }).sort('timestamp', 1))
        elif report_type == 'receive_list':
            # Receive List
            receive_list = list(transactions.find({
                'type': 'receive',
                'timestamp': {'$gte': start_dt, '$lte': end_dt}
            }).sort('timestamp', 1))
        elif report_type == 'expiry':
            # Expired and Close to Expire Report
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

if __name__ == '__main__':
    app.run(debug=True)
