"""Entry point: ``python -m mediahub`` / ``mediahub``."""

from __future__ import annotations

import uvicorn

from mediahub.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "mediahub.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,  # required: poller + cache are in-process (see runtime_lock.py)
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=10,
        server_header=False,
    )


if __name__ == "__main__":
    main()
