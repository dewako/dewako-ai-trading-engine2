# GitHub CI/CD improvement plan

Base: `main` = `9172eb2af4127baaa599af6edf1808b24f4f8ca5` (Stage 3 merged as PR #6 after three green runs of `da950cb`).
Authorised by the owner on 2026-11-13 ("go ahead implement all"). Decision taken for the owner: Windows 3.14 becomes informational
(continue-on-error) until its hang is root-caused; 3.13 stays blocking.

## Workflow structure (decision)
| Proposed | Decision | Why |
|---|---|---|
| ci.yml (core) | **keep and harden** | already covers backend, SQLite, PG16, crash, Windows, e2e, Docker |
| backend-tests.yml | not added | would duplicate ci.yml jobs |
| frontend-tests.yml | not added | no frontend toolchain exists; a JS syntax check and enforced Node UI tests go into ci.yml |
| security.yml | **add** | pip-audit of the installed set, gitleaks over history, image scan; also weekly |
| docker-validation.yml | not added | a `compose-smoke` job in ci.yml |
| paper-trading-validation.yml | **add** as `paper-qualification.yml` | checklist tests + bounded soak, nightly and manual |
| testnet-validation.yml | not added | the owner's hard rule forbids testnet adapters; an empty placeholder adds nothing |
| release-gate.yml | not now | needs a release process first |

## Changes (in order)
1. ci.yml: `workflow_dispatch`; concurrency (cancel superseded runs, never on main); push + pull_request both kept (the PR run tests the
   merge result; skipping it could let a "skipped" result stand in for a failure); min-tests raised to measured counts; required tests in
   one shared file (`.github/required-tests/`), read by `assert_no_skips.py --require-file`; JUnit for every job kept with
   `if-no-files-found: error`; e2e writes JUnit, must run in real mode and record all 16 checks; Node required for the UI tests;
   JS syntax check; actions pinned to commit SHAs; stale comments replaced; Windows 3.14 informational.
2. Docker: multi-stage Dockerfile (production without tests, test stage for compose), dependencies installed with the CI pins,
   HEALTHCHECK, .dockerignore; compose test JUnit kept and skip policy enforced; `compose-smoke` job for the production image.
3. security.yml + Dependabot + `.gitleaks.toml`.
4. paper-qualification.yml + `backend/tests/paper_soak.py`.
5. Failure-injection proof on a throwaway branch; documentation reconciliation; branch protection; three green runs; final report.

## Rollback
Every step is its own commit on branch `ci-hardening` and lands through one pull request; `git revert` of a commit restores the previous
workflow. Branch protection can be removed in the repository settings.
