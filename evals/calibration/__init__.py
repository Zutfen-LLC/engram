"""ENG-CALIBRATION-001F (#202): reviewed dogfood calibration campaign tooling.

Non-authoritative evaluation code. Nothing in this package imports into
production serving paths, changes recall admission, or alters candidate
ranking. The only production-facing contract consumed here is the #157
``CalibrationProfile`` loader shape (``engram.assessment_calibration``),
which the reviewed artifact must remain compatible with.
"""
