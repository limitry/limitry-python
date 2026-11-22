# limitry

The Limitry Python SDK puts a limit around costly work in a `with` block: `guard` checks before the block runs, and `reserve` reserves what the work will probably cost, then charges what it actually cost. Every API operation is a typed method too, sync or async.

[![A $2 AI budget for one of your users: three calls are charged, and the next is refused before the AI runs.](https://media.limitry.com/launch/readme-20261122-0258.gif)](https://limitry.com/#demo)

[▶ Watch the 44-second demo](https://limitry.com/#demo)

Limitry is the limits, credits and budgets layer for the software you build: give each of your users, teams, tenants, API keys or agents an allowance, and before any costly request one call tells your code whether it may happen.

## Install

```bash
pip install limitry
```

Needs Python 3.10 or later. Set `LIMITRY_API_KEY` to a key from
**Settings → Developers** in the app. Use a test key (`lim_test_…`)
while you build. To pass the key yourself, use `Limitry(api_key=key)`.

## Charge what it actually cost

Say each of your users has a $2 AI budget a month, counted in cents.
You only know what a reply cost once it's done. `reserve` holds what it
will probably cost (5 cents here), runs the block, then charges what it
actually cost. If the block raises, `reserve` gives the hold back:

```python
from limitry import Limitry

limitry = Limitry()  # reads LIMITRY_API_KEY

with limitry.reserve(
    subject={"kind": "user", "id": "user_123"}, action="ai-call", cost=5
) as reservation:
    response = call_model(prompt)
    reservation.cost = price_cents(response.usage)  # your prices, in cents
```

Each hold counts until it's charged, so two replies at once can't
overspend.

## Guard work that always costs the same

When every request costs the same, `guard` is simpler. It checks the limit,
then runs the block only if it's allowed. Here it counts one request to
your API against a limit of 60 a minute for each key:

```python
from limitry import LimitReachedError, Limitry

limitry = Limitry()  # reads LIMITRY_API_KEY

try:
    with limitry.guard(subject={"kind": "apikey", "id": "key_42"}, action="api-request"):
        response = handle_request(request)
except LimitReachedError as error:
    print(f"Try again in {error.retry_after_seconds} seconds")
```

A refusal raises `LimitReachedError`. If Limitry can't answer within
`timeout` (1.5 seconds by default), each limit's fail mode decides:
`open` lets the work run, and `closed` raises `UnavailableError`. Both
errors are a `LimitryError`.

## Everything else

Every API operation is a typed method, named like its CLI command:
`limitry limits create` is `limitry.limits.create()`. For asyncio, use
`AsyncLimitry`.

```python
from limitry import Limitry

limitry = Limitry()

# "200 cents per user per month for ai-call"
limitry.limits.create(
    subject={"kind": "user"},
    action="ai-call",
    amount=200,
    unit="cents",
    window="month",
)
```

A failure raises an `APIError` subclass for its status, such as
`NotFoundError`, with the error's `code`, `message`, `status_code` and
`request_id`.

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
