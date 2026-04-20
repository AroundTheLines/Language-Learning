// for use in js console on read.amazon.com/
(() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const getHighlightColor = (wrapper) => {
    const colorEl = wrapper.querySelector('.notebook-editable-item__highlight-color');
    if (!colorEl) return '';
    const colorClass = [...colorEl.classList].find(c =>
      c.startsWith('notebook-editable-item__highlight-color--')
    );
    return colorClass
      ? colorClass.replace('notebook-editable-item__highlight-color--', '')
      : '';
  };

  const parseTitle = (titleText) => {
    const raw = clean(titleText);
    const match = raw.match(/^(Highlight|Note)\s*•\s*Page\s+(.+)$/i);
    if (match) {
      return { type: match[1], page: match[2] };
    }
    return { type: raw, page: '' };
  };

  const wrappers = Array.from(document.querySelectorAll('.notebook-editable-item-wrapper'));

  const rows = wrappers.map((wrapper, index) => {
    const item = wrapper.querySelector('ion-item.notebook-editable-item');
    const groupedId = item?.id || '';
    const highlightColor = getHighlightColor(wrapper);
    const contents = Array.from(wrapper.querySelectorAll('.notebook-editable-item--content'));

    let highlightText = '';
    let noteText = '';
    let highlightPage = '';
    let notePage = '';

    contents.forEach((content) => {
      const titleEl = content.querySelector('.grouped-annotation_title');
      const textEl = content.querySelector('.notebook-editable-item-black');
      const title = parseTitle(titleEl?.textContent || '');
      const text = clean(textEl?.textContent || '');

      if (/^highlight$/i.test(title.type)) {
        highlightText = text;
        highlightPage = title.page;
      } else if (/^note$/i.test(title.type)) {
        noteText = text;
        notePage = title.page;
      }
    });

    const page = highlightPage || notePage || '';

    return {
      index: index + 1,
      grouped_id: groupedId,
      highlight_color: highlightColor,
      page,
      highlight_text: highlightText,
      note_text: noteText,
      anki_front: highlightText,
      anki_back: noteText || '',
      source_context: `Page ${page}${highlightColor ? ` • ${highlightColor}` : ''}`
    };
  });

  const headers = Object.keys(rows[0] || {});
  const csvEscape = (value) => `"${String(value ?? '').replace(/"/g, '""')}"`;
  const csv = [
    headers.join(','),
    ...rows.map(row => headers.map(h => csvEscape(row[h])).join(','))
  ].join('\n');

  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'kindle_anki_export.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  console.table(rows);
  return rows;
})();