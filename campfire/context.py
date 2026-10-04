from contextvars import ContextVar

request_host = ContextVar("campfire_request_host", default="")
