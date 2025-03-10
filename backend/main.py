# backend/main.py
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from auth import router as auth_router

app = FastAPI()

# Incluir las rutas de autenticación
app.include_router(auth_router)

# Definir el esquema de OAuth2 para extraer el token
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, "TU_SECRET_KEY_AQUI", algorithms=["HS256"])
        username: str = payload.get("sub")
        role: str = payload.get("role")
        if username is None or role != "admin":
            raise HTTPException(status_code=401, detail="No autorizado")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido")
    return username

# Ejemplo de ruta protegida para administradores
@app.get("/admin/protected")
def admin_protected_route(current_user: str = Depends(get_current_admin)):
    return {"message": f"Hola, administrador {current_user}"}
