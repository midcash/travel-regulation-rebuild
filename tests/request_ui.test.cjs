const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = id => {
  if(!elements.has(id))elements.set(id,{value:'',textContent:'',innerHTML:'',disabled:false,
    classList:{toggle(){}},querySelectorAll:()=>[]});
  return elements.get(id);
};
let submitted;
const context = vm.createContext({
  document:{getElementById:element,querySelectorAll:()=>[]},
  fetch:async(path,options)=>{
    if(path==='/api/run'){
      submitted={path,body:JSON.parse(options.body)};
      return {ok:true,json:async()=>({id:'job-1',request_summary:'按上海时间解析：2030-09-21 09:00 · 飞机 · 1 人 1 间房。'})};
    }
    if(path==='/api/jobs/job-1')return {ok:true,json:async()=>({status:'completed',result_id:'trace-1'})};
    throw new Error(`Unexpected request: ${path}`);
  },
  setTimeout:(callback)=>setImmediate(callback),console
});
const source=fs.readFileSync('travel_agent/web/app.js','utf8').replace(/init\(\);\s*$/, '');
vm.runInContext(source,context);
vm.runInContext(`refresh=async()=>{};loadTrace=async()=>{};updateToolMode=()=>{};
  state.trace={termination:'completed'};state.hil=null;`,context);

(async()=>{
  element('scenario').value='natural';
  element('natural-request').value='下周六早上 9 点在三亚湾红树林国际会议中心开会，从杭州出发，提前一晚抵达，乘飞机并住一晚。';
  element('tool-mode').value='real';
  element('run-provider').value='deepseek';
  await vm.runInContext("launch('run')",context);
  assert.equal(submitted.path,'/api/run');
  assert.equal(submitted.body.request_text,element('natural-request').value);
  assert.equal(submitted.body.scenario,undefined);
  assert.match(element('scenario-description').textContent,/2030-09-21 09:00/);
  assert.match(element('scenario-description').textContent,/1 人 1 间房/);
  console.log('PASS: natural-language request reaches the planning API and displays its resolved summary');
})().catch(error=>{console.error(error);process.exitCode=1;});
