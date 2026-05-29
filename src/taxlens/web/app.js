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
  else if (name === 'advisor')  renderAdvisor();
  else if (name === 'plan')     renderPlan();
  else if (name === 'trends')   renderTrends();
}
window.showTab = showTab;

// ─── state ─────────────────────────────────────────────────────────────────
let RETURNS = [];           // list_returns() output, sorted by year
const FULL = new Map();     // id → full return record (lazy)

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    // Try to extract a JSON `detail` field (FastAPI HTTPException body) so the
    // UI surfaces a useful message instead of the raw JSON envelope.
    let detail = '';
    try {
      const body = await r.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body);
    } catch (_) {
      try { detail = await r.text(); } catch (_) { detail = ''; }
    }
    const err = new Error(`${r.status}: ${detail || r.statusText}`);
    err.status = r.status;
    err.detail = detail;
    throw err;
  }
  return r.json();
}

async function refreshAll() {
  RETURNS = await api('/api/returns');
  RETURNS.sort((a, b) => a.tax_year - b.tax_year);
  $('#returnCount').textContent = RETURNS.length;
  renderReturnsMenu();
  populateYearPickers();
  // re-render whichever tab is active
  const active = $$('.tab').find(b => b.classList.contains('active'));
  if (active) showTab(active.dataset.tab);
}

// ─── Returns menu (top-right popover) ─────────────────────────────────────
function renderReturnsMenu() {
  const list = document.getElementById('returnsMenuList');
  if (!list) return;
  if (RETURNS.length === 0) {
    list.innerHTML = `<div class="px-4 py-6 text-center text-slate-400 text-xs italic">
      No returns imported yet.</div>`;
    return;
  }
  list.innerHTML = RETURNS.map(r => {
    const status = r.reconciled === null || r.reconciled === undefined
      ? '<span class="text-slate-400 text-[10px]">unverified</span>'
      : r.reconciled
        ? '<span class="text-emerald-600 text-[10px]">✓ reconciled</span>'
        : `<span class="text-amber-600 text-[10px]">Δ $${r.reconciliation_delta}</span>`;
    return `<div class="px-4 py-2 flex items-center justify-between hover:bg-slate-50">
      <button type="button" class="text-left flex-1 cursor-pointer"
              onclick="closeReturnsMenu(); pickYear(${r.id})">
        <div class="font-medium">${r.tax_year}
          <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-600 ml-1">${r.source}</span>
        </div>
        <div class="text-xs text-slate-500">${r.filing_status.toUpperCase()} · AGI ${fmt(r.agi)} · tax ${fmt(r.total_tax)}</div>
        <div class="mt-0.5">${status}</div>
      </button>
      <button type="button" class="ml-3 text-slate-400 hover:text-rose-600 px-2 py-1 rounded hover:bg-rose-50"
              title="Remove this return"
              onclick="event.stopPropagation(); deleteReturn(${r.id}, ${r.tax_year})">🗑</button>
    </div>`;
  }).join('');
}

window.closeReturnsMenu = () => document.getElementById('returnsMenu').classList.add('hidden');
window.toggleReturnsMenu = () => {
  const m = document.getElementById('returnsMenu');
  m.classList.toggle('hidden');
};

