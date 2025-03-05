import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))  # Usamos 465 para SSL
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

def send_email(to_email, subject, body):
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        print(f"🔹 Enviando email a {to_email} con asunto: {subject}...")

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())

        print(f"✅ Email enviado correctamente a {to_email}")

    except Exception as e:
        print(f"❌ Error enviando email: {e}")

def send_confirmation_email(user_email, confirmation_code):
    subject = "Confirmación de Email - Registro con CV"
    body = (
        f"Hola,\n\n"
        f"Para confirmar tu cuenta, haz clic en el siguiente enlace:\n"
        f"http://fapmendoza.online/cv/confirm?code={confirmation_code}\n\n"
        f"Si no solicitaste este registro, puedes ignorar este mensaje.\n\n"
        f"Saludos,\nEl equipo de FAP Mendoza"
    )
    send_email(user_email, subject, body)

def send_credentials_email(user_email, username, password):
    subject = "Bienvenido a la Plataforma - Tus Credenciales"
    body = (
        f"Hola {username},\n\n"
        f"Tu cuenta ha sido creada exitosamente.\n\n"
        f"📌 **Usuario: {user_email}\n"
        f"🔑 **Contraseña temporal: {password}\n\n"
        f"Por favor, inicia sesión y cambia tu contraseña.\n\n"
        f"Saludos,\nEl equipo de FAP Mendoza"
    )
    send_email(user_email, subject, body)
