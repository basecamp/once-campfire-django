import asyncio
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "campfire.settings")
from django.core.asgi import get_asgi_application

http = get_asgi_application()
from . import cable, jobs


async def application(scope, receive, send):
    if scope["type"] == "lifespan":
        relay = None
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                jobs.start()
                if os.environ.get("REDIS_URL"):
                    relay = asyncio.create_task(cable.relay())
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                if relay:
                    relay.cancel()
                from asgiref.sync import sync_to_async

                await sync_to_async(jobs.stop)()
                await send({"type": "lifespan.shutdown.complete"})
                return
    elif scope["type"] == "websocket":
        if scope["path"] == "/cable":
            await cable.websocket(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1008})
    else:
        await http(scope, receive, send)
