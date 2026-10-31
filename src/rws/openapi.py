"""Export the OpenAPI schema: ``python -m rws.openapi > openapi.json``.

The committed ``openapi.json`` is the published contract; CI fails if it drifts
from the code, and the typed client in ``rws.client`` is generated from it.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rws.api.app import create_app
from rws.config import Settings


def build_schema() -> dict[str, Any]:
    app = create_app(Settings(database_url="sqlite://", log_json=False, log_level="ERROR"))
    return app.openapi()


def main() -> None:
    sys.stdout.write(json.dumps(build_schema(), indent=2) + "\n")


if __name__ == "__main__":
    main()
