// Smoke test of the popup's DOM layer with a minimal fake DOM and a stubbed fetch (no browser needed).
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

// ── minimal fake DOM ─────────────────────────────────────────────────
class Text { constructor(t) { this.text = t; } get textContent() { return this.text; } }
class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.dataset = {}; this.parent = null;
    this.className = ''; this._text = ''; this.value = ''; this.disabled = false; this.title = '';
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set textContent(t) { this._text = t; this.children = []; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  replaceChildren(...cs) { this.children = []; this._text = ''; cs.forEach((c) => this.appendChild(c)); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter((x) => x !== f); }
  dispatchEvent(ev) { ev.target = ev.target || this; (this.listeners[ev.type] || []).forEach((f) => f(ev)); return true; }
  focus() { this.focused = true; }
  closest(sel) { return sel === 'input[data-adjustments-editor]' && this.tag === 'input' && 'adjustmentsEditor' in this.dataset ? this : null; }
}
function walk(node, fn) { fn(node); (node.children || []).forEach((c) => c.children && walk(c, fn)); }
function findAll(root, pred) { const out = []; walk(root, (n) => { if (n.tag && pred(n)) out.push(n); }); return out; }

const docListeners = {};
const body = new Node('body');
globalThis.document = {
  body,
  createElement: (t) => new Node(t),
  createTextNode: (t) => new Text(t),
  addEventListener: (t, f) => { (docListeners[t] = docListeners[t] || []).push(f); },
  removeEventListener: (t, f) => { docListeners[t] = (docListeners[t] || []).filter((x) => x !== f); },
};
globalThis.Event = class { constructor(type, init) { this.type = type; this.bubbles = !!(init && init.bubbles); } };
const fire = (type, ev) => (docListeners[type] || []).slice().forEach((f) => f(ev));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── stubbed server ───────────────────────────────────────────────────
const calls = [];
const SOURCE = 'the protocol JSON file acrel/acrel_dtsd1352_c.json';
const OPTIONS = {
  accuenergy_acuvim: { variables: ['pt1', 'pt2', 'basic_parameter_mode'], context_variables: ['voltage_scale'], register_endian: ['big_endian-ABCD', 'little_endian-CDAB'], known_keys: [] },
  acrel_dtsd1352_c: {
    variables: ['pt_ratio', 'ct_ratio', 'power_total'], context_variables: ['power_scale'],
    context_variable_definitions: { power_scale: '{pt_ratio}*{ct_ratio}' }, context_variable_source: SOURCE,
    register_endian: ['big_endian-ABCD'], known_keys: [],
  },
};
globalThis.fetch = async (url, init) => {
  const reply = (json, ok = true) => ({ ok, json: async () => json });
  if (url.startsWith('/api/protocols/adjustments/options')) {
    const name = decodeURIComponent(url.split('protocol_name=')[1]);
    // what GET /{protocol_name}/{registry_type} (list_registers) would answer if it shadowed this route: a bare list
    if (name === 'shadowed_protocol') return reply([]);
    return reply(OPTIONS[name]);
  }
  const payload = JSON.parse(init.body);
  calls.push({ url, payload });
  if (url.endsWith('/validate')) {
    const bad = payload.value.includes('BAD');
    const notes = payload.value.includes('power_scale') ? ['{power_scale} is a context variable defined in ' + SOURCE + ' (under context_variables): {pt_ratio}*{ct_ratio}'] : [];
    return reply({ ok: !bad, errors: bad ? ['Context formula: not a valid arithmetic expression'] : [], warnings: [], notes, normalized: bad ? '' : payload.value.replace(/":/g, '": ').replace(/,"/g, ', "') });
  }
  return reply({ ok: true, errors: [], warnings: [], post_decode: 120, final: 4160, formula: '120.0*(34.6)', waiting_for: [] });
};

require(path.join(__dirname, '..', '..', 'classes', 'WebServer', 'static', 'js', 'adjustments-editor.js'));

function makeInput(value, protocol = 'accuenergy_acuvim') {
  const input = new Node('input');
  input.dataset = { adjustmentsEditor: '1', protocol, variable: 'voltage_l1', address: '0x4002' };
  input.value = value;
  input.changes = 0;
  input.addEventListener('change', () => { input.changes += 1; });
  return input;
}
const open = async (input) => {
  // a failed test must not leave its popup open for the next one
  const stale = body.children.find((c) => c.className.includes('adj-overlay'));
  if (stale) fire('keydown', { key: 'Escape', stopPropagation() {} });
  fire('click', { target: input, preventDefault() {} });
  await sleep(30);
};
const overlay = () => body.children.find((c) => c.className.includes('adj-overlay'));
const byText = (root, tag, text) => findAll(root, (n) => n.tag === tag && n.textContent === text)[0];
const typeInto = (node, value) => { node.value = value; node.dispatchEvent({ type: 'input', target: node }); };

test('clicking the cell opens the popup with the current adjustments as a form', async () => {
  const input = makeInput('{"Context": {"formula": "x*{voltage_scale}"}}');
  await open(input);
  assert.ok(overlay(), 'popup is attached to the page');
  const formula = findAll(overlay(), (n) => n.tag === 'input' && n.value === 'x*{voltage_scale}')[0];
  assert.ok(formula, 'the Context formula is shown in an input');
  assert.ok(byText(overlay(), 'button', 'Apply'));
  await sleep(40);
  assert.ok(calls.some((c) => c.url.endsWith('/validate')), 'the server was asked to validate on open');
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
  assert.equal(overlay(), undefined, 'Cancel closes the popup');
  assert.equal(input.changes, 0, 'Cancel does not touch the cell');
});

test('editing a formula revalidates and Apply writes the normalized value and fires change', async () => {
  const input = makeInput('{"Context": {"formula": "x"}}');
  await open(input);
  const formula = findAll(overlay(), (n) => n.tag === 'input' && n.value === 'x')[0];
  typeInto(formula, 'x*{voltage_scale}');
  await sleep(320);
  const last = calls.filter((c) => c.url.endsWith('/validate')).pop();
  assert.deepEqual(JSON.parse(last.payload.value), { Context: { formula: 'x*{voltage_scale}' } });
  assert.equal(last.payload.protocol_name, 'accuenergy_acuvim');
  const apply = byText(overlay(), 'button', 'Apply');
  assert.equal(apply.disabled, false);
  apply.dispatchEvent({ type: 'click' });
  await sleep(30);
  assert.equal(overlay(), undefined, 'popup closes after Apply');
  assert.equal(input.value, '{"Context": {"formula": "x*{voltage_scale}"}}');
  assert.equal(input.changes, 1, 'the existing hx-patch change handler is triggered exactly once');
});

test('a server error disables Apply and shows the message', async () => {
  const input = makeInput('{"Offset": 1}');
  await open(input);
  const offset = findAll(overlay(), (n) => n.tag === 'input' && n.value === '1')[0];
  typeInto(offset, 'BAD');
  await sleep(320);
  assert.equal(byText(overlay(), 'button', 'Apply').disabled, true);
  assert.ok(overlay().textContent.includes('not a valid arithmetic expression'));
  fire('keydown', { key: 'Escape', stopPropagation() {} });
  assert.equal(overlay(), undefined, 'Escape closes the popup');
  assert.equal(input.value, '{"Offset": 1}', 'a rejected edit never reaches the cell');
});

test('adding an adjustment from the menu adds a card and removes it from the menu', async () => {
  const input = makeInput('');
  await open(input);
  assert.ok(overlay().textContent.includes('No adjustments'));
  const menu = findAll(overlay(), (n) => n.tag === 'select' && n.attrs['aria-label'] === 'Add an adjustment')[0];
  menu.value = 'Register_Endian';
  menu.dispatchEvent({ type: 'change', target: menu });
  await sleep(30);
  assert.ok(overlay().textContent.includes('Word/byte order for this row only'));
  const menuAfter = findAll(overlay(), (n) => n.tag === 'select' && n.attrs['aria-label'] === 'Add an adjustment')[0];
  assert.ok(!findAll(menuAfter, (n) => n.tag === 'option' && n.attrs.value === 'Register_Endian').length, 'already-present keys are not offered again');
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});

test('clearing every adjustment applies an empty cell', async () => {
  const input = makeInput('{"Offset": 5}');
  await open(input);
  findAll(overlay(), (n) => n.tag === 'button' && n.attrs.title === 'Remove this adjustment')[0].dispatchEvent({ type: 'click' });
  await sleep(30);
  byText(overlay(), 'button', 'Apply').dispatchEvent({ type: 'click' });
  await sleep(30);
  assert.equal(input.value, '');
  assert.equal(input.changes, 1);
});

test('an unreadable existing value opens in raw mode and cannot be applied until fixed', async () => {
  const input = makeInput('{broken');
  await open(input);
  assert.ok(overlay().textContent.includes('could not be read as adjustments'));
  assert.equal(byText(overlay(), 'button', 'Apply').disabled, true);
  const raw = findAll(overlay(), (n) => n.tag === 'textarea' && n.attrs['aria-label'] === 'Raw adjustments JSON')[0];
  typeInto(raw, '{"Offset": 7}');
  byText(overlay(), 'button', 'Apply raw JSON to the form').dispatchEvent({ type: 'click' });
  await sleep(40);
  assert.equal(byText(overlay(), 'button', 'Apply').disabled, false);
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});

test('the preview posts the current adjustments and shows the result', async () => {
  const input = makeInput('{"Context": {"formula": "x*{voltage_scale}"}}');
  await open(input);
  await sleep(40);
  byText(overlay(), 'button', 'Run').dispatchEvent({ type: 'click' });
  await sleep(30);
  const call = calls.filter((c) => c.url.endsWith('/preview')).pop();
  assert.deepEqual(JSON.parse(call.payload.value), { Context: { formula: 'x*{voltage_scale}' } });
  assert.deepEqual(Object.keys(call.payload.variables), ['voltage_scale']);
  assert.ok(overlay().textContent.includes('published value:  4160'));
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});

test('a failed save from the server shows a toast on the cell', async () => {
  const input = makeInput('{"Offset": 1}');
  fire('htmx:responseError', { detail: { elt: input, xhr: { responseText: '{"detail": "Invalid adjustments: Offset must be a number"}' } } });
  assert.ok(input.title.includes('Offset must be a number'));
  assert.ok(body.children.some((c) => c.attrs.role === 'alert' && c.textContent.includes('Offset must be a number')));
});

const chipsOf = (root, cls) => findAll(root, (n) => n.tag === 'button' && n.className.split(' ').includes(cls));

test('context variables from the protocol JSON are offered as chips that name their source and definition', async () => {
  const input = makeInput('{"Context": {"formula": "x"}}', 'acrel_dtsd1352_c');
  await open(input);
  await sleep(40);
  const ctx = chipsOf(overlay(), 'adj-chip-ctx');
  assert.equal(ctx.length, 1);
  assert.equal(ctx[0].textContent, '{power_scale}');
  assert.ok(ctx[0].attrs.title.includes(SOURCE), 'the hover text says which file defines it');
  assert.ok(ctx[0].attrs.title.includes('{pt_ratio}*{ct_ratio}'), 'and what it is');
  assert.ok(overlay().textContent.includes('Context variables, defined once in ' + SOURCE), 'a legend explains where they come from');
  const plain = chipsOf(overlay(), 'adj-chip').filter((c) => !c.className.includes('adj-chip-ctx')).map((c) => c.textContent);
  assert.deepEqual(plain, ['{pt_ratio}', '{ct_ratio}', '{power_total}'], "the protocol's own variables are listed separately");
  const formula = findAll(overlay(), (n) => n.tag === 'input' && n.value === 'x')[0];
  ctx[0].dispatchEvent({ type: 'click' });
  assert.equal(formula.value, 'x{power_scale}', 'clicking a context variable inserts it into the formula');
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});

test('context variables can be picked as the variable to read, and appear in the suggestion list', async () => {
  const input = makeInput('{"Context": {"key": "pt_ratio", "cases": {"1": "x"}}}', 'acrel_dtsd1352_c');
  await open(input);
  await sleep(40);
  const options = findAll(overlay(), (n) => n.tag === 'option' && n.parent && n.parent.tag === 'datalist').map((o) => o.value);
  assert.deepEqual(options, ['power_scale', 'pt_ratio', 'ct_ratio', 'power_total'], 'context variables lead the list');
  const keyInput = findAll(overlay(), (n) => n.tag === 'input' && n.value === 'pt_ratio')[0];
  chipsOf(overlay(), 'adj-chip-ctx')[0].dispatchEvent({ type: 'click' });
  assert.equal(keyInput.value, 'power_scale');
  await sleep(320);
  const last = calls.filter((c) => c.url.endsWith('/validate')).pop();
  assert.equal(JSON.parse(last.payload.value).Context.key, 'power_scale');
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});

test('validation notes tell the user where a context variable comes from, next to "Valid."', async () => {
  const input = makeInput('{"Context": {"formula": "x*{power_scale}"}}', 'acrel_dtsd1352_c');
  await open(input);
  await sleep(60);
  const text = overlay().textContent;
  assert.ok(text.includes('Valid.'), 'a note does not make the value invalid');
  assert.ok(text.includes('{power_scale} is a context variable defined in ' + SOURCE));
  const note = findAll(overlay(), (n) => n.tag === 'p' && n.className.includes('adj-info'))[0];
  assert.ok(note, 'notes use their own style, distinct from warnings and errors');
  assert.equal(byText(overlay(), 'button', 'Apply').disabled, false);
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});

test('the preview asks for the inputs of a context variable, not the derived value', async () => {
  const input = makeInput('{"Context": {"formula": "x*{power_scale}"}}', 'acrel_dtsd1352_c');
  await open(input);
  await sleep(60);
  byText(overlay(), 'button', 'Run').dispatchEvent({ type: 'click' });
  await sleep(30);
  const call = calls.filter((c) => c.url.endsWith('/preview')).pop();
  assert.deepEqual(Object.keys(call.payload.variables), ['pt_ratio', 'ct_ratio']);
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});

test('a wrong answer from the options request is reported instead of silently offering nothing', async () => {
  const input = makeInput('{"Context": {"formula": "x*{power_scale}"}}', 'shadowed_protocol');
  await open(input);
  await sleep(60);
  const text = overlay().textContent;
  assert.ok(text.includes("Could not load this protocol's variables and context variables"), 'the user is told');
  assert.equal(chipsOf(overlay(), 'adj-chip').length, 0, 'no chips are invented');
  assert.ok(text.includes('Valid.'), 'the value itself is still checked by the server, and the notice does not hide that');
  assert.equal(byText(overlay(), 'button', 'Apply').disabled, false, 'editing still works');
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});

test('the header has a help link to the annotated screen, next to the close button, opening in a new tab', async () => {
  const input = makeInput('{"Offset": 1}');
  await open(input);
  const help = findAll(overlay(), (n) => n.tag === 'a' && n.className.includes('adj-help-btn'))[0];
  assert.ok(help, 'a help link is present');
  assert.equal(help.attrs.href, '/pages/help/screen/adjustments_editor', 'the same page Help > This Screen shows for this screen');
  assert.equal(help.attrs.target, '_blank', 'a new tab, so the popup and its unapplied edits are untouched');
  assert.ok(help.attrs.rel.includes('noopener'));
  assert.ok(help.attrs['aria-label'].toLowerCase().includes('help'), 'it has an accessible name');
  assert.equal(help.textContent, '?');
  const actions = help.parent;
  assert.ok(actions.className.includes('adj-header-actions'));
  const close = findAll(actions, (n) => n.tag === 'button' && n.attrs['aria-label'] === 'Close')[0];
  assert.ok(close, 'the close button is in the same group');
  assert.ok(actions.children.indexOf(help) < actions.children.indexOf(close), 'help sits to the left of the close button');
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});

test('clicking the help link does not close the popup or touch the cell', async () => {
  const input = makeInput('{"Offset": 1}');
  await open(input);
  const help = findAll(overlay(), (n) => n.tag === 'a' && n.className.includes('adj-help-btn'))[0];
  fire('click', { target: help, preventDefault() {} });     // the page-level click handler sees it too
  help.dispatchEvent({ type: 'click' });
  assert.ok(overlay(), 'the popup is still open');
  assert.equal(input.changes, 0);
  assert.equal(input.value, '{"Offset": 1}');
  byText(overlay(), 'button', 'Cancel').dispatchEvent({ type: 'click' });
});
