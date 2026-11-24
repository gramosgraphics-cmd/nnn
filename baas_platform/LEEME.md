# LEEME — baas_platform en un teléfono Android (Termux)

Convierte un teléfono Android viejo en un servidor **Backend-as-a-Service**: API REST, auth, realtime,
storage, funciones y panel de control, todo corriendo en el propio teléfono con **Termux**.

> ¿Primero quieres probarlo en tu PC con Visual Studio Code? Lee **[LEEME-PC.md](LEEME-PC.md)**.

## Qué cambió respecto a la versión para PC

| Componente | Antes (Docker Compose) | Ahora (Android / Termux) |
|---|---|---|
| **Docker Compose** | Levantaba todo en contenedores | **Eliminado** (Android no lo soporta). Lo sustituyen `scripts/*.sh`. |
| **PostgreSQL** | Contenedor `postgres:16` | Paquete nativo de Termux (`pkg install postgresql`), instalado y configurado por `scripts/install.sh` (ajustado a poca RAM, escucha solo en `127.0.0.1`). |
| **Redis** | Contenedor (opcional) | **Eliminado.** Un solo teléfono = una sola instancia: el realtime ya funciona en memoria y el bloqueo de backups usa un *advisory lock* de PostgreSQL. |
| **MinIO / S3** | Contenedor + `boto3` | **Almacenamiento local**: los archivos se guardan como archivos normales en `storage_data/` (mismas políticas, URLs firmadas y límites). S3/MinIO sigue disponible como opción: `STORAGE_BACKEND=s3` + `pip install -e ".[s3]"`. |

> **¿Por qué no SQLite?** El backend se apoya en funciones propias de PostgreSQL: un *schema* y roles de BD por proyecto,
> **RLS** (seguridad por filas), `LISTEN/NOTIFY` para el realtime y `pg_dump` para los backups. SQLite no tiene nada de eso,
> así que usarlo significaría reescribir la mitad de la plataforma y perder el aislamiento entre proyectos. PostgreSQL en Termux funciona bien y mantiene todo igual.

---

## 0. Requisitos

- Teléfono Android 7+ con **≥ 3 GB de RAM** libres de preferencia y **≥ 5 GB** de almacenamiento.
- **Termux** instalado desde **F-Droid** o desde GitHub (la versión de Google Play está abandonada y no sirve).
- Conexión Wi-Fi (para instalar y para que otros dispositivos lleguen al servidor).
- Opcional: **Termux:Boot** (F-Droid) para arrancar solo al encender el teléfono.

---

## 1. Copiar el proyecto al teléfono

Elige una:

**a) Con git** (dentro de Termux):

```bash
pkg install -y git
git clone URL-DE-TU-REPOSITORIO ~/baas_platform
cd ~/baas_platform
```

**b) Desde el ZIP:** copia `baas_platform.zip` a la carpeta *Descargas* del teléfono y, en Termux:

```bash
termux-setup-storage            # acepta el permiso de archivos
pkg install -y unzip
unzip ~/storage/downloads/baas_platform.zip -d ~
cd ~/baas_platform
```

> Deja el proyecto en `~` (almacenamiento interno de Termux), **no** en `~/storage/...`: la tarjeta/almacenamiento compartido no permite permisos de archivo ni enlaces y PostgreSQL fallaría.

---

## 2. Instalar todo (un solo comando)

```bash
bash scripts/install.sh
```

Hace, en orden: instala `python`, `postgresql`, compiladores y `rust` → crea el entorno virtual `.venv` e instala las
dependencias → genera `.env` con **JWT_SECRET, contraseña de PostgreSQL y contraseña del panel aleatorias** (te las muestra una vez) →
inicializa el clúster de PostgreSQL → crea la base `baas` → ejecuta las migraciones.

⏱ **La primera vez tarda 10–30 minutos** porque `pydantic-core`, `bcrypt` y `asyncpg` se compilan en el teléfono. Mantén Termux abierto y el teléfono cargando. Es seguro volver a ejecutarlo si se corta.

---

## 3. Arrancar, parar y vigilar

```bash
bash scripts/start.sh      # arranca PostgreSQL + API (+ workers de webhooks y backups) en segundo plano
bash scripts/status.sh     # estado, URL del panel y espacio usado
bash scripts/stop.sh       # apagado ordenado de la API y de PostgreSQL
bash scripts/start.sh --fg # alternativa: en primer plano, para ver los logs en vivo
```

Al arrancar verás la dirección, por ejemplo `http://192.168.1.50:8000/dashboard`. Ábrela desde cualquier dispositivo de la misma red Wi-Fi.
Usuario del panel: `admin@example.com`; contraseña: la que mostró la instalación (también está en `.env` → `PLATFORM_ADMIN_PASSWORD`).

| Qué | Dirección |
|---|---|
| Panel de control (Studio) | `http://IP-DEL-TELEFONO:8000/dashboard` |
| Documentación interactiva | `http://IP-DEL-TELEFONO:8000/docs` |
| Estado | `http://IP-DEL-TELEFONO:8000/health` |

