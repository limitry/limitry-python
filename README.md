# limitry

The Limitry Python SDK puts a limit around costly work in a `with` block: `guard` checks before the block runs, and `reserve` reserves what the work will probably cost, then charges what it actually cost. Every API operation is a typed method too, sync or async.

Limitry is the limits, credits and budgets layer for your software: give each of your users, teams, tenants, API keys or agents an allowance, and before any costly action one call tells your code whether it may happen.

## Install

```bash
pip install limitry
```

Needs Python 3.10 or later. Set `LIMITRY_API_KEY` to a key from **API
keys** in the app. Use a test key (`lim_test_…`) while you build.

## Guard costly work

`guard` checks the limit, then runs the block only if it's allowed.
Here it counts one AI call against a limit such as 100 a day:

```python
from limitry import LimitReached, Limitry

limitry = Limitry()  # reads LIMITRY_API_KEY

try:
    with limitry.guard(subject={"kind": "user", "id": "usr_123"}, action="ai-call"):
        reply = call_model(prompt)
except LimitReached as error:
    print(f"Try again in {error.retry_after_seconds} seconds")
```

## Charge what it actually cost

When you only know the cost afterwards, such as an AI reply priced by
the token, `reserve` reserves what it will probably cost (5 cents here),
runs the block and charges what it used. If the block raises, the
reservation is released:

```python
from limitry import Limitry

limitry = Limitry()

with limitry.reserve(
    subject={"kind": "user", "id": "usr_123"}, action="ai-call", cost=5
) as reservation:
    response = call_model(prompt)
    reservation.cost(price_cents(response.usage))  # your prices, in cents
```

A refusal raises `LimitReached`. If Limitry can't be reached, each
limit's fail mode decides: `open` lets the work run, `closed` raises
`Unavailable`.

## Everything else

Every API operation is a typed method, named like its CLI command:
`limitry limits create` is `limitry.limits.create()`. For asyncio, use
`AsyncLimitry`.

```python
from limitry import Limitry

limitry = Limitry()

# "100 units per user per day for ai-call"
limitry.limits.create(
    subject={"kind": "user"},
    action="ai-call",
    amount=100,
    window="day",
)
```

A failure raises `ApiError` with the error's `code`, `message`, `status`
and `request_id`.

## Links

- [Website](https://limitry.com)
- [Python SDK guide](https://limitry.com/docs/python/)
- [API reference](https://limitry.com/docs/reference/)
- [Changelog](https://github.com/limitry/limitry-python/blob/main/CHANGELOG.md)
- [Discord](https://limitry.com/discord)
- [Report a security issue](https://limitry.com/security/)

## Contributing

This repository is generated from Limitry's source on each
release, so pull requests are ported there and credited (see
[CONTRIBUTING.md](https://github.com/limitry/limitry-python/blob/main/CONTRIBUTING.md)).

## License

Licensed under the Apache License 2.0.
