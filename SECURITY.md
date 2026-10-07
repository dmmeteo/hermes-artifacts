# Security policy

Hermes Artifacts is a preview. Security fixes land on the default branch; there are no supported release lines yet.

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability** (https://github.com/dmmeteo/hermes-artifacts/security/advisories/new). Do not open a public issue for a vulnerability.

Include the affected component (service, console, guest viewer, Hermes plugin or guest bridge), the revision, steps to reproduce and the impact you expect. Do not include real share links, credentials or other people's artifacts; use synthetic data.

## Scope

In scope: authentication of the machine API and the console, guest-link expiry and revocation, isolation of artifact content from the dashboard, console and guest viewer, credential exposure, and the shipped deployment templates.

Known residual limitations are listed in [docs/SECURITY.md](docs/SECURITY.md#residual-limitations), for example that CSP does not stop WebRTC beacons from a hostile artifact and that a forwarded link grants access until it expires or is revoked. Reports that bypass them in a new way are welcome.
