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


def send_move_grade_notification(
    email: str,
    name: str,
    transaction_type: str,     # "add", "drop", "trade"
    grade_letter: str,         # "A+", "B-", etc.
    rationale: str,
    player_names: list[str],   # main player(s) involved
    manager_name: str,
    league_name: str,
    share_url: str,            # URL to the grade card page
) -> bool:
    """Send a move grade notification email. Returns True on success."""
    if not settings.resend_api_key:
        _log.warning("RESEND_API_KEY not set — skipping move grade email to %s", email)
        return False

    # Grade colour for email
    if grade_letter.startswith("A") or grade_letter.startswith("B"):
        grade_colour = "#2d8a40"
    elif grade_letter.startswith("C"):
        grade_colour = "#d97706"
    else:
        grade_colour = "#c0392b"

    type_label = transaction_type.upper()
    player_str = ", ".join(player_names[:3])
    subject = f"[{grade_letter}] {manager_name}'s {type_label}: {player_str} — {league_name}"

    try:
        _client()
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": email,
            "subject": subject,
            "html": f"""
            <div style="font-family: sans-serif; max-width: 560px; margin: 0 auto; background: #0a0f1a; padding: 24px; border-radius: 12px; color: #f9fafb;">
              <div style="text-align: center; margin-bottom: 24px;">
                <span style="font-size: 22px; font-weight: 900; color: #f9fafb;">Fantas</span><span style="font-size: 22px; font-weight: 900; color: #2d8a40;">AI</span><span style="font-size: 22px; font-weight: 900; color: #f9fafb;"> Sports</span>
              </div>
              <div style="background: #111827; border-radius: 10px; padding: 20px; margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                  <div>
                    <div style="font-size: 11px; color: #9ca3af; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px;">{league_name}</div>
                    <div style="font-size: 13px; color: #9ca3af;">{manager_name} · {type_label}</div>
                  </div>
                  <div style="width: 56px; height: 56px; border-radius: 50%; background: {grade_colour}; display: flex; align-items: center; justify-content: center; font-size: 26px; font-weight: 900; color: white; line-height: 56px; text-align: center;">{grade_letter}</div>
                </div>
                <div style="font-size: 20px; font-weight: 700; color: #f9fafb; margin-bottom: 8px;">{player_str}</div>
                <p style="font-size: 14px; color: #d1d5db; line-height: 1.6; margin: 0;">{rationale}</p>
              </div>
              <div style="text-align: center;">
                <a href="{share_url}" style="background: #2d8a40; color: white; padding: 10px 24px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 600; display: inline-block;">View Grade Card →</a>
              </div>
              <p style="color: #4b5563; font-size: 11px; text-align: center; margin-top: 24px;">
                You're receiving this because you opted in to move grade notifications.<br>
                <a href="{settings.app_url}/profile" style="color: #6b7280;">Manage notifications</a>
              </p>
            </div>
            """,
        })
        _log.info("Move grade email sent to %s (%s)", email, grade_letter)
        return True
    except Exception:
        _log.error("send_move_grade_notification: failed for %s", email, exc_info=True)
        return False
