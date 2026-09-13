#!/usr/bin/env bash
# One-time: create the GitHub repository and push this repo to it.
#
# The token is read with `read -s`, so it is never echoed to the terminal, never passed as
# an argument (arguments are visible in `ps`), and never written outside this repository.
# It is stored only in .git/.git-credentials, which git created for this repo alone --
# your global git config and login keychain are untouched.
#
# Create a token first at https://github.com/settings/tokens
#   Fine-grained: "Administration: read and write" + "Contents: read and write"
#   Classic:      the `repo` scope
#
# Usage: scripts/github_setup.sh [repo-name] [public|private]
set -euo pipefail

repo_name="${1:-whatsapp-gemini-bot}"
visibility="${2:-public}"
owner="geek957"

case "$visibility" in
public | private) ;;
*)
  echo "visibility must be public or private, got: $visibility" >&2
  exit 2
  ;;
esac

cd "$(dirname "$0")/.."
git rev-parse --git-dir >/dev/null

printf 'GitHub personal access token for %s (input hidden): ' "$owner" >&2
read -rs token
printf '\n' >&2
[ -n "$token" ] || {
  echo "no token entered" >&2
  exit 1
}

api() {
  curl -sS -o /tmp/gh_setup_body.$$ -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$@"
}

echo "==> verifying token"
status="$(api https://api.github.com/user)"
if [ "$status" != "200" ]; then
  echo "token check failed (HTTP $status):" >&2
  head -c 300 /tmp/gh_setup_body.$$ >&2
  rm -f /tmp/gh_setup_body.$$
  exit 1
fi
login="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["login"])' /tmp/gh_setup_body.$$)"
echo "    authenticated as ${login}"
if [ "$login" != "$owner" ]; then
  echo "    WARNING: expected ${owner}; continuing with ${login}" >&2
  owner="$login"
fi

echo "==> creating ${owner}/${repo_name} (${visibility})"
private=false
[ "$visibility" = "private" ] && private=true
status="$(api -X POST https://api.github.com/user/repos \
  -d "{\"name\":\"${repo_name}\",\"private\":${private},\"description\":\"WhatsApp group images to Gemini and back, scheduled by GitHub Actions\",\"has_issues\":true,\"has_wiki\":false}")"

case "$status" in
201) echo "    created" ;;
422)
  echo "    already exists, reusing it"
  ;;
*)
  echo "create failed (HTTP $status):" >&2
  head -c 400 /tmp/gh_setup_body.$$ >&2
  rm -f /tmp/gh_setup_body.$$
  exit 1
  ;;
esac
rm -f /tmp/gh_setup_body.$$

echo "==> storing credential for this repository only"
creds=".git/.git-credentials"
printf 'https://%s:%s@github.com\n' "$owner" "$token" >"$creds"
chmod 600 "$creds"
unset token

echo "==> pushing main"
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/${owner}/${repo_name}.git"
git push -u origin main

cat <<EOF

Done: https://github.com/${owner}/${repo_name}

Next, add the three secrets (Settings -> Secrets and variables -> Actions):
  GREEN_API_INSTANCE_ID
  GREEN_API_TOKEN
  GEMINI_API_KEY

And at least one variable:
  WHATSAPP_CHAT_IDS   (find it with: python3 -m bot.cli doctor)

Then run the workflow manually with "doctor" ticked to verify the connection.
EOF
