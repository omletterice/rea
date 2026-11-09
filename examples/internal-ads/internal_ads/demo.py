"""Explicit simulated provider. Its state is persisted and cannot mix with live data."""

from .providers_base import AdsError, Provider, metrics


class Demo(Provider):
    capabilities = Provider.capabilities + ["google_search", "meta_traffic", "keywords"]

    def __init__(self, account, store):
        self.account, self.store = account, store
        if self.store.brand("__demo:" + account.alias) is None:
            self.store.brand(
                "__demo:" + account.alias,
                {
                    "1001": {
                        "id": "1001",
                        "name": "DEMO · starter",
                        "status": "PAUSED",
                        "daily_budget": "100",
                        "currency": account.currency,
                    }
                },
            )

    def data(self):
        return self.store.brand("__demo:" + self.account.alias)

    async def verify_account(self):
        return {
            "id": self.account.account_id,
            "currency": self.account.currency,
            "timezone": "UTC",
            "simulated": True,
        }

    async def campaigns(self, limit=50, cursor=None):
        values = list(self.data().values())
        start = int(cursor or 0)
        end = start + limit
        return {
            "items": values[start:end],
            "next_cursor": str(end) if end < len(values) else None,
            "complete": end >= len(values),
            "simulated": True,
        }

    async def get_campaign(self, campaign_id):
        data = self.data()
        if campaign_id not in data:
            raise AdsError("Demo campaign not found", "not_found", 404)
        return data[campaign_id]

    async def performance(self, dates, limit=50, cursor=None):
        page = await self.campaigns(limit, cursor)
        return {
            **page,
            "items": [
                metrics(
                    c["id"], spend="100", impressions=2000, clicks=80, conversions=4, revenue=600
                )
                for c in page["items"]
            ],
            "attribution": "SIMULATED fixtures; never use for business decisions",
        }

    async def keywords(self, request):
        return {
            "items": [
                {"text": s, "average_monthly_searches": None, "cpc": None} for s in request.seeds
            ],
            "complete": True,
            "next_cursor": None,
            "simulated": True,
            "source": "Demo seeds only; no invented market estimates",
        }

    async def create(self, draft, receipt):
        values = self.data()
        key = str(max(map(int, values)) + 1)
        c = {
            "id": key,
            "name": draft.name,
            "status": "PAUSED",
            "daily_budget": str(draft.daily_budget),
            "currency": self.account.currency,
        }
        values[key] = c
        self.store.brand("__demo:" + self.account.alias, values)
        await receipt("demo_create", {"id": key, "simulated": True})
        return {"campaign": c, "simulated": True}

    async def change(self, draft, baseline, receipt):
        values = self.data()
        c = values[draft.campaign_id]
        if draft.action == "set_budget":
            c["daily_budget"] = str(draft.daily_budget)
        else:
            c["status"] = "PAUSED" if draft.action == "pause" else "ACTIVE"
        self.store.brand("__demo:" + self.account.alias, values)
        await receipt("demo_change", {"id": c["id"], "simulated": True})
        return {"campaign": c, "simulated": True}
