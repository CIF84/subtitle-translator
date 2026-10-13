'use strict';
// Pure display helpers. They never send requests or modify persisted SRT text.
const SubtitleUI = (() => {
  const names = ['Upload', 'Configure', 'Estimate', 'Translate', 'Review', 'Save'];
  const colors = new Set(['white','black','red','green','blue','yellow','cyan','magenta',
    'orange','purple','pink','gray','grey','lime','navy','teal','silver','maroon']);

  function tokens(text) {
    const result = [], stack = [];
    for (const part of String(text).split(/(<[^>\n]*>)/g)) {
      const open = /^<(i|b|u|font)\s*>$/i.exec(part);
      const font = /^<font\s+color\s*=\s*(["'])([a-z]+|#[0-9a-f]{3}|#[0-9a-f]{6})\1\s*>$/i.exec(part);
      const close = /^<\/(i|b|u|font)\s*>$/i.exec(part);
      if (open || (font && (font[2].startsWith('#') || colors.has(font[2].toLowerCase())))) {
        const tag = open ? open[1].toLowerCase() : 'font';
        result.push({kind:'open', tag, color:font ? font[2].toLowerCase() : null});
        stack.push(tag);
      } else if (close) {
        if (stack.pop() !== close[1].toLowerCase()) return [{kind:'text', text:String(text)}];
        result.push({kind:'close'});
      } else if (part) result.push({kind:'text', text:part});
    }
    return stack.length ? [{kind:'text', text:String(text)}] : result;
  }

  function preview(target, text, raw=false, doc=document) {
    if (raw) { target.textContent = text; return; }
    const fragment = doc.createDocumentFragment(), parents = [fragment];
    for (const token of tokens(text)) {
      if (token.kind === 'text') parents.at(-1).append(doc.createTextNode(token.text));
      else if (token.kind === 'close') parents.pop();
      else {
        const node = doc.createElement(token.tag);
        if (token.color) node.setAttribute('color', token.color);
        parents.at(-1).append(node);
        parents.push(node);
      }
    }
    target.replaceChildren(fragment);
  }

  function stage(job, dirty=false, quote=null, configured=true) {
    if (!job) return {index:0, title:'Upload your subtitles', next:'Choose an SRT file', action:'upload', completed:[]};
    let index = 2, title = 'Estimate translation', next = 'Estimate translation', action = 'translation';
    if (dirty || !configured) { index=1; title='Configure your translation'; next='Save settings'; action='settings'; }
    else if (job.status.startsWith('translation_')) { index=3; title='Translating'; next='Translation in progress'; action=null; }
    else if (job.status.startsWith('revision_')) { index=4; title='Revising'; next='Revision in progress'; action=null; }
    else if (job.revision_complete) {
      index=job.review.remaining ? 4 : 5;
      title=job.review.remaining ? 'Review each suggestion' : job.has_final ? 'Export ready' : 'Save your reviewed subtitles';
      next=job.review.remaining ? 'Continue review' : job.has_final ? 'Download reviewed SRT' : 'Save reviewed SRT';
      action=job.review.remaining ? 'review' : job.has_final ? 'download-final' : 'export';
    } else if (job.has_translation) {
      index=4;
      title=job.config.revision_enabled ? 'Revision available' : 'Translation completed';
      next=job.config.revision_enabled ? 'Estimate revision' : 'Download translated SRT';
      action=job.config.revision_enabled ? 'revision' : 'download-translated';
      if (!job.config.revision_enabled) index=5;
    }
    if (!dirty && configured && ['failed','paused','interrupted'].includes(job.status)) {
      index=job.has_translation ? 4 : 2;
      title=job.status==='failed' ? 'Failed — work retained' : job.status==='paused' ? 'Paused' : 'Ready to resume';
      next=job.has_translation ? 'Estimate revision to resume' : 'Estimate translation to resume';
      action=job.has_translation ? 'revision' : 'translation';
    }
    if (quote && !dirty) { index=2; title=quote.operation==='revision' ? 'Approve revision separately' : 'Review translation cost'; next='Review spending approval'; action='approval'; }
    const completed = [0];
    if (!dirty && configured) completed.push(1);
    if (job.has_translation) completed.push(2,3);
    if (job.revision_complete && !job.review.remaining) completed.push(4);
    if (job.has_final) completed.push(5);
    return {index,title,next,action,completed};
  }

  function errorHelp(message) {
    if (!message) return '';
    if (/API key is empty|Set OPENAI_API_KEY/i.test(message)) return 'Configure the key locally on this Mac, restart the server, then estimate and approve recovery. Never enter the key in this browser.';
    if (/budget|remaining combined/i.test(message)) return 'Review actual spending and reserved usage. Estimate recovery again; increase the total job limit only if you choose to authorize more spending.';
    if (/truncat|output.token|response was incomplete/i.test(message)) return 'The response did not complete. Paid usage was retained. Inspect local diagnostic evidence before approving another attempt.';
    if (/validation|invalid.*edit|violates/i.test(message)) return 'Check identifiers, speaker separation and supported formatting. Saved work is retained; no correction is accepted automatically.';
    if (/model access|API configuration/i.test(message)) return 'Check local API configuration and account access to the selected model, then estimate and approve recovery.';
    return 'Saved work and spending remain on this Mac. Review the message, then estimate and explicitly approve recovery when ready.';
  }

  function costCards(quote, comparable=null, calibration=null) {
    const empirical=quote.operation==='revision'&&calibration?.kind==='calibrated';
    const revision=quote.operation==='revision';
    const rows = [
      {label:revision?`Expected revision cost · ${empirical?'calibrated':'heuristic'}`:'Estimated normal cost',
        value:empirical?calibration.expected_cost:quote.estimated_cost,approximate:empirical,
        note:empirical?'Measured consumption projection · not a guaranteed bill':revision?'All-cues-edited proxy · no empirical calibration applied':'This operation · estimate, not a guaranteed bill'},
      {label:'Maximum authorized spending',value:quote.max_cost,note:'Total job limit · translation and revision combined'}
    ];
    if (comparable) rows.push({label:'Previously observed comparable cost',value:comparable.cost,
      note:`Same file and settings · ${comparable.entries} cues · accepted requests only${comparable.includes_successful_probes ? ', including a successful probe' : ''}`});
    return rows;
  }
  return {names, tokens, preview, stage, errorHelp, costCards};
})();
if (typeof module !== 'undefined') module.exports = SubtitleUI;
