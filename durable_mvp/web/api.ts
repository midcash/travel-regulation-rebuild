export interface CalendarEvent {
  title: string;
  start: string;
  end: string;
}

export interface PendingEvent {
  event: CalendarEvent;
  conflicts?: CalendarEvent[];
  updateId?: string;
  result?: unknown;
}

export interface Message {
  role: 'user' | 'assistant' | 'toolResult';
  text: string;
  calls: string[];
}

export interface State {
  conversationId: string;
  mode: 'offline' | 'deepseek';
  model: {modelId: string; provider: string};
  messages: Message[];
  note: string;
  timers: {until: number; state?: {status: string}}[];
  calendar: {events: CalendarEvent[]; pending: Record<string, PendingEvent>};
  busy: boolean;
  counts: {model: number; tool: number};
}

export interface Prompt {
  text: string;
  requestId: string;
}

async function request(url: string, body?: object): Promise<unknown> {
  const response = await fetch(url, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(15000),
  });
  const result: unknown = await response.json();
  if (!response.ok) {
    const message = result && typeof result === 'object' && 'error' in result ? result.error : undefined;
    throw new Error(typeof message === 'string' && message ? message : `HTTP ${response.status}`);
  }
  return result;
}

export async function loadState(): Promise<State> {
  // The same-origin server owns the response schema.
  return await request('/state') as State;
}

export async function sendPrompt(prompt: Prompt): Promise<void> {
  await request('/prompt', prompt);
}

export async function sendAction(endpoint: '/abort' | '/timer'): Promise<void> {
  await request(endpoint, {});
}
