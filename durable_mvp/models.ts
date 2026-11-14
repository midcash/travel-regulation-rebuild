import {createModels} from '@earendil-works/pi-ai/models';
import {deepseekProvider} from '@earendil-works/pi-ai/providers/deepseek';
import {fauxProvider, fauxAssistantMessage, fauxToolCall, type FauxResponseFactory}
  from '@earendil-works/pi-ai/providers/faux';
import type {ModelRef} from '@earendil-works/pi-durable';

const calendarCommands: ReadonlyMap<string, string> = new Map([
  ['/calendar', 'create_calendar_event'],
  ['/events', 'list_calendar_events'],
  ['/update', 'update_calendar_event'],
  ['/delete', 'delete_calendar_event'],
]);

const demoCommands: ReadonlyMap<string, string> = new Map([
  ['/note', 'save_note'],
  ['/slow', 'slow_echo'],
  ['/fail', 'fail_demo'],
]);

const respondOffline: FauxResponseFactory = request => {
  const index = request.messages.findLastIndex(message => message.role === 'user');
  const user = request.messages[index];
  const text = user?.role === 'user' ? user.content : '';
  const result = request.messages.slice(index + 1).findLast(message => message.role === 'toolResult');
  if (result) return fauxAssistantMessage((result.isError ? '工具失败：' : '工具结果：') +
    result.content.filter(block => block.type === 'text').map(block => block.text).join('\n'));

  const [command = '', ...rest] = String(text).split(' ');
  if (command === '/now') return fauxAssistantMessage(fauxToolCall('get_current_time', {}), {stopReason: 'toolUse'});
  const calendarTool = calendarCommands.get(command);
  if (calendarTool) {
    try {
      return fauxAssistantMessage(fauxToolCall(calendarTool, JSON.parse(rest.join(' '))), {stopReason: 'toolUse'});
    } catch {
      return fauxAssistantMessage('请提供有效 JSON 参数；离线模式不具备自然语言理解能力。');
    }
  }
  if (command === '确认重叠') return fauxAssistantMessage(
    fauxToolCall('create_calendar_event', {confirmationId: rest.join(' ')}), {stopReason: 'toolUse'});
  const demoTool = demoCommands.get(command);
  if (demoTool) return fauxAssistantMessage(
    fauxToolCall(demoTool, demoTool === 'fail_demo' ? {} : {text: rest.join(' ')}), {stopReason: 'toolUse'});
  return fauxAssistantMessage('离线回显（非真实模型）：' + text);
};

export function configureModels(offline: boolean, event: (kind: 'model') => void) {
  const models = createModels();
  let model: ModelRef;
  if (offline) {
    const faux = fauxProvider();
    const base = faux.provider;
    models.setProvider({
      ...base,
      streamSimple: (...args) => {
        // Replenish scripted responses rather than imposing a finite conversation length.
        faux.appendResponses([respondOffline]);
        return base.streamSimple(...args);
      },
    });
    model = {provider: 'faux', modelId: 'faux-1'};
  } else {
    const provider = deepseekProvider();
    const modelId = process.env.DEEPSEEK_MODEL || 'deepseek-flash';
    const known = provider.getModels().find(candidate => candidate.id === modelId);
    if (!known) throw new Error('DEEPSEEK_MODEL is not in the locked pi-ai catalog. Supported: ' +
      provider.getModels().map(candidate => candidate.id).join(', '));
    // Model transport, authentication and streaming remain entirely in pi-ai.
    const configured = {...known, baseUrl: process.env.DEEPSEEK_BASE_URL || known.baseUrl};
    models.setProvider({...provider, getModels: () => [configured]});
    model = {provider: 'deepseek', modelId};
  }
  const stream = models.streamSimple.bind(models);
  models.streamSimple = (...args) => { event('model'); return stream(...args); };
  return {models, model};
}
