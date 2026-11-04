import json
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ReadTimeoutError

import server
from test_server import response


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def run_case(self, responses, status, kind=None, stage='search', failures=1):
        with patch.object(server.SESSION, 'get', side_effect=responses) as get:
            with self.assertLogs(server.app.logger, level='INFO') as captured:
                r = self.client.get('/search?quality=lossless&atmos=auto&q=linked+jim+yosef')
        entries = [json.loads(line.split('archive_search ', 1)[1])
                   for line in captured.output if 'archive_search ' in line]
        summary, = entries
        self.assertEqual(r.status_code, status)
        self.assertEqual(summary['upstream_failures'], failures)
        if kind:
            self.assertEqual(r.json['errorType'], kind)
            self.assertEqual(r.json['stage'], stage)
            self.assertEqual(summary['failures'][-1]['error_type'], kind)
        self.assertNotIn('sensitive-value', '\n'.join(captured.output) + r.text)
        return r, summary, captured.output, get

    def test_http_errors_are_classified_and_retried_only_when_transient(self):
        for code in (400, 403, 429, 500, 503):
            with self.subTest(code=code):
                r = response()
                r.status_code = code
                r.raise_for_status.side_effect = requests.HTTPError('sensitive-value', response=r)
                attempts = 1 if code in (400, 403) else 2
                _, summary, logs, get = self.run_case([r] * attempts, 502, 'upstream_http')
                self.assertEqual(get.call_count, attempts)
                self.assertEqual(summary['upstream_attempt_failures'], attempts)
                self.assertEqual(summary['failures'][0]['upstream_status'], code)
                self.assertTrue(any('"attempt": 1' in line and 'elapsed_ms' in line for line in logs))

    def test_initial_timeout_is_counted(self):
        _, summary, _, get = self.run_case([requests.Timeout('sensitive-value')] * 2,
                                          504, 'upstream_timeout')
        self.assertEqual(summary['candidates'], 0)
        self.assertEqual(summary['upstream_attempt_failures'], 2)
        self.assertEqual(get.call_count, 2)

    def test_connection_failure(self):
        self.run_case([requests.ConnectionError('sensitive-value')] * 2, 502, 'upstream_network')

    def test_json_and_schema_errors_do_not_become_empty_results(self):
        bad_json = response()
        bad_json.json.side_effect = ValueError('sensitive-value')
        for r, kind in [(bad_json, 'upstream_parse'),
                        (response({'error': 'sensitive-value'}), 'upstream_api'),
                        (response({'responseHeader': {'status': 400}}), 'upstream_api'),
                        (response([]), 'upstream_schema'),
                        (response({}), 'upstream_schema'),
                        (response({'response': {'docs': None}}), 'upstream_schema'),
                        (response({'response': {'docs': [None]}}), 'upstream_schema')]:
            with self.subTest(kind=kind):
                _, summary, _, get = self.run_case([r], 502, kind)
                self.assertEqual(summary['upstream_attempt_failures'], 0)
                self.assertEqual(summary['failures'][0]['upstream_status'], 200)
                self.assertEqual(get.call_count, 1)

    def test_valid_empty_query_response_and_unchanged_query(self):
        r, summary, _, get = self.run_case([response({'response': {'docs': []}})],
                                          200, failures=0)
        self.assertEqual(r.json, {'tracks': []})
        self.assertEqual(summary['outcome'], 'empty')
        self.assertEqual(get.call_args.kwargs['params']['q'],
                         'mediatype:audio AND format:Flac AND licenseurl:* AND ("linked" AND "jim" AND "yosef")')

    def test_metadata_timeout_is_classified_at_correct_stage(self):
        search = response({'response': {'docs': [{'identifier': 'album'}]}})
        _, summary, _, _ = self.run_case([search] + [requests.Timeout()] * 2,
                                        504, 'upstream_timeout', 'metadata')
        self.assertEqual(summary['candidates'], 1)

    def test_metadata_schema_error(self):
        search = response({'response': {'docs': [{'identifier': 'album'}]}})
        self.run_case([search, response({'metadata': [], 'files': []})],
                      502, 'upstream_schema', 'metadata')

    def test_budget_exhausted_differs_from_upstream_timeout(self):
        self.run_case([server.SearchBudgetExceeded()], 504, 'budget_exhausted')

    def test_wrapped_signal_timeout_still_reports_budget(self):
        import time
        def wrapped_timeout(*args, **kwargs):
            try:
                time.sleep(.5)
            except server.SearchBudgetExceeded as exc:
                raise requests.ConnectionError("sensitive-value") from exc
        with patch.object(server, 'SEARCH_SECONDS', .03):
            _, summary, logs, get = self.run_case(wrapped_timeout, 504, 'budget_exhausted')
        self.assertEqual(get.call_count, 1)
        self.assertFalse(any('upstream_network' in line for line in logs))

    def test_flac_body_read_timeout_is_handled(self):
        search = response({'response': {'docs': [{'identifier': 'album'}]}})
        meta = response({'metadata': {'licenseurl': 'https://creativecommons.org/licenses/by/4.0/'},
                         'files': [{'name': 'linked jim yosef.flac'}]})
        audio = response()
        audio.raw = Mock()
        audio.raw.read.side_effect = ReadTimeoutError(None, '/secret', 'sensitive-value')
        self.run_case([search, meta, audio], 504, 'upstream_timeout', 'flac_probe')
        audio.__exit__.assert_called_once()

    def test_partial_results_survive_later_failure(self):
        search = response({'response': {'docs': [{'identifier': 'one'}, {'identifier': 'two'}]}})
        meta = response({'metadata': {'licenseurl': 'https://creativecommons.org/licenses/by/4.0/'},
                         'files': [{'name': 'linked jim yosef.flac'}]})
        r, summary, _, _ = self.run_case([search, meta, response()] + [requests.Timeout()] * 2,
                                        200)
        self.assertEqual(len(r.json['tracks']), 1)
        self.assertEqual(summary['outcome'], 'partial')

    def test_recovered_retry_is_visible_without_false_terminal_failure(self):
        r, summary, _, _ = self.run_case([requests.Timeout(), response({'response': {'docs': []}})],
                                        200, failures=0)
        self.assertEqual(summary['upstream_attempt_failures'], 1)
        self.assertEqual(r.json['tracks'], [])


if __name__ == '__main__':
    unittest.main()
