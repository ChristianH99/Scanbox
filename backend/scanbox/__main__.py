"""Entry point: ``python -m scanbox`` / ``scanbox`` (also: ``scanbox openapi > openapi.json``)."""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "openapi":
        from .config import AppConfig
        from .main import create_app

        app = create_app(AppConfig(disable_background=True))
        print(json.dumps(app.openapi(), indent=2, ensure_ascii=False))
        return
    import uvicorn

    uvicorn.run(
        "scanbox.main:create_app",
        factory=True,
        host=os.environ.get("SCANBOX_HOST", "0.0.0.0"),  # noqa: S104 - container
        port=int(os.environ.get("SCANBOX_PORT", "8080")),
        workers=1,  # in-process queue + scanner lock require exactly one process
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("SCANBOX_FORWARDED_ALLOW_IPS", "*"),
        log_config=None,
        timeout_graceful_shutdown=25,
    )


if __name__ == "__main__":
    main()
