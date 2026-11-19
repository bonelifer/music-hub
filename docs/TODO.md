# TODO

## Next services

- **Pandora:** stations, thumbs up and down, skip, and station management
  through mympd-pandora's `stream.py` bridge. It needs controls while a song
  plays, not just a list to pick from.
- **Listening tools and music-librarian:** listening history and the
  never-scrobbled mixes from mpd-listening-tools, and links to
  music-librarian's genre, tag and lyrics pages.

## Bridges

- **iBroadcast album order:** the mympd-ibroadcast bridge lists an album's
  tracks in upload order, not track order (Abbey Road starts at "Oh!
  Darling"). myMPD and music-hub both get the bridge's order, so the fix
  belongs in the bridge.

## Browser player

- **HLS streams:** none of the current TuneIn or iHeartRadio favorites use
  HLS (`.m3u8`), which Chrome and Firefox cannot play with `<audio>` alone.
  If one does later, load a pinned copy of hls.js for that stream only.
- **Web Audio:** crossfade, gapless playback and volume levelling for
  iBroadcast albums.
