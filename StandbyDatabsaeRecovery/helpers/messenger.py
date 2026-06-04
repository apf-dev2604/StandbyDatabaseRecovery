#!/usr/bin/env python3
"""Standalone text messenger. Uses free Python package requests if installed, otherwise urllib."""

import json
from urllib import request as urllib_request


class Messager:
    def __init__(self, enabled=False, webhook_url="", channel=""):
        self.enabled = str(enabled).lower() in ("1", "true", "yes", "on") if isinstance(enabled, str) else bool(enabled)
        self.webhook_url = webhook_url
        self.channel = channel

    def send(self, text: str) -> bool:
        if not self.enabled or not self.webhook_url:
            return False
        payload = {"text": text}
        if self.channel:
            payload["channel"] = self.channel
        data = json.dumps(payload).encode("utf-8")
        try:
            import requests  # type: ignore
            response = requests.post(self.webhook_url, json=payload, timeout=15)
            return 200 <= response.status_code < 300
        except ImportError:
            req = urllib_request.Request(self.webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib_request.urlopen(req, timeout=15) as response:
                return 200 <= response.status < 300
