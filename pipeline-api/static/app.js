/* app.js — minimal UI interactions only. No business logic. */

/* ── Scroll back to the record after a re-crawl redirect ───────── */
document.addEventListener('DOMContentLoaded', function() {
  var params = new URLSearchParams(window.location.search);
  var rid = params.get('scrollto');
  if (!rid) return;
  var row = document.querySelector('.record-row[data-rid="' + rid + '"]');
  if (!row) return;
  /* expand the detail panel */
  var detail = document.getElementById('detail-' + rid);
  var icon   = document.getElementById('icon-'   + rid);
  if (detail && detail.style.display === 'none') {
    detail.style.display = 'table-row';
    if (icon) icon.textContent = '▼';
  }
  /* scroll the row into view with a short delay so layout is settled */
  setTimeout(function() {
    row.scrollIntoView({behavior: 'smooth', block: 'center'});
  }, 120);
  /* clean the param from the URL without a reload */
  params.delete('scrollto');
  var newUrl = window.location.pathname + (params.toString() ? '?' + params.toString() : '');
  history.replaceState(null, '', newUrl);
});

/* ── Disable all re-crawl submit buttons while a request is in flight ── */
function lockRecrawlForms(label) {
  document.querySelectorAll('.recrawl-form button[type=submit], .manual-content-form button[type=submit]')
    .forEach(function(btn) {
      btn.disabled = true;
      btn.classList.add('btn-working');
      btn.textContent = label || 'Processing…';
    });
}

/* ── Browser re-crawl: submit in the background and show live % progress ──
   The server blocks until the record is merged; we stay on the page, poll the
   crawl's step progress, paint it into the button, then jump to the fresh card. */
var _recrawlPoll = null;
function submitRecrawl(form, event) {
  event.preventDefault();
  if (form.dataset.submitting === '1') return false;
  form.dataset.submitting = '1';
  var runId = form.dataset.runId;
  var recordId = form.dataset.recordId;
  var btn = form.querySelector('button[type=submit]');
  var target = '/dashboard/' + runId + '?scrollto=' + recordId;
  lockRecrawlForms('Starting…');

  if (_recrawlPoll) clearInterval(_recrawlPoll);
  _recrawlPoll = setInterval(function() {
    fetch('/runs/' + runId + '/recrawl-progress', {headers: {'Accept': 'application/json'}})
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(d) {
        if (d && d.active && btn) {
          btn.textContent = (d.step_name || 'Working') + ' · ' + d.percent + '%';
        }
      })
      .catch(function() {});
  }, 1500);

  function done() {
    if (_recrawlPoll) { clearInterval(_recrawlPoll); _recrawlPoll = null; }
    window.location = target;
  }
  fetch(form.action, {method: 'POST', body: new FormData(form), redirect: 'follow'})
    .then(done).catch(done);
  return false;
}

/* ── Row expand/collapse ────────────────────────────────────── */
function toggleDetail(recordId) {
  var detail = document.getElementById('detail-' + recordId);
  var icon   = document.getElementById('icon-'   + recordId);
  if (!detail) return;
  var isHidden = detail.style.display === 'none';
  detail.style.display = isHidden ? 'table-row' : 'none';
  if (icon) icon.textContent = isHidden ? '▼' : '▶';
}

/* ── Stat block filter ──────────────────────────────────────── */
function filterByStatBlock(tier, el) {
  var isActive = el.classList.contains('stat-active');
  var bar = el.closest('.stats-bar');

  document.querySelectorAll('.stats-bar .stat-item').forEach(function(s) {
    s.classList.remove('stat-active');
  });

  if (isActive || tier === 'all') {
    _applyFilter(function() { return true; });
    if (bar) bar.classList.remove('filter-active');
    return;
  }

  el.classList.add('stat-active');
  if (bar) bar.classList.add('filter-active');

  if (tier === 'excluded') {
    _applyFilter(function() { return true; });
    var section = document.getElementById('excluded-section');
    if (section) section.scrollIntoView({behavior: 'smooth', block: 'start'});
    return;
  }
  if (tier === 'pending') {
    _applyFilter(function(row) { return row.dataset.qc === 'pending'; });
    return;
  }
  _applyFilter(function(row) { return row.dataset.tier === tier; });
}

