// Pure-logic tests for the adjustments popup (static/js/adjustments-editor.js).
// Run with: node --test pytests/js   (pytests/test_adjustments_editor_js.py does this when node is installed)
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const lib = require(path.join(__dirname, '..', '..', 'classes', 'WebServer', 'static', 'js', 'adjustments-editor.js'));

// Round trip: text -> cards -> data must give back the same object.
function roundTrip(obj) {
  const cards = lib.modelFromData(obj);
  const out = lib.modelToData(cards);
  assert.deepEqual(out.errors, []);
  return out.data;
}

test('parseText: empty, JSON object, shorthand and errors', () => {
  assert.deepEqual(lib.parseText(''), { data: {} });
  assert.deepEqual(lib.parseText('   '), { data: {} });
  assert.deepEqual(lib.parseText('{"Offset": -40}'), { data: { Offset: -40 } });
  assert.deepEqual(lib.parseText('Offset:-40'), { data: { Offset: -40 } });
  assert.deepEqual(lib.parseText('Register_Endian:little'), { data: { Register_Endian: 'little' } });
  assert.ok(lib.parseText('{oops').error);
  assert.ok(lib.parseText('[1]').error);                         // not an object
  assert.ok(lib.parseText('nonsense').error);
});

test('highLowParse / highLowBuild round trip', () => {
  const blocks = lib.highLowParse('x?(0,32768]->x x?(32768,65535)->x-65536');
  assert.deepEqual(blocks, [{ lo: '0', hi: '32768', formula: 'x' }, { lo: '32768', hi: '65535', formula: 'x-65536' }]);
  assert.equal(lib.highLowBuild(blocks), 'x?(0,32768]->x x?(32768,65535]->x-65536');
  assert.equal(lib.highLowParse('garbage'), null);
  assert.equal(lib.highLowParse('x?(0,10]->x garbage x?'), null);   // text that is not a block
});

test('a High_Low value that is not block-shaped is kept as free text', () => {
  const data = roundTrip({ High_Low: 'something else' });
  assert.deepEqual(data, { High_Low: 'something else' });
});

test('formulaNames finds each placeholder once', () => {
  assert.deepEqual(lib.formulaNames('x*{pt1}/{pt2}+{pt1}'), ['pt1', 'pt2']);
  assert.deepEqual(lib.formulaNames('x*2'), []);
});

test('Register_Endian and Offset round trip; numeric offsets become numbers', () => {
  assert.deepEqual(roundTrip({ Register_Endian: 'little_endian-CDAB' }), { Register_Endian: 'little_endian-CDAB' });
  assert.deepEqual(roundTrip({ Offset: -40 }), { Offset: -40 });
  assert.deepEqual(roundTrip({ Offset: '12' }), { Offset: 12 });
  const bad = lib.modelToData([{ type: 'Offset', value: 'abc' }]);
  assert.equal(bad.data.Offset, 'abc');      // left for the server to reject
});

test('Context formula mode round trips', () => {
  assert.deepEqual(roundTrip({ Context: { formula: 'x*{pt1}/{pt2}' } }), { Context: { formula: 'x*{pt1}/{pt2}' } });
});

test('Context key mode keeps cases, range and default', () => {
  const ctx = { key: 'ct2', cases: { '333,80': 'x*{ct1}' }, range: [-3, 3], default: 'x*{ct1}/{ct2}' };
  assert.deepEqual(roundTrip({ Context: ctx }), { Context: ctx });
  const sunspec = { key: 'meter_{i}_ac_voltage_sf', range: [-10, 10] };
  assert.deepEqual(roundTrip({ Context: sunspec }), { Context: sunspec });
  assert.deepEqual(roundTrip({ Context: { key: 'sf', range: '-3~3' } }), { Context: { key: 'sf', range: [-3, 3] } });
});

