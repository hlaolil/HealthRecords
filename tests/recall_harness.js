// Runs the page's own recall code against a tiny DOM stub, so the behaviour
// is tested rather than the presence of the source text.
const fs = require('fs');
const js = fs.readFileSync(process.argv[2], 'utf8');
const records = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function mkInput() { return { value: '', dataset: {}, addEventListener() {} }; }
// document.activeElement: which box the cursor is in. The lookup must never
// write into it.
let activeEl = null;
const fields = {
  company: mkInput(), position: mkInput(),
  birth_decade: mkInput(), gender: mkInput(),
};
const patient = mkInput();
const note = { textContent: '', className: '', addEventListener() {} };

global.document = {
  get activeElement() { return activeEl; },
  getElementById: id => (id === 'patient' ? patient
                       : id === 'patient_recall' ? note : null),
  querySelector: sel => {
    const m = sel.match(/name="([a-z_]+)"/);
    return m ? (fields[m[1]] || null) : null;
  },
  addEventListener() {},
};
// A lookup takes time. fetchDelay lets a test look at the form WHILE the
// answer is still on the wire, which is where the validation bug lived.
let fetchDelay = 0;
const fetchLog = [];
global.fetch = async (url) => {
  const name = decodeURIComponent(url.split('name=')[1] || '');
  fetchLog.push(name);
  if (fetchDelay) await new Promise(r => setTimeout(r, fetchDelay));
  const rec = records[name];
  return { ok: true, json: async () => (rec || {}) };
};

eval(js);

function snap() {
  const o = {};
  for (const k of Object.keys(fields)) o[k] = fields[k].value;
  o.note = note.textContent.slice(0, 40);
  return o;
}

(async () => {
  const out = {};
  patient.value = 'Thabo Mokoena'; await recallPatient(); out.first = snap();
  patient.value = 'Palesa Nkoe';   await recallPatient(); out.changed = snap();
  patient.value = 'Nobody Known';  await recallPatient(); out.unknown = snap();
  // a hand-typed value must survive
  patient.value = 'Thabo Mokoena'; await recallPatient();
  fields.company.value = 'Typed By Hand';
  delete fields.company.dataset.autofilled;      // what the input listener does
  patient.value = 'Palesa Nkoe';   await recallPatient(); out.typed = snap();

  // The details must NOT be blanked while the lookup is in flight. They are
  // required fields: emptying them made the very click that triggered the
  // lookup fail validation, so pressing Dispense did nothing at all.
  for (const k of Object.keys(fields)) { fields[k].value = ''; delete fields[k].dataset.autofilled; }
  patient.value = 'Thabo Mokoena'; await recallPatient();
  fetchDelay = 60;
  patient.value = 'Palesa Nkoe';
  const pending = recallPatient();
  out.midLookup = snap();          // mid-flight: the old answer is still there
  await pending;
  out.afterLookup = snap();        // and has been replaced by the new one
  fetchDelay = 0;

  // An answer that arrives after the name has moved on again must not paint
  // itself over the newer one.
  for (const k of Object.keys(fields)) { fields[k].value = ''; delete fields[k].dataset.autofilled; }
  fetchDelay = 80;
  patient.value = 'Thabo Mokoena';
  const slow = recallPatient();                 // started first, answers last
  await new Promise(r => setTimeout(r, 10));
  fetchDelay = 0;
  patient.value = 'Palesa Nkoe';
  await recallPatient();                        // started second, answers first
  await slow;
  out.outOfOrder = snap();

  // The cursor is in Company: the user is answering it themselves, and an
  // answer landing there mid-word produced "Letseng DiamondsMy Own Answer".
  for (const k of Object.keys(fields)) { fields[k].value = ''; delete fields[k].dataset.autofilled; }
  patient.value = 'Thabo Mokoena';
  activeEl = fields.company;
  await recallPatient();
  out.cursorInField = snap();
  activeEl = null;
  // Left empty, it still deserves its answer.
  await recallPatient();
  out.afterLeaving = snap();

  out.fetches = fetchLog.length;
  console.log(JSON.stringify(out));
})();
