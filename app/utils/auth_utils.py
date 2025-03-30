from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import os

SECRET_KEY = os.getenv("SECRET_KEY", "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12")
ALGORITHM = os.getenv("ALGORITHM", "HS256")

def get_current_admin(token: str = Depends(OAuth2PasswordBearer(tokenUrl="/auth/admin-login"))):
    print("🔐 TOKEN RECIBIDO:", token)
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        print("✅ PAYLOAD DECODIFICADO:", payload)
        username: str = payload.get("sub")
        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido o sesión expirada (sub no encontrado)"
            )
        return username
    except JWTError as e:
        print("❌ Error al decodificar el token:", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token inválido o sesión expirada: {e}"
        )

    return username
