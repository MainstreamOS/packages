import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

helper = Path(__file__).resolve().parents[1] / 'metadata.py'
if not helper.exists():
    helper = Path(__file__).resolve().parent / 'metadata.py'
spec = importlib.util.spec_from_file_location('metadata', helper)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MetadataTests(unittest.TestCase):
    def test_release_filename(self):
        result = m.parse_filename('VisionQuest S01E01 No Strings 2160p DSNP WEB-DL.mkv')
        self.assertEqual(result, {'show': 'VisionQuest', 'year': None, 'season': 1, 'episode': 1})

    def test_dots_year_and_prefix(self):
        result = m.parse_filename('www.UIndex.org - [Group] The.Show.(2024).S02E03.1080p.mkv')
        self.assertEqual(result, {'show': 'The Show', 'year': 2024, 'season': 2, 'episode': 3})

    def test_no_episode_or_multiple_episodes_are_not_guessed(self):
        for name in ['Home video.mp4', 'Movie.2024.mkv', 'Show.S01E01E02.mkv', 'Show.S01E01-E02.mkv', 'Show.S00E01.mkv', 'Show.S01E00.mkv']:
            self.assertIsNone(m.parse_filename(name), name)

    def test_match_requires_exact_normalized_name(self):
        query = m.parse_filename('The.Show.S01E01.mkv')
        self.assertIsNone(m.select_show([{'show': {'id': 1, 'name': 'The Other Show'}}], query))
        show = {'id': 2, 'name': 'The Show'}
        self.assertEqual(m.select_show([{'show': show}], query), show)

    def test_ambiguous_remakes_need_a_year(self):
        shows = [{'show': {'id': n, 'name': 'The Show', 'premiered': f'{year}-01-01'}} for n, year in [(1, 2001), (2, 2024)]]
        self.assertIsNone(m.select_show(shows, m.parse_filename('The.Show.S01E01.mkv')))
        self.assertEqual(m.select_show(shows, m.parse_filename('The.Show.2024.S01E01.mkv'))['id'], 2)

    def test_image_links_are_provider_https_urls(self):
        for url in ['file:///etc/passwd', 'https://example.com/image.jpg', 'http://static.tvmaze.com/image.jpg']:
            self.assertEqual(m.image_url({'image': {'medium': url}}), '')
        self.assertEqual(m.image_url({'image': None}), '')

    def test_cache_image_validates_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(m, 'fetch', return_value=b'<html>failure</html>'):
                with self.assertRaises(ValueError):
                    m.cache_image('https://static.tvmaze.com/image.jpg', Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_success_cache_and_offline_reuse(self):
        with tempfile.TemporaryDirectory(prefix='mpv cache ') as directory:
            root = Path(directory)
            query = m.parse_filename('VisionQuest.S01E01.mkv')
            records = [[{'show': {'id': 12, 'name': 'VisionQuest'}}], {'season': 1, 'number': 1, 'name': 'No Strings', 'summary': '<p>A &amp; B</p>', 'url': 'https://www.tvmaze.com/episodes/1', 'image': {'medium': 'https://static.tvmaze.com/image.jpg'}}]
            with patch.object(m, 'fetch_json', side_effect=records) as api, patch.object(m, 'fetch', return_value=b'\xff\xd8\xfftest'):
                result = m.lookup(query, root)
                self.assertEqual(api.call_count, 2)
            self.assertEqual(result['displayTitle'], 'VisionQuest · S01E01 — No Strings')
            self.assertEqual(result['summary'], 'A & B')
            with patch.object(m, 'fetch_json', side_effect=AssertionError('network must not be used')):
                self.assertEqual(m.lookup(query, root, offline=True), result)
                self.assertEqual(m.lookup(query, root), result)
            self.assertIn('%20', result['artUrl'])

    def test_negative_cache_prevents_repeated_requests(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'fetch_json', return_value=[]) as api:
            query = m.parse_filename('Missing.S01E01.mkv')
            self.assertIsNone(m.lookup(query, Path(directory)))
            self.assertIsNone(m.lookup(query, Path(directory)))
            self.assertEqual(api.call_count, 1)

    def test_wrong_episode_is_not_used(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'fetch_json', side_effect=[[{'show': {'id': 12, 'name': 'Show'}}], {'season': 2, 'number': 1, 'name': 'Wrong'}]):
            self.assertIsNone(m.lookup(m.parse_filename('Show.S01E01.mkv'), Path(directory)))

    def test_image_failure_keeps_useful_details(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'fetch_json', side_effect=[[{'show': {'id': 12, 'name': 'Show'}}], {'season': 1, 'number': 1, 'name': 'Episode', 'image': {'medium': 'https://static.tvmaze.com/image.jpg'}}]), patch.object(m, 'fetch', side_effect=OSError('offline')):
            self.assertEqual(m.lookup(m.parse_filename('Show.S01E01.mkv'), Path(directory))['title'], 'Episode')

    def test_network_errors_are_not_cached_as_missing_shows(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'fetch_json', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                m.lookup(m.parse_filename('Show.S01E01.mkv'), Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_missing_image_is_refetched_from_valid_cached_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            query=m.parse_filename('Show.S01E01.mkv')
            records=[[{'show': {'id': 12, 'name': 'Show'}}], {'season': 1, 'number': 1, 'name': 'Episode', 'image': {'medium': 'https://static.tvmaze.com/image.jpg'}}]
            with patch.object(m, 'fetch_json', side_effect=records), patch.object(m, 'fetch', return_value=b'\xff\xd8\xfftest'):
                m.lookup(query, root)
            next(root.glob('*.jpg')).unlink()
            with patch.object(m, 'fetch_json', side_effect=AssertionError('details should be cached')), patch.object(m, 'fetch', return_value=b'\xff\xd8\xfftest') as image:
                self.assertTrue(m.lookup(query, root)['artUrl'])
                image.assert_called_once()


if __name__ == '__main__':
    unittest.main()