// Wire returns-menu listeners immediately. app.js is loaded at end-of-body,
// so the DOM is already parsed — using DOMContentLoaded here would no-op.
(function wireReturnsMenu() {
  const btn = document.getElementById('returnsMenuBtn');
  const menu = document.getElementById('returnsMenu');
  const closeBtn = document.getElementById('returnsMenuClose');
  const importBtn = document.getElementById('returnsMenuImport');
  if (!btn || !menu) return;
  btn.addEventListener('click', (e) => { e.stopPropagation(); window.toggleReturnsMenu(); });
  if (closeBtn) closeBtn.addEventListener('click', window.closeReturnsMenu);
  if (importBtn) importBtn.addEventListener('click', () => { window.closeReturnsMenu(); showTab('import'); });
  // Click anywhere outside the menu closes it.
  document.addEventListener('click', (e) => {
    if (menu.classList.contains('hidden')) return;
    if (menu.contains(e.target) || btn.contains(e.target)) return;
    window.closeReturnsMenu();
  });
  // Esc also closes.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') window.closeReturnsMenu();
  });
})();

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
          </div></div>
        <div class="flex items-center gap-2">${badge}
          <button class="text-slate-400 hover:text-rose-600 px-2 py-1 rounded hover:bg-rose-50" title="Remove this return"
            onclick="removeImportedReturn(${out.id}, ${out.tax_year}, this)">🗑</button>
        </div>`;
    } catch (err) {
      // err.detail (set by api()) contains the diagnostic from the 422 body.
      // Split on " Tail: " so the noisy traceback can be hidden by default.
      const detail = err.detail || err.message || 'Unknown error';
      const tailIdx = detail.indexOf(' Tail:');
      const headline = tailIdx >= 0 ? detail.slice(0, tailIdx) : detail;
      const tail = tailIdx >= 0 ? detail.slice(tailIdx + 7) : '';
      const escTail = tail.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
      const details = tail
        ? `<details class="mt-1"><summary class="text-xs text-rose-400 cursor-pointer hover:underline">Show technical details</summary>
             <pre class="mt-1 text-[10px] text-rose-500 bg-rose-50 border border-rose-100 rounded p-2 whitespace-pre-wrap break-all">${escTail}</pre>
             <button class="mt-1 text-xs text-sky-600 hover:underline" onclick="navigator.clipboard.writeText(${JSON.stringify(detail)}); this.textContent='Copied ✓'">Copy diagnostic</button>
           </details>`
        : '';
      row.innerHTML = `<div class="flex items-start gap-3 flex-1 min-w-0">
          <span class="text-rose-500 mt-0.5">✗</span>
          <div class="min-w-0 flex-1"><div class="font-medium">${f.name}</div>
            <div class="text-xs text-rose-500 break-words">${headline}</div>
            ${details}
          </div></div><span class="text-xs px-2 py-0.5 rounded-full bg-rose-100 text-rose-700 ml-2 flex-shrink-0">failed</span>`;
      row.className = 'px-5 py-3 flex items-start justify-between';
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
  const prior  = RETURNS.length > 1 ? RETURNS[RETURNS.length - 2] : null;
  const effective = totalIncome > 0 ? totalTax / totalIncome : 0;
  const latestEff = Number(latest.agi) > 0 ? Number(latest.total_tax) / Number(latest.agi) : 0;
  const priorEff  = prior && Number(prior.agi) > 0 ? Number(prior.total_tax) / Number(prior.agi) : null;
  const dAgi  = prior ? Number(latest.agi)       - Number(prior.agi)       : null;
  const dTax  = prior ? Number(latest.total_tax) - Number(prior.total_tax) : null;
  const dEff  = priorEff != null ? (latestEff - priorEff) * 100 : null;
  $('#kpis').innerHTML = `
    ${kpi('Total AGI', fmt(totalIncome), `${RETURNS.length} year(s) · latest ${fmt(latest.agi)}`, dAgi, fmt)}
    ${kpi('Total federal tax', fmt(totalTax), `latest ${fmt(latest.total_tax)} · ${fmtPct(latestEff)} eff.`, dTax, fmt, true)}
    ${kpi('Latest effective rate', fmtPct(latestEff), `lifetime avg ${fmtPct(effective)}`, dEff, v => (v >= 0 ? '+' : '') + v.toFixed(2) + 'pp', true)}
    ${kpi('Latest refund/owed', fmt(latest.refund_or_owed), Number(latest.refund_or_owed) >= 0 ? 'refund' : 'owed')}
  `;

  // returns table
  $('#returnsTable').innerHTML = RETURNS.map(r => {
    const recon = r.reconciled === null || r.reconciled === undefined ? '—'
                : r.reconciled ? '<span class="text-emerald-600">✓</span>'
                : `<span class="text-amber-600">Δ $${r.reconciliation_delta}</span>`;
    return `<tr class="hover:bg-slate-50">
      <td class="py-2 cursor-pointer" onclick="pickYear(${r.id})">${r.tax_year}</td>
      <td class="cursor-pointer" onclick="pickYear(${r.id})">${r.filing_status.toUpperCase()}</td>
      <td class="cursor-pointer" onclick="pickYear(${r.id})"><span class="text-xs px-2 py-0.5 rounded-full bg-slate-100">${r.source}</span></td>
      <td class="text-right cursor-pointer" onclick="pickYear(${r.id})">${fmt(r.agi)}</td>
      <td class="text-right cursor-pointer" onclick="pickYear(${r.id})">${fmt(r.total_tax)}</td>
      <td class="text-right cursor-pointer" onclick="pickYear(${r.id})">${fmt(r.refund_or_owed)}</td>
      <td class="text-center cursor-pointer" onclick="pickYear(${r.id})">${recon}</td>
      <td class="text-center"><button class="text-slate-400 hover:text-rose-600 px-2 py-1 rounded hover:bg-rose-50" title="Delete this return" onclick="deleteReturn(${r.id}, ${r.tax_year})">🗑</button></td></tr>`;
  }).join('');

  // charts (need full records for income decomposition + taxes by type)
  Promise.all(RETURNS.map(r => loadFull(r.id))).then(fulls => {
    drawIncomeStack(fulls);
    drawRateLine(fulls);
    drawTaxDonut(fulls[fulls.length - 1]);
    drawTaxStack(fulls);
    drawCarryforwards(fulls);
  });
}

function kpi(label, big, small, delta, deltaFmt, inverse) {
  // `inverse=true` flips the color polarity (tax/effective-rate going UP is bad).
  let deltaHtml = '';
  if (delta != null && Number.isFinite(delta) && delta !== 0) {
    const goodWhenNegative = !!inverse;
    const isGood = goodWhenNegative ? delta < 0 : delta > 0;
    const color = isGood ? 'text-emerald-600' : 'text-rose-600';
    const arrow = delta > 0 ? '▲' : '▼';
    const txt = deltaFmt ? deltaFmt(delta) : String(delta);
    deltaHtml = `<div class="text-xs ${color} mt-1">${arrow} ${txt} vs prior year</div>`;
  }
  return `<div class="bg-white rounded-2xl border border-slate-200 p-5">
    <div class="text-xs text-slate-500">${label}</div>
    <div class="text-2xl font-bold mt-1">${big}</div>
    <div class="text-xs text-slate-500 mt-1">${small}</div>
    ${deltaHtml}
  </div>`;
}

window.pickYear = (id) => {
  $('#yearPicker').value = String(id);
  showTab('year');
};

window.deleteReturn = async (id, year) => {
  if (!confirm(`Delete tax year ${year}? This cannot be undone.`)) return;
  try {
    await api('/api/returns/' + id, { method: 'DELETE' });
    FULL.delete(id);
    await refreshAll();
  } catch (err) {
    alert('Could not delete return: ' + (err.detail || err.message));
  }
};

// Remove an entry directly from the post-upload import list (no nav required).
window.removeImportedReturn = async (id, year, btn) => {
  if (!confirm(`Remove tax year ${year} from TaxLens? This cannot be undone.`)) return;
  const row = btn.closest('div.px-5');
  btn.disabled = true;
  btn.textContent = '…';
  try {
    await api('/api/returns/' + id, { method: 'DELETE' });
    FULL.delete(id);
    if (row) {
      row.style.opacity = '0.5';
      row.innerHTML = `<div class="text-xs text-slate-500 italic">Removed tax year ${year}.</div>`;
    }
    await refreshAll();
  } catch (err) {
    btn.disabled = false;
    btn.textContent = '🗑';
    alert('Could not remove return: ' + (err.detail || err.message));
  }
};

function recreate(id, cfg) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), cfg);
}

function drawIncomeStack(fulls) {
  const years = fulls.map(f => f.tax_year);
  const series = {
    Wages:        fulls.map(f => Number(f.return.wages)),
    'Qual div':   fulls.map(f => Number(f.return.qualified_dividends)),
    'Ord div':    fulls.map(f => Number(f.return.ordinary_dividends) - Number(f.return.qualified_dividends)),
    LTCG:         fulls.map(f => Number(f.return.long_term_capital_gains)),
    STCG:         fulls.map(f => Number(f.return.short_term_capital_gains)),
    Interest:     fulls.map(f => Number(f.return.interest_income)),
    SE:           fulls.map(f => Number(f.return.se_income)),
    Pensions:     fulls.map(f => Number(f.return.pension_distributions_taxable || 0)),
    'IRA dist.':  fulls.map(f => Number(f.return.ira_distributions_taxable || 0)),
    'SS taxable': fulls.map(f => Number(f.result.social_security_taxable || 0)),
    Unemployment: fulls.map(f => Number(f.return.unemployment_compensation || 0)),
    Other:        fulls.map(f => Number(f.return.other_ordinary_income)),
  };
  // Drop all-zero series to keep the legend tidy across older returns.
  for (const k of Object.keys(series)) {
    if (series[k].every(v => v === 0)) delete series[k];
  }
  const colors = ['#34d399','#60a5fa','#3b82f6','#a78bfa','#c084fc','#fbbf24','#f472b6','#22d3ee','#06b6d4','#facc15','#fb923c','#94a3b8'];
  const datasets = Object.entries(series).map(([label, data], i) => ({ label, data, backgroundColor: colors[i % colors.length] }));
  recreate('incomeStack', {
    type: 'bar',
    data: { labels: years, datasets },
    options: {
      maintainAspectRatio: false,
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
    ['Early-wd penalty', Number(r.early_withdrawal_penalty || 0)],
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

function drawTaxStack(fulls) {
  const years = fulls.map(f => f.tax_year);
  const pull = (f, k) => Number(f.result[k] || 0);
  const stateOf = (f) => Number(f.result.state_result ? f.result.state_result.state_tax : 0);
  const credits = (f) => Number(f.result.credits || 0)
                       + Number(f.result.eitc || 0)
                       + Number(f.result.actc || 0)
                       + Number(f.result.aotc_refundable || 0)
                       + Number(f.result.dependent_care_credit_refundable || 0);
  const series = {
    'Ordinary tax':    fulls.map(f => pull(f, 'ordinary_tax')),
    'Qualified tax':   fulls.map(f => pull(f, 'qualified_tax')),
    'AMT':             fulls.map(f => pull(f, 'amt')),
    'SE tax':          fulls.map(f => pull(f, 'se_tax')),
    "Add'l Medicare":  fulls.map(f => pull(f, 'additional_medicare_tax')),
    'NIIT':            fulls.map(f => pull(f, 'niit')),
    'Early-wd pen.':   fulls.map(f => pull(f, 'early_withdrawal_penalty')),
    'State tax':       fulls.map(stateOf),
    'Credits':         fulls.map(f => -credits(f)),
  };
  for (const k of Object.keys(series)) {
    if (series[k].every(v => v === 0)) delete series[k];
  }
  const palette = {
    'Ordinary tax':   '#0f172a',
    'Qualified tax':  '#60a5fa',
    'AMT':            '#ef4444',
    'SE tax':         '#f472b6',
    "Add'l Medicare": '#fbbf24',
    'NIIT':           '#a78bfa',
    'Early-wd pen.':  '#fb923c',
    'State tax':      '#14b8a6',
    'Credits':        '#10b981',
  };
  const datasets = Object.entries(series).map(([label, data]) => ({
    label, data, backgroundColor: palette[label] || '#94a3b8', borderRadius: 3,
  }));
  recreate('taxStack', {
    type: 'bar',
    data: { labels: years, datasets },
    options: {
      maintainAspectRatio: false,
      scales: {
        x: { stacked: true },
        y: { stacked: true, ticks: { callback: v => '$' + (v/1000).toFixed(0) + 'k' } }
      },
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 10 } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${fmt(ctx.parsed.y)}`,
            footer: (items) => {
              const total = items.reduce((s, it) => s + it.parsed.y, 0);
              return `Net liability: ${fmt(total)}`;
            }
          }
        }
      }
    }
  });
}