/* ── Client-side tier filter ────────────────────────────────── */
function filterRecords(tier, btn) {
  _setActiveFilter(btn);
  _applyFilter(function(row) {
    return tier === 'all' || row.dataset.tier === tier;
  });
}

function filterByQC(qc, btn) {
  _setActiveFilter(btn);
  _applyFilter(function(row) {
    return row.dataset.qc === qc;
  });
}

function _setActiveFilter(btn) {
  document.querySelectorAll('.filter-btn').forEach(function(b) {
    b.classList.remove('active');
  });
  if (btn) btn.classList.add('active');
}

function _applyFilter(predicate) {
  document.querySelectorAll('#results-table .record-row').forEach(function(row) {
    var recordId = row.querySelector('.expand-icon') &&
                   row.querySelector('.expand-icon').id &&
                   row.querySelector('.expand-icon').id.replace('icon-', '');
    var show = predicate(row);
    row.style.display = show ? '' : 'none';
    /* also hide open detail rows for hidden records */
    if (recordId) {
      var detail = document.getElementById('detail-' + recordId);
      if (detail && !show) detail.style.display = 'none';
    }
  });
}

/* ── Column sorting ─────────────────────────────────────────── */
/* Each record is two rows: a .record-row and its hidden .detail-row.
   Sorting reorders these pairs together so detail panels stay attached. */
var _sortState = {key: null, dir: 1};

function sortRecords(key, th) {
  var tbody = document.querySelector('#results-table tbody');
  if (!tbody) return;

  /* toggle direction when re-clicking the same column */
  if (_sortState.key === key) {
    _sortState.dir = -_sortState.dir;
  } else {
    _sortState.key = key;
    _sortState.dir = 1;
  }
  var dir = _sortState.dir;

  /* collect (record-row, detail-row) pairs */
  var pairs = [];
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('.record-row'));
  rows.forEach(function(row) {
    var rid = row.dataset.rid || '';
    var detail = rid ? document.getElementById('detail-' + rid) : null;
    pairs.push({row: row, detail: detail});
  });

  var numeric = (key === 'score' || key === 'tier');
  pairs.sort(function(a, b) {
    var av = a.row.dataset['sort' + key.charAt(0).toUpperCase() + key.slice(1)] || '';
    var bv = b.row.dataset['sort' + key.charAt(0).toUpperCase() + key.slice(1)] || '';
    if (numeric) {
      return (parseFloat(av || 0) - parseFloat(bv || 0)) * dir;
    }
    return av.localeCompare(bv) * dir;
  });

  pairs.forEach(function(p) {
    tbody.appendChild(p.row);
    if (p.detail) tbody.appendChild(p.detail);
  });

  /* update header arrows */
  document.querySelectorAll('#results-table th.sortable').forEach(function(h) {
    h.classList.remove('sort-asc', 'sort-desc');
  });
  if (th) th.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
}

/* ── Toggle override reason field ───────────────────────────── */
function toggleReasonField(recordId) {
  var select = document.getElementById('override-' + recordId);
  var group  = document.getElementById('reason-group-' + recordId);
  if (!select || !group) return;
  group.style.display = select.value ? '' : 'none';
}

function toggleOtherReason(recordId) {
  var sel = document.getElementById('reason-' + recordId);
  var inp = document.getElementById('reason-other-' + recordId);
  if (!sel || !inp) return;
  inp.style.display = sel.value === '__other__' ? '' : 'none';
}

/* ── Set QC status via button click ─────────────────────────── */
function setQC(recordId, status) {
  var btns = document.querySelectorAll('#review-' + recordId + ' .qc-btn');
  btns.forEach(function(btn) {
    btn.classList.remove('btn-qc-active-approved', 'btn-qc-active-rejected', 'btn-qc-active-pending');
  });
  var map = { approved: 'btn-qc-active-approved', rejected: 'btn-qc-active-rejected', pending: 'btn-qc-active-pending' };
  var btn = document.querySelector('[onclick="setQC(\'' + recordId + '\', \'' + status + '\')"]');
  if (btn && map[status]) btn.classList.add(map[status]);
  /* store in a data attribute so saveReview() can read it */
  var reviewEl = document.getElementById('review-' + recordId);
  if (reviewEl) reviewEl.dataset.qcStatus = status;
}

