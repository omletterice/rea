"""Create private local configuration once. Never overwrites existing configuration."""

import argparse
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

parser = argparse.ArgumentParser()
parser.add_argument("--demo", action="store_true", help="Use isolated simulated accounts")
args = parser.parse_args()
root = Path(__file__).resolve().parent
for name in (".env", "accounts.json"):
    if (root / name).exists():
        parser.error(f"{name} exists; edit it explicitly rather than overwriting")
values = {
    "ADS_API_KEY": secrets.token_urlsafe(36),
    "ADS_ADMIN_KEY": secrets.token_urlsafe(36),
    "ADS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "ADS_DEMO": str(args.demo).lower(),
    "ADS_ENABLE_WRITES": "false",
    "ADS_DATABASE": "data/demo.sqlite3" if args.demo else "data/live.sqlite3",
    "ADS_PUBLIC_URL": "http://127.0.0.1:8787",
    "ADS_ALLOWED_HOSTS": "127.0.0.1,localhost",
    "ADS_ACCOUNTS_FILE": "accounts.json",
}
previous = os.umask(0o077)
try:
    (root / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    (root / "accounts.json").write_text((root / "accounts.example.json").read_text())
finally:
    os.umask(previous)
print("Created .env and accounts.json. Keys are private in .env. Writes are disabled.")
if not args.demo:
    print(
        "Replace sample account IDs/currencies/limits and configure platform credentials before use."
    )
