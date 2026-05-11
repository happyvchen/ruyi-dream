# Production Runbook

## Scope

This runbook is for MVP single-machine private deployments using Docker Compose, FastAPI, SQLite, RustFS, Gemini, and fal GPT Image-2.

Use it to decide whether an incident is configuration, storage, provider, data, or application code related.

## First Checks

```bash
docker compose ps
curl http://127.0.0.1:8000/api/health
docker compose logs --tail=200 backend
docker compose logs --tail=200 rustfs
```

Expected `/api/health` response is intentionally non-sensitive:

```json
{
  "ok": true,
  "storage": "configured",
  "geminiConfigured": true,
  "imageProviderConfigured": true
}
```

## Severity

| Level | Impact | Examples | Action |
| --- | --- | --- | --- |
| SEV-1 | Production blocked or data at risk | Backend cannot start, SQLite corruption, RustFS data inaccessible | Stop writes, preserve logs, escalate to engineering immediately |
| SEV-2 | Core generation flow degraded | fal failures, Gemini failures, upload failures for all users | Triage provider/config/storage, escalate if not fixed in 30 minutes |
| SEV-3 | Partial or recoverable issue | One SKU fails, one run stuck, preview URL not reachable from one browser | Use this runbook, collect affected IDs, escalate if repeated |

## Backend Will Not Start

Check:

```bash
docker compose logs --tail=200 backend
docker compose config
```

Common causes:

- Missing `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY`.
- `APP_ENV=production` with placeholder or weak RustFS credentials.
- `APP_ENV=production` without `APP_GATE_PASSWORD` / `APP_GATE_PASSWORD_HASH` and without `APP_TRUST_REVERSE_PROXY_AUTH=true`.
- Built-in app gate configured without `APP_SESSION_SECRET`.
- RustFS not ready yet; Compose should restart backend automatically.
- SQLite path is not writable under `deployment/app-data/backend`.

Recovery:

1. Fix `.env`.
2. Run `docker compose up -d backend`.
3. Confirm `curl http://127.0.0.1:8000/api/health`.

## Login or API Returns 401

The browser app uses the built-in password gate when `APP_GATE_PASSWORD` or `APP_GATE_PASSWORD_HASH` is configured. Login writes a signed `HttpOnly` session cookie; all business `/api/*` routes require that cookie.

Check:

```bash
curl http://127.0.0.1:8000/api/auth/session
```

For scripts or ops calls, `APP_API_ACCESS_TOKEN` is an optional fallback. If configured, requests can use one of:

```text
X-Ruyi-Access-Token: <token>
Authorization: Bearer <token>
```

Changing `APP_GATE_PASSWORD`, `APP_GATE_PASSWORD_HASH`, `APP_SESSION_SECRET`, or `APP_API_ACCESS_TOKEN` only requires restarting the backend:

```bash
docker compose up -d backend
```

If an external reverse proxy handles authentication instead, production `.env` must explicitly set:

```env
APP_TRUST_REVERSE_PROXY_AUTH=true
APP_GATE_PASSWORD=
APP_GATE_PASSWORD_HASH=
```

## Upload Fails

Symptoms:

- UI reports image format, file size, or pixel limit errors.
- Backend logs show upload validation failures.

Checks:

```bash
docker compose logs --tail=200 backend
```

Current limits:

- JPG / PNG / WebP only.
- Default max size: `APP_MAX_UPLOAD_IMAGE_BYTES=15728640`.
- Default max pixels: `APP_MAX_UPLOAD_IMAGE_PIXELS=25000000`.
- Default max garment images per SKU: `APP_MAX_GARMENT_IMAGES_PER_SKU=20`.
- Default max head references per model: `APP_MAX_HEAD_REFERENCES_PER_PROFILE=20`.

Recovery:

1. Ask the operator to retry with valid images.
2. If limits need adjustment, update `.env` and restart backend.
3. If DB or storage errors occur during upload, backend should clean up newly written objects; check logs if orphan cleanup failed.

## Gemini Planning Fails

Symptoms:

- Shot plan remains `FAILED`.
- UI says default plan generation failed.
- `/api/health` shows `geminiConfigured=false`.

Checks:

```bash
docker compose logs --tail=200 backend | grep -i gemini
```

Recovery:

1. Confirm `GEMINI_API_KEY` is present in `.env`.
2. Confirm outbound network access to Gemini.
3. Retry plan creation from the UI.
4. If repeated, preserve the SKU id and backend logs for engineering.

## fal / GPT Image-2 Generation Fails

Symptoms:

- Generation image status becomes `FAILED`.
- Runs stay in active status for too long.
- `/api/health` shows `imageProviderConfigured=false`.

Checks:

```bash
docker compose logs --tail=300 backend | grep -Ei "fal|generation|provider"
```

Recovery:

1. Confirm `FAL_KEY` is present in `.env`.
2. Confirm outbound network access to fal queue URLs.
3. Retry individual failed images from the UI.
4. If a run is stuck, record `generation_run.id` and affected `generation_images.id`.

Known limitation:

- Claim/lock, cancellation, timeout, and retry ceilings are still open production-readiness work. Do not run multiple backend replicas until that is fixed.

## RustFS Or Presigned URLs Fail

Symptoms:

- Uploaded images are missing.
- Browser cannot preview images.
- Backend logs contain S3 signature or connection errors.

Checks:

```bash
docker compose ps rustfs
docker compose logs --tail=200 rustfs
curl http://127.0.0.1:9000
```

Common causes:

- `PUBLIC_S3_URL` is not reachable from the browser.
- Server clock skew causes S3 signing errors.
- RustFS credentials changed without matching existing data/config.
- Bucket data missing from `deployment/rustfs/data`.

Recovery:

1. Fix `PUBLIC_S3_URL` to the browser-reachable address.
2. Restart backend after credential changes.
3. Restore RustFS data and SQLite from the same backup point if objects are missing.

## SQLite Problems

Symptoms:

- Backend logs show SQLite errors.
- Lists or details fail while object storage is healthy.

Checks:

```bash
ls -lh deployment/app-data/backend
sqlite3 deployment/app-data/backend/app.sqlite3 "PRAGMA integrity_check;"
```

Recovery:

1. Stop backend to prevent writes:

```bash
docker compose stop backend
```

2. Copy the database before attempting repair:

```bash
cp deployment/app-data/backend/app.sqlite3 deployment/app-data/backend/app.sqlite3.incident-copy
```

3. Restore the latest matching SQLite and RustFS backup.
4. Restart backend and verify health.

Escalate to engineering if integrity check is not `ok`.

## Backup And Restore

Backup both application metadata and objects together:

```bash
docker compose stop backend
tar -czf backup-app-data-$(date +%Y%m%d-%H%M%S).tgz deployment/app-data/backend
tar -czf backup-rustfs-data-$(date +%Y%m%d-%H%M%S).tgz deployment/rustfs/data
docker compose start backend
```

Restore both from the same point in time. Restoring only one side can produce broken history or missing objects.

## Escalation Packet

When escalating, include:

- Incident severity and start time.
- Exact SKU id, shot plan id, generation run id, or image id.
- The last 300 backend log lines.
- The last 200 RustFS log lines if media is involved.
- Current `.env` with secrets redacted.
- Whether this is single backend replica or multiple replicas.
