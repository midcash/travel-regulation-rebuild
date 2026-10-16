"""Strict fixture replay of archived supplier summaries; never a live supplier fallback."""
import copy
import json
from pathlib import Path
import tempfile
import time

from control import Journal, Node, ROOT, summary
from run import initialize, NOW, current_selection
from travel_agent.real_tools import REAL_SCHEMAS
from travel_agent.settings import load_env

HISTORY = ROOT / 'experiments/experiment_2_hil_tools/results/sanya_live_20260910T094400'


def project(value):
    """Explicit compact projection, keeping unknowns and source IDs intact."""
    if isinstance(value, dict):
        omitted = {'supplier', 'raw', 'detail', 'flight', 'photos', 'polyline', 'tmcs', 'steps', 'source_text', 'clauses', 'text', 'policies', 'reviews'}
        return {k: project(v) for k, v in value.items() if k not in omitted}
    if isinstance(value, list):
        return [project(v) for v in value[:10]]
    return value


class Replay:
    def __init__(self, scenario):
        self.data = json.loads((HISTORY / 'summary.json').read_text(encoding='utf-8'))
        self.scenario = scenario
        self.attempts = {}
        self.requests = []
        self.observed = set()
        self.observed_candidates = set()
        self.http_requests = 0

    def lookup(self, event):
        name, args = event['name'], event['args']
        key = None
        if name == 'search_transport' and all(args.get(k) == v for k, v in {
            'mode': 'flight', 'origin': '杭州', 'destination': '三亚', 'departure_date': '2026-09-18'}.items()) and args.get('service_class', 'any') == 'any':
            key = 'flights'
        elif name == 'search_stays' and all(args.get(k) == v for k, v in {
            'destination': '三亚', 'check_in': '2026-09-18', 'check_out': '2026-09-19'}.items()) and args.get('meeting_place', '三亚湾红树林国际会议中心') == '三亚湾红树林国际会议中心':
            key = 'stays'
        elif name == 'get_transport_detail' and args.get('candidate_id') == 'flight:HU7386' and 'flight:HU7386' in self.observed_candidates:
            key = 'flight_detail'
        elif name == 'get_stay_detail' and args.get('candidate_id') == 'hotel:34548' and 'hotel:34548' in self.observed_candidates:
            key = 'hotel_detail'
        elif name == 'resolve_place' and args.get('city', '三亚') == '三亚':
            role = args.get('role')
            if role == 'meeting' and args.get('keyword', '三亚湾红树林国际会议中心') == '三亚湾红树林国际会议中心':
                key = 'meeting'
            elif role == 'arrival' and args.get('candidate_id') == 'flight:HU7386' and 'flight_detail' in self.observed and args.get('keyword', '三亚凤凰国际机场T3航站楼') == '三亚凤凰国际机场T3航站楼':
                key = 'airport'
            elif role == 'hotel' and args.get('candidate_id') == 'hotel:34548' and 'hotel:34548' in self.observed_candidates and args.get('keyword', '三亚海韵度假酒店') == '三亚海韵度假酒店':
                key = 'hotel_place'
        elif name == 'calculate_route':
            for k, start, end in [('airport_hotel', 'airport', 'hotel_place'), ('hotel_meeting', 'hotel_place', 'meeting')]:
                if start in self.observed and end in self.observed:
                    a = self.data[start]['result']['data']['candidates'][0]
                    b = self.data[end]['result']['data']['candidates'][0]
                    by_id = (args.get('origin_candidate_id'), args.get('destination_candidate_id')) == ('place:'+start, 'place:'+end)
                    by_coordinate = (args.get('origin'), args.get('destination')) == (a['location'], b['location'])
                    identity_ok = args.get('origin_id', a['place_id']) == a['place_id'] and args.get('destination_id', b['place_id']) == b['place_id']
                    if (by_id or by_coordinate) and identity_ok:
                        key = k
        matched = key is not None
        self.requests.append({'tool': name, 'query': args, 'matched': matched, 'fixture': key})
        if not matched:
            return {'status': 'error', 'error': {'type': 'NoHistoricalData', 'message': '无历史数据：没有匹配本工具与查询参数的记录。', 'retryable': False}, 'evidence_refs': [], 'http_requests': 0}
        defaults = {'search_transport':{'service_class':'any','max_results':5}, 'search_stays':{'max_results':5},
                    'get_transport_detail':{'max_options':20}, 'resolve_place':{'max_results':5},
                    'calculate_route':{'max_paths':3}}
        query_key = json.dumps({'tool':name, **defaults.get(name, {}), **args}, sort_keys=True, ensure_ascii=False)
        self.attempts[query_key] = self.attempts.get(query_key, 0)+1
        if self.scenario == 'retry' and self.attempts[query_key] <= {'flights': 2, 'stays': 1}.get(key, 0):
            self.http_requests += 1
            return {'status': 'error', 'error': {'type': 'HistoricalSupplierFailure', 'message': '历史该次查询失败，可在预算内重试。', 'retryable': True}, 'http_requests': 1, 'evidence_refs': []}
        self.observed.add(key)
        result = copy.deepcopy(self.data[key]['result'])
        if name == 'resolve_place':
            for c in result['data']['candidates']:
                c['candidate_id'] = 'place:'+key
        if name == 'get_transport_detail':
            # Old summary puts real detail in data.flight, so retain this small projection.
            flight = result['data'].get('flight') or {}
            result['data']['flight_summary'] = {k: flight.get(k) for k in ['flightNumber', 'craftType', 'departureTime', 'arrivalTime', 'departureAirport', 'arrivalAirport', 'departureTerminal', 'arrivalTerminal']}
        result = project(result)
        if name in ('search_transport', 'search_stays'):
            result['data']['candidates'] = result['data']['candidates'][:args.get('max_results', 5)]
        if name == 'resolve_place':
            result['data']['candidates'] = result['data']['candidates'][:args.get('max_results', 5)]
        if name == 'get_transport_detail' and isinstance(result['data'].get('cabin_options'), list):
            result['data']['cabin_options'] = result['data']['cabin_options'][:args.get('max_options', 20)]
        if name == 'calculate_route':
            result['data']['paths'] = result['data']['paths'][:args.get('max_paths', 3)]
        for candidate in result['data'].get('candidates', []):
            if candidate.get('candidate_id'):
                self.observed_candidates.add(candidate['candidate_id'])
        result['http_requests'] = {'meeting': 3, 'airport': 3, 'hotel_place': 3}.get(key, 1)
        self.http_requests += result['http_requests']
        result['historical_observed_at'] = NOW
        result['historical_fixture'] = key
        return result


