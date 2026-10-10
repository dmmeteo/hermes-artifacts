# Next epics

The only outcome checklist for this repository. Statuses describe this public `main` revision (0.1.0 preview). Implemented code is not runtime verification or user acceptance. An item is checked only after its completion condition is verified. Candidates are not selected and are not permission to implement. No outcome is selected yet. The candidates below come from the README's known gaps.

## Delivered baseline

This revision implements the library, the standalone console, the Hermes plugin and expiring guest links described in [README.md](../README.md). The README [Status](../README.md#status) section records what the release gate exercised on 2026-10-09. This pass recorded no new runtime or user acceptance.

## Outcomes

- [ ] **Candidate, not selected: a published release.**
  - Status: unselected. There is no published container image, package or release.
  - Done when: an owner-approved release is published and installs and runs from that artifact alone.
  - Fails if: the release differs from the revision that passed the release gate.
- [ ] **Candidate, not selected: the full gate passes on a fresh clone.**
  - Status: unselected. `scripts/verify.py` fails until a real-agent smoke is recorded for the current source.
  - Done when: a contributor can run the documented gates on a fresh clone and get a result that does not depend on a prior smoke on another machine.
  - Fails if: the gate passes without evidence for the current agent and backend source.
  - Reference: [Integration gates](../CONTRIBUTING.md#integration-gates).
- [ ] **Candidate, not selected: schema migration.**
  - Status: unselected. An older database schema is refused, not migrated.
  - Done when: an older library opens after upgrade with its artifacts, shares, expiry and revocation state intact.
  - Fails if: a migration reactivates a revoked or expired share.
- [ ] **Candidate, not selected: browser coverage beyond Chromium.**
  - Status: unselected. Firefox and Safari are expected to fail closed but are untested.
  - Done when: the sandbox, CSP and guest viewer are verified in Firefox and Safari.
  - Fails if: any browser runs artifact content in the Hermes origin, or allows a network request that the content CSP blocks in Chromium.
  - Reference: [docs/SECURITY.md](../docs/SECURITY.md#residual-limitations).
