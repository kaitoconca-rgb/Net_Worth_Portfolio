"""
build_gdrive_creds.py
═══════════════════════════════════════════════════════════════════════
One-time helper: builds a correctly-formatted gdrive_creds.json from your
service account details, without you having to manually escape newlines
in the private key (which is what caused the JSONDecodeError).

HOW TO USE:
1. Open your secrets.toml, find the [gdrive] block.
2. Copy the private_key value's contents (everything between the quotes,
   including "-----BEGIN PRIVATE KEY-----" and "-----END PRIVATE KEY-----")
   into a new local file called private_key_raw.txt, in this same folder.
   - If it's stored in TOML as a single line with literal \n characters,
     paste it exactly as-is (this script handles both cases).
   - If it's stored as an actual multi-line block, paste it as multiple
     real lines — that's fine too.
3. Fill in PRIVATE_KEY_ID below (the short string, not the long key).
4. Run: python build_gdrive_creds.py
5. This creates gdrive_creds.json correctly. Delete private_key_raw.txt
   afterward — you don't need it anymore and it's a plaintext secret.
═══════════════════════════════════════════════════════════════════════
"""

import json

# ── Fill this in — the private_key_id value from secrets.toml ──────────────
PRIVATE_KEY_ID = "7b2eae001d82d2b9e5ef6c6a11b40de6ea012ff0"

# ── These are already correct from what you shared — no need to touch ──────
PROJECT_ID = "n8n-battery-engine"
CLIENT_EMAIL = "raiz-938@n8n-battery-engine.iam.gserviceaccount.com"
CLIENT_ID = "102459706956599350649"

# ── Read the raw private key from a separate local file ────────────────────
with open("private_key_raw.txt", "r", encoding="utf-8") as f:
    raw_key = f.read().strip()

# Normalise: if it was pasted with literal \n escape sequences (single line),
# turn those into real newlines first, then let json.dump escape them
# correctly and consistently either way.
if "\\n" in raw_key and "\n" not in raw_key.replace("\\n", ""):
    raw_key = raw_key.replace("\\n", "\n")

creds = {
    "type": "service_account",
    "project_id": PROJECT_ID,
    "private_key_id": PRIVATE_KEY_ID,
    "private_key": raw_key,
    "client_email": CLIENT_EMAIL,
    "client_id": CLIENT_ID,
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": f"https://www.googleapis.com/robot/v1/metadata/x509/{CLIENT_EMAIL.replace('@', '%40')}",
}

with open("gdrive_creds.json", "w", encoding="utf-8") as f:
    json.dump(creds, f, indent=2)

print("✅ gdrive_creds.json written successfully.")
print("   Now delete private_key_raw.txt — it's no longer needed.")