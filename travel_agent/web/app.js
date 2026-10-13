'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {runs:[], evaluations:[], trace:null, evaluation:null, config:null, busy:false, view:'traces'};
const roots = {feasible:'可行方案',no_feasible_route:'无可行方案',missing_data:'资料不足',execution_failed:'执行未完成'};
const labels = {completed:'正常结束',budget_exceeded:'预算耗尽',invalid_response:'结果被拦截',provider_error:'模型调用失败'};
Object.assign(labels,{human_cancelled:'用户已取消',human_expired:'回复已过期',request_expired:'行程日期已过期',execution_interrupted:'执行中断，需重新规划'});
const hilEndText={cancelled:'任务已取消。如需修改核心需求，请新建任务。',expired:'回复期限或行程日期已过期，请新建任务。'};
const hilLabels={running:'规划中',waiting_for_human:'等待回复',completed:'已结束',failed:'执行未完成',cancelled:'已取消',expired:'已过期'};
const checks = {completed:'执行完成',required_tools:'四类工具均已查询',canonical_records:'记录内容与数据一致',reason:'结果原因正确',status:'状态正确',required_evidence:'四类证据齐全',grounded_citations:'引用真实',itinerary:'行程顺序正确',schedule:'地点与时间衔接',budget:'费用与政策正确',retry_count:'重试次数正确',safe_failure:'失败不编造方案',no_guardrail_violation:'无越界尝试'};
const date = value => new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
const runLabel = r => r.request ? `${r.request.origin} → ${r.request.destination} · ${r.request.meeting_at.replace('T',' ')}` : r.scenario;
const ms = value => value == null ? '—' : value >= 1000 ? `${(value/1000).toFixed(2)} s` : `${Number(value).toFixed(1)} ms`;
const pct = value => value == null ? '—' : `${(value*100).toFixed(0)}%`;
const badge = (label,kind='') => `<span class="badge ${kind}">${esc(label)}</span>`;
const metric = (label,value,caption,accent=false) => `<div class="metric ${accent?'accent':''}"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div><div class="metric-caption">${esc(caption)}</div></div>`;
async function api(path, body){
  const options = body ? {method:'POST',headers:{'Content-Type':'application/json','X-Travel-Client':'dashboard'},body:JSON.stringify(body)} : {};
  const response = await fetch(path,options);
  const value = await response.json();
  if(!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}
function notice(message,error=false){$('notice').textContent=message;$('notice').className=`notice${error?' error':''}`;}
function setView(view){
  state.view=view;
  $('traces-view').classList.toggle('hidden',view!=='traces');$('evals-view').classList.toggle('hidden',view!=='evals');$('flow-view').classList.toggle('hidden',view!=='flow');
  document.querySelectorAll('[data-view]').forEach(el=>el.classList.toggle('active',el.dataset.view===view));
  $('section-label').textContent=view==='traces'?'OBSERVABILITY':view==='evals'?'EVALUATION':'SYSTEM MAP';
  $('eyebrow').textContent=view==='traces'?'FOLLOW THE EVIDENCE':'MEASURE BEFORE YOU SHIP';
  $('page-title').textContent=view==='traces'?'每一段行程，都有据可查。':view==='evals'?'改动之后，质量是否依然可靠？':'一张图看懂 Agent 全链路。';
  $('page-subtitle').textContent=view==='traces'?'从会议需求到差旅方案，定位工具异常、观察执行预算、核对证据引用。':view==='evals'?'对照标准答案检查行程、证据、时间与费用，让失败案例进入下一次回归。':'模型调用工具、读取结果并继续决策，直至提交方案；每一步均进入观测链路。';
}
function renderRuns(){
  const filter=$('provider-filter').value;
  const runs=state.runs.filter(r=>filter==='all'||r.provider===filter);
  const finished=runs.filter(r=>r.status!=='running');
  const durations=finished.map(r=>r.active_duration_ms??r.duration_ms).sort((a,b)=>a-b);
  const p95=durations[Math.max(0,Math.ceil(durations.length*.95)-1)];
  const measured=finished.filter(r=>!['cancelled','expired'].includes(r.execution_status));
  const errors=measured.filter(r=>r.termination!=='completed').length;
  const tokens=finished.reduce((n,r)=>n+r.input_tokens+r.output_tokens,0);
  const incomplete=finished.some(r=>!r.usage_complete);
  $('run-metrics').innerHTML=metric('运行数量',runs.length,filter==='all'?'全部执行方式 / 最近 200 条':filter,true)+metric('执行异常率',measured.length?pct(errors/measured.length):'—','不含人工取消、过期和等待')+metric('P95 规划耗时',ms(p95),'交互任务按活跃执行时间统计')+metric('累计 Token',tokens.toLocaleString(),incomplete?'部分调用用量未知，仅展示已知值':'规则基线不消耗模型 Token');
  $('run-count').textContent=`${runs.length} RUNS`;
  $('run-list').innerHTML=runs.length?runs.map(r=>`<button class="run-item ${state.trace?.trace_id===r.trace_id?'selected':''}" data-trace="${esc(r.trace_id)}"><div class="run-top"><span class="run-name">${esc(runLabel(r))}</span>${badge(r.execution_status?hilLabels[r.execution_status]:r.status==='running'?'规划中':r.termination!=='completed'?'执行异常':r.status==='planned'?'已生成':r.status==='unavailable'?'无可行方案':'资料不足',r.status==='running'?'':r.termination!=='completed'?'bad':r.status==='planned'?'good':'warn')}</div><div class="run-meta"><span>${esc(r.provider==='baseline'?'规则基线':r.model)} · ${r.tool_mode==='real'?'真实工具':'演示工具'} ${r.evaluation_id?'· 评测':''}</span><span>${ms(r.duration_ms)}</span></div><div class="run-meta"><span>${date(r.started_at)}</span><span>${esc(r.trace_id.slice(0,8))}</span></div></button>`).join(''):'<div class="empty">尚无运行记录。<br>选择上方场景开始第一次规划。</div>';
  $('run-list').querySelectorAll('[data-trace]').forEach(el=>el.onclick=()=>loadTrace(el.dataset.trace).catch(showError));
}
let traceLoadVersion=0;
async function loadTrace(id){
  const version=++traceLoadVersion;
  const trace=await api(`/api/runs/${encodeURIComponent(id)}`);
  const interaction=trace.execution_status?await api(`/api/hil/runs/${encodeURIComponent(id)}`):null;
  if(version!==traceLoadVersion)return;
  if(interaction){trace.execution_status=interaction.execution_status;const listed=state.runs.find(r=>r.trace_id===id);if(listed)listed.execution_status=interaction.execution_status;}
  state.trace=trace;state.hil=interaction;
  renderRuns();renderTrace();renderHil();
}
function renderHil(){
  const run=state.hil,panel=$('hil-panel');panel.classList.toggle('hidden',!run);
  if(!run){panel.innerHTML='';return;}
  const q=run.human_request;
  panel.innerHTML=`<div class="panel-heading"><h2>人工交互 · ${esc(hilLabels[run.execution_status])}</h2></div>`;
  if(!q){
    panel.innerHTML+=`<p>${esc(hilEndText[run.execution_status]||run.result?.draft||'可刷新状态或继续处理此任务。')}</p>`;
    if(!run.result)panel.innerHTML+='<button id="hil-resume" class="subtle">继续处理</button>';
    if($('hil-resume'))$('hil-resume').onclick=()=>hilAction('resume',{},run.run_id);
    return;
  }
  panel.innerHTML+=`<p>${esc(q.question)}</p><small>回复截止：${esc(date(q.expires_at))}。修改城市、会议地点或日期请新建任务。</small>`;
  if(q.kind==='select')panel.innerHTML+=`<label>选择候选<select id="hil-choice"><option value="">请选择</option>${q.options.map((o,i)=>`<option value="candidate:${i}">候选 ${i+1} · ${esc(o.record.label||o.record.service_number||o.record.candidate_id||o.option_id)}</option>`).join('')}<option value="reject">拒绝这些候选，继续寻找</option></select></label>${q.options.map((o,i)=>`<details><summary>候选 ${i+1} · 已查询证据</summary><pre>${esc(JSON.stringify(o.record,null,2))}</pre></details>`).join('')}`;
  if(q.kind==='confirm')panel.innerHTML+='<label>确认结果<select id="hil-confirm"><option value="">请选择</option><option value="yes">确认</option><option value="no">不确认，继续讨论</option></select></label>';
  panel.innerHTML+='<label>偏好补充<textarea id="hil-preference" maxlength="1000" rows="3" placeholder="例如：酒店安静、优先靠窗。请勿在此修改核心需求。"></textarea></label><button id="hil-reply" class="primary">提交回复并继续</button> <button id="hil-cancel" class="subtle">取消任务</button>';
  $('hil-reply').onclick=async()=>{
    try{
      const reply={},text=$('hil-preference').value.trim();if(text)reply.preference=text;
      if(q.kind==='select'){const choice=$('hil-choice').value;if(!choice)throw new Error('请选择候选或拒绝这些候选。');if(choice==='reject')reply.rejected=true;else reply.option_id=q.options[Number(choice.slice('candidate:'.length))].option_id;}
      if(q.kind==='confirm'){const choice=$('hil-confirm').value;if(!choice)throw new Error('请选择确认结果。');reply.confirmed=choice==='yes';}
      if(q.kind==='preference'&&!text)throw new Error('请填写偏好。');
      const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(reply)));
      const hash=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
      const key=`hil:${run.run_id}:${q.interaction_id}:${hash}`;
      const response_id=sessionStorage.getItem(key)||crypto.randomUUID();sessionStorage.setItem(key,response_id);
      await hilAction('respond',{interaction_id:q.interaction_id,state_version:run.state_version,response_id,reply},run.run_id);
    }catch(error){showError(error);}
  };
  $('hil-cancel').onclick=()=>hilAction('cancel',{state_version:run.state_version},run.run_id);
}
async function hilAction(action,body,runId){
  if(state.busy)return;
  state.busy=true;$('run').disabled=$('evaluate').disabled=true;
  $('hil-panel').querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
    const job=await api(`/api/hil/runs/${encodeURIComponent(runId)}/${action}`,body);
    let status;
    do{await new Promise(resolve=>setTimeout(resolve,300));status=await api(`/api/jobs/${job.id}`);}while(status.status==='running');
    if(status.status==='failed')throw new Error(status.error);
    await refresh();notice(hilLabels[status.execution_status]);
  }catch(error){showError(error);}finally{state.busy=false;$('evaluate').disabled=false;updateToolMode();$('hil-panel').querySelectorAll('button').forEach(b=>b.disabled=false);}
}
function renderToolResults(run){
  if(run.tool_mode!=='real')return '';
  const results=run.tool_results||[];
  return `<section class="tool-results"><h2>工具结果与证据</h2>${results.length?results.map(result=>`<details><summary>${esc(result.tool)} · ${result.status==='ok'?'成功':'失败'}</summary><pre>${esc(JSON.stringify(result,null,2))}</pre></details>`).join(''):'<p>本次尚未取得工具结果。</p>'}</section>`;
}
function renderTrace(){
  const r=state.trace;if(!r)return;
  const spans=r.spans||[];
  $('trace-detail').className='trace-content';
  $('trace-detail').innerHTML=`<div class="result-heading"><h2 class="trace-title">${esc(runLabel(r))}</h2>${badge(r.tool_mode==='real'?'真实工具':'演示工具')} ${badge(r.provider==='baseline'?'规则基线':'真实模型')}</div><div class="mono">TRACE ${esc(r.trace_id)}</div><div class="result-box"><div class="result-heading"><span>${esc(roots[r.verdict?.reason]||'规划进行中')}</span>${badge((r.execution_status?hilLabels[r.execution_status]:labels[r.termination])||'执行中',r.termination&&r.termination!=='completed'?'bad':'good')}</div><p class="itinerary-text">${esc(hilEndText[r.execution_status]||r.draft||'正在收集证据，请稍后刷新。')}</p><div class="evidence">${(r.verdict?.evidence_ids||[]).map(x=>`<span>${esc(x)}</span>`).join('')||'<span>无可引用的行程证据</span>'}</div></div><dl class="detail-stats"><div><dt>总耗时</dt><dd>${ms(r.duration_ms)}</dd></div><div><dt>工具调用 / 重试</dt><dd>${r.tool_calls} / ${r.retries}</dd></div><div><dt>Token 输入 / 输出</dt><dd>${r.input_tokens} / ${r.output_tokens}${r.usage_complete?'':' *'}</dd></div><div><dt>模型费用</dt><dd>${r.cost_usd==null?'未计价':`$${r.cost_usd.toFixed(4)}`}</dd></div></dl>${renderToolResults(r)}<div class="waterfall-head"><h2>执行调用链</h2><small>相对开始时间 · 点击查看属性</small></div><div id="waterfall">${spans.map((s,i)=>`<button class="span-row" data-span="${i}"><span class="span-name ${s.parent_span_id?'child':''}">${s.status==='error'?'! ':''}${esc(s.attributes.tool||s.name)}${s.attributes.attempt?' · '+s.attributes.attempt:''}</span><span class="span-track"><span class="span-bar ${s.status==='error'?'error':''}" data-bar="${i}"></span></span><span class="span-duration">${ms(s.duration_ms)}</span></button>`).join('')}</div><pre id="span-detail" class="span-detail hidden"></pre><div class="metadata">模型 ${esc(r.model)} · 应用 ${esc(r.app_version)} · 终止原因 ${esc(r.termination||'pending')}${r.budget_reason?' · '+esc(r.budget_reason):''}<br>${r.request_received_at?`请求按亚洲/上海时间 ${esc(r.request_received_at.replace('T',' '))} 解析<br>`:''}提示词 ${esc(r.prompt_hash.slice(0,16))} · 数据 ${esc(r.dataset_hash.slice(0,16))}<br>遥测保存结构与用量；工具证据及已校验行程作为业务结果保存，不保存模型思维。${r.usage_complete?'':'<br>* 部分模型调用用量未知，不能将已知 Token 视为总用量。'}</div>`;
  const base=Date.parse(r.started_at),total=Math.max(r.duration_ms||1,1);
  document.querySelectorAll('[data-bar]').forEach(el=>{const s=spans[Number(el.dataset.bar)];const left=Math.min(99,Math.max(0,(Date.parse(s.started_at)-base)/total*100));el.style.left=`${left}%`;el.style.width=`${Math.min(100-left,Math.max(.5,s.duration_ms/total*100))}%`;});
  document.querySelectorAll('[data-span]').forEach(el=>el.onclick=()=>{const s=spans[Number(el.dataset.span)];$('span-detail').textContent=JSON.stringify(s,null,2);$('span-detail').classList.remove('hidden');});
}
function renderEvaluation(){
  const report=state.evaluations.find(e=>e.id===state.evaluation)||state.evaluations[0];
  if(!report){$('eval-metrics').innerHTML='';$('gates').innerHTML='<div class="empty">运行首批评测后显示门禁结果。</div>';$('eval-rows').innerHTML='<tr><td colspan="6">尚无评测记录。</td></tr>';return;}
  state.evaluation=report.id;$('eval-history').value=report.id;
  $('eval-metrics').innerHTML=metric('任务通过率',pct(report.pass_rate),`${report.passed} / ${report.total} 次试验通过`,true)+metric('安全断言通过率',pct(report.safety_rate),'引用 / 时间 / 预算 / 失败检查')+metric('跨轮次案例一致性',pct(report.case_consistency_rate),'同一案例的所有轮次均通过')+metric('P95 评测耗时',ms(report.p95_latency_ms),`${report.provider} · ${report.model}`);
  $('gate-label').innerHTML=badge(report.gate_passed?'门禁通过':'门禁未通过',report.gate_passed?'good':'bad');
  const gateLabels={pass_rate:'任务正确性',safety_rate:'安全约束',latency:'耗时预算',tool_budget:'工具调用预算'};
  const threshold={pass_rate:`通过率 ≥ ${pct(report.gates.min_pass_rate)}`,safety_rate:`通过率 ≥ ${pct(report.gates.min_safety_rate)}`,latency:`P95 ≤ ${ms(report.gates.max_p95_latency_ms)}`,tool_budget:`每次规划 ≤ ${report.gates.max_tool_calls} 次`};
  $('gates').innerHTML=Object.entries(report.gate_checks).map(([k,v])=>`<div class="gate">${badge(v?'PASS':'FAIL',v?'good':'bad')} ${gateLabels[k]}<small>${threshold[k]}</small></div>`).join('');
  const rows=report.rows.map((r,i)=>({...r,index:i})).filter(r=>!$('failures-only').checked||!r.passed);
  $('eval-rows').innerHTML=rows.length?rows.map(r=>`<tr><td>${esc(r.scenario)}<small>${esc(r.split)} · 第 ${r.trial} 轮</small></td><td>${esc(roots[r.expected.reason])}</td><td>${esc(roots[r.actual.reason])}</td><td>${ms(r.duration_ms)}</td><td>${badge(r.passed?'通过':'失败',r.passed?'good':'bad')}</td><td><button class="link-button" data-case="${r.index}">查看断言 ↗</button></td></tr>`).join(''):'<tr><td colspan="6">当前批次没有失败案例。</td></tr>';
  $('case-detail').classList.add('hidden');
  document.querySelectorAll('[data-case]').forEach(el=>el.onclick=()=>{const r=report.rows[Number(el.dataset.case)];$('case-detail').classList.remove('hidden');$('case-detail').innerHTML=`<div class="result-heading"><h3>${esc(r.scenario)} · 第 ${r.trial} 轮</h3><button class="link-button" id="case-trace">打开运行 Trace ↗</button></div><div class="check-list">${Object.entries(r.checks).map(([k,v])=>badge(`${v?'✓':'×'} ${checks[k]||k}`,v?'good':'bad')).join('')}</div><div class="comparison"><div><strong>预期结果 / Oracle</strong><pre>${esc(JSON.stringify(r.expected,null,2))}</pre></div><div><strong>实际结果 / ${esc(labels[r.termination]||r.termination)}</strong><pre>${esc(JSON.stringify(r.actual,null,2))}</pre></div></div>`;$('case-trace').onclick=async()=>{try{setView('traces');$('provider-filter').value='all';await loadTrace(r.trace_id);window.scrollTo({top:0,behavior:'smooth'});}catch(error){showError(error);}};});
}
async function refresh(){
  const [runs,evaluations]=await Promise.all([api('/api/runs'),api('/api/evaluations')]);state.runs=runs;state.evaluations=evaluations;
  $('eval-history').innerHTML=evaluations.length?evaluations.map(r=>`<option value="${esc(r.id)}">${date(r.started_at)} · ${esc(r.provider)} · ${esc(r.split)} · ${r.passed}/${r.total}</option>`).join(''):'<option>暂无评测批次</option>';
  renderRuns();renderEvaluation();
  if(state.trace) await loadTrace(state.trace.trace_id);else if(runs.length)await loadTrace(runs[0].trace_id);
  $('connection').textContent='● 数据已同步';
}
function showError(error){notice(error.message,true);$('connection').textContent='连接或操作异常';}
async function launch(kind){
  if(state.busy)return;
  state.busy=true;$('run').disabled=$('evaluate').disabled=true;
  try{
    const body=kind==='run'?{scenario:$('scenario').value,provider:$('run-provider').value,tool_mode:$('tool-mode').value}:{split:$('eval-split').value,repeats:Number($('repeats').value),provider:$('eval-provider').value};
    if(kind==='run' && body.tool_mode==='real' && (body.provider==='baseline'||!body.provider))throw new Error('真实工具需要可用的 DeepSeek 或 Ollama 模型，请配置模型后重启服务。');
    if(kind==='run' && $('scenario').value==='custom'){
      body.request={origin:$('origin').value.trim(),destination:$('destination').value.trim(),meeting_location:$('meeting-location').value.trim(),meeting_at:$('meeting-at').value};
      if(Object.values(body.request).some(v=>!v))throw new Error('请填写完整的城市、地点和会议时间。');
      delete body.scenario;
    }
    if(kind==='run' && body.scenario==='natural'){
      body.request_text=$('natural-request').value.trim();
      if(!body.request_text)throw new Error('请输入会议需求。');
      delete body.scenario;
    }
    const job=await api(kind==='run'?($('hil-enabled').checked?'/api/hil/runs':'/api/run'):'/api/evaluate',body);
    if(job.request_summary)$('scenario-description').textContent=job.request_summary;
    notice(kind==='run'?'规划进行中，正在收集证据…':'评测进行中，每次试验都会保存独立 Trace…');
    let status;
    do{await new Promise(resolve=>setTimeout(resolve,750));status=await api(`/api/jobs/${job.id}`);}while(status.status==='running');
    if(status.status==='failed')throw new Error(`任务失败：${status.error}`);
    if(kind==='eval')state.evaluation=status.result_id;
    await refresh();
    if(kind==='run'){await loadTrace(status.result_id);if(state.hil)notice(hilLabels[state.hil.execution_status]);else notice(state.trace.termination==='completed'?'规划已结束，结论与调用链已保存。':`规划需检查：${labels[state.trace.termination]}。请查看错误 Span。`,state.trace.termination!=='completed');}
    else{const r=state.evaluations.find(x=>x.id===status.result_id);notice(`评测完成：${r.passed}/${r.total} 通过，${r.gate_passed?'门禁通过':'门禁未通过'}。`,!r.gate_passed);}
  }catch(error){showError(error);}finally{state.busy=false;$('evaluate').disabled=false;updateToolMode();}
}
async function init(){
  try{
    state.config=await api('/api/config');
    $('scenario').innerHTML=state.config.scenarios.map(s=>`<option value="${esc(s.id)}">${esc(s.label)}</option>`).join('')+'<option value="natural">自然语言请求</option><option value="custom">结构化自定义会议</option>';
    if(!state.config.ollama_enabled){for(const id of ['run-provider','eval-provider']){$(id).querySelector('[value="ollama"]').disabled=true;$(id).querySelector('[value="ollama"]').textContent='Ollama · 需启动时启用';}}
    for(const id of ['run-provider','eval-provider']){const option=$(id).querySelector('[value="deepseek"]');option.disabled=!state.config.deepseek_enabled;option.textContent=state.config.deepseek_enabled?`DeepSeek · ${state.config.deepseek_model}`:'DeepSeek · 请配置 .env 后重启';if(state.config.deepseek_enabled)$(id).value='deepseek';}
    updateToolMode();
    await refresh();
  }catch(error){showError(error);}
}
document.querySelectorAll('[data-view]').forEach(el=>el.onclick=()=>setView(el.dataset.view));
function updateToolMode(){
  const real=$('tool-mode').value==='real';
  $('run-provider').querySelector('[value="baseline"]').disabled=real;
  if(real && ($('run-provider').value==='baseline'||!$('run-provider').value)){
    const available=[...$('run-provider').options].find(o=>!o.disabled);
    $('run-provider').value=available?.value||'';
  }else if(!real && !$('run-provider').value){$('run-provider').value='baseline';}
  $('run').disabled=state.busy||!$('run-provider').value;
  $('tool-mode-note').textContent=real?'真实数据 · 由模型按需查询':'四类模拟工具 · 默认前一晚出发';
  $('scenario').onchange();
}
$('tool-mode').onchange=updateToolMode;
$('scenario').onchange=()=>{
  const custom=$('scenario').value==='custom';
  const natural=$('scenario').value==='natural';
  $('request-fields').classList.toggle('hidden',!custom);
  $('natural-fields').classList.toggle('hidden',!natural);
  const current=state.config.scenarios.find(s=>s.id===$('scenario').value);
  if($('tool-mode').value==='real'){
    const tools=(state.config?.real_tools||[]).join('、');
    $('scenario-description').textContent=`真实工具：${tools||'暂无可用工具'}。模型按需查询；资料不足时正常结束，不代表完整方案可行。${$('run-provider').value?'':'请配置 DeepSeek 或启用 Ollama 后重启服务。'}`;
    return;
  }
  $('scenario-description').textContent=natural?'输入会议日期、会场、出发城市与出行偏好；相对日期按本次提交时的上海时间解析，并先展示解析结果。':custom?'模拟数据只覆盖杭州 → 上海东方明珠塔；可修改会议日期与时间。前一晚出发，全部价格与时长仅供演示。':`${current.request.origin} → ${current.request.destination} · ${current.request.meeting_location} · ${current.request.meeting_at.replace('T',' ')}（模拟数据）`;
};
$('refresh').onclick=()=>refresh().catch(showError);$('provider-filter').onchange=renderRuns;
$('eval-history').onchange=()=>{state.evaluation=$('eval-history').value;renderEvaluation();};$('failures-only').onchange=renderEvaluation;
$('run').onclick=()=>launch('run');$('evaluate').onclick=()=>launch('eval');
init();
