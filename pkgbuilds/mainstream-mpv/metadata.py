#!/usr/bin/env python3
"""Find TV episode details without modifying media files or sending local paths."""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError
import unicodedata
from urllib.parse import urlencode, urlparse, unquote
from urllib.request import Request, urlopen

API = 'https://api.tvmaze.com'
TTL = 30 * 86400
NEGATIVE_TTL = 6 * 3600
MAX_JSON = 2 * 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if c.isalnum())


def parse_filename(filename):
    name = Path(filename).name
    match = re.search(r'(?i)(?<![a-z0-9])s(\d{1,2})e(\d{1,3})(?!\d)', name)
    if not match or re.match(r'(?i)(?:e\d|[-+]e?\d)', name[match.end():]):
        return None
    show = name[:match.start()]
    show = re.sub(r'(?i)^(?:www\.)?[a-z0-9-]+\.(?:org|com|net)\s*[-_]+\s*', '', show)
    show = re.sub(r'^\[[^\]]+\]\s*', '', show)
    year_match = re.search(r'(?:[ ._(\[])(19\d\d|20\d\d)[)\]]?\s*[ ._-]*$', show)
    year = int(year_match[1]) if year_match else None
    if year_match:
        show = show[:year_match.start()]
    show = re.sub(r'[._]+', ' ', show).strip(' -[]()')
    season, episode = int(match[1]), int(match[2])
    if not show or len(show) > 160 or season < 1 or episode < 1:
        return None
    return {'show': show, 'year': year, 'season': season, 'episode': episode}


def fetch(url, limit):
    request = Request(url, headers={'User-Agent': 'mainstream-mpv-metadata/1.0', 'Accept': '*/*'})
    with urlopen(request, timeout=8) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('response exceeds size limit')
    return data


def fetch_json(url):
    return json.loads(fetch(url, MAX_JSON))


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.lookup-', delete=False) as file:
            name = file.name
            file.write(data)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def select_show(results, parsed):
    if not isinstance(results, list):
        return None
    matches = []
    for item in results:
        show = item.get('show', {}) if isinstance(item, dict) else {}
        if not isinstance(show, dict) or not isinstance(show.get('name'), str):
            continue
        if normalized(show['name']) != normalized(parsed['show']):
            continue
        if not isinstance(show.get('id'), int) or show['id'] <= 0:
            continue
        if parsed['year'] and str(show.get('premiered', ''))[:4] != str(parsed['year']):
            continue
        matches.append(show)
    return matches[0] if len(matches) == 1 else None


def image_url(record):
    image = record.get('image')
    if not isinstance(image, dict):
        return ''
    url = image.get('medium') or image.get('original')
    if not isinstance(url, str):
        return ''
    parts = urlparse(url)
    return url if parts.scheme == 'https' and parts.netloc == 'static.tvmaze.com' else ''


def cache_image(url, directory):
    if not url:
        return ''
    extension = Path(urlparse(url).path).suffix.lower()
    if extension not in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
        return ''
    file = directory / ('image-' + hashlib.sha256(url.encode()).hexdigest() + extension)
    if not file.is_file():
        data = fetch(url, MAX_IMAGE)
        if not data or not (data.startswith(b'\xff\xd8\xff') or data.startswith(b'\x89PNG\r\n\x1a\n')
                            or data.startswith((b'GIF87a', b'GIF89a'))
                            or (data.startswith(b'RIFF') and data[8:12] == b'WEBP')):
            raise ValueError('invalid artwork')
        atomic_write(file, data)
    return file.resolve().as_uri()


def lookup(parsed, directory, refresh=False, offline=False):
    key = hashlib.sha256(json.dumps(parsed, sort_keys=True).encode()).hexdigest()
    cached = directory / (key + '.json')
    if cached.is_file() and not refresh:
        try:
            saved = json.loads(cached.read_text())
            result = saved['result']
            if result is not None and not isinstance(result, dict):
                raise ValueError('invalid cache record')
            valid_age = TTL if result else NEGATIVE_TTL
            if offline or time.time() - saved['time'] < valid_age:
                if result and result.get('imageUrl') and (not result.get('artUrl') or not Path(unquote(urlparse(result['artUrl']).path)).is_file()):
                    # Retry missing/failed images without repeating the series lookup.
                    result = dict(result, artUrl='')
                    if not offline:
                        try:
                            result['artUrl'] = cache_image(result['imageUrl'], directory)
                        except (OSError, ValueError):
                            pass
                return result
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if offline:
        return None
    show = select_show(fetch_json(API + '/search/shows?' + urlencode({'q': parsed['show']})), parsed)
    result = None
    if show:
        try:
            episode = fetch_json(API + f"/shows/{show['id']}/episodebynumber?" + urlencode({'season': parsed['season'], 'number': parsed['episode']}))
        except HTTPError as error:
            if error.code != 404:
                raise
            episode = None
        if isinstance(episode, dict) and episode.get('season') == parsed['season'] and episode.get('number') == parsed['episode'] and isinstance(episode.get('name'), str) and episode['name']:
            source = episode.get('url', '')
            if not isinstance(source, str) or urlparse(source).netloc not in ['www.tvmaze.com', 'tvmaze.com']:
                source = f"https://www.tvmaze.com/shows/{show['id']}"
            image = image_url(episode) or image_url(show)
            art = ''
            try:
                art = cache_image(image, directory)
            except (OSError, ValueError):
                # Details remain useful if the image server is temporarily unavailable.
                pass
            summary = episode.get('summary') or ''
            summary = html.unescape(re.sub('<[^>]*>', '', summary))[:4096] if isinstance(summary, str) else ''
            result = {'series': show['name'][:256], 'title': episode['name'][:256],
                      'displayTitle': f"{show['name']} · S{parsed['season']:02}E{parsed['episode']:02} — {episode['name']}"[:1024],
                      'season': parsed['season'], 'episode': parsed['episode'], 'summary': summary,
                      'artUrl': art, 'imageUrl': image, 'source': 'TVmaze', 'sourceUrl': source}
    atomic_write(cached, json.dumps({'time': time.time(), 'result': result}, ensure_ascii=False).encode())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--filename', required=True)
    parser.add_argument('--cache-dir', type=Path, default=Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'mainstream-mpv/metadata-v1')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    parsed = parse_filename(args.filename)
    try:
        result = lookup(parsed, args.cache_dir, args.refresh, args.offline) if parsed else None
        print(json.dumps(result or {}, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('{}')
        print('Episode lookup unavailable: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
