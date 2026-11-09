from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .models import Account


@dataclass
class Settings:
    api_key: str
    admin_key: str
    encryption_key: str
    database: str
    accounts: list[Account]
    demo: bool = False
    enable_writes: bool = False
    public_url: str = "http://127.0.0.1:8787"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")
    approval_ttl: int = 900

    def __post_init__(self):
        if len(self.api_key) < 32 or len(self.admin_key) < 32 or self.api_key == self.admin_key:
            raise ValueError(
                "Set distinct ADS_API_KEY and ADS_ADMIN_KEY, each at least 32 characters"
            )
        from cryptography.fernet import Fernet

        Fernet(self.encryption_key.encode())
        if len({a.alias for a in self.accounts}) != len(self.accounts):
            raise ValueError("Account aliases must be unique")
        if len({(a.platform, a.account_id) for a in self.accounts}) != len(self.accounts):
            raise ValueError("Each provider account must have one policy owner/alias")
        if not self.public_url.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
            raise ValueError("Remote ADS_PUBLIC_URL must use HTTPS")

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv

        load_dotenv(override=False)
        return cls(
            api_key=os.environ["ADS_API_KEY"],
            admin_key=os.environ["ADS_ADMIN_KEY"],
            encryption_key=os.environ["ADS_ENCRYPTION_KEY"],
            database=os.getenv("ADS_DATABASE", "data/internal-ads.sqlite3"),
            accounts=[
                Account.model_validate(a)
                for a in json.loads(
                    Path(os.getenv("ADS_ACCOUNTS_FILE", "accounts.json")).read_text()
                )
            ],
            demo=os.getenv("ADS_DEMO", "false").lower() == "true",
            enable_writes=os.getenv("ADS_ENABLE_WRITES", "false").lower() == "true",
            public_url=os.getenv("ADS_PUBLIC_URL", "http://127.0.0.1:8787").rstrip("/"),
            allowed_hosts=tuple(os.getenv("ADS_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")),
        )
