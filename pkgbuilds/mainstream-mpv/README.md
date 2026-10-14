# Automatic TV episode metadata for mpv

The companion script recognizes local filenames containing `S01E01`, searches
TVmaze for an exact series-name match, and requests that episode. A filename year
resolves remakes; ambiguous matches and multi-episode files retain their original
metadata. Only the cleaned series name and episode numbers leave the computer.
No media file is rewritten.

The lookup runs in a subprocess and never delays playback. New files cancel
pending work, and both the Lua script and native plugin reject results for a
previous file. Embedded titles, custom titles, artist/album tags and existing
artwork retain priority. Results and images are cached under
`$XDG_CACHE_HOME/mainstream-mpv/metadata-v1` (normally `~/.cache`). Successful
matches are reused for 30 days; unmatched titles for six hours. Cached details
survive unavailable artwork. The current feature covers TV episodes, not movies.

TVmaze supplies the data and images: https://www.tvmaze.com/api . Its source name
and episode URL are included in MPRIS metadata and briefly shown in mpv's OSD.
TVmaze's API documentation describes the attribution and ShareAlike terms for
its data. The helper and integration code are MIT licensed.

The small `mpv-mpris-mainstream` patch reads optional
`user-data/mainstream-metadata` fields, validates the current media path, fills
missing MPRIS fields and announces updates. Network operations remain in the
companion helper; the native playback plugin contains no provider API calls.

To disable online lookup, create
`~/.config/mpv/script-opts/mainstream-metadata.conf` with:

```ini
enabled=no
```

`show_credit=no` hides the brief OSD credit; source attribution remains in MPRIS
metadata. Refresh the current episode through mpv's command console with
`script-message-to mainstream-metadata metadata-refresh`.

Run regression checks with:

```sh
python3 -m unittest discover -s . -p 'test_*.py'
lua test_lifecycle.lua mainstream-metadata.lua
```

The package replaces the stock `mpv-mpris` dependency with
`mpv-mpris-mainstream`, which provides the same MPRIS integration plus the optional
metadata hook. Restart mpv after updating native plugins.
