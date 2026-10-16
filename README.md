# limitry

The Python client for the Limitry API. Every operation is a
method, typed from the API itself; sync and async.

Limitry is the limits, credits and budgets layer for your software: give each of your users, teams, tenants, API keys or agents an allowance, and before any costly action one call tells your code whether it may happen.

## Install

```bash
pip install limitry
```

## Use

```python
from limitry import Limitry

limitry = Limitry()  # reads LIMITRY_API_KEY
workspace = limitry.workspace.get()
```

A failure raises `ApiError` with the error's `code`, `message`,
`status` and `request_id`. For asyncio, use `AsyncLimitry`.

- Guide: https://limitry.com/docs/python/
- API reference: https://api.limitry.com/v1/docs
