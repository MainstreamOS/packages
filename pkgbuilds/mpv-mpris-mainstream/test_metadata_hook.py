#!/usr/bin/env python3
"""Run with dbus-run-session; test native enrichment without network or desktop players."""
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

plugin = str(Path(sys.argv[1]).resolve())

def eventually(function, message):
    for _ in range(100):
        try:
            value = function()
            if value:
                return value
        except (subprocess.CalledProcessError, IndexError):
            pass
        time.sleep(.05)
    raise AssertionError(message)

with tempfile.TemporaryDirectory(prefix='mpv metadata ') as temporary:
    root = Path(temporary)
    clips = root / 'clips'
    clips.mkdir()
    blank, tagged, art = clips / 'Show.S01E01.mkv', clips / 'Show.S01E02.mkv', root / 'cover.png'
    for file, tags in [(blank, []), (tagged, ['-metadata', 'title=Embedded title', '-metadata', 'artist=Embedded artist', '-metadata', 'album=Embedded album'])]:
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=16x16:d=1', '-c:v', 'ffv1', *tags, str(file)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=16x16', '-frames:v', '1', '-threads', '1', str(art)], check=True)
    helper_art = root / 'helper.png'
    shutil.copy2(art, helper_art)
    ipc_path = root / 'mpv.sock'
    with (root / 'mpv.log').open('w') as log:
        process = subprocess.Popen(['mpv', '--no-config', '--load-scripts=no', '--script=' + plugin,
            '--idle=yes', '--pause', '--vo=null', '--ao=null', '--keep-open=yes',
            '--input-ipc-server=' + str(ipc_path), str(blank)], stdout=log, stderr=subprocess.STDOUT)
        try:
            eventually(ipc_path.exists, 'mpv IPC did not start')
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(5)
            client.connect(str(ipc_path))
            stream = client.makefile('rwb', buffering=0)
            sequence = 0
            def ipc(*command):
                global sequence
                sequence += 1
                stream.write(json.dumps({'command': command, 'request_id': sequence}).encode() + b'\n')
                while True:
                    result = json.loads(stream.readline())
                    if result.get('request_id') == sequence:
                        assert result['error'] == 'success', result
                        return result.get('data')
            def metadata():
                return subprocess.check_output(['playerctl', '-p', 'mpv', 'metadata'], text=True, stderr=subprocess.DEVNULL)
            eventually(lambda: 'Show.S01E01' in metadata(), 'initial source missing')
            payload = dict(path='/wrong/file.mkv', displayTitle='Online title', series='Online series',
                           artUrl=helper_art.as_uri(), source='TVmaze', sourceUrl='https://www.tvmaze.com/episodes/1')
            ipc('set_property', 'user-data/mainstream-metadata', payload)
            time.sleep(.15)
            assert 'Online title' not in metadata(), 'stale result applied to wrong file'
            payload['path'] = str(blank)
            ipc('set_property', 'user-data/mainstream-metadata', payload)
            enriched = eventually(lambda: (m if 'Online title' in (m := metadata()) else False), 'asynchronous enrichment missing')
            assert helper_art.as_uri() in enriched and 'Online series' in enriched
            ipc('set_property', 'force-media-title', 'Custom title')
            eventually(lambda: 'Custom title' in metadata(), 'custom title overwritten')
            ipc('set_property', 'force-media-title', '')
            ipc('set_property', 'cover-art-files', [str(art)])
            ipc('loadfile', str(tagged))
            eventually(lambda: 'Embedded title' in metadata(), 'tagged source missing')
            payload['path'] = str(tagged)
            ipc('set_property', 'user-data/mainstream-metadata', payload)
            time.sleep(.15)
            existing = metadata()
            for value in ['Online title', 'Embedded artist', 'Embedded album', art.as_uri()]:
                assert value in existing, (value, existing)
            assert 'Embedded title' not in existing and helper_art.as_uri() not in existing
            ipc('set_property', 'user-data/mainstream-metadata', {})
            eventually(lambda: 'mpv:metadataSource' not in metadata() and 'Embedded title' in metadata(), 'original metadata did not return after clearing lookup')
            print('Native MPRIS checks passed: live updates, wrong-file rejection, embedded-title replacement, custom-title and other tag/artwork preservation, cleanup.')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
