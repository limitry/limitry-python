# Changelog

What changed in the Limitry Python SDK (`limitry`). Generated from the source on each release; newest first.

## 1.0.0rc15 (2026-11-22)

### Added

- Limitry is here: limits, credits and budgets for the software you
  build.

  Before each costly request, your code asks whether this user, team, API
  key or agent may go ahead. Limitry answers yes or no. Two requests at
  once can't overspend.

  Everything happens in your code. Create and change limits, subjects and
  credits through the `/v1` API, the TypeScript and Python SDKs or the
  CLI. A check is one call, `limitry.checks.create()`, and its `allowed`
  is the answer.

  Webhooks let your code react. `limit.reached` arrives when a limit
  first refuses one of your users. `limit.threshold_crossed` arrives when
  one of them passes 80% or 100% of a limit, or when a balance of credits
  runs low.

  The app is where you see it all and try a check. With the MCP server,
  Claude and ChatGPT, or Claude Code and Codex, can work in your
  workspace with the access you give them. Your team also hears about
  refusals in the app, by email or on their phone.
- A refused check now says whose limit refused it. `reason.subject` names
  the check's own subject, or the shared subject whose limit said no, such
  as a team's budget, and `reason.message` names it too. On the Usage
  page, a user's row says when a shared budget refused their checks.
  Busiest now counts every subject a check names, so a busy team or
  organization pool shows there.
- Subjects can now have an optional name and labels. Set them with
  `PUT /v1/subjects/{kind}/{id}/names` (`limitry subjects set`), or name
  up to 1,000 at once with `POST /v1/subjects/import`.
  `DELETE /v1/subjects/{kind}/{id}` erases them. Checks never need them,
  and every limit works with IDs alone.

  `GET /v1/subjects` lists and searches subjects (`limitry subjects
  list`). `GET /v1/subjects/{kind}/{id}` now also returns a subject's
  budgets and the subjects it's checked with, with what was spent within
  each budget. `GET /v1/checks/recent` lists the last 3 days' checks
  (`limitry checks recent`).

  In the app, the new **Who** page lists every subject your checks name,
  with filters and search. Each subject has its own page: its budgets,
  the subjects it's checked with and what was spent within each budget,
  and its recent checks. The Usage page lists **Recent checks** from the
  last 3 days. On **Settings → General**, the **Kinds** card names your
  kinds and puts them in order, so a subject's page shows who is above
  and below it.

  `limitry.json` can list your kinds too, in an optional `kinds` block that
  `limitry sync` applies. Subject names stay out of the file.

  What `limitry.json` sets is now locked in the app and the API, and says
  so: its limits, and its kinds when it has them. An owner or admin can
  override one until the next sync, with a reason the audit log keeps.
  The next sync puts the file's values back, `limitry sync --preview`
  lists those overrides, and whoever made one gets a notification.

### Changed

- A limit a sync sets now says where it came from. `limits.sync` takes an
  optional `source`, such as `"Billing deploy"`, and the limit's page,
  the 409 and the audit log name it, as in **Set by Billing deploy**.
  `limitry sync` sends the file's name, or the label you pass with
  `--source`. A sync with no source reads **Set by a sync**, where it
  used to say `limitry.json` every time. The **Kinds** card names the
  source the same way.

  `limitry sync` now also sends the file's `kinds`.

## 1.0.0rc14 (2026-11-17)

No user-facing changes.

## 1.0.0rc13 (2026-11-14)

No user-facing changes.

## 1.0.0rc12 (2026-11-09)

No user-facing changes.

## 1.0.0rc11 (2026-11-07)

No user-facing changes.

## 1.0.0rc10 (2026-11-07)

No user-facing changes.

## 1.0.0rc9 (2026-10-27)

No user-facing changes.

## 1.0.0rc8 (2026-10-27)

No user-facing changes.

## 1.0.0rc7 (2026-10-22)

No user-facing changes.

## 1.0.0rc6 (2026-10-21)

No user-facing changes.

## 1.0.0rc5 (2026-10-19)

No user-facing changes.

## 1.0.0rc4 (2026-10-19)

No user-facing changes.

## 1.0.0rc3 (2026-10-16)

No user-facing changes.

## 1.0.0rc2 (2026-10-16)

No user-facing changes.

## 1.0.0rc1 (2026-10-16)

No user-facing changes.
