#!/usr/bin/env bash
# Deploy this agent to LiveKit Cloud.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> LiveKit CLI version"
lk --version || true

echo "==> Linked projects"
lk project list

echo "==> Existing agents"
lk agent list || true

SECRETS_FILE=".env.deploy.secrets"
python3 << 'PY'
from pathlib import Path

src = Path(".env")
if not src.exists():
    src = Path(".env.local")
out = Path(".env.deploy.secrets")
keep = {
    "AGENT_NAME",
    "SIP_OUTBOUND_TRUNK_ID",
    "TWILIO_FROM_NUMBER",
    "LK_STT_MODEL",
    "LK_LLM_MODEL",
    "LK_TTS_MODEL",
    "LK_TTS_VOICE",
    "LK_TTS_SPEED",
    "COMPANY_NAME",
    "COMPANY_PITCH",
    "NUM_IDLE_PROCESSES",
}
vals = {}
for line in src.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    k, v = k.strip(), v.strip().strip('"').strip("'")
    if k in keep and v:
        vals[k] = v

if vals.get("NUM_IDLE_PROCESSES") == "0":
    vals["NUM_IDLE_PROCESSES"] = "1"

vals.setdefault("AGENT_NAME", "my-agent")
vals.setdefault("COMPANY_NAME", "our company")

out.write_text("\n".join(f"{k}={v}" for k, v in vals.items()) + "\n")
print("Secrets to upload (names only):", ", ".join(sorted(vals)))
print("Wrote", out)
PY

REGION="${LIVEKIT_AGENT_REGION:-eu-central}"

if [[ -f livekit.toml ]]; then
  echo "==> Updating existing agent (lk agent deploy)"
  lk agent update-secrets --secrets-file="$SECRETS_FILE" --yes -y || true
  lk agent deploy --secrets-file="$SECRETS_FILE" --yes -y
else
  echo "==> Creating new agent in region [$REGION] (lk agent create)"
  lk agent create --secrets-file="$SECRETS_FILE" --region "$REGION" --yes -y
fi

rm -f "$SECRETS_FILE"

echo "==> Status"
lk agent status

echo "Done."
echo "  uv run python voice_agent.py start"
echo "  uv run python voice_agent.py call +1XXXXXXXXXX"
echo "  lk agent logs"
