"""The quality layer: checks, incidents, quarantine, baselines and the alert (spec §5).

Terms follow CONTEXT.md. An incident is a failed source-level check. A quarantined
observation is one value held out of training until a person clears it.
"""
