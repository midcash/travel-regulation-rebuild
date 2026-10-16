"""Publish only aggregate measurements, never prompts, responses, credentials or reasoning."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ART = ROOT / 'artifacts/pi-step0'


def read(name):
    return json.loads((ART / name).read_text(encoding='utf-8'))


def compact_row(row):
    fields = ('scenario', 'trial', 'status', 'model_calls', 'completed_requests', 'tool_calls',
              'http_requests', 'repeated_matched_queries', 'matched', 'unmatched', 'coverage',
              'elapsed_seconds', 'usage', 'usage_complete', 'error', 'coverage_error', 'ended_normally', 'observed_fixtures')
    return {k: row[k] for k in fields if k in row}


def generate():
    original = read('replay.json')
    compactions = read('replay-compact_restore.json')
    assert original.get('validation_revision') == 'review-fixed'
    rows = [r for r in original['rows'] if r['scenario'] != 'compact_restore']+compactions['rows']
    for row in rows:
        assert row['tool_calls'] == len(row['requests']), 'coverage mismatch must not be published as valid'
    ranges = {}
    for name in ('normal', 'retry', 'redirect', 'compact_restore'):
        group = [r for r in rows if r['scenario'] == name]
        assert len(group) == 3
        def bounds(field):
            values = [r[field] for r in group]
            return [min(values), max(values)]
        ranges[name] = {'runs': len(group), 'passed_protocol': sum(r['status'] == 'PASS' for r in group),
                        **{f: bounds(f) for f in ('model_calls', 'tool_calls', 'http_requests', 'coverage', 'elapsed_seconds')},
                        'total_tokens': [min(r['usage']['totalTokens'] for r in group), max(r['usage']['totalTokens'] for r in group)],
                        'input_including_cache': [min(r['usage']['input']+r['usage']['cacheRead'] for r in group),
                                                  max(r['usage']['input']+r['usage']['cacheRead'] for r in group)]}
    live = read('live.json')
    offline = read('offline.json')
    superseded = {}
    for name in ('replay-pilot.json','replay-pre-review.json','replay-redirect.json','replay-compact-before-oracle.json'):
        if (ART/name).exists():
            superseded[name] = [compact_row(r) for r in read(name)['rows']]
    digest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in Path(__file__).parent.glob('*') if p.suffix in ('.py', '.mjs') or p.name in ('package.json','package-lock.json')}
    evidence = {
        'date': '2026-10-16', 'baseline_commit': '0b1bf2e',
        'verification_commit': subprocess.check_output(['git', '-c', 'safe.directory='+str(ROOT), 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'model': 'deepseek-flash', 'reasoning_effort': 'low', 'sdk': '@earendil-works/pi-coding-agent@1.3.0',
        'fixed_shanghai_time': original['fixed_shanghai_time'],
        'ranges': ranges, 'runs': [compact_row(r) for r in rows],
        'superseded_debug_cohorts_not_for_budget': superseded,
        'pre_structured_restore_oracle_runs': [compact_row(r) for r in original['rows'] if r['scenario'] == 'compact_restore'],
        'exclusion_reason': 'Debug cohorts include parallel-request undercount, unobserved-candidate access, and weak restore assertions. Final full rerun fixes these; final compact rerun independently asserts structured selection/evidence and zero new queries. Earlier cap failure remains recorded.',
        'offline': offline,
        'live': {k: live[k] for k in ('status', 'configuration', 'tool_stream_usage', 'steer_button', 'cancel', 'compaction',
                                    'resource_audit', 'raw_usage_fields', 'local_trace_id', 'otlp', 'restore_usage', 'restored_otlp')},
        'source_sha256_at_report': digest,
        'limits': {'normal_output': 4096, 'compaction_output': 8192, 'compaction_reserve': 16384,
                   'keep_recent_for_forced_test_only': 64, 'automatic_retry': False, 'automatic_compaction': False},
        'interpretation': 'Protocol completion is not full business acceptance. HTTP counts replay archived operations, not actual supplier network requests. Pi input excludes cacheRead. Missing raw cache-write usage is unknown. Cancelled request accounting is incomplete.'}
    target = ROOT / 'docs/pi-step0-results.json'
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(ranges, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    generate()
