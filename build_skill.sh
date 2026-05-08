#!/usr/bin/env bash
# Build roomcomm-skill.tar.gz from skill/ for distribution.
# Output structure inside the tarball:
#   roomcomm/
#   ├── SKILL.md
#   └── scripts/roomcomm.py
set -euo pipefail
cd "$(dirname "$0")"
rm -f roomcomm-skill.tar.gz
tar czf roomcomm-skill.tar.gz \
    --transform 's,^skill,roomcomm,' \
    skill/SKILL.md skill/scripts/roomcomm.py
echo "built: $(pwd)/roomcomm-skill.tar.gz"
tar tzf roomcomm-skill.tar.gz
