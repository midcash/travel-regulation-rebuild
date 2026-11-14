import {element} from './dom.js';

export function setupLayout(resizeCalendar: () => void) {
  const app = element('app');
  const buttons = [element('schedule-toggle'), element('schedule-toggle-notes')];
  const narrow = window.matchMedia('(max-width: 860px)');
  let scheduleOpen = !narrow.matches;
  let narrowLayout = narrow.matches;
  let pendingAutoScroll = false;

  function setSchedule(open: boolean) {
    scheduleOpen = open;
    app.classList.toggle('schedule-open', open);
    app.classList.toggle('schedule-closed', !open);
    for (const button of buttons) {
      button.setAttribute('aria-expanded', String(open));
      button.textContent = open ? '收起日程' : '显示日程';
    }
    requestAnimationFrame(resizeCalendar);
  }

  function scrollToLatestMessage() {
    const scroller = element('chat-scroll');
    scroller.scrollTop = scroller.scrollHeight;
    pendingAutoScroll = false;
  }

  function showView() {
    const notes = location.hash === '#/notes';
    element('chat-view').hidden = notes;
    element('notes-view').hidden = !notes;
    for (const [id, active] of [['nav-chat', !notes], ['nav-notes', notes]] as const) {
      const link = element(id);
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    document.title = (notes ? '笔记与数据' : '对话') + ' · Pi Durable';
    if (!notes && pendingAutoScroll) requestAnimationFrame(scrollToLatestMessage);
  }

  for (const button of buttons) button.onclick = () => setSchedule(!scheduleOpen);
  element('schedule-close').onclick = () => setSchedule(false);
  element('confirmation-shortcut').onclick = () => {
    if (!scheduleOpen) setSchedule(true);
    requestAnimationFrame(() => {
      const panel = element('confirmation-panel');
      panel.scrollIntoView({block: 'center'});
      panel.focus({preventScroll: true});
    });
  };
  setSchedule(scheduleOpen);

  addEventListener('resize', () => {
    if (narrow.matches === narrowLayout) return;
    narrowLayout = narrow.matches;
    setSchedule(!narrowLayout);
  });

  const composerText = element('text', HTMLTextAreaElement);
  composerText.addEventListener('input', () => {
    composerText.style.height = 'auto';
    composerText.style.height = Math.min(composerText.scrollHeight, 180) + 'px';
  });
  addEventListener('hashchange', showView);
  showView();

  return {
    followConversation() {
      // A hidden chat view has no layout.
      if (element('chat-view').hidden) pendingAutoScroll = true;
      else scrollToLatestMessage();
    },
  };
}
