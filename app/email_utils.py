# app/email_utils.py
"""
Módulo centralizado para todas las comunicaciones por correo electrónico de FAP Mendoza.

Este módulo proporciona un motor de envío de email robusto y funciones de alto nivel
para cada tipo de notificación transaccional del sistema.

- Utiliza plantillas HTML para un aspecto profesional.
- Maneja conexiones SMTP seguras (SSL y STARTTLS).
- Incluye un sistema de alertas para notificar a los administradores sobre errores críticos.
- Centraliza toda la lógica de comunicación para facilitar el mantenimiento.
"""
from __future__ import annotations

import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Final, Dict

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ───────────────────── Configuración Central ─────────────────────
SMTP_HOST: Final[str] = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: Final[int] = int(os.getenv("SMTP_PORT", 587))
SMTP_USER: Final[str | None] = os.getenv("SMTP_USER")
SMTP_PASS: Final[str | None] = os.getenv("SMTP_PASS")
SMTP_TIMEOUT: Final[int] = int(os.getenv("SMTP_TIMEOUT", 20))

# Email del administrador para recibir alertas del sistema.
ADMIN_EMAIL: Final[str | None] = os.getenv("ADMIN_EMAIL")

# ───────────────────── Motor de Envío de Email ─────────────────────

def send_email(to_email: str, subject: str, body: str, *, html: bool = True) -> bool:
    """
    Envía un correo electrónico de manera robusta.

    - Se conecta de forma segura usando STARTTLS (puerto 587) o SSL (puerto 465).
    - Lanza excepciones en caso de error para que el llamador pueda manejarlas.
    - Devuelve True si el envío fue exitoso.
    """
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS]):
        logging.error("La configuración SMTP (HOST, PORT, USER, PASS) está incompleta.")
        raise ValueError("Configuración SMTP incompleta.")

    msg = MIMEMultipart("alternative")
    msg["From"] = f"FAP Mendoza <{SMTP_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html" if html else "plain", "utf-8"))

    logging.info(f"📧  Intentando enviar email a: {to_email} | Asunto: {subject}")

    try:
        # Se elige el tipo de conexión basado en el puerto
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
            server.starttls()

        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
        server.quit()

        logging.info(f"✅  Email enviado exitosamente a {to_email}")
        return True
    except smtplib.SMTPException as e:
        logging.error(f"❌  Error de SMTP al enviar a {to_email}: {e}")
        raise  # Re-lanza la excepción para que el código que llama sepa del fallo
    except Exception as e:
        logging.error(f"❌  Error inesperado al enviar correo a {to_email}: {e}")
        raise

# ───────────────── Funciones de Notificación de Alto Nivel ─────────────────

def send_confirmation_email(user_email: str, confirmation_code: str):
    """(1) Envía el email para que el usuario confirme su cuenta."""
    subject = "Confirma tu email para activar tu cuenta en FAP Mendoza"
    body = (
        f"Hola,<br><br>"
        f"¡Gracias por registrarte! Para completar tu registro y activar tu cuenta, por favor haz clic en el siguiente enlace:<br><br>"
        f'<a href="https://fapmendoza.online/cv/confirm?code={confirmation_code}" style="background-color: #007bff; color: white; padding: 12px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Activar mi cuenta</a><br><br>'
        f"Si no solicitaste este registro, puedes ignorar este mensaje.<br><br>"
        f"Saludos,<br>El equipo de FAP Mendoza"
    )
    send_email(user_email, subject, body)

def send_credentials_email(user_email: str, name: str, password: str):
    """(2) Envía las credenciales de acceso una vez confirmada la cuenta."""
    subject = "¡Bienvenido a FAP Mendoza! Aquí tienes tus credenciales"
    body = (
        f"Hola {name},<br><br>"
        f"¡Tu cuenta ha sido creada y activada exitosamente!<br><br>"
        f"Puedes iniciar sesión con los siguientes datos:<br>"
        f"<ul>"
        f"<li><strong>Usuario:</strong> {user_email}</li>"
        f"<li><strong>Contraseña temporal:</strong> {password}</li>"
        f"</ul>"
        f"Te recomendamos cambiar tu contraseña después de iniciar sesión por primera vez.<br><br>"
        f"Saludos,<br>El equipo de FAP Mendoza"
    )
    send_email(user_email, subject, body)

