// for use in js console on read.amazon.ca/notebook
(() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const getHighlightColor = (container) => {
    const highlightDiv = container.querySelector('.kp-notebook-highlight');
    if (!highlightDiv) return '';
    const colorClass = [...highlightDiv.classList].find(c =>
      c.startsWith('kp-notebook-highlight-')
    );
    return colorClass
      ? colorClass.replace('kp-notebook-highlight-', '')
      : '';
  };

  const parseHeader = (headerText) => {
    const raw = clean(headerText);
    const match = raw.match(/^(\w+)\s+highlight\s*\|\s*Location:\s*(.+)$/i);
    if (match) {
      return { color: match[1].toLowerCase(), location: match[2].trim() };
    }
    return { color: '', location: '' };
  };

  const containers = Array.from(document.querySelectorAll('.a-row.a-spacing-base'));

  const rows = [];
  containers.forEach((container) => {
    const highlightDiv = container.querySelector('.kp-notebook-highlight');
    if (!highlightDiv) return;

    const highlightSpan = highlightDiv.querySelector('span#highlight');
    const highlightText = clean(highlightSpan?.textContent || '');
    if (!highlightText) return;

    const highlightColor = getHighlightColor(container);

    const headerEl = container.querySelector('#annotationHighlightHeader');
    const headerInfo = parseHeader(headerEl?.textContent || '');
    const location = headerInfo.location ||
      (container.querySelector('#kp-annotation-location')?.value || '');

    const noteDiv = container.querySelector('.kp-notebook-note');
    let noteText = '';
    if (noteDiv && !noteDiv.classList.contains('aok-hidden')) {
      const noteSpan = noteDiv.querySelector('span#note');
      noteText = clean(noteSpan?.textContent || '');
    }

    const highlightId = highlightDiv.id || '';

    rows.push({
      index: rows.length + 1,
      highlight_id: highlightId,
      highlight_color: highlightColor,
      location,
      highlight_text: highlightText,
      note_text: noteText,
      source_context: `Location ${location}${highlightColor ? ` • ${highlightColor}` : ''}`
    });
  });

  if (rows.length === 0) {
    console.warn('No highlights found on this page.');
    return [];
  }

  const headers = Object.keys(rows[0]);
  const csvEscape = (value) => `"${String(value ?? '').replace(/"/g, '""')}"`;
  const csv = [
    headers.join(','),
    ...rows.map(row => headers.map(h => csvEscape(row[h])).join(','))
  ].join('\n');

  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'kindle_notebook_export.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  console.table(rows);
  return rows;
})();
