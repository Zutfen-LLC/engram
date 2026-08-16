# Engram

Engram provides governed, durable memory for AI agents. This glossary defines
the product terms used in planning and design.

## Language

**Public launch**:
The first supported open-source release that external operators can install and
run on their own infrastructure. It excludes the hosted portal and managed
service, which belong to a separate repository.
_Avoid_: Hosted launch, SaaS launch

**Supported Hermes integration**:
The first-class Engram integration for a named stock-Hermes compatibility range.
It covers installation, upgrade, uninstall, explicit MCP operations, automatic
lifecycle capture, fresh-session recall, failure behavior, and rollback. It does
not cover arbitrary Hermes commits or defects that Engram does not cause.
_Avoid_: Hermes compatibility, Hermes example

**External operator**:
A technical early adopter who can operate Docker Compose, PostgreSQL,
environment files, and command-line agent configuration.
_Avoid_: Launch user, end user

**Supported deployment**:
An Engram deployment that uses Docker Compose v2 with the bundled PostgreSQL 16
and pgvector stack. A supported Hermes host uses the provided shell installer
on a Unix-like system.
_Avoid_: Reference deployment, example deployment

**Public beta**:
The first tagged public release. Supported paths receive active support, but
backward compatibility is not guaranteed before version 1.0. Breaking changes
still require release notes and an upgrade path.
_Avoid_: General availability, stable release

**Self-hosted data responsibility**:
The external operator controls Engram data, backups, retention, and deletion.
The public beta does not claim suitability for regulated or sensitive data.
Hard-delete APIs, PII classification, and sensitive-read auditing are not beta
launch requirements, but a security review is required.
_Avoid_: Hosted data responsibility, compliance support

**Beta support**:
Best-effort help through public GitHub issues and private security reports. Only
the latest beta and its documented Hermes compatibility range receive fixes.
No response-time or uptime service-level agreement applies.
_Avoid_: Production support, support SLA

**Release artifact**:
An immutable, versioned artifact tied to a public release tag. The supported
installation path does not install mutable code from the `main` branch.
_Avoid_: Main-branch install, development build

**Release gate**:
A non-waivable, evidence-backed condition for the public-beta go/no-go decision.
One named release owner approves the complete gate record.
_Avoid_: Recommendation, optional check

**Supported interface**:
A public-beta interface covered by release tests and documentation. The REST
API, Python SDK, and MCP adapter are general supported interfaces. Hermes is the
only supported automatic agent integration.
_Avoid_: Example integration, incidental compatibility

**Semantic launch path**:
The supported configuration that uses one documented OpenAI-compatible
embedding provider and model for semantic recall. Keyword-only operation is a
supported degraded mode when embeddings are disabled.
_Avoid_: Mocked semantic path, unverified provider

**Clean-machine proof**:
An onboarding test performed by a person who did not implement the setup. The
person uses only public documentation to deploy, configure Hermes, verify the
memory loop, upgrade, uninstall, and restore data.
_Avoid_: Maintainer smoke test, local development test

**Beta security review**:
A documented internal threat-model review with automated dependency, secret,
container, and code scanning. The public beta cannot launch with an unresolved
critical or high-severity finding. It does not claim a third-party audit.
_Avoid_: External audit, compliance certification

**Launch communication**:
Technical material that states product positioning, supported claims, known
limitations, release notes, contribution guidance, and security-reporting
guidance. It excludes campaigns, press outreach, social posts, and growth
targets.
_Avoid_: Marketing campaign, launch campaign
