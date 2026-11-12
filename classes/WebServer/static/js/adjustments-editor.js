/*
 * Description: Guided popup editor for the "adjustments" column of the protocol editor.
 * File: adjustments-editor.js
 *
 * Copyright 2026 Kevin Burke
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://apache.org
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/*
 * How it works
 * ------------
 * Clicking (or pressing Enter in) a protocol-editor adjustments cell opens a popup.  The popup edits the
 * four adjustments MPG acts on (Register_Endian, High_Low, Offset, Context) with typed controls, and
 * allows any other key as a "custom" JSON entry, so editing is guided but not closed.  The server
 * (DataAdjustments.validate_adjustments) is the only authority on what is valid: this file never
 * re-implements the rules, it asks /api/protocols/adjustments/validate on every change and shows the
 * errors and warnings.  Apply writes the server's normalized JSON back into the cell and fires its
 * `change` event, so the existing hx-patch save path runs unchanged.
 *
 * The file has two parts: pure functions (parsing/serializing the form model, exported for tests) and
 * the DOM layer below them.
 */
(function (root) {
  'use strict';

  // ══════════════════════════════════════════════════════════════════════
  // Pure model logic
  // ══════════════════════════════════════════════════════════════════════

  const KNOWN_KEYS = ['Register_Endian', 'High_Low', 'Offset', 'Context'];
  const CONTEXT_KEYS = ['formula', 'key', 'cases', 'range', 'default'];
  const ENDIAN_CHOICES = ['big_endian-ABCD', 'big_endian_byte_swap-BADC', 'little_endian-CDAB', 'little_endian_byte_swap-DCBA'];
  const ENDIAN_HELP = {
    'big_endian-ABCD': 'High word first, bytes in order (the default).',
    'big_endian_byte_swap-BADC': 'High word first, bytes swapped inside each word.',
    'little_endian-CDAB': 'Low word first, bytes in order (common on inverters).',
    'little_endian_byte_swap-DCBA': 'Low word first, bytes swapped inside each word.',
  };
  const BLOCK_RE = /x\?[(\[]([-0-9.]+),([-0-9.]+)[)\]]->(.+?)(?=x\?|$)/g;
  const NAME_RE = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

  function isPlainObject(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }

  function canonicalKey(key) {
    const hit = KNOWN_KEYS.find((k) => k.toLowerCase() === String(key).trim().toLowerCase());
    return hit || String(key).trim();
  }

  /** Parse the cell text. Returns {data} for a JSON object / Key:value shorthand, or {error}. */
  function parseText(text) {
    const t = String(text == null ? '' : text).trim();
    if (t === '') return { data: {} };
    if (t.startsWith('{')) {
      let parsed;
      try { parsed = JSON.parse(t); } catch (e) { return { error: 'Not valid JSON: ' + e.message }; }
      if (!isPlainObject(parsed)) return { error: 'Expected a JSON object' };
      return { data: parsed };
    }
    const i = t.indexOf(':');
    if (i <= 0) return { error: 'Expected a JSON object such as {"Offset": -40} or Offset:-40' };
    const value = t.slice(i + 1).trim();
    let parsedValue;
    try { parsedValue = JSON.parse(value); } catch (e) { parsedValue = value; }
    return { data: { [t.slice(0, i).trim()]: parsedValue } };
  }

  /** "x?(0,32768]->x x?(32768,65535]->x-65536" -> [{lo, hi, formula}], or null when it is not in that shape. */
  function highLowParse(text) {
    const cleaned = String(text).replace(/ /g, '');
    const blocks = [];
    let consumed = '';
    for (const m of cleaned.matchAll(BLOCK_RE)) {
      blocks.push({ lo: m[1], hi: m[2], formula: m[3] });
      consumed += m[0];
    }
    if (!blocks.length || consumed !== cleaned) return null;
    return blocks;
  }

  function highLowBuild(blocks) {
    return blocks
      .filter((b) => b.lo !== '' || b.hi !== '' || b.formula !== '')
      .map((b) => 'x?(' + b.lo + ',' + b.hi + ']->' + b.formula)
      .join(' ');
  }

  /** Names used as {name} in a formula. */
  function formulaNames(formula) {
    const out = [];
    for (const m of String(formula).matchAll(NAME_RE)) if (!out.includes(m[1])) out.push(m[1]);
    return out;
  }

  /** Context object -> form model. Unknown sub-keys are kept in `extra` so nothing is lost. */
  function contextToForm(ctx) {
    const form = {
      mode: 'formula', formula: '', key: '', cases: [], lo: '', hi: '', rangeRaw: null, dflt: '', extra: {},
    };
    if (!isPlainObject(ctx)) { form.invalid = ctx; return form; }
    for (const k of Object.keys(ctx)) if (!CONTEXT_KEYS.includes(k)) form.extra[k] = ctx[k];
    const hasKey = typeof ctx.key === 'string' && ctx.key.trim() !== '';
    form.mode = (hasKey || 'cases' in ctx || 'range' in ctx || ('default' in ctx && !('formula' in ctx))) ? 'key' : 'formula';
    if (typeof ctx.key === 'string') form.key = ctx.key;
    if (form.mode === 'formula') {
      // MPG ignores these without a key; keep them as written instead of silently dropping them
      for (const k of ['cases', 'range', 'default']) if (k in ctx) form.extra[k] = ctx[k];
      if (typeof ctx.formula === 'string') form.formula = ctx.formula;
      return form;
    }
    if ('formula' in ctx) form.extra.formula = ctx.formula;     // ignored when a key is set
    if (isPlainObject(ctx.cases)) {
      form.cases = Object.keys(ctx.cases).map((k) => ({ value: k, formula: String(ctx.cases[k]) }));
    }
    if (Array.isArray(ctx.range) && ctx.range.length === 2) {
      form.lo = String(ctx.range[0]); form.hi = String(ctx.range[1]);
    } else if (typeof ctx.range === 'string' && ctx.range.includes('~')) {
      const parts = ctx.range.split('~'); form.lo = parts[0]; form.hi = parts.slice(1).join('~');
    } else if ('range' in ctx) {
      form.rangeRaw = ctx.range;
    }
    if (typeof ctx.default === 'string') form.dflt = ctx.default;
    return form;
  }

  /** Form model -> Context object. */
  function formToContext(form) {
    if (form.invalid !== undefined) return form.invalid;
    const out = {};
    if (form.mode === 'formula') {
      out.formula = form.formula;
    } else {
      out.key = form.key;
      const cases = form.cases.filter((c) => c.value !== '' || c.formula !== '');
      if (cases.length) {
        out.cases = {};
        for (const c of cases) out.cases[c.value] = c.formula;
      }
      if (form.rangeRaw !== null) {
        out.range = form.rangeRaw;
      } else if (form.lo !== '' || form.hi !== '') {
        const lo = Number(form.lo), hi = Number(form.hi);
        out.range = form.lo !== '' && form.hi !== '' && Number.isFinite(lo) && Number.isFinite(hi) ? [lo, hi] : form.lo + '~' + form.hi;
      }
      if (form.dflt !== '') out.default = form.dflt;
    }
    for (const k of Object.keys(form.extra)) out[k] = form.extra[k];
    return out;
  }

  /** Parsed adjustments object -> ordered list of cards (one per key). */
  function modelFromData(data) {
    const cards = [];
    for (const rawKey of Object.keys(data)) {
      const key = canonicalKey(rawKey);
      const value = data[rawKey];
      if (key === 'Register_Endian') {
        cards.push({ type: 'Register_Endian', value: typeof value === 'string' ? value : JSON.stringify(value) });
      } else if (key === 'Offset') {
        cards.push({ type: 'Offset', value: typeof value === 'string' ? value : JSON.stringify(value) });
      } else if (key === 'High_Low') {
        const blocks = typeof value === 'string' ? highLowParse(value) : null;
        cards.push({ type: 'High_Low', blocks: blocks || [], text: typeof value === 'string' ? value : JSON.stringify(value), freeText: blocks === null });
      } else if (key === 'Context') {
        cards.push({ type: 'Context', form: contextToForm(value) });
      } else {
        cards.push({ type: 'custom', key: rawKey, text: JSON.stringify(value, null, 2) });
      }
    }
    return cards;
  }

  /** Cards -> {data, errors}. `errors` are problems only the browser can see (e.g. a custom value that is not JSON). */
  function modelToData(cards) {
    const data = {};
    const errors = [];
    for (const card of cards) {
      if (card.type === 'Register_Endian') {
        data.Register_Endian = card.value;
      } else if (card.type === 'Offset') {
        const t = String(card.value).trim();
        data.Offset = t !== '' && Number.isFinite(Number(t)) ? Number(t) : card.value;
      } else if (card.type === 'High_Low') {
        data.High_Low = card.freeText ? card.text : highLowBuild(card.blocks);
      } else if (card.type === 'Context') {
        data.Context = formToContext(card.form);
      } else {
        const key = String(card.key).trim();
        if (key === '') { errors.push('A custom key has no name'); continue; }
        if (key in data) { errors.push("'" + key + "' is given twice"); continue; }
        try { data[key] = JSON.parse(card.text); } catch (e) { errors.push("Custom key '" + key + "': value is not valid JSON (" + e.message + ')'); }
      }
    }
    return { data, errors };
  }

  /** Names a context-variable definition reads: an expression string, or a key/cases/default object. */
  function definitionNames(def) {
    if (typeof def === 'string') return formulaNames(def);
    if (!isPlainObject(def)) return [];
    const out = [];
    if (typeof def.key === 'string' && def.key && !def.key.includes('{')) out.push(def.key);
    const texts = [def.formula, def.default].concat(isPlainObject(def.cases) ? Object.values(def.cases) : []);
    for (const t of texts) if (typeof t === 'string') formulaNames(t).forEach((n) => { if (!out.includes(n)) out.push(n); });
    return out;
  }

  /**
   * Variables a preview needs sample values for: every {name} in the Context formulas plus the Context key.
   * A name that is a protocol context variable (`defs`, from the protocol JSON) is replaced by the inputs
   * of its definition, because the gateway computes it from those.
   */
  function previewVariables(cards, defs) {
    const known = defs || {};
    const names = [];
    const seen = new Set();
    const add = (n) => {
      if (!n || n.includes('{') || seen.has(n)) return;
      seen.add(n);
      if (Object.prototype.hasOwnProperty.call(known, n)) definitionNames(known[n]).forEach(add);
      else names.push(n);
    };
    for (const card of cards) {
      if (card.type !== 'Context' || card.form.invalid !== undefined) continue;
      const f = card.form;
      add(f.key);
      [f.formula, f.dflt, ...f.cases.map((c) => c.formula)].forEach((text) => formulaNames(text).forEach(add));
    }
    return names;
  }

  const api = {
    KNOWN_KEYS, CONTEXT_KEYS, ENDIAN_CHOICES, parseText, canonicalKey, highLowParse, highLowBuild, formulaNames,
    contextToForm, formToContext, modelFromData, modelToData, previewVariables, definitionNames,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.MPGAdjustments = api;

  // ══════════════════════════════════════════════════════════════════════
  // DOM layer
  // ══════════════════════════════════════════════════════════════════════
  if (typeof root.document === 'undefined') return;
  const doc = root.document;
  if (root.__mpgAdjustmentsEditorLoaded) return;
  root.__mpgAdjustmentsEditorLoaded = true;

  const OPTIONS_CACHE = {};
  let active = null;   // the open editor, if any

  /** Tiny element builder: h('div', {class: 'x', onclick: fn, text: 'hi'}, child, child...) */
  function h(tag, props, ...children) {
    const el = doc.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'value') el.value = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of children.flat()) {
      if (c === undefined || c === null || c === false) continue;
      el.appendChild(typeof c === 'string' ? doc.createTextNode(c) : c);
    }
    return el;
  }

  function api_(path, body) {
    return root.fetch(path, body === undefined ? undefined : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then((r) => r.json().then((j) => ({ ok: r.ok, json: j })));
  }

  /**
   * Fetch the protocol's variables, context variables and fixed choices.  Only a well-formed answer is accepted
   * (and cached): any other response, such as a different route answering the same URL, falls back to empty
   * options with `load_error` set, so the popup says so instead of silently offering nothing.
   */
  function loadOptions(protocol) {
    if (OPTIONS_CACHE[protocol]) return Promise.resolve(OPTIONS_CACHE[protocol]);
    const fallback = { variables: [], context_variables: [], context_variable_definitions: {}, context_variable_source: '', register_endian: ENDIAN_CHOICES, known_keys: KNOWN_KEYS, load_error: true };
    return api_('/api/protocols/adjustments/options?protocol_name=' + encodeURIComponent(protocol))
      .then((r) => {
        const good = r.ok && isPlainObject(r.json) && Array.isArray(r.json.variables) && Array.isArray(r.json.context_variables);
        if (!good) return fallback;
        return (OPTIONS_CACHE[protocol] = r.json);
      })
      .catch(() => fallback);
  }

  function openEditor(input) {
    if (active) return;
    const editor = {
      input,
      protocol: input.dataset.protocol || '',
      variable: input.dataset.variable || '',
      address: input.dataset.address || '',
      options: { variables: [], context_variables: [], context_variable_definitions: {}, context_variable_source: '', register_endian: ENDIAN_CHOICES, known_keys: KNOWN_KEYS },
      cards: [],
      initialError: '',
      check: null,          // last server answer {ok, errors, warnings, normalized}
      localErrors: [],
      pending: false,
      seq: 0,
      timer: null,
      previewX: '100',
      previewValues: {},
      previewResult: null,
      rawOpen: false,
      rawDirty: false,
      els: {},
    };
    const parsed = parseText(input.value);
    if (parsed.error) {
      editor.initialError = parsed.error;
      editor.rawOpen = true;
      editor.rawText = input.value;
    } else {
      editor.cards = modelFromData(parsed.data);
    }
    active = editor;
    buildShell(editor);
    loadOptions(editor.protocol).then((options) => {
      editor.options = options;
      render(editor);
      scheduleValidate(editor, 0);
    });
    render(editor);
    scheduleValidate(editor, 0);
  }

  function closeEditor(editor) {
    if (editor.timer) clearTimeout(editor.timer);
    editor.els.overlay.remove();
    doc.removeEventListener('keydown', editor.onKey, true);
    active = null;
    if (editor.input && editor.input.focus) editor.input.focus();
  }

  // ── shell (built once per open) ─────────────────────────────────────
  function buildShell(editor) {
    const els = editor.els;
    els.cards = h('div', { class: 'adj-cards' });
    els.addRow = h('div', { class: 'adj-add' });
    els.messages = h('div', { class: 'adj-messages', role: 'status', 'aria-live': 'polite' });
    els.raw = h('div', { class: 'adj-raw' });
    els.preview = h('div', { class: 'adj-preview' });
    els.apply = h('button', { class: 'btn-primary text-xs px-4 py-1.5', type: 'button', text: 'Apply', onclick: () => apply(editor) });
    els.panel = h('div', { class: 'modal-panel-dark adj-panel', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Edit adjustments' },
      h('div', { class: 'modal-header-dark' },
        h('div', {},
          h('h2', { class: 'adj-title', text: 'Edit adjustments' }),
          h('p', { class: 'adj-sub' }, h('span', { class: 'adj-mono', text: editor.variable || 'row' }), editor.address ? '  at ' + editor.address : '')),
        h('button', { class: 'modal-close-btn-dark', type: 'button', 'aria-label': 'Close', text: '\u00d7', onclick: () => closeEditor(editor) })),
      h('div', { class: 'adj-body' },
        h('p', { class: 'adj-help', text: 'Adjustments change how a register value is decoded or scaled before it is published. Pick the adjustments this row needs; MPG ignores anything it does not recognize.' }),
        els.cards, els.addRow, els.messages, els.preview, els.raw),
      h('div', { class: 'adj-footer' },
        h('button', { class: 'btn-secondary text-xs px-3 py-1.5', type: 'button', text: 'Cancel', onclick: () => closeEditor(editor) }),
        els.apply));
    els.overlay = h('div', { class: 'modal-overlay-dark px-4 adj-overlay' },
      h('div', { class: 'absolute inset-0 bg-black/60', 'aria-hidden': 'true', onclick: () => closeEditor(editor) }),
      els.panel);
    editor.onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); closeEditor(editor); } };
    doc.addEventListener('keydown', editor.onKey, true);
    doc.body.appendChild(els.overlay);
  }

  // ── rendering ───────────────────────────────────────────────────────
  function changed(editor) { scheduleValidate(editor, 250); }

  function field(label, control, help) {
    return h('label', { class: 'adj-field' }, h('span', { class: 'adj-label', text: label }), control, help ? h('span', { class: 'adj-hint', text: help }) : null);
  }

  function textInput(value, onInput, attrs) {
    return h('input', Object.assign({ class: 'chrome-input adj-input adj-mono', type: 'text', value: value, spellcheck: 'false', oninput: (e) => onInput(e.target.value) }, attrs || {}));
  }

  function removeButton(label, onClick) {
    return h('button', { class: 'adj-x', type: 'button', title: label, 'aria-label': label, text: '\u00d7', onclick: onClick });
  }

  function render(editor) {
    const els = editor.els;
    els.cards.replaceChildren(...editor.cards.map((card, index) => renderCard(editor, card, index)));
    if (!editor.cards.length) {
      els.cards.appendChild(h('p', { class: 'adj-empty', text: 'No adjustments: the register is published exactly as decoded. Add one below if it needs one.' }));
    }
    // add-menu: recognized keys not already present, plus a custom key
    const present = new Set(editor.cards.map((c) => c.type));
    const select = h('select', { class: 'chrome-input adj-input', 'aria-label': 'Add an adjustment', onchange: (e) => {
      const kind = e.target.value;
      e.target.value = '';
      if (kind) addCard(editor, kind);
    } }, h('option', { value: '', text: '+ Add adjustment\u2026' }),
      ...KNOWN_KEYS.filter((k) => !present.has(k)).map((k) => h('option', { value: k, text: k })),
      h('option', { value: 'custom', text: 'Custom key\u2026' }));
    els.addRow.replaceChildren(select);
    renderMessages(editor);
    renderPreview(editor);
    renderRaw(editor);
    els.apply.disabled = !canApply(editor);
  }

  function addCard(editor, kind) {
    const opts = editor.options;
    if (kind === 'Register_Endian') editor.cards.push({ type: kind, value: (opts.register_endian || ENDIAN_CHOICES)[0] });
    else if (kind === 'Offset') editor.cards.push({ type: kind, value: '0' });
    else if (kind === 'High_Low') editor.cards.push({ type: kind, blocks: [{ lo: '0', hi: '32768', formula: 'x' }, { lo: '32768', hi: '65535', formula: 'x-65536' }], text: '', freeText: false });
    else if (kind === 'Context') editor.cards.push({ type: kind, form: contextToForm({ formula: 'x' }) });
    else editor.cards.push({ type: 'custom', key: '', text: '""' });
    render(editor);
    scheduleValidate(editor, 0);
  }

  function renderCard(editor, card, index) {
    const drop = removeButton('Remove this adjustment', () => { editor.cards.splice(index, 1); render(editor); scheduleValidate(editor, 0); });
    const head = (title, blurb) => h('div', { class: 'adj-card-head' }, h('div', {}, h('strong', { class: 'adj-mono', text: title }), h('p', { class: 'adj-sub', text: blurb })), drop);
    if (card.type === 'Register_Endian') {
      const choices = (editor.options.register_endian || ENDIAN_CHOICES).slice();
      if (!choices.includes(card.value)) choices.push(card.value);
      const select = h('select', { class: 'chrome-input adj-input adj-mono', onchange: (e) => { card.value = e.target.value; render(editor); changed(editor); } },
        ...choices.map((c) => h('option', { value: c, text: c + (ENDIAN_CHOICES.includes(c) ? '' : '  (alias)'), selected: c === card.value })));
      select.value = card.value;
      return h('section', { class: 'adj-card' }, head('Register_Endian', 'Word/byte order for this row only (multi-register values).'),
        select, h('p', { class: 'adj-hint', text: ENDIAN_HELP[card.value] || 'An accepted alias of one of the four standard orders.' }));
    }
    if (card.type === 'Offset') {
      return h('section', { class: 'adj-card' }, head('Offset', 'A constant added after unit scaling (for example -40 for a temperature stored with a +40 bias).'),
        field('Offset', textInput(card.value, (v) => { card.value = v; changed(editor); }, { inputmode: 'decimal', placeholder: '-40' })));
    }
    if (card.type === 'High_Low') return renderHighLow(editor, card, head);
    if (card.type === 'Context') return renderContext(editor, card, head);
    return h('section', { class: 'adj-card adj-card-custom' }, head('Custom key', 'Not used by MPG today. Kept in the file as written.'),
      field('Key', textInput(card.key, (v) => { card.key = v; changed(editor); }, { placeholder: 'key_name' })),
      field('Value (JSON)', h('textarea', { class: 'chrome-input adj-input adj-mono', rows: '3', spellcheck: 'false', oninput: (e) => { card.text = e.target.value; changed(editor); } }, card.text)));
  }

  function renderHighLow(editor, card, head) {
    if (card.freeText) {
      return h('section', { class: 'adj-card' }, head('High_Low', 'Value ranges that decode differently.'),
        h('p', { class: 'adj-hint', text: 'This value is not in the x?(lower,upper]->formula form, so it is shown as text.' }),
        textInput(card.text, (v) => { card.text = v; changed(editor); }));
    }
    const rows = card.blocks.map((b, i) => h('div', { class: 'adj-row adj-row-hl' },
      textInput(b.lo, (v) => { b.lo = v; changed(editor); }, { 'aria-label': 'Above (exclusive)', placeholder: '0' }),
      textInput(b.hi, (v) => { b.hi = v; changed(editor); }, { 'aria-label': 'Up to (inclusive)', placeholder: '32768' }),
      textInput(b.formula, (v) => { b.formula = v; changed(editor); }, { 'aria-label': 'Formula', placeholder: 'x-65536' }),
      removeButton('Remove this range', () => { card.blocks.splice(i, 1); render(editor); scheduleValidate(editor, 0); })));
    return h('section', { class: 'adj-card' }, head('High_Low', 'A different formula for each range of the decoded value x. It replaces unit scaling and Offset.'),
      h('div', { class: 'adj-row adj-row-hl adj-colhead' }, h('span', { text: 'x above' }), h('span', { text: 'up to and including' }), h('span', { text: 'result =' }), h('span')),
      ...rows,
      h('div', { class: 'adj-actions' },
        h('button', { class: 'btn-secondary text-xs px-2 py-1', type: 'button', text: '+ Add range', onclick: () => { card.blocks.push({ lo: '', hi: '', formula: 'x' }); render(editor); } }),
        h('button', { class: 'btn-secondary text-xs px-2 py-1', type: 'button', title: 'Replace the ranges with the 16-bit two\u2019s complement pair', text: 'Signed 16-bit', onclick: () => { card.blocks = [{ lo: '0', hi: '32768', formula: 'x' }, { lo: '32768', hi: '65535', formula: 'x-65536' }]; render(editor); scheduleValidate(editor, 0); } })));
  }

  function renderContext(editor, card, head) {
    const f = card.form;
    const ctxDefs = editor.options.context_variable_definitions || {};
    const ctxNames = Object.keys(ctxDefs);
    const source = editor.options.context_variable_source || "the protocol's JSON file";
    const variables = (editor.options.variables || []).filter((v) => !ctxNames.includes(v));
    const allNames = ctxNames.concat(variables);
    if (f.invalid !== undefined) {
      return h('section', { class: 'adj-card' }, head('Context', 'Depends on another register.'),
        h('p', { class: 'adj-hint', text: 'This Context value is not an object. Remove it, or fix it in the raw JSON below.' }));
    }
    const listId = 'adj-vars-' + Math.random().toString(36).slice(2, 8);
    const datalist = h('datalist', { id: listId }, ...allNames.map((v) => h('option', { value: v })));
    const mode = h('div', { class: 'adj-modes', role: 'radiogroup', 'aria-label': 'Context mode' },
      ['formula', 'key'].map((m) => h('label', { class: 'adj-mode' },
        h('input', { type: 'radio', name: 'adj-mode-' + listId, checked: f.mode === m, onchange: () => { f.mode = m; render(editor); changed(editor); } }),
        m === 'formula' ? ' Always apply a formula' : ' Pick the formula from another variable')));
    const defText = (n) => (typeof ctxDefs[n] === 'string' ? ctxDefs[n] : JSON.stringify(ctxDefs[n]));
    // Context variables first (few, and the user may not know the JSON file defines them), then the protocol's own variables.
    const chips = (pick, braces) => h('div', { class: 'adj-varbox' },
      ctxNames.length ? h('div', {},
        h('p', { class: 'adj-hint', text: 'Context variables, defined once in ' + source + ' (under "context_variables"). Click one to use it:' }),
        h('div', { class: 'adj-chips' }, ...ctxNames.map((n) => h('button', { class: 'adj-chip adj-chip-ctx', type: 'button', title: 'Context variable from ' + source + ': ' + defText(n), text: braces ? '{' + n + '}' : n, onclick: () => pick(n) })))) : null,
      variables.length ? h('div', {},
        h('p', { class: 'adj-hint', text: 'Variables in this protocol:' }),
        h('div', { class: 'adj-chips' }, ...variables.slice(0, 30).map((v) => h('button', { class: 'adj-chip', type: 'button', title: 'Variable of this protocol', text: braces ? '{' + v + '}' : v, onclick: () => pick(v) })))) : null);
    let body;
    if (f.mode === 'formula') {
      const input = textInput(f.formula, (v) => { f.formula = v; changed(editor); }, { placeholder: 'x*{pt1}/{pt2}' });
      body = [field('Formula', input, 'x is this row\u2019s value. Use {name} for another variable. Operators: + - * / // % ** and ( ).'),
        allNames.length ? chips((n) => { input.value += '{' + n + '}'; f.formula = input.value; input.focus(); changed(editor); }, true) : null];
    } else {
      const keyInput = textInput(f.key, (v) => { f.key = v; changed(editor); }, { placeholder: 'variable_name', list: listId });
      const caseRows = f.cases.map((c, i) => h('div', { class: 'adj-row adj-row-case' },
        textInput(c.value, (v) => { c.value = v; changed(editor); }, { 'aria-label': 'When the variable is', placeholder: '0   or   1,2,3' }),
        textInput(c.formula, (v) => { c.formula = v; changed(editor); }, { 'aria-label': 'Formula', placeholder: '-x' }),
        removeButton('Remove this case', () => { f.cases.splice(i, 1); render(editor); scheduleValidate(editor, 0); })));
      body = [field('Variable to read', keyInput, 'Its value decides which formula applies.'),
        h('div', { class: 'adj-subhead', text: 'Cases' }),
        f.cases.length ? h('div', { class: 'adj-row adj-row-case adj-colhead' }, h('span', { text: 'when it equals (comma = any of)' }), h('span', { text: 'formula' }), h('span')) : null,
        ...caseRows,
        h('button', { class: 'btn-secondary text-xs px-2 py-1', type: 'button', text: '+ Add case', onclick: () => { f.cases.push({ value: '', formula: '' }); render(editor); } }),
        h('div', { class: 'adj-subhead', text: 'Scale-factor range (optional)' }),
        h('div', { class: 'adj-row adj-row-range' },
          textInput(f.lo, (v) => { f.lo = v; f.rangeRaw = null; changed(editor); }, { 'aria-label': 'Range from', placeholder: '-10' }),
          textInput(f.hi, (v) => { f.hi = v; f.rangeRaw = null; changed(editor); }, { 'aria-label': 'Range to', placeholder: '10' })),
        h('p', { class: 'adj-hint', text: 'For a whole-number variable in this range the value is multiplied by 10 to the power of that number (the SunSpec scale-factor pattern).' }),
        field('Default formula (optional)', textInput(f.dflt, (v) => { f.dflt = v; changed(editor); }, { placeholder: 'x' }), 'Used when no case matches.'),
        allNames.length ? chips((n) => { keyInput.value = n; f.key = n; changed(editor); }, false) : null];
    }
    return h('section', { class: 'adj-card' }, head('Context', 'A scale or sign that depends on another register, mode or ratio.'), mode, datalist, ...body,
      Object.keys(f.extra).length ? h('p', { class: 'adj-hint', text: 'Also kept as written: ' + Object.keys(f.extra).join(', ') }) : null);
  }

  // ── messages, validation ────────────────────────────────────────────
  function scheduleValidate(editor, delay) {
    if (editor.timer) clearTimeout(editor.timer);
    editor.pending = true;
    renderMessages(editor);
    editor.timer = setTimeout(() => runValidate(editor), delay);
  }

  function currentData(editor) {
    if (editor.initialError && !editor.cards.length && editor.rawOpen) return null;
    return modelToData(editor.cards);
  }

  function runValidate(editor) {
    const built = currentData(editor);
    if (!built) { editor.pending = false; render(editor); return Promise.resolve(); }
    editor.localErrors = built.errors;
    const seq = ++editor.seq;
    const text = Object.keys(built.data).length ? JSON.stringify(built.data) : '';
    return api_('/api/protocols/adjustments/validate', { value: text, protocol_name: editor.protocol })
      .then((r) => {
        if (seq !== editor.seq || active !== editor) return;
        editor.check = r.ok ? r.json : { ok: false, errors: ['The server could not check this value: ' + (r.json && r.json.detail ? r.json.detail : 'unknown error')], warnings: [], normalized: '' };
      })
      .catch(() => {
        if (seq !== editor.seq || active !== editor) return;
        editor.check = { ok: false, errors: ['Could not reach the server to check this value.'], warnings: [], normalized: '' };
      })
      .then(() => {
        if (seq !== editor.seq || active !== editor) return;
        editor.pending = false;
        renderMessages(editor);
        editor.els.apply.disabled = !canApply(editor);
        renderRaw(editor);
        if (previewVariables(editor.cards, editor.options.context_variable_definitions).join('|') !== editor.previewNames) renderPreview(editor);   // a {name} was added or removed
      });
  }

  function canApply(editor) {
    return !editor.pending && !(editor.rawDirty && editor.rawOpen) && !editor.initialError
      && !!editor.check && editor.check.ok && !editor.localErrors.length;
  }

  function renderMessages(editor) {
    const els = editor.els;
    const items = [];
    const check = editor.check;
    if (editor.options.load_error) items.push(h('p', { class: 'adj-msg adj-warn', text: "Could not load this protocol's variables and context variables from the server, so none are suggested here. The checks below still run on the server." }));
    if (editor.initialError) items.push(h('p', { class: 'adj-msg adj-err', text: 'The current value could not be read as adjustments (' + editor.initialError + '). Fix it in the raw JSON below, or clear it.' }));
    for (const e of editor.localErrors) items.push(h('p', { class: 'adj-msg adj-err', text: e }));
    if (check) for (const e of check.errors) items.push(h('p', { class: 'adj-msg adj-err', text: e }));
    if (check) for (const w of check.warnings) items.push(h('p', { class: 'adj-msg adj-warn', text: w }));
    if (editor.pending) items.push(h('p', { class: 'adj-msg adj-muted', text: 'Checking\u2026' }));
    else if (check && check.ok && !items.some((i) => i.className.includes('adj-err') || (i.className.includes('adj-warn') && !i.textContent.startsWith('Could not load')))) items.push(h('p', { class: 'adj-msg adj-ok', text: 'Valid.' }));
    // Notes are information, not problems: e.g. which JSON file defines a context variable the formulas use.
    if (check && check.notes) for (const n of check.notes) items.push(h('p', { class: 'adj-msg adj-info', text: n }));
    els.messages.replaceChildren(...items);
  }

  // ── preview ─────────────────────────────────────────────────────────
  function renderPreview(editor) {
    const names = previewVariables(editor.cards, editor.options.context_variable_definitions);
    editor.previewNames = names.join('|');
    const hasContext = editor.cards.some((c) => c.type === 'Context');
    const varRows = names.map((n) => h('label', { class: 'adj-field adj-field-inline' }, h('span', { class: 'adj-label adj-mono', text: n }),
      textInput(editor.previewValues[n] === undefined ? '1' : editor.previewValues[n], (v) => { editor.previewValues[n] = v; }, { inputmode: 'decimal' })));
    const out = editor.previewResult;
    const lines = [];
    if (out) {
      if (!out.ok) out.errors.forEach((e) => lines.push(h('p', { class: 'adj-msg adj-err', text: e })));
      else {
        lines.push(h('p', { class: 'adj-mono', text: 'after unit scaling / Offset / High_Low:  ' + out.post_decode }));
        if (hasContext) {
          lines.push(h('p', { class: 'adj-mono', text: 'Context formula:  ' + (out.formula || '(none selected)') }));
          if (out.waiting_for && out.waiting_for.length) lines.push(h('p', { class: 'adj-msg adj-warn', text: 'Needs a value for ' + out.waiting_for.join(', ') + '; the gateway would publish the value above unchanged.' }));
        }
        lines.push(h('p', { class: 'adj-mono adj-result', text: 'published value:  ' + out.final }));
      }
    }
    editor.els.preview.replaceChildren(h('details', { open: editor.previewOpen ? '' : null, ontoggle: (e) => { editor.previewOpen = e.target.open; } },
      h('summary', { text: 'Try it on a sample value' }),
      h('div', { class: 'adj-row adj-row-preview' },
        h('label', { class: 'adj-field adj-field-inline' }, h('span', { class: 'adj-label adj-mono', text: 'x' }), textInput(editor.previewX, (v) => { editor.previewX = v; }, { inputmode: 'decimal' })),
        h('button', { class: 'btn-secondary text-xs px-3 py-1.5', type: 'button', text: 'Run', onclick: () => runPreview(editor) })),
      ...varRows, ...lines));
  }

  function runPreview(editor) {
    const built = modelToData(editor.cards);
    const variables = {};
    for (const n of previewVariables(editor.cards, editor.options.context_variable_definitions)) {
      const v = editor.previewValues[n] === undefined ? 1 : Number(editor.previewValues[n]);
      variables[n] = Number.isFinite(v) ? v : 1;
    }
    const x = Number(editor.previewX);
    return api_('/api/protocols/adjustments/preview', {
      value: Object.keys(built.data).length ? JSON.stringify(built.data) : '', x: Number.isFinite(x) ? x : 1, variables, protocol_name: editor.protocol,
    }).then((r) => {
      editor.previewResult = r.ok ? r.json : { ok: false, errors: ['Preview failed: ' + (r.json && r.json.detail ? r.json.detail : 'unknown error')] };
      editor.previewOpen = true;
      renderPreview(editor);
    }).catch(() => { editor.previewResult = { ok: false, errors: ['Could not reach the server.'] }; renderPreview(editor); });
  }

  // ── raw JSON ────────────────────────────────────────────────────────
  function renderRaw(editor) {
    if (editor.rawDirty && editor.rawOpen && editor.els.rawArea) return;   // never overwrite text the user is typing
    if (editor.rawOpen && !editor.rawDirty && !editor.initialError) editor.rawText = JSON.stringify(modelToData(editor.cards).data, null, 2);
    const toggle = h('button', { class: 'btn-secondary text-xs px-3 py-1.5', type: 'button', text: editor.rawOpen ? 'Hide raw JSON' : 'Show raw JSON', onclick: () => {
      editor.rawOpen = !editor.rawOpen;
      if (editor.rawOpen) { const b = modelToData(editor.cards); editor.rawText = JSON.stringify(b.data, null, 2); editor.rawDirty = false; }
      editor.els.rawArea = null;
      renderRaw(editor);
    } });
    const parts = [toggle];
    if (editor.rawOpen) {
      const area = h('textarea', { class: 'chrome-input adj-input adj-mono adj-rawarea', rows: '8', spellcheck: 'false', 'aria-label': 'Raw adjustments JSON',
        oninput: (e) => { editor.rawText = e.target.value; editor.rawDirty = true; editor.els.apply.disabled = true; } }, editor.rawText);
      editor.els.rawArea = area;
      parts.push(area, h('div', { class: 'adj-actions' },
        h('button', { class: 'btn-primary text-xs px-3 py-1.5', type: 'button', text: 'Apply raw JSON to the form', onclick: () => applyRaw(editor) }),
        h('span', { class: 'adj-hint', text: 'Free-form: unknown keys are kept as custom entries and checked like everything else.' })));
    }
    editor.els.raw.replaceChildren(...parts);
  }

  function applyRaw(editor) {
    const parsed = parseText(editor.rawText || '');
    if (parsed.error) {
      editor.initialError = '';
      editor.localErrors = ['Raw JSON: ' + parsed.error];
      renderMessages(editor);
      return false;
    }
    editor.cards = modelFromData(parsed.data);
    editor.initialError = '';
    editor.rawDirty = false;
    editor.els.rawArea = null;
    editor.rawText = undefined;
    render(editor);
    scheduleValidate(editor, 0);
    return true;
  }

  // ── apply ───────────────────────────────────────────────────────────
  function apply(editor) {
    if (editor.rawDirty && editor.rawOpen && !applyRaw(editor)) return Promise.resolve();
    const built = modelToData(editor.cards);
    editor.localErrors = built.errors;
    if (built.errors.length) { renderMessages(editor); return Promise.resolve(); }
    const text = Object.keys(built.data).length ? JSON.stringify(built.data) : '';
    return api_('/api/protocols/adjustments/validate', { value: text, protocol_name: editor.protocol }).then((r) => {
      if (!r.ok || !r.json.ok) {
        editor.check = r.ok ? r.json : { ok: false, errors: ['The server rejected this value.'], warnings: [], normalized: '' };
        renderMessages(editor);
        editor.els.apply.disabled = true;
        return;
      }
      const input = editor.input;
      const before = input.value;
      input.value = r.json.normalized;
      input.title = r.json.normalized;
      closeEditor(editor);
      if (before !== r.json.normalized) input.dispatchEvent(new root.Event('change', { bubbles: true }));
    });
  }

  // ── wiring: delegated, so HTMX table swaps never break it ───────────
  function trigger(el) { return el && el.closest ? el.closest('input[data-adjustments-editor]') : null; }
  doc.addEventListener('click', (e) => { const input = trigger(e.target); if (input) { e.preventDefault(); openEditor(input); } });
  doc.addEventListener('keydown', (e) => {
    if (active) return;
    const input = trigger(e.target);
    if (input && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openEditor(input); }
  });
  doc.addEventListener('htmx:responseError', (e) => {
    const input = trigger(e.detail && e.detail.elt);
    if (!input) return;
    let detail = 'The adjustments could not be saved.';
    try { const j = JSON.parse(e.detail.xhr.responseText); if (j.detail) detail = String(j.detail); } catch (err) { /* keep default */ }
    input.title = detail;
    const note = h('div', { class: 'adj-toast', role: 'alert', text: detail });
    doc.body.appendChild(note);
    setTimeout(() => note.remove(), 8000);
  });

  api.openEditor = openEditor;   // exposed for tests and for other pages that want the popup
})(typeof window !== 'undefined' ? window : globalThis);
