# Subtitle Translator v1.2 MVP

**MVP accepted — 2026-10-13.** v1.1 was released on 2026-10-13. Translate SRT subtitles locally with the official
OpenAI SDK. The **local web interface is the recommended way to use the app**:
upload, approve estimated costs, translate, optionally review AI suggestions, and
save validated subtitles. The original CLI remains a supported alternative.

Defaults: English → Czech, `gpt-5.4-mini`, **25 entries per batch**, reasoning
**`none`**, **`simple` response schema**, five context entries on each side,
two bounded retries and a **$1.00 total job budget**. Spending always requires approval.

## Installation on macOS

Use Python 3.12 or newer:

```sh
brew install python@3.12
cd "$HOME/Documents/Subtitle Translator"
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-web.txt
python -m pytest -q
```

Skip Homebrew if a supported Python is already installed. For an existing
installation, retain `.venv` and install the web requirements with
`.venv/bin/python -m pip install -r requirements-web.txt`.
The web app adds FastAPI and Uvicorn to the OpenAI SDK and tiktoken; pytest is for
offline tests. CLI-only installations can use `requirements.txt` instead.
The tested environment used Python 3.14.6 and OpenAI SDK 2.54.0.

## API key

Set `OPENAI_API_KEY` in your local environment or project `.env` file. No key is
needed for dry runs or tests. To create the local file:

```sh
cd "$HOME/Documents/Subtitle Translator"
if [ ! -f .env ]; then cp .env.example .env; fi
chmod 600 .env
nano .env
```

Enter `OPENAI_API_KEY=...` locally. Environment values take precedence. Never
share or commit `.env`; keys, raw API errors and tracebacks are never printed.

## Recommended usage: local web interface

After installation and local API key configuration:

```sh
cd "$HOME/Documents/Subtitle Translator"
open "Start Subtitle Translator.command"
```

You can also double-click **Start Subtitle Translator.command** in Finder. It
opens the app at **http://127.0.0.1:8000**, bound to localhost only. Starting again
opens the existing server. After updating a running installation, stop and relaunch
once, then refresh Safari. Stop with **Stop Subtitle Translator.command** or
**Ctrl+C** in its Terminal window.

1. Upload a valid UTF-8 `.srt` and review the languages, models and total budget.
2. Estimate translation, then explicitly consent and approve. Uploading or
   estimating never starts paid work.
3. Follow progress and spending; the completed translation stays unmodified.
4. Optionally enable revision, estimate it and approve it separately. GPT-5.4 is
   the default revision model; revision is disabled initially.
5. Accept, reject or edit individual suggestions. Optionally confirm **Accept all
   remaining** or **Reject all remaining**; saved decisions and edits are preserved.
   Choose **Export current decisions** to save before finishing review: undecided
   suggestions retain their original Czech text and remain undecided. **Save reviewed
   SRT** keeps its existing all-decisions-required behavior. AI suggestions are
   applied only through your explicit decisions/confirmation.

The cost card distinguishes **estimated normal cost**, **maximum authorized
spending**, and eligible **previously observed comparable cost**. The expandable
extreme conservative scenario is not the authorized budget. Estimates and the
application guard are not provider billing guarantees.

Jobs and decisions survive refresh/restart. Paid recovery always requires a new
estimate and explicit approval. Originals are stored in `input/web/`, untouched
translations in `output/translated/`, reviewed exports in `output/revised/`, and
recovery/accounting data in `checkpoints/web/` and `jobs/`. Export uses validated,
atomic writes and collision-safe filenames.

See [the web interface guide](docs/WEB-INTERFACE.md) for review shortcuts, safe
formatting previews, costs, recovery and troubleshooting.

## Bulk review and flexible export (v1.2 MVP)

Bulk actions cover all undecided suggestions across all categories and show the
number affected before confirmation. **Cancel** is the default. Accepting remaining
suggestions applies AI proposals you have not individually reviewed; rejection keeps
original Czech. Existing accepted, rejected and edited decisions are never replaced.

**Export current decisions** shows accepted/rejected/edited/undecided counts and
requires confirmation. Accepted cues use the proposal, edited cues use your text,
and rejected/undecided cues use the untouched translation. Export never marks
undecided suggestions as rejected. Files are validated and saved with collision-safe
names; originals and older exports remain intact.

**Undo latest bulk action** restores the persistent pre-operation decision snapshot
only if no newer decision changed. A new bulk action replaces the previous undo
snapshot. Individual decisions remain editable. Review state persists across refresh,
restart and saved-job switching. Stale confirmations are refused; refresh and review
current counts before confirming again. Save or cancel an unsaved draft first.

## Browser acceptance verified

Successful functional acceptance in Safari confirmed localhost launch, SRT upload,
cost estimation and explicit approval, real GPT-5.4 mini translation, optional
GPT-5.4 revision with separate approval, accept/reject/edit decisions, decision
recovery after browser refresh, and validated SRT export. The existing CLI remained
operational. These are prior browser integration results; release closure uses
only offline tests and makes no paid API calls.

