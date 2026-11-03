#!/usr/bin/env bash
# Fail when a built front-end bundle carries server-side secret material.
#
#   scripts/check_bundle_secrets.sh [storefront/dist]
#
# Scans every text file under the directory (source maps excluded) for the
# names of golden's deploy secrets and for well-known key shapes. Exits 1 and
# prints "<file>: <pattern>" per hit; 0 when clean; 2 when the directory is
# missing (build it first: `cd storefront && bun run build`).
#
# A Supabase *anon* key and VITE_* values are public by design and are not
# flagged; the service-role key name and every server-only key name are.
# Mirrors KEY_PATTERNS in core/tests/test_m3_secrets_headers.py (S7).
set -u

dir="${1:-storefront/dist}"
if [ ! -d "$dir" ]; then
  echo "check_bundle_secrets: $dir does not exist (run the storefront build first)" >&2
  exit 2
fi

patterns=(
  'SUPABASE_SERVICE_ROLE'
  'SERVICE_ROLE_KEY'
  'DJANGO_SECRET_KEY'
  'API_JWT_SIGNING_KEY'
  'GOLDEN_SECRET_KEYS'
  'LLMHUB_FERNET_KEY'
  'ANTHROPIC_API_KEY'
  'OPENAI_API_KEY'
  'MAILGUN_API_KEY'
  'sk-ant-[A-Za-z0-9_-]{8,}'
  'sk-proj-[A-Za-z0-9_-]{8,}'
  'sk_(live|test)_[A-Za-z0-9]{8,}'
  'AKIA[0-9A-Z]{16}'
  'xox[bap]-[A-Za-z0-9-]+'
  'ghp_[A-Za-z0-9]{10,}'
  '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
)

status=0
while IFS= read -r -d '' file; do
  case "$file" in *.map) continue ;; esac
  # Binary files (images, fonts) are skipped by grep -I.
  for pattern in "${patterns[@]}"; do
    if grep -IqE -- "$pattern" "$file"; then
      echo "$file: $pattern"
      status=1
    fi
  done
done < <(find "$dir" -type f -print0)

if [ "$status" -eq 0 ]; then
  echo "check_bundle_secrets: $dir is clean"
fi
exit "$status"
