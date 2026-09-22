#!/usr/bin/env bash
#
# First live LLM run: real model, real planning, real money.
#
# Everything before this has run on FakeLLM — canned plans, no network. This
# points the orchestrator at a real provider and drives one report end to end
# so you can watch the model actually plan.
#
#   LLM_PROVIDER=anthropic LLM_API_KEY=sk-ant-... ./scripts/live_demo.sh
#   LLM_PROVIDER=openai LLM_API_KEY=sk-... LLM_MODEL=gpt-4o-mini \
#     LLM_BASE_URL=https://api.openai.com/v1 ./scripts/live_demo.sh
#
# Pass --yes to skip the spend confirmation (CI). AGENTS.md requires human
# approval before spending money, so the prompt is on by default.
set -euo pipefail

cd "$(dirname "$0")/.."

API="${API:-http://localhost:8000}"

# Fall back to whatever .env already holds, so the settings only have to be
# written once. Anything exported in the shell still wins. Only LLM_* keys are
# read, and the value is taken verbatim after the first '=' so keys containing
# '=' survive.
if [ -f .env ]; then
  for _key in LLM_PROVIDER LLM_API_KEY LLM_MODEL LLM_BASE_URL; do
    if [ -z "$(eval "echo \${$_key:-}")" ]; then
      _val=$(grep -m1 "^${_key}=" .env 2>/dev/null | cut -d= -f2- || true)
      [ -n "$_val" ] && export "$_key=$_val"
    fi
  done
  unset _key _val
fi

LLM_PROVIDER="${LLM_PROVIDER:-anthropic}"
LLM_MODEL="${LLM_MODEL:-}"
LLM_BASE_URL="${LLM_BASE_URL:-}"
GOAL="${GOAL:-Find the most viable home organization products to source from China for the US market.}"
POLL_TIMEOUT="${POLL_TIMEOUT:-180}"
ASSUME_YES="no"
[ "${1:-}" = "--yes" ] && ASSUME_YES="yes"

fail() { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- checks ---
[ -n "${LLM_API_KEY:-}" ] || fail "LLM_API_KEY is not set. Put it in .env or export it (see .env.example)."

# Catch the untouched placeholder here, rather than as a 401 after the stack
# is already up and a container has been recreated.
case "$LLM_API_KEY" in
  *REPLACE*|your-key-here|sk-REPLACE-ME)
    fail "LLM_API_KEY is still the placeholder. Edit .env and paste your real key."
    ;;
esac

case "$LLM_PROVIDER" in
  anthropic)
    # llm.DEFAULT_ANTHROPIC_MODEL (claude-opus-5) applies if unset.
    ;;
  openai)
    [ -n "$LLM_MODEL" ]    || fail "LLM_PROVIDER=openai requires LLM_MODEL (e.g. gpt-4o-mini)."
    [ -n "$LLM_BASE_URL" ] || fail "LLM_PROVIDER=openai requires LLM_BASE_URL (e.g. https://api.openai.com/v1)."
    ;;
  fake)
    fail "LLM_PROVIDER=fake would not exercise a real model. Set anthropic or openai."
    ;;
  *)
    fail "unknown LLM_PROVIDER: '$LLM_PROVIDER' (anthropic, openai)"
    ;;
esac

echo "=============================================================="
echo " FIRST LIVE LLM RUN"
echo "   provider : $LLM_PROVIDER"
echo "   model    : ${LLM_MODEL:-<provider default>}"
echo "   goal     : $GOAL"
echo
echo " This calls a real API and SPENDS REAL MONEY. One goal is two"
echo " model calls (one plan, one synthesis), so cents — but it is not free."
echo "=============================================================="
if [ "$ASSUME_YES" != "yes" ]; then
  printf "Proceed? [y/N] "
  read -r reply
  case "$reply" in [yY]*) ;; *) echo "Aborted; nothing spent."; exit 0 ;; esac
fi

