/*
 * Description: Provides browser-side behavior for the help-annotations web UI asset.
 * File: help-annotations.js
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

(function () {
  const image = document.getElementById('docs-base-image');
  const svgLayer = document.getElementById('docs-annotation-layer');
  const calloutLayer = document.getElementById('docs-callout-layer');
  if (!image || !svgLayer || !calloutLayer) return;

  function makeSvg(tag, attrs) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }

  function esc(value) {
    const span = document.createElement('span');
    span.textContent = value == null ? '' : String(value);
    return span.innerHTML;
  }

  function renderAnnotation(item, index) {
    const x = Number(item.x_percent);
    const y = Number(item.y_percent);
    const shape = item.shape_type || 'dot';
    const labelX = item.label_x_percent == null ? x : Number(item.label_x_percent);
    const labelY = item.label_y_percent == null ? y : Number(item.label_y_percent);
    const num = String(index + 1);

    if (shape === 'rect') {
      const w  = Number(item.width_percent  || 8);
      const h  = Number(item.height_percent || 6);
      const rx = Math.max(0, x - w / 2);
      const ry = Math.max(0, y - h / 2);
      const strokeColor = item.stroke_color || 'rgb(14, 165, 233)';
      const fillColor   = item.fill_color   || 'rgba(14, 165, 233, 0.18)';

      const g = makeSvg('g', { class: 'docs-hotspot-rect-group' });
      const rectEl = makeSvg('rect', {
        x: rx, y: ry, width: w, height: h, rx: 0.8,
        class: 'docs-hotspot-rect',
        'stroke-width': '0.35',
        'vector-effect': 'non-scaling-stroke',
      });
      // Use inline style (not presentation attributes) so colours override any CSS rule
      rectEl.style.fill   = fillColor;
      rectEl.style.stroke = strokeColor;
      g.appendChild(rectEl);
      // Number badge in top-left corner of rect — slightly larger than annotation callout number
      const badgeR = 0.82;
      const bx = rx + badgeR + 0.2;
      const by = ry + badgeR + 0.2;
      const badgeEl = makeSvg('circle', {
        cx: bx, cy: by, r: badgeR,
        'stroke-width': '0.3',
        'vector-effect': 'non-scaling-stroke',
      });
      badgeEl.style.fill   = strokeColor;
      badgeEl.style.stroke = 'white';
      g.appendChild(badgeEl);
      const t = makeSvg('text', {
        x: bx, y: by,
        'text-anchor': 'middle', 'dominant-baseline': 'central',
        'font-size': num.length > 1 ? badgeR * 0.9 : badgeR * 1.05,
        'font-weight': '700', 'font-family': 'sans-serif',
        fill: 'white', 'pointer-events': 'none',
      });
      t.textContent = num;
      g.appendChild(t);
      svgLayer.appendChild(g);
    } else {
      const r = 1.9;
      const g = makeSvg('g', { class: 'docs-hotspot-dot-group' });
      g.appendChild(makeSvg('circle', {
        cx: x, cy: y, r,
        class: 'docs-hotspot-dot',
        fill: 'rgb(14, 165, 233)',
        stroke: 'white',
        'stroke-width': '0.45',
        'vector-effect': 'non-scaling-stroke',
      }));
      const t = makeSvg('text', {
        x, y,
        'text-anchor': 'middle', 'dominant-baseline': 'central',
        'font-size': num.length > 1 ? r * 0.95 : r * 1.1,
        'font-weight': '700', 'font-family': 'sans-serif',
        fill: 'white', 'pointer-events': 'none',
      });
      t.textContent = num;
      g.appendChild(t);
      svgLayer.appendChild(g);
    }

    const callout = document.createElement('div');
    callout.className = 'docs-callout';
    // If label is in the right 25% of the image, flip it left so it doesn't overflow
    if (labelX > 75) {
      callout.style.left = `${labelX}%`;
      callout.dataset.flip = 'right';
    } else {
      callout.style.left = `${labelX}%`;
    }
    callout.style.top = `${labelY}%`;
    callout.dataset.autoOffset = item.label_x_percent == null && item.label_y_percent == null ? 'true' : 'false';
    callout.innerHTML = `<span>${num}</span><p>${esc(item.label)}</p>`;
    calloutLayer.appendChild(callout);
  }

  async function loadAnnotations() {
    const imageId = image.dataset.imageId;
    if (!imageId) return;
    const response = await fetch(`/annotations/${encodeURIComponent(imageId)}`);
    if (!response.ok) return;
    const annotations = await response.json();
    svgLayer.replaceChildren();
    calloutLayer.replaceChildren();
    annotations.forEach(renderAnnotation);
  }

  if (image.complete) {
    loadAnnotations().catch(() => {});
  } else {
    image.addEventListener('load', () => loadAnnotations().catch(() => {}), { once: true });
  }

  function enableCoordinatePicker() {
    if (!new URLSearchParams(window.location.search).has('edit')) return;

    const readout = document.createElement('div');
    readout.className = 'docs-coordinate-readout';
    readout.textContent = 'Click the screenshot to copy annotation coordinates.';
    image.parentElement.appendChild(readout);

    image.parentElement.style.cursor = 'crosshair';
    image.parentElement.addEventListener('click', async (event) => {
      const rect = image.getBoundingClientRect();
      const x = ((event.clientX - rect.left) / rect.width) * 100;
      const y = ((event.clientY - rect.top) / rect.height) * 100;
      const text = `"x_percent": ${x.toFixed(2)}, "y_percent": ${y.toFixed(2)}`;
      readout.textContent = text;
      try {
        await navigator.clipboard.writeText(text);
        readout.textContent = `${text} copied`;
      } catch {
        readout.textContent = text;
      }
    });
  }

  enableCoordinatePicker();
})();
