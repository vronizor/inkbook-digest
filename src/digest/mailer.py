import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

log = logging.getLogger(__name__)


def _send(host: str, port: int, user: str, password: str, msg: EmailMessage) -> None:
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


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


def build_html_message(
    *,
    sender: str,
    sender_name: str,
    recipient: str,
    subject: str,
    html: str,
    text: str,
    reply_to: str = "",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    unsubscribe_to = reply_to or sender
    msg["List-Unsubscribe"] = f"<mailto:{unsubscribe_to}?subject=unsubscribe>"
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send_message(*, host: str, port: int, user: str, password: str, msg: EmailMessage) -> int:
    _send(host, port, user, password, msg)
    size = len(msg.as_bytes())
    log.info(f"email sent to {msg['To']}: {msg['Subject']} ({size} bytes)")
    return size
