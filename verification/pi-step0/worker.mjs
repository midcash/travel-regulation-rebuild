// Isolated validation worker, not a production entry point.
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {syncBuiltinESMExports} from 'node:module';
import readline from 'node:readline';

const root=path.resolve(process.env.STEP0_REPO).toLowerCase();
const accesses=[];
function inspect(value, operation) {
  if(typeof value==='number')return;
  const p=path.resolve(value instanceof URL?fileURLToPath(value):String(value)).toLowerCase();
  accesses.push({path:p,operation});
  if(p===path.join(root,'agents.md')||p===path.join(root,'.agents')||p.startsWith(path.join(root,'.agents')+path.sep))
    throw new Error('Forbidden project resource access');
  const home=process.env.STEP0_REAL_HOME?.toLowerCase();
  if(home&&(p===home||p.startsWith(home+path.sep)))throw new Error('Forbidden real user directory access');
}
for(const [object,names] of [[fs,['readFileSync','readFile','readdirSync','readdir','openSync','open','statSync','stat','lstatSync','lstat','accessSync','access','existsSync','createReadStream','writeFileSync','appendFileSync','mkdirSync']],
  [fsp,['readFile','readdir','open','stat','lstat','access','writeFile','appendFile','mkdir']]]) {
  for(const name of names){const original=object[name];object[name]=function(p,...args){inspect(p,name);return original.call(this,p,...args);};}
}
syncBuiltinESMExports();
// Dynamic import is essential: SDK named fs imports must see the audit hooks.
const sdk=await import('@earendil-works/pi-coding-agent');
const ai=await import('@earendil-works/pi-ai');
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
const pending=new Map();
const toolStarts=new Map();
let session,manager,runtime,faux,commandId,commandKind,conditionVersion=1,modelCalls=0,toolRequests=0;
function snapshot(){return [manager.getHeader(),...manager.getEntries()];}
function requestTool(toolCallId,name,args,signal){
  const id=`${manager.getSessionId()}:${toolCallId}`;toolRequests++;
  toolStarts.set(id,performance.now());
  return new Promise((resolve,reject)=>{
    const abort=()=>{pending.delete(id);reject(new Error('Tool cancelled'));};
    if(signal?.aborted){abort();return;}
    pending.set(id,{resolve:value=>{signal?.removeEventListener('abort',abort);resolve(value);},reject});
    signal?.addEventListener('abort',abort,{once:true});
    emit({type:'tool_request',id,name,args,commandId,conditionVersion});
  });
}
async function initialize(c){
  manager=sdk.SessionManager.inMemory(process.cwd(),undefined,c.entries);
  runtime=await sdk.ModelRuntime.create({credentials:new ai.InMemoryCredentialStore(),modelsStore:new ai.InMemoryModelsStore(),modelsPath:null,refreshOnCreate:false});
  if(c.fake){
    faux=ai.fauxProvider({provider:'step0-faux',models:[{id:'deterministic',reasoning:false}],responses:[]});
    runtime.registerNativeProvider(faux.provider);
    await runtime.setRuntimeApiKey('step0-faux','not-a-secret');
  }else {
    // Explicit memory model registration: the pinned catalog predates the API model name.
    runtime.registerProvider('deepseek',{baseUrl:process.env.DEEPSEEK_BASE_URL||'https://api.deepseek.com',api:'openai-completions',
      models:[{id:c.model,name:c.model,input:['text'],reasoning:true,contextWindow:131072,maxTokens:8192,
        cost:{input:0,output:0,cacheRead:0,cacheWrite:0}}]});
    await runtime.setRuntimeApiKey('deepseek',process.env.DEEPSEEK_API_KEY);
  }
  let model=faux?.getModel()||runtime.getModel('deepseek',c.model);
  if(!model)throw new Error('Model not in locked Pi catalog: '+c.model);
  if(!c.fake&&process.env.DEEPSEEK_BASE_URL)model={...model,baseUrl:process.env.DEEPSEEK_BASE_URL};
  const stream=runtime.streamSimple.bind(runtime);
  runtime.streamSimple=(m,ctx,opts)=>{
    const call=++modelCalls;
    if(call>(c.maxCalls||24))throw new Error('Step0 model call safety limit');
    emit({type:'model_start',call,commandId,model:m.id,conditionVersion,operation:commandKind,at:Date.now()});
    const cap=commandKind==='compact'?8192:(c.maxOutput||4096);
    const source=c.failStream?{async *[Symbol.asyncIterator](){throw new Error('Injected stream iterator failure');}}:stream(m,ctx,{...opts,maxTokens:Math.min(opts?.maxTokens??cap,cap),
      onPayload:payload=>{emit({type:'request_meta',call,model:payload.model,thinking:payload.thinking,reasoning_effort:payload.reasoning_effort,max_tokens:payload.max_tokens});return payload;},
      fetch:async(url,options)=>{
        const response=await fetch(url,options);
        if(response.body){
          // Read a tee only to inspect usage, never save raw SSE or reasoning text.
          const clone=response.clone();
          (async()=>{const reader=clone.body.getReader();const decoder=new TextDecoder();let buffer='';
            while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});
              const lines=buffer.split('\n');buffer=lines.pop();for(const line of lines){if(!line.startsWith('data:'))continue;
                try{const chunk=JSON.parse(line.slice(5));if(chunk.usage)emit({type:'raw_usage',call,responseModel:chunk.model,usage:chunk.usage});}catch{}}
            }
          })().catch(()=>{});
        }
        return response;
      }});
    const output=ai.createAssistantMessageEventStream();
    void (async()=>{
      for await(const event of source){
        if(event.type==='done'||event.type==='error'){
          const message=event.message||event.error;
          emit({type:'model_end',call,commandId,model:message.model,usage:message.usage,stopReason:message.stopReason,error:message.errorMessage,
            thinkingLength:message.content.filter(b=>b.type==='thinking').reduce((n,b)=>n+b.thinking.length,0),durationMs:message.durationMs});
        }
        output.push(event);
      }
      output.end(await source.result());
    })().catch(error=>{
      const failure={...ai.fauxAssistantMessage('',{stopReason:'error',errorMessage:String(error.message)}),model:m.id,provider:m.provider,api:m.api};
      emit({type:'model_end',call,commandId,model:m.id,stopReason:'error',error:String(error.message)});
      output.push({type:'error',reason:'error',error:failure});output.end(failure);
    });
    return output;
  };
  const loader={getExtensions:()=>({extensions:[],errors:[],runtime:sdk.createExtensionRuntime()}),
    getSkills:()=>({skills:[],diagnostics:[]}),getPrompts:()=>({prompts:[],diagnostics:[]}),getThemes:()=>({themes:[],diagnostics:[]}),
    getAgentsFiles:()=>({agentsFiles:[]}),getSystemPrompt:()=>c.system||'You are a travel assistant. Be concise. Never invent tool data.',
    getSystemPromptSource:()=>undefined,getAppendSystemPrompt:()=>[],getAppendSystemPromptSources:()=>[],extendResources(){},async reload(){}};
  const customTools=(c.tools||[]).map(t=>({name:t.name,label:t.name,description:t.description,parameters:t.parameters,
    execute:async(id,args,signal)=>{
      const result=await requestTool(id,t.name,args,signal);
      return {content:[{type:'text',text:JSON.stringify(result)}],details:{}};
    }}));
  ({session}=await sdk.createAgentSession({cwd:process.cwd(),agentDir:process.env.PI_CODING_AGENT_DIR,model,modelRuntime:runtime,
    thinkingLevel:c.fake?'off':(c.thinking||'low'),resourceLoader:loader,customTools,tools:customTools.map(t=>t.name),sessionManager:manager,
    settingsManager:sdk.SettingsManager.inMemory({compaction:{enabled:false,reserveTokens:16384,keepRecentTokens:64},retry:{enabled:false,provider:{maxRetries:0}},cacheWarming:'off',defaultTools:[]})}));
  session.subscribe(e=>{
    if(e.type==='message_update'){
      const u=e.assistantMessageEvent;
      if(u.type==='text_delta'||u.type==='thinking_delta')emit({type:u.type,length:u.delta.length,text:u.type==='text_delta'?u.delta:undefined,commandId});
    }else if(e.type==='tool_execution_end'){
      const id=`${manager.getSessionId()}:${e.toolCallId}`;
      emit({type:'tool_end',id,name:e.toolName,isError:e.isError,durationMs:performance.now()-(toolStarts.get(id)||performance.now())});
      toolStarts.delete(id);
    }else if(e.type==='message_end'){
      const m=e.message;
      emit({type:'message_end',role:m.role,stopReason:m.stopReason,usage:m.usage,model:m.model,durationMs:m.durationMs,
        thinkingLength:(m.content instanceof Array?m.content:[]).filter(b=>b.type==='thinking').reduce((n,b)=>n+b.thinking.length,0),
        text:(m.content instanceof Array?m.content:[]).filter(b=>b.type==='text').map(b=>b.text).join(''),error:m.errorMessage,commandId});
    }else if(['agent_settled','auto_compaction_end','auto_retry_start'].includes(e.type))emit({type:e.type,commandId});
  });
  return {model:model.id,reasoning:model.reasoning,sessionFile:session.sessionFile,toolNames:session.getActiveToolNames(),restoredMessages:session.messages.length};
}
async function handle(c){
  if(c.op==='tool_result'){const p=pending.get(c.id);pending.delete(c.id);p?.resolve(c.result);return;}
  try{
    let result;
    if(c.op==='init')result=await initialize(c);
    else if(c.op==='prompt'){
      commandId=c.id;
      commandKind='prompt';
      conditionVersion=c.conditionVersion;
      if(faux){faux.setResponses((c.responses||[]).map(r=>ai.fauxAssistantMessage(r.tool?ai.fauxToolCall(r.tool,r.args):r.text,{stopReason:r.tool?'toolUse':'stop'})));}
      await session.prompt(c.text);await session.waitForIdle();
      result={entries:snapshot(),modelCalls,toolRequests};
    }else if(c.op==='steer'){conditionVersion=c.conditionVersion;await session.steer(c.text);emit({type:'control_event',operation:'steer'});result={queued:true};}
    else if(c.op==='cancel'){await session.abort();emit({type:'control_event',operation:'cancel'});result={cancelled:true,entries:snapshot()};}
    else if(c.op==='compact'){
      commandId=c.id;
      commandKind='compact';
      if(faux)faux.setResponses([ai.fauxAssistantMessage('Goal: retain hotel A. Evidence: evidence-hotel-A. User selected A; no booking.')]);
      const compacted=await session.compact('Preserve selected items and evidence IDs.');
      result={compacted,entries:snapshot(),modelCalls};
    }else if(c.op==='inject'){
      manager.appendCustomMessageEntry('travel.current_state',c.text,false,{conditionVersion:c.version});
      // Rebuild on next init, like the proposed SQLite-backed request lifecycle.
      result={entries:snapshot(),modelCalls};
    }else if(c.op==='snapshot')result={entries:snapshot(),messages:manager.buildSessionContext().messages,modelCalls};
    else if(c.op==='audit'){
      const before=accesses.length;let rejected=0;
      for(const p of [path.join(process.env.STEP0_REPO,'AGENTS.md'),path.join(process.env.STEP0_REPO,'.agents')])try{fs.readFileSync(p);}catch(e){if(e.message==='Forbidden project resource access')rejected++;}
      result={accessCount:before,negativeRejected:rejected,accesses:accesses.slice(0,before),sessionFile:session.sessionFile};
    }else if(c.op==='crash')process.exit(17);
    else if(c.op==='shutdown'){await session?.abort();session?.dispose();emit({type:'done',id:c.id,result:{closed:true}});process.exit(0);}
    else throw new Error('Unknown operation');
    emit({type:'done',id:c.id,result});
  }catch(e){emit({type:'failed',id:c.id,error:String(e.message)});}
}
const input=readline.createInterface({input:process.stdin});
input.on('line',line=>{try{void handle(JSON.parse(line));}catch{emit({type:'protocol_error'});}});
input.on('close',()=>{session?.abort().finally(()=>process.exit(0))||process.exit(0);});
emit({type:'ready',pid:process.pid});
