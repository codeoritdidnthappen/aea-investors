#!/usr/bin/env sh
# Seed synthetic patient/identity fixtures for the local demo topology. Output is
# offline, evaluation-only data (never loaded into a runtime path) and lands under
# generated-fixtures/, which .gitignore already excludes.
#
# Usage: ./deploy/local/seed.sh [seed] [count]
set -eu

cd "$(dirname "$0")/../.."

SEED="${1:-local-demo}"
COUNT="${2:-3}"
OUTPUT="generated-fixtures/${SEED}"

uv run --locked python -m ai_server.fixtures.generator \
  --seed "${SEED}" \
  --count "${COUNT}" \
  --output "${OUTPUT}"

echo "Wrote ${COUNT} synthetic identities to ${OUTPUT}"
