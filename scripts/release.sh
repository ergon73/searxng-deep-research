#!/usr/bin/env bash
# release.sh — thin wrapper around release_packaging.build_release()
# Usage: ./scripts/release.sh [output_path]
# Default output: ./release-<UTC-date>.tar.gz

set -euo pipefail

# Default output: ./release-<YYYY-MM-DD>.tar.gz
OUTPUT="${1:-./release-$(date -u +%Y-%m-%d).tar.gz}"

# Ensure we're in repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Python with PYTHONPATH (so release_packaging + redact are importable)
export PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}"

# Run packer
python3 - <<EOF
import sys
from pathlib import Path
from release_packaging import ReleaseConfig, build_release, quick_verify

cfg = ReleaseConfig(
    root=Path("$REPO_ROOT"),
    output=Path("$OUTPUT").resolve(),
    redact_secrets=True,
)

result = quick_verify(cfg)
if result["verified"]:
    manifest = result["manifest"]
    print(f"OK: {manifest['tar_path']}")
    print(f"    sha256: {manifest['sha256']}")
    print(f"    size:   {manifest['size_bytes']:,} bytes")
    print(f"    files:  {manifest['file_count']}")
    print(f"    sidecar: {manifest['tar_path']}.sha256")
    sys.exit(0)
else:
    print(f"FAIL: {result['errors']}", file=sys.stderr)
    sys.exit(1)
EOF
