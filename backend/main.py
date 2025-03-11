# backend/main.py
from fastapi import FastAPI
from auth import router as auth_router  # Importamos el router correctamente

app = FastAPI()

# Incluir el router con el prefijo "/auth"
app.include_router(auth_router, prefix="/auth", tags=["auth"])

# Verificación rápida: Mostrar en logs si las rutas se cargaron
@app.on_event("startup")
async def startup_event():
    for route in app.routes:
        print(f"✅ Ruta cargada: {route.path}")