The accepted v1.2 MVP processed **816 cues**, reviewed **816 cues**, and produced
**237 suggestions**, for approximately **$0.56149 combined API cost**. Bulk acceptance
and flexible export were verified. Real video playback passed for the **first minute**;
**full-episode linguistic review remains pending**.

## Supported alternative: CLI

Activate the environment, place a subtitle file in `input/`, and run:

```sh
cd "$HOME/Documents/Subtitle Translator"
source .venv/bin/activate
python translate_srt.py input/Movie.en.srt
```

That is the complete translation command: no batch, reasoning or schema flags
are needed. Output defaults to `output/Movie.en.cs.srt`. The script preserves
original identifiers, timestamp lines and entry ordering, and writes valid UTF-8
SRT atomically after final validation.

First check structure and estimated cost without any API calls:

```sh
python translate_srt.py input/Movie.en.srt --dry-run
```

Review the estimate and type **y** or **yes** at `Proceed? [y/N]:` to approve a
real run. Enter, EOF and all other answers decline. After approval, remaining
batches run automatically. There is no automatic approval flag.

## Verified v1 CLI production results

The English → Czech production test used the defaults above:

| Result | Verified value |
| --- | ---: |
| Subtitles in the completed file | 816 |
| Successful run elapsed time | 135.2 seconds |
| Successful translation run cost | Approximately $0.112 |
| Total including development experiments | $0.198280 |

The checkpoint's exact successful normal-run cost is $0.11216175; exact cumulative
cost is $0.19827975, rounded above. The completed file includes five entries reused
from the successful probe. Elapsed time is the reported Terminal measurement.
Counts and usage costs were checked against the completed output and checkpoint.
These results describe one file; future costs and times will vary.

## CLI overrides

All three tested settings remain configurable:

```sh
python translate_srt.py input/Movie.en.srt --batch-size 50
python translate_srt.py input/Movie.en.srt --reasoning-effort low
python translate_srt.py input/Movie.en.srt --response-schema constrained
python translate_srt.py input/Movie.en.srt --target de
python translate_srt.py input/Movie.de.srt --source de --target cs
python translate_srt.py input/Movie.en.srt --model gpt-5.4 --max-cost 2.00
python translate_srt.py --help
```

Batch size accepts 1–500 entries; context accepts 0–50 per side; retries accept
0–5. Source-length estimates may split batches below the requested entry ceiling.
`--max-output-tokens` sets an explicit per-request cap including JSON and reasoning;
requests that cannot fit are rejected locally. Reasoning overrides `none` and `low`
are verified for the configured GPT-5.4 models; unverified overrides fail closed.

The simple schema (v4) uses one shared entry object, allowed string IDs and plain
text fields. The constrained schema (v3) retains per-ID branches and text patterns.
Both use the same production validator for entry counts/IDs, nonempty text, valid
formatting, speaker separation, SDH and glossary content. Neither generates timestamps.
Prompts are centralized in `SYSTEM_PROMPT` with a version and content fingerprint.

## Pricing and budget

`pricing.json` contains versioned USD rates, source URLs and a last-verified date.
Unknown pricing fails closed. Before adding a model, verify its rates, context and
output limits, JSON support and reasoning configuration; retain documented sources
and update the verification date. No model substitution happens silently.

Preflight includes repeated prompts/schema/context, JSON overhead, glossary growth,
output allowance and bounded retries. It makes no API calls. Cached tiktoken data
improves local estimates; otherwise the CLI labels its UTF-8 byte heuristic. An
optional tokenizer-data download is free of API charges:

```sh
python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'
```

The default **$1.00 applies to the entire job**, including probes, failed validation,
retries and resumed invocations. Before each request the guard reserves its
conservative cost. Returned input/output/cached token usage settles actual cost;
unknown usage keeps its reservation. A budget stop preserves completed work.
`--max-cost 2.00` explicitly changes the total budget.

Estimates are not guaranteed bills. The guard operates within this application;
it is not an absolute provider billing cap or an account-wide spending limit.
Requests use the standard service tier, the official endpoint and `store=False`.
Pricing sources remain recorded in `pricing.json`.

## Resume

After an interruption, retain the original input and checkpoint and run:

```sh
python translate_srt.py input/Movie.en.srt --resume
python translate_srt.py input/Movie.en.srt --resume --dry-run
```

Resume shows prior spending and additional estimates before requiring approval.
Source hash, languages, model, context, prompt fingerprint and pricing protect
compatibility. Supported scheduling/reasoning/schema changes share the original
ledger and record approved configuration changes. Never delete or restore an
older checkpoint to reset costs. Checkpoints and outputs are written atomically.
A completed job can be inspected read-only with `--resume --dry-run`.

## Probes and local diagnostics

For a controlled single-request test:

```sh
python translate_srt.py input/Movie.en.srt --resume --probe 5 --debug-responses
```

