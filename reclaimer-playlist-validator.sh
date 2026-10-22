#!/bin/sh
set -u

ROOT=/server/.admin-playlist-validation
REQUESTS="$ROOT/requests"
RESULTS="$ROOT/results"
mkdir -p "$REQUESTS" "$RESULTS"

CATALOG="$ROOT/catalog.txt"

# Rebuild the content catalog for the admin UI's map and game dropdowns when it
# is missing, when content/ changes, or when the UI asks via catalog-refresh.
refresh_catalog() {
  if [ -f "$CATALOG" ] && [ ! -f "$ROOT/catalog-refresh" ] &&
     ! find /server/content/Maps "/server/content/Game Modes" /server/content/Mods \
       -maxdepth 1 -newer "$CATALOG" -print 2>/dev/null | grep -q .; then
    return
  fi
  rm -f "$ROOT/catalog-refresh"
  (cd /server && dedicated content) > "$ROOT/catalog.tmp" 2>&1
  sed -i 's/error: XDG_RUNTIME_DIR is invalid or not set in the environment\.//Ig' "$ROOT/catalog.tmp"
  if grep -q '^Game types:' "$ROOT/catalog.tmp"; then
    mv "$ROOT/catalog.tmp" "$CATALOG"
  else
    mv "$ROOT/catalog.tmp" "$ROOT/catalog-error.txt"
    touch "$CATALOG"
  fi
}

while :; do
  refresh_catalog
  date +%s > "$ROOT/worker-ready.tmp"
  mv "$ROOT/worker-ready.tmp" "$ROOT/worker-ready"

  for request in "$REQUESTS"/*.txt; do
    [ -f "$request" ] || continue
    request_id=${request##*/}
    request_id=${request_id%.txt}
    work="$REQUESTS/$request_id.work"
    mv "$request" "$work" 2>/dev/null || continue
    playlist=$(cat "$work")
    case "$playlist" in
      admin-validation-[A-Fa-f0-9]*.playlist.json) ;;
      *)
        printf '1\nInvalid validation request filename' > "$RESULTS/$request_id.tmp"
        mv "$RESULTS/$request_id.tmp" "$RESULTS/$request_id.txt"
        rm -f "$work"
        continue
        ;;
    esac

    output="$RESULTS/$request_id.output"
    result="$RESULTS/$request_id.tmp"
    if (cd /server && dedicated playlist show "$playlist") > "$output" 2>&1; then
      code=0
    else
      code=$?
    fi
    # The dedicated wrapper may print this harmless headless-session warning
    # even after it has successfully described and validated the playlist.
    sed -i 's/error: XDG_RUNTIME_DIR is invalid or not set in the environment\.//Ig' "$output"
    # `playlist show` prints this confirmation after checking every game. Some
    # image builds return a nonzero shell status because the display helper is
    # unavailable, despite Reclaimer having completed playlist validation.
    if grep -Fqi 'Every game is installed.' "$output"; then
      code=0
    fi
    if grep -Eqi '^Error:|not found|did you mean|cannot be played' "$output"; then
      code=1
    fi
    {
      printf '%s\n' "$code"
      cat "$output"
    } > "$result"
    mv "$result" "$RESULTS/$request_id.txt"
    rm -f "$output" "$work"
  done
  sleep 1
done