Logs: `logs/api.log` y `logs/postgres.log`.

---

## 4. Que el servidor sobreviva (Android intenta matar los procesos)

1. **Ahorro de batería:** *Ajustes → Apps → Termux → Batería → Sin restricciones* (el nombre varía según la marca).
2. `start.sh` ya ejecuta `termux-wake-lock` para que la CPU no se duerma con la pantalla apagada. También puedes tocar **“Acquire wakelock”** en la notificación de Termux.
3. **Arranque automático al encender:** instala **Termux:Boot**, ábrelo una vez y ejecuta `bash scripts/enable_boot.sh`.
4. **IP fija:** reserva una IP para el teléfono en tu router (reserva DHCP), o la dirección cambiará.
5. **Calor/batería:** un teléfono 24/7 enchufado puede hinchar la batería. Si se puede, quítala o limita la carga al 80 %.
6. **Acceso desde Internet:** no abras el puerto en el router sin HTTPS. Lo más seguro es un túnel (Cloudflare Tunnel, Tailscale, etc.).

---

## 5. Dónde queda cada cosa

| Qué | Dónde | Notas |
|---|---|---|
| Base de datos | `$PREFIX/var/lib/postgresql` | Solo accesible desde el teléfono (`127.0.0.1`). |
| Archivos subidos (Storage) | `~/baas_platform/storage_data/<proyecto>/<bucket>/...` | Archivos normales; cámbialo con `STORAGE_DIR`. |
| Backups automáticos | `~/baas_platform/backups/` | `pg_dump` cada `BACKUP_INTERVAL_HOURS`. |
| Configuración | `~/baas_platform/.env` | Reinicia (`stop.sh` + `start.sh`) tras editarlo. |

**Backup completo manual** (base de datos + archivos subidos en un `.tar.gz`), por ejemplo hacia *Descargas* para copiarlo a tu PC:

```bash
bash scripts/backup_now.sh ~/storage/downloads
```

> Los backups automáticos respaldan la base de datos, **no** los archivos de `storage_data/`; para eso usa `backup_now.sh`. Un servidor en un teléfono viejo **necesita copias fuera del teléfono**.

---

## 6. Tu primer proyecto, de principio a fin

1. En el panel pulsa **Crear proyecto**, ponle un nombre y ve a la sección **Proyecto y claves** (icono de llave).
2. Copia las dos claves: **anon** (pública, para tu app) y **service_role** (secreta, solo servidor: se salta la seguridad por filas).
3. Abre **`requests.http`** en VS Code de tu PC (extensión *REST Client*), cambia `@url` por la dirección del teléfono, pega tus claves arriba y pulsa **Send Request** sobre cada bloque, en orden. (O usa `curl` desde Termux con `http://127.0.0.1:8000`.) Hace esto:
   - registra e inicia sesión de un usuario,
   - crea una tabla `tareas`, activa **RLS** y una política (cada usuario ve solo lo suyo),
   - inserta, lee con filtros/paginación y actualiza,
   - activa realtime, crea un bucket, despliega e invoca una función y registra un webhook.
4. Vuelve al panel: en **Tablas** verás la tabla y sus filas; en **Resumen**, las métricas de uso.

### Probar realtime desde la consola del navegador

Pega esto en la consola (F12) de cualquier pestaña, con tus claves (tras `POST /realtime/v1/enable/tareas`):

```js
const ws = new WebSocket("ws://IP-DEL-TELEFONO:8000/realtime/v1/websocket?apikey=TU_ANON&token=TOKEN_DE_USUARIO");
ws.onmessage = (e) => console.log(JSON.parse(e.data));
ws.onopen = () => ws.send(JSON.stringify({ event: "subscribe", channel: "db:tareas" }));
```

Al insertar o modificar filas en `tareas` verás llegar los eventos. Los canales que no empiezan por `db:` sirven para broadcast y presencia entre clientes.

### Importante sobre las políticas RLS

Cada proyecto tiene sus propios roles de base de datos aislados. Por eso, en las políticas **no uses `TO authenticated`**: escribe la condición con las funciones `auth.uid()` y `auth.role()`:

```sql
ALTER TABLE tareas ENABLE ROW LEVEL SECURITY;
CREATE POLICY propias ON tareas USING (owner = auth.uid()) WITH CHECK (owner = auth.uid());
```

En el panel, una tabla **sin RLS** aparece con una etiqueta naranja: cualquiera con la clave `anon` podría leerla y escribirla.

---

## 7. Pruebas automáticas (opcional)

```bash
source .venv/bin/activate
pip install -e ".[dev]"
TEST_DATABASE_URL="postgresql+asyncpg://postgres:TU_CLAVE_DB@127.0.0.1:5432/baas_test" pytest
```

`TU_CLAVE_DB` es la contraseña dentro de `DATABASE_URL` en `.env`. Las pruebas crean y borran su propia base `baas_test` y usan una carpeta temporal para el storage.

