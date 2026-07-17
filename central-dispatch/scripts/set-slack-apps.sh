#!/usr/bin/env bash
#
# Interactively build the SLACK_APPS JSON blob and push it to Railway.
#
# Prompts for each Slack app (name / App ID / Client ID / Client Secret /
# Signing Secret), hiding the two secret fields. The value is assembled as valid
# JSON by node (so odd characters are escaped correctly) and sent to Railway over
# stdin — it never appears in argv, `ps`, or your shell history, and is never
# printed back.
#
# Run it yourself in a terminal:  ./central-dispatch/scripts/set-slack-apps.sh
#
set -euo pipefail

# Railway target (override via env if needed).
PROJECT="${RAILWAY_PROJECT_ID:-06312cb9-32a3-4f3d-9e43-1d4b13d1d86b}"
ENVIRONMENT="${RAILWAY_ENVIRONMENT:-production}"
SERVICE="${RAILWAY_SERVICE:-claudebot}"

command -v railway >/dev/null || { echo "railway CLI not found on PATH"; exit 1; }
command -v node    >/dev/null || { echo "node not found on PATH"; exit 1; }

echo "Configure SLACK_APPS for Central-Dispatch → $SERVICE / $ENVIRONMENT"
echo "Enter each Slack app. The FIRST app is the primary (dashboard sign-in)."
echo "Leave 'App name' blank to finish."
echo

APPS_JSON="[]"
while true; do
  read -rp  "App name (blank to finish): " NAME
  [ -z "$NAME" ] && break
  read -rp  "  App ID (e.g. A0BFM4WA2RZ): " APPID
  read -rp  "  Client ID: "                 CLIENTID
  read -rsp "  Client Secret: "             CLIENTSECRET; echo
  read -rsp "  Signing Secret: "            SIGNINGSECRET; echo

  # Append one object; values passed via env (not argv) so they stay private.
  APPS_JSON=$(
    APPS_JSON="$APPS_JSON" NAME="$NAME" APPID="$APPID" CLIENTID="$CLIENTID" \
    CLIENTSECRET="$CLIENTSECRET" SIGNINGSECRET="$SIGNINGSECRET" \
    node -e '
      const arr = JSON.parse(process.env.APPS_JSON);
      arr.push({
        appId:         process.env.APPID,
        name:          process.env.NAME,
        clientId:      process.env.CLIENTID,
        clientSecret:  process.env.CLIENTSECRET,
        signingSecret: process.env.SIGNINGSECRET,
      });
      process.stdout.write(JSON.stringify(arr));
    '
  )
  echo "  ✓ added \"$NAME\""
  echo
done

COUNT=$(APPS_JSON="$APPS_JSON" node -e 'process.stdout.write(String(JSON.parse(process.env.APPS_JSON).length))')
if [ "$COUNT" -eq 0 ]; then
  echo "No apps entered; nothing to do."
  exit 1
fi

# Masked preview so you can sanity-check names/App IDs without leaking secrets.
echo "About to set SLACK_APPS with $COUNT app(s):"
APPS_JSON="$APPS_JSON" node -e '
  for (const a of JSON.parse(process.env.APPS_JSON))
    console.log(`  - ${a.name}  (appId=${a.appId}, clientId=${a.clientId}, secrets hidden)`);
'
read -rp "Push to Railway now? [y/N] " OK
[ "$OK" = "y" ] || [ "$OK" = "Y" ] || { echo "Aborted."; exit 0; }

# Send over stdin (value not in argv). This triggers a redeploy so the running
# service picks up the new variable.
printf '%s' "$APPS_JSON" | railway variable set SLACK_APPS --stdin \
  -p "$PROJECT" -e "$ENVIRONMENT" -s "$SERVICE" >/dev/null

echo "Done — SLACK_APPS set on $SERVICE/$ENVIRONMENT (value not printed)."
echo "Railway will redeploy to apply it."
