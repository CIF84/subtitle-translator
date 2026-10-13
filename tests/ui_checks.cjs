// Offline unit DOM and fetch doubles; no browser, network, or runtime dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(process.argv[2]);
const UI = require(path.join(root,'web/static/presentation.js'));

class Element {
  constructor(tag='div') {
    this.tagName=tag.toUpperCase();this.children=[];this.attributes={};this.dataset={};
    this.hidden=false;this.disabled=false;this.checked=false;this.open=false;
    this.value='';this._text='';this.events={};this.classList={add(){},remove(){}};
  }
  set textContent(text){this._text=String(text);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  get options(){return this.children.filter(c=>c.tagName==='OPTION');}
  append(...children){for(const c of children){if(c.tagName==='FRAGMENT')this.children.push(...c.children);else this.children.push(c);}}
  replaceChildren(...children){this._text='';this.children=[];this.append(...children);}
  setAttribute(k,v){this.attributes[k]=String(v);}
  addEventListener(k,fn){this.events[k]=fn;}
  add(child){this.append(child);}
  click(){if(!this.disabled&&this.onclick)return this.onclick();}
  focus(){doc.activeElement=this;}
  showModal(){this.open=true;}
  close(){this.open=false;}
  scrollIntoView(){}
}
const nodes=new Map();
const doc={createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),
  createTextNode:text=>{const n=new Element('#text');n.textContent=text;return n;},
  getElementById:id=>{assert(nodes.has(id),'Missing UI element '+id);return nodes.get(id);},
  addEventListener(){}};
const flatten=node=>[node,...node.children.flatMap(flatten)];

// Independently authored enclosure example, fictional speaker and SDH.
const original='[Nora]\n<i>The terrarium needs a lamp.</i>\n- [water trickling]';
const target=new Element('pre');UI.preview(target,original,false,doc);
assert.equal(target.textContent,'[Nora]\nThe terrarium needs a lamp.\n- [water trickling]');
assert(flatten(target).some(n=>n.tagName==='I'));
UI.preview(target,'<b>bold <u>underlined</u></b>\n<font color="#FF0000">red</font>',false,doc);
assert(flatten(target).some(n=>n.tagName==='B'));
assert(flatten(target).some(n=>n.tagName==='U'));
assert.equal(flatten(target).find(n=>n.tagName==='FONT').attributes.color,'#ff0000');
for(const hostile of ['<script>alert(1)</script>','<img src=x onerror=alert(1)>',
  '<i onclick="alert(1)">dialogue</i>','<font color="javascript:alert(1)">bad</font>',
  '<font color="red" onmouseover="steal()">bad</font>','<iframe src="https://evil.test"></iframe>',
  '<svg/onload=alert(1)>','<a href="javascript:alert(1)">link</a>',
  '<font color="url(https://evil.test)">text</font>']) {
  UI.preview(target,hostile,false,doc);
  assert.equal(target.textContent,hostile);
  assert(flatten(target).every(n=>['PRE','#TEXT'].includes(n.tagName)));
}
for(const malformed of ['<i>missing','<b><i>wrong order</b></i>','</u>unexpected']) {
  UI.preview(target,malformed,false,doc);assert.equal(target.textContent,malformed);
  assert.equal(flatten(target).filter(n=>['I','B','U'].includes(n.tagName)).length,0);
}
UI.preview(target,original,true,doc);assert.equal(target.textContent,original);
UI.preview(target,'{\\an8}♪ [gentle music] ♪\nJOHN: Hello.\n- Stay.',false,doc);
assert.equal(target.textContent,'{\\an8}♪ [gentle music] ♪\nJOHN: Hello.\n- Stay.');

const base={id:'offline',filename:'Example.en.srt',entries:2,status:'uploaded',completed:0,
  config:{source:'en',target:'cs',translation_model:'gpt-5.4-mini',revision_model:'gpt-5.4',revision_enabled:false,max_cost:'1.00'},
  config_frozen:false,revision_model_frozen:false,has_translation:false,has_final:false,revision_complete:false,
  error:null,translation_cost:'0',revision_cost:'0',remaining:'1.00',reservations:'0',revision_entries_complete:0,
  review:{total:0,accepted:0,rejected:0,edited:0,remaining:0},created:1};
