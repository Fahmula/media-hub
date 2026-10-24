#!/usr/bin/env sh
# Build src/mediahub/static/dist/app.css with the Tailwind v4 standalone CLI and daisyUI 5.
# No Node.js required. Tools are downloaded once into ./bin and verified by SHA-256.
#
#   scripts/build-css.sh           # one-off minified build
#   scripts/build-css.sh --watch   # rebuild on template changes (development)
set -eu

TAILWIND_VERSION="4.3.3"
DAISYUI_VERSION="5.7.47"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/bin"
mkdir -p "$BIN"

case "$(uname -m)" in
  x86_64 | amd64) TW_ASSET="tailwindcss-linux-x64"
    TW_SHA="dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a" ;;
  aarch64 | arm64) TW_ASSET="tailwindcss-linux-arm64"
    TW_SHA="55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195" ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

fetch() { # url dest sha256
  if [ -f "$2" ] && echo "$3  $2" | sha256sum -c - >/dev/null 2>&1; then return 0; fi
  echo "downloading $1"
  curl -fsSL -o "$2.tmp" "$1"
  echo "$3  $2.tmp" | sha256sum -c - >/dev/null || { echo "checksum mismatch for $1" >&2; rm -f "$2.tmp"; exit 1; }
  mv "$2.tmp" "$2"
}

fetch "https://github.com/tailwindlabs/tailwindcss/releases/download/v$TAILWIND_VERSION/$TW_ASSET" \
  "$BIN/tailwindcss" "$TW_SHA"
chmod +x "$BIN/tailwindcss"
fetch "https://github.com/saadeghi/daisyui/releases/download/v$DAISYUI_VERSION/daisyui.mjs" \
  "$BIN/daisyui.mjs" "85819d3fe86a852237b439b13f481481aac58e562ac47694cd11c3038e994f2a"
fetch "https://github.com/saadeghi/daisyui/releases/download/v$DAISYUI_VERSION/daisyui-theme.mjs" \
  "$BIN/daisyui-theme.mjs" "d92d2f488933dab757f046e81ebd4986e51e9423307798dc3545d09a15542c89"

cd "$ROOT"
if [ "${1:-}" = "--watch" ]; then
  exec "$BIN/tailwindcss" -i src/mediahub/static/css/input.css -o src/mediahub/static/dist/app.css --watch
fi
"$BIN/tailwindcss" -i src/mediahub/static/css/input.css -o src/mediahub/static/dist/app.css --minify