/* ── Save review via fetch() ────────────────────────────────── */
function saveReview(runId, recordId) {
  var statusEl = document.getElementById('save-status-' + recordId);
  if (statusEl) { statusEl.textContent = 'Saving…'; statusEl.className = 'save-status'; }

  var note     = (document.getElementById('note-'     + recordId) || {}).value || '';
  var override = (document.getElementById('override-' + recordId) || {}).value || null;
  var reasonSel   = document.getElementById('reason-' + recordId);
  var reasonOther = document.getElementById('reason-other-' + recordId);
  var reason = null;
  if (reasonSel && reasonSel.value) {
    reason = reasonSel.value === '__other__'
      ? ((reasonOther && reasonOther.value.trim()) || null)
      : reasonSel.value;
  }
  var reviewEl = document.getElementById('review-'    + recordId);
  var qcStatus = (reviewEl && reviewEl.dataset.qcStatus) ||
                 _currentQC(recordId) || 'pending';

  /* Setting a positive override tier implies the operator wants this record included.
     Auto-promote to approved so they don't have to click the button separately. */
  if (override && override.toLowerCase() !== 'excluded' && qcStatus === 'pending') {
    qcStatus = 'approved';
    setQC(recordId, 'approved');
  }

  var payload = {
    analyst_note:    note,
    override_tier:   override || null,
    override_reason: reason   || null,
    qc_status:       qcStatus,
  };

  fetch('/api/ui/reviews/' + runId + '/' + encodeURIComponent(recordId), {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(payload),
  })
  .then(function(res) { return res.json().then(function(data) { return { ok: res.ok, data: data }; }); })
  .then(function(result) {
    if (!statusEl) return;
    if (result.ok) {
      statusEl.textContent = 'Saved ✓';
      statusEl.className = 'save-status ok';
      /* update the QC badge in the main row */
      _updateRowBadges(recordId, payload.override_tier, qcStatus);
      /* collapse the detail row after a brief pause so the analyst can see the confirmation */
      setTimeout(function() {
        window.location.reload();
      }, 900);
    } else {
      var msg = (result.data && result.data.detail) || 'Save failed.';
      statusEl.textContent = msg;
      statusEl.className = 'save-status error';
    }
    setTimeout(function() {
      if (statusEl) { statusEl.textContent = ''; statusEl.className = 'save-status'; }
    }, 4000);
  })
  .catch(function(err) {
    if (statusEl) {
      statusEl.textContent = 'Network error. Try again.';
      statusEl.className = 'save-status error';
    }
  });
}

function _currentQC(recordId) {
  var reviewEl = document.getElementById('review-' + recordId);
  if (reviewEl && reviewEl.dataset.qcStatus) return reviewEl.dataset.qcStatus;
  /* fallback: read active button class */
  var active = document.querySelector('#review-' + recordId + ' .qc-btn[class*="active"]');
  if (!active) return 'pending';
  if (active.classList.contains('btn-qc-active-approved')) return 'approved';
  if (active.classList.contains('btn-qc-active-rejected')) return 'rejected';
  return 'pending';
}

/* ── Phone detail modal ─────────────────────────────────────── */
function showPhoneModal(el, event) {
  event.stopPropagation(); /* prevent row expand */
  var phone = el.dataset.phone || '';
  var hours = el.dataset.hours || '';
  if (!phone) return;

  var existing = document.getElementById('phone-modal-overlay');
  if (existing) existing.remove();

  var digits = phone.replace(/\D/g, '');
  var telHref = (digits.length === 11 && digits[0] === '1') ? digits : digits;

  var hoursHtml = hours
    ? '<p style="margin:10px 0 0"><span class="eyebrow">Hours</span><br><span style="font-size:14px">' + _escHtml(hours) + '</span></p>'
    : '<p style="color:#888;font-size:13px;margin:10px 0 0">Hours not listed on website.</p>';

  var overlay = document.createElement('div');
  overlay.id = 'phone-modal-overlay';
  overlay.className = 'modal-overlay';
  overlay.innerHTML =
    '<div class="modal" style="max-width:320px" onclick="event.stopPropagation()">' +
    '<p style="margin:0 0 2px"><span class="eyebrow">Phone</span></p>' +
    '<h3 style="margin:0 0 8px">' + _escHtml(phone) + '</h3>' +
    '<a href="tel:+' + _escHtml(telHref) + '" class="btn btn-secondary btn-sm">Call</a>' +
    hoursHtml +
    '<div class="modal-actions"><button class="btn btn-secondary" onclick="document.getElementById(\'phone-modal-overlay\').remove()">Close</button></div>' +
    '</div>';

  overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
}

