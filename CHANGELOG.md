# Changelog

What changed in the Limitry Python SDK (`limitry`). Generated from the source on each release; newest first.

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
