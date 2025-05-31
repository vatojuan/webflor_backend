# app/token_utils.py
"""
Módulo para generación y verificación de tokens JWT de confirmación de email.
Usa PyJWT (o compatible) y lee la clave secreta y expiración desde variables de entorno.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
import jwt
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY: str = os.getenv("SECRET_KEY", "supersecreto")
EXPIRATION_MINUTES: int = int(os.getenv("EXPIRATION_MINUTES", "30"))
ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")


def generate_confirmation_token(email: str) -> str:
    """
    Crea un JWT con el email como sujeto y fecha de expiración.
    """
    expire_at = datetime.now(timezone.utc) + timedelta(minutes=EXPIRATION_MINUTES)
    payload = {
        "sub": email,
        "exp": expire_at,
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token


def verify_confirmation_token(token: str) -> str | None:
    """
    Verifica y decodifica el JWT de confirmación. Retorna el email o None si inválido/expirado.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
