import io
import unittest
from unittest.mock import patch, Mock
import requests
import server


def response(data=None, audio=False):
    r = Mock()
    r.status_code = 200
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    r.json.return_value = data
    packed = (44100 << 44) | (15 << 36) | 441000
    r.raw = io.BytesIO(b'fLaC' + bytes([0, 0, 0, 34]) + bytes(10) + packed.to_bytes(8, 'big') + bytes(16))
    return r


class SearchTests(unittest.TestCase):
    def setUp(self):
        server.SEARCH_CACHE.clear()
        server.METADATA_CACHE.clear()
        self.client = server.app.test_client()

    def run_search(self, query):
        metadata = {'metadata': {'title': 'Unrelated album', 'creator': 'Real Artist',
                     'licenseurl': 'https://creativecommons.org/licenses/by/4.0/'},
                    'files': [{'name': query + '.flac', 'title': query, 'length': '99:59'},
                              {'name': 'different.flac', 'title': 'Different'}]}
        with patch.object(server.SESSION, 'get', side_effect=[
                response({'response': {'docs': [{'identifier': 'album'}]}}),
                response(metadata), response(audio=True)]) as get:
            r = self.client.get('/search', query_string={'q': query, 'quality': 'lossless', 'atmos': 'auto'})
        self.assertEqual(r.status_code, 200)
        track, = r.json['tracks']
        self.assertEqual(track['title'], query)
        self.assertEqual(track['artist'], 'Real Artist')
        self.assertEqual(track['duration'], 10)
        self.assertEqual(track['sampleRate'], 44100)
        self.assertTrue(track['id'].endswith('::' + query + '.flac'))
        self.assertIn('timeout', get.call_args.kwargs)

    def test_classical(self):
        self.run_search('classical')

    def test_english(self):
        self.run_search("A Song's Dream")

    def test_unicode(self):
        self.run_search('月亮代表我的心')
        self.assertEqual(server.normalized('ＡＢＣ—月亮！'), 'abc 月亮')

    def test_empty(self):
        with patch.object(server.SESSION, 'get', return_value=response({'response': {'docs': []}})):
            self.assertEqual(self.client.get('/search?q=missing').json, {'tracks': []})

    def test_timeout(self):
        with patch.object(server.SESSION, 'get', side_effect=requests.Timeout('secret')) as get:
            r = self.client.get('/search?q=song')
        self.assertEqual(r.status_code, 504)
        self.assertEqual(get.call_count, 2)
        self.assertNotIn('secret', r.text)

    def test_license_required(self):
        self.assertFalse(server.licensed({}))
        self.assertFalse(server.licensed({'licenseurl': 'https://creativecommons.org.evil/licenses/by/4.0/'}))

    def test_fake_flac(self):
        r = response()
        r.raw = io.BytesIO(b'not flac')
        with patch.object(server.SESSION, 'get', return_value=r):
            self.assertIsNone(server.flac_info('https://archive.org/file', server.time.monotonic() + 12))

    def test_album_does_not_match_arbitrary_file(self):
        with patch.object(server.SESSION, 'get', side_effect=[
            response({'response': {'docs': [{'identifier': 'album'}]}}),
            response({'metadata': {'title': 'Wanted Song', 'licenseurl': 'https://creativecommons.org/publicdomain/zero/1.0/'},
                      'files': [{'name': 'Other.flac'}]})]) as get:
            self.assertEqual(self.client.get('/search?q=Wanted+Song').json['tracks'], [])
            self.assertEqual(get.call_count, 2)

    def test_stream_exact_file(self):
        with patch.object(server, 'get_flac', return_value=None) as get:
            self.assertEqual(self.client.get('/stream', query_string={'id': 'album::song.flac'}).status_code, 404)
            get.assert_called_once_with('album', 'song.flac')

    def test_classical_subject_keeps_real_track_title(self):
        with patch.object(server.SESSION, 'get', side_effect=[
            response({'response': {'docs': [{'identifier': 'album'}]}}),
            response({'metadata': {'creator': 'Composer', 'subject': ['piano', 'classical'],
                      'licenseurl': 'https://creativecommons.org/licenses/by/4.0/'},
                      'files': [{'name': '01-Sonata.flac', 'title': 'Sonata'}]}),
            response(audio=True)]):
            with self.assertLogs(server.app.logger, level='INFO') as logs:
                r = self.client.get('/search?q=classical')
        self.assertEqual(r.json['tracks'][0]['title'], 'Sonata')
        self.assertIn('"returned": 1', logs.output[-1])
        self.assertIn('elapsed_ms', logs.output[-1])

    def test_deadline_interrupts_slow_upstream(self):
        import time
        with patch.object(server, 'SEARCH_SECONDS', .05), patch.object(
                server.SESSION, 'get', side_effect=lambda *a, **kw: time.sleep(.5)):
            start = time.monotonic()
            r = self.client.get('/search?q=slow')
        self.assertEqual(r.status_code, 504)
        self.assertEqual(r.json["errorType"], "budget_exhausted")
        self.assertLess(time.monotonic() - start, .3)

    def test_permanent_error_not_retried(self):
        r = response()
        r.status_code = 404
        r.raise_for_status.side_effect = requests.HTTPError(response=r)
        with patch.object(server.SESSION, 'get', return_value=r) as get:
            with self.assertRaises(requests.HTTPError):
                server.upstream('https://archive.org/missing', server.time.monotonic() + 12)
        self.assertEqual(get.call_count, 1)
        r.close.assert_called_once()

    def test_routes(self):
        for url in ('/', '/manifest.json', '/health'):
            self.assertEqual(self.client.get(url).status_code, 200)

if __name__ == '__main__':
    unittest.main()
