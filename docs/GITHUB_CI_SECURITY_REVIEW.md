# GitHub CI security and supply-chain review

Scope: GitHub Actions configuration, Python and frontend dependencies, secrets (working tree and history), Docker images, unsafe defaults,
credential exposure in logs and artifacts. No secret value is printed in this report. Commits are listed in the final validation report.

## Findings and what was done
| Area | Before | After | Status |
|---|---|---|---|
| Actions permissions | `contents: read` for the whole workflow | unchanged in every workflow (`ci`, `security`, `paper-qualification`) | Implemented, verified by reading the files |
| Action pinning | mutable tags (`@v4`, `@v5`) | full commit SHAs with the version in a comment: checkout v4.4.0, setup-python v5.6.0, setup-node v4.4.0, upload-artifact v4.6.2, anchore/scan-action v7.4.2 (SHAs resolved with `git ls-remote` against each action's repository) | Implemented. Hosted runs warn that these v4 actions target Node 20 (forced to Node 24); Dependabot will propose the current majors |
| Dependency updates | none | Dependabot for GitHub Actions, pip (`/backend`) and Docker (`/backend`), weekly | Implemented; takes effect once on `main` |
| Python vulnerabilities | pip-audit on whatever `requirements.txt` resolved to | pip-audit of the exact pinned set CI installs (`constraints-test.txt`, includes test tools), of the packages inside the production image, and of a fresh install; weekly as well | Automated-test verified: "No known vulnerabilities found" on all three in the first hosted runs |
| Frontend dependencies | n/a | the dashboard has no package manager or third-party JS dependency (plain files in `frontend/prototype`) | Not applicable |
| Secrets, working tree | `scripts/scan_secrets.py` (regex heuristic) | kept, and now also run over the CI outputs that are uploaded (e2e log and screenshots, compose logs) | Automated-test verified (0 findings) |
| Secrets, history | none | gitleaks 8.30.1 over every commit (release tarball checked against the release checksum file), values redacted; findings listed by location only | First run: 3 findings, all synthetic values in redaction tests (`test_activity.py:219`, `test_adapter_contract.py:314-315`, commit 6e451a4), reviewed by location and allow-listed by marker in `.gitleaks.toml` with the reason |
| Image vulnerabilities | none | Grype (anchore/scan-action) on the production image; fails on High/Critical with a fix available | First run FAILED: 2 High zlib CVEs (CVE-2026-76844, CVE-2026-85091) with a Debian fix, and pip 25.0.1 advisories (Medium). Fixed: the image applies Debian security updates and pins pip 26.2.0. Remaining: Medium CVEs in Python 3.12 fixed only in newer Python lines (see below) |
| Image contents | tests and caches copied in; unpinned dependencies; no health check | production stage has no tests (checked by `compose-smoke`), runs as uid 10001 (checked), installs the CI pins, HEALTHCHECK on `/health/live`, `.dockerignore` excludes caches, databases and `.env` | Automated-test verified (`compose-smoke`) |
| Image pinning | base images by tag | still by tag (`python:3.13-slim` since 2026-11-13, `postgres:16`, `redis:7`, `nginx:alpine`) | Not done: no registry access from the authoring session to resolve digests. `compose-smoke` records the digest it used (`ci-out/base-image-digest.txt`); Dependabot (docker) proposes updates. Owner decision: pin by digest |
| Credentials in CI | none present | still none: no job has exchange credentials; test databases use the fake password `dewako_test_pw`; the compose smoke `.env` holds only test values and is deleted after the job | Verified by reading the workflows |
| Unsafe defaults | `docker-compose.yml` publishes the backend on 127.0.0.1 only, read-only filesystem, all capabilities dropped, no-new-privileges; production refuses SQLite | unchanged; `compose-smoke` proves a missing PostgreSQL URL in production and an invalid setting both refuse to start | Automated-test verified |
| Unused service | `redis` in `docker-compose.yml` (profile `postgres-port`) although the code has no Redis dependency | kept, profile-gated (never started by default); documented here | Owner decision: remove or keep for the planned port |

## Open items for the owner
1. Repository settings this session cannot read or change: GitHub secret scanning and push protection, Dependabot alerts, Actions
   "allowed actions" policy. Recommended: turn on secret scanning + push protection and Dependabot alerts.
2. *Resolved 2026-11-13: the image moved to `python:3.13-slim`.* Python 3.12 in the production image had Medium advisories fixed only in 3.13+ (CVE-2025-12781, CVE-2025-15366, CVE-2025-15367,
   CVE-2026-116566, CVE-2026-3446, CVE-2026-6019; low: CVE-2026-103995, CVE-2026-3479). CI already tests 3.13 everywhere; moving the image
   to `python:3.13-slim` is a separate, testable change.
3. Pin base images by digest.
4. Accepted-exception policy: every entry in `.gitleaks.toml` or a future `.grype.yaml` needs a reason, the location, and a review date.
