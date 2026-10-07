"""Native launcher: one self-contained worker, or an affinity-sized Redis deployment."""

import os


def worker_count():
    configured = os.environ.get("WEB_WORKERS")
    if configured:
        workers = int(configured)
    elif os.environ.get("REDIS_URL"):
        cpus = (
            len(os.sched_getaffinity(0))
            if hasattr(os, "sched_getaffinity")
            else os.cpu_count() or 1
        )
        workers = min(4, max(1, cpus))
    else:
        workers = 1
    if workers < 1:
        raise ValueError("WEB_WORKERS must be positive")
    if workers > 1 and not os.environ.get("REDIS_URL"):
        raise ValueError("REDIS_URL is required for multiple workers")
    return workers


def main():
    import uvicorn
    from django.core.management import execute_from_command_line

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "campfire.settings")
    workers = worker_count()
    execute_from_command_line(["manage.py", "prepare"])
    uvicorn.run(
        "campfire.asgi:application",
        host=os.environ.get("BIND", "0.0.0.0"),
        port=int(os.environ.get("HTTP_PORT", "8080")),
        workers=workers,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("TRUSTED_PROXIES", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
