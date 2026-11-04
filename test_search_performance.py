import json
import unittest
from unittest.mock import patch

import requests
import server
from test_server import response

LICENSE = 'https://creativecommons.org/licenses/by/4.0/'


def metadata(title='linked', licenseurl=LICENSE):
    return response({'metadata': {'licenseurl': licenseurl},
                     'files': [{'name': title + '.flac', 'title': title}]})


def discovery(docs):
    return response({'response': {'docs': docs}})


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        server.SEARCH_CACHE.clear()
        server.METADATA_CACHE.clear()
        self.client = server.app.test_client()

    def search(self, q='linked'):
        with self.assertLogs(server.app.logger, level='INFO') as logs:
            r = self.client.get('/search', query_string={'q': q})
        summary = json.loads(next(line.split('archive_search ', 1)[1]
                                 for line in logs.output if 'archive_search ' in line))
        return r, summary

    def test_metadata_work_is_capped_for_large_candidate_list(self):
        docs = [{'identifier': str(i)} for i in range(16)]
        with patch.object(server.SESSION, 'get', side_effect=[discovery(docs)] + [
                response({'metadata': {'licenseurl': LICENSE}, 'files': []}) for _ in range(4)]) as get:
            r, summary = self.search()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(summary['metadata_requests'], 4)
        self.assertEqual(summary['selected_candidates'], 4)
        self.assertEqual(get.call_count, 5)
        self.assertEqual(r.json['tracks'], [])

    def test_title_match_is_ranked_first_and_verified_before_early_return(self):
        docs = [{'identifier': str(i), 'title': 'Other album'} for i in range(15)]
        docs.append({'identifier': 'best', 'title': 'Linked', 'licenseurl': LICENSE, 'format': ['Flac']})
        with patch.object(server.SESSION, 'get', side_effect=[discovery(docs), metadata(), response()]) as get:
            r, summary = self.search()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json['tracks']), 1)
        self.assertIn('/metadata/best', get.call_args_list[1].args[0])
        self.assertEqual(get.call_count, 3)
        self.assertEqual(summary['stop_reason'], 'verified_result')
        self.assertEqual(summary['metadata_requests'], 1)

    def test_index_rejections_avoid_metadata_requests(self):
        docs = [{'identifier': 'restricted', 'access-restricted-item': 'true'},
                {'identifier': 'unlicensed', 'licenseurl': 'https://example.org/all-rights-reserved'},
                {'identifier': 'mp3', 'format': ['MP3']},
                {'identifier': 'mp3', 'format': ['MP3']}]
        with patch.object(server.SESSION, 'get', return_value=discovery(docs)) as get:
            r, summary = self.search()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(summary['metadata_requests'], 0)
        self.assertEqual(get.call_count, 1)

    def test_index_license_is_not_a_playback_grant(self):
        docs = [{'identifier': 'album', 'title': 'linked', 'licenseurl': LICENSE}]
        with patch.object(server.SESSION, 'get', side_effect=[discovery(docs), metadata(licenseurl='')]) as get:
            r, _ = self.search()
        self.assertEqual(r.json['tracks'], [])
        self.assertEqual(get.call_count, 2)

    def test_invalid_flac_is_never_cached_as_a_track(self):
        audio = response()
        audio.raw.read = lambda _: b'not a flac'
        with patch.object(server.SESSION, 'get', side_effect=[
                discovery([{'identifier': 'album'}]), metadata(), audio]):
            r, _ = self.search()
        self.assertEqual(r.json['tracks'], [])
        self.assertEqual(server.SEARCH_CACHE.get(('linked', 20)), [])

    def test_repeat_normalized_search_uses_verified_result_cache(self):
        with patch.object(server.SESSION, 'get', side_effect=[
                discovery([{'identifier': 'album'}]), metadata(), response()]) as get:
            first, _ = self.search()
            second, summary = self.search('ＬＩＮＫＥＤ！')
        self.assertEqual(second.json, first.json)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(summary['cache_hits']['search'], 1)
        self.assertEqual(summary['metadata_requests'], 0)

    def test_metadata_reused_across_distinct_searches_and_license_rechecked(self):
        with patch.object(server.SESSION, 'get', side_effect=[
                discovery([{'identifier': 'album'}]), metadata('linked song'), response(),
                discovery([{'identifier': 'album'}]), response()]) as get:
            self.search('linked')
            r, summary = self.search('linked song')
        self.assertEqual(len(r.json['tracks']), 1)
        self.assertEqual(summary['cache_hits']['metadata'], 1)
        self.assertEqual(get.call_count, 5)
        server.METADATA_CACHE.put('denied', {'metadata': {'licenseurl': ''},
                                           'files': [{'name': 'linked.flac'}]}, 60)
        with patch.object(server.SESSION, 'get', return_value=discovery([{'identifier': 'denied'}])) as get:
            r, _ = self.search('linked! denied')
        self.assertEqual(r.json['tracks'], [])
        self.assertEqual(get.call_count, 1)

    def test_verified_search_expiry_revalidates_file_with_cached_metadata(self):
        with patch.object(server.time, 'monotonic', return_value=100):
            with patch.object(server.SESSION, 'get', side_effect=[
                    discovery([{'identifier': 'album'}]), metadata(), response()]):
                first, _ = self.search()
        with patch.object(server.time, 'monotonic', return_value=131):
            with patch.object(server.SESSION, 'get', side_effect=[
                    discovery([{'identifier': 'album'}]), response()]) as get:
                second, summary = self.search()
        self.assertEqual(first.json, second.json)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(summary['cache_hits'], {'search': 0, 'metadata': 1})
        self.assertIn('/download/', get.call_args.args[0])

    def test_upstream_failure_is_not_negative_cached(self):
        with patch.object(server.SESSION, 'get', side_effect=[requests.Timeout()] * 4) as get:
            for _ in range(2):
                r, _ = self.search()
                self.assertEqual(r.status_code, 504)
                self.assertEqual(r.json['errorType'], 'upstream_timeout')
        self.assertEqual(get.call_count, 4)

    def test_empty_result_has_short_cache_and_expires(self):
        with patch.object(server.SESSION, 'get', return_value=discovery([])) as get:
            with patch.object(server.time, 'monotonic', return_value=100):
                self.search()
                self.search()
            with patch.object(server.time, 'monotonic', return_value=111):
                self.search()
        self.assertEqual(get.call_count, 2)

    def test_metadata_cache_expiry_fetches_new_license(self):
        with patch.object(server.time, 'monotonic', return_value=100):
            server.METADATA_CACHE.put('album', {'metadata': {'licenseurl': LICENSE},
                                              'files': [{'name': 'linked.flac'}]}, 60)
        with patch.object(server.time, 'monotonic', return_value=161):
            with patch.object(server.SESSION, 'get', side_effect=[
                    discovery([{'identifier': 'album'}]), metadata(licenseurl='')]) as get:
                r, summary = self.search()
        self.assertEqual(r.json['tracks'], [])
        self.assertEqual(summary['cache_hits']['metadata'], 0)
        self.assertEqual(get.call_count, 2)

    def test_reserve_stops_further_metadata_and_is_not_cached(self):
        clock = [100.]
        def fetch(url, **kwargs):
            if url == server.IA_SEARCH:
                return discovery([{'identifier': 'one'}, {'identifier': 'two'}])
            clock[0] += 7
            return response({'metadata': {'licenseurl': LICENSE}, 'files': []})
        with patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]):
            with patch.object(server.SESSION, 'get', side_effect=fetch) as get:
                r, summary = self.search()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(r.status_code, 504)
        self.assertEqual(r.json['errorType'], 'budget_exhausted')
        self.assertEqual(summary['stop_reason'], 'budget_reserve')
        self.assertIsNone(server.SEARCH_CACHE.get(('linked', 20)))

    def test_stage_measurements_include_network_cost(self):
        clock = [100.]
        queue = [discovery([{'identifier': 'album'}]), metadata(), response()]
        costs = [.1, .3, .2]
        def fetch(*args, **kwargs):
            clock[0] += costs.pop(0)
            return queue.pop(0)
        with patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]):
            with patch.object(server.SESSION, 'get', side_effect=fetch):
                r, summary = self.search()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(summary['stage_ms'], {'initial_search': 100, 'candidate_filter': 0,
                                              'metadata': 300, 'flac_validation': 200})

    def test_caches_are_bounded_and_do_not_share_mutable_values(self):
        cache = server.TTLCache(2, 100)
        cache.put('one', [1], 60)
        cache.put('two', [2], 60)
        cached = cache.get('one')
        cached.append(9)
        self.assertEqual(cache.get('one'), [1])
        cache.put('three', [3], 60)
        self.assertIsNone(cache.get('two'))
        cache.put('huge', 'x' * 101, 60)
        self.assertIsNone(cache.get('huge'))
        self.assertEqual(len(cache.entries), 2)


if __name__ == '__main__':
    unittest.main()
