// TaxLens single-page app. Talks to the FastAPI sidecar on the same origin.
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const fmt = (n) => n == null ? '—' : '$' + Number(n).toLocaleString(undefined, {maximumFractionDigits: 0});
const fmtPct = (n) => (Number(n) * 100).toFixed(1) + '%';
const charts = {};

// ─── tabs ──────────────────────────────────────────────────────────────────
$$('.tab').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
function showTab(name) {
  $$('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab-panel').forEach(p => p.classList.toggle('hidden', p.id !== 'tab-' + name));
  // refresh data when a tab opens
  if (name === 'dashboard') renderDashboard();
  else if (name === 'year')     renderYearDetail();
  else if (name === 'math')     renderMath();
  else if (name === 'whatif')   renderWhatif();
  else if (name === 'compare')  renderCompare();
}
window.showTab = showTab;

// ─── state ─────────────────────────────────────────────────────────────────
let RETURNS = [];           // list_returns() output, sorted by year
const FULL = new Map();     // id → full return record (lazy)

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

async function refreshAll() {
  RETURNS = await api('/api/returns');
  RETURNS.sort((a, b) => a.tax_year - b.tax_year);
  $('#returnCount').textContent = RETURNS.length;
  populateYearPickers();
  // re-render whichever tab is active
  const active = $$('.tab').find(b => b.classList.contains('active'));
  if (active) showTab(active.dataset.tab);
}

function populateYearPickers() {
  const opts = RETURNS.map(r => `<option value="${r.id}">${r.tax_year}</option>`).join('');
  for (const id of ['yearPicker', 'mathYearPicker', 'whatifYearPicker', 'cmpLeft', 'cmpRight']) {
    const sel = document.getElementById(id);
    const prev = sel.value;
    sel.innerHTML = opts;
    if (prev) sel.value = prev;
  }
  if (RETURNS.length >= 2) {
    $('#cmpLeft').value  = String(RETURNS[RETURNS.length - 2].id);
    $('#cmpRight').value = String(RETURNS[RETURNS.length - 1].id);
  }
}

async function loadFull(id) {
  if (FULL.has(id)) return FULL.get(id);
  const r = await api('/api/returns/' + id);
  FULL.set(id, r);
  return r;
}

// ─── import ────────────────────────────────────────────────────────────────
const dz = $('#dropzone'), fi = $('#fileInput');
dz.addEventListener('click', () => fi.click());
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('border-emerald-400'); });
dz.addEventListener('dragleave', () => dz.classList.remove('border-emerald-400'));
dz.addEventListener('drop', e => {
  e.preventDefault();
  dz.classList.remove('border-emerald-400');
  uploadFiles([...e.dataTransfer.files]);
});
fi.addEventListener('change', () => uploadFiles([...fi.files]));

async function uploadFiles(files) {
  const list = $('#importList');
  list.classList.remove('hidden');
  for (const f of files) {
    const row = document.createElement('div');
    row.className = 'px-5 py-3 flex items-center justify-between';
    row.innerHTML = `<div class="flex items-center gap-3">
        <span class="text-sky-500 animate-pulse">⟳</span>
        <div><div class="font-medium">${f.name}</div><div class="text-xs text-slate-500">parsing…</div></div>
      </div><span class="text-xs px-2 py-0.5 rounded-full bg-sky-100 text-sky-700">parsing</span>`;
    list.appendChild(row);
    try {
      const fd = new FormData();
      fd.append('file', f);
      const out = await api('/api/returns/import', { method: 'POST', body: fd });
      const recon = out.result.reconciliation_delta;
      const reconciled = recon != null && Math.abs(Number(recon)) <= 1;
      const badge = recon == null
        ? `<span class="text-xs px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">no reported tax</span>`
        : reconciled
          ? `<span class="text-xs px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700">Reconciled ✓</span>`
          : `<span class="text-xs px-2 py-0.5 rounded-full bg-amber-100 text-amber-700">Δ $${recon}</span>`;
      const warn = out.warnings && out.warnings.length ? ` · ${out.warnings.length} warning(s)` : '';
      row.innerHTML = `<div class="flex items-center gap-3">
          <span class="text-emerald-600">✓</span>
          <div><div class="font-medium">${f.name}</div>
            <div class="text-xs text-slate-500">TY ${out.tax_year} · ${out.filing_status.toUpperCase()} · ${out.source}${warn}</div>
          </div></div>${badge}`;
    } catch (err) {
      row.innerHTML = `<div class="flex items-center gap-3">
          <span class="text-rose-500">✗</span>
          <div><div class="font-medium">${f.name}</div>
            <div class="text-xs text-rose-500">${err.message}</div>
          </div></div><span class="text-xs px-2 py-0.5 rounded-full bg-rose-100 text-rose-700">failed</span>`;
    }
  }
  await refreshAll();
}

