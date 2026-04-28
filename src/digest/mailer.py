import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)


def _send(host: str, port: int, user: str, password: str, msg: EmailMessage) -> None:
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


def send_digest(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    sender: str,
    recipient: str,
    subject: str,
    epub_path: Path,
) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(f"{epub_path.name} attached.")
    msg.add_attachment(
        epub_path.read_bytes(),
        maintype="application",
        subtype="epub+zip",
        filename=epub_path.name,
    )
    _send(host, port, user, password, msg)
    log.info(f"digest sent to {recipient}: {epub_path.name} ({epub_path.stat().st_size} bytes)")


def send_alert(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    _send(host, port, user, password, msg)
    log.info(f"alert sent to {recipient}: {subject}")
