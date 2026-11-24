# LEEME-PC — Probar baas_platform en tu computadora con Visual Studio Code

Guía para quien **parte de cero**. Sirve para Windows, macOS y Linux. Es el paso previo a llevarlo al teléfono (ver `LEEME.md`).
Docker ya **no** hace falta: ahora solo necesitas Python y PostgreSQL. Los archivos de `scripts/` son solo para Termux; en el PC sigue estos pasos.

---

## 1. Instala estos 3 programas

| Programa | Dónde | Notas |
|---|---|---|
| **Python 3.11 o superior** | https://www.python.org/downloads/ | En Windows marca la casilla **“Add python.exe to PATH”** al instalar. |
| **PostgreSQL 14 o superior** | https://www.postgresql.org/download/ | Durante la instalación te pide una **contraseña para el usuario `postgres`**: usa `postgres` (así no tendrás que tocar nada después). Deja el puerto `5432`. |
| **Visual Studio Code** | https://code.visualstudio.com/ | Es el editor. |

Comprueba en una terminal: `python --version` (en macOS/Linux `python3 --version`). Debe decir 3.11 o más.

---

## 2. Abre el proyecto en VS Code

1. Descomprime `baas_platform_termux.zip`; sale la carpeta `baas_platform`.
2. VS Code → **Archivo → Abrir carpeta…** → elige `baas_platform`. Si pregunta si confías en los autores, pulsa **Sí, confío**.
3. Abajo a la derecha te sugerirá instalar extensiones (Python, Pylance, REST Client…): pulsa **Instalar**.
4. Abre la terminal integrada: menú **Terminal → Nueva terminal**. **Todos los comandos de abajo se escriben ahí.**

---

## 3. Crea la base de datos `baas`

Elige una forma:

- **Con pgAdmin** (se instaló con PostgreSQL): conéctate → clic derecho en *Databases* → **Create → Database…** → nombre `baas` → **Save**.
- **Con SQL Shell (psql)** (también viene con PostgreSQL): pulsa Enter en todo, escribe tu contraseña y ejecuta:
  ```sql
  CREATE DATABASE baas;
  ```

---

## 4. Entorno de Python e instalación

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```
> Si dice que “la ejecución de scripts está deshabilitada”: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, responde `S` y repite.

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

Verás `(.venv)` al inicio de la línea. Ahora:
```bash
pip install --upgrade pip
pip install -e ".[dev]"
```
Luego en VS Code: `Ctrl+Shift+P` → **Python: Select Interpreter** → elige el que dice `.venv`.

---

## 5. Configuración `.env`

**Windows:** `Copy-Item .env.example .env`  ·  **macOS/Linux:** `cp .env.example .env`

- Si pusiste la contraseña `postgres` en el paso 1, **no cambies nada** más que esto: genera un secreto con
  `python -c "import secrets; print(secrets.token_urlsafe(48))"` y pégalo en `.env` como valor de `JWT_SECRET=`.
- Si usaste otra contraseña, cámbiala en la línea `DATABASE_URL=postgresql+asyncpg://postgres:TU_CLAVE@127.0.0.1:5432/baas`.
- Tu usuario del panel está en `PLATFORM_ADMIN_EMAIL` y `PLATFORM_ADMIN_PASSWORD` (por defecto `admin@example.com` / `admin12345`).

---

## 6. Crea las tablas y arranca

```bash
alembic upgrade head
uvicorn app.main:app --reload
```
(O pulsa `F5` en VS Code y elige **API (uvicorn --reload)**.)

Cuando veas `Application startup complete`, abre en el navegador:

| Qué | Dirección |
|---|---|
| **Panel de control** | http://localhost:8000/dashboard |
| Documentación interactiva | http://localhost:8000/docs |
| Estado | http://localhost:8000/health |

Entra con el email y contraseña del paso 5. Para detener: `Ctrl + C` en la terminal.
Los archivos que subas al Storage se guardan en la carpeta `storage_data/` dentro del proyecto.

---

## 7. Tu primer recorrido

1. En el panel pulsa **Crear proyecto** y ve a **Proyecto y claves**. Copia la clave **anon** (pública) y la **service_role** (secreta, nunca en una app/navegador).
2. Abre `requests.http` en VS Code (extensión *REST Client*), pega tus claves arriba y pulsa **Send Request** sobre cada bloque, en orden: registra un usuario, crea una tabla con seguridad por filas, inserta y lee datos, crea un bucket, etc.
3. Vuelve al panel: en **Tablas** verás lo que creaste.

---

## 8. Pruebas automáticas (opcional)

Con la base de datos encendida y el entorno activo:
```bash
pytest
```
Crean y borran su propia base `baas_test` (no tocan `baas`). Deben terminar todas en verde. Si usaste otra contraseña: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:TU_CLAVE@127.0.0.1:5432/baas_test` (en PowerShell: `$env:TEST_DATABASE_URL="..."`).

---

## 9. Problemas comunes

| Síntoma | Solución |
|---|---|
| `alembic` / `uvicorn` “no se reconoce” | El entorno no está activo: repite el paso 4 (debe verse `(.venv)`). |
| `password authentication failed` | La contraseña de `DATABASE_URL` en `.env` no es la que pusiste al instalar PostgreSQL. |
| `database "baas" does not exist` | Crea la base (paso 3). |
| `Connect call failed ... 5432` | PostgreSQL no está encendido: en Windows busca el servicio *postgresql-x64-…* en “Servicios” e inícialo. |
| `La base de datos no está migrada` | Ejecuta `alembic upgrade head`. |
| Cambié `.env` y no se nota | Detén (`Ctrl+C`) y vuelve a arrancar `uvicorn`. |
| `could not create role` / permisos | La app necesita crear roles: usa el usuario `postgres` (superusuario). |
