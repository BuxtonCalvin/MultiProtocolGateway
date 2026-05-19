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

    if (shape === 'rect') {
      svgLayer.appendChild(makeSvg('rect', {
        x: Math.max(0, x - Number(item.width_percent || 8) / 2),
        y: Math.max(0, y - Number(item.height_percent || 6) / 2),
        width: Number(item.width_percent || 8),
        height: Number(item.height_percent || 6),
        rx: 0.8,
        class: 'docs-hotspot-rect',
        fill: 'rgba(14, 165, 233, 0.18)',
        stroke: 'rgb(14, 165, 233)',
        'stroke-width': '0.35',
        'vector-effect': 'non-scaling-stroke',
      }));
    } else {
      svgLayer.appendChild(makeSvg('circle', {
        cx: x,
        cy: y,
        r: 1.45,
        class: 'docs-hotspot-dot',
        fill: 'rgb(14, 165, 233)',
        stroke: 'white',
        'stroke-width': '0.55',
        'vector-effect': 'non-scaling-stroke',
      }));
    }

    const callout = document.createElement('div');
    callout.className = 'docs-callout';
    callout.style.left = `${labelX}%`;
    callout.style.top = `${labelY}%`;
    callout.dataset.autoOffset = item.label_x_percent == null && item.label_y_percent == null ? 'true' : 'false';
    callout.innerHTML = `<span>${index + 1}</span><p>${esc(item.label)}</p>`;
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
