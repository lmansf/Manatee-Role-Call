"""FWC synoptic aerial surveys (optional statewide context layer).

Spec §3.3: a handful of flights per winter, far too sparse to model on. If it is built at
all it is a context table for the dashboard, not a model input. Left as a stub so the
package layout shows where it would go. Data lives at geodata.myfwc.com (CSV, WFS).
"""
from __future__ import annotations

SOURCE_NAME = "fwc_synoptic"


def fetch_surveys():
    raise NotImplementedError("optional; see spec §3.3")
