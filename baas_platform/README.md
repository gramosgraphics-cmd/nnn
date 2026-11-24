# baas_platform

Alternativa abierta y extensible a Supabase construida con **FastAPI, SQLAlchemy 2 / asyncpg, Pydantic v2 y Alembic**, pensada para correr en un **teléfono Android viejo con Termux** (PostgreSQL nativo + almacenamiento local; S3/MinIO opcional).

> **¿Quieres ejecutarlo en tu teléfono?** Sigue la guía paso a paso en español: **[LEEME.md](LEEME.md)** (en PC con VS Code: **[LEEME-PC.md](LEEME-PC.md)**).

## Qué incluye

| Módulo | Ruta base | Resumen |
|---|---|---|
| Auth + RLS | `/auth/v1` | signup, login, refresh (rotativo), logout, usuario, OAuth GitHub/Google. Cada petición se ejecuta con el rol `anon`, `authenticated` o `service_role` del proyecto y los claims JWT (`auth.uid()`). |
| Database API | `/rest/v1` | CRUD dinámico estilo PostgREST (`?col=eq.x&order=id.desc&limit=10`), introspección (`/_meta/tables`) y SQL runner (`/_sql`, solo `service_role`). |
| Realtime | `/realtime/v1` | WebSocket con cambios de tablas (LISTEN/NOTIFY, filtrados por RLS), broadcast y presencia. |
| Storage | `/storage/v1` | Buckets públicos/privados, subida/descarga en carpeta local (o S3/MinIO), límites de tamaño y MIME, URLs firmadas. |
| Functions | `/functions/v1`, `/webhooks/v1` | Funciones Python en subproceso aislado, funciones tipo webhook y webhooks de eventos de base de datos con firma HMAC y reintentos. |
| Studio | `/dashboard` | Panel oscuro: proyectos, claves (rotación), métricas, explorador de tablas, editor SQL, usuarios y backups. |
| Workers | — | `webhook_worker` (entregas con backoff) y `backup_worker` (`pg_dump` con retención). |

## Arquitectura multi-tenant

Un **schema de PostgreSQL por proyecto** (`proj_<ref>`) y **tres roles de BD por proyecto** (`anon`, `authenticated`, `service_role`). Cada rol solo tiene permisos sobre su propio schema, de modo que ni el SQL arbitrario de un proyecto puede leer otro proyecto ni el plano de control (`platform`). Las claves `anon`/`service_role` son JWT firmados con un secreto distinto por proyecto.

## Arranque rápido (Android / Termux)

```bash
bash scripts/install.sh   # paquetes, PostgreSQL, .env con secretos aleatorios y migraciones
bash scripts/start.sh     # PostgreSQL + API + workers → http://IP-DEL-TELEFONO:8000/dashboard
bash scripts/status.sh    # estado  ·  bash scripts/stop.sh para detener
```

En un PC con PostgreSQL 14+ ya instalado: `pip install -e ".[dev]"`, `cp .env.example .env`, `alembic upgrade head`, `uvicorn app.main:app` y `pytest`.

Detalle completo, solución de problemas y checklist de seguridad en **[LEEME.md](LEEME.md)**.