def run_replays(scenarios=None):
    load_env(ROOT / '.env')
    rows = []
    tools = [{'name': s['function']['name'], 'description': s['function']['description'], 'parameters': s['function']['parameters']}
             for s in REAL_SCHEMAS if s['function']['name'] not in ('finish', 'get_policy_snapshot')]
    for scenario in scenarios or ('normal', 'retry', 'redirect', 'compact_restore'):
        for trial in range(1, 4):
            at = time.monotonic()
            row = {'scenario': scenario, 'trial': trial}
            replay = Replay(scenario)
            with tempfile.TemporaryDirectory() as directory:
                journal = Journal(Path(directory) / 'journal.sqlite3')
                node = Node()
                node.journal = journal
                original_events = []
                try:
                    initialize(node, tools=tools)
                    prompt = ('下周六2026-09-19早上9点在三亚湾红树林国际会议中心开会，从杭州出发，乘飞机提前一晚抵达，住一晚。'
                        '请查询可用候选，推荐能准时到会的安排，按需核对航班详情、酒店详情、地点和两段路线。未知费用不能填零，不默认适用单位制度。'
                        '这是稀疏历史回放，缺少所选候选的详情时可以给出有依据的部分成果，不要编造。最多给一个主要推荐，最终回答不超过400字。')
                    changed = []
                    def tool(event):
                        started_version = event['conditionVersion']
                        if scenario == 'redirect' and not changed:
                            changed.append(True)
                            node.condition_version = 2
                            node.send('steer', text='用户改口：先不做整套方案，改查同日期杭州到三亚高铁。缺历史数据就说明，停止继续酒店和路线查询。')
                        return journal.tool(event, replay.lookup, version=started_version)
                    output = node.call('prompt', text=prompt, tool=tool)
                    journal.save(output['entries'])
                    if scenario == 'compact_restore':
                        journal.select('hotel', 1, {'candidate':'A','evidence_ids':['evidence-hotel-A']})
                        compact = node.call('compact')
                        journal.save(compact['entries'])
                        injected = node.call('inject', text=current_selection(journal), version=1)
                        journal.save(injected['entries'])
                        completed_events = list(node.events)
                        node.close()
                        node = Node()
                        original_events = completed_events
                        node.journal = journal
                        initialize(node, entries=journal.load(), tools=tools)
                        node.call('prompt', text='仅根据当前有效选定项返回JSON对象，字段为 selected_candidate 和 evidence_id。不要查询新数据，也不要附加说明。', tool=tool)
                        restored_answers = [e.get('text','') for e in node.events if e['type']=='message_end' and e.get('role')=='assistant']
                        assert restored_answers
                        assert_restored_selection(restored_answers[-1])
                        assert summary(node.events)['tool_calls'] == 0, 'Restore must not start a new supplier query'
                        events = original_events+node.events
                    else:
                        events = node.events
                    row.update(summary(events))
                    answers = [e for e in events if e['type'] == 'message_end' and e.get('role') == 'assistant']
                    row['ended_normally'] = bool(answers) and answers[-1].get('stopReason') == 'stop'
                    row['final_answer'] = answers[-1].get('text', '') if answers else ''
                    row['status'] = 'PASS' if row['ended_normally'] and not row['errors'] else 'FAIL'
                    row['observed_fixtures'] = sorted(replay.observed)
                except Exception as error:
                    row.update(status='FAIL', error=str(error), **summary(original_events+node.events))
                finally:
                    node.close()
            matched = sum(r['matched'] for r in replay.requests)
            if len(replay.requests) != row.get('tool_calls', 0):
                row['status'] = 'FAIL'
                row['coverage_error'] = 'Tool request coverage accounting mismatch'
            row.update(requests=replay.requests, matched=matched, unmatched=len(replay.requests)-matched,
                       http_requests=replay.http_requests, repeated_matched_queries=sum(max(0,n-1) for n in replay.attempts.values()),
                       coverage=matched/len(replay.requests) if replay.requests else 0,
                       elapsed_seconds=round(time.monotonic()-at, 2))
            rows.append(row)
            out = ROOT / 'artifacts/pi-step0/replay-progress.json'
            out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: row.get(k) for k in ('scenario', 'trial', 'status', 'model_calls', 'tool_calls', 'usage', 'coverage', 'elapsed_seconds')}, ensure_ascii=False), flush=True)
    return {'model': 'deepseek-flash', 'thinking': 'low', 'fixed_shanghai_time': NOW, 'trials_per_class': 3,
            'validation_revision': 'review-fixed',
            'normal_output_cap': 4096, 'compaction_output_cap': 8192, 'compaction_reserve': 16384,
            'fixture_source': str(HISTORY.relative_to(ROOT)), 'rows': rows,
            'all_runs_completed': all(r['status'] == 'PASS' for r in rows),
            'note': 'New DeepSeek decisions using archived supplier summaries; not reproduction of historical model usage or production B acceptance.'}


def assert_restored_selection(text):
    # The values are only in SQLite-derived state, never repeated in this query.
    start, end = text.find('{'), text.rfind('}')
    value = json.loads(text[start:end+1])
    assert value.get('selected_candidate') == 'A', 'Wrong restored selected candidate'
    assert value.get('evidence_id') == 'evidence-hotel-A', 'Wrong restored evidence ID'
