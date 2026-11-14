import type {State} from './api.js';
import {element, setText} from './dom.js';

const clock = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Etc/GMT-8', timeStyle: 'short', hourCycle: 'h23'});
const labels = {user: '你', assistant: '助手', toolResult: '工具返回'};

export function setupConversation(followConversation: () => void) {
  let renderedMessages = '';

  return function render(state: State) {
    setText('mode', state.mode === 'offline' ? '离线演示 · 无模型费用' : `真实模型 · ${state.model.modelId}`);
    setText('description', state.mode === 'offline'
      ? '当前使用脚本化回显验证运行链路，不具备真实语言理解能力。会话、笔记和后台任务保存在本机。'
      : '由 pi-ai 接入 DeepSeek。提交消息和恢复未完成任务可能产生模型费用。');
    setText('status', state.busy ? '正在执行，可取消或刷新' : '就绪 · 已载入保存状态');

    // The conversation is a live region: redraw only when its content really changed.
    const signature = JSON.stringify(state.messages);
    if (signature !== renderedMessages) {
      const scroller = element('chat-scroll');
      const follow = renderedMessages === '' || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
      renderedMessages = signature;
      const messages = element('messages');
      messages.replaceChildren();
      for (const message of state.messages) {
        if (!message.text && !message.calls.length) continue;
        const item = document.createElement('article');
        item.className = 'message ' + message.role;
        const label = document.createElement('strong');
        label.textContent = labels[message.role];
        if (message.role !== 'toolResult') label.className = 'sr-only';
        const text = document.createElement('p');
        text.textContent = message.text;
        item.append(label, text);
        if (message.calls.length) {
          const calls = document.createElement('small');
          calls.textContent = '调用工具：' + message.calls.join(', ');
          item.append(calls);
        }
        messages.append(item);
      }
      if (follow) followConversation();
    }

    setText('note', state.note || '暂无笔记');
    setText('identity', `会话 ${state.conversationId} · ${state.model.provider}`);
    setText('counts', `本进程模型调用 ${state.counts.model} · 工具调用 ${state.counts.tool}`);
    element('timers').replaceChildren(...state.timers.map(timer => {
      const li = document.createElement('li');
      li.textContent = timer.state?.status === 'terminal' ? '计时完成' : '等待到期 · ' + clock.format(timer.until);
      return li;
    }));
  };
}