function _escHtml(s) {
  var d = document.createElement('div');
  d.appendChild(document.createTextNode(String(s)));
  return d.innerHTML;
}

/* The upload pre-flight preview modal is driven by the page-local script in
   templates/upload.html, which renders full EXACT/SIMILAR duplicate detail. It
   lived here too and double-bound the submit handler (two preview fetches per
   submit); removed to leave a single owner. */

function _updateRowBadges(recordId, overrideTier, qcStatus) {
  var rows = document.querySelectorAll('.record-row');
  rows.forEach(function(r) {
    var icon = r.querySelector('#icon-' + recordId);
    if (icon) {
      r.dataset.qc = qcStatus;
      var qcBadge = r.querySelector('.badge-qc-pending, .badge-qc-approved, .badge-qc-rejected');
      if (qcBadge) {
        qcBadge.className = 'badge badge-qc-' + qcStatus;
        qcBadge.textContent = qcStatus;
      }
      /* update displayed tier if override changed */
      if (overrideTier !== undefined) {
        var tierBadge = r.querySelector('[class*="badge-tier-"]');
        if (tierBadge && overrideTier) {
          tierBadge.className = 'badge badge-tier-' + overrideTier.toLowerCase();
          tierBadge.textContent = overrideTier;
          r.dataset.tier = overrideTier.toLowerCase();
        }
      }
    }
  });
  if (overrideTier !== undefined) {
    _reconcileRecordTable(recordId, overrideTier);
  }
}

function _reconcileRecordTable(recordId, overrideTier) {
  var row = document.querySelector('.record-row[data-rid="' + recordId + '"]');
  if (!row) return;
  var currentTable = row.closest('table');
  if (!currentTable) return;
  var inExcluded = currentTable.id === 'excluded-table';
  var pipelineExcluded = row.dataset.pipelineExcluded === 'true';
  var wantExcluded = overrideTier && overrideTier.toLowerCase() === 'excluded';
  /* never move a pipeline-excluded record back to the main table */
  var wantMain = !pipelineExcluded && !wantExcluded;
  if (wantExcluded && !inExcluded) {
    _moveRecord(recordId, true);
  } else if (wantMain && inExcluded) {
    _moveRecord(recordId, false);
  }
}

function _moveRecord(recordId, toExcluded) {
  var row = document.querySelector('.record-row[data-rid="' + recordId + '"]');
  var detail = document.getElementById('detail-' + recordId);
  if (!row) return;
  var targetId = toExcluded ? 'excluded-table' : 'results-table';
  var targetTbody = document.querySelector('#' + targetId + ' tbody');
  if (!targetTbody) return;
  /* collapse the detail panel before moving so it doesn't flash open */
  if (detail && detail.style.display !== 'none') {
    detail.style.display = 'none';
    var icon = document.getElementById('icon-' + recordId);
    if (icon) icon.textContent = '▶';
  }
  targetTbody.appendChild(row);
  if (detail) targetTbody.appendChild(detail);
  /* show/hide the excluded section container */
  var section = document.getElementById('excluded-section');
  var excBody = document.querySelector('#excluded-table tbody');
  if (section) {
    section.style.display = (excBody && excBody.querySelector('.record-row')) ? '' : 'none';
  }
  /* update the count badge */
  var badge = document.getElementById('excluded-count-badge');
  if (badge && excBody) {
    badge.textContent = excBody.querySelectorAll('.record-row').length;
  }
}

