import json
import unittest

from travel_agent.providers import tool_call
from test_transport import TransportTests, TransportModel


class DetailModel(TransportModel):
    def chat(self, messages, schemas, timeout):
        results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        if len(results) == 1:
            candidate = results[0]['data']['candidates'][0]
            return {'message': tool_call('get_transport_detail', {'candidate_id': candidate['candidate_id']}),
                    'input_tokens': 0, 'output_tokens': 0}
        return super().chat(messages, schemas, timeout)


class DetailTests(unittest.TestCase):
    run_supplier = TransportTests.run_supplier
    response = staticmethod(TransportTests.response)

    def test_observed_flight_detail_preserves_terminals_prices_and_lineage(self):
        def supplier(request):
            body = json.loads(request.content)
            if body['params']['name'] == 'searchLowestPriceFlight':
                return self.response(request, [{'flightNumber': 'HU7386',
                    'departureTime': '2026-09-18 16:05:00', 'arrivalTime': '2026-09-18 18:55:00'}])
            self.assertEqual(body['params'], {'name': 'multiCabinDetails', 'arguments': {
                'departureCityName': '杭州', 'arrivalCityName': '三亚',
                'departureDate': '2026-09-18', 'flightNo': 'HU7386'}})
            payload = {'flightInfo': {'flightNo': 'HU7386', 'arrivalAirport': '凤凰机场',
                        'arrivalTerminal': 'T3', 'departureTerminal': 'T3'},
                       'cabinInfo': [{'basePrice': '123.4501', 'totalTax': '50.01', 'cabinClass': '经济舱'}]}
            return http_response(body, payload)
        run, trace, files = self.run_supplier(supplier, DetailModel())
        self.assertEqual(run['termination'], 'completed')
        search, detail = run['tool_results']
        self.assertEqual(detail['status'], 'ok')
        self.assertEqual(detail['data']['candidate_id'], search['data']['candidates'][0]['candidate_id'])
        self.assertEqual(detail['data']['candidate_evidence_ref'], search['evidence_refs'][0])
        self.assertEqual(detail['data']['flight']['arrivalAirport'], '凤凰机场')
        self.assertEqual(detail['data']['flight']['arrivalTerminal'], 'T3')
        self.assertEqual(detail['data']['cabin_options'][0]['basePrice'], '123.4501')
        self.assertNotEqual(search['evidence_refs'], detail['evidence_refs'])
        self.assertEqual(run['http_requests'], 2)
        self.assertEqual(len(files), 2)
        self.assertEqual(trace['tool_results'], run['tool_results'])
        self.assertEqual(run['status'], 'needs_review')


def http_response(body, payload):
    import httpx
    return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'],
                                   'result': {'structuredContent': payload}})
