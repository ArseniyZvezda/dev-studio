const fs = require('node:fs');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const file = process.argv[2];
const data = fs.readFileSync(file);
const rows = data.toString('utf8').trim().split(/\r?\n/).map((s,i)=>({...JSON.parse(s),line:i+1}));
const count = (a,key) => a.reduce((r,x)=>(r[x[key]]=(r[x[key]]||0)+1,r),{});
const sn = rows.filter(x=>x.event==='snapshot');
const baseline = sn.find(x=>x.label==='baseline');
const stableKeys = ['sentinel','timeOrigin','bootstrap','nodeSame','fileSame','fileName','fileSize','fileText','draft','selectionStart','selectionEnd','scrollTop','domSelection'];
const issues=[];
const chk=(ok,msg)=>{if(!ok)issues.push(msg);};
const eq=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
const baselineDifferences=[];
for(const s of sn){
  chk(s.wcId===baseline.wcId,`wc id at ${s.line}`);
  chk(eq(s.counters,baseline.counters),`counters at ${s.line}`);
  chk(eq(s.bounds,s.expectedBounds),`bounds at ${s.line}`);
  chk(s.children.filter(x=>x.ownsView).length===1 && s.children.some(x=>x.id===s.owner&&x.ownsView),`owner at ${s.line}`);
  chk(s.snapshot.nodeSame&&s.snapshot.fileSame,`object identity at ${s.line}`);
  const diff=stableKeys.filter(k=>!eq(s.snapshot[k],baseline.snapshot[k]));
  if(diff.length)baselineDifferences.push({line:s.line,label:s.label,diff});
}
const pairs=[]; let before;
for(const s of sn){
  if(s.label.startsWith('before:'))before=s;
  if(s.label.startsWith('commit:')||s.label.startsWith('rollback:')){
    assert.ok(before,'no before');
    const diff=stableKeys.filter(k=>!eq(s.snapshot[k],before.snapshot[k]));
    chk(!diff.length,`state changed within transfer ${before.line}->${s.line}: ${diff}`);
    chk(s.ownerFocused&&s.contentsFocused&&s.snapshot.documentFocused,`focus at ${s.line}`);
    chk(s.snapshot.activeElement===before.snapshot.activeElement,`active element at ${s.line}`);
    pairs.push({before:before.line,after:s.line,label:s.label,from:before.owner,to:s.owner,stable:!diff.length,focus:s.ownerFocused&&s.contentsFocused&&s.snapshot.documentFocused});
    before=undefined;
  }
}
const native=rows.filter(x=>x.event==='native-helper'&&x.record?.event==='terminal');
const linearized=rows.filter(x=>x.event==='terminal-linearized');
for(const l of linearized){
  const t=native.find(x=>x.record.token===l.token);chk(!!t,`linearized without native ${l.line}`);
  chk(t?.record.provenance==='native-hook'&&!t.record.injected&&t.record.kind==='up',`provenance ${l.line}`);
  const prior=rows.filter(x=>x.line<l.line&&x.event==='native-helper').at(-1)?.record;
  chk(prior?.valid===true&&prior?.consumed===true,`finish not validated/consumed ${l.line}`);
  const next=rows.find(x=>x.line>l.line&&['transfer-commit','gesture-begin'].includes(x.event));
  chk(next?.event==='transfer-commit',`no commit after terminal ${l.line}`);
}
const cancel=rows.filter(x=>x.event==='gesture-cancel');
for(const c of cancel)chk(!linearized.some(l=>l.token===c.token),`cancel token committed ${c.line}`);
const ends=rows.filter(x=>x.event==='test-end');
chk(ends.length===1&&ends[0].code===0,'test-end must be clean');
chk(rows.filter(x=>x.event==='project:destroyed').every(x=>x.line>ends[0].line),'destroy before cleanup');
for(const evt of ['project:did-start-navigation','project:did-finish-load','project-bootstrap'])
  chk(rows.filter(x=>x.event===evt).length===1&&rows.find(x=>x.event===evt).line<baseline.line,`unexpected ${evt}`);
chk(!rows.some(x=>['fatal','project:render-process-gone','blocked-network','native-helper-error'].includes(x.event)),'fatal/crash/network/helper error');
chk(pairs.length===rows.filter(x=>['transfer-commit','transfer-rollback'].includes(x.event)).length,'snapshot pair coverage');
const trace=rows.filter(x=>['gesture-begin','terminal-decision','terminal-linearized','gesture-cancel','terminal-rejected','transfer-commit','transfer-rollback','command-error','test-end','manual-ready','project:did-start-navigation','project:did-finish-load','project-bootstrap','project:destroyed'].includes(x.event)).map(x=>({line:x.line,at:x.at,event:x.event,token:x.token,kind:x.decision?.kind,reason:x.reason||x.error,name:x.name,source:x.source,target:x.target,windowId:x.decision?.windowId}));
const summary={source:file,sha256:crypto.createHash('sha256').update(data).digest('hex'),bytes:data.length,lines:rows.length,range:[rows[0].at,rows.at(-1).at],counts:count(rows,'event'),commitReasons:count(rows.filter(x=>x.event==='transfer-commit'),'reason'),cancelReasons:count(rows.filter(x=>x.event==='gesture-cancel'),'reason'),nativeTerminals:native.map(x=>({line:x.line,at:x.at,token:x.record.token,kind:x.record.kind,provenance:x.record.provenance,injected:x.record.injected,point:x.record.point,className:x.record.target?.className,pid:x.record.target?.pid,hwnd:x.record.target?.hwnd})),baseline:{line:baseline.line,wcId:baseline.wcId,owner:baseline.owner,bounds:baseline.bounds,counters:baseline.counters,snapshot:Object.fromEntries(stableKeys.map(k=>[k,['draft','fileText'].includes(k)?crypto.createHash('sha256').update(baseline.snapshot[k]).digest('hex'):baseline.snapshot[k]]))},baselineDifferences,pairs,manualSnapshots:sn.filter(x=>x.label==='manual-snapshot').map(x=>({line:x.line,at:x.at,owner:x.owner})),issues,trace};
fs.writeFileSync(process.argv[3],JSON.stringify(summary,null,2)+'\n');
console.log(JSON.stringify({...summary,trace:undefined,nativeTerminals:undefined,pairs:undefined},null,2));
console.log('TRACE\n'+trace.map(x=>`${x.line} ${x.at.slice(11,23)} ${x.event} ${x.kind||''} ${x.reason||''} ${x.token||''} ${x.source?`${x.source}->${x.target||''}`:''}`).join('\n'));
console.log('TERMINALS\n'+summary.nativeTerminals.map(x=>JSON.stringify(x)).join('\n'));
if(issues.length)process.exitCode=1;
