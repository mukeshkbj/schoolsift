#!/usr/bin/env bash
# Build the AgentCore direct-code-deploy zip (aarch64, Python 3.12).
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="dist/agentcore-package.zip"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

uv export --no-dev --no-hashes --no-emit-project -o "$TMP/requirements.txt"
uv pip install \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.12 \
  --target "$TMP/pkg" \
  --only-binary=:all: \
  -r "$TMP/requirements.txt"

cp -R backend/src/schoolsift "$TMP/pkg/schoolsift"
cp backend/agentcore_entry.py "$TMP/pkg/agentcore_entry.py"
chmod -R a+rX "$TMP/pkg"

mkdir -p dist
rm -f "$OUT"
(cd "$TMP/pkg" && zip -qr "$OLDPWD/$OUT" .)

du -h "$OUT"
echo "package dir: $TMP/pkg (removed on exit)"
