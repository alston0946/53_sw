import importlib.util
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("五三形态_五佛手_简洁版_申万行业.py")


def load_strategy_module():
    spec = importlib.util.spec_from_file_location("five_buddha_strategy", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load strategy module: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_recipients(raw: str) -> list[str]:
    parts = []
    for piece in (raw or "").replace(";", ",").split(","):
        item = piece.strip()
        if item:
            parts.append(item)
    return parts


def build_email_body(result: dict) -> str:
    lines = [
        "GitHub Actions 已完成五佛手扫描。",
        "",
        f"目标日期: {', '.join(result.get('target_dates', []))}",
        f"股票池数量: {result.get('universe_size', 0)}",
        f"观察池结果数: {result.get('observation_rows', 0)}",
        f"精简结果数: {result.get('observation_lite_rows', 0)}",
        f"调试记录数: {result.get('debug_rows', 0)}",
        f"失败记录数: {result.get('failed_rows', 0)}",
        "",
        "默认附件包含精简结果和完整结果。你也可以在 GitHub Actions Artifacts 下载全部输出文件。",
    ]
    return "\n".join(lines)


def attach_file(message: EmailMessage, path: str) -> None:
    file_path = Path(path)
    if not file_path.exists():
        return
    data = file_path.read_bytes()
    message.add_attachment(
        data,
        maintype="text",
        subtype="csv",
        filename=file_path.name,
    )


def send_email(result: dict) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.qq.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465").strip())
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    email_to = split_recipients(os.getenv("EMAIL_TO", smtp_user))
    subject_prefix = os.getenv("EMAIL_SUBJECT_PREFIX", "五佛手监控").strip()

    if not smtp_user or not smtp_password or not email_to:
        raise RuntimeError("SMTP_USER, SMTP_PASSWORD, EMAIL_TO must be configured.")

    message = EmailMessage()
    message["From"] = smtp_user
    message["To"] = ", ".join(email_to)
    message["Subject"] = f"{subject_prefix} | {', '.join(result.get('target_dates', []))} | {result.get('observation_rows', 0)} 条"
    message.set_content(build_email_body(result))

    attach_file(message, result.get("observation_lite_file", ""))
    attach_file(message, result.get("observation_file", ""))

    if os.getenv("EMAIL_ATTACH_DEBUG", "false").lower() == "true":
        attach_file(message, result.get("debug_file", ""))
        attach_file(message, result.get("failed_file", ""))
        attach_file(message, result.get("filtered_file", ""))

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(message)


def main():
    strategy = load_strategy_module()
    result = strategy.main()
    send_email(result)
    print("Email sent successfully.")


if __name__ == "__main__":
    main()
