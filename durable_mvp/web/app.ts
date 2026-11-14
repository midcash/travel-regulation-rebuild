import {loadState, sendAction, sendPrompt} from './api.js';
import type {Prompt, State} from './api.js';
import {setupCalendar} from './calendar.js';
import {setupConversation} from './conversation.js';
import {element, errorMessage, setText} from './dom.js';
import {setupLayout} from './layout.js';

const pendingKey = 'durable-mvp-pending';
let pending = readPendingPrompt();
let sending = false;
let healthy = false;
let current: State | undefined;
let refreshId = 0;
let connectionError = false;

const composerText = element('text', HTMLTextAreaElement);
const calendar = setupCalendar(id => queuePrompt('确认重叠 ' + id));
const layout = setupLayout(calendar.resize);
const renderConversation = setupConversation(layout.followConversation);

function readPendingPrompt(): Prompt | null {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(pendingKey) || 'null');
    if (value && typeof value === 'object' && 'text' in value && 'requestId' in value
        && typeof value.text === 'string' && typeof value.requestId === 'string') {
      return {text: value.text, requestId: value.requestId};
    }
  } catch {
    // Invalid session data cannot be retried.
  }
  return null;
}

function canQueuePrompt() {
  return healthy && !sending && !current?.busy && !pending;
}

function controls() {
  element('send', HTMLButtonElement).disabled = !canQueuePrompt();
  element('cancel', HTMLButtonElement).disabled = !healthy || !current?.busy;
  element('timer', HTMLButtonElement).disabled = !healthy || sending;
  const retry = element('retry', HTMLButtonElement);
  retry.hidden = !pending;
  retry.disabled = !healthy || sending;

  let confirmationStatus: string | undefined;
  if (!healthy) confirmationStatus = '连接中断，恢复后可确认。';
  else if (pending) confirmationStatus = '有未确认提交的请求，请先重试同一请求。';
  else if (sending || current?.busy) confirmationStatus = '正在处理会话，完成后可确认。';
  calendar.setConfirmationControls(!canQueuePrompt(), confirmationStatus, current?.calendar.pending);
}

async function refresh() {
  const id = ++refreshId;
  try {
    const state = await loadState();
    if (id !== refreshId) return;
    healthy = true;
    current = state;
    renderConversation(state);
    calendar.render(state.calendar);
    calendar.retryIfFailed();
    if (connectionError) {
      setText('error', '');
      connectionError = false;
      calendar.refetch();
    }
  } catch (error) {
    if (id !== refreshId) return;
    healthy = false;
    connectionError = true;
    setText('status', '连接中断，等待服务恢复');
    setText('error', errorMessage(error));
  }
  controls();
}

function queuePrompt(text: string) {
  if (!canQueuePrompt() || !text.trim()) return;
  pending = {text, requestId: crypto.randomUUID()};
  sessionStorage.setItem(pendingKey, JSON.stringify(pending));
  void submit(pending);
}

async function submit(prompt: Prompt) {
  sending = true;
  controls();
  setText('error', '');
  try {
    await sendPrompt(prompt);
    if (composerText.value === prompt.text) {
      composerText.value = '';
      composerText.style.height = '';
    }
    pending = null;
    sessionStorage.removeItem(pendingKey);
  } catch (error) {
    setText('error', errorMessage(error) + '。请求未确认，可用相同 ID 重试。');
  } finally {
    sending = false;
    await refresh();
  }
}

element('composer').addEventListener('submit', event => {
  event.preventDefault();
  queuePrompt(composerText.value);
});
element('retry').onclick = () => { if (pending) void submit(pending); };
for (const [id, endpoint] of [['cancel', '/abort'], ['timer', '/timer']] as const) {
  element(id).onclick = async () => {
    sending = true;
    controls();
    setText('error', '');
    try {
      await sendAction(endpoint);
    } catch (error) {
      setText('error', errorMessage(error));
    } finally {
      sending = false;
      await refresh();
    }
  };
}

async function poll() {
  await refresh();
  setTimeout(poll, 700);
}
void poll();
