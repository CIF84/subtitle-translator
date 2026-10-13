# Subtitle Translator v1.2 MVP — local web guide

**MVP accepted — 2026-10-13.** v1.1 was released on 2026-10-13. The recommended browser app runs on this Mac at **http://127.0.0.1:8000**. Safari functional acceptance verified upload, cost approval, real translation, optional revision, accept/reject/edit, refresh recovery, and validated export. The CLI remains supported. v1.2 adds review-only bulk actions and flexible export; AI engines, pricing, checkpoints and spending guards are unchanged.

## Install, launch, and stop

Use the existing Python 3.12+ environment:

```bash
cd "$HOME/Documents/Subtitle Translator"
.venv/bin/python -m pip install -r requirements-web.txt
open "Start Subtitle Translator.command"
```

For a fresh installation only, create `.venv` with `python3.12 -m venv .venv`. API keys stay server-side in `OPENAI_API_KEY` or the project's local `.env`; never enter a key in the browser. An empty key produces a configuration error before any API request.

Double-click the Start shortcut in Finder if preferred. Keep its Terminal open. Starting again opens the existing localhost server. Stop with **Stop Subtitle Translator.command** or **Ctrl+C** in Terminal. An active batch may finish, including bounded retries, before shutdown. After this update, stop and relaunch once, then refresh Safari so the new read-only history route is available. Port 8000 must be free of other applications.

## Normal workflow

**Upload → Configure → Estimate → Translate → Review → Save** appears above the main workspace. The active stage and next action are emphasized; completed stages are marked. Upload, configuration, and completed progress collapse to reduce clutter. Their headings and stage buttons let you inspect them again without starting paid work.

1. Drop/select valid UTF-8 `.srt` (up to 5 MiB and 10,000 cues). Filename and validated cue count appear in the sidebar.
2. Review languages, models, and the total job limit, then save settings. Defaults remain English → Czech, GPT-5.4 mini, batch ceiling 25, reasoning none, simple schema, $1.00. Revision is disabled initially.
3. Choose **Estimate translation**. Review the wide approval card, selected models, costs and existing spending.
4. Explicitly check the consent box, then choose **Approve translation & start**. Approval starts unchecked. **No, cancel** approves nothing. The workflow button only brings approval into view; it never bypasses consent.
5. Translation runs in the background. Expand **Job progress** for completed cues, actual translation/revision costs, remaining budget, and reservations. Download the untouched translation when ready.
6. Optionally enable revision in configuration (GPT-5.4 default), save settings, estimate revision, and **approve it separately**. Suggestions do not change the untouched translation automatically.
7. Review individually or confirm a bulk action. **Save reviewed SRT** requires every decision; **Export current decisions** permits undecided suggestions to keep original Czech. Download a browser copy.

Languages and the translation model freeze after checkpointed work begins. The revision model freezes after its first paid request. Eligible settings, including an explicit budget increase while idle, remain editable. Earlier spending is never reset by changing settings or resuming.

## What the costs mean

The approval card separates:

- **Estimated normal cost:** expected usage for this operation, including repeated prompts/context and translated output. It is an estimate, not a guaranteed bill.
- **Maximum authorized spending:** the combined total job limit, including translation, optional revision, earlier attempts, and retries.
- **Previously observed comparable cost:** shown only when matching complete local execution records exist. It is historical evidence, not a universal episode price or prediction.

Revision's expected-cost card now says **calibrated** when matching verified measurements are available, otherwise **heuristic**. It shows sample/cue counts, measurement dates and model/configuration match, with a warning that sensitivity ranges are not guaranteed bounds. The optional calibration checkbox changes the forecast display only; it clears consent and never changes request allowances or the budget. Expand **How estimates work** for the unchanged conservative scenario and original all-cues-edited proxy. See [calibrated revision costs](CALIBRATED-REVISION-COSTS.md) for eligibility, calculations and safe browser verification.

Actual spending, remaining budget, and unsettled reservations are displayed separately. A prospective revision estimate before translation is labeled provisional; it does not approve revision.

Expand **How estimates work** for token estimates and the **extreme conservative scenario**. That scenario assumes maximal context/glossary growth, token consumption, and all permitted retries. It is not the authorized budget and is not necessarily required for normal completion. The application-level guard checks actual cost, unknown-usage reservations, and the next request allowance before each request. Work may pause before completion if the request cannot fit. This guard is **not an absolute provider billing cap**.

Historical eligibility uses the same source content, languages, model, compatible prompt/settings, verified accounting, and complete accepted-work coverage. The most recent eligible record is shown. Failed attempts are excluded and successful probes are identified when included. Revision history also requires an identical untouched translated baseline. Incompatible, partial, corrupt, or unavailable evidence is omitted. The current job is not its own historical comparison; there is no hardcoded MobLand baseline. For resumed v1 records, accepted requests are matched to their recorded configuration history rather than assuming the original ledger identity represents the final successful settings.

## Review controls and formatting

The desktop workspace compares **Original English | Current Czech | Suggested Czech**, with distinct backgrounds for current and suggested text. Smaller screens stack the comparison. Compare the explanation with the actual dialogue; model confidence never accepts a suggestion.

Use **Accept**, **Reject**, or **Edit** for each suggestion. Filter by category, navigate previous/next, and expand **View surrounding dialogue**. Counters cover all categories, including hidden suggestions.

Keyboard controls: **Alt+A** accept, **Alt+R** reject, **Alt+E** edit, **← / →** navigate, **Ctrl/⌘+Enter** save an edit. Ordinary typing in input fields is not intercepted.

