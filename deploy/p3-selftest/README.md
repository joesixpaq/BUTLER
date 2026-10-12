# P3 selftest — tasks, reminders, payments

Automated regression for the P3 layer (deadlines → reminders, Swiss QR payment
prep). No hardware, no network; runs against a temp DB and temp dirs.

    cd ~/BUTLER && python3 deploy/p3-selftest/selftest.py

Deps: `zxing-cpp` (QR decode; user-site — `python3 -m pip install --user
--break-system-packages zxing-cpp`). Without it, the D-checks skip gracefully.

Last run 2026-10-12: **ALL PASS (85 checks)**.

Covers:
- **A** date parsing (dd.mm.yyyy, „31. Juli 2026", ISO, ranges, invalid) + fmt
- **B** Swiss QR (SPC) parsing: full 33-field payload, LF endings, trailing
  fields omitted, IBAN/QRR/SCOR formatting
- **C** cross-check vs analysis facts: amount, account-tail (QR-IBAN vs printed
  IBAN note), reference; mismatch + not-comparable cases
- **D** REAL decode of the live Post CH payment slip (`~/scans/direct/
  pull_20261011_235058`; skipped gracefully if absent) + idempotent upsert that
  keeps a `paid` status
- **E** deadline-fact extraction: create, idempotent re-run, trashed-batch
  guard, long-past backfill pre-silenced
- **F** reminder engine: offsets 14/7/3/1 on a walk-down, catch-up when slots
  were missed, real-days wording, single overdue notice, failed send retried
  (not marked), dry mode marks nothing, done/cancelled/trashed ignored,
  payment line attached to reminders
- **G** .ics: CRLF, all-day DTSTART/DTEND (+1d), UID, summary, −P1D alarm
- **H** board: card chips (due / pay), header counter, sheet-page blocks +
  copy block, task add/status APIs, payment paid API, `/ics/<id>` route

The live sheet used by D is the user's real mail — this file keeps only its
path and expected values (no mail content is stored in the repo).
