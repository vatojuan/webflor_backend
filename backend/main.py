from fastapi import FastAPI
from backend.auth import router as auth_router

app = FastAPI()

# Registrar el router de administración con prefijo "/auth"
app.include_router(auth_router, prefix="/auth", tags=["admin"])

# (Opcional) Aquí podrías incluir otros endpoints o lógica propia de la API administrativa

# Evento de arranque: listar las rutas cargadas (para depuración)
@app.on_event("startup")
def list_routes():
    for route in app.routes:
        print(f"✅ Ruta cargada: {route.path}")

# Si deseas agregar alguna ruta de prueba, por ejemplo:
@app.get("/admin/protected", tags=["admin"])
def admin_protected():
    return {"message": "Ruta protegida para administradores"}