Preview safely renders italic, bold, underline, and font colors with quoted allowlisted names or hexadecimal values. Line breaks, speaker labels, and SDH descriptions remain visible. **Show SRT markup** reveals the exact underlying text. Unsupported/malformed formatting and ASS positioning commands remain literal; arbitrary HTML, event attributes, links, scripts, and external resources are never rendered. Font face/size are not simulated by the small preview renderer.

Editing always uses the original raw SRT text, including tags. Save the edit to persist it; an unsaved typing draft is not stored. Preview toggles do not change the draft or saved text. Formatting and speaker checks still apply on save. The existing **Save reviewed SRT** requires an explicit decision for every suggestion. **Export current decisions** is separate and preserves original Czech for undecided cues. Both validate IDs, ordering, timestamps, UTF-8, supported formatting and speaker separation. Zero suggestions export the untouched translation. New exports preserve previous files.

## Bulk actions, partial export and undo

The buttons beside the review summary operate across **all categories**:

- **Accept all remaining:** confirm the displayed count and warning about applying
  unreviewed AI proposals. Existing accepts, rejects and edits stay unchanged.
- **Reject all remaining:** confirm the count; those cues keep the original Czech.
- **Export current decisions:** confirm accepted/rejected/edited/undecided counts.
  Accepted uses suggested Czech; edited uses your text; rejected and undecided use
  the original Czech. Undecided stays undecided. The review status is unchanged.
- **Undo latest bulk action:** confirm restoration of the saved pre-bulk snapshot.
  New bulk decisions become undecided again; prior decisions/edits remain. It is
  disabled if a later individual decision changed or the snapshot was restored.

All dialogs focus **Cancel** first. No bulk action or partial export runs before
explicit confirmation. Bulk buttons disable when no undecided cues remain. You
can still change individual decisions after any bulk action; doing so disables
undo to prevent overwriting those newer decisions. A new bulk operation replaces
its predecessor's one-level snapshot. Finish or cancel an unsaved edit before
opening these dialogs. Keyboard shortcuts remain available outside the dialog.

Decisions and the snapshot commit atomically in the existing job file; there is no
second review-state store. Current-state tokens and execution locks reject stale,
duplicate or concurrent submissions. A stale message means refresh and inspect the
new counts; nothing is silently retried. Refresh, restart and switching jobs retain
saved state. A failed save cannot produce a partially updated decision map.

Current-decision exports end in `.current.srt` under `output/revised/`, separately
from the original translation and fully reviewed `.revised.srt`. Download with
**Download current-decisions SRT**. Re-exporting unchanged decisions reuses that
validated export; changing decisions creates a new filename. Later decisions clear
its current download link but leave earlier files on disk. A storage error can
leave a complete unlinked export; no incomplete SRT is published.

### Safe browser verification

Stop the existing idle server and wait for exit, then:

```bash
cd "$HOME/Documents/Subtitle Translator"
open "Start Subtitle Translator.command"
```

Refresh Safari and use a disposable review job to verify bulk actions, decisions
across refresh/restart, undo and flexible export. Preserve historical completed jobs.
Browser acceptance verified bulk acceptance and flexible export on the real episode:
816 cues translated/reviewed, 237 suggestions, approximately $0.56149 combined cost.
The first minute passed video playback; full-episode linguistic review remains pending.

## Local storage and recovery

```text
input/web/<job-id>/                  uploaded originals
output/translated/                  untouched translations
output/revised/                     reviewed exports
checkpoints/web/<job-id>/            translation work and usage
jobs/<job-id>/                       decisions, revision ledger, pricing snapshots,
                                    redacted response/audit evidence
```

Files are written atomically with collision-safe names. Historical v1/WP2 files and spending ledgers remain in their original locations. Private job data is ignored by version control. These local files can contain subtitle dialogue; no credentials or arbitrary local paths are exposed to browser JavaScript.

Choose a saved job after refresh or restart. Paid work never restarts automatically. Decisions persist on disk; the current stage is derived from the saved job. Configuration-review markers are a browser convenience and do not authorize work. To resume a paused/failed job, inspect its message, request a fresh estimate, and explicitly approve recovery. Duplicate starts remain blocked.

Do not delete or alter checkpoints/ledgers to recover: missing files, changed source/output, incompatible identities, and corrupt spending fail closed. Unknown usage stays reserved rather than refunded. Completed reviews use billing snapshots offline; paid operations require current verified pricing (older than 30 days is rejected). An invalid/incomplete revision response retains charged usage and local evidence without automatic retry.

## Troubleshooting and offline tests

- **Empty key/model access:** check the local server configuration and account access, restart, then estimate/approve recovery. A key exported in another Terminal is not inherited by the existing launcher.
- **Paused for budget:** inspect actual cost and reservations. Increase the original total limit only if you choose to authorize more, then estimate again.
- **Refresh before submitting:** refresh after restart for a new local session; saved decisions remain.
- **History absent:** no eligible comparison exists, or the server needs a restart after the UI update. This does not affect estimates or spending guards.
- **Invalid edit/response:** preserve tags and speaker separation, and inspect redacted local diagnostics. Never delete spending records to retry.

```bash
cd "$HOME/Documents/Subtitle Translator"
env -u OPENAI_API_KEY PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider
.venv/bin/python -B translate_srt.py tests/fixtures/synthetic.en.srt --dry-run
```

Tests mock API calls and block network access. Node is used only by offline frontend tests (a bundled Codex runtime is detected when available); it is not an application runtime dependency. One existing nonfatal Starlette TestClient deprecation warning may appear. Browser decisions do not synchronize live across multiple tabs. Structural validation and historical cost comparisons do not prove translation accuracy or guarantee a future bill.
