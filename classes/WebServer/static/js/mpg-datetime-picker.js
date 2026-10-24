/*
 * mpg-datetime-picker.js -- shared Flatpickr setup for the Admin UI's date/time boxes.
 *
 * Used by: Timescale Metrics Edit, InfluxDB Metrics Edit (1.x / 3.x) and Timeshift Data (InfluxDB 1.x / 3.x / TimescaleDB).
 *
 * Display / entry format: "2026-10-17  20:05:55" -- two spaces between date and time, whole seconds, 24-hour clock.
 * Typing is first-class (allowInput); the calendar is a convenience. parseDate is custom so a typed value with one space
 * (or several), or an ISO "T" separator such as the server returns for an EG4 file's earliest timestamp, is accepted too.
 * Flatpickr's own parser would insist on exactly the two characters in the format string.
 *
 * Requires flatpickr.min.js to be loaded first. If it isn't (asset failed to load), every function still works and the
 * boxes simply behave as plain text fields.
 *
 * Public API (window.MpgDateTime):
 *   init([{id, end: true|false}, ...])  attach a picker to each input. `end: true` makes a day picked with no time mean
 *                                       23:59:59 instead of 00:00:00.
 *   read(id)    -> "YYYY-MM-DD HH:MM:SS" (single space, ready to send to the server) when the box holds a valid time;
 *                  "" when the box is empty; null when it holds something unparseable. Also tidies the box's text
 *                  into the canonical display format.
 *   set(id, v)  put a time into a box; v is a Date or any string parse() accepts.
 *   parse(str)  -> Date | undefined
 *
 * Copyright 2026 Kevin Burke
 * Licensed under the Apache License, Version 2.0.
 */
(function (global) {
  'use strict';

  var FORMAT = 'Y-m-d  H:i:S';
  var RE = /^(\d{4})-(\d{1,2})-(\d{1,2})[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?$/;

  function pad(n) { return String(n).padStart(2, '0'); }

  function parse(str) {
    var m = RE.exec((str || '').trim());
    if (!m) { return undefined; }
    var d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], m[6] ? +m[6] : 0);
    // Reject roll-overs such as month 13 or Feb 30.
    return (d.getFullYear() === +m[1] && d.getMonth() === +m[2] - 1 && d.getDate() === +m[3]) ? d : undefined;
  }

  function toServerString(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
           pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  function toDisplayString(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + '  ' +
           pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  function set(id, value) {
    var el = document.getElementById(id);
    if (!el) { return; }
    var d = (value instanceof Date) ? value : parse(String(value));
    if (!d) { el.value = value ? String(value) : ''; return; }
    if (el._flatpickr) { el._flatpickr.setDate(d, false); } else { el.value = toDisplayString(d); }
  }

  function read(id) {
    var el = document.getElementById(id);
    var raw = el ? el.value.trim() : '';
    if (!raw) { return ''; }
    var d = parse(raw);
    if (!d) { return null; }
    set(id, d);
    return toServerString(d);
  }

  function init(specs) {
    if (typeof global.flatpickr !== 'function') { return; }
    specs.forEach(function (spec) {
      var el = document.getElementById(spec.id);
      if (!el || el._flatpickr) { return; }
      global.flatpickr(el, {
        enableTime: true, enableSeconds: true, time_24hr: true,
        dateFormat: FORMAT,
        allowInput: true, disableMobile: true,
        parseDate: parse,
        // Picking just a day (no time) means the whole day: 00:00:00 for a start box, 23:59:59 for an end box.
        defaultHour: spec.end ? 23 : 0, defaultMinute: spec.end ? 59 : 0, defaultSeconds: spec.end ? 59 : 0,
        onClose: function (_selected, _text, fp) {
          var d = parse(fp.input.value);
          if (d) { fp.setDate(d, false); }
        },
      });
    });
  }

  global.MpgDateTime = { init: init, read: read, set: set, parse: parse, FORMAT: FORMAT };
})(window);
