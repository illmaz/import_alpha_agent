#!/usr/bin/env bash
#
# Switch the orchestrator between FakeLLM (free) and a real provider (spends).
#
#   ./scripts/toggle_llm.sh fake      # canned plans, no network, no spend
#   ./scripts/toggle_llm.sh real      # prompts for a key, hidden input
#   ./scripts/toggle_llm.sh status    # what is configured right now
#
# The key is read with `read -s`, so it is never echoed and never lands in
# shell history — unlike `export LLM_API_KEY=sk-...`, which does both.
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  echo "usage: $0 {fake|real|status}" >&2
  exit 2
}

[ $# -eq 1 ] || usage

# .env is gitignored and read by compose's env_file; it is the only place
# these settings belong.
[ -f .env ] || { echo "==> creating .env from .env.example"; cp .env.example .env; }

# Rewrite one KEY=value line in place, appending if absent. Uses a temp file
# rather than sed -i, whose syntax differs between GNU and BSD.
set_env() {
  local key="$1" value="$2"
  grep -v -E "^${key}=" .env > .env.tmp || true
  printf '%s=%s\n' "$key" "$value" >> .env.tmp
  mv .env.tmp .env
}

current() {
  grep -m1 -E "^$1=" .env 2>/dev/null | cut -d= -f2- || true
}

# True when .env holds a usable key. Never prints the key itself.
has_real_key() {
  local k; k="$(current LLM_API_KEY)"
  [ -n "$k" ] || return 1
  case "$k" in *REPLACE*|your-key-here) return 1 ;; esac
  return 0
}

key_state() {
  local k; k="$(current LLM_API_KEY)"
  if has_real_key; then echo "set (${#k} chars)"
  elif [ -n "$k" ]; then echo "placeholder"
  else echo "not set"
  fi
}

show_status() {
  echo "  LLM_PROVIDER : $(current LLM_PROVIDER)"
  echo "  LLM_MODEL    : $(current LLM_MODEL)"
  echo "  LLM_BASE_URL : $(current LLM_BASE_URL)"
  echo "  LLM_API_KEY  : $(key_state)"
}

apply() {
  echo "==> recreating the orchestrator so it picks up the change"
  docker compose up -d --force-recreate orchestrator
  echo "==> done. Current settings:"
  show_status
}

case "$1" in
  status)
    show_status
    exit 0
    ;;

  fake)
    set_env LLM_PROVIDER fake
    # The key is deliberately left in place: switching back to real should not
    # require pasting it again. FakeLLM never reads it, so nothing is spent.
    echo "==> LLM_PROVIDER=fake (canned plans, no network, no spend)"
    apply
    ;;

  real)
    provider="$(current LLM_PROVIDER)"
    if [ -z "$provider" ] || [ "$provider" = "fake" ]; then
      provider="openai"
    fi

    printf 'Provider [openai/anthropic] (default %s): ' "$provider"
    read -r entered
    [ -n "$entered" ] && provider="$entered"

    case "$provider" in
      openai|anthropic) ;;
      *) echo "ERROR: provider must be openai or anthropic" >&2; exit 1 ;;
    esac

    need_key=yes
    if has_real_key; then
      printf 'A key is already stored. Reuse it? [Y/n] '
      read -r reuse
      case "$reuse" in [nN]*) need_key=yes ;; *) need_key=no ;; esac
    fi

    if [ "$need_key" = yes ]; then
      # -s: no echo. The key never appears on screen or in shell history,
      # unlike `export LLM_API_KEY=sk-...`, which leaves it in both.
      printf 'Paste %s API key (input hidden): ' "$provider"
      read -rs api_key
      echo
      [ -n "$api_key" ] || { echo "ERROR: no key entered" >&2; exit 1; }
      set_env LLM_API_KEY "$api_key"
      unset api_key
    fi

    set_env LLM_PROVIDER "$provider"

    if [ "$provider" = "openai" ]; then
      [ -n "$(current LLM_MODEL)" ]    || set_env LLM_MODEL gpt-4o-mini
      [ -n "$(current LLM_BASE_URL)" ] || set_env LLM_BASE_URL https://api.openai.com/v1
    fi

    echo
    echo "*** LLM_PROVIDER=$provider — every report now makes real API calls and"
    echo "*** spends real money. Run '$0 fake' to stop."
    apply
    ;;

  *)
    usage
    ;;
esac