// ─── dashboard ─────────────────────────────────────────────────────────────
function renderDashboard() {
  const empty = RETURNS.length === 0;
  $('#dashEmpty').classList.toggle('hidden', !empty);
  $('#dashContent').classList.toggle('hidden', empty);
  if (empty) return;

  const totalIncome = RETURNS.reduce((s, r) => s + Number(r.agi || 0), 0);
  const totalTax = RETURNS.reduce((s, r) => s + Number(r.total_tax || 0), 0);
  const latest = RETURNS[RETURNS.length - 1];
  const effective = totalIncome > 0 ? totalTax / totalIncome : 0;
  $('#kpis').innerHTML = `
    ${kpi('Total AGI', fmt(totalIncome), `${RETURNS.length} year(s)`)}
    ${kpi('Total federal tax', fmt(totalTax), fmtPct(effective) + ' avg effective')}
    ${kpi('Latest year', String(latest.tax_year), latest.filing_status.toUpperCase())}
    ${kpi('Latest refund/owed', fmt(latest.refund_or_owed), Number(latest.refund_or_owed) >= 0 ? 'refund' : 'owed')}
  `;

  // returns table
  $('#returnsTable').innerHTML = RETURNS.map(r => {
    const recon = r.reconciled === null || r.reconciled === undefined ? '—'
                : r.reconciled ? '<span class="text-emerald-600">✓</span>'
                : `<span class="text-amber-600">Δ $${r.reconciliation_delta}</span>`;
    return `<tr class="hover:bg-slate-50 cursor-pointer" onclick="pickYear(${r.id})">
      <td class="py-2">${r.tax_year}</td>
      <td>${r.filing_status.toUpperCase()}</td>
      <td><span class="text-xs px-2 py-0.5 rounded-full bg-slate-100">${r.source}</span></td>
      <td class="text-right">${fmt(r.agi)}</td>
      <td class="text-right">${fmt(r.total_tax)}</td>
      <td class="text-right">${fmt(r.refund_or_owed)}</td>
      <td class="text-center">${recon}</td></tr>`;
  }).join('');

  // charts (need full records for income decomposition + taxes by type)
  Promise.all(RETURNS.map(r => loadFull(r.id))).then(fulls => {
    drawIncomeStack(fulls);
    drawRateLine(fulls);
    drawTaxDonut(fulls[fulls.length - 1]);
  });
}

function kpi(label, big, small) {
  return `<div class="bg-white rounded-2xl border border-slate-200 p-5">
    <div class="text-xs text-slate-500">${label}</div>
    <div class="text-2xl font-bold mt-1">${big}</div>
    <div class="text-xs text-slate-500 mt-1">${small}</div></div>`;
}

window.pickYear = (id) => {
  $('#yearPicker').value = String(id);
  showTab('year');
};

function recreate(id, cfg) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), cfg);
}

function drawIncomeStack(fulls) {
  const years = fulls.map(f => f.tax_year);
  const series = {
    Wages:    fulls.map(f => Number(f.return.wages)),
    'Qual div': fulls.map(f => Number(f.return.qualified_dividends)),
    'Ord div': fulls.map(f => Number(f.return.ordinary_dividends) - Number(f.return.qualified_dividends)),
    LTCG:     fulls.map(f => Number(f.return.long_term_capital_gains)),
    STCG:     fulls.map(f => Number(f.return.short_term_capital_gains)),
    Interest: fulls.map(f => Number(f.return.interest_income)),
    SE:       fulls.map(f => Number(f.return.se_income)),
    Other:    fulls.map(f => Number(f.return.other_ordinary_income)),
  };
  const colors = ['#34d399','#60a5fa','#3b82f6','#a78bfa','#c084fc','#fbbf24','#f472b6','#94a3b8'];
  const datasets = Object.entries(series).map(([label, data], i) => ({ label, data, backgroundColor: colors[i] }));
  recreate('incomeStack', {
    type: 'bar',
    data: { labels: years, datasets },
    options: {
      scales: { x: { stacked: true }, y: { stacked: true, ticks: { callback: v => '$' + (v/1000).toFixed(0) + 'k' } } },
      plugins: { legend: { position: 'bottom' } }
    }
  });
}