---

## 8. Workers

Por defecto (`RUN_WORKERS_IN_PROCESS=true`) los workers de **webhooks** y **backups** corren dentro de la API: es lo ideal para un teléfono (un solo proceso).
Si prefieres separarlos: `RUN_WORKERS_IN_PROCESS=false` y, en otras sesiones de Termux con `.venv` activo, `python -m app.workers.webhook_worker` y `python -m app.workers.backup_worker`.

---

## 9. Estructura del proyecto

```text
baas_platform/
├── scripts/       install.sh · start.sh · stop.sh · status.sh · backup_now.sh · enable_boot.sh
├── config/        settings.py (variables de entorno) y logging.py
├── migrations/    Alembic: env.py y versions/
├── app/
│   ├── main.py        punto de entrada (API + WebSocket + dashboard)
│   ├── core/          seguridad (JWT, hashing), tenancy (aislamiento por proyecto), base de datos
│   ├── auth/          signup, login, refresh, OAuth y contexto RLS
│   ├── database_api/  API REST dinámica, introspección y SQL runner
│   ├── realtime/      WebSocket, gestor de canales y escucha LISTEN/NOTIFY
│   ├── storage/       buckets, políticas, URLs firmadas y backends (local / S3)
│   ├── functions/     funciones Python aisladas y webhooks
│   ├── dashboard/     Studio: API de administración + interfaz (static/)
│   └── workers/       backups y entrega de webhooks con reintentos
├── tests/ · pyproject.toml · .env.example · requests.http
```

---

## 10. Solución de problemas

| Síntoma | Solución |
|---|---|
| `pkg` dice que no encuentra paquetes / errores de repositorio | `termux-change-repo` y elige otro espejo; luego `pkg update`. |
| La compilación de `pydantic-core`/`bcrypt` falla por memoria (`Killed`) | Cierra otras apps, deja el teléfono cargando y repite `bash scripts/install.sh`; si persiste, `CARGO_BUILD_JOBS=1 bash scripts/install.sh`. |
| Error de `ANDROID_API_LEVEL` al compilar | `export ANDROID_API_LEVEL=$(getprop ro.build.version.sdk)` y repite la instalación. |
| `could not create lock file` / PostgreSQL no arranca | Mira `logs/postgres.log`. Si quedó un `postmaster.pid` viejo tras apagarse el teléfono, `bash scripts/stop.sh` y `bash scripts/start.sh`. |
| `La base de datos no está migrada` | `source .venv/bin/activate && alembic upgrade head` (o simplemente `scripts/start.sh`, que migra solo). |
| No abre desde otro dispositivo | Misma red Wi-Fi, usa la IP que muestra `status.sh`, sin VPN activa en el teléfono, y `HOST=0.0.0.0` en `.env`. |
| El servidor “desaparece” con la pantalla apagada | Revisa la sección 4 (sin restricciones de batería + wake lock). |
| Backup `failed` con “No se encontró pg_dump” | `pkg install postgresql` (incluye `pg_dump`). |
| Cambié `.env` y no se nota | `bash scripts/stop.sh && bash scripts/start.sh`. |
| `Storage no disponible` | Comprueba que `STORAGE_DIR` existe y es escribible (por defecto `./storage_data`). |
| `Un objeto no puede ser a la vez archivo y carpeta` (`path_conflict`) | Con almacenamiento local no puedes tener `a/b` y `a/b/c` como objetos a la vez; usa rutas distintas. |

---

## 11. Antes de exponerlo a otras personas

- `scripts/install.sh` ya genera secretos aleatorios; **no** subas `.env` a git ni lo compartas.
- Pon `ENVIRONMENT=production` (la app se niega a arrancar con el secreto de ejemplo) y `WEBHOOKS_ALLOW_PRIVATE_NETWORKS=false` (evita que un webhook apunte a tu red interna).
- Pon HTTPS delante de la API (túnel o proxy inverso) y restringe `CORS_ORIGINS`.
- Activa **RLS** en todas tus tablas y no expongas jamás la clave `service_role` en un navegador o app móvil.
- Hardware de teléfono: sin redundancia ni UPS. Haz `backup_now.sh` con regularidad y cópialo fuera del dispositivo.

### Qué es y qué no es (alcance honesto)

Esto es una **base funcional y extensible**, no un clon completo de Supabase. Está implementado: auth con refresh tokens rotativos y OAuth (GitHub/Google), RLS por rol aislado por proyecto, API REST dinámica con filtros, realtime con LISTEN/NOTIFY respetando RLS, storage con políticas y URLs firmadas, funciones Python, webhooks firmados con reintentos, backups lógicos y panel de métricas.

No incluye (requieren infraestructura propia): recuperación a un punto en el tiempo (PITR), réplicas de lectura, SSO/SAML, ni un sandbox de seguridad para funciones (corren en un subproceso con límites de tiempo/memoria: despliega solo código en el que confíes). El SQL runner admite **una sentencia por petición**.
