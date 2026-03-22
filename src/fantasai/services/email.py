"""Email delivery via Resend."""
from __future__ import annotations

import logging

import resend

from fantasai.config import settings

_log = logging.getLogger(__name__)

FROM_ADDRESS = "FantasAI Sports <noreply@fantasaisports.com>"


def _client() -> None:
    resend.api_key = settings.resend_api_key


def send_welcome(email: str, name: str) -> bool:
    """Send a welcome email to a newly registered user. Returns True on success."""
    if not settings.resend_api_key:
        _log.warning("RESEND_API_KEY not set — skipping welcome email to %s", email)
        return False
    try:
        _client()
        resend.Emails.send(
            {
                "from": FROM_ADDRESS,
                "to": email,
                "subject": "Welcome to FantasAI Sports!",
                "html": f"""
                <div style="font-family: sans-serif; max-width: 560px; margin: 0 auto;">
                  <h2 style="color: #1e3a5f;">Welcome, {name or 'Manager'}! ⚾</h2>
                  <p>Your FantasAI Sports account is ready. You now have access to:</p>
                  <ul>
                    <li>AI-powered player rankings &amp; blurbs</li>
                    <li>Trade evaluations &amp; waiver recommendations</li>
                    <li>Team evaluations and keeper planning</li>
                  </ul>
                  <p>
                    <a href="{settings.app_url}" style="
                      background:#2563eb; color:#fff; padding:10px 20px;
                      border-radius:6px; text-decoration:none; display:inline-block;
                    ">Open FantasAI Sports →</a>
                  </p>
                  <p style="color:#6b7280; font-size:12px; margin-top:32px;">
                    Powered by Claude AI · You received this because you signed up at fantasaisports.com
                  </p>
                </div>
                """,
            }
        )
        _log.info("Welcome email sent to %s", email)
        return True
    except Exception:
        _log.warning("Failed to send welcome email to %s", email, exc_info=True)
        return False