function drawRateLine(fulls) {
  const years = fulls.map(f => f.tax_year);
  const eff = fulls.map(f => {
    const agi = Number(f.result.agi);
    return agi ? Number((Number(f.result.total_tax) / agi * 100).toFixed(2)) : 0;
  });
  // Marginal = top filled ordinary bracket rate
  const marg = fulls.map(f => {
    const fills = f.result.ordinary_bracket_fills || [];
    const top = fills[fills.length - 1];
    return top ? Number(Number(top.rate) * 100) : 0;
  });
  recreate('rateLine', {
    type: 'line',
    data: { labels: years, datasets: [
      { label: 'Effective', data: eff,  borderColor: '#0f766e', tension: 0.3 },
      { label: 'Marginal',  data: marg, borderColor: '#94a3b8', borderDash: [4,4] },
    ]},
    options: { plugins: { legend: { position: 'bottom' } }, scales: { y: { ticks: { callback: v => v + '%' } } } }
  });
}

function drawTaxDonut(full) {
  const r = full.result;
  const data = [
    ['Ordinary income', Number(r.ordinary_tax)],
    ['Qualified inc.',  Number(r.qualified_tax)],
    ['Collectibles',    Number(r.collectibles_tax || 0)],
    ['Unrecap. §1250',  Number(r.unrecaptured_1250_tax || 0)],
    ['AMT',             Number(r.amt || 0)],
    ['SE tax',          Number(r.se_tax)],
    ['Add\'l Medicare', Number(r.additional_medicare_tax)],
    ['NIIT',            Number(r.niit)],
    ['State',           Number(r.state_result ? r.state_result.state_tax : 0)],
  ].filter(([_, v]) => v > 0);
  recreate('taxDonut', {
    type: 'doughnut',
    data: { labels: data.map(d => d[0]), datasets: [{
      data: data.map(d => d[1]),
      backgroundColor: ['#0f172a','#60a5fa','#f97316','#10b981','#ef4444','#f472b6','#fbbf24','#a78bfa','#14b8a6']
    }]},
    options: { plugins: { legend: { position: 'bottom', labels: { boxWidth: 10 } } } }
  });
}

// ─── year detail ───────────────────────────────────────────────────────────
$('#yearPicker').addEventListener('change', renderYearDetail);
async function renderYearDetail() {
  if (RETURNS.length === 0) return;
  const id = Number($('#yearPicker').value || RETURNS[0].id);
  const full = await loadFull(id);
  const r = full.result, ret = full.return;

  // Waterfall
  const gross = ['wages','interest_income','ordinary_dividends','long_term_capital_gains','short_term_capital_gains','se_income','other_ordinary_income']
    .reduce((s, k) => s + Number(ret[k] || 0), 0);
  const adj = gross - Number(r.agi);
  const ded = Number(r.deduction_used);
  const taxablePoint = Number(r.taxable_income);
  const tax = Number(r.total_tax);
  const afterTax = gross - tax;

  recreate('waterfall', {
    type: 'bar',
    data: {
      labels: ['Gross', '−Adj', 'AGI', '−Deduction', 'Taxable', '−Tax', 'After-tax'],
      datasets: [{ label: '$', data: [
        [0, gross],
        [Number(r.agi), gross],
        [0, Number(r.agi)],
        [taxablePoint, Number(r.agi)],
        [0, taxablePoint],
        [afterTax, gross],
        [0, afterTax],
      ], backgroundColor: ['#34d399','#ef4444','#0ea5e9','#ef4444','#0ea5e9','#ef4444','#0f172a'] }]
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { callback: v => '$'+(v/1000).toFixed(0)+'k' } } } }
  });

  // Bracket fill
  const fills = r.ordinary_bracket_fills || [];
  recreate('brackets', {
    type: 'bar',
    data: {
      labels: fills.map(f => (Number(f.rate)*100).toFixed(0) + '%'),
      datasets: [{
        label: 'Taxable $ in bracket',
        data: fills.map(f => Number(f.amount_in_bracket)),
        backgroundColor: '#0f172a',
      }]
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { callback: v => '$'+(v/1000).toFixed(0)+'k' } } } }
  });

  // Tax breakdown cards
  $('#taxBreakdown').innerHTML = [
    ['Ordinary income tax', r.ordinary_tax],
    ['Qualified income tax', r.qualified_tax],
    ['Collectibles tax (28% cap)', r.collectibles_tax || '0'],
    ['Unrecaptured §1250 tax (25% cap)', r.unrecaptured_1250_tax || '0'],
    ['AMT (Form 6251)', r.amt || '0'],
    ['SE tax', r.se_tax],
    ['Additional Medicare', r.additional_medicare_tax],
    ['NIIT', r.niit],
    ['Credits', '-' + r.credits],
    ['Total federal tax', r.total_tax],
    ...(r.state_result ? [[`${r.state_result.state} state tax`, r.state_result.state_tax]] : []),
    ['Withholding + estimated', String(Number(ret.federal_withholding) + Number(ret.estimated_payments))],
    ['Refund/owed', r.refund_or_owed],
  ].map(([k,v]) => `<div class="border border-slate-200 rounded-lg p-3">
    <div class="text-xs text-slate-500">${k}</div><div class="font-mono text-lg">${fmt(v)}</div></div>`).join('');
}

