import json
import os
import uuid

from .jobs import permitted_endpoint
from .push_transport import PinnedSession


def test_push(subscription):
    private = os.environ.get("VAPID_PRIVATE_KEY")
    if not private or not permitted_endpoint(subscription.endpoint):
        return
    from pywebpush import webpush

    webpush(
        {
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh_key, "auth": subscription.auth_key},
        },
        json.dumps(
            {
                "title": "Campfire Test",
                "options": {
                    "body": str(uuid.uuid4()),
                    "data": {"path": "/users/me/push_subscriptions", "badge": 0},
                },
            }
        ),
        requests_session=PinnedSession(),
        vapid_private_key=private,
        vapid_claims={
            "sub": os.environ.get("VAPID_SUBJECT", "mailto:campfire@example.com")
        },
        timeout=7,
    )
