import json
from pathlib import Path
import tempfile
import unittest

from control import Journal, Node, summary
from replay import Replay, assert_restored_selection


class Contracts(unittest.TestCase):
    def test_restore_candidate_and_evidence_are_independent_assertions(self):
        assert_restored_selection('{"selected_candidate":"A","evidence_id":"evidence-hotel-A"}')
        with self.assertRaisesRegex(AssertionError, 'candidate'):
            assert_restored_selection('{"selected_candidate":"B","evidence_id":"evidence-hotel-A"}')
        with self.assertRaisesRegex(AssertionError, 'evidence'):
            assert_restored_selection('{"selected_candidate":"A","evidence_id":"evidence-hotel-B"}')
    def test_cancelled_provider_error_is_not_complete_usage(self):
        result = summary([{'type':'model_start'}, {'type':'model_end', 'stopReason':'error',
                          'error':'This operation was aborted', 'usage':{'totalTokens':0}}])
        self.assertFalse(result['usage_complete'])

    def test_missing_model_end_is_not_complete_usage(self):
        result = summary([{'type':'model_start'}, {'type':'model_end','stopReason':'stop','usage':{}}, {'type':'model_start'}])
        self.assertFalse(result['usage_complete'])

    def test_stream_iterator_exception_finishes_prompt_and_shutdown(self):
        node = Node()
        try:
            node.call('init', fake=True, failStream=True)
            result = node.wait(node.send('prompt', text='test', responses=[{'text':'unused'}]), timeout=3)
            self.assertTrue(result['entries'])
            self.assertFalse(summary(node.events)['usage_complete'])
            self.assertTrue(summary(node.events)['errors'])
        finally:
            node.close()

    def test_unknown_outcome_requires_new_query_id_and_completed_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            journal = Journal(Path(d)/'j.sqlite3')
            calls = []
            def supplier(e):
                calls.append(e['id'])
                return {'status': 'ok'}
            event = {'id': 'a', 'args': {'city': '三亚'}}
            a = journal.tool(event, supplier)
            self.assertEqual(a, journal.tool(event, supplier))
            self.assertEqual(len(calls), 1)
            with self.assertRaises(InterruptedError):
                journal.tool({'id': 'b', 'args': {}}, supplier, interrupt=True)
            with self.assertRaisesRegex(ValueError, 'Unknown outcome'):
                journal.tool({'id': 'b', 'args': {}}, supplier)
            journal.tool({'id': 'c', 'args': {}}, supplier)
            self.assertEqual(calls, ['a', 'b', 'c'])

    def test_condition_version_and_actual_query_survive_result_commit(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(Path(d)/'j.sqlite3')
            result = j.tool({'id': 'a', 'args': {'city': '三亚'}}, lambda _: {'status':'ok'}, version=1)
            j.select('hotel', 2, {'candidate': 'B'})
            self.assertEqual(result['condition_version'], 1)
            self.assertEqual(result['query'], {'city': '三亚'})
            with self.assertRaisesRegex(ValueError, 'Stale'):
                j.select('hotel', 1, {'candidate': 'A'})

    def test_replay_rejects_wrong_date_mode_and_unobserved_details(self):
        replay = Replay('normal')
        for name, args in [('search_transport', {'mode':'train','origin':'杭州','destination':'三亚','departure_date':'2026-09-18'}),
                           ('search_transport', {'mode':'flight','origin':'杭州','destination':'三亚','departure_date':'2030-09-18'}),
                           ('get_transport_detail', {'candidate_id':'flight:HU7386'})]:
            result = replay.lookup({'name':name,'args':args})
            self.assertEqual(result['error']['type'], 'NoHistoricalData')
            self.assertEqual(result['http_requests'], 0)
        self.assertEqual(sum(r['matched'] for r in replay.requests), 0)

    def test_replay_limits_candidates_and_reproduces_recorded_failures(self):
        replay = Replay('retry')
        request = {'name':'search_transport','args':{'mode':'flight','origin':'杭州','destination':'三亚','departure_date':'2026-09-18','max_results':2}}
        self.assertEqual(replay.lookup(request)['status'], 'error')
        self.assertEqual(replay.lookup(request)['status'], 'error')
        result = replay.lookup(request)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['data']['candidates']), 2)
        self.assertEqual(replay.http_requests, 3)
        missing = replay.lookup({'name':'get_transport_detail','args':{'candidate_id':'flight:HU7386'}})
        self.assertEqual(missing['error']['type'], 'NoHistoricalData')

    def test_distinct_actual_queries_have_distinct_retry_histories(self):
        r = Replay('retry')
        args = {'mode':'flight','origin':'杭州','destination':'三亚','departure_date':'2026-09-18'}
        r.lookup({'name':'search_transport','args':{**args,'max_results':1}})
        r.lookup({'name':'search_transport','args':{**args,'max_results':2}})
        self.assertEqual(len(r.attempts), 2)
        self.assertEqual(list(r.attempts.values()), [1,1])

    def test_unreturned_hotel_detail_is_not_observed(self):
        r = Replay('normal')
        r.lookup({'name':'search_stays','args':{'destination':'三亚','check_in':'2026-09-18','check_out':'2026-09-19','max_results':5}})
        result = r.lookup({'name':'get_stay_detail','args':{'candidate_id':'hotel:34548'}})
        self.assertEqual(result['error']['type'], 'NoHistoricalData')


if __name__ == '__main__':
    unittest.main()