// ─── math view ─────────────────────────────────────────────────────────────
$('#mathYearPicker').addEventListener('change', renderMath);
async function renderMath() {
  if (RETURNS.length === 0) return;
  const id = Number($('#mathYearPicker').value || RETURNS[0].id);
  const full = await loadFull(id);
  const r = full.result;
  $('#mathSteps').innerHTML = r.steps.map(s => `
    <details class="px-5 py-3" ${s.label.startsWith('Total') || s.label.startsWith('Refund') ? 'open' : ''}>
      <summary class="flex items-center justify-between">
        <div><span class="text-xs px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 mr-2">Step ${s.index}</span>
          <span class="font-medium">${s.label}</span></div>
        <div class="font-mono">${fmt(s.output)}</div>
      </summary>
      <div class="mt-3 ml-12 text-sm">
        <div class="font-mono text-slate-700 bg-slate-50 rounded p-3">${s.formula}</div>
        <div class="text-xs text-slate-500 mt-2">inputs: ${Object.entries(s.inputs).map(([k,v])=>`<code>${k}=${v}</code>`).join(' · ')}</div>
      </div>
    </details>`).join('');

  $('#ordinaryBrackets').innerHTML = bracketTable(r.ordinary_bracket_fills);
  const hasQual = (r.qualified_bracket_fills || []).length > 0;
  $('#qualifiedBracketsSection').classList.toggle('hidden', !hasQual);
  if (hasQual) $('#qualifiedBrackets').innerHTML = bracketTable(r.qualified_bracket_fills);
}

function bracketTable(fills) {
  return `<table class="w-full text-sm font-mono">
    <thead class="text-xs text-slate-500 text-left">
      <tr><th class="py-1">Bracket</th><th>Rate</th><th>In bracket</th><th>Tax</th></tr></thead>
    <tbody class="divide-y divide-slate-100">${fills.map(f => `<tr>
      <td class="py-1">$${Number(f.lower).toLocaleString()} – ${f.upper == null ? '∞' : '$'+Number(f.upper).toLocaleString()}</td>
      <td>${(Number(f.rate)*100).toFixed(0)}%</td>
      <td>${fmt(f.amount_in_bracket)}</td>
      <td>${fmt(f.tax_in_bracket)}</td></tr>`).join('')}</tbody></table>`;
}

// ─── what-if ───────────────────────────────────────────────────────────────
const WHATIF_FIELDS = ['wages','interest_income','ordinary_dividends','qualified_dividends',
  'long_term_capital_gains','short_term_capital_gains','se_income','other_ordinary_income',
  'hsa_deduction','federal_withholding','estimated_payments','qualifying_children'];

$('#whatifYearPicker').addEventListener('change', renderWhatif);
$('#whatifReset').addEventListener('click', renderWhatif);
$('#whatifRun').addEventListener('click', runWhatif);

async function renderWhatif() {
  if (RETURNS.length === 0) return;
  const id = Number($('#whatifYearPicker').value || RETURNS[0].id);
  const full = await loadFull(id);
  $('#whatifForm').innerHTML = WHATIF_FIELDS.map(f => `
    <div class="flex items-center gap-3">
      <label class="w-48 text-slate-600">${f.replace(/_/g,' ')}</label>
      <input data-field="${f}" class="flex-1 border border-slate-300 rounded px-2 py-1 font-mono text-right"
             value="${full.return[f] ?? 0}" />
    </div>`).join('');
  $('#whatifTable').innerHTML = '';
}