Omit `--resume` for a new job. A probe translates the first N untranslated entries,
uses the production prompt/validator, defaults to a 4096-token output cap, requires
approval, disables retries and stops after one request. Accepted translations are
displayed and checkpointed for reuse; no partial SRT is published.

`--debug-responses` is opt-in. Failed generated text, output-block boundaries,
status, incomplete reason, token usage, cost/reservation and schema/version are
saved locally in `debug_responses/` beside the checkpoint directory. Directory
permissions are 700, atomic files are 600, and the directory is ignored by Git.
SDK objects, headers, exception/refusal bodies and account metadata are excluded;
known credentials and recognizable credential strings are redacted. Captures can
still contain sensitive subtitle dialogue; keep them local. Raw failed text is
not printed to Terminal. Dry runs, declined approval and successful responses
create no capture files. Capture write failures do not change acceptance or billing.

## Validation, troubleshooting and limitations

- The web app supports local single-user use, not public hosting. Uploads are
  limited to 5 MiB and 10,000 cues. Use the CLI for supported workflows outside
  these web upload limits.
- Saved review decisions and the latest bulk snapshot persist, but unsaved edit drafts do not. Decisions do
  not synchronize live across multiple browser tabs. Save edits before refreshing.
- Safe previews render italic, bold, underline and supported font colors; font
  face/size and ASS positioning are not simulated. Raw SRT markup remains
  available, and preview rendering never changes stored text.
- Historical costs appear only for eligible comparable local records. Model
  access/pricing can change; paid web operations require current verified pricing.
  Unknown API usage remains reserved. Do not delete spending records to recover.

- IDs, entry counts, timestamp lines and ordering are checked in the final output.
  Tags must balance, retain attributes and preserve styling. Speaker turns,
  attribution, SDH brackets and music markers are protected. Ordinary wrapping,
  punctuation and word order may change. Outer whitespace is trimmed only from
  new responses; internal blank lines and empty translations fail.
- Non-UTF-8 input, duplicate numeric IDs, reversed timestamps and empty cues fail
  locally. CRLF and BOM input are accepted; output uses UTF-8 with LF and no BOM.
  Malformed source markup needs correction before translation.
- HTTP/network failures use bounded backoff. Truncation/refusals stop immediately;
  identical repeated validation failures stop. Costs and unresolved reservations
  remain in the checkpoint. Fix the reported cause before resuming.
- For authentication/access failures, check your local key and account access.
  For budget stops, inspect usage before explicitly changing `--max-cost`.
  Unknown pricing or incompatible/corrupt checkpoints fail closed.
- Structural checks cannot prove translation accuracy. Long/untranslated-text
  checks are warnings; inspect meaning, tone, names and accessibility content.
  Speaker/SDH classification uses heuristic vocabularies, the glossary is bounded
  to 40 terms, and terminology/character-voice consistency is best-effort.
- Model responses can still truncate or contain invalid content. The successful
  production test is evidence for this configuration, not a guarantee for every
  language, subtitle file or API response.

Run all offline tests with `python -m pytest -q`. API requests are mocked and
socket access is blocked. No paid calls were made during v1.1 release closure. See
[CHANGELOG.md](CHANGELOG.md) for release notes. No GitHub repository or infrastructure is needed.

## Source checkout and offline reproducibility

Place the source checkout in `$HOME/Documents/Subtitle Translator` (or change the
`cd` commands to its location), then follow the installation commands above. A
checkout starts without subtitles, credentials, historical jobs, spending ledgers
or calibration evidence. The app creates runtime directories when needed. For
CLI uploads, create `input/` with `mkdir -p input` before copying your own SRT there.
No API key is needed for the offline suite; it uses authored synthetic fixtures,
mocked SDK calls and a network-blocking fixture. Install Node to include the optional
frontend checks: `brew install node`. Node is not required to run the application.

Dependency ranges declare compatible packages rather than a locked environment.
The verified versions and proposed Git tracking/exclusion policy are documented in
[Git preparation](docs/GIT-PREPARATION.md). Standalone research runners need their
private benchmark/job inputs; those historical records are deliberately excluded
from Git. Core web/CLI operation and all tests require no private episode data.
Calibration is optional: a clean checkout uses the heuristic estimate until valid
local matching measurements exist. Never copy a real `.env` into a repository.

Git has not been initialized or published. Initialization, staging, commits and the existing
public remote require separate explicit approval. Ignoring files is not encryption
or a backup: retain private subtitles, checkpoints and spending records locally.

Public-source fixtures use independently authored dialogue, fictional speakers and
synthetic timestamps. The standalone calibration harness has no embedded historical
job identifier: use `python revision_calibration.py --prepare --job JOB_ID` for a new
local registration. Replace `JOB_ID` locally with the chosen compatible job; never
commit its private job files. For a registered series, sample/dry-run commands can
omit `--job` and reuse the saved selection. Choosing a different job cannot reset
calibration spending. The production web/CLI translation and revision engines are
unchanged; calibration still uses their exact requests and validators.