test('Context keeps sub-keys MPG ignores instead of dropping them', () => {
  const ctx = { formula: 'x', default: 'x*2', color: 'red' };
  assert.deepEqual(roundTrip({ Context: ctx }), { Context: { formula: 'x', default: 'x*2', color: 'red' } });
  const keyed = { key: 'mode', cases: { 0: 'x' }, formula: 'x*9' };
  const out = roundTrip({ Context: keyed });
  assert.equal(out.Context.formula, 'x*9');
  assert.deepEqual(out.Context.cases, { 0: 'x' });
});

test('an invalid range is passed through for the server to report', () => {
  const form = lib.contextToForm({ key: 'sf', range: 'nonsense' });
  assert.equal(lib.formToContext(form).range, 'nonsense');
  const half = { mode: 'key', key: 'sf', formula: '', cases: [], lo: '5', hi: '', rangeRaw: null, dflt: '', extra: {} };
  assert.equal(lib.formToContext(half).range, '5~');
});

test('unknown keys become custom entries; key names are canonicalised', () => {
  assert.deepEqual(roundTrip({ Clamp: [0, 100] }), { Clamp: [0, 100] });
  assert.deepEqual(roundTrip({ register_endian: 'big' }), { Register_Endian: 'big' });
  assert.equal(lib.canonicalKey(' offset '), 'Offset');
});

test('custom entries: bad JSON, missing and duplicate keys are reported locally', () => {
  const r = lib.modelToData([
    { type: 'custom', key: 'a', text: '{bad' },
    { type: 'custom', key: '', text: '1' },
    { type: 'custom', key: 'b', text: '1' },
    { type: 'custom', key: 'b', text: '2' },
  ]);
  assert.equal(r.errors.length, 3);
  assert.deepEqual(r.data, { b: 1 });
});

test('order of keys is preserved', () => {
  const data = roundTrip({ Context: { formula: 'x' }, Offset: 1, Register_Endian: 'big' });
  assert.deepEqual(Object.keys(data), ['Context', 'Offset', 'Register_Endian']);
});

test('previewVariables lists the key and every {name}, once, skipping template placeholders', () => {
  const cards = lib.modelFromData({
    Context: { key: 'ct2', cases: { '5': 'x*{ct1}/{ct2}' }, default: 'x*{pt1}' },
  });
  assert.deepEqual(lib.previewVariables(cards).sort(), ['ct1', 'ct2', 'pt1']);
  const template = lib.modelFromData({ Context: { key: 'meter_{i}_sf', range: [-1, 1] } });
  assert.deepEqual(lib.previewVariables(template), []);
});

test('definitionNames reads expression strings and key/cases/default objects', () => {
  assert.deepEqual(lib.definitionNames('{pt_ratio}*{ct_ratio}'), ['pt_ratio', 'ct_ratio']);
  assert.deepEqual(
    lib.definitionNames({ key: 'ct2', cases: { '333,80': '{ct1}' }, default: '{ct1}/{ct2}' }).sort(),
    ['ct1', 'ct2'],
  );
  assert.deepEqual(lib.definitionNames(42), []);
});

test('previewVariables replaces a context variable by the inputs of its definition', () => {
  const cards = lib.modelFromData({ Context: { formula: 'x*{power_scale}+{offset}' } });
  const defs = { power_scale: '{pt_ratio}*{ct_ratio}' };
  assert.deepEqual(lib.previewVariables(cards, defs), ['pt_ratio', 'ct_ratio', 'offset']);
  assert.deepEqual(lib.previewVariables(cards), ['power_scale', 'offset'], 'without definitions the name itself is asked for');
});

test('previewVariables follows chained context variables and survives a cycle', () => {
  const cards = lib.modelFromData({ Context: { formula: 'x*{current_scale}' } });
  const chained = { current_scale: '{mode}+(1-{mode})*{ct_ratio_eff}', ct_ratio_eff: { key: 'ct2', cases: { '333': '{ct1}' }, default: '{ct1}/{ct2}' } };
  assert.deepEqual(lib.previewVariables(cards, chained).sort(), ['ct1', 'ct2', 'mode']);
  const loop = { a: '{b}', b: '{a}' };
  assert.deepEqual(lib.previewVariables(lib.modelFromData({ Context: { formula: 'x*{a}' } }), loop), []);
});