assert.equal(UI.stage(null).index,0);
assert.equal(UI.stage(base,false,null,false).index,1);
assert.equal(UI.stage(base,true).action,'settings');
assert.equal(UI.stage(base).index,2);
assert.equal(UI.stage({...base,status:'translation_running'}).index,3);
assert.equal(UI.stage({...base,status:'translated',has_translation:true}).action,'download-translated');
assert.equal(UI.stage({...base,status:'translated',has_translation:true,config:{...base.config,revision_enabled:true}}).action,'revision');
assert.equal(UI.stage({...base,status:'revision_running',has_translation:true}).index,4);
const review={...base,status:'review',has_translation:true,revision_complete:true,review:{...base.review,total:1,remaining:1}};
assert.equal(UI.stage(review).action,'review');
assert.equal(UI.stage({...review,review:{...review.review,remaining:0}}).action,'export');
assert.equal(UI.stage({...review,has_final:true,review:{...review.review,remaining:0}}).action,'download-final');
assert.equal(UI.stage({...base,status:'failed'}).action,'translation');
assert.equal(UI.stage({...base,status:'paused',has_translation:true}).action,'revision');
assert.equal(UI.stage({...base,status:'interrupted'}).title,'Ready to resume');
assert.deepEqual(UI.stage({...review,has_final:true,review:{...review.review,remaining:0}}).completed,[0,1,2,3,4,5]);
assert(!UI.errorHelp('API key is empty').includes('sk-'));
assert(UI.errorHelp('Budget guard stopped').includes('actual spending'));

const quote={operation:'translation',filename:base.filename,entries:2,config:base.config,
  estimated_cost:'0.19307175',conservative_cost:'5.29659450',max_cost:'1.00',spent:'0',remaining:'1.00',
  reservations:'0',input_tokens:76697,output_tokens:30122,quote_id:'offline-quote'};
assert.deepEqual(UI.costCards(quote).map(c=>c.value),['0.19307175','1.00']);
assert(!UI.costCards(quote).some(c=>c.label.includes('conservative')));
assert.equal(UI.costCards(quote,{cost:'.11323650',entries:2,includes_successful_probes:true}).length,3);
assert(UI.costCards(quote,{cost:'.11323650',entries:2,includes_successful_probes:true})[2].note.includes('probe'));

const calibrated={kind:'calibrated',expected_cost:'0.3496675',input_tokens:58327,output_tokens:13590,
  sensitivity_low:'0.2613175',sensitivity_high:'0.4225075',evidence:{samples:3,cues:75,files:1,
  measured_from:'2026-10-13',measured_to:'2026-10-13',configuration_match:{model:'gpt-5.4',source:'en',target:'cs',batch_size:25,context:5,reasoning:'none',prompt_version:'wp3-revision-1.0',schema_version:'wp2-corrections-1.0'},
  uncertainty:'Small nonrandom samples; sensitivity is not guaranteed bounds.'}};
assert.equal(UI.costCards({...quote,operation:'revision'},null,calibrated)[0].value,'0.3496675');
assert(UI.costCards({...quote,operation:'revision'},null,calibrated)[0].label.includes('calibrated'));
assert(UI.costCards({...quote,operation:'revision'})[0].label.includes('heuristic'));
assert.equal(UI.costCards(quote,null,calibrated)[0].value,quote.estimated_cost);

// Exercise actual app handlers with local DOM and API doubles.
const html=fs.readFileSync(path.join(root,'web/static/index.html'),'utf8');
for(const match of html.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
  const n=new Element(match[1]);n.hidden=/\bhidden\b/.test(match[0]);n.disabled=/\bdisabled\b/.test(match[0]);
  n.open=/\bopen\b/.test(match[0]);nodes.set(match[2],n);
}
const storage=new Map(),calls=[];
let persisted=structuredClone(base),serverRows=[],reviewGeneration=0,bulkSnapshot=null,currentExport=false;
const reviewToken=()=>reviewGeneration.toString(16).padStart(64,'0');
const reviewCounts=()=>({total:persisted.review.total,accepted:persisted.review.accepted,rejected:persisted.review.rejected,edited:persisted.review.edited,undecided:persisted.review.remaining});
function recount(){
  persisted.review={total:serverRows.length,accepted:serverRows.filter(s=>s.decision?.action==='accept').length,rejected:serverRows.filter(s=>s.decision?.action==='reject').length,edited:serverRows.filter(s=>s.decision?.action==='edit').length,remaining:serverRows.filter(s=>!s.decision).length};
}