async function runWhatif() {
  const id = Number($('#whatifYearPicker').value);
  const edits = {};
  $$('#whatifForm input[data-field]').forEach(inp => { edits[inp.dataset.field] = inp.value; });
  const out = await api(`/api/returns/${id}/whatif`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(edits)
  });
  const rows = [
    ['AGI', 'agi'], ['Taxable income', 'taxable_income'], ['Ordinary tax', 'ordinary_tax'],
    ['Qualified tax', 'qualified_tax'], ['SE tax', 'se_tax'], ['NIIT', 'niit'],
    ['Add\'l Medicare', 'additional_medicare_tax'], ['Credits', 'credits'],
    ['Total tax', 'total_tax'], ['Refund/owed', 'refund_or_owed'],
  ];
  $('#whatifTable').innerHTML = rows.map(([label, key]) => {
    const a = Number(out.original[key]), b = Number(out.whatif[key]);
    const d = b - a;
    const color = d === 0 ? 'text-slate-400' : (d > 0 ? 'text-emerald-600' : 'text-rose-600');
    return `<tr><td class="py-1">${label}</td>
      <td class="text-right">${fmt(a)}</td><td class="text-right">${fmt(b)}</td>
      <td class="text-right ${color}">${d >= 0 ? '+' : ''}${fmt(d)}</td></tr>`;
  }).join('');
}

// ─── compare ───────────────────────────────────────────────────────────────
['cmpLeft','cmpRight'].forEach(id => document.getElementById(id).addEventListener('change', renderCompare));
async function renderCompare() {
  if (RETURNS.length < 2) { $('#compareTable').innerHTML = '<tr><td class="px-5 py-4 text-slate-400">Need at least 2 returns.</td></tr>'; return; }
  const li = Number($('#cmpLeft').value), ri = Number($('#cmpRight').value);
  if (!li || !ri) return;
  const [L, R] = await Promise.all([loadFull(li), loadFull(ri)]);
  const rows = [
    ['Wages',                    L.return.wages,                    R.return.wages],
    ['Interest',                 L.return.interest_income,          R.return.interest_income],
    ['Qualified dividends',      L.return.qualified_dividends,      R.return.qualified_dividends],
    ['Long-term cap gains',      L.return.long_term_capital_gains,  R.return.long_term_capital_gains],
    ['SE income',                L.return.se_income,                R.return.se_income],
    ['AGI',                      L.result.agi,                      R.result.agi],
    ['Taxable income',           L.result.taxable_income,           R.result.taxable_income],
    ['Ordinary tax',             L.result.ordinary_tax,             R.result.ordinary_tax],
    ['Qualified tax',            L.result.qualified_tax,            R.result.qualified_tax],
    ['Collectibles tax',         L.result.collectibles_tax || 0,    R.result.collectibles_tax || 0],
    ['Unrecap. §1250 tax',       L.result.unrecaptured_1250_tax || 0, R.result.unrecaptured_1250_tax || 0],
    ['AMT',                      L.result.amt || 0,                 R.result.amt || 0],
    ['SE tax',                   L.result.se_tax,                   R.result.se_tax],
    ['NIIT',                     L.result.niit,                     R.result.niit],
    ['Credits',                  L.result.credits,                  R.result.credits],
    ['Total tax',                L.result.total_tax,                R.result.total_tax],
    ['Refund/owed',              L.result.refund_or_owed,           R.result.refund_or_owed],
  ];
  $('#compareTable').innerHTML = rows.map(([k, a, b]) => {
    a = Number(a); b = Number(b); const d = b - a;
    const pct = a !== 0 ? (d / Math.abs(a) * 100).toFixed(1) + '%' : '—';
    const color = d === 0 ? '' : (d > 0 ? 'text-emerald-600' : 'text-rose-600');
    return `<tr><td class="px-5 py-2">${k}</td>
      <td class="text-right">${fmt(a)}</td><td class="text-right">${fmt(b)}</td>
      <td class="text-right ${color}">${d >= 0 ? '+' : ''}${fmt(d)}</td>
      <td class="text-right pr-6 ${color}">${pct}</td></tr>`;
  }).join('');
}

// ─── boot ──────────────────────────────────────────────────────────────────
refreshAll();
