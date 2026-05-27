#!/usr/bin/env bash
# Package lingo-async for AWS Lambda deployment.
#
# Output: dist/lingo-async.zip — contains app/ plus the runtime deps from
# requirements-lambda.txt. No FastAPI / Mangum — this is an SQS-triggered
# worker with no HTTP surface.
#
# Targets ARM64 / py3.13 — the Lambda runs on Graviton2 for ~20% cost
# savings over x86. If you change the Lambda arch in Terraform, update
# the --platform tag below to match.
#
# Optional: -f <LAMBDA_NAME> pushes the zip after building.

set -e
cd "$(dirname "$0")/.."
ROOT=$(pwd)
DIST_DIR="$ROOT/dist"
ZIP_NAME="lingo-async.zip"
OUT="$DIST_DIR/$ZIP_NAME"
BUILD_DIR="$ROOT/build"
LAMBDA_ARN=""

while getopts "f:" opt; do
  case $opt in
    f) LAMBDA_ARN="$OPTARG" ;;
    *) echo "Usage: $0 [-f LAMBDA_NAME_OR_ARN]" >&2; exit 1 ;;
  esac
done

echo "Building $OUT ..."

# Fresh build dir / dist dir.
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

# Install runtime deps targeted at the Lambda arch.
# --platform/--python-version/--only-binary=:all: forces pip to pull the
# manylinux2014_aarch64 wheels even when running on an x86 dev box, so
# the bundle is binary-compatible with Lambda's Graviton runtime.
pip install \
  --target "$BUILD_DIR" \
  --platform manylinux2014_aarch64 \
  --python-version 3.13 \
  --only-binary=:all: \
  --quiet \
  -r requirements-lambda.txt

# Copy the application code in alongside the deps.
cp -r app "$BUILD_DIR/"

# Zip from inside build/ so paths don't have a leading "build/" prefix —
# Lambda extracts the zip at the root of /var/task.
( cd "$BUILD_DIR" && zip -rq "$OUT" . \
    -x "*.pyc" -x "*__pycache__*" \
    -x "*.dist-info/*" -x "*.egg-info/*" \
    -x "*/tests/*" -x "*/test/*" )

# Tidy up.
rm -rf "$BUILD_DIR"

# Report size + memory hint.
SIZE_BYTES=$(stat -c%s "$OUT" 2>/dev/null || stat -f%z "$OUT")
SIZE_MB=$(awk -v b="$SIZE_BYTES" 'BEGIN { printf "%.1f", b/1024/1024 }')
echo "Done: $OUT (${SIZE_MB} MB)"
echo "Recommended Lambda memory: 512 MB (matches lingo-core / lingo-ops)."

if [ -n "$LAMBDA_ARN" ]; then
  echo "Pushing to Lambda: $LAMBDA_ARN"
  aws lambda update-function-code \
    --function-name "$LAMBDA_ARN" \
    --zip-file "fileb://$OUT"
  echo "Lambda updated."
fi
