const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const elements = new Map();
const element = id => {
  if(!elements.has(id))elements.set(id,{value:'',innerHTML:'',classList:{toggle(){},add(){},remove(){}},querySelectorAll:()=>[]});
  return elements.get(id);
};
const context = vm.createContext({document:{getElementById:element,querySelectorAll:()=>[]},
  crypto:webcrypto,TextEncoder,sessionStorage:{getItem:()=>null,setItem(){}},setTimeout,console});
const source = fs.readFileSync('travel_agent/web/app.js','utf8').replace(/init\(\);\s*$/, '');
vm.runInContext(source,context);

(async()=>{
  const pending = new Map();
  context.fetchForTest=path=>new Promise(resolve=>pending.set(path,resolve));
  vm.runInContext('api=fetchForTest;renderRuns=()=>{};renderTrace=()=>{};',context);
  const a=vm.runInContext("loadTrace('A')",context);
  pending.get('/api/runs/A')({trace_id:'A',execution_status:'running'});
  await new Promise(setImmediate);
  const b=vm.runInContext("loadTrace('B')",context);
  pending.get('/api/runs/B')({trace_id:'B',execution_status:'running'});
  await new Promise(setImmediate);
  pending.get('/api/hil/runs/B')({run_id:'B',execution_status:'running'});await b;
  pending.get('/api/hil/runs/A')({run_id:'A',execution_status:'running'});await a;
  assert.equal(vm.runInContext('state.trace.trace_id',context),'B');
  assert.equal(vm.runInContext('state.hil.run_id',context),'B');

  vm.runInContext(`state.hil={run_id:'A',state_version:2,execution_status:'waiting_for_human',human_request:{
    kind:'select',question:'选择',interaction_id:'i',expires_at:'2099-01-01',
    options:[{option_id:'__reject__',record:{label:'真实候选'}}]}};renderHil();`,context);
  assert.match(element('hil-panel').innerHTML,/value="candidate:0"/);
  element('hil-choice').value='candidate:0';element('hil-preference').value='';
  let received;
  context.actionForTest=(...args)=>{received=args;};
  vm.runInContext('hilAction=actionForTest',context);
  // The displayed question's handler must retain A even if another load wins.
  vm.runInContext("state.hil={run_id:'B'}",context);
  await element('hil-reply').onclick();
  assert.equal(received[2],'A');
  assert.equal(received[1].reply.option_id,'__reject__');
  assert.equal(received[1].reply.rejected,undefined);
  console.log('PASS: out-of-order trace loading, bound run identity, candidate/reject separation');
})().catch(error=>{console.error(error);process.exitCode=1;});
