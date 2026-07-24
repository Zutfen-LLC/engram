"""Engram Control Plane — hosted SaaS control-plane boundary (ENG-PORTAL-001A).

The Control Plane is a hard service boundary separate from Engram Core. This
slice establishes only the scaffold: a dedicated database boundary, health/
readiness, safe metadata, structured logging, and an independent Alembic
migration history. No production identity, billing, delegation, organization,
or management behavior is implemented here.

This package MUST NOT import the ``engram`` Core package.
"""

__version__ = "0.1.0"
