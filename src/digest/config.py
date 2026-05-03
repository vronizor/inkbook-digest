import os
import sys
from dataclasses import dataclass
from pathlib import Path


REQUIRED_ALWAYS = ("READER_TOKEN",)
REQUIRED_FOR_SEND = (
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "ALERT_EMAIL",
)


@dataclass(frozen=True)
class Config:
    reader_token: str
    reader_tag_trigger: str
    reader_tag_done: str

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str

    alert_email: str

    digest_hour: int
    digest_minute: int
    tz: str

    image_soft_cap_mb: int
    data_dir: Path
    log_level: str

    word_budget: int

    library_max_upload_mb: int
    base_url: str


def load(*, require_smtp: bool = True) -> Config:
    required = REQUIRED_ALWAYS + (REQUIRED_FOR_SEND if require_smtp else ())
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(
            f"FATAL: missing required env vars: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        return Config(
            reader_token=os.environ["READER_TOKEN"],
            reader_tag_trigger=os.environ.get("READER_TAG_TRIGGER", "toepub"),
            reader_tag_done=os.environ.get("READER_TAG_DONE", "sent-to-inkbook"),
            smtp_host=os.environ.get("SMTP_HOST", "smtp.protonmail.ch"),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_from=os.environ.get("SMTP_FROM", ""),
            alert_email=os.environ.get("ALERT_EMAIL", ""),
            digest_hour=int(os.environ.get("DIGEST_HOUR", "6")),
            digest_minute=int(os.environ.get("DIGEST_MINUTE", "30")),
            tz=os.environ.get("TZ", "Europe/Paris"),
            image_soft_cap_mb=int(os.environ.get("IMAGE_SOFT_CAP_MB", "10")),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            word_budget=int(os.environ.get("WORD_BUDGET", "5000")),
            library_max_upload_mb=int(os.environ.get("LIBRARY_MAX_UPLOAD_MB", "200")),
            base_url=os.environ.get("BASE_URL", "").rstrip("/"),
        )
    except ValueError as e:
        print(f"FATAL: invalid env var value: {e}", file=sys.stderr)
        sys.exit(2)