const ctx={document:doc,SubtitleUI:{...UI,preview:(node,text,raw)=>UI.preview(node,text,raw,doc)},Option:class extends Element{constructor(text,value){super('option');this.textContent=text;this.value=value;}},
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
  setTimeout(){},console,crypto:{randomUUID:()=>require('node:crypto').randomUUID()},
  fetch:async(url,options={})=>{
    assert(url.startsWith('/api/'),'No external network permitted');calls.push({url,...options});
    let data;
    if(url==='/api/session')data={csrf_token:'offline-csrf'};
    else if(url==='/api/options')data={languages:{en:'English',cs:'Czech'}};
    else if(url==='/api/jobs')data=[persisted];
    else if(url.endsWith('/config')){persisted.config=JSON.parse(options.body);data=persisted;}
    else if(url.includes('/estimate/'))data={...quote,operation:url.endsWith('/revision')?'revision':'translation',config:persisted.config};
    else if(url.includes('/revision-cost/'))data={quote_id:quote.quote_id,forecast:calibrated};
    else if(url.includes('/history/'))data={comparable:null};
    else if(url.includes('/approve/')){assert.equal(JSON.parse(options.body).approved,true);persisted.status='translation_queued';data=persisted;}
    else if(url.endsWith('/review-state'))data={version:reviewToken(),counts:reviewCounts(),undo_available:!!bulkSnapshot&&bulkSnapshot.applied===reviewGeneration,has_current_export:currentExport};
    else if(url.includes('/decisions/')){
      const body=JSON.parse(options.body);assert.equal(body.expected_version,reviewToken());
      const row=serverRows.find(r=>r.id===url.split('/').at(-1));
      row.decision={action:body.action,text:body.action==='accept'?row.suggested:body.action==='reject'?row.current:body.text};
      reviewGeneration++;recount();data=persisted;
    }
    else if(url.endsWith('/review/bulk')){
      const body=JSON.parse(options.body);assert.equal(body.confirmed,true);assert.equal(body.expected_version,reviewToken());
      bulkSnapshot={rows:structuredClone(serverRows),applied:reviewGeneration+1};
      serverRows.forEach(s=>{if(!s.decision)s.decision={action:body.action,text:body.action==='accept'?s.suggested:s.current};});
      reviewGeneration++;recount();data=persisted;
    }
    else if(url.endsWith('/review/restore')){
      const body=JSON.parse(options.body);assert.equal(body.confirmed,true);assert.equal(body.expected_version,reviewToken());
      serverRows=structuredClone(bulkSnapshot.rows);bulkSnapshot=null;reviewGeneration++;recount();data=persisted;
    }
    else if(url.endsWith('/export/current')){
      const body=JSON.parse(options.body);assert.equal(body.confirmed,true);assert.equal(body.expected_version,reviewToken());
      currentExport=true;data={download_url:'/api/jobs/offline/download/current',counts:reviewCounts(),review_version:reviewToken()};
    }
    else if(url.endsWith('/suggestions'))data=serverRows;
    else if(url.includes('/context/'))data=[{id:'1',timestamp:'00:03:41,125 --> 00:03:44,875',original,current:'<i>Terárium potřebuje lampu.</i>'}];
    else if(url.includes('/api/jobs/'))data=persisted;
    else throw new Error('Unmocked request '+url);
    return {ok:true,json:async()=>structuredClone(data)};
  }};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(root,'web/static/app.js'),'utf8'),ctx);
