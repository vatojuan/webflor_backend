# app/email_utils.py
"""
Utilidades de envío de correo para FAP Mendoza.
Mantiene compatibilidad con:
  • send_email()
  • send_confirmation_email()
  • send_credentials_email()
Añade:
  • send_match_email()
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))     # 465 = SSL
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")


# ───────────────────────── Helper genérico ─────────────────────────
def send_email(to_email: str, subject: str, body: str, html: bool = False) -> None:
    """
    Envía un correo.  Si `html=True` el body se interpreta como HTML.
    No retorna nada; loggea éxito / error en consola.
    """
    msg          = MIMEMultipart("alternative")
    msg["From"]  = SMTP_USER
    msg["To"]    = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html" if html else "plain"))

    print(f"🔹 Enviando email a {to_email} — {subject}")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print(f"✅ Email enviado a {to_email}")
    except Exception as e:
        print(f"❌ Error enviando email a {to_email}: {e}")


# ───────────────────────── Correos existentes ─────────────────────────
def send_confirmation_email(user_email: str, confirmation_code: str) -> None:
    subject = "Confirmación de Email - Registro con CV"
    body = (
        "Hola,\n\n"
        "Para confirmar tu cuenta, hacé clic en el siguiente enlace:\n"
        f"http://fapmendoza.online/cv/confirm?code={confirmation_code}\n\n"
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


# ─────────────────────── NUEVO: correo de matching ───────────────────────
def send_match_email(email: str, name: str, title: str,
                     description: str, score: float) -> None:
    """
    Notifica al candidato una oferta que coincide con su perfil.
    Solo agrega funcionalidad; no afecta llamadas existentes.
    """
    percent = f"{score * 100:.1f}%"
    subject = f"¡Nueva oferta para vos ({percent} de coincidencia)!"
    body = (
        f"Hola {name},<br><br>"
        "Encontramos una oferta que se ajusta muy bien a tu perfil:<br><br>"
        f"<strong>{title}</strong><br>"
        f"{description}<br><br>"
        f"<em>Coincidencia: {percent}</em><br><br>"
        "Podés postularte ingresando a tu panel de usuario.<br><br>"
        "Saludos,<br>Equipo FAP Mendoza"
    )
    send_email(email, subject, body, html=True)
