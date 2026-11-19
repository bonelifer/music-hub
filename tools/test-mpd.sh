#!/usr/bin/bash
# Start or stop a throwaway MPD for testing music-hub.
#
# The MPD listens on 127.0.0.1:16601 with a null audio output, so tests can
# queue, play and save playlists without touching the real MPD, its queue,
# or the speakers. Point [mpd] port at 16601 in a test music-hub.cfg.
#
# Usage: tools/test-mpd.sh start|stop
set -Eeuo pipefail

name="music-hub-test-mpd"
image="giof71/mpd-alsa:latest"
here="$(cd "$(dirname "$0")" && pwd)"

case "${1:-}" in
    start)
        docker rm -f "$name" >/dev/null 2>&1 || true
        docker run -d --name "$name" --network host \
            --entrypoint sh \
            -v "$here/test-mpd.conf:/etc/test-mpd.conf:ro" \
            "$image" -c 'mkdir -p /tmp/mpd/playlists && exec mpd --no-daemon /etc/test-mpd.conf' \
            >/dev/null
        # Wait up to 10 seconds for MPD to answer.
        for _ in $(seq 1 20); do
            if printf 'close\n' | timeout 1 bash -c 'exec 3<>/dev/tcp/127.0.0.1/16601; head -1 <&3' 2>/dev/null | grep -q '^OK MPD'; then
                echo "Test MPD is up on 127.0.0.1:16601"
                exit 0
            fi
            sleep 0.5
        done
        echo "Test MPD did not start; see: docker logs $name" >&2
        exit 1
        ;;
    stop)
        docker rm -f "$name" >/dev/null 2>&1 || true
        echo "Test MPD removed"
        ;;
    *)
        echo "Usage: $0 start|stop" >&2
        exit 2
        ;;
esac
