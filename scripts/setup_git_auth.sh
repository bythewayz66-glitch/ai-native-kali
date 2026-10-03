#!/usr/bin/env bash
#
# setup_git_auth.sh — make git push work for the AI-native Kali repo WITHOUT a PAT.
#
# The sandbox has no GITHUB_TOKEN, but it DOES have an SSH ed25519 key that is
# registered on GitHub as a write-enabled DEPLOY KEY for bythewayz66-glitch/
# ai-native-kali. This script wires git to use that key persistently so that
# future agent runs can `git push` directly and never hit the "no credentials"
# blocker again.
#
# Auth strategy (in priority order):
#   1. SSH deploy key  (~/.ssh/id_ed25519) — preferred, no token needed, already
#      registered and verified against github.com.
#   2. GITHUB_TOKEN    — optional PAT fallback; used to (re)create the repo and
#      to push over HTTPS if SSH is unavailable.
#
# This script is idempotent and safe to re-run. It does NOT modify the petrichor
# repo — the remote is always (re)pointed at the AI-native Kali repo.
#
# USAGE
#   ./scripts/setup_git_auth.sh           # set up SSH auth (default)
#   GITHUB_TOKEN=ghp_... ./scripts/setup_git_auth.sh   # also note the PAT path
#
# OPTIONS (env vars)
#   GITHUB_OWNER   default: bythewayz66-glitch
#   REPO_NAME      default: ai-native-kali
#   BRANCH         default: main
#
set -euo pipefail

GITHUB_OWNER="${GITHUB_OWNER:-bythewayz66-glitch}"
REPO_NAME="${REPO_NAME:-ai-native-kali}"
BRANCH="${BRANCH:-main}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

# --- sanity: we are in the right tree ---------------------------------------
if [ ! -f Makefile ] || [ ! -d packaging ]; then
  echo "ERROR: run this from the AI-native Kali repo root (Makefile + packaging/ not found)." >&2
  exit 2
fi
if [ ! -d .git ]; then
  echo "ERROR: no .git here. Run 'git init && git add -A && git commit -m init' first." >&2
  exit 2
fi

# --- git identity ------------------------------------------------------------
git config user.name  "${GIT_AUTHOR_NAME:-$GITHUB_OWNER}"
git config user.email "${GIT_AUTHOR_EMAIL:-$GITHUB_OWNER@users.noreply.github.com}"
echo "==> git identity: $(git config user.name) <$(git config user.email)>"

# --- hard guard: never touch petrichor --------------------------------------
CURRENT_REMOTE="$(git remote get-url origin 2>/dev/null || echo '')"
case "$CURRENT_REMOTE" in
  *petrichor*)
    echo "REFUSING: the current 'origin' points at petrichor ($CURRENT_REMOTE)." >&2
    echo "petrichor is a different project and must not be touched." >&2
    echo "Repoint origin to the AI-native Kali repo first, then re-run." >&2
    exit 1
    ;;
esac

SSH_URL="git@github.com:${GITHUB_OWNER}/${REPO_NAME}.git"

# --- prefer SSH deploy key ---------------------------------------------------
USE_SSH=0
if [ -f "$SSH_KEY" ]; then
  # Make sure the ssh config pins this key for github.com.
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  if [ ! -f "$HOME/.ssh/config" ] || ! grep -q "Host github.com" "$HOME/.ssh/config" 2>/dev/null; then
    cat > "$HOME/.ssh/config" <<EOF
Host github.com
    HostName github.com
    User git
    IdentityFile $SSH_KEY
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
    chmod 600 "$HOME/.ssh/config"
  fi
  echo "==> testing SSH auth to github.com ..."
  # Point a throwaway ls-remote at the SSH URL: this is the reliable signal that
  # the deploy key works (the `ssh -T` banner grep is flaky over a pipe).
  git remote remove origin 2>/dev/null || true
  git remote add origin "$SSH_URL"
  if git ls-remote origin >/dev/null 2>&1; then
    echo "    SSH deploy key authenticated (ls-remote OK)."
    USE_SSH=1
  else
    echo "    SSH auth did not confirm (key may not be registered). Will try token path."
  fi
fi

# --- point the remote --------------------------------------------------------
if [ "$USE_SSH" = "1" ]; then
  git remote remove origin 2>/dev/null || true
  git remote add origin "$SSH_URL"
  echo "==> origin -> $SSH_URL  (SSH, deploy key)"
else
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/${GITHUB_OWNER}/${REPO_NAME}.git"
  echo "==> origin -> https://github.com/${GITHUB_OWNER}/${REPO_NAME}.git  (HTTPS, needs GITHUB_TOKEN)"
fi

# --- verify read access ------------------------------------------------------
echo "==> verifying with: git ls-remote origin"
if git ls-remote origin >/dev/null 2>&1; then
  echo "    OK — remote is reachable."
  git ls-remote origin | sed 's/^/    /'
else
  echo "    could not read the remote yet."
  if [ "$USE_SSH" != "1" ] && [ -z "${GITHUB_TOKEN:-}" ]; then
    echo
    echo "    No SSH key authenticated and no GITHUB_TOKEN is set."
    echo "    ACTION REQUIRED (one of):"
    echo "      1. Add a repo-scoped PAT as a sandbox secret named GITHUB_TOKEN, then:"
    echo "           GITHUB_TOKEN=\$GITHUB_TOKEN ./push_to_github.sh"
    echo "      2. Or register the public half of ~/.ssh/id_ed25519 as a write deploy key"
    echo "         on ${GITHUB_OWNER}/${REPO_NAME}, then re-run this script."
    exit 1
  fi
fi

echo
echo "==> setup_git_auth done. Push with:  git push -u origin ${BRANCH}"
if [ "$USE_SSH" = "1" ]; then
  echo "    (no token required — SSH deploy key is active)"
fi
