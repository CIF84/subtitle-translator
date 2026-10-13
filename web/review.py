"""Confirmed, atomic review actions; no AI, pricing or spending operations."""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import re
import time
import uuid

import translate_srt as tr
import revision_experiment as rev
from .service import BUSY, WebError, filename


def version(job):
    return tr.sha(tr.dumps(dict(source=job['source_sha256'],target=job['translation_sha256'],
        suggestions=job['suggestions'],decisions=job['decisions'],generation=job.get('review_generation',0))).encode())


def counts(job):
    n=Counter(d['action'] for d in job['decisions'].values())
    return dict(total=len(job['suggestions']),accepted=n['accept'],rejected=n['reject'],
                edited=n['edit'],undecided=len(job['suggestions'])-len(job['decisions']))


def validate(service,job):
    _,entries=service.source(job);by_id={e.id:e for e in entries}
    rows={r['id']:r for r in job['suggestions']}
    if not isinstance(job['decisions'],dict) or not set(job['decisions'])<=set(rows):
        raise WebError('Unknown review decision identifiers.',409)
    for ident,d in job['decisions'].items():
        if not isinstance(d,dict) or d.get('action') not in ('accept','reject','edit'):
            raise WebError('Stored review decision is invalid. Existing records preserved.',409)
        chosen=rows[ident]['proposed_czech'] if d['action']=='accept' else rows[ident]['existing_czech'] if d['action']=='reject' else d.get('text')
        if d.get('text')!=chosen:raise WebError('Stored decision does not match its action.',409)
        tr.validate_text(by_id[ident],chosen)
        if rev.credential_redactor(None)(chosen)!=chosen:raise WebError('Credential-like review content rejected.')


def state(service,ident):
    job=service.review_job(ident);validate(service,job)
    current=version(job);snapshot=job.get('review_bulk_snapshot')
    return dict(version=current,counts=counts(job),
        undo_available=bool(snapshot and not snapshot.get('restored') and snapshot.get('applied_version')==current),
        has_current_export=bool(job.get('current_export')))


@contextmanager
def transaction(service,ident,expected=None,confirmed=None,operation_id=None):
    with service.lock:
        with tr.job_lock(service.folder(ident)/'execution.lock'):
            job=service.review_job(ident);validate(service,job)
            if job['status'] in BUSY:raise WebError('Finish revision before reviewing.',409)
            if expected is not None and expected!=version(job):
                raise WebError('Review changed in another request. Refresh before confirming again.',409)
            if confirmed is not None:
                if confirmed is not True:raise WebError('Explicit confirmation is required.',400)
                if expected is None:raise WebError('Refresh the current review before confirming.',409)
                if not isinstance(operation_id,str) or not re.fullmatch('[a-f0-9]{32}',operation_id):raise WebError('Invalid review operation ID.')
            yield job


def updated(job):
    candidate=deepcopy(job)
    candidate['review_generation']=job.get('review_generation',0)+1
    candidate['final_file']=None
    candidate['current_export']=None
    return candidate


def decision(service,ident,subtitle_id,action,text=None,expected=None):
    """Keep legacy single-decision semantics; current browsers submit state tokens."""
    with transaction(service,ident,expected) as job:
        if expected is None and job.get('review_generation',0):
            raise WebError('Refresh the review; newer decisions require a current state token.',409)
        row=next((r for r in job['suggestions'] if r['id']==subtitle_id),None)
        if row is None or action not in ('accept','reject','edit'):raise WebError('Unknown suggestion or action.')
        chosen=row['proposed_czech'] if action=='accept' else row['existing_czech'] if action=='reject' else text
        candidate=updated(job)
        candidate['decisions'][subtitle_id]=dict(action=action,text=chosen,at=time.time())
        validate(service,candidate)
        candidate['status']='reviewed' if len(candidate['decisions'])==len(candidate['suggestions']) else 'review'
        service.persist(candidate)
        return service.public(ident)


def bulk(service,ident,action,expected,operation_id,confirmed=False):
    with transaction(service,ident,expected,confirmed,operation_id) as job:
        if action not in ('accept','reject'):raise WebError('Unknown bulk action.')
        old=job.get('review_bulk_snapshot')
        if old and old['operation_id']==operation_id:raise WebError('Bulk operation already submitted.',409)
        pending=[r for r in job['suggestions'] if r['id'] not in job['decisions']]
        if not pending:raise WebError('No undecided suggestions remain.',409)
        candidate=updated(job);at=time.time()
        for r in pending:
            chosen=r['proposed_czech'] if action=='accept' else r['existing_czech']
            candidate['decisions'][r['id']]=dict(action=action,text=chosen,at=at)
        validate(service,candidate)
        candidate['status']='reviewed'
        # Snapshot and complete decision map commit together in the existing job JSON.
        candidate['review_bulk_snapshot']=dict(version=1,operation_id=operation_id,action=action,
            affected_ids=[r['id'] for r in pending],decisions=deepcopy(job['decisions']),
            applied_version=version(candidate),at=at,restored=False)
        service.persist(candidate)
        return service.public(ident)


def restore(service,ident,expected,operation_id,confirmed=False):
    with transaction(service,ident,expected,confirmed,operation_id) as job:
        snapshot=job.get('review_bulk_snapshot')
        if not snapshot or snapshot.get('restored') or snapshot.get('applied_version')!=version(job):
            raise WebError('The latest bulk snapshot cannot be restored after newer decisions. Snapshot preserved.',409)
        if snapshot['operation_id']==operation_id:raise WebError('Use a new restore operation ID.',409)
        candidate=updated(job);candidate['decisions']=deepcopy(snapshot['decisions'])
        validate(service,candidate)
        candidate['review_bulk_snapshot'].update(restored=True,restored_at=time.time())
        candidate['status']='reviewed' if len(candidate['decisions'])==len(candidate['suggestions']) else 'review'
        service.persist(candidate)
        return service.public(ident)


def current_path(service,ident):
    job=service.review_job(ident);record=job.get('current_export')
    if not record:raise WebError('No current-decisions export available.',404)
    path=service.artifact_path('revised',record['filename'])
    if tr.sha(path.read_bytes())!=record['sha256']:raise WebError('Exported file changed; download refused.',409)
    return path


def export_current(service,ident,expected,operation_id,confirmed=False):
    with transaction(service,ident,expected,confirmed,operation_id) as job:
        signature=version(job)
        saved=job.get('current_export')
        if saved and saved.get('review_version')==signature:
            current_path(service,ident)
            return dict(download_url=f'/api/jobs/{ident}/download/current',counts=counts(job),review_version=signature)
        _,entries=service.source(job);translations=service.target(job)
        for i,d in job['decisions'].items():translations[i]=d['text']
        text=tr.render_output(entries,translations)  # Full production structural/UTF-8 validation.
        name=filename(Path(service.output_name(job)).stem+'.'+uuid.uuid4().hex[:12]+'.current.srt')
        path=service.artifact_path('revised',name)
        if path.exists():raise WebError('Output collision; existing file preserved.',409)
        tr.atomic_write(path,text)
        candidate=deepcopy(job)
        candidate['current_export']=dict(filename=name,sha256=tr.sha(text.encode()),review_version=signature,
            counts=counts(job),operation_id=operation_id)
        service.persist(candidate)  # A storage failure may leave a valid unlinked file, never a partial SRT.
        return dict(download_url=f'/api/jobs/{ident}/download/current',counts=counts(job),review_version=signature)


def export_full(service,ident,expected=None):
    with transaction(service,ident,expected):
        return service.export(ident)  # Preserve the original all-decisions-required path.
