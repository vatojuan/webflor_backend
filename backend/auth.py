# backend/auth.py
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext

# Configuración del JWT (cambia la SECRET_KEY por una segura y guárdala de forma adecuada)
SECRET_KEY = "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"  
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

router = APIRouter()

# Usuario administrador ficticio (puedes integrarlo con tu base de datos)
fake_admin_db = {
    "support@fapmendoza.com": {
        "username": "support@fapmendoza.com",
        # Contraseña "F4pm3nd024!!" hasheada con bcrypt
        "hashed_password": "$2b$12$hsu3vKp7yG3TP.GfTFSvQeTSlIykZjZtVhRcn5ugNJ7te0x3iO8S2",
        "role": "admin"
    }
}

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta if expires_delta else timedelta(minutes=15))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

@router.post("/auth/admin-login")
async def admin_login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = fake_admin_db.get(form_data.username)
    if not user:
        raise HTTPException(status_code=400, detail="Usuario o contraseña incorrectos")
    if not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(status_code=400, detail="Usuario o contraseña incorrectos")
    
    access_token = create_access_token(
        data={"sub": user["username"], "role": user["role"]},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer"}
