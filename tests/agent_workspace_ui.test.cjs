const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {
    value:'',innerHTML:'',textContent:'',disabled:false,focus(){},
    classList:{toggle(){},add(){},remove(){}},querySelectorAll:()=>[]
  });
  return elements.get(id);
};
const context = vm.createContext({
  document:{getElementById:element,querySelectorAll:()=>[]},console,setTimeout
});
const source = fs.readFileSync('travel_agent/web/app.js','utf8').replace(/init\(\);\s*$/, '');
vm.runInContext(source,context);

(async()=>{
  let resolveTrace;
  context.requestForTest=()=>new Promise(resolve=>{resolveTrace=resolve;});
  vm.runInContext('api=requestForTest;state.config={scenarios:[]};',context);
  const loading=vm.runInContext("loadTrace('old')",context);
  vm.runInContext('newTask()',context);
  resolveTrace({trace_id:'old',scenario:'old',spans:[]});
  await loading;
  assert.equal(vm.runInContext('state.trace',context),null);
  assert.match(element('conversation').innerHTML,/下一程/);
  assert.match(element('trace-detail').innerHTML,/你的行程将在这里展开/);

  vm.runInContext(`state.trace={trace_id:'safe',scenario:'custom-x',status:'planned',
    termination:'completed',tool_mode:'demo',usage_complete:true,spans:[],
    tool_calls:4,retries:0,input_tokens:0,output_tokens:0,model:'rules-v1',
    verdict:{reason:'feasible',total_cny:25,evidence_ids:[],itinerary:[
      {label:'<img src=x onerror=alert(1)>',from:'杭州',to:'上海',
       start_at:'2030-01-01T08:00',end_at:'2030-01-01T09:00',price_cny:25}
    ]}};renderTrace();`,context);
  assert.match(element('trace-detail').innerHTML,/&lt;img/);
  assert.doesNotMatch(element('trace-detail').innerHTML,/<img/);
  assert.match(element('trace-detail').innerHTML,/¥25（模拟）/);
  assert.match(element('trace-detail').innerHTML,/<details class="diagnostics">/);
  vm.runInContext(`state.trace.verdict.total_cny=400;state.trace.verdict.itinerary=[{
    kind:'hotel',label:'模拟住宿',from:'桔子水晶酒店',to:'桔子水晶酒店',
    start_at:'2030-01-01T21:00',end_at:'2030-01-02T07:00',price_cny:400
  }];renderTrace();state.config.scenarios=[{id:'custom-x',request:{
    origin:'错误城市',meeting_at:'2040-01-01T00:00',destination:'错误城市'
  }}];`,context);
  assert.match(element('trace-detail').innerHTML,/桔子水晶酒店/);
  assert.doesNotMatch(element('trace-detail').innerHTML,/undefined/);
  assert.doesNotMatch(vm.runInContext('requestText(state.trace)',context),/错误城市|2040/);

  context.failureForTest=async()=>{throw new Error('请求失败');};
  vm.runInContext('api=failureForTest;updateToolMode=()=>{};',context);
  element('scenario').value='natural';element('natural-request').value='三天后杭州开会';
  element('tool-mode').value='real';element('run-provider').value='deepseek';
  await vm.runInContext("launch('run')",context);
  assert.equal(vm.runInContext('state.busy',context),false);
  assert.equal(element('new-task').disabled,false);
  assert.match(element('notice').textContent,/请求失败/);
  console.log('PASS: new-task race isolation, escaped itinerary, diagnostics, failed-request recovery');
})().catch(error=>{console.error(error);process.exitCode=1;});
