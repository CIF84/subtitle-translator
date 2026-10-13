"""WP5 read-only history and UI checks. All API calls remain mocked."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_web import (webroot, webclient, upload, paid_job, generate_revision,
                      Deferred, SRT)
from web.service import Jobs, save
from web.presentation import history
import translate_srt as tr
from dataclasses import asdict, replace

ROOT = Path(__file__).parents[1]


def snapshot(root):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def completed_translation(root):
    j,p,a,f,i,q=paid_job(root)
    j.start(i,'translation',True,q['quote_id']);p.run()
    return j,p,i


def test_history_requires_full_matching_source_and_settings(webroot):
    j,p,original=completed_translation(webroot)
    same=j.upload('Renamed.en.srt',SRT)['id']
    before=snapshot(webroot)
    figure=history(j,same,'translation')['comparable']
    assert figure['cost']=='0.000345' and figure['entries']==2 and not figure['includes_successful_probes']
    assert snapshot(webroot)==before
    changed_source=SRT.replace(b'mineral',b'crystal')
    assert changed_source!=SRT
    different=j.upload('Movie.en.srt',changed_source)['id']
    assert history(j,different,'translation')['comparable'] is None
    for changes in ({'target':'de'},{'translation_model':'gpt-5.4'}):
        candidate=j.upload('Movie.en.srt',SRT)['id'];j.configure(candidate,changes)
        assert history(j,candidate,'translation')['comparable'] is None


def test_history_excludes_failed_attempt_cost(webroot):
    j,p,first=completed_translation(webroot)
    state=json.loads(j.checkpoint(first).read_text())
    failed=dict(state['requests'][0]);failed.pop('validation');failed['response_status']='incomplete'
    state['requests'].insert(0,failed);tr.save_state(j.checkpoint(first),state)
    same=j.upload('Movie.en.srt',SRT)['id']
    assert j.public(first)['spent']=='0.000690'
    assert history(j,same,'translation')['comparable']['cost']=='0.000345'


@pytest.mark.parametrize('damage',['incomplete','overlap','settings','prompt','price','cost','schema','unknown_usage','incomplete_response','refusal'])
def test_ineligible_historical_evidence_is_omitted(webroot,damage):
    j,p,first=completed_translation(webroot);state=json.loads(j.checkpoint(first).read_text())
    if damage=='incomplete':state['translations'].pop('2')
    elif damage=='overlap':state['requests'].append(dict(state['requests'][0]))
    elif damage=='settings':state['identity']['config']['context']=6
    elif damage=='prompt':state['identity']['prompt_sha256']='0'*64
    elif damage=='price':state['identity']['pricing_sha256']='0'*64
    elif damage=='cost':state['requests'][0]['cost']='0'
    elif damage=='schema':state['requests'][0]['response_schema_mode']='constrained'
    elif damage=='incomplete_response':state['requests'][0]['response_status']='incomplete'
    elif damage=='refusal':state['requests'][0]['refusal']=True
    else:state['requests'][0]['status']='unknown'
    tr.save_state(j.checkpoint(first),state)
    same=j.upload('Movie.en.srt',SRT)['id']
    assert history(j,same,'translation')['comparable'] is None


def test_same_job_is_not_its_own_history(webroot):
    j,p,first=completed_translation(webroot)
    assert history(j,first,'translation')['comparable'] is None


def test_successful_probe_plus_continuation_has_complete_cost_scope(webroot):
    j,p,first=completed_translation(webroot);state=json.loads(j.checkpoint(first).read_text())
    probe=dict(state['requests'][0]);continuation=dict(state['requests'][0])
    probe['mode']='probe';probe['probe_entries']=1
    probe_config=replace(tr.Config(),retries=0,max_output_tokens=4096)
    probe['configuration_sha256']=tr.sha(tr.dumps(asdict(probe_config)).encode())
    for record in (probe,continuation):record['validation']={'result':'passed','expected_entries':1,'received_entries':1}
    continuation['batch_first_id']='2'
    state['requests']=[probe,continuation]
    state['configuration_history']=[{**state['identity'],'config':asdict(probe_config)}]
    tr.save_state(j.checkpoint(first),state)
    same=j.upload('Movie.en.srt',SRT)['id'];figure=history(j,same,'translation')['comparable']
    assert figure['cost']=='0.000690' and figure['includes_successful_probes']


def test_resumed_ledger_uses_accepted_configuration_history(webroot):
    j,p,first=completed_translation(webroot);state=json.loads(j.checkpoint(first).read_text())
    current=json.loads(json.dumps(state['identity']))
    state['identity']['prompt_version']='1.0'
    state['identity']['prompt_sha256']=tr.LEGACY_PROMPT_SHA256
    state['identity']['config'].update(batch_size=75,reasoning_effort=None,response_schema='constrained')
    state['configuration_history']=[current]
    tr.save_state(j.checkpoint(first),state)
    same=j.upload('Movie.en.srt',SRT)['id']
    assert history(j,same,'translation')['comparable']['cost']=='0.000345'


def test_matching_revision_history_and_different_model(webroot):
    j,p,a,first,baseline=generate_revision(webroot)
    second=j.upload('Movie.en.srt',SRT)['id']
    from test_web import translated_client
    j.client_factory=lambda:translated_client()
    q=j.estimate(second,'translation');j.start(second,'translation',True,q['quote_id']);p.run()
    j.configure(second,{'revision_enabled':True})
    before=snapshot(webroot)
    figure=history(j,second,'revision')['comparable']
    assert figure['cost']=='0.0023' and figure['operation']=='revision'
    assert snapshot(webroot)==before
    j.configure(second,{'revision_model':'gpt-5.4-mini'})
    assert history(j,second,'revision')['comparable'] is None


def test_history_route_is_read_only_and_never_creates_client(webclient,webroot):
    c,j,f=webclient;ident=upload(c).json()['id'];before=snapshot(webroot)
    response=c.get(f'/api/jobs/{ident}/history/translation')
    assert response.status_code==200 and response.json()=={'comparable':None}
    assert snapshot(webroot)==before
    assert c.get(f'/api/jobs/{ident}/history/unknown').status_code==422
    assert c.get('/api/jobs/not-a-uuid/history/translation').status_code==404
    f.assert_not_called()


def test_history_rejects_symlinked_candidate(webroot):
    j,p,first=completed_translation(webroot)
    path=j.checkpoint(first);backup=path.with_name('original.json');path.rename(backup);path.symlink_to(backup)
    same=j.upload('Movie.en.srt',SRT)['id']
    assert history(j,same,'translation')['comparable'] is None


def test_desktop_and_small_screen_layout_contract():
    css=(ROOT/'web/static/style.css').read_text()
    assert 'max-width:1840px' in css and 'grid-template-columns:296px minmax(0,1fr)' in css
    assert '.comparison { display:grid; grid-template-columns:repeat(3,minmax(0,1fr))' in css
    assert '@media(max-width:1200px)' in css and '.comparison { grid-template-columns:1fr; }' in css
    assert '@media(max-width:950px)' in css and '.layout { grid-template-columns:1fr' in css
    assert '@media(max-width:600px)' in css and 'overflow-wrap:anywhere' in css


def test_approval_is_wide_and_extreme_scenario_is_disclosed():
    html=(ROOT/'web/static/index.html').read_text()
    assert html.index('</aside>')<html.index('id="quote"')
    assert 'id="spend-approval" type="checkbox"' in html and 'id="start" type="button" disabled' in html
    assert 'id="estimate-details"' in html and 'How estimates work' in html
    assert 'not the authorized budget' in html and 'not an absolute provider billing cap' in html
    assert 'No, cancel' in html


def test_frontend_behavior_and_safe_formatting():
    node=shutil.which('node')
    if not node:
        bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
        node=str(bundled) if bundled.is_file() else None
    if not node:
        pytest.skip('Node is needed only for offline frontend tests, not application runtime')
    result=subprocess.run([node,str(ROOT/'tests/ui_checks.cjs'),str(ROOT)],capture_output=True,text=True,check=False)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    assert 'WP5 frontend checks passed' in result.stdout
