/* app.js — minimal UI interactions only. No business logic. */

/* ── Row expand/collapse ────────────────────────────────────── */
function toggleDetail(recordId) {
  var detail = document.getElementById('detail-' + recordId);
  var icon   = document.getElementById('icon-'   + recordId);
  if (!detail) return;
  var isHidden = detail.style.display === 'none';
  detail.style.display = isHidden ? 'table-row' : 'none';
  if (icon) icon.textContent = isHidden ? '▼' : '▶';
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
  document.querySelectorAll('.record-row').forEach(function(row) {
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

/* ── Toggle override reason field ───────────────────────────── */
function toggleReasonField(recordId) {
  var select = document.getElementById('override-' + recordId);
  var group  = document.getElementById('reason-group-' + recordId);
  if (!select || !group) return;
  group.style.display = select.value ? '' : 'none';
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
  var reason   = (document.getElementById('reason-'   + recordId) || {}).value || null;
  var reviewEl = document.getElementById('review-'    + recordId);
  var qcStatus = (reviewEl && reviewEl.dataset.qcStatus) ||
                 _currentQC(recordId) || 'pending';

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
}

