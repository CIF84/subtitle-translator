# Accepted MVP and Git preparation

The v1.2 MVP was accepted on **2026-10-13**. Acceptance covers 816 cues translated
and reviewed, 237 suggestions, browser bulk acceptance/flexible export and the first
minute of video playback. Combined API cost was approximately $0.56149.
**Full-episode linguistic review remains pending.**

## Proposed tracking set

- `translate_srt.py`, `revision_experiment.py`, `revision_calibration.py`.
- `web/` Python modules and `web/static/` HTML/CSS/JavaScript.
- `tests/` Python tests, the frontend check script, and authored synthetic fixtures.
- `requirements.txt`, `requirements-web.txt`, `pricing.json`, `.env.example`, `.gitignore`.
- README, CHANGELOG, this guide, `docs/WEB-INTERFACE.md`, and
  `docs/CALIBRATED-REVISION-COSTS.md` (aggregate measurements; no episode dialogue).
- Start/Stop `.command` launchers, retaining their executable permissions.
- Empty `input/.gitkeep`, `output/.gitkeep`, `checkpoints/.gitkeep` placeholders.

The experimental Python modules are required by shared production imports; excluding
these source files would break the web app. Their standalone harnesses still need
private historical inputs. For a new calibration, select a local job explicitly with `--job`; an existing
series reuses its saved selection. No historical identifier is embedded in source.
No completed subtitles, personal episode benchmark, real evaluation references,
response captures, job state or accounting ledger belongs in the tracking set.

## Exclusions and preservation

The root `.gitignore` excludes credentials/environments, caches, logs, macOS/editor
metadata, uploaded/generated subtitles, checkpoints, jobs, spending ledgers,
experimental responses and calibration evidence. Research documents containing
private dialogue or detailed job provenance are individually excluded. Synthetic
fixtures remain included; there is no blanket `.srt` exclusion.

Exclusions do not delete files or back them up, encrypt them, or retroactively remove
already tracked secrets. Keep original private data locally and back it up separately.
Derived forecasts and reports are reproducible only from retained local authoritative
records. Paid model outputs themselves cannot be guaranteed bit-for-bit reproducible.

## Fresh checkout

Choose a destination matching the README commands, or use your checkout directory
instead of `$HOME/Documents/Subtitle Translator`. Install Python 3.12+, create
`.venv`, and install `requirements-web.txt` as described in README. HTTPX is explicitly
declared for the offline SDK transport tests. Node is optional at runtime but needed
for all frontend test checks (`brew install node`). Launchers require executable
permissions; if an archive loses them, run:

```sh
chmod +x "Start Subtitle Translator.command" "Stop Subtitle Translator.command"
```

Create `.env` from the empty template only when preparing an explicitly approved paid
run. A fresh checkout has no jobs or comparable history/calibration: it uses existing
heuristic forecasts and unchanged spending guards. Standalone experiments need local
private datasets, but normal web/CLI use and the full offline suite do not.

```sh
env -u OPENAI_API_KEY PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider
.venv/bin/python -B translate_srt.py tests/fixtures/synthetic.en.srt --dry-run
```

Verified dependency versions: Python 3.14.6, OpenAI 2.54.0, tiktoken 0.14.0,
HTTPX 0.28.1, FastAPI 0.143.0, Starlette 1.7.0, Uvicorn 0.54.0, pytest 9.1.1.
Requirements use compatibility ranges, not an environment lock; future package/model
changes may need revalidation. API access, a configured key and current verified
pricing are needed only for paid operations. Tests block network access and use
mocked API calls.

## Public-content remediation and next approval

The public-release audit found episode-derived regression snippets and an embedded
historical job identifier in the original candidate source. These were remediated
using independently authored terrarium, mineral and video-call examples, fictional
speaker labels and synthetic timestamps. Formatting/Unicode/error assertions retain
their coverage. New calibration registrations use `revision_calibration.py --prepare
--job JOB_ID`, where `JOB_ID` is chosen locally; existing series read their registered
job from the unchanged excluded `selection.json`. Mismatched jobs fail closed.

The source project remains uninitialized. A separate user-created GitHub directory
now has an initialized repository, the intended origin, and an untracked `.gitignore`.
That file was inspected and preserved. Its exclusions are narrower: experimental and
calibration directories, private benchmarks/research docs and temporary records need
review before any future synchronization. It also ignores the empty data-directory
placeholders included in the source manifest. Do not overwrite its ignore file or
copy runtime data there. Reconcile those differences explicitly before staging.

The intended destination is the existing **public** `CIF84/subtitle-translator`
repository. All source and Git history would be public; previous private-remote
advice is superseded. After separate publication approval, inspect remote history,
use only the re-audited manifest, scan exact staged/outgoing content, run offline
tests, and verify destination/visibility before any non-forced push. Use account/SSH
authentication; never include tokens in URLs or local personal details in commits.

This remediation performs no Git initialization, staging, commits or publication.
Historical local subtitles, decisions, checkpoints and accounting must stay local.
