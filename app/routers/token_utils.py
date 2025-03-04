
import jwt
import os
from datetime import datetime, timedelta

SECRET_KEY = os.getenv("SECRET_KEY", "supersecreto")
EXPIRATION_MINUTES = 30  # Expira en 30 minutos

def generate_confirmation_token(email):
    expiration = datetime.utcnow() + timedelta(minutes=EXPIRATION_MINUTES)
    payload = {"email": email, "exp": expiration}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def verify_confirmation_token(token):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload["email"]
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
