#!/usr/local/bin/python3
"""Container healthcheck: GET /api/health on the loopback address; exit 0 only for a 200.

Standard library only (no curl in the image) and no proxy handling, so a proxy environment
variable can never send the probe somewhere else. The endpoint needs no login and answers with a
bare status, never data.
"""

import http.client
import os
import sys

port = int(os.environ.get("PFA_SERVER__PORT", "8756"))
try:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    connection.request("GET", "/api/health")
    sys.exit(0 if connection.getresponse().status == 200 else 1)
except (OSError, http.client.HTTPException, ValueError):
    sys.exit(1)