const run=code=>vm.runInContext(code,ctx);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  for(let i=0;i<5;i++)await tick();
  assert.equal(nodes.get('stage-title').textContent,'Upload your subtitles');
  await run('select("offline")');
  assert.equal(nodes.get('stage-title').textContent,'Configure your translation');
  await run('estimate("translation")');
  assert.equal(nodes.get('start').disabled,true);assert.equal(nodes.get('spend-approval').checked,false);
  assert.equal(nodes.get('quote-highlights').children.length,2);
  assert(nodes.get('quote-highlights').textContent.includes('$0.193072'));
  assert(nodes.get('quote-highlights').textContent.includes('$1.000000'));
  assert(!nodes.get('quote-highlights').textContent.includes('$5.296595'));
  assert(nodes.get('estimate-values').textContent.includes('$5.296595'));
  assert.equal(nodes.get('estimate-details').open,false);
  assert(!calls.some(c=>c.url.includes('/approve/')));
  await nodes.get('next-action').click(); // Scroll to consent; never approve.
  assert(!calls.some(c=>c.url.includes('/approve/')));
  await nodes.get('start').onclick();assert(!calls.some(c=>c.url.includes('/approve/')));
  nodes.get('spend-approval').checked=true;nodes.get('spend-approval').onchange();
  assert.equal(nodes.get('start').disabled,false);
  nodes.get('cancel-approval').onclick();assert.equal(nodes.get('start').disabled,true);
  assert.equal(nodes.get('spend-approval').checked,false);
  await run('select("offline")'); // Config progress survives a fresh selection.
  assert.equal(nodes.get('stage-title').textContent,'Estimate translation');
  await run('estimate("revision")');assert(nodes.get('quote-title').textContent.includes('Revision'));
  assert.equal(nodes.get('spend-approval').checked,false);
  assert(nodes.get('start').textContent.includes('revision'));
  assert(nodes.get('quote-highlights').textContent.includes('About $0.35'));
  assert(nodes.get('quote-highlights').textContent.includes('calibrated'));
  assert(nodes.get('calibration-evidence').textContent.includes('3 samples · 75 cues'));
  assert(nodes.get('calibration-evidence').textContent.includes('2026-10-13'));
  assert(nodes.get('estimate-values').textContent.includes('not guaranteed bounds'));
  assert(nodes.get('estimate-values').textContent.includes('$5.296595')); // Unchanged conservative value in double.
  assert.equal(run('quote.estimated_cost'),quote.estimated_cost);
  assert.equal(run('quote.quote_id'),quote.quote_id);
  nodes.get('spend-approval').checked=true;nodes.get('spend-approval').onchange();
  nodes.get('calibration-use').checked=false;nodes.get('calibration-use').onchange();
  assert(nodes.get('quote-highlights').textContent.includes('heuristic'));
  assert(nodes.get('quote-highlights').textContent.includes('$0.193072'));
  assert.equal(nodes.get('start').disabled,true);assert.equal(nodes.get('spend-approval').checked,false);
  assert(!calls.some(c=>c.url.includes('/approve/')));
  nodes.get('calibration-use').checked=true;nodes.get('calibration-use').onchange();
  assert(nodes.get('quote-highlights').textContent.includes('About $0.35'));
  run('clearQuote()');
  persisted={...review,config:{...base.config,revision_enabled:true},config_frozen:true};
  serverRows=[{id:'1',timestamp:'00:03:41,125 --> 00:03:44,875',original,current:'<i>Terárium má prasklé sklo.</i>',
    suggested:'<i>Terárium potřebuje lampu.</i>',explanation:'The enclosure needs suitable lighting.',category:'semantic_error',
    decision:{action:'edit',text:'<i>Terárium potřebuje nové osvětlení.</i>'}}];
  await run('select("offline")');
  assert.equal(nodes.get('suggested').textContent,'Terárium potřebuje nové osvětlení.');
  assert.equal(nodes.get('edit-text').value,'<i>Terárium potřebuje nové osvětlení.</i>');
  assert(flatten(nodes.get('original')).some(n=>n.tagName==='I'));
  nodes.get('raw-preview').checked=true;nodes.get('raw-preview').onchange();
  assert.equal(nodes.get('suggested').textContent,'<i>Terárium potřebuje nové osvětlení.</i>');
  assert.equal(nodes.get('edit-text').value,'<i>Terárium potřebuje nové osvětlení.</i>');
  assert.equal(nodes.get('export').disabled,true);
  await run('select("offline")'); // Persisted edit, raw source and pending count retained.
  assert.equal(nodes.get('edit-text').value,'<i>Terárium potřebuje nové osvětlení.</i>');
  assert.equal(nodes.get('export').disabled,true);
  nodes.get('context-details').open=true;await run('context()');
  assert(nodes.get('context').textContent.includes('[Nora]'));
  assert(!calls.some(c=>c.url.includes('/approve/')));
  // New review actions: confirm explicitly, preserve previous decisions, no auto acceptance.
  persisted={...review,config:{...base.config,revision_enabled:true},config_frozen:true,review:{total:3,accepted:1,rejected:0,edited:0,remaining:2}};
  serverRows=[1,2,3].map(n=>({id:String(n),timestamp:'00:03:41,125 --> 00:03:44,875',original:'English',current:'Current '+n,suggested:'Suggested '+n,explanation:'Reason',category:'semantic_error',decision:n===1?{action:'accept',text:'Suggested 1'}:null}));
  await run('select("offline")');
  assert.equal(nodes.get('bulk-accept').disabled,false);assert.equal(nodes.get('export-current').disabled,false);
  const oldDecisions=structuredClone(serverRows[0].decision);
  let mutations=calls.filter(c=>c.url.endsWith('/review/bulk')).length;
  await nodes.get('bulk-accept').click();
  assert.equal(nodes.get('review-dialog').open,true);assert.equal(doc.activeElement,nodes.get('review-confirm-cancel'));
  assert(nodes.get('review-dialog-title').textContent.includes('Accept all 2 remaining'));
  assert(nodes.get('review-dialog-text').textContent.includes('not individually reviewed'));
  assert.equal(nodes.get('review-confirm-submit').textContent,'Accept 2 suggestions');
  assert.equal(calls.filter(c=>c.url.endsWith('/review/bulk')).length,mutations);
  nodes.get('review-confirm-cancel').click();assert.equal(nodes.get('review-dialog').open,false);
  assert.equal(calls.filter(c=>c.url.endsWith('/review/bulk')).length,mutations);
  await nodes.get('export-current').click();
  assert(nodes.get('review-dialog-text').textContent.includes('Accepted: 1'));
  assert(nodes.get('review-dialog-text').textContent.includes('Undecided: 2'));
  assert(nodes.get('review-dialog-text').textContent.includes('Undecided suggestions will retain'));
  await nodes.get('review-confirm-submit').click();
  assert.equal(persisted.review.remaining,2);assert.equal(nodes.get('download-current').hidden,false);
  assert.equal(nodes.get('export').disabled,true);
  await nodes.get('bulk-accept').click();await nodes.get('review-confirm-submit').click();
  assert.equal(calls.filter(c=>c.url.endsWith('/review/bulk')).length,mutations+1);
  assert.deepEqual(serverRows[0].decision,oldDecisions);
  assert.equal(persisted.review.remaining,0);assert.equal(nodes.get('bulk-accept').disabled,true);
  assert.equal(nodes.get('bulk-reject').disabled,true);assert.equal(nodes.get('undo-bulk').disabled,false);
  await nodes.get('undo-bulk').click();assert(nodes.get('review-dialog-title').textContent.includes('Undo'));
  await nodes.get('review-confirm-submit').click();assert.equal(persisted.review.remaining,2);
  await nodes.get('bulk-reject').click();assert(nodes.get('review-dialog-title').textContent.includes('Reject all 2'));
  assert(nodes.get('review-dialog-text').textContent.includes('original translations'));
  await nodes.get('review-confirm-submit').click();assert.equal(persisted.review.rejected,2);
  assert.deepEqual(serverRows[0].decision,oldDecisions);
  await run('select("offline")');assert.equal(nodes.get('bulk-accept').disabled,true);
  assert.equal(nodes.get('export').disabled,false);
  run('index=1;showSuggestion()');
  await run('decide("accept")');
  assert.equal(run('index'),2); // Preserve next-suggestion navigation after refreshing tokens.
  assert.equal(nodes.get('undo-bulk').disabled,true);
  assert(!calls.some(c=>c.url.includes('/approve/')));
  console.log('WP5 frontend checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