def send_match_notification(user_email: str, context: Dict[str, str]):
    """(3) Notifica a un candidato que su perfil coincide con una oferta."""
    subject = f"¡{context.get('applicant_name', '')}, encontramos una nueva oportunidad para ti!"
    body = (
        f"Hola, {context.get('applicant_name', '')}.<br><br>"
        f"Basado en tu perfil, hemos encontrado una oferta laboral que tiene una alta compatibilidad contigo (<strong>{context.get('score', 'N/A')}</strong>).<br><br>"
        f"<strong>Puesto:</strong> {context.get('job_title', 'No especificado')}<br><br>"
        f"Creemos que es una excelente oportunidad para tu carrera. Si te interesa, puedes postularte directamente desde aquí:<br><br>"
        f'<a href="{context.get("apply_link", "#")}" style="background-color: #007bff; color: white; padding: 12px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Ver oferta y postularme</a><br><br>'
        f"Este enlace es único para ti y estará activo durante 30 días. ¡Mucha suerte!<br><br>"
        f"Saludos cordiales,<br>El equipo de FAP Mendoza."
    )
    send_email(user_email, subject, body)

def send_proposal_to_employer(employer_email: str, context: Dict[str, str]):
    """(4) Envía la postulación de un candidato al empleador."""
    subject = f"Nueva postulación para la oferta \"{context.get('job_title', '')}\""
    body = (
        f"Hola, {context.get('employer_name', 'equipo de selección')}.<br><br>"
        f"Has recibido una nueva postulación para tu oferta \"<strong>{context.get('job_title', '')}</strong>\".<br><br>"
        f"<strong>Datos del Candidato:</strong>"
        f"<ul>"
        f"<li><strong>Nombre:</strong> {context.get('applicant_name', 'No especificado')}</li>"
        f"<li><strong>Email:</strong> {context.get('applicant_email', 'No especificado')}</li>"
        f"</ul>"
        f"Puedes revisar su CV completo en el siguiente enlace:<br>"
        f'<a href="{context.get("cv_url", "#")}" target="_blank">Ver CV de {context.get("applicant_name", "candidato")}</a><br><br>'
        f"Te recomendamos contactar al candidato a la brevedad posible para continuar con el proceso.<br><br>"
        f"Gracias por utilizar nuestra plataforma.<br>Equipo FAP Mendoza."
    )
    send_email(employer_email, subject, body)

def send_application_confirmation(user_email: str, context: Dict[str, str]):
    """(5) Confirma al candidato que su postulación fue enviada con éxito."""
    subject = f"Recibimos tu postulación para {context.get('job_title', '')}"
    body = (
        f"¡Excelente, {context.get('applicant_name', '')}!<br><br>"
        f"Hemos registrado y enviado correctamente tu postulación para la oferta \"<strong>{context.get('job_title', '')}</strong>\".<br><br>"
        f"El equipo de la empresa ha recibido tu perfil y lo revisará a la brevedad. Si tu perfil avanza en el proceso, se pondrán en contacto directamente contigo.<br><br>"
        f"¡Te deseamos el mayor de los éxitos!<br><br>"
        f"Atentamente,<br>El equipo de FAP Mendoza."
    )
    send_email(user_email, subject, body)

def send_cancellation_warning(user_email: str, context: Dict[str, str]):
    """(6) Advierte al usuario que su postulación se enviará en 5 minutos."""
    subject = f"Tu postulación para {context.get('job_title', '')} está en espera"
    body = (
        f"Hola, {context.get('applicant_name', '')}.<br><br>"
        f"Hemos recibido tu interés en la oferta \"<strong>{context.get('job_title', '')}</strong>\". Tu postulación será enviada a la empresa en 5 minutos.<br><br>"
        f"Si te has postulado por error o cambiaste de opinión, este es el momento para anularla. Puedes hacerlo desde tu panel de usuario.<br><br>"
        f"Pasado este tiempo, la postulación será definitiva y no podrá cancelarse.<br><br>"
        f"Saludos,<br>Equipo FAP Mendoza."
    )
    send_email(user_email, subject, body)

# ───────────────────── Sistema de Alertas Internas ─────────────────────

def send_admin_alert(subject: str, details: str):
    """Envía un email de alerta a los administradores del sistema."""
    if not ADMIN_EMAIL:
        logging.warning("ADMIN_EMAIL no está configurado. No se puede enviar la alerta.")
        return

    full_subject = f"🚨 Alerta FAP Mendoza: {subject}"
    body = (
        f"<h2>Alerta del Sistema</h2>"
        f"<p>Se ha producido un evento que requiere atención:</p>"
        f"<p><strong>Tipo de Alerta:</strong><br>{subject}</p>"
        f"<p><strong>Detalles:</strong><br><pre>{details}</pre></p>"
        f"<p>Por favor, revisa los logs del sistema para obtener más información.</p>"
    )
    try:
        send_email(ADMIN_EMAIL, full_subject, body, html=True)
    except Exception as e:
        logging.error(f"FALLO CRÍTICO: No se pudo enviar la alerta de administrador a {ADMIN_EMAIL}. Error: {e}")
