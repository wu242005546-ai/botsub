"""邮件通知：QQ SMTP，SSL 465 或 STARTTLS 587。失败不影响主流程退出码（CI 由状态库负责红绿）。"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from typing import List

from config import Config

logger = logging.getLogger("notify")

QQ_HOST = "smtp.qq.com"


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _mime(self, subject: str, body: str) -> MIMEText:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str("SubBot V2"), self.cfg.QQ_EMAIL))
        msg["To"] = self.cfg.TO_EMAIL
        return msg

    def send(self, subject: str, body: str) -> bool:
        if not self.cfg.email_enabled:
            logger.info("email disabled (missing QQ_EMAIL/auth/TO_EMAIL), skip send")
            return False
        msg = self._mime(subject, body)
        last_err = None
        for port in (465, 587):
            try:
                if port == 465:
                    ctx = ssl.create_default_context()
                    with smtplib.SMTP_SSL(QQ_HOST, port, context=ctx, timeout=30) as s:
                        s.login(self.cfg.QQ_EMAIL, self.cfg.QQ_EMAIL_AUTH_CODE)
                        s.send_message(msg)
                else:
                    with smtplib.SMTP(QQ_HOST, port, timeout=30) as s:
                        s.starttls(context=ssl.create_default_context())
                        s.login(self.cfg.QQ_EMAIL, self.cfg.QQ_EMAIL_AUTH_CODE)
                        s.send_message(msg)
                logger.info("email sent via port %d: %s", port, subject)
                return True
            except Exception as e:
                last_err = e
                logger.warning("email via port %d failed: %s", port, e)
                continue
        logger.error("email send failed: %s", last_err)
        return False

    def build_summary(self, subject: str, lines: List[str]) -> str:
        return "\n".join(lines) + "\n"