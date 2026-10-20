# Changelog

What changed in the Limitry Python SDK (`limitry`). Generated from the source on each release; newest first.

## 1.0.0rc6 (2026-10-21)

### Added

- Limits have `counting`: `fast` answers the instant it decides, and
  `guaranteed` stores every count safely before answering, so the limit
  never goes over, even if a server fails. A `total` balance defaults to
  `guaranteed`, every other window to `fast`. Set it when creating or
  updating a limit (`counting`, `--counting`), or in `limitry.json`.

### Changed

- - Creating a limit needs only who it is for, the amount and the window:
    `limits.create({ subject: { kind: "user" }, amount: 100, window: "month" })`
    (`limitry limits create --subject '{"kind":"user"}' --amount 100 --window month`).
    `action` defaults to every action (`*`), `unit` to `units`, and `name`
    to the sentence the limit reads as, such as "100 units per user per
    month". If a limit already has that name, the API answers `409`: give
    this one a `name`. `limitry.json` entries still need a `name`.
  - A limit's input names who it is for as `subject: { kind, id }` (was
    `subjectKind` and `subjectId`), the same shape the limit itself has.
  - What a limit counts is fixed once it is created: its subject, action,
    unit, window, `every`, `start` and `rolling`. Changing one answers
    `400 invalid_request`: create a new limit and archive the old one (its
    history stays). Name, amount, fail mode and enabled still change any
    time. `limitry sync` refuses such a change until you rename the entry.
  - A limit says when a check last reached it (`lastCheckedAt`), so you can
    see a limit no check names.
- One name for each idea, everywhere: the API, both SDKs, the CLI, the MCP
  tools, `limitry.json` and webhooks. Update your code when you upgrade:

  - `hold()` is `reserve()`: it takes `cost` (was `estimate`), and the block
    gets a `Reservation` (was `Held`; Python `Reservation` and
    `AsyncReservation`), with the same `cost()` and `extend()`.
  - A refusal throws `LimitReached` (was `LimitExceeded`), with `limitId`
    (Python `limit_id`) and `reason` `limit_reached` or
    `too_many_reservations`. When Limitry can't be reached and a limit fails
    closed, you now get a separate `Unavailable` error. "Can't be reached"
    also covers a `402 plan_limit_reached` and a `429` from Limitry's flood
    guard, so each limit's fail mode decides instead of your code getting an
    error.
  - A check's answer lists its limits in `limits` (was `remaining`): read
    `decision.limits[0].remaining`. Every answer that names a limit says
    `limitId`: `reason.limitId`, `limits[].limitId`, `usage.standing`'s
    `limits[].limitId`, and webhook payloads.
  - The refusal code is `limit_reached` (was `limit_exceeded`), and the
    webhook event is `limit.reached` (was `limit.exceeded`): subscribe your
    endpoints to the new name.
  - Usage says `refused` (was `denied`); `usage.subjects` no longer has
    `over` (it is `refused > 0`).
  - Credits: `limits.credits.create()` and `limits.credits.list()` at
    `/v1/limits/{limitId}/credits` (were `limits.grants.*` at `…/grants`),
    ids start `crd_` (were `grt_`), and the approval an agent asks for is
    `limit.credit` (was `limit.grant`).
  - A period that starts on the subject's billing date is
    `start: "billing_date"` (was `"anchor"`), and `subjects.update()` takes
    `billingDate` (was `anchor`); the subject says `billingDate` too.
  - `usage.standing()` and `usage.query()` take one `subject`, written
    `kind:id` (`user:usr_123`), instead of separate kind and id fields
    (`limitry usage standing --subject user:usr_123`).
  - A limit's id starts `lmt_` (was `lim_`); keys still start `lim_test_`
    and `lim_live_`.
  - `limitry sync --preview` (was `--dry-run`), and the API's `preview`
    (was `dryRun`), in what you send and what comes back.

### Removed

- Limits no longer have `counting` (Fast or Guaranteed), and the CLI no
  longer has `--counting`: every count is now saved before Limitry answers,
  for every limit. Remove `counting` from your code and from
  `limitry.json`; Limitry ignores it.

## 1.0.0rc5 (2026-10-19)

### Added

- A refused check's `reason` has a `message`: the refusal in one sentence,
  such as "Refused by AI budget: 3 cents left, resets
  2026-11-01T00:00:00.000Z. Try again in 3600 s." `LimitExceeded` and
  `limitry check` show it as the API writes it, so every client says the
  same. Branch on `allowed` and `reason.code`, never on the message.

### Fixed

- - `held.cost()` refuses a cost the API can't commit (not a whole number
    from 0 to 1,000,000,000): TypeScript throws `RangeError`, Python raises
    `ValueError`, in your code. Before, the commit failed silently after
    the work and its usage was never counted.
  - TypeScript's `held.extend()` throws the API's error, such as for an
    expired hold, as Python's already did. Both send the `ttlSeconds` you
    give as it is, so `0` is refused rather than read as the default.
  - Python checks, commits and releases now retry exactly as TypeScript's
    do: a network error, `408`, `429` or `5xx`, at most twice, all within
    the check's `timeout` (1.5 s) or 5 s for a settle. `standing()` retries
    too, and reports an error answer that isn't JSON.
  - `limitry check` and `limitry sync` refuse `--workspace` with an API key,
    as every other command does, instead of ignoring it.

## 1.0.0rc4 (2026-10-19)

### Changed

- `LimitExceeded` says which limit refused, by name, and when to try
  again: "Refused by AI replies per day: 0 replies left, resets
  2026-10-20T00:00:00.000Z. Try again in 60 s." It used to show only the
  limit's id. `error.limit` (`limit` in Python) is still the id.

## 1.0.0rc3 (2026-10-16)

No user-facing changes.

## 1.0.0rc2 (2026-10-16)

### Added

- Each release comes with a `CHANGELOG.md` listing what changed, newest first.

### Changed

- The package page (README, on GitHub, npm and PyPI) shows what the product does with a first real example, and links the guide, the API reference, the changelog and support.

## 1.0.0rc1 (2026-10-16)

No user-facing changes.
