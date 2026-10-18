"""Run offline contracts and opt-in real DeepSeek checks. No supplier network calls."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from control import Journal, Node, ROOT, summary, tool_schema
from travel_agent.settings import load_env
from travel_agent.telemetry import Store, Tracer

OUT = ROOT / 'artifacts/pi-step0'
NOW = '2026-09-10T09:00:00+08:00'
STATE = '当前用户已选酒店 A；证据 ID evidence-hotel-A；查询时间 2026-09-10T09:00:00+08:00；只是选定，没有预订。'


def initialize(node, fake=False, entries=None, tools=None):
    node.captured_at = NOW
    return node.call('init', fake=fake, entries=entries, tools=tools or [tool_schema()],
                     model='deepseek-flash', thinking='low',
                     system=f'你是差旅助手。当前捕获的上海时间固定为 {NOW}，相对日期基于这个时间。工具结果是历史回放，不能当成当前线上报价。不要编造。工具失败可重试，每项最多3次；无历史数据时说明缺口，不重复同样请求。简洁回答。')


def probe():
    load_env(ROOT / '.env')
    node = Node()
    try:
        config = initialize(node)
        node.call('prompt', text='请用 lookup 查询三亚，然后根据工具返回的 city 回答一句话。',
                  tool=lambda e: {'status': 'ok', 'city': e['args']['city'], 'evidence_id': 'sanya-1'})
        return {'configuration': config, **summary(node.events),
                'answers': [e.get('text') for e in node.events if e['type'] == 'message_end' and e.get('role') == 'assistant'],
                'request_meta': [e for e in node.events if e['type'] == 'request_meta']}
    finally:
        node.close()


def offline():
    results = {}
    with tempfile.TemporaryDirectory() as directory:
        journal = Journal(Path(directory) / 'journal.sqlite3')
        node = Node()
        node.journal = journal
        try:
            initialize(node, fake=True)
            def operation(e):
                return journal.tool(e, lambda _: {'status': 'ok', 'city': '三亚', 'evidence_ids': ['sanya-1']})
            out = node.call('prompt', text='查询三亚', responses=[{'tool': 'lookup', 'args': {'city': '三亚'}}, {'text': '三亚，证据 sanya-1。'}], tool=operation)
            assert out['modelCalls'] == 2 and out['toolRequests'] == 1
            assert any(e['type'] == 'text_delta' for e in node.events)
            journal.save(out['entries'])
            node.call('prompt', text='保留旧历史以验证压缩。'+('历史上下文，仅测试，不改变选定项。'*100), responses=[{'text': '已记录历史。'}])
            node.call('compact')
            injected = node.call('inject', text=STATE, version=1)
            journal.save(injected['entries'])
            assert any(e['type'] == 'compaction' for e in journal.load())
            before = injected['modelCalls']
            journal.select('hotel', 2, {'candidate': 'B', 'evidence_ids': ['evidence-hotel-B']})
            assert node.call('snapshot')['modelCalls'] == before
            results['idle_button_no_model'] = True
            # Simulate the next request boundary, without invoking a model.
            next_turn = node.call('inject', text=current_selection(journal), version=2)
            assert next_turn['modelCalls'] == before
            journal.save(next_turn['entries'])
            results['idle_selection_injected_at_next_turn'] = True
            audit = node.call('audit')
            assert audit['negativeRejected'] == 2 and audit.get('sessionFile') is None
            protected = [x for x in audit['accesses'] if x['path'].endswith('agents.md') or '.agents' in Path(x['path']).parts]
            assert protected == []
            assert list(Path(node.temp.name).rglob('*.jsonl')) == []
            results['resource_isolation'] = {'audited_accesses': audit['accessCount'], 'forbidden_attempts': len(protected), 'negative_rejected': 2}
            results['deterministic_pi_baseline'] = summary(node.events)
        finally:
            node.close()
        restored = Node()
        try:
            config = initialize(restored, fake=True, entries=journal.load())
            snap = restored.call('snapshot')
            assert 'evidence-hotel-A' in json.dumps(snap['messages'], ensure_ascii=False)
            assert 'evidence-hotel-B' in json.dumps(snap['messages'][-1], ensure_ascii=False)
            assert config.get('sessionFile') is None
            assert any(m['role'] == 'compactionSummary' for m in snap['messages'])
            results['sqlite_memory_restore'] = {'messages': config['restoredMessages'], 'session_file': False, 'compaction_preserved': True}
        finally:
            restored.close()
        calls = []
        event = {'id': 'idempotent', 'args': {'city': '三亚'}}
        def supplier(e):
            calls.append(e['id'])
            return {'status': 'ok'}
        journal.tool(event, supplier)
        journal.tool(event, supplier)
        assert len(calls) == 1
        try:
            journal.tool({'id': 'unknown', 'args': {}}, supplier, interrupt=True)
        except InterruptedError:
            pass
        with journal.connect() as db:
            assert db.execute("SELECT status FROM pi0_tools WHERE id='unknown'").fetchone()[0] == 'unknown'
        journal.tool({'id': 'new-query', 'args': {}}, supplier)
        results['commit_before_delivery_and_unknown'] = True
        interrupted = Node()
        interrupted.journal = journal
        try:
            initialize(interrupted, fake=True, entries=journal.load())
            interrupted.send('prompt', text='查询三亚', responses=[{'tool':'lookup','args':{'city':'三亚'}}])
            while True:
                event = interrupted.receive(5)
                if event['type'] == 'tool_request':
                    unknown_id = event['id']
                    try:
                        journal.tool(event, lambda _: {'status':'ok','city':'三亚'}, interrupt=True)
                    except InterruptedError:
                        pass
                    break
        finally:
            interrupted.close(kill=True)
        restarted = Node()
        restarted.journal = journal
        try:
            initialize(restarted, fake=True, entries=journal.load())
            assert restarted.call('snapshot')['modelCalls'] == 0
            with journal.connect() as db:
                assert db.execute('SELECT status FROM pi0_tools WHERE id=?',(unknown_id,)).fetchone()[0] == 'unknown'
            continued = restarted.call('prompt', text='继续查询三亚',
                responses=[{'tool':'lookup','args':{'city':'三亚'}},{'text':'新查询成功。'}],
                tool=lambda e: journal.tool(e, lambda _: {'status':'ok','city':'三亚'}))
            new_ids = [e['id'] for e in restarted.events if e['type']=='tool_request']
            assert len(new_ids)==1 and new_ids[0] != unknown_id and continued['toolRequests']==1
            results['windows_active_tool_kill_and_restart'] = {'unknown_preserved':True,'no_automatic_model_call':True,'continue_uses_new_query_id':True}
        finally:
            restarted.close()
        for action in ('graceful', 'kill', 'crash'):
            child = Node()
            initialize(child, fake=True)
            if action == 'crash':
                child.send('crash')
                child.process.wait(timeout=10)
                assert child.process.returncode == 17
            child.close(kill=action == 'kill')
            assert child.process.poll() is not None
            results['windows_'+action] = child.process.returncode
    return results


def live():
    from otlp import Capture
    load_env(ROOT / '.env')
    results = {}
    with tempfile.TemporaryDirectory() as directory:
        journal = Journal(Path(directory) / 'journal.sqlite3')
        node = Node()
        node.journal = journal
        capture = Capture()
        try:
            results['configuration'] = initialize(node)
            store = Store(OUT / 'live-trace.sqlite3')
            tracer = Tracer(store, capture.tracer)
            model_spans = {}
            def observe(e):
                if e['type'] == 'model_start':
                    span = tracer.span('chat', model=e['model'], request_id=e['call'],
                                       operation=e.get('operation','prompt'), condition_version=e.get('conditionVersion',1))
                    attrs = span.__enter__()
                    model_spans[e['call']] = (span, attrs)
                if e['type'] == 'model_end':
                    span, attrs = model_spans.pop(e['call'])
                    attrs.update({f'tokens.{k}': v for k, v in e.get('usage', {}).items() if isinstance(v, (int, float))})
                    attrs['node.duration_ms'] = e.get('durationMs') or 0
                    attrs['stop_reason'] = e['stopReason']
                    span.__exit__(None, None, None)
                if e['type'] == 'tool_end':
                    with tracer.span('execute_tool', tool=e['name'], tool_call_id=e['id'],
                                     node_duration_ms=e['durationMs'], error=e['isError']):
                        pass
                if e['type'] == 'control_event':
                    with tracer.span('control', operation=e['operation']):
                        pass
            node.observer = observe
            with tracer.span('invoke_agent'):
                first = node.call('prompt', text='请调用 lookup 查询三亚。根据返回结果，用一句话回答，并引用 evidence_id。',
                    tool=lambda e: journal.tool(e, lambda _: {'status': 'ok', 'city': '三亚', 'evidence_id': 'sanya-1'}))
            journal.save(first['entries'])
            assert first['toolRequests'] >= 1
            results['tool_stream_usage'] = summary(node.events)
            assert results['tool_stream_usage']['text_deltas'] > 0
            assert results['tool_stream_usage']['usage']['totalTokens'] > 0
            journal.select('hotel', 1, {'candidate': 'A', 'evidence_ids': ['evidence-hotel-A']})
            injected = node.call('inject', text=current_selection(journal), version=1)
            journal.save(injected['entries'])
            at = len(node.events)
            node.call('prompt', text='请先调用 lookup 查询杭州，拿到结果后再回答。',
                tool=lambda e: active_button(node, journal, e))
            after = summary(node.events[at:])
            text = '\n'.join(e.get('text', '') for e in node.events[at:] if e['type'] == 'message_end' and e.get('role') == 'assistant')
            assert 'STEER_OK' in text
            results['steer_button'] = {**after, 'acknowledged': True}
            at = len(node.events)
            request = node.send('prompt', text='调用 lookup 查询北京，得到结果后再回答。')
            while True:
                event = node.receive()
                if event['type'] == 'tool_request':
                    cancel_at = time.monotonic()
                    node.call('cancel')
                    assert time.monotonic()-cancel_at < 10
                    break
                if event['type'] == 'done' and event.get('id') == request:
                    raise AssertionError('Model did not call cancellation test tool')
            # Drain the prompt completion if it was emitted before cancel completion.
            results['cancel'] = {**summary(node.events[at:]), 'settled_within_10s': True}
            at = len(node.events)
            compact = node.call('compact')
            journal.save(compact['entries'])
            assert any(e['type'] == 'compaction' for e in compact['entries'])
            results['compaction'] = summary(node.events[at:])
            injected = node.call('inject', text=current_selection(journal), version=2)
            journal.save(injected['entries'])
            audit = node.call('audit')
            assert audit['negativeRejected'] == 2 and not audit.get('sessionFile')
            results['resource_audit'] = {'access_count': audit['accessCount'], 'negative_rejected': audit['negativeRejected']}
            results['all_usage'] = summary(node.events)
            results['raw_usage_fields'] = sorted({k for u in results['all_usage']['raw_usage'] for k in u})
            store.save_run({'trace_id': tracer.trace_id, 'started_at': NOW, 'kind': 'pi-step0', 'model': 'deepseek-flash',
                            'usage': results['all_usage']['usage']})
            results['local_trace_id'] = tracer.trace_id
        finally:
            node.close()
            results['otlp'] = capture.close(expect_tools=True)
        restored = Node()
        restored.journal = journal
        restored_capture = Capture()
        try:
            tracer = Tracer(store, restored_capture.tracer)
            restored.observer = observe
            initialize(restored, entries=journal.load())
            with tracer.span('invoke_agent'):
                answer = restored.call('prompt', text='只回顾已选酒店及证据 ID，不要查询新数据。')
            text = '\n'.join(e.get('text', '') for e in restored.events if e['type'] == 'message_end')
            assert 'evidence-hotel-B' in text and answer['toolRequests'] == 0
            results['restored_answer'] = text
            results['restore_usage'] = summary(restored.events)
            store.save_run({'trace_id': tracer.trace_id, 'started_at': NOW, 'kind': 'pi-step0-restore',
                            'usage': results['restore_usage']['usage']})
        finally:
            restored.close()
            results['restored_otlp'] = restored_capture.close()
    return results


def active_button(node, journal, event):
    journal.select('hotel', 2, {'candidate': 'B', 'evidence_ids': ['evidence-hotel-B']})
    node.condition_version = 2
    node.send('steer', text='用户刚点击选定酒店 B，条件版本2，证据 evidence-hotel-B。停止旧查询方向。下一次回答以 STEER_OK 开头，说明已选 B，未预订。')
    return journal.tool(event, lambda _: {'status': 'ok', 'city': event['args']['city']}, version=event['conditionVersion'])


def current_selection(journal):
    with journal.connect() as db:
        row = db.execute("SELECT version,body FROM pi0_state WHERE id='hotel'").fetchone()
    return ('SQLite 当前有效选定项，优先于历史中的旧选择：条件版本'+str(row[0])+'，'+row[1]+
            '；查询时间 '+NOW+'；只选定，未预订。旧选择已被替代，不需要用户再次确认。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['offline', 'probe', 'live'])
    args = parser.parse_args()
    start = time.monotonic()
    try:
        result = globals()[args.mode]()
        result = {'status': 'PASS', 'elapsed_seconds': round(time.monotonic()-start, 2), **result}
    except Exception as error:
        result = {'status': 'FAIL', 'error_type': type(error).__name__, 'error': str(error), 'elapsed_seconds': round(time.monotonic()-start, 2)}
    if args.mode != 'offline':
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f'{args.mode}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if result['status'] != 'PASS':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
