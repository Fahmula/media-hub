#!/usr/bin/env sh
# Regenerate the app icons in src/mediahub/static/img/ from favicon.svg and icon-512.png.
# Needs rsvg-convert (librsvg) and ImageMagick 7 (magick). The PNGs are committed, so this only
# runs when the logo changes.
#
#   icon-192.png                  icon-512.png scaled down ("any" purpose in the manifest)
#   icon-maskable-512.png         glyph inside the 80% safe zone on the app background
#   apple-touch-icon.png          180x180, opaque (iOS fills transparency with black)
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMG="$ROOT/src/mediahub/static/img"
BG="#0b1220"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# canvas glyph-size output
on_background() {
  rsvg-convert -w "$2" -h "$2" "$IMG/favicon.svg" -o "$TMP/glyph.png"
  magick -size "$1x$1" "xc:$BG" "$TMP/glyph.png" -gravity center -composite \
    -strip "PNG32:$3"
}

magick "$IMG/icon-512.png" -resize 192x192 -strip "PNG32:$IMG/icon-192.png"
on_background 512 300 "$IMG/icon-maskable-512.png"
on_background 180 124 "$IMG/apple-touch-icon.png"
echo "icons written to $IMG"