function drawCarryforwards(fulls) {
  const years = fulls.map(f => f.tax_year);
  const series = {
    'Capital loss':       fulls.map(f => Number(f.result.capital_loss_carryforward_out || 0)),
    'NOL':                fulls.map(f => Number(f.result.nol_carryforward_out || 0)),
    'AMT credit':         fulls.map(f => Number(f.result.amt_credit_carryforward_out || 0)),
    'Foreign tax credit': fulls.map(f => Number(f.result.ftc_carryforward_out || 0)),
    'Charitable':         fulls.map(f => Number(f.result.charitable_carryover_out || 0)),
    'IRA basis (8606)':   fulls.map(f => Number(f.result.ira_basis_out || 0)),
  };
  for (const k of Object.keys(series)) {
    if (series[k].every(v => v === 0)) delete series[k];
  }
  const card = document.getElementById('carryCard');
  if (Object.keys(series).length === 0) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  const palette = {
    'Capital loss':       '#ef4444',
    'NOL':                '#f97316',
    'AMT credit':         '#a78bfa',
    'Foreign tax credit': '#0ea5e9',
    'Charitable':         '#10b981',
    'IRA basis (8606)':   '#6366f1',
  };
  const datasets = Object.entries(series).map(([label, data]) => ({
    label, data,
    borderColor: palette[label] || '#94a3b8',
    backgroundColor: (palette[label] || '#94a3b8') + '33',
    fill: true, tension: 0.25, pointRadius: 3,
  }));
  recreate('carryChart', {
    type: 'line',
    data: { labels: years, datasets },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 10 } },
        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${fmt(ctx.parsed.y)}` } }
      },
      scales: { y: { ticks: { callback: v => '$' + (v/1000).toFixed(0) + 'k' } } }
    }
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

  // Bracket fill — color by rate, tooltip shows both $ in bracket and tax owed
  const fills = r.ordinary_bracket_fills || [];
  const rateColor = (rate) => {
    const r2 = Number(rate);
    if (r2 <= 0.10) return '#10b981';
    if (r2 <= 0.12) return '#34d399';
    if (r2 <= 0.22) return '#facc15';
    if (r2 <= 0.24) return '#fb923c';
    if (r2 <= 0.32) return '#f97316';
    if (r2 <= 0.35) return '#ef4444';
    return '#be123c';
  };
  // Find the last bracket that actually has dollars in it — that's where
  // the marginal dollar of taxable income lands.
  let marginalIdx = -1;
  for (let i = fills.length - 1; i >= 0; i--) {
    if (Number(fills[i].amount_in_bracket) > 0) { marginalIdx = i; break; }
  }
  const marginalRate = marginalIdx >= 0 ? Number(fills[marginalIdx].rate) : 0;
  const marginalPlugin = {
    id: 'marginalMarker',
    afterDatasetsDraw(chart) {
      if (marginalIdx < 0) return;
      const meta = chart.getDatasetMeta(0);
      const bar = meta.data[marginalIdx];
      if (!bar) return;
      const { x, y } = bar.getProps(['x','y'], true);
      const ctx = chart.ctx;
      ctx.save();
      // Marker arrow pointing down at the top of the marginal bar
      ctx.fillStyle = '#0f172a';
      ctx.beginPath();
      ctx.moveTo(x - 6, y - 14);
      ctx.lineTo(x + 6, y - 14);
      ctx.lineTo(x, y - 4);
      ctx.closePath();
      ctx.fill();
      // Pill label above the arrow
      const txt = `marginal ${(marginalRate*100).toFixed(0)}%`;
      ctx.font = '600 11px ui-sans-serif, system-ui, sans-serif';
      const w = ctx.measureText(txt).width + 12;
      ctx.fillStyle = '#0f172a';
      ctx.beginPath();
      const px = x - w/2, py = y - 32;
      const rr = 8;
      ctx.moveTo(px + rr, py);
      ctx.arcTo(px + w, py, px + w, py + 18, rr);
      ctx.arcTo(px + w, py + 18, px, py + 18, rr);
      ctx.arcTo(px, py + 18, px, py, rr);
      ctx.arcTo(px, py, px + w, py, rr);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center';
      ctx.fillText(txt, x, py + 13);
      ctx.restore();
    }
  };
  recreate('brackets', {
    type: 'bar',
    data: {
      labels: fills.map(f => (Number(f.rate)*100).toFixed(0) + '%'),
      datasets: [{
        label: 'Taxable $ in bracket',
        data: fills.map(f => Number(f.amount_in_bracket)),
        backgroundColor: fills.map(f => rateColor(f.rate)),
        borderRadius: 4,
      }]
    },
    options: {
      maintainAspectRatio: false,
      layout: { padding: { top: 36 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => {
              const f = fills[items[0].dataIndex];
              const lo = Number(f.lower || 0);
              const hi = f.upper != null ? Number(f.upper) : null;
              const range = hi != null
                ? `$${(lo/1000).toFixed(0)}k–$${(hi/1000).toFixed(0)}k`
                : `$${(lo/1000).toFixed(0)}k+`;
              return `${(Number(f.rate)*100).toFixed(0)}% bracket · ${range}`;
            },
            label: (item) => {
              const f = fills[item.dataIndex];
              return [
                `In this bracket: ${fmt(f.amount_in_bracket)}`,
                `Tax owed here:   ${fmt(f.tax_in_bracket)}`,
              ];
            }
          }
        }
      },
      scales: { y: { ticks: { callback: v => '$'+(v/1000).toFixed(0)+'k' } } }
    },
    plugins: [marginalPlugin],
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
    ...(Number(r.early_withdrawal_penalty || 0) > 0
        ? [['Early-withdrawal penalty (§72(t))', '+' + r.early_withdrawal_penalty]] : []),
    ['Credits', '-' + r.credits],
    ...(Number(r.eitc || 0) > 0
        ? [['Earned Income Tax Credit (refundable)', '-' + r.eitc]] : []),
    ...(Number(r.aotc_refundable || 0) > 0
        ? [['AOTC refundable portion (Form 8863)', '-' + r.aotc_refundable]] : []),
    ...(Number(r.aotc_nonrefundable || 0) > 0
        ? [['AOTC nonrefundable portion', '-' + r.aotc_nonrefundable]] : []),
    ...(Number(r.llc_credit || 0) > 0
        ? [['Lifetime Learning Credit', '-' + r.llc_credit]] : []),
    ...(Number(r.actc || 0) > 0
        ? [['Additional CTC refundable (Form 8812)', '-' + r.actc]] : []),
    ...(Number(r.savers_credit || 0) > 0
        ? [["Saver's Credit (Form 8880)", '-' + r.savers_credit]] : []),
    ...(Number(r.dependent_care_credit || 0) > 0
        ? [['Child & Dependent Care Credit (Form 2441)', '-' + r.dependent_care_credit]] : []),
    ...(Number(r.dependent_care_credit_refundable || 0) > 0
        ? [['Dependent Care Credit (refundable, TY2021)', '-' + r.dependent_care_credit_refundable]] : []),
    ...(Number(r.residential_clean_energy_credit || 0) > 0
        ? [['Residential Clean Energy Credit (Form 5695)', '-' + r.residential_clean_energy_credit]] : []),
    ...(Number(r.clean_vehicle_credit || 0) > 0
        ? [['Clean Vehicle Credit (Form 8936)', '-' + r.clean_vehicle_credit]] : []),
    ...(Number(r.ptc_net || 0) > 0
        ? [['Premium Tax Credit refund (Form 8962)', '-' + r.ptc_net]] : []),
    ...(Number(r.ptc_excess_aptc_repayment || 0) > 0
        ? [['Excess APTC repayment (Form 8962)', '+' + r.ptc_excess_aptc_repayment]] : []),
    ...(Number(r.student_loan_interest_deduction || 0) > 0
        ? [['Student loan interest deduction (Sch 1)', '-' + r.student_loan_interest_deduction]] : []),
    ...(Number(r.educator_expense_deduction || 0) > 0
        ? [['Educator expenses deduction (Sch 1)', '-' + r.educator_expense_deduction]] : []),
    ...(Number(r.ira_deduction_allowed || 0) > 0
        ? [['Traditional IRA deduction (§219)', '-' + r.ira_deduction_allowed]] : []),
    ['Total federal tax', r.total_tax],
    ...(Number(r.capital_loss_carryforward_out || 0) > 0
        ? [['Cap-loss carried to next year', r.capital_loss_carryforward_out]] : []),
    ...(Number(r.nol_carryforward_out || 0) > 0
        ? [['NOL carried to next year', r.nol_carryforward_out]] : []),
    ...(Number(r.amt_credit_carryforward_out || 0) > 0
        ? [['AMT credit carried (Form 8801)', r.amt_credit_carryforward_out]] : []),
    ...(Number(r.ftc_carryforward_out || 0) > 0
        ? [['Foreign tax credit carried', r.ftc_carryforward_out]] : []),
    ...(Number(r.charitable_carryover_out || 0) > 0
        ? [['Charitable carryover to next year', r.charitable_carryover_out]] : []),
    ...(Number(r.ira_distribution_nontaxable || 0) > 0
        ? [['IRA basis recovered (Form 8606)', '-' + r.ira_distribution_nontaxable]] : []),
    ...(Number(r.ira_basis_out || 0) > 0
        ? [['IRA basis carried to next year', r.ira_basis_out]] : []),
    ...(Number(r.depreciation_current_year || 0) > 0
        ? [['MACRS depreciation (Form 4562)', '-' + r.depreciation_current_year]] : []),
    ...(Number(r.passive_loss_disallowed || 0) > 0
        ? [['Passive loss carried (Form 8582)', r.passive_loss_disallowed]] : []),
    ...(r.state_result ? [[`${r.state_result.state} state tax`, r.state_result.state_tax]] : []),
    ...(r.state_result && r.state_result.locality
        ? [[`${r.state_result.locality} locality tax`, r.state_result.locality_tax]] : []),
    ['Withholding + estimated', String(Number(ret.federal_withholding) + Number(ret.estimated_payments))],
    ['Refund/owed', r.refund_or_owed],
  ].map(([k,v]) => `<div class="border border-slate-200 rounded-lg p-3">
    <div class="text-xs text-slate-500">${k}</div><div class="font-mono text-lg">${fmt(v)}</div></div>`).join('');

  drawSankey(full);
}

// Lightweight SVG Sankey: income sources (left) → tax buckets + take-home (right).
// Flow widths are proportional to (source$ * bucket$ / gross$).
function drawSankey(full) {
  const r = full.result, ret = full.return;
  const SRC = [
    ['Wages',        Number(ret.wages || 0),                                   '#34d399'],
    ['Interest',     Number(ret.interest_income || 0),                         '#fbbf24'],
    ['Qual div',     Number(ret.qualified_dividends || 0),                     '#60a5fa'],
    ['Ord div',      Number(ret.ordinary_dividends || 0) - Number(ret.qualified_dividends || 0), '#3b82f6'],
    ['LTCG',         Number(ret.long_term_capital_gains || 0),                 '#a78bfa'],
    ['STCG',         Number(ret.short_term_capital_gains || 0),                '#c084fc'],
    ['SE',           Number(ret.se_income || 0),                               '#f472b6'],
    ['Pensions',     Number(ret.pension_distributions_taxable || 0),           '#22d3ee'],
    ['IRA dist.',    Number(ret.ira_distributions_taxable || 0),               '#06b6d4'],
    ['SS taxable',   Number(r.social_security_taxable || 0),                   '#facc15'],
    ['Unemployment', Number(ret.unemployment_compensation || 0),               '#fb923c'],
    ['Other',        Number(ret.other_ordinary_income || 0),                   '#94a3b8'],
  ].filter(s => s[1] > 0);

  const fedTax = Number(r.ordinary_tax || 0) + Number(r.qualified_tax || 0)
               + Number(r.collectibles_tax || 0) + Number(r.unrecaptured_1250_tax || 0)
               + Number(r.amt || 0) - Number(r.credits || 0);
  const fica   = Number(r.se_tax || 0) + Number(r.additional_medicare_tax || 0) + Number(r.niit || 0)
               + Number(r.early_withdrawal_penalty || 0);
  const state  = Number(r.state_result ? r.state_result.state_tax : 0);
  const gross  = SRC.reduce((s, x) => s + x[1], 0);
  const totalTax = Math.max(0, fedTax) + fica + state;
  const takeHome = Math.max(0, gross - totalTax);

  const BUCKETS = [
    ['Federal income tax', Math.max(0, fedTax), '#0f172a'],
    ['FICA / SE / NIIT',   fica,                '#475569'],
    ['State tax',          state,               '#14b8a6'],
    ['Take-home',          takeHome,            '#10b981'],
  ].filter(b => b[1] > 0);

  if (gross <= 0) { $('#sankey').innerHTML = '<div class="text-sm text-slate-400 italic">No income data.</div>'; return; }

  const W = 800, H = 280, PAD = 8, COL_W = 140, GAP = 6;
  const totalRight = BUCKETS.reduce((s, b) => s + b[1], 0) || 1;
  const totalLeft  = gross;
  const usableH = H - PAD * 2 - GAP * Math.max(SRC.length, BUCKETS.length);
  const leftScale  = (usableH) / totalLeft;
  const rightScale = (usableH) / totalRight;

  // Layout source rects
  let yL = PAD;
  const srcRects = SRC.map(([label, val, color]) => {
    const h = Math.max(6, val * leftScale);
    const rect = { label, val, color, x: 20, y: yL, w: COL_W, h };
    yL += h + GAP;
    return rect;
  });
  let yR = PAD;
  const bktRects = BUCKETS.map(([label, val, color]) => {
    const h = Math.max(8, val * rightScale);
    const rect = { label, val, color, x: W - 20 - COL_W, y: yR, w: COL_W, h };
    yR += h + GAP;
    return rect;
  });

  // For each source, distribute its outflow across buckets proportionally.
  // Track running offset within each side so flows stack cleanly.
  const srcOff = srcRects.map(() => 0);
  const bktOff = bktRects.map(() => 0);
  const flows = [];
  srcRects.forEach((src, si) => {
    bktRects.forEach((bkt, bi) => {
      const share = (src.val * bkt.val) / (totalLeft * totalRight);
      const thick = share * totalRight * rightScale; // pixels
      if (thick < 0.5) return;
      const y1 = src.y + srcOff[si] + thick / 2;
      const y2 = bkt.y + bktOff[bi] + thick / 2;
      srcOff[si] += thick;
      bktOff[bi] += thick;
      const x1 = src.x + src.w, x2 = bkt.x;
      const cx = (x1 + x2) / 2;
      flows.push({
        d: `M${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`,
        stroke: src.color, width: thick,
      });
    });
  });

  const esc = (s) => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const svg = `
    <svg viewBox="0 0 ${W} ${H}" class="w-full" style="max-height:340px">
      <g stroke-opacity="0.45" fill="none">
        ${flows.map(f => `<path d="${f.d}" stroke="${f.stroke}" stroke-width="${f.width.toFixed(2)}"/>`).join('')}
      </g>
      <g font-size="11" font-family="ui-sans-serif,system-ui" fill="white">
        ${srcRects.map(s => `
          <rect x="${s.x}" y="${s.y}" width="${s.w}" height="${s.h}" rx="4" fill="${s.color}">
            <title>${esc(s.label)}: ${fmt(s.val)}</title>
          </rect>
          <text x="${s.x + s.w/2}" y="${s.y + s.h/2 + 4}" text-anchor="middle" ${s.h < 14 ? 'font-size="9"' : ''}>
            ${esc(s.label)} ${fmt(s.val)}
          </text>`).join('')}
        ${bktRects.map(b => `
          <rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="4" fill="${b.color}">
            <title>${esc(b.label)}: ${fmt(b.val)}</title>
          </rect>
          <text x="${b.x + b.w/2}" y="${b.y + b.h/2 + 4}" text-anchor="middle" ${b.h < 14 ? 'font-size="9"' : ''}>
            ${esc(b.label)} ${fmt(b.val)}
          </text>`).join('')}
      </g>
    </svg>`;
  $('#sankey').innerHTML = svg;
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
  await renderDiff(li, ri);
}

async function renderDiff(li, ri) {
  const panel = document.getElementById('diffPanel');
  panel.classList.remove('hidden');
  try {
    const d = await api(`/api/diff?left=${li}&right=${ri}`);
    const overall = Number(d.overall_tax_delta);
    const sign = overall >= 0 ? '+' : '';
    const color = overall > 0 ? 'text-rose-600' : overall < 0 ? 'text-emerald-600' : 'text-slate-600';
    document.getElementById('diffSummary').innerHTML =
      `Total tax shifted <span class="${color} font-semibold">${sign}${fmt(overall)}</span> ` +
      `(TY${d.left.tax_year} → TY${d.right.tax_year}). Largest contributors:`;
    const maxAbs = Math.max(1, ...d.drivers.map(x => Math.abs(Number(x.attributed_tax || 0))));
    const KIND_COLOR = {
      income:     'bg-sky-500',
      deductions: 'bg-emerald-500',
      credits:    'bg-violet-500',
      payments:   'bg-amber-500',
      rules:      'bg-slate-700',
    };
    const KIND_LABEL = {
      income: 'income', deductions: 'deduction', credits: 'credit',
      payments: 'payment', rules: 'rules',
    };
    document.getElementById('diffDrivers').innerHTML = d.drivers.map(x => {
      const v = Number(x.attributed_tax || 0);
      if (Math.abs(v) < 1) return '';
      const pct = Math.abs(v) / maxAbs * 100;
      const dir = v > 0 ? 'right' : 'left';
      const barColor = KIND_COLOR[x.kind] || 'bg-slate-500';
      const txt = (v > 0 ? '+' : '') + fmt(v);
      const detail = x.kind === 'rules'
        ? ''
        : ` <span class="text-slate-400 text-xs">(${fmt(Number(x.left))} → ${fmt(Number(x.right))})</span>`;
      return `<div class="flex items-center gap-2 text-sm">
        <span class="w-44 truncate" title="${x.label}">${x.label}${detail}</span>
        <div class="flex-1 relative h-5 bg-slate-100 rounded">
          <div class="absolute top-0 ${dir === 'right' ? 'left-1/2' : 'right-1/2'} h-5 ${barColor} rounded" style="width:${pct/2}%"></div>
          <div class="absolute inset-0 border-l border-slate-300" style="left:50%"></div>
        </div>
        <span class="w-24 text-right font-mono ${v > 0 ? 'text-rose-600' : 'text-emerald-600'}">${txt}</span>
        <span class="w-20 text-xs text-slate-400">${KIND_LABEL[x.kind] || x.kind}</span>
      </div>`;
    }).join('');
    const resid = Number(d.residual || 0);
    document.getElementById('diffResidual').textContent = Math.abs(resid) >= 1
      ? `Unattributed residual (bracket-crossing & non-linear interactions): ${resid > 0 ? '+' : ''}${fmt(resid)}`
      : 'All deltas attributed cleanly.';
  } catch (e) {
    panel.classList.add('hidden');
  }
}

// ─── boot ──────────────────────────────────────────────────────────────────
refreshAll();


// --- advisor -------------------------------------------------------------
const SEV_COLOR = {
  high:      'bg-rose-100 text-rose-700',
  suggested: 'bg-amber-100 text-amber-700',
  info:      'bg-sky-100 text-sky-700',
};
const CAT_ICON = {
  retirement: '🏖️', deductions: '🧾', investments: '📈',
  structure: '🏛️',  compliance: '⚠️',
};

async function renderAdvisor() {
  const data = await api('/api/advisor');

  const allRecs = [...data.cross_year, ...data.per_year.flatMap(p => p.recommendations)];
  const totalSavings = allRecs.reduce((s, r) => s + Number(r.est_annual_savings || 0), 0);
  const highCount = allRecs.filter(r => r.severity === 'high').length;

  $('#advisorSummary').innerHTML = [
    advTile('Total opportunities', allRecs.length, 'across all returns'),
    advTile('High-priority items', highCount, 'flagged for immediate action'),
    advTile('Est. annual savings', '$' + Math.round(totalSavings).toLocaleString(), 'rough headline number'),
  ].join('');

  $('#advisorCross').innerHTML = data.cross_year.length
    ? data.cross_year.map(recCard).join('')
    : '<div class="text-sm text-slate-500 italic md:col-span-2">No cross-year patterns yet — import at least 2 years.</div>';

  $('#advisorPerYear').innerHTML = data.per_year.length
    ? data.per_year.map(p => {
        const count = p.recommendations.length;
        const body = count
          ? '<div class="grid md:grid-cols-2 gap-4">' + p.recommendations.map(recCard).join('') + '</div>'
          : '<div class="text-sm text-slate-500 italic">No opportunities found for this year. Nicely optimized!</div>';
        return [
          '<div class="bg-white border border-slate-200 rounded-2xl p-5">',
          '  <div class="flex items-center justify-between mb-3">',
          '    <h4 class="font-semibold text-slate-800">Tax year ' + p.tax_year + '</h4>',
          '    <span class="text-xs text-slate-500">' + count + ' suggestion' + (count === 1 ? '' : 's') + '</span>',
          '  </div>',
          body,
          '</div>',
        ].join('');
      }).join('')
    : '<div class="text-sm text-slate-500 italic">Import a return to see year-specific advice.</div>';
}

function advTile(label, value, sub) {
  return [
    '<div class="bg-white border border-slate-200 rounded-2xl p-4">',
    '  <div class="text-xs uppercase tracking-wide text-slate-500">' + label + '</div>',
    '  <div class="text-2xl font-bold mt-1">' + value + '</div>',
    '  <div class="text-xs text-slate-500 mt-1">' + sub + '</div>',
    '</div>',
  ].join('');
}

function recCard(r) {
  const savings = Number(r.est_annual_savings || 0);
  const sevClass = SEV_COLOR[r.severity] || 'bg-slate-100 text-slate-700';
  const icon = CAT_ICON[r.category] || '💡';
  const refs = (r.references || []).join(' · ');
  const savingsStr = savings > 0 ? '$' + Math.round(savings).toLocaleString() : '—';
  return [
    '<div class="border border-slate-200 rounded-xl p-4 bg-white">',
    '  <div class="flex items-start gap-3">',
    '    <div class="text-2xl">' + icon + '</div>',
    '    <div class="flex-1">',
    '      <div class="flex items-center gap-2 flex-wrap">',
    '        <h5 class="font-semibold text-slate-800">' + r.title + '</h5>',
    '        <span class="text-[10px] px-2 py-0.5 rounded-full ' + sevClass + ' uppercase font-bold tracking-wide">' + r.severity + '</span>',
    '      </div>',
    '      <div class="text-sm text-slate-600 mt-2">' + r.rationale + '</div>',
    '      <div class="text-sm mt-2"><span class="font-semibold text-emerald-700">Action:</span> <span class="text-slate-700">' + r.action + '</span></div>',
    '      <div class="flex items-baseline justify-between mt-3 pt-3 border-t border-slate-100">',
    '        <div class="text-xs text-slate-500">' + refs + '</div>',
    '        <div class="text-right">',
    '          <div class="text-[10px] uppercase tracking-wide text-slate-400">Est. annual savings</div>',
    '          <div class="font-mono font-bold text-emerald-600">' + savingsStr + '</div>',
    '        </div>',
    '      </div>',
    '    </div>',
    '  </div>',
    '</div>',
  ].join('');
}

// --- demo loader ---------------------------------------------------------
const demoBtn = document.getElementById('loadDemoBtn');
if (demoBtn) {
  demoBtn.addEventListener('click', async () => {
    demoBtn.disabled = true;
    demoBtn.textContent = 'Loading 3 sample returns…';
    try {
      const out = await api('/api/demo/load', { method: 'POST' });
      demoBtn.textContent = `✓ Loaded ${out.count} demo returns — switching to Dashboard`;
      await refreshAll();
      setTimeout(() => showTab('dashboard'), 600);
    } catch (e) {
      demoBtn.disabled = false;
      demoBtn.textContent = '❌ Demo load failed — see console';
      console.error(e);
    }
  });
}

// --- planner tab ---------------------------------------------------------
function populatePlanYearPicker() {
  const sel = document.getElementById('planYearPicker');
  if (!sel) return;
  sel.innerHTML = RETURNS.map(r =>
    '<option value="' + r.id + '">' + r.tax_year + '</option>'
  ).join('');
}

function _fmtMoney(n) {
  const v = Number(n);
  const sign = v < 0 ? '-' : '+';
  return sign + '$' + Math.abs(v).toLocaleString(undefined, {maximumFractionDigits: 0});
}

function _fmtPct(n) {
  return (Number(n) * 100).toFixed(2) + '%';
}

function _renderSim(targetId, out) {
  const cls = Number(out.tax_delta) < 0 ? 'text-emerald-700' : 'text-rose-700';
  const lines = [
    '<div class="font-semibold ' + cls + ' text-lg">' + _fmtMoney(out.tax_delta) + ' federal tax</div>',
    '<div class="text-slate-500 text-xs mt-1">' + out.scenario + '</div>',
    '<div class="mt-3 text-slate-700">Effective marginal rate: <span class="font-mono">' + _fmtPct(out.federal_marginal_rate) + '</span></div>',
    '<div class="mt-2 text-slate-500 text-xs">Original total tax: $' + Number(out.original.total_tax).toLocaleString() + ' → after: $' + Number(out.after.total_tax).toLocaleString() + '</div>',
  ];
  document.getElementById(targetId).innerHTML = lines.join('');
}

async function runRoth() {
  const id = Number(document.getElementById('planYearPicker').value);
  const amount = document.getElementById('rothAmount').value;
  document.getElementById('rothResult').textContent = 'Simulating…';
  try {
    const out = await api('/api/returns/' + id + '/simulate/roth',
      { method: 'POST', body: JSON.stringify({ amount }) });
    _renderSim('rothResult', out);
  } catch (e) {
    document.getElementById('rothResult').textContent = 'Error: ' + e.message;
  }
}

async function runTLH() {
  const id = Number(document.getElementById('planYearPicker').value);
  const loss_amount = document.getElementById('tlhAmount').value;
  document.getElementById('tlhResult').textContent = 'Simulating…';
  try {
    const out = await api('/api/returns/' + id + '/simulate/tlh',
      { method: 'POST', body: JSON.stringify({ loss_amount }) });
    _renderSim('tlhResult', out);
  } catch (e) {
    document.getElementById('tlhResult').textContent = 'Error: ' + e.message;
  }
}

function renderPlan() {
  populatePlanYearPicker();
}

const _rothBtn = document.getElementById('rothRun');
if (_rothBtn) _rothBtn.addEventListener('click', runRoth);
const _tlhBtn = document.getElementById('tlhRun');
if (_tlhBtn) _tlhBtn.addEventListener('click', runTLH);

// ─── Trends ─────────────────────────────────────────────────────────────────
async function renderTrends() {
  if (RETURNS.length === 0) {
    document.getElementById('trendsAgiTax').innerHTML =
      '<text x="20" y="40" font-size="14" fill="#94a3b8">Import returns to see trends.</text>';
    document.getElementById('trendsRates').innerHTML = '';
    document.getElementById('trendsStack').innerHTML = '';
    document.getElementById('trendsTaxStack').innerHTML = '';
    document.getElementById('trendsYoyTable').innerHTML = '';
    return;
  }
  const fulls = await Promise.all(RETURNS.map(r => loadFull(r.id)));
  const years = fulls.map(f => f.return.tax_year);

  const agi = fulls.map(f => Number(f.result.agi));
  const tax = fulls.map(f => Number(f.result.total_tax));
  drawLineChart('trendsAgiTax', years, [
    { label: 'AGI',       data: agi, color: '#0ea5e9' },
    { label: 'Total tax', data: tax, color: '#ef4444' },
  ], { yfmt: fmt });

  const eff = fulls.map(f => {
    const a = Number(f.result.agi) || 1;
    return Number(f.result.total_tax) / a * 100;
  });
  const marg = fulls.map(f => Number(f.result.marginal_rate || 0) * 100);
  drawLineChart('trendsRates', years, [
    { label: 'Effective %', data: eff,  color: '#0ea5e9' },
    { label: 'Marginal %',  data: marg, color: '#f59e0b' },
  ], { yfmt: v => v.toFixed(1) + '%' });

  // Stacked income composition
  const buckets = [
    { key: 'wages',                     label: 'Wages',       color: '#0ea5e9' },
    { key: 'interest_income',           label: 'Interest',    color: '#22c55e' },
    { key: 'ordinary_dividends',        label: 'Div (ord)',   color: '#84cc16' },
    { key: 'qualified_dividends',       label: 'Div (qual)',  color: '#10b981' },
    { key: 'short_term_capital_gains',  label: 'STCG',        color: '#f97316' },
    { key: 'long_term_capital_gains',   label: 'LTCG',        color: '#a855f7' },
    { key: 'self_employment_income',    label: 'SE',          color: '#ec4899' },
    { key: 'rental_real_estate_income', label: 'Rental',      color: '#14b8a6' },
  ];
  const series = buckets.map(b => ({
    label: b.label,
    color: b.color,
    data: fulls.map(f => Math.max(0, Number(f.return[b.key] || 0))),
  }));
  drawStackedBars('trendsStack', years, series);

  // Stacked tax composition (positive = tax owed pieces; reflects what drove total_tax)
  const taxBuckets = [
    { key: 'tax_before_credits',      label: 'Ordinary + qual',    color: '#0ea5e9' },
    { key: 'amt_tax',                 label: 'AMT add-on',         color: '#f59e0b' },
    { key: 'self_employment_tax',     label: 'SE tax',             color: '#ec4899' },
    { key: 'niit',                    label: 'NIIT 3.8%',          color: '#a855f7' },
    { key: 'additional_medicare_tax', label: 'Addl Medicare',      color: '#f97316' },
    { key: 'ptc_excess_aptc_repayment', label: 'Excess APTC',      color: '#dc2626' },
  ];
  const taxSeries = taxBuckets.map(b => ({
    label: b.label,
    color: b.color,
    data: fulls.map(f => Math.max(0, Number(f.result[b.key] || 0))),
  }));
  drawStackedBars('trendsTaxStack', years, taxSeries);

  // YoY delta table
  const yoyRows = [
    { label: 'AGI',           pick: f => Number(f.result.agi) },
    { label: 'Taxable income',pick: f => Number(f.result.taxable_income) },
    { label: 'Total tax',     pick: f => Number(f.result.total_tax) },
    { label: 'Effective rate',pick: f => { const a = Number(f.result.agi) || 1; return Number(f.result.total_tax) / a * 100; }, isPct: true },
    { label: 'Refund / owed', pick: f => Number(f.result.refund_or_owed) },
    { label: 'Wages',         pick: f => Number(f.return.wages || 0) },
    { label: 'Cap gains (LT)',pick: f => Number(f.return.long_term_capital_gains || 0) },
    { label: 'Credits',       pick: f => Number(f.result.credits || 0) },
  ];
  const head = '<tr class="text-slate-500"><th class="text-left py-1 pr-3">Metric</th>' +
    years.map(y => `<th class="text-right pr-3">${y}</th>`).join('') +
    years.slice(1).map((y, i) => `<th class="text-right pr-3">Δ ${years[i]}→${y}</th>`).join('') +
    '</tr>';
  const body = yoyRows.map(row => {
    const vals = fulls.map(row.pick);
    const cells = vals.map(v => `<td class="text-right pr-3">${row.isPct ? v.toFixed(2)+'%' : fmt(v)}</td>`);
    const deltas = vals.slice(1).map((v, i) => {
      const d = v - vals[i];
      const cls = d > 0 ? 'text-rose-600' : d < 0 ? 'text-emerald-600' : 'text-slate-400';
      const sign = d > 0 ? '+' : '';
      const txt = row.isPct ? `${sign}${d.toFixed(2)} pp` : `${sign}${fmt(d)}`;
      return `<td class="text-right pr-3 ${cls}">${txt}</td>`;
    });
    return `<tr class="border-t border-slate-100"><td class="py-1 pr-3 font-medium">${row.label}</td>${cells.join('')}${deltas.join('')}</tr>`;
  }).join('');
  document.getElementById('trendsYoyTable').innerHTML =
    `<table class="w-full"><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

function drawLineChart(svgId, xs, series, opts = {}) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const W = 600, H = 280, P = { l: 60, r: 20, t: 20, b: 40 };
  const innerW = W - P.l - P.r, innerH = H - P.t - P.b;
  const all = series.flatMap(s => s.data);
  const ymin = Math.min(0, ...all);
  const ymax = Math.max(...all, 1);
  const xpos = i => P.l + (xs.length === 1 ? innerW / 2 : (i / (xs.length - 1)) * innerW);
  const ypos = v => P.t + innerH - ((v - ymin) / (ymax - ymin)) * innerH;
  const yfmt = opts.yfmt || (v => String(v));
  let html = '';
  // Y gridlines
  for (let i = 0; i <= 4; i++) {
    const v = ymin + (ymax - ymin) * (i / 4);
    const y = ypos(v);
    html += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="#e2e8f0"/>`;
    html += `<text x="${P.l - 6}" y="${y + 4}" font-size="10" fill="#64748b" text-anchor="end">${yfmt(v)}</text>`;
  }
  // X labels
  xs.forEach((x, i) => {
    html += `<text x="${xpos(i)}" y="${H - P.b + 16}" font-size="11" fill="#475569" text-anchor="middle">${x}</text>`;
  });
  // Lines
  series.forEach(s => {
    const d = s.data.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xpos(i)} ${ypos(v)}`).join(' ');
    html += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2"/>`;
    s.data.forEach((v, i) => {
      html += `<circle cx="${xpos(i)}" cy="${ypos(v)}" r="3" fill="${s.color}"/>`;
    });
  });
  // Legend
  series.forEach((s, i) => {
    const x = P.l + i * 140, y = P.t - 4;
    html += `<rect x="${x}" y="${y}" width="10" height="10" fill="${s.color}"/>`;
    html += `<text x="${x + 14}" y="${y + 9}" font-size="11" fill="#334155">${s.label}</text>`;
  });
  svg.innerHTML = html;
}

