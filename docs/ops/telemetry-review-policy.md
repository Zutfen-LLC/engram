# Telemetry and future billing review policy

The repository currently has no `.github/CODEOWNERS`, and no appropriate
existing team identity could be safely inferred without inventing one. A
repository administrator must configure a GitHub ruleset that:

- requires at least one human approval;
- requires code-owner approval for `engram/usage.py`, `engram/usage_report.py`,
  `engram/classification.py`, `engram/conflicts.py`, `engram/embeddings.py`,
  `migrations/*usage*`, `docs/usage-metering.md`, and future metering/billing paths;
- dismisses stale approvals after changes;
- requires CI;
- prohibits auto-merge until the required human approval is present.

Branch-protection and ruleset state lives outside this repository. An
administrator must separately confirm it; this document does not claim those
settings are active.
