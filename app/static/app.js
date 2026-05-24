const messagesEl = document.getElementById('messages');
const composerEl = document.getElementById('composer');
const questionEl = document.getElementById('question');
const propertyCodeEl = document.getElementById('propertyCode');
const modelSelectEl = document.getElementById('modelSelect');
const sendBtn = document.getElementById('sendBtn');

async function loadModels() {
  const res = await fetch('/models');
  const data = await res.json();
  const models = data.models || [];
  modelSelectEl.innerHTML = '';
  models.forEach((m) => {
    const opt = document.createElement('option');
    opt.value = m.model_id;
    opt.textContent = `${m.model_id} (${m.provider})`;
    if (m.enabled_by_default) opt.selected = true;
    modelSelectEl.appendChild(opt);
  });
}

function addMessage(role, markdown, extra = {}) {
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = role === 'user' ? 'You' : `Assistant • ${extra.route || 'N/A'} • ${extra.model_id || ''}`;

  const content = document.createElement('div');
  content.className = 'content';
  content.innerHTML = marked.parse(markdown || '');

  wrap.appendChild(meta);
  wrap.appendChild(content);

  if (extra.ui_blocks && Array.isArray(extra.ui_blocks) && extra.ui_blocks.length) {
    wrap.appendChild(renderUiBlocks(extra.ui_blocks));
  }

  if (role !== 'user' && extra.citations && Array.isArray(extra.citations) && extra.citations.length) {
    wrap.appendChild(renderCitations(extra.citations));
  }

  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderCitations(citations) {
  const root = document.createElement('div');
  root.className = 'citations';
  const title = document.createElement('div');
  title.className = 'citations-title';
  title.textContent = 'Sources';
  root.appendChild(title);

  const list = document.createElement('ul');
  list.className = 'citations-list';

  citations.forEach((c) => {
    const li = document.createElement('li');
    if (c.source_type === 'sql' || c.source_type === 'mysql_snapshot') {
      const month = c.month_year || c.period_applied || 'n/a';
      const mode = c.query_source || c.sql_mode || 'deterministic';
      li.textContent = `SQL (${mode}) • property=${c.property_code || 'n/a'} • period=${month}`;
    } else if (c.source_url) {
      const a = document.createElement('a');
      a.href = c.source_url;
      a.target = '_blank';
      a.rel = 'noreferrer noopener';
      a.textContent = c.source_url;
      li.appendChild(a);
      const meta = document.createElement('span');
      meta.textContent = ` • chunk=${c.chunk_id || 'n/a'}${c.score != null ? ` • score=${Number(c.score).toFixed(3)}` : ''}`;
      li.appendChild(meta);
    } else {
      li.textContent = JSON.stringify(c);
    }
    list.appendChild(li);
  });

  root.appendChild(list);
  return root;
}

function renderUiBlocks(blocks) {
  const root = document.createElement('div');
  root.className = 'ui-blocks';

  const kpis = blocks.filter((b) => b.type === 'kpi_card');
  if (kpis.length) {
    const grid = document.createElement('div');
    grid.className = 'kpi-grid';
    kpis.forEach((k) => {
      const card = document.createElement('div');
      card.className = 'kpi';
      card.innerHTML = `<div class="t">${k.title || 'Metric'}</div><div class="v">${k.value ?? '-'}</div>`;
      grid.appendChild(card);
    });
    root.appendChild(grid);
  }

  blocks.filter((b) => b.type === 'table').forEach((tb) => {
    const wrap = document.createElement('div');
    wrap.className = 'table-wrap';
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const trh = document.createElement('tr');
    (tb.columns || []).forEach((c) => {
      const th = document.createElement('th');
      th.textContent = c;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    (tb.rows || []).forEach((r) => {
      const tr = document.createElement('tr');
      r.forEach((v) => {
        const td = document.createElement('td');
        td.textContent = v == null ? '' : String(v);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    root.appendChild(wrap);
  });

  blocks.filter((b) => b.type === 'chart').forEach((ch) => {
    const canvas = document.createElement('canvas');
    root.appendChild(canvas);
    new Chart(canvas, ch.config || { type: 'bar', data: { labels: [], datasets: [] } });
  });

  blocks.filter((b) => b.type === 'comparison_view').forEach((cmp) => {
    const div = document.createElement('div');
    div.className = 'kpi';
    div.innerHTML = `<div class="t">Comparison</div><pre>${JSON.stringify(cmp.data || {}, null, 2)}</pre>`;
    root.appendChild(div);
  });

  return root;
}

composerEl.addEventListener('submit', async (e) => {
  e.preventDefault();
  const question = questionEl.value.trim();
  if (!question) return;

  const propertyCode = propertyCodeEl.value.trim().toUpperCase();
  const modelId = modelSelectEl.value;

  addMessage('user', question);
  questionEl.value = '';
  sendBtn.disabled = true;

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Property-Code': propertyCode,
      },
      body: JSON.stringify({
        property_code: propertyCode,
        question,
        model_id: modelId,
      }),
    });

    const data = await res.json();
    if (!res.ok) {
      addMessage('assistant', `**Error:** ${data.detail || 'Request failed'}`);
    } else {
      addMessage('assistant', data.answer_markdown || '', data);
    }
  } catch (err) {
    addMessage('assistant', `**Network error:** ${err.message}`);
  } finally {
    sendBtn.disabled = false;
  }
});

loadModels();
addMessage('assistant', 'Welcome. Set a property code and ask your first question.');
