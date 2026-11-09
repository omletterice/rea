from __future__ import annotations

from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Platform = Literal["google", "meta", "tiktok", "linkedin", "amazon"]
Amount = Decimal


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Account(StrictModel):
    alias: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    platform: Platform
    account_id: str = Field(pattern=r"^\d{1,30}$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    minor_units: int = Field(default=2, ge=0, le=3)
    daily_budget_limit: Decimal = Field(gt=0, max_digits=16, decimal_places=3)
    credentials_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    login_customer_id: str | None = Field(default=None, pattern=r"^\d{10}$")
    region: Literal["na", "eu", "fe"] = "na"


class CampaignDraft(StrictModel):
    account: str
    kind: Literal["google_search", "meta_traffic"]
    name: str = Field(min_length=1, max_length=128)
    daily_budget: Decimal = Field(gt=0, max_digits=16, decimal_places=3)
    final_url: str
    # Google Search: a complete paused campaign, ad group, exact-match keywords,
    # responsive search ad, geographic and language targeting.
    keywords: list[str] = Field(default_factory=list, max_length=100)
    headlines: list[str] = Field(default_factory=list, max_length=15)
    descriptions: list[str] = Field(default_factory=list, max_length=4)
    location_ids: list[str] = Field(default_factory=list, max_length=20)
    language_ids: list[str] = Field(default_factory=list, max_length=10)
    cpc_bid: Decimal | None = Field(default=None, gt=0, max_digits=16, decimal_places=3)
    # Meta Traffic: existing page and uploaded image, website destination.
    page_id: str | None = Field(default=None, pattern=r"^\d+$")
    image_hash: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{16,64}$")
    primary_text: str | None = Field(default=None, min_length=1, max_length=2000)
    countries: list[str] = Field(default_factory=list, max_length=30)
    age_min: int = Field(default=18, ge=18, le=65)
    age_max: int = Field(default=65, ge=18, le=65)
    special_ad_categories: list[
        Literal["HOUSING", "EMPLOYMENT", "CREDIT", "ISSUES_ELECTIONS_POLITICS"]
    ] = Field(default_factory=list)

    @field_validator("final_url")
    @classmethod
    def public_https(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("A public HTTPS landing-page URL without credentials is required")
        # This URL is only sent to the ad platform, never fetched by this service.
        return value

    @model_validator(mode="after")
    def requirements(self):
        if self.kind == "google_search":
            if (
                not self.keywords
                or not self.location_ids
                or not self.language_ids
                or not self.cpc_bid
            ):
                raise ValueError(
                    "Search needs keywords, numeric location_ids, language_ids and cpc_bid"
                )
            if not 3 <= len(self.headlines) <= 15 or not 2 <= len(self.descriptions) <= 4:
                raise ValueError("Search needs 3–15 headlines and 2–4 descriptions")
            if len(set(self.headlines)) != len(self.headlines) or len(
                set(self.descriptions)
            ) != len(self.descriptions):
                raise ValueError("Headlines and descriptions must each be unique")
            if any(
                [
                    self.page_id,
                    self.image_hash,
                    self.primary_text,
                    self.countries,
                    self.special_ad_categories,
                ]
            ):
                raise ValueError("Meta fields are not accepted in a Google Search draft")
            if any(len(s) > 30 or not s.strip() for s in self.headlines):
                raise ValueError("Headlines must contain 1–30 characters")
            if any(len(s) > 90 or not s.strip() for s in self.descriptions):
                raise ValueError("Descriptions must contain 1–90 characters")
            if any(not s.isdigit() for s in self.location_ids + self.language_ids):
                raise ValueError("Targeting IDs must be numeric provider identifiers")
            if any(not s.strip() or len(s) > 80 or len(s.split()) > 10 for s in self.keywords):
                raise ValueError("Keywords must contain 1–80 characters and at most 10 words")
        else:
            if len(self.headlines) != 1 or any(
                [
                    self.keywords,
                    self.descriptions,
                    self.location_ids,
                    self.language_ids,
                    self.cpc_bid,
                ]
            ):
                raise ValueError(
                    "Meta Traffic accepts exactly one headline and no Google Search fields"
                )
            if not all(
                [self.page_id, self.image_hash, self.primary_text, self.headlines, self.countries]
            ):
                raise ValueError(
                    "Meta needs page_id, existing image_hash, primary_text, headline and countries"
                )
            if self.age_max < self.age_min or any(
                len(c) != 2 or not c.isupper() or not c.isalpha() for c in self.countries
            ):
                raise ValueError("Check age range and ISO 3166 two-letter uppercase country codes")
            if self.special_ad_categories:
                raise ValueError(
                    "Regulated Meta categories need a dedicated targeting workflow; unsupported here"
                )
        return self


class ChangeDraft(StrictModel):
    account: str
    campaign_id: str = Field(pattern=r"^\d{1,30}$")
    action: Literal["pause", "resume", "set_budget"]
    daily_budget: Decimal | None = Field(default=None, gt=0, max_digits=16, decimal_places=3)

    @model_validator(mode="after")
    def budget_requirement(self):
        if (self.action == "set_budget") != (self.daily_budget is not None):
            raise ValueError("daily_budget is required only for set_budget")
        return self


class DateRange(StrictModel):
    start: str
    end: str

    @model_validator(mode="after")
    def dates(self):
        from datetime import date

        start, end = date.fromisoformat(self.start), date.fromisoformat(self.end)
        if start > end or (end - start).days > 366 or end > date.today():
            raise ValueError("Use a past/current range of at most 367 inclusive days")
        return self


class KeywordRequest(StrictModel):
    account: str
    seeds: list[str] = Field(min_length=1, max_length=20)
    location_ids: list[str] = Field(min_length=1, max_length=20)
    language_id: str = Field(pattern=r"^\d+$")
    page_token: str | None = None

    @field_validator("location_ids")
    @classmethod
    def locations(cls, values):
        if any(not v.isdigit() for v in values):
            raise ValueError("Use numeric geo target constants")
        return values
