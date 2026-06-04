#!/usr/bin/env python3
"""Standalone SMTP mailer used for error notifications."""

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Optional


class Mailer:
    def __init__(self, enabled=False, smtp_host="localhost", smtp_port=25, sender="oracle-refresh@localhost", recipients=None, username=None, password=None, use_tls=False):
        self.enabled = str(enabled).lower() in ("1", "true", "yes", "on") if isinstance(enabled, str) else bool(enabled)
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port)
        self.sender = sender
        self.recipients = self._split_recipients(recipients)
        self.username = username or None
        self.password = password or None
        self.use_tls = str(use_tls).lower() in ("1", "true", "yes", "on") if isinstance(use_tls, str) else bool(use_tls)

    @staticmethod
    def _split_recipients(recipients) -> list[str]:
        if not recipients:
            return []
        if isinstance(recipients, str):
            return [x.strip() for x in recipients.split(",") if x.strip()]
        return list(recipients)

    def send(self, subject: str, body: str, attachments: Optional[Iterable[str]] = None) -> bool:
        if not self.enabled or not self.recipients:
            return False
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = subject
        msg.set_content(body)
        for attachment in attachments or []:
            path = Path(attachment)
            if not path.is_file():
                continue
            msg.add_attachment(path.read_bytes(), maintype="application", subtype="octet-stream", filename=path.name)
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(msg)
        return True
