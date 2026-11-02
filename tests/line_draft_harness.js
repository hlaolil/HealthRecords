// Runs the delivery page's own draft code against a DOM stub, so the
// behaviour is tested rather than the presence of the source text.
//
// Three loads of the same page share one sessionStorage, which is the whole
// point: a draft has to survive the page going away and coming back.
const fs = require('fs');
// Two renderings of the same page's script: the ordinary one, and the one the
// server sends back right after a line has posted (line_added=1). Which of the
// two is running is the only thing that tells the page a line went through.
const js = fs.readFileSync(process.argv[2], 'utf8');
const jsPosted = fs.readFileSync(process.argv[3], 'utf8');

const store = {};   // the one sessionStorage, shared across all three loads
global.sessionStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};

const FIELDS = ['med_name', 'quantity', 'pack_size', 'pack_price',
                'batch', 'expiry_date'];

function loadPage(bodyText) {
  const inputs = {};
  const listeners = [];
  const form = {
    querySelector(sel) {
      const m = sel.match(/name="([a-z_]+)"/);
      return m ? (inputs[m[1]] || null) : null;
    },
    addEventListener(evt, fn) { listeners.push([evt, fn]); },
  };
  FIELDS.forEach(f => {
    inputs[f] = { value: '', dataset: {}, name: f,
                  closest: () => form, addEventListener() {},
                  style: {} };
  });
  const note = { textContent: '', className: '' };
  const datalist = { options: [], appendChild() {} };

  let ready = null;
  global.document = {
    body: { textContent: bodyText },
    getElementById: id => (id === 'med_name' ? inputs.med_name
                         : id === 'med_suggestions' ? datalist
                         : id === 'line-draft-note' ? note : null),
    querySelector(sel) { return form.querySelector(sel); },
    querySelectorAll(sel) {
      const el = form.querySelector(sel);
      return el ? [el] : [];
    },
    createElement: () => ({ style: {}, set value(v) {} }),
    addEventListener(evt, fn) { if (evt === 'DOMContentLoaded') ready = fn; },
  };
  global.getMedicationOptions = async () => [];
  global.fetch = async () => ({ ok: true, json: async () => ({}) });

  return { inputs, note, listeners, run: async () => { if (ready) await ready(); } };
}

function snap(p) {
  const o = {};
  FIELDS.forEach(f => { o[f] = p.inputs[f].value; });
  o.note = p.note.textContent;
  o.stored = store['delivery-line-draft'] || null;
  return o;
}

(async () => {
  const out = {};

  // 1. A half-typed line, then the page goes away (session timeout, a stray
  //    reload, a fat-fingered Back).
  let p = loadPage('Receive Delivery');
  eval(js);
  await p.run();
  out.fresh = snap(p);
  p.inputs.med_name.value = 'Amoxicillin, 250 mg';
  p.inputs.quantity.value = '400';
  p.inputs.batch.value = 'AX77';
  p.listeners.filter(l => l[0] === 'input').forEach(l => l[1]());
  out.afterTyping = snap(p);

  // 2. The page comes back.
  p = loadPage('Receive Delivery');
  eval(js);
  await p.run();
  out.restored = snap(p);

  // 3. The line actually posts. The server says so in the redirect — not in
  //    the page prose, which the script's own source text would match.
  p = loadPage('Receive Delivery');
  eval(jsPosted);
  await p.run();
  out.afterPost = snap(p);

  // 4. A page loaded with nothing but the pre-filled pack size must not
  //    leave a draft behind that would resurrect itself later.
  p = loadPage('Receive Delivery');
  eval(js);
  await p.run();
  p.inputs.pack_size.value = '100';
  p.listeners.filter(l => l[0] === 'input').forEach(l => l[1]());
  out.packSizeOnly = snap(p);

  console.log(JSON.stringify(out));
})();
