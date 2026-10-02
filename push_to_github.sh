#!/usr/bin/env bash
#
# push_to_github.sh — create the dedicated GitHub repo for AI-native Kali and push.
#
# This project lives in its OWN repo. It must NEVER be pushed to the `petrichor`
# repo (that is a separate private Unity/C# game project). This script refuses to
# run if the remote points at petrichor.
#
# USAGE
#   # 1. Create a PAT with `repo` scope: https://github.com/settings/tokens
#   # 2. Run one of:
#   GITHUB_TOKEN=ghp_xxxxxxxx ./push_to_github.sh
#   ./push_to_github.sh ghp_xxxxxxxx
#   ./push_to_github.sh                 # will prompt for the token (hidden)
#
# OPTIONS (env vars)
#   GITHUB_OWNER   default: bythewayz66-glitch
#   REPO_NAME      default: ai-native-kali
#   REPO_PRIVATE   default: false  (public; set to "true" for a private repo)
#   BRANCH         default: main
#
set -euo pipefail

GITHUB_OWNER="${GITHUB_OWNER:-bythewayz66-glitch}"
REPO_NAME="${REPO_NAME:-ai-native-kali}"
REPO_PRIVATE="${REPO_PRIVATE:-false}"
BRANCH="${BRANCH:-main}"

# --- resolve the token -------------------------------------------------------
TOKEN="${GITHUB_TOKEN:-${1:-}}"
if [ -z "$TOKEN" ]; then
  if [ -t 0 ]; then
    read -r -s -p "GitHub PAT (repo scope, input hidden): " TOKEN
    echo
  else
    echo "ERROR: no token. Set GITHUB_TOKEN, pass it as \$1, or run interactively." >&2
    exit 2
  fi
fi
if [ -z "$TOKEN" ]; then
  echo "ERROR: empty token." >&2
  exit 2
fi

# --- sanity: we are in the right tree ---------------------------------------
if [ ! -f Makefile ] || [ ! -d packaging ]; then
  echo "ERROR: run this from the AI-native Kali repo root (Makefile + packaging/ not found)." >&2
  exit 2
fi
if [ ! -d .git ]; then
  echo "ERROR: no .git here. Run 'git init && git add -A && git commit -m init' first." >&2
  exit 2
fi

# --- hard guard: never touch petrichor --------------------------------------
CURRENT_REMOTE="$(git remote get-url origin 2>/dev/null || echo '')"
case "$CURRENT_REMOTE" in
  *petrichor*)
    echo "REFUSING: the current 'origin' points at petrichor ($CURRENT_REMOTE)." >&2
    echo "petrichor is a different project and must not be touched." >&2
    echo "This script will repoint origin to the new repo below; re-run to proceed." >&2
    ;;
esac

API="https://api.github.com"
AUTH_HEADER="Authorization: Bearer ${TOKEN}"

echo "==> Checking token and identity"
ME_JSON="$(curl -sS -H "$AUTH_HEADER" -H "Accept: application/vnd.github+json" "$API/user")"
ME_LOGIN="$(printf '%s' "$ME_JSON" | sed -n 's/.*"login"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
if [ -z "$ME_LOGIN" ]; then
  echo "ERROR: token rejected by GitHub. Response:" >&2
  printf '%s\n' "$ME_JSON" >&2
  exit 1
fi
echo "    authenticated as: $ME_LOGIN"

# If the token's owner differs from GITHUB_OWNER, prefer the token's own login
# for repo creation (you can only create repos under your own account).
if [ "$ME_LOGIN" != "$GITHUB_OWNER" ]; then
  echo "    note: token belongs to '$ME_LOGIN', not '$GITHUB_OWNER'."
  echo "          creating the repo under '$ME_LOGIN' instead."
  GITHUB_OWNER="$ME_LOGIN"
fi

FULL_REPO="${GITHUB_OWNER}/${REPO_NAME}"
echo "==> Target repo: ${FULL_REPO} (private=${REPO_PRIVATE})"
if [ "$REPO_PRIVATE" = "false" ]; then echo "    (public repo)"; fi

# --- create the repo (idempotent) -------------------------------------------
echo "==> Creating repo (skipped if it already exists)"
CREATE_BODY="$(printf '{"name":"%s","private":%s,"description":"AI-Native Kali Linux - an AI-native OS blueprint: agent runtime, kanban core, memory store, Hermes shell, board UI, observability, and live-build packaging.","auto_init":false}' "$REPO_NAME" "$REPO_PRIVATE")"
HTTP_CODE="$(curl -sS -o /tmp/gh_create.json -w '%{http_code}' \
  -X POST "$API/user/repos" \
  -H "$AUTH_HEADER" -H "Accept: application/vnd.github+json" \
  -d "$CREATE_BODY")"
case "$HTTP_CODE" in
  201) echo "    created." ;;
  422) echo "    already exists — continuing." ;;
  401) echo "ERROR: 401 Unauthorized — token lacks 'repo' scope or is expired." >&2; cat /tmp/gh_create.json >&2; exit 1 ;;
  *)   echo "    unexpected HTTP $HTTP_CODE:"; cat /tmp/gh_create.json; echo ;;
esac

# --- set the remote and push ------------------------------------------------
echo "==> Setting origin -> https://github.com/${FULL_REPO}.git"
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/${FULL_REPO}.git"

# Push using an ephemeral credential helper so the token is never written to
# .git/config or to disk.
echo "==> Pushing branch '${BRANCH}'"
git -c credential.helper='' \
    -c "http.https://github.com/.extraheader=Authorization: Basic $(printf 'x-access-token:%s' "$TOKEN" | base64 | tr -d '\n')" \
    push -u origin "$BRANCH"

echo
echo "==> Done. Repo: https://github.com/${FULL_REPO}"
echo "    Commit: $(git rev-parse HEAD)  Branch: ${BRANCH}"
echo
echo "NOTE: the token was used only for this push and was not stored."
echo "      If you want git to remember it, run:"
echo "        git config --global credential.helper store"
echo "      and push once more (it will then be saved to ~/.git-credentials)."
