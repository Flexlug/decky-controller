#!/usr/bin/env bash
# Build out/decky-controller.zip for "Install from zip" in Decky Loader (Developer menu).
#
# Zip layout (what Decky Loader expects - see docs/DEV.md "Packaging"):
#   decky-controller/
#     dist/index.js  main.py  plugin.json  package.json  py_modules/  LICENSE  README.md  THIRD_PARTY_NOTICES.md
#
# Deliberately NOT shipped: dist/index.js.map (1.4 MB source map), node_modules/, tests/, docs/,
# notes/ (local drafts and spikes), __pycache__/ and any other working files - only the files listed above are copied.
#
# Usage: scripts/build-zip.sh [--no-build]   (run from anywhere)
#   --no-build  skip the frontend build and package the existing dist/index.js (CI builds it in a previous step)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="decky-controller"
OUT_DIR="$ROOT/out"
STAGE="$OUT_DIR/$NAME"
ZIP="$OUT_DIR/$NAME.zip"
BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cd "$ROOT"

# --- frontend -----------------------------------------------------------------
# pnpm is the package manager (package.json "packageManager"); fall back to corepack when pnpm is not on PATH.
pnpm_cmd() {
  if command -v pnpm >/dev/null 2>&1; then pnpm "$@"
  elif command -v corepack >/dev/null 2>&1; then corepack pnpm "$@"
  else die "pnpm not found - install it (https://pnpm.io/installation) or enable corepack"
  fi
}
if [ "$BUILD" = 1 ]; then
  if [ ! -d node_modules ]; then
    echo ">> pnpm install --frozen-lockfile"
    pnpm_cmd install --frozen-lockfile
  fi
  echo ">> pnpm run build"
  pnpm_cmd run build
fi
[ -f dist/index.js ] || die "dist/index.js missing - run 'pnpm run build' first"

# --- stage --------------------------------------------------------------------
rm -rf "$STAGE" "$ZIP"
mkdir -p "$STAGE/dist"
# Only the bundle ships (the source map stays local).
cp dist/index.js "$STAGE/dist/"
cp plugin.json package.json LICENSE "$STAGE/"
[ -f THIRD_PARTY_NOTICES.md ] && cp THIRD_PARTY_NOTICES.md "$STAGE/"

# Parts produced by other parts of the project; tolerate their absence (warn, not fail).
if [ -f main.py ]; then
  cp main.py "$STAGE/"
else
  warn "main.py not found - the zip will have no backend"
fi
if [ -d py_modules ]; then
  mkdir -p "$STAGE/py_modules"
  # Recursive copy excluding bytecode caches (tar is always available on SteamOS/dev hosts).
  tar -C py_modules --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' -cf - . \
    | tar -xf - -C "$STAGE/py_modules"
else
  warn "py_modules/ not found - the zip will have no daemon"
fi
if [ -f README.md ]; then
  cp README.md "$STAGE/"
else
  warn "README.md not found"
fi

# --- zip ----------------------------------------------------------------------
if command -v zip >/dev/null 2>&1; then
  (cd "$OUT_DIR" && zip -qr "$NAME.zip" "$NAME")
else
  # Fallback without the zip binary (stdlib only).
  python3 - "$OUT_DIR" "$NAME" <<'PY'
import os, sys, zipfile
out_dir, name = sys.argv[1], sys.argv[2]
root = os.path.join(out_dir, name)
with zipfile.ZipFile(os.path.join(out_dir, name + ".zip"), "w", zipfile.ZIP_DEFLATED) as zf:
    for dirpath, _dirs, files in sorted(os.walk(root)):
        for f in sorted(files):
            full = os.path.join(dirpath, f)
            zf.write(full, os.path.relpath(full, out_dir))
PY
fi

# --- sanity: nothing that must stay local slipped in ---------------------------
python3 - "$ZIP" <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
bad = [n for n in names if n.endswith(".map") or "/notes/" in n or "/__pycache__/" in n
       or "/node_modules/" in n or "/assets/" in n]
if bad:
    print("ERROR: unexpected files in zip: " + ", ".join(bad), file=sys.stderr)
    sys.exit(1)
if "decky-controller/dist/index.js" not in names or "decky-controller/plugin.json" not in names:
    print("ERROR: zip is missing dist/index.js or plugin.json", file=sys.stderr)
    sys.exit(1)
PY

echo ">> $ZIP"
(cd "$OUT_DIR" && unzip -l "$NAME.zip" 2>/dev/null || python3 -c "import zipfile,sys; [print(i.filename) for i in zipfile.ZipFile(sys.argv[1]).infolist()]" "$ZIP")
