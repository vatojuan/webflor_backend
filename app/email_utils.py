# app/email_utils.py
"""
Utilidades centralizadas de correo para FAP Mendoza.

Mantiene compatibilidad con funciones existentes:
    • send_confirmation_email()
    • send_credentials_email()

Añade:
    • send_match_email()

Todas usan el helper send_email() que envía por SMTP-SSL (puerto 465).
"""
from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Final

from dotenv import load_dotenv

load_dotenv()

# ───────────────────── Config SMTP ─────────────────────
SMTP_HOST: Final[str] = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: Final[int] = int(os.getenv("SMTP_PORT", 465))  # 465 → SSL
SMTP_USER: Final[str | None] = os.getenv("SMTP_USER")
SMTP_PASS: Final[str | None] = os.getenv("SMTP_PASS")


# ───────────────────── Helper genérico ─────────────────────
def send_email(
    to_email: str,
    subject: str,
    body: str,
    *,
    html: bool = False,
) -> None:
    """
    Envía un e-mail. Si *html* es True, el cuerpo se interpreta como HTML.

    Muestra trazas en consola para facilitar debug en Render / Uvicorn.
    Nunca lanza excepción; sólo escribe error a stdout.
    """
    msg            = MIMEMultipart("alternative")
    msg["From"]    = SMTP_USER or ""
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html" if html else "plain"))

    try:
        print(f"📧  Enviando → {to_email} | {subject}")
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print("✅  Envío OK")
    except Exception as exc:
        print(f"❌  Error enviando correo a {to_email}: {exc}")


# ─────────────────── Correos ya existentes ───────────────────
def send_confirmation_email(user_email: str, confirmation_code: str) -> None:
    subject = "Confirmación de Email - Registro con CV"
    body = (
        "Hola,\n\n"
        "Para confirmar tu cuenta, hacé clic en el siguiente enlace:\n"
        f"https://fapmendoza.online/cv/confirm?code={confirmation_code}\n\n"
        "Si no solicitaste este registro, ignorá este mensaje.\n\n"
        "Saludos,\nEl equipo de FAP Mendoza"
    )
    send_email(user_email, subject, body)


def send_credentials_email(user_email: str, username: str, password: str) -> None:
    subject = "Bienvenido a la Plataforma - Tus Credenciales"
    body = (
        f"Hola {username},\n\n"
        "Tu cuenta ha sido creada exitosamente.\n\n"
        f"Usuario: {user_email}\n"
        f"Contraseña temporal: {password}\n\n"
        "Por favor, iniciá sesión y cambiá la contraseña.\n\n"
        "Saludos,\nEl equipo de FAP Mendoza"
    )
    send_email(user_email, subject, body)


# ─────────────── NUEVO: notificación de matching ───────────────
def send_match_email(
    email: str,
    name: str,
    title: str,
    description: str,
    score: float,
    apply_url: str | None = None,
) -> None:
    """
    Notifica al candidato que una oferta coincide con su perfil.
    No rompe firmas existentes; simplemente agrega funcionalidad.
    """
    percent   = f"{score * 100:.1f}%"
    apply_url = (
        apply_url
        or f"https://fapmendoza.online/apply?title={title.replace(' ', '%20')}"
    )

    subject = f"¡Nueva oferta para vos! ({percent} de coincidencia)"
    body = (
        f"Hola {name},<br><br>"
        "Encontramos una oferta que se ajusta muy bien a tu perfil:<br><br>"
        f"<strong>{title}</strong><br>"
        f"{description}<br><br>"
        f"<em>Coincidencia: {percent}</em><br><br>"
        f'<a href="{apply_url}">Postularme ahora</a><br><br>'
        "Saludos,<br>Equipo FAP Mendoza"
    )

    send_email(email, subject, body, html=True)