function drawStackedBars(svgId, xs, series) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const W = 800, H = 320, P = { l: 70, r: 140, t: 20, b: 40 };
  const innerW = W - P.l - P.r, innerH = H - P.t - P.b;
  const totals = xs.map((_, i) => series.reduce((s, ser) => s + (ser.data[i] || 0), 0));
  const ymax = Math.max(1, ...totals);
  const barW = innerW / Math.max(xs.length, 1) * 0.6;
  const slot  = innerW / Math.max(xs.length, 1);
  const ypos = v => P.t + innerH - (v / ymax) * innerH;
  let html = '';
  // Y gridlines
  for (let i = 0; i <= 4; i++) {
    const v = ymax * (i / 4);
    const y = ypos(v);
    html += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="#e2e8f0"/>`;
    html += `<text x="${P.l - 6}" y="${y + 4}" font-size="10" fill="#64748b" text-anchor="end">${fmt(v)}</text>`;
  }
  xs.forEach((x, i) => {
    const cx = P.l + slot * (i + 0.5);
    let acc = 0;
    series.forEach(s => {
      const v = s.data[i] || 0;
      if (v <= 0) return;
      const y0 = ypos(acc + v), y1 = ypos(acc);
      html += `<rect x="${cx - barW / 2}" y="${y0}" width="${barW}" height="${Math.max(0, y1 - y0)}" fill="${s.color}"><title>${s.label}: ${fmt(v)}</title></rect>`;
      acc += v;
    });
    html += `<text x="${cx}" y="${H - P.b + 16}" font-size="11" fill="#475569" text-anchor="middle">${x}</text>`;
  });
  // Legend
  series.forEach((s, i) => {
    const x = W - P.r + 10, y = P.t + i * 18;
    html += `<rect x="${x}" y="${y}" width="10" height="10" fill="${s.color}"/>`;
    html += `<text x="${x + 14}" y="${y + 9}" font-size="11" fill="#334155">${s.label}</text>`;
  });
  svg.innerHTML = html;
}
