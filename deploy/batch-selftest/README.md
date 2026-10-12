# Batch-view selftest — letters as the unit of action

Regression for the letter view (2026-10-12): a *batch = the sheets of one
letter*; every user-facing action applies to the whole letter; the only
per-scan actions are removing/restoring a scan or a single side (blank backs).
No hardware; temp DB + temp dirs.

    cd ~/BUTLER && python3 deploy/batch-selftest/selftest.py

Last run 2026-10-12: **ALL PASS (49 checks)**.

Covers:
- one board card per letter (trashed letters hidden; header letter/scan counts)
- derived letter status: inbox → analyzing (progress on the card) → review /
  noise / user-move wins
- letter-wide status moves: `POST /api/batch/<id>/status` (JSON + form) sets
  `batches.board_status` and syncs every scan's status; validation + 404s
- per-scan removal: whole scan and single sides; flags, letter-page reflection
  ("back removed" + restore), board counters, removed-scan list with restore;
  all-removed letter shows "no scans" under noise
- analyzer respects exclusions: pending list filters, removed sides dropped
  from the analysis images/prompt (fake-LLM capture), fully-removed scan
  analyzes as a clean no-op
- letter page: scans table with thumbs + remove buttons, status form, trash,
  payment block, aggregated tasks (scan column), docs with scan refs
- scan page: letter-status form (posts to the batch), removal banner + restore,
  old per-sheet status route is gone (404)