# ------------------------------------------------------- configure .env ---
# The orchestrator reads these through compose's env_file, so they have to be
# on disk before the container starts. Writing .env is the documented path;
# it is gitignored.
if [ -f .env ]; then
  BACKUP=".env.backup.$(date +%s)"
  cp .env "$BACKUP"
  echo "==> backed up existing .env to $BACKUP"
fi
touch .env
# Drop any previous LLM_* lines, then append the current ones.
grep -v -E '^(LLM_PROVIDER|LLM_API_KEY|LLM_MODEL|LLM_BASE_URL)=' .env > .env.tmp || true
{
  cat .env.tmp
  echo "LLM_PROVIDER=$LLM_PROVIDER"
  echo "LLM_API_KEY=$LLM_API_KEY"
  [ -n "$LLM_MODEL" ]    && echo "LLM_MODEL=$LLM_MODEL"
  [ -n "$LLM_BASE_URL" ] && echo "LLM_BASE_URL=$LLM_BASE_URL"
} > .env
rm -f .env.tmp
echo "==> wrote LLM settings into .env"

# --------------------------------------------------------- bring it up ---
echo "==> starting the stack"
docker compose up -d >/dev/null

echo "==> recreating the orchestrator so it picks up the new provider"
docker compose up -d --force-recreate orchestrator >/dev/null

echo "==> waiting for the API"
for _ in $(seq 1 60); do
  curl -fsS "$API/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "$API/health" >/dev/null || fail "API never came up. docker compose logs fastapi"

# Give the orchestrator's consumer group time to join, or the goal sits
# unread while the group rebalances.
echo "==> waiting for the orchestrator to join its consumer group"
sleep 15

# ------------------------------------------------------- submit a goal ---
echo "==> submitting a report request"
REPORT_ID=$(curl -fsS -X POST "$API/v1/reports" \
  -H 'Content-Type: application/json' \
  -d "{\"category\":\"home_organization\",\"query\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$GOAL"),\"max_products\":5}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["report_id"])')
echo "    report_id=$REPORT_ID"

echo
echo "---------------- orchestrator (real planning) ----------------"
docker compose logs -f orchestrator --since 30s &
LOG_PID=$!
trap 'kill $LOG_PID 2>/dev/null || true' EXIT

# ------------------------------------------------------------- poll ------
STATUS=""
for _ in $(seq 1 "$POLL_TIMEOUT"); do
  CODE=$(curl -s -o /tmp/live_demo_report.json -w '%{http_code}' "$API/v1/reports/$REPORT_ID")
  if [ "$CODE" = "200" ]; then
    STATUS=$(python3 -c 'import json;print(json.load(open("/tmp/live_demo_report.json"))["report_status"])')
    break
  fi
  sleep 1
done

kill $LOG_PID 2>/dev/null || true
echo "--------------------------------------------------------------"
echo

if [ -z "$STATUS" ]; then
  echo "TIMED OUT after ${POLL_TIMEOUT}s — report $REPORT_ID never settled." >&2
  echo "Check:  docker compose logs orchestrator report_listener" >&2
  exit 1
fi

# ------------------------------------------------------------ report -----
echo "================= FINAL REPORT ($STATUS) ====================="
python3 - "$REPORT_ID" <<'PY'
import json, sys
d = json.load(open("/tmp/live_demo_report.json"))
print("report_id      :", d["report_id"])
print("report_status  :", d["report_status"])
print("data status    :", d["status"], "(curated — fixtures, not sourced)")
print("confidence     :", d["confidence_score"])
print("opportunities  :", len(d["opportunities"]))
print()
print("SUMMARY (written by the live model):")
print(" ", d["summary"])
print()
for item in d["opportunities"]:
    print(f"  {item['opportunity_score']:>5}  {item['title']}")
PY
echo "=============================================================="
echo
echo "To go back to FakeLLM (no spend):"
echo "  sed -i '' 's/^LLM_PROVIDER=.*/LLM_PROVIDER=fake/' .env"
echo "  docker compose up -d --force-recreate orchestrator"
echo
echo "NOTE: your API key is now in .env (gitignored). Remove it when done."
