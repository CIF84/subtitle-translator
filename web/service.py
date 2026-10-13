"""Small persistent job service built on the unchanged translation functions."""
from collections import Counter
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import argparse
import json
import re
import sys
import threading
import time
import uuid

import translate_srt as tr
import revision_experiment as rev

MAX_UPLOAD = 5 * 1024 * 1024
MAX_ENTRIES = 10000
DEFAULTS = dict(source='en', target='cs', translation_model='gpt-5.4-mini',
                revision_enabled=False, revision_model='gpt-5.4', max_cost='1.00')
BUSY = {'translation_queued', 'translation_running', 'revision_queued', 'revision_running'}
REVISION_VERSION = 'wp3-revision-1.0'


class WebError(tr.TranslationError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def filename(value):
    if not isinstance(value, str) or not value.lower().endswith('.srt') or any(c in value for c in '/\\\x00\r\n'):
        raise WebError('Choose a .srt filename without directory components.')
    value = re.sub(r'[^\w .()\[\]-]', '_', value, flags=re.UNICODE).lstrip('. ')
    if len(value.encode('utf-8')) > 150:
        value = value.encode('utf-8')[:146].decode('utf-8',errors='ignore') + '.srt'
    if not value or not value.lower().endswith('.srt'):
        raise WebError('Invalid filename.')
    return value


def safe_path(root, *parts):
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise WebError('Unsafe local file path.',409)
    for component in (path,*path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise WebError('Symlinked local storage is not supported.',409)
    return path


def save(path, data):
    tr.atomic_write(path, json.dumps(data, ensure_ascii=True, allow_nan=False, indent=2) + '\n')


def read(path):
    try:
        return rev.read_json(path)
    except tr.TranslationError:
        raise WebError('Local job data is unreadable. It was preserved; do not delete its spending records.', 409) from None


def prices(root, model):
    data, price = tr.load_pricing(root / 'pricing.json', model)
    if (date.today() - date.fromisoformat(data['last_verified'])).days > 30:
        raise WebError('Pricing verification is older than 30 days. Reverify pricing.json before spending.')
    tr.effective_reasoning(tr.Config(model=model), price)
    return data, price


class Jobs:
    def __init__(self, root, client_factory=None, executor=None):
        if sys.version_info < (3,12):
            raise WebError('Python 3.12 or newer is required.')
        self.root = Path(root).resolve()
        self.directory = safe_path(self.root,'jobs')
        if self.directory.is_symlink():
            raise WebError('Unsafe jobs directory.')
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.client_factory = client_factory or self._client
        self.pool = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix='subtitles')
        self.lock = threading.RLock()
        self.recover()

    def _client(self):
        from openai import OpenAI
        key = tr.load_api_key(self.root / '.env')
        if not key:
            raise WebError("API key is empty. Configure OPENAI_API_KEY in the server environment or the project's .env file. No API request was sent.")
        return OpenAI(api_key=key,
                      base_url='https://api.openai.com/v1', max_retries=0, timeout=180.0)

    def folder(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{32}', ident):
            raise WebError('Unknown job.', 404)
        folder = safe_path(self.root,'jobs',ident)
        if folder.is_symlink() or not folder.is_dir():
            raise WebError('Unknown job.', 404)
        return folder

    def job(self, ident):
        job = read(self.folder(ident) / 'job.json')
        if job['id'] != ident:
            raise WebError('Job identity is incompatible.', 409)
        return job

    def persist(self, job):
        save(self.folder(job['id']) / 'job.json', job)

    def checkpoint(self, ident):
        return safe_path(self.root,'checkpoints','web',ident,'translation.json')

    def source(self, job):
        name = filename(job['filename'])
        if name != job['filename']:
            raise WebError('Source filename is incompatible.', 409)
        path = safe_path(self.root,'input','web',job['id'],name)
        if path.is_symlink() or path.parent.is_symlink():
            raise WebError('Unsafe source path.', 409)
        raw, entries = tr.read_source(path)
        if tr.sha(raw) != job['source_sha256']:
            raise WebError('Source file changed; refusing incompatible recovery.', 409)
        return raw, entries

    def upload(self, name, raw):
        name = filename(name)
        if len(raw) > MAX_UPLOAD:
            raise WebError('File exceeds the 5 MiB upload limit.', 413)
        try:
            text = raw.decode('utf-8-sig')
            entries = tr.parse_srt(text)
        except UnicodeError:
            raise WebError('Upload must be UTF-8 SRT.') from None
        if len(entries) > MAX_ENTRIES:
            raise WebError('File exceeds the 10,000-subtitle limit.', 413)
        for entry in entries:
            # Reject malformed source tags rather than spending on an unrepairable file.
            tr.validate_text(entry, entry.text)
        ident = uuid.uuid4().hex
        folder = safe_path(self.root,'jobs',ident)
        folder.mkdir(mode=0o700)
        source = safe_path(self.root,'input','web',ident,name)
        tr.atomic_write(source, raw.decode('utf-8'))
        job = dict(version=1, id=ident, filename=name, source_sha256=tr.sha(raw), entries=len(entries),
                   created=time.time(), config=DEFAULTS.copy(), status='uploaded', error=None,
                   translation_file=None, revision_complete=False, suggestions=[], decisions={},
                   revision_noops=[], revision_completed_ids=[], cancelled=False, final_file=None, approval_version=0, translation_checkpoint_created=False)
        self.persist(job)
        save(folder / 'revision-ledger.json', dict(version=1, identity=None, requests=[], completed_ids=[], suggestions=[], noops=[]))
        return self.public(ident)

    def configure(self, ident, values):
        with self.lock:
            job = self.job(ident)
            if job['status'] in BUSY:
                raise WebError('This job is running.', 409)
            config = {**job['config'], **values}
            if set(config) != set(DEFAULTS) or type(config['revision_enabled']) is not bool:
                raise WebError('Invalid configuration.')
            try:
                config['source'], config['target'] = tr.language(config['source']), tr.language(config['target'])
                config['max_cost'] = str(tr.positive_decimal(config['max_cost']))
            except (argparse.ArgumentTypeError, TypeError):
                raise WebError('Use language codes and a positive finite budget.') from None
            for key in ('translation_model', 'revision_model'):
                prices(self.root, config[key])
            paid = self.checkpoint(ident).exists() or bool(read(self.folder(ident) / 'revision-ledger.json')['requests'])
            if (paid and any(config[k] != job['config'][k] for k in ('source','target','translation_model'))) or (read(self.folder(ident)/'revision-ledger.json')['requests'] and config['revision_model'] != job['config']['revision_model']):
                raise WebError('Languages/models are frozen once work starts. Retain this job and its spending; use a new upload for another configuration.', 409)
            if job['revision_complete'] and not config['revision_enabled']:
                raise WebError('Review suggestions or download the untouched translation.', 409)
            job['config'] = config
            job['approval_version'] = job.get('approval_version',0)+1
            job.pop('quote', None)
            self.persist(job)
            return self.public(ident)

    def translation_state(self, job, live=False):
        raw, entries = self.source(job)
        config = tr.Config(source=job['config']['source'], target=job['config']['target'], model=job['config']['translation_model'])
        snapshot=self.folder(job['id'])/'translation-pricing.json'
        if not live and snapshot.exists():
            pricing,price=tr.load_pricing(snapshot,config.model)
        elif live:
            pricing,price=prices(self.root,config.model)
        else:
            pricing,price=tr.load_pricing(self.root/'pricing.json',config.model)
        identity, _ = tr.identity_for(raw, config, pricing)
        path = self.checkpoint(job['id'])
        if not path.exists() and job.get('translation_checkpoint_created'):
            raise WebError('Translation checkpoint is missing. Spending cannot be safely inferred; restore it before recovery.',409)
        state = tr.load_state(path, identity, entries, config, price) if path.exists() else tr.new_state(identity)
        return entries, config, price, state

    def translation_plan(self, job):
        entries,config,price,state = self.translation_state(job,live=True)
        counter = tr.TokenCounter()
        pending = tr.planned_batches(entries, config, price, counter, state['translations'])
        estimates = tr.preflight(entries, pending, config, state['translations'], state['glossary'], price, counter)
        return entries, config, price, counter, state, pending, estimates

    def metrics(self, job):
        cp = self.checkpoint(job['id'])
        trans = self.translation_state(job)[3] if cp.exists() or job.get('translation_checkpoint_created') else {'translations': {}, 'requests': []}
        revision = self.revision_state(job)
        ta, trsv, ti, to, _ = tr.totals(trans)
        ra, rrsv, ri, ro, _ = tr.totals(revision)
        return dict(completed=len(trans['translations']), translation_cost=str(ta), revision_cost=str(ra),
                    reservations=str(trsv+rrsv), spent=str(ta+ra), remaining=str(Decimal(job['config']['max_cost'])-ta-ra-trsv-rrsv),
                    input_tokens=ti+ri, output_tokens=to+ro)

    def public(self, ident):
        job = self.job(ident)
        counts = Counter(d['action'] for d in job['decisions'].values())
        return {**{k:job[k] for k in ('id','filename','entries','created','config','status','error','revision_complete')},
                **self.metrics(job), 'has_translation': bool(job['translation_file']), 'has_final': bool(job['final_file']), 'config_frozen': self.checkpoint(ident).exists(),
                'revision_model_frozen': bool(self.revision_state(job)['requests']),
                'revision_entries_complete': len(self.revision_state(job)['completed_ids']),
                'review': dict(total=len(job['suggestions']), accepted=counts['accept'], rejected=counts['reject'],
                               edited=counts['edit'], remaining=len(job['suggestions'])-len(job['decisions']))}

    def listing(self):
        return [self.public(p.name) for p in sorted(self.directory.iterdir(), key=lambda p:p.name)
                if p.is_dir() and re.fullmatch(r'[a-f0-9]{32}',p.name)]

    def estimate(self, ident, operation):
        job = self.job(ident)
        if job['status'] in BUSY:
            raise WebError('This job is running.', 409)
        if operation == 'translation':
            entries, config, price, counter, state, pending, estimates = self.translation_plan(job)
            expected = sum((e.expected_cost for e in estimates), Decimal(0))
            upper = sum((e.upper_cost for e in estimates), Decimal(0)) * (config.retries+1)
            data = dict(input_tokens=sum(e.input_tokens for e in estimates), output_tokens=sum(e.output_tokens for e in estimates),
                        estimated_cost=str(expected), conservative_cost=str(upper), next_request_cost=str(estimates[0].upper_cost if estimates else 0), batches=len(pending))
            if job['config']['revision_enabled']:
                # Prospective target text is a language-expansion proxy, never a paid call.
                target = {e.id:state['translations'].get(e.id,e.text+' '*(len(e.text)//2)) for e in entries}
                rp = self.revision_plan(job, target, prospective=True)
                data['revision_preview'] = self.revision_estimate(rp)
        elif operation == 'revision':
            if not job['translation_file'] or not job['config']['revision_enabled'] or job['revision_complete']:
                raise WebError('Finish translation and enable revision before its separate approval.', 409)
            data = self.revision_estimate(self.revision_plan(job))
        else:
            raise WebError('Unknown operation.')
        metrics = self.metrics(job)
        data.update(operation=operation, filename=job['filename'], entries=job['entries'], config=job['config'],
                    **{k:metrics[k] for k in ('spent','remaining','reservations')}, max_cost=job['config']['max_cost'], approval_version=job.get('approval_version',0),
                    source_sha256=job['source_sha256'], pricing_sha256=tr.sha((self.root/'pricing.json').read_bytes()))
        data['quote_id'] = tr.sha(tr.dumps(data).encode())
        return data

    def change(self, ident, **changes):
        with self.lock:
            job = self.job(ident)
            job.update(changes)
            self.persist(job)

    def start(self, ident, operation, approved=False, quote_id=''):
        with self.lock:
            with tr.job_lock(self.folder(ident) / 'execution.lock'):
                job = self.job(ident)
                if job['status'] in BUSY:
                    raise WebError('A worker already owns this job.', 409)
                if approved is not True:
                    raise WebError('Spending was not approved. No API calls made.', 400)
                if operation == 'translation' and job['translation_file']:
                    raise WebError('Translation already completed; no duplicate spending.', 409)
                quote = self.estimate(ident, operation)
                if quote_id != quote['quote_id']:
                    raise WebError('Estimate changed. Review a fresh estimate and approve again.', 409)
                if Decimal(quote['remaining']) < Decimal(quote['next_request_cost']):
                    raise WebError('The next conservative request cost exceeds the remaining combined budget.', 409)
                job.update(status=operation+'_queued', error=None, cancelled=False,
                           approval_version=job.get('approval_version',0)+1,
                           last_approval=dict(operation=operation, quote_id=quote_id,
                                              budget=job['config']['max_cost'], at=time.time(),
                                              pricing_sha256=quote['pricing_sha256'],
                                              config_sha256=tr.sha(tr.dumps(job['config']).encode())))
                self.persist(job)  # Durable queued status consumes approval before dispatch.
            self.pool.submit(self.worker, ident, operation)
            return self.public(ident)


    def pause(self, ident):
        with self.lock:
            job = self.job(ident)
            if job['status'] in BUSY:
                job['cancelled'] = True
                job['error'] = 'Pause requested. The current request may finish and incur cost; no next batch will start.'
                self.persist(job)
            return self.public(ident)

    def worker(self, ident, operation):
        client = None
        try:
            with ExitStack() as locks:
                try:
                    locks.enter_context(tr.job_lock(self.folder(ident) / 'execution.lock'))
                except tr.TranslationError:
                    return  # A duplicate task must not change a live worker's state.
                job = self.job(ident)
                if job['status'] != operation+'_queued':
                    return
                if job['cancelled']:
                    self.change(ident,status='paused')
                    return
                self.change(ident,status=operation+'_running')
                approval=job['last_approval']
                if (approval['pricing_sha256']!=tr.sha((self.root/'pricing.json').read_bytes()) or
                        approval['config_sha256']!=tr.sha(tr.dumps(job['config']).encode())):
                    raise WebError('Pricing or configuration changed after approval. Estimate and explicitly approve again.')
                snapshot=self.folder(ident)/(operation+'-pricing.json')
                current=read(self.root/'pricing.json')
                if snapshot.exists() and read(snapshot)!=current and (self.checkpoint(ident).exists() if operation=='translation' else bool(self.revision_state(job)['requests'])):
                    raise WebError('Existing billing snapshot differs. Spending preserved; cannot migrate prices silently.',409)
                save(snapshot,current)
                if operation == 'translation':
                    entries, config, price, counter, state, pending, _ = self.translation_plan(job)
                    tr.save_state(self.checkpoint(ident), state)
                    self.change(ident,translation_checkpoint_created=True)
                    if pending:
                        client = self.client_factory()
                        try:
                            client.models.retrieve(config.model)
                        except Exception:
                            raise WebError('Model access check failed. Check API configuration/account access. No translation request was sent.') from None
                    for number, batch in enumerate(pending,1):
                        if self.job(ident)['cancelled']:
                            self.change(ident,status='paused')
                            return
                        revision = read(self.folder(ident) / 'revision-ledger.json')
                        ra, rr, *_ = tr.totals(revision)
                        limit = Decimal(job['config']['max_cost']) - ra - rr
                        tr.translate_batch(client, entries, batch, config, price, counter, state,
                                           self.checkpoint(ident), limit, batch_number=number,
                                           debug_dir=self.folder(ident)/'translation-diagnostics')
                    text = tr.render_output(entries,state['translations'])
                    if rev.credential_redactor(client)(text) != text:
                        state['translations'] = {i:t for i,t in state['translations'].items() if rev.credential_redactor(client)(t)==t}
                        tr.save_state(self.checkpoint(ident),state)
                        raise WebError('Credential-like generated content rejected; output was not published. Charged usage retained.')
                    name = self.output_name(job)
                    path = self.artifact_path('translated',name)
                    if path.exists():
                        if path.read_text(encoding='utf-8') != text:
                            raise WebError('Output collision. Existing file preserved; no overwrite.',409)
                    else:
                        tr.atomic_write(path,text)
                    self.change(ident,status='translated',error=None,translation_file=name,
                                translation_sha256=tr.sha(text.encode()),
                                warnings=tr.quality_warnings(entries,state['translations'],config.source,config.target))
                else:
                    client = self.revise(ident,job)
        except Exception as error:
            message = str(error) if isinstance(error,tr.TranslationError) else 'Operation failed. Work and spending were preserved. Estimate and approve recovery explicitly.'
            self.change(ident,status='failed',error=message)
        finally:
            if client is not None:
                client.close()

    def output_name(self, job):
        stem = Path(job['filename']).stem
        suffix = '.' + job['config']['source']
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)]
        stem=stem.encode('utf-8')[:50].decode('utf-8',errors='ignore')
        return filename(f"{stem}.{job['config']['target']}.{job['id']}.srt")

    def artifact_path(self, kind, name):
        if kind not in ('translated','revised') or filename(name) != name:
            raise WebError('Invalid output reference.',409)
        path = safe_path(self.root,'output',kind,name)
        if not path.resolve().is_relative_to(self.root):
            raise WebError('Unsafe output path.',409)
        if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)):
            raise WebError('Unsafe output path.',409)
        return path

    def target(self, job):
        if not job['translation_file']:
            raise WebError('Translation is not completed.',409)
        path = self.artifact_path('translated',job['translation_file'])
        raw, translated = tr.read_source(path)
        if tr.sha(raw) != job['translation_sha256']:
            raise WebError('Completed translation changed; original preserved. Do not revise incompatible content.',409)
        _, original = self.source(job)
        if [(e.id,e.timing) for e in translated] != [(e.id,e.timing) for e in original]:
            raise WebError('Translation structure is incompatible.',409)
        return {e.id:e.text for e in translated}

    def download(self, ident, kind):
        job = self.job(ident)
        name = job['translation_file'] if kind=='translated' else job['final_file']
        if not name:
            raise WebError('No completed file is available.',409)
        if kind=='translated':
            self.target(job)
        path = self.artifact_path(kind,name)
        if kind=='revised' and tr.sha(path.read_bytes()) != job['final_sha256']:
            raise WebError('Reviewed output changed; download refused.',409)
        return path

    def revision_state(self, job):
        state = read(self.folder(job['id']) / 'revision-ledger.json')
        try:
            if state['version'] != 1 or not all(isinstance(state[k],list) for k in ('requests','completed_ids','suggestions','noops')):
                raise ValueError
            if state['identity'] is None and any(state[k] for k in ('requests','completed_ids','suggestions','noops')):
                raise ValueError
            if len(state['completed_ids']) != len(set(state['completed_ids'])):
                raise ValueError
            seen = set()
            for record in state['requests']:
                if record['id'] in seen or record['status'] not in ('pending','unknown','settled'):
                    raise ValueError
                seen.add(record['id'])
                reservation = Decimal(record['reservation'])
                if not reservation.is_finite() or reservation <= 0:
                    raise ValueError
                if record['status'] == 'settled':
                    inp,out,cached = (record[k] for k in ('input_tokens','output_tokens','cached_tokens'))
                    if any(type(v) is not int or v<0 for v in (inp,out,cached)) or cached>inp:
                        raise ValueError
                    if record['model'] != state['identity']['model']:
                        raise ValueError
                    snapshot=self.folder(job['id'])/'revision-pricing.json'
                    _, verified_price = tr.load_pricing(snapshot if snapshot.exists() else self.root/'pricing.json',record['model'])
                    if any(record['rates'][k] != verified_price[k] for k in ('input_per_million','cached_input_per_million','output_per_million')):
                        raise ValueError
                    rates = [Decimal(record['rates'][k]) for k in ('input_per_million','cached_input_per_million','output_per_million')]
                    if any(not v.is_finite() or v<0 for v in rates) or rates[0]==0 or rates[2]==0 or rates[1]>rates[0]:
                        raise ValueError
                    if Decimal(record['cost']) != tr.cost(record['rates'],inp,out,cached):
                        raise ValueError
            actual,reserved,*_ = tr.totals(state)
            if not actual.is_finite() or not reserved.is_finite():
                raise ValueError
        except (ValueError,KeyError,TypeError,InvalidOperation):
            raise WebError('Revision spending ledger is corrupt. Preserved; refusing to reset or ignore spending.',409) from None
        return state

    def revision_plan(self, job, target=None, prospective=False):
        _,entries = self.source(job)
        target = target if target is not None else self.target(job)
        cfg = job['config']
        pricing,price = prices(self.root,cfg['revision_model'])
        source_name,target_name = (tr.LANGUAGES.get(cfg[k],cfg[k]) for k in ('source','target'))
        prompt = rev.REVISION_PROMPT.replace('English-to-Czech',source_name+'-to-'+target_name).replace('English and surrounding',source_name+' and surrounding').replace('Existing Czech','Existing '+target_name)
        prompt += f"\nSource language: {source_name}. Target language: {target_name}. The fields existing_czech/proposed_czech hold the existing/proposed target-language text."
        identity = dict(source_sha256=job['source_sha256'], target_sha256=tr.sha(tr.dumps(target).encode()),
                        source=cfg['source'], target=cfg['target'], model=cfg['revision_model'],
                        prompt_version=REVISION_VERSION, prompt_sha256=tr.sha(prompt.encode()),
                        schema_version=rev.SCHEMA_VERSION, reasoning='none', batch_size=25, context=5,
                        pricing_sha256=tr.sha(tr.dumps(pricing).encode()))
        state = self.revision_state(job)
        if not prospective and state['identity'] not in (None,identity) and (state['requests'] or state['completed_ids']):
            raise WebError('Revision checkpoint identity changed. Costs and earlier work retained.',409)
        if not set(state['completed_ids']) <= {e.id for e in entries}:
            raise WebError('Invalid revision checkpoint IDs.',409)
        counter = tr.TokenCounter(); plans=[]
        positions={e.id:i for i,e in enumerate(entries)}
        pending=[e for e in entries if prospective or e.id not in state['completed_ids']]
        for batch in tr.batches(pending,25):
            ids={e.id for e in batch}
            context_indexes=set()
            for e in batch:
                i=positions[e.id];context_indexes.update(range(max(0,i-5),min(len(entries),i+6)))
            cases=[dict(id=e.id,timestamp=e.timing,english=e.text,baseline_czech=target[e.id]) for e in batch]
            payload={'review':[dict(id=e.id,english=e.text,existing_czech=target[e.id],
                explicit_speaker_labels=[label for _,kind,label in tr.speaker_anchors(e.text) if kind!='dash']) for e in batch],
                'read_only_context':[dict(id=entries[i].id,english=entries[i].text,existing_czech=target[entries[i].id]) for i in sorted(context_indexes) if entries[i].id not in ids]}
            proxy={'corrections':[dict(id=e.id,existing_czech=target[e.id],proposed_czech=target[e.id],category='semantic_error',explanation='Context supports a different meaning; preserve the speaker, tone and original formatting.',confidence='medium') for e in batch]}
            expected_output=round(counter.count(tr.dumps(proxy))*1.3)+256
            cap=max(4096,expected_output*2)
            if cap>price['max_output_tokens']:
                raise WebError('Revision batch cannot fit the configured output allowance. Source is too long; no API call made.')
            request=dict(model=cfg['revision_model'],instructions=prompt,input=tr.dumps(payload),
                text={'format':{'type':'json_schema','name':'subtitle_corrections','strict':True,'schema':rev.revision_schema([e.id for e in batch])}},
                reasoning={'effort':'none'},max_output_tokens=cap,store=False,service_tier='default')
            serialized=tr.dumps(request);inp=counter.count(serialized)+128;upper=len(serialized.encode())+512
            if upper+cap>price['context_window']:
                raise WebError('Revision context exceeds the configured model limit; no API call made.')
            estimate=tr.Estimate(inp,expected_output,upper,cap,tr.cost(price,inp,expected_output),tr.cost(price,upper,cap))
            plans.append(dict(entries=batch,cases=cases,request=request,estimate=estimate))
        return dict(plans=plans,identity=identity,price=price,state=state,prospective=prospective)

    def revision_estimate(self, plan):
        estimates=[p['estimate'] for p in plan['plans']]
        return dict(input_tokens=sum(e.input_tokens for e in estimates), output_tokens=sum(e.output_tokens for e in estimates),
                    estimated_cost=str(sum((e.expected_cost for e in estimates),Decimal(0))),
                    conservative_cost=str(sum((e.upper_cost for e in estimates),Decimal(0))),
                    next_request_cost=str(estimates[0].upper_cost if estimates else 0), batches=len(estimates), provisional=plan['prospective'])

    def revise(self, ident, job):
        plan=self.revision_plan(job);state=plan['state'];price=plan['price'];client=None
        state['identity']=plan['identity']
        ledger_path=self.folder(ident)/'revision-ledger.json'
        save(ledger_path,state)
        try:
            if plan['plans']:
                client=self.client_factory()
                try:
                    client.models.retrieve(job['config']['revision_model'])
                except Exception:
                    raise WebError('Revision model access check failed; no revision request sent.') from None
            redact=rev.credential_redactor(client)
            for batch in plan['plans']:
                if self.job(ident)['cancelled']:
                    self.change(ident,status='paused')
                    return None
                # Independent revision ledger, combined per-job budget including translation.
                trans=self.translation_state(job)[3]
                ta,ts,*_=tr.totals(trans)
                tr.budget_guard(state,Decimal(job['config']['max_cost'])-ta-ts,batch['estimate'].upper_cost)
                record=dict(id=uuid.uuid4().hex,status='pending',reservation=str(batch['estimate'].upper_cost),
                            model=job['config']['revision_model'],batch_ids=[e.id for e in batch['entries']],
                            rates={k:price[k] for k in ('input_per_million','cached_input_per_million','output_per_million')})
                state['requests'].append(record);save(ledger_path,state)
                response=None
                try:
                    response=client.responses.create(**batch['request'])
                    # Complete generated text is retained before validating response/usage.
                    capture=dict(version=1,prompt_version=REVISION_VERSION,schema_version=rev.SCHEMA_VERSION,
                        response_metadata=tr.response_metadata(response),output_limit=batch['estimate'].output_cap,
                        raw_generated_text=getattr(response,'output_text',None),credentials_redacted=True)
                    save(self.folder(ident)/'revision-responses'/f"{record['id']}.json",rev.safe_document(capture,redact))
                    tr.settle_response(record,response,price);record.update(tr.response_metadata(response));save(ledger_path,state)
                    audit=rev.audit_corrections(response.output_text,batch['cases'],redact)
                    save(self.folder(ident)/'revision-responses'/f"{record['id']}-audit.json",rev.safe_document(audit,redact))
                    if record['response_status']!='completed' or record['refusal']:
                        raise WebError('Revision response was incomplete or refused; charged usage retained. No automatic retry.')
                    if audit['validation_errors']:
                        record['validation']=audit['validation_errors'][0]['category']
                        raise WebError('Revision validation failed: '+record['validation']+'. Response retained locally; no automatic retry.')
                    state['suggestions'].extend(audit['corrections']);state['noops'].extend(audit['unchanged_proposals'])
                    state['completed_ids'].extend(e.id for e in batch['entries']);record['validation']='passed'
                    save(ledger_path,state)  # Accepted work and its accounting commit together.
                except Exception:
                    if record['status']!='settled' and response is not None:
                        try:tr.settle_response(record,response,price)
                        except tr.TranslationError:pass
                    if record['status']!='settled':record['status']='unknown'
                    save(ledger_path,state)
                    raise
            self.change(ident,status='review',error=None,revision_complete=True,
                        suggestions=state['suggestions'],revision_noops=state['noops'],revision_completed_ids=state['completed_ids'])
            return None
        finally:
            if client is not None:client.close()

    def review_job(self, ident):
        job=self.job(ident)
        if not job['revision_complete']:
            raise WebError('Finish revision before reviewing suggestions.',409)
        state=self.revision_state(job);_,entries=self.source(job);target=self.target(job)
        if set(state['completed_ids'])!={e.id for e in entries} or state['suggestions']!=job['suggestions']:
            raise WebError('Review checkpoint is incompatible. Existing records preserved.',409)
        fields={'id','existing_czech','proposed_czech','category','explanation','confidence'}
        if any(set(r)!=fields|{'timestamp'} for r in state['suggestions']):
            raise WebError('Invalid stored suggestion fields.',409)
        cases=[dict(id=e.id,timestamp=e.timing,english=e.text,baseline_czech=target[e.id]) for e in entries]
        rows=[{k:v for k,v in r.items() if k!='timestamp'} for r in state['suggestions']]
        audit=rev.audit_corrections(tr.dumps({'corrections':rows}),cases)
        if audit['validation_errors'] or audit['corrections']!=state['suggestions']:
            raise WebError('Stored suggestions failed structural validation. Preserved for recovery.',409)
        if not set(job['decisions']) <= {r['id'] for r in state['suggestions']}:
            raise WebError('Unknown review decision identifiers.',409)
        return job

    def suggestions(self, ident):
        job=self.review_job(ident)
        if not job['revision_complete']:
            raise WebError('Finish revision before reviewing suggestions.',409)
        _,entries=self.source(job);original={e.id:e for e in entries}
        return [dict(id=row['id'],timestamp=original[row['id']].timing,original=original[row['id']].text,
                     current=row['existing_czech'],suggested=row['proposed_czech'],explanation=row['explanation'],
                     category=row['category'],decision=job['decisions'].get(row['id'])) for row in job['suggestions']]

    def context(self, ident, subtitle_id):
        job=self.job(ident);_,entries=self.source(job);target=self.target(job)
        positions={e.id:i for i,e in enumerate(entries)}
        if subtitle_id not in positions:
            raise WebError('Unknown subtitle ID.',404)
        i=positions[subtitle_id]
        return [dict(id=e.id,timestamp=e.timing,original=e.text,current=target[e.id]) for e in entries[max(0,i-5):i+6]]

    def decide(self, ident, subtitle_id, action, text=None):
        with self.lock:
            job=self.review_job(ident)
            if not job['revision_complete'] or job['status'] in BUSY:
                raise WebError('Revision must finish before review.',409)
            row=next((r for r in job['suggestions'] if r['id']==subtitle_id),None)
            if row is None or action not in ('accept','reject','edit'):
                raise WebError('Unknown suggestion or action.',400)
            _,entries=self.source(job);entry=next(e for e in entries if e.id==subtitle_id)
            chosen=row['proposed_czech'] if action=='accept' else row['existing_czech'] if action=='reject' else text
            tr.validate_text(entry,chosen)
            if rev.credential_redactor(None)(chosen)!=chosen:
                raise WebError('Credential-like edit rejected.')
            job['decisions'][subtitle_id]=dict(action=action,text=chosen,at=time.time())
            job['final_file']=None
            job['status']='reviewed' if len(job['decisions'])==len(job['suggestions']) else 'review'
            self.persist(job)
            return self.public(ident)

    def export(self, ident):
        with self.lock:
            job=self.review_job(ident)
            if set(job['decisions'])!={r['id'] for r in job['suggestions']}:
                raise WebError('Every suggestion needs an explicit accept, reject, or edit decision before final export.',409)
            signature=tr.sha(tr.dumps(job['decisions']).encode())
            if job['final_file'] and job.get('final_decisions_sha256')==signature:
                self.download(ident,'revised')
                return self.public(ident)
            _,entries=self.source(job);translations=self.target(job)
            for ident2,decision in job['decisions'].items():translations[ident2]=decision['text']
            text=tr.render_output(entries,translations)
            name=filename(Path(self.output_name(job)).stem+'.'+uuid.uuid4().hex[:12]+'.revised.srt')
            path=self.artifact_path('revised',name)
            if path.exists():raise WebError('Output collision; no file overwritten.',409)
            tr.atomic_write(path,text)
            job.update(final_file=name,final_sha256=tr.sha(text.encode()),final_decisions_sha256=signature,status='exported')
            self.persist(job)
            return self.public(job['id'])

    def recover(self):
        for p in self.directory.iterdir():
            if not p.is_dir() or not re.fullmatch(r'[a-f0-9]{32}', p.name):
                continue
            job = self.job(p.name)
            if job['status'] not in BUSY:
                continue
            try:
                with tr.job_lock(p / 'execution.lock'):
                    job['status'] = 'interrupted'
                    job['error'] = 'Server stopped. Completed work and spending were retained. Estimate and explicitly approve resume; no work restarted automatically.'
                    self.persist(job)
            except tr.TranslationError:
                pass  # A live worker still owns this job.

    def close(self):
        for job in self.listing():
            if job['status'] in BUSY:
                self.pause(job['id'])
        self.pool.shutdown(wait=True)
