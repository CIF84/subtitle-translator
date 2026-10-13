'use strict';
const $=id=>document.getElementById(id);
let csrf='',job=null,quote=null,suggestions=[],index=0,dirty=false,uploading=false,loadedReview='',languages={
};
let disclosureKey='',costCalibration=null,costComparable=null,reviewState=null,reviewBusy=false,pendingReview=null;
const busy=()=>job && ['translation_queued','translation_running','revision_queued','revision_running'].includes(job.status);
const money=value=>'$'+Number(value||0).toFixed(6);
const labels={
  semantic_error:'Meaning',contextual_ambiguity:'Context',unnatural_czech:'Natural language',tone_mismatch:'Tone',terminology_inconsistency:'Terminology',sdh_formatting_issue:'SDH / formatting'
};
const statuses={
  uploaded:'Ready',translation_queued:'Translation queued',translation_running:'Translating',translated:'Translation completed',revision_queued:'Revision queued',revision_running:'Revising',review:'Suggestions ready',reviewed:'Review completed',exported:'Export ready',failed:'Failed',paused:'Paused',interrupted:'Ready to resume'
};
function configured(){
  return job && (job.config_frozen || localStorage.getItem('subtitle-config-'+job.id)===JSON.stringify(job.config));
}
function openSettings(){
  $('config-details').open=true;
  $('config-details').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function renderWorkflow(){
  const state=SubtitleUI.stage(job,dirty,quote,!!configured());
  $('stage-title').textContent=state.title;
  $('next-action').textContent=state.next;
  $('next-action').disabled=!state.action;
  $('stage-list').replaceChildren();
  SubtitleUI.names.forEach((name,i)=>{
    const li=document.createElement('li'),button=document.createElement('button');
    button.type='button';
    button.textContent=(state.completed.includes(i)?'✓ ':`${i+1} `)+name;
    button.dataset.state=state.completed.includes(i)?'completed':'pending';
    if(i===state.index)button.setAttribute('aria-current','step');
    button.disabled=!job&&i>0;
    button.onclick=()=>{
      if(i===0){$('upload-details').open=true;$('upload-details').scrollIntoView({behavior:'smooth'});}
      else if(i===1)openSettings();
      else if(i===2)(quote?$('quote'):$('cost-controls')).scrollIntoView({behavior:'smooth'});
      else if(i===3){$('progress-details').open=true;$('progress-details').scrollIntoView({behavior:'smooth'});}
      else if(i===4)(job.revision_complete?$('review'):$('review-empty')).scrollIntoView({behavior:'smooth'});
      else (job.revision_complete?$('export'):$('download-translated')).scrollIntoView({behavior:'smooth'});
    };
    li.append(button);$('stage-list').append(li);
  });
  const help={upload:'Start with a UTF-8 SRT file. Nothing is sent to OpenAI until you approve spending.',
    settings:'Review the language, models and total job limit. Revision is optional.',
    translation:'Review an estimate before separately approving translation or recovery.',
    revision:'The untouched translation stays safe. Revision requires its own estimate and approval.',
    approval:'Spending remains unapproved until you check the consent box and choose Approve & start.',
    review:'Compare meaning and context. Accept, reject or edit every suggestion; confidence never accepts a change for you.',
    export:'All suggestions have explicit decisions. Save a validated SRT while retaining the untouched translation.',
    'download-final':'Your reviewed SRT is saved locally. Download a browser copy when ready.',
    'download-translated':'Your untouched translation is saved. Download it, or enable optional revision in settings.'};
  $('stage-help').textContent=help[state.action]||'This operation is running. You can refresh or return later; paid work is never restarted by a refresh.';
  $('next-action').onclick=()=>{
    const targets={upload:'file',settings:'save-config',translation:'estimate-translation',revision:'estimate-revision',
      review:'review',export:'export','download-final':'download-final','download-translated':'download-translated',approval:'quote'};
    const target=$(targets[state.action]);
    if(['approval','review'].includes(state.action))target.scrollIntoView({behavior:'smooth'});
    else if(target&&!target.disabled)target.click();
  };
  const key=(job?.id||'none')+':'+state.index+':'+(quote?.operation||'');
  if(key!==disclosureKey){
    $('upload-details').open=!job;
    $('config-details').open=state.index===1||!job;
    $('progress-details').open=!!job&&(busy()||['failed','paused','interrupted'].includes(job.status));
    disclosureKey=key;
  }
}
function message(text){
  $('message').textContent=text;
  $('message').hidden=!text;
}
async function api(path,options={
}){
  const headers={
    ...options.headers
  };
  if(options.method && options.method!=='GET')headers['x-csrf-token']=csrf;
  const response=await fetch(path,{
    ...options,headers,credentials:'same-origin'
  });
  const data=await response.json();
  if(!response.ok){const error=new Error(data.error||'Request failed. Refresh the local app and retry.');error.status=response.status;throw error;}
  return data;
}
function json(method,data){
  return {
    method,headers:{
      'Content-Type':'application/json'
    },body:JSON.stringify(data)
  };
}
function clearQuote(){
  quote=null;costCalibration=null;costComparable=null;
  $('quote').hidden=true;
  $('cost-placeholder').hidden=false;
  $('spend-approval').checked=false;
  $('start').disabled=true;
  renderWorkflow();
}
function settings(){
  return {
    source:$('source').value,target:$('target').value,translation_model:$('translation-model').value,revision_enabled:$('revision-enabled').checked,revision_model:$('revision-model').value,max_cost:$('budget').value
  };
}
async function saveSettings(){
  if(!job)throw new Error('Upload a subtitle file first.');
  job=await api(`/api/jobs/${job.id}/config`,json('PUT',settings()));
  dirty=false;
  localStorage.setItem('subtitle-config-'+job.id,JSON.stringify(job.config));
  clearQuote();
  await renderJob();
}
async function listJobs(){
  const saved=await api('/api/jobs');
  $('jobs').replaceChildren(new Option('Choose a saved job…',''));
  saved.sort((a,b)=>b.created-a.created).forEach(j=>{
    $('jobs').add(new Option(`${j.filename} · ${statuses[j.status]||j.status}`,j.id));
  });
  if(job)$('jobs').value=job.id;
  return saved;
}
async function select(id){
  if(!id)return;
  job=await api('/api/jobs/'+id);
  localStorage.setItem('subtitle-job',id);
  dirty=false;
  loadedReview='';reviewState=null;
  suggestions=[];
  clearQuote();
  await renderJob();
  $('jobs').value=id;
}
async function upload(file){
  if(!file||uploading)return;
  if(!file.name.toLowerCase().endsWith('.srt'))throw new Error('Please choose an SRT file.');
  if(file.size>5*1024*1024)throw new Error('File exceeds 5 MiB.');
  uploading=true;
  message('Validating your subtitles…');
  try{
    job=await api('/api/jobs?filename='+encodeURIComponent(file.name),{
      method:'POST',body:file,headers:{
        'Content-Type':'application/octet-stream'
      }
    });
    localStorage.setItem('subtitle-job',job.id);
    dirty=false;
    loadedReview='';reviewState=null;
    suggestions=[];
    clearQuote();
    await listJobs();
    await renderJob();
    message('');
  }finally{
    uploading=false;
    $('file').value='';
  }
}
async function renderJob(){
  if(!job)return;
  $('file-info').textContent=`${job.filename} · ${job.entries} subtitles`;
  $('status').textContent=statuses[job.status]||job.status;
  if(job.status==='translated'&&job.config.revision_enabled)$('status').textContent='Revision available';
  const savedOption=[...$('jobs').options].find(option=>option.value===job.id);
  if(savedOption)savedOption.textContent=`${job.filename} · ${$('status').textContent}`;
  $('upload-summary').textContent=`${job.entries} validated cues`;
  $('config-summary').textContent=`${languages[job.config.source]||job.config.source} → ${languages[job.config.target]||job.config.target}`;
  $('config-lock-note').hidden=!job.config_frozen;
  $('progress-details').hidden=job.status==='uploaded'&&!job.completed&&!job.has_translation;
  $('translation-progress').max=job.entries;
  $('translation-progress').value=job.completed;
  $('completed').textContent=`${job.completed} / ${job.entries}`;
  $('translation-cost').textContent=money(job.translation_cost);
  $('revision-cost').textContent=money(job.revision_cost);
  $('remaining').textContent=money(job.remaining);
  $('job-error-area').hidden=!job.error;
  $('job-error').textContent=job.error||'';
  $('job-error-help').textContent=SubtitleUI.errorHelp(job.error);
  $('revision-checked').textContent=job.config.revision_enabled?`Revision entries checked: ${job.revision_entries_complete} / ${job.entries}`:'';
  $('reservations').textContent=Number(job.reservations)>0?`${money(job.reservations)} reserved for requests with unsettled usage. Reservations are retained during recovery.`:'';
  $('pause').disabled=!busy();
  $('estimate-translation').disabled=busy()||job.has_translation;
  $('estimate-translation').hidden=job.has_translation;
  $('estimate-revision').disabled=busy()||!job.has_translation||!job.config.revision_enabled||job.revision_complete;
  $('estimate-revision').hidden=!job.has_translation||!job.config.revision_enabled||job.revision_complete;
  $('save-config').disabled=busy();
  if(!dirty){
    $('source').value=job.config.source;
    $('target').value=job.config.target;
    $('translation-model').value=job.config.translation_model;
    $('revision-model').value=job.config.revision_model;
    $('revision-enabled').checked=job.config.revision_enabled;
    $('budget').value=job.config.max_cost;
  }['source','target','translation-model'].forEach(id=>$(id).disabled=busy()||job.config_frozen);
  $('revision-model').disabled=busy()||job.revision_model_frozen;
  $('budget').disabled=busy();
  $('revision-enabled').disabled=busy()||job.revision_complete;
  $('revision-settings').hidden=!$('revision-enabled').checked;
  $('download-translated').hidden=!job.has_translation;
  $('download-translated').href=`/api/jobs/${job.id}/download/translated`;
  $('progress-help').textContent=busy()?'You can keep this page open or return later. Paid work is not restarted by a refresh.':job.has_translation?'Translation is saved. Revision suggestions require their own approval.':'Estimate translation to start or resume with preserved spending.';
  $('download-final').hidden=!job.has_final;
  $('download-final').href=`/api/jobs/${job.id}/download/revised`;
  $('export').disabled=!job.revision_complete||job.review.remaining>0;
  $('review-summary').replaceChildren();
  ['total','accepted','rejected','edited','remaining'].forEach(k=>{
    const span=document.createElement('span');
    span.textContent=`${k.charAt(0).toUpperCase()+k.slice(1)} ${job.review[k]}`;
    $('review-summary').append(span);
  });
  $('review-progress').max=Math.max(1,job.review.total);
  $('review-progress').value=job.review.total-job.review.remaining;
  $('review').hidden=!job.revision_complete;
  $('review-empty').hidden=!job.has_translation||job.revision_complete||busy();
  $('cost-controls').hidden=job.revision_complete||busy();
  if(job.revision_complete && loadedReview!==job.id){
    reviewState=await api(`/api/jobs/${job.id}/review-state`);
    suggestions=await api(`/api/jobs/${job.id}/suggestions`);
    loadedReview=job.id;
    index=0;
    const selectedCategory=$('filter').value;
    $('filter').replaceChildren(new Option('All categories',''));
    [...new Set(suggestions.map(s=>s.category))].forEach(c=>$('filter').add(new Option(labels[c]||c,c)));
    $('filter').value=[...new Set(suggestions.map(s=>s.category))].includes(selectedCategory)?selectedCategory:'';
    showSuggestion();
  }
  updateReviewControls();
  renderWorkflow();
}
function updateReviewControls(){
  const remaining=reviewState?.counts?.undecided??job?.review.remaining??0;
  ['bulk-accept','bulk-reject'].forEach(id=>$(id).disabled=reviewBusy||!job?.revision_complete||remaining===0);
  $('export-current').disabled=reviewBusy||!job?.revision_complete;
  $('undo-bulk').disabled=reviewBusy||!reviewState?.undo_available;
  $('download-current').hidden=!reviewState?.has_current_export;
  if(job)$('download-current').href=`/api/jobs/${job.id}/download/current`;
}
async function openReviewAction(action){
  if(reviewBusy||$('review-dialog').open)return;
  if(!$('editor').hidden)throw new Error('Save or cancel the current edit before a bulk action or export.');
  reviewBusy=true;updateReviewControls();
  try{
    const ident=job.id,cursor=index;
    job=await api(`/api/jobs/${ident}`);
    loadedReview='';await renderJob();index=cursor;showSuggestion();
    const c=reviewState.counts,n=c.undecided;
    if(['accept','reject'].includes(action)&&!n)throw new Error('No undecided suggestions remain.');
    if(action==='restore'&&!reviewState.undo_available)throw new Error('Snapshot restore is unavailable after newer decisions.');
    const title=action==='accept'?`Accept all ${n} remaining suggestions?`:action==='reject'?`Reject all ${n} remaining suggestions?`:action==='export'?'Export current decisions?':'Undo latest bulk action?';
    const text=action==='accept'?'This will apply AI-proposed corrections that you have not individually reviewed.\n\nYour existing decisions will remain unchanged.':
      action==='reject'?'The original translations will be preserved for these entries.':
      action==='export'?`Accepted: ${c.accepted}\nRejected: ${c.rejected}\nEdited: ${c.edited}\nUndecided: ${c.undecided}\n\nUndecided suggestions will retain the original translation.`:
      'Restore the pre-operation review-state snapshot. Suggestions decided by the latest bulk action will become undecided again. Earlier decisions and edits remain unchanged.';
    pendingReview={action,ident,expected_version:reviewState.version,operation_id:crypto.randomUUID().replaceAll('-','')};
    $('review-dialog-title').textContent=title;$('review-dialog-text').textContent=text;
    $('review-confirm-submit').textContent=action==='accept'?`Accept ${n} suggestions`:action==='reject'?`Reject ${n} suggestions`:action==='export'?'Export current decisions':'Restore snapshot';
    $('review-confirm-submit').className=action==='accept'?'bulk-accept':'secondary';
    $('review-dialog').showModal();$('review-confirm-cancel').focus();
  }finally{reviewBusy=false;updateReviewControls();}
}
async function confirmReviewAction(){
  if(!pendingReview||reviewBusy)return;
  const context={...pendingReview};
  if(job.id!==context.ident)throw new Error('Saved job changed; reopen confirmation.');
  reviewBusy=true;updateReviewControls();
  $('review-confirm-submit').disabled=true;$('review-confirm-cancel').disabled=true;
  try{
    const payload={confirmed:true,expected_version:context.expected_version,operation_id:context.operation_id};
    if(['accept','reject'].includes(context.action))payload.action=context.action;
    const route=context.action==='export'?'export/current':context.action==='restore'?'review/restore':'review/bulk';
    await api(`/api/jobs/${context.ident}/${route}`,json('POST',payload));
    $('review-dialog').close();pendingReview=null;
    job=await api(`/api/jobs/${context.ident}`);loadedReview='';await renderJob();
    message(context.action==='export'?'Current decisions exported. Undecided translations and review decisions are unchanged. Use the download button.':context.action==='restore'?'Pre-bulk decisions restored.':'Bulk decisions saved. Earlier decisions and edits were preserved.');
  }catch(error){
    $('review-dialog').close();pendingReview=null;
    loadedReview='';job=await api(`/api/jobs/${context.ident}`);await renderJob();
    throw error;
  }finally{
    reviewBusy=false;$('review-confirm-submit').disabled=false;$('review-confirm-cancel').disabled=false;updateReviewControls();
  }
}
function filtered(){
  return suggestions.filter(s=>!$('filter').value||s.category===$('filter').value);
}
function current(){
  return filtered()[index];
}
async function context(){
  const s=current();
  if(!s||!$('context-details').open)return;
  const rows=await api(`/api/jobs/${job.id}/context/${s.id}`);
  if(current()?.id!==s.id)return;
  $('context').replaceChildren();
  rows.forEach(row=>{
    const div=document.createElement('div');
    div.className='context-row';
    const label=document.createElement('small');
    label.textContent=`${row.id} · ${row.timestamp}`;
    div.append(label);
    ['original','current'].forEach(k=>{
      const pre=document.createElement('pre');
      SubtitleUI.preview(pre,row[k],$('raw-preview').checked);
      div.append(pre);
    });
    $('context').append(div);
  });
}
function showSuggestion(){
  const list=filtered();
  index=Math.max(0,Math.min(index,list.length-1));
  const s=current();
  $('suggestion').hidden=!s;
  $('no-suggestions').hidden=!!s;
  $('no-suggestions').textContent=suggestions.length?'No suggestions match this filter.':'No changes suggested. Exporting keeps your translation unchanged.';
  $('review-position').textContent=s?`${index+1} / ${list.length}`:'0 suggestions';
  $('editor').hidden=true;
  if(!s)return;
  $('cue').textContent=`Subtitle ${s.id} · ${s.timestamp}`;
  $('original-label').textContent='Original '+(languages[job.config.source]||job.config.source);
  $('current-label').textContent='Current '+(languages[job.config.target]||job.config.target);
  $('suggested-label').textContent='Suggested '+(languages[job.config.target]||job.config.target);
  renderComparison(s);
  $('explanation').textContent=s.explanation;
  if(s.decision?.action==='edit'){
    $('suggested-label').textContent='Your edit';
    SubtitleUI.preview($('suggested'),s.decision.text,$('raw-preview').checked);
  }$('category').textContent=labels[s.category]||s.category;
  $('decision').textContent=s.decision?({
    accept:'Accepted',reject:'Rejected',edit:'Edited'
  }[s.decision.action]):'Decision needed';
  $('previous').disabled=index===0;
  $('next').disabled=index===list.length-1;
  $('edit-text').value=s.decision?.action==='edit'?s.decision.text:s.suggested;
  context().catch(e=>message(e.message));
}
function renderComparison(s){
  ['original','current','suggested'].forEach(k=>SubtitleUI.preview($(k),s[k],$('raw-preview').checked));
  if(s.decision?.action==='edit')SubtitleUI.preview($('suggested'),s.decision.text,$('raw-preview').checked);
}
async function decide(action){
  const s=current();
  if(!s)return;
  const text=action==='edit'?$('edit-text').value:undefined;
  job=await api(`/api/jobs/${job.id}/decisions/${s.id}`,json('PUT',{
    action,expected_version:reviewState?.version,...(text!==undefined?{
      text
    }: {
    })
  }));
  s.decision={action,text};
  loadedReview='';
  const nextIndex=Math.min(index+1,filtered().length-1);
  await renderJob();
  index=nextIndex;
  showSuggestion();
  message('');
}
async function estimate(operation){
  await saveSettings();
  quote=await api(`/api/jobs/${job.id}/estimate/${operation}`,{
    method:'POST'
  });
  costCalibration=null;costComparable=null;
  $('calibration-use').checked=true;
  try{costComparable=(await api(`/api/jobs/${job.id}/history/${operation}`)).comparable;}catch(_){/* Unavailable evidence is omitted. */}
  if(operation==='revision'){
    try{
      const data=await api(`/api/jobs/${job.id}/revision-cost/${quote.quote_id}`);
      if(data.quote_id===quote.quote_id)costCalibration=data.forecast;
    }catch(_){/* Missing or incompatible evidence keeps the original heuristic. */}
  }
  renderCostPresentation();
  $('spend-approval').checked=false;
  $('start').disabled=true;
  $('start').textContent=`Approve ${operation} & start`;
  renderWorkflow();
}

function renderCostPresentation(){
  if(!quote)return;
  const selectedCalibration=$('calibration-use').checked?costCalibration:null;
  $('cost-placeholder').hidden=true;
  $('quote').hidden=false;
  $('quote-title').textContent=quote.operation==='translation'?'Translation approval':'Revision approval';
  $('quote-file').textContent=`${quote.filename} · ${quote.entries} subtitles`;
  $('quote-models').textContent=`Translation: ${quote.config.translation_model} · Revision: ${quote.config.revision_enabled?quote.config.revision_model:'disabled'}`;
  $('quote-values').replaceChildren();
  $('quote-highlights').replaceChildren();
  SubtitleUI.costCards(quote,costComparable,selectedCalibration).forEach(card=>{
    const div=document.createElement('div'),title=document.createElement('h3'),value=document.createElement('strong'),note=document.createElement('small');
    div.className='cost-highlight';title.textContent=card.label;value.textContent=card.approximate?`About $${Number(card.value).toFixed(2)}`:money(card.value);note.textContent=card.note;
    div.append(title,value,note);$('quote-highlights').append(div);
  });
  const rows=[['Actual spending so far',money(quote.spent)],['Budget remaining',money(quote.remaining)],['Unsettled usage reserved',money(quote.reservations)]];
  if(quote.revision_preview)rows.push(['Optional revision estimate (provisional)',money(quote.revision_preview.estimated_cost)]);
  rows.forEach(([k,v])=>{
    const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');
    dt.textContent=k;
    dd.textContent=v;
    div.append(dt,dd);
    $('quote-values').append(div);
  });
  $('estimate-values').replaceChildren();
  const details=[['Extreme conservative scenario',money(quote.conservative_cost)],['Estimated input / output tokens',`${quote.input_tokens.toLocaleString()} / ${quote.output_tokens.toLocaleString()}`],['Translation model',quote.config.translation_model],['Revision model',quote.config.revision_enabled?quote.config.revision_model:'Disabled']];
  if(quote.operation==='revision'){
    details.push(['Heuristic all-cues-edited estimate',money(quote.estimated_cost)]);
    if(selectedCalibration?.kind==='calibrated')details.push(
      ['Calibrated input / output tokens',`${selectedCalibration.input_tokens.toLocaleString()} / ${selectedCalibration.output_tokens.toLocaleString()}`],
      ['Observed-ratio sensitivity (not guaranteed bounds)',`${money(selectedCalibration.sensitivity_low)} – ${money(selectedCalibration.sensitivity_high)}`]);
  }
  if(quote.revision_preview)details.push(['Revision extreme scenario (provisional)',money(quote.revision_preview.conservative_cost)]);
  details.forEach(([k,v])=>{const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=v;div.append(dt,dd);$('estimate-values').append(div);});
  $('estimate-details').open=false;
  $('budget-warning').hidden=Number(quote.conservative_cost)<=Number(quote.remaining);
  $('budget-warning').textContent=`The extreme completion scenario exceeds the remaining budget. Your total job limit stays ${money(quote.max_cost)}. Each request is checked; work may pause if it cannot fit. This does not mean the extreme amount is required for normal execution.`;
  $('calibration-controls').hidden=costCalibration?.kind!=='calibrated';
  $('calibration-evidence').hidden=quote.operation!=='revision';
  if(quote.operation==='revision'){
    if(selectedCalibration?.kind==='calibrated'){
      const e=selectedCalibration.evidence,c=e.configuration_match;
      $('calibration-evidence').textContent=`${e.samples} samples · ${e.cues} cues · ${e.files} source file(s) · measurements ${e.measured_from} to ${e.measured_to}. Configuration matches ${c.model}, ${c.source} → ${c.target}, batch ${c.batch_size}, context ${c.context}, reasoning ${c.reasoning}, prompt ${c.prompt_version}, schema ${c.schema_version}. ${e.uncertainty}`;
    }else $('calibration-evidence').textContent=$('calibration-use').checked?(costCalibration?.reason||'Verified matching calibration unavailable; heuristic shown.'):'Empirical calibration disabled for this display; heuristic shown. Request allowances and spending guard are unchanged.';
  }
}
$('calibration-use').onchange=()=>{
  renderCostPresentation();
  $('spend-approval').checked=false;
  $('start').disabled=true;
};
function safe(fn){
  return async()=>{
    try{
      await fn();
    }catch(e){
      message(e.message);
    }
  };
}
$('drop').addEventListener('click',()=> $('file').click());
$('drop').addEventListener('keydown',e=>{
  if(['Enter',' '].includes(e.key)){
    e.preventDefault();
    $('file').click();
  }
});
['dragenter','dragover'].forEach(k=>$('drop').addEventListener(k,e=>{
  e.preventDefault();
  $('drop').classList.add('over');
}));
['dragleave','drop'].forEach(k=>$('drop').addEventListener(k,e=>{
  e.preventDefault();
  $('drop').classList.remove('over');
}));
$('drop').addEventListener('drop',e=>upload(e.dataTransfer.files[0]).catch(e=>message(e.message)));
$('file').addEventListener('change',e=>upload(e.target.files[0]).catch(e=>message(e.message)));
$('jobs').addEventListener('change',e=>select(e.target.value).catch(e=>message(e.message)));
$('configuration').addEventListener('submit',e=>e.preventDefault());
$('configuration').addEventListener('input',()=>{
  dirty=true;
  clearQuote();
  $('revision-settings').hidden=!$('revision-enabled').checked;
  renderWorkflow();
});
$('review-settings').onclick=openSettings;
$('raw-preview').onchange=()=>{
  if(current())renderComparison(current());
  context().catch(e=>message(e.message));
};
$('save-config').onclick=safe(saveSettings);
$('estimate-translation').onclick=safe(()=>estimate('translation'));
$('estimate-revision').onclick=safe(()=>estimate('revision'));
$('spend-approval').onchange=()=> $('start').disabled=!$('spend-approval').checked;
$('cancel-approval').onclick=clearQuote;
$('start').onclick=safe(async()=>{
  if(!quote||!$('spend-approval').checked)return;
  const operation=quote.operation;
  $('start').disabled=true;
  job=await api(`/api/jobs/${job.id}/approve/${operation}`,json('POST',{
    approved:true,quote_id:quote.quote_id
  }));
  clearQuote();
  await renderJob();
});
$('pause').onclick=safe(async()=>{
  job=await api(`/api/jobs/${job.id}/pause`,{
    method:'POST'
  });
  await renderJob();
});
$('filter').onchange=()=>{
  index=0;
  showSuggestion();
};
$('previous').onclick=()=>{
  index--;
  showSuggestion();
};
$('next').onclick=()=>{
  index++;
  showSuggestion();
};
$('context-details').ontoggle=()=>context().catch(e=>message(e.message));
$('accept').onclick=safe(()=>decide('accept'));
$('reject').onclick=safe(()=>decide('reject'));
$('edit').onclick=()=>{
  $('editor').hidden=false;
  $('edit-text').focus();
};
$('cancel-edit').onclick=()=> $('editor').hidden=true;
$('save-edit').onclick=safe(()=>decide('edit'));
$('export').onclick=safe(async()=>{
  job=await api(`/api/jobs/${job.id}/export`,json('POST',{expected_version:reviewState?.version}));
  await renderJob();
  message('Reviewed SRT saved. Use the download button to save a browser copy.');
});
$('bulk-accept').onclick=safe(()=>openReviewAction('accept'));
$('bulk-reject').onclick=safe(()=>openReviewAction('reject'));
$('export-current').onclick=safe(()=>openReviewAction('export'));
$('undo-bulk').onclick=safe(()=>openReviewAction('restore'));
$('review-confirm-cancel').onclick=()=>{if(!reviewBusy){pendingReview=null;$('review-dialog').close();}};
$('review-dialog').oncancel=e=>{if(reviewBusy)e.preventDefault();else pendingReview=null;};
$('review-confirm-submit').onclick=safe(confirmReviewAction);
document.addEventListener('keydown',e=>{
  if($('review-dialog').open)return;
  if(e.target===$('edit-text')&&(e.ctrlKey||e.metaKey)&&e.key==='Enter'){
    e.preventDefault();
    safe(()=>decide('edit'))();
    return;
  }if(['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)||!current())return;
  if(e.altKey&&['a','r','e'].includes(e.key.toLowerCase())){
    e.preventDefault();
    ({
      a:$('accept'),r:$('reject'),e:$('edit')
    })[e.key.toLowerCase()].click();
  }if(e.key==='ArrowLeft'&&!$('previous').disabled){
    e.preventDefault();
    $('previous').click();
  }if(e.key==='ArrowRight'&&!$('next').disabled){
    e.preventDefault();
    $('next').click();
  }
});
async function poll(){
  try{
    if(job){
      job=await api('/api/jobs/'+job.id);
      await renderJob();
    }
  }catch(e){
    message(e.message);
  }finally{
    setTimeout(poll,1500);
  }
}
(async()=>{
  renderWorkflow();
  try{
    csrf=(await api('/api/session')).csrf_token;
    const options=await api('/api/options');
    languages=options.languages;
    Object.entries(languages).forEach(([code,name])=>$('languages').append(new Option(name,code)));
    const saved=await listJobs();
    const id=localStorage.getItem('subtitle-job');
    if(saved.some(j=>j.id===id))await select(id);
    poll();
  }catch(e){
    message(e.message);
  }
})();
