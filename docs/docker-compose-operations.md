# Docker Compose 部署与管理

## 首次部署

```bash
cd /path/to/ruyi-dream
cp .env.example .env
```

编辑 `.env`：

```env
RUSTFS_ACCESS_KEY=<random-access-key>
RUSTFS_SECRET_KEY=<random-secret-key>
APP_ENV=production
APP_GATE_PASSWORD=<random-app-password>
APP_GATE_PASSWORD_HASH=
APP_SESSION_SECRET=<random-session-signing-secret>
APP_SESSION_TTL_SECONDS=43200
APP_SESSION_COOKIE_SECURE=true
APP_API_ACCESS_TOKEN=<optional-ops-script-token>
PUBLIC_BACKEND_URL=http://<server-ip-or-domain>:8000
PUBLIC_S3_URL=http://<server-ip-or-domain>:9000
BACKEND_CORS_ORIGINS=http://<server-ip-or-domain>:5173
GEMINI_API_KEY=<optional-but-required-for-planning>
FAL_KEY=<required-for-real-image-generation>
```

根目录 `.env` 是 Compose、后端裸跑和本地联调共同使用的主配置源。不要再维护一份 `backend/.env`；如需后端本机临时覆盖，使用不提交 Git 的 `backend/.env.local`。

如果已经由 HTTPS 反向代理或企业网关完成访问控制，可不设置内置门禁密码，但生产环境必须显式设置 `APP_TRUST_REVERSE_PROXY_AUTH=true`。`APP_API_ACCESS_TOKEN` 只用于脚本/运维调用，不作为浏览器前端门禁。

门禁配置说明：

- `APP_GATE_PASSWORD` 是浏览器进入工作台时输入的访问密码，生产环境必须使用强随机密码。
- `APP_GATE_PASSWORD_HASH` 可替代明文密码，当前格式为 SHA-256 hex digest；即便使用 hash，也要保证原始密码足够强。
- `APP_SESSION_SECRET` 用于签名登录 session，必须是另一段强随机密钥。
- `APP_SESSION_COOKIE_SECURE=true` 只适用于 HTTPS。如果测试环境仍是 HTTP，应暂时设为 `false`。
- 如果要轮换访问密码并强制所有已登录浏览器重新登录，需要同时轮换 `APP_SESSION_SECRET` 并重启后端。
- `APP_API_ACCESS_TOKEN` 是可选脚本/运维 token，不会注入前端静态包，也不作为浏览器门禁。

启动：

```bash
docker compose up -d --build
docker compose ps
```

Compose 会使用固定容器名：`ruyi-dream-frontend`、`ruyi-dream-backend`、`ruyi-dream-rustfs`，用于和同机其它服务区分。日常命令仍使用 service 名 `frontend`、`backend`、`rustfs`。

验收：

```bash
curl http://127.0.0.1:8000/api/health
docker compose logs --tail=100 backend
docker compose logs --tail=100 rustfs
```

浏览器打开：

```text
http://<server-ip-or-domain>:5173
```

## 日常管理命令

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f backend
docker compose logs -f frontend
docker compose logs -f rustfs
```

重启服务：

```bash
docker compose restart backend
docker compose restart frontend
docker compose restart rustfs
```

停止：

```bash
docker compose stop
```

停止并移除容器网络，不删除数据：

```bash
docker compose down
```

## 升级发布

代码更新后：

```bash
git pull
docker compose build
docker compose up -d
docker compose ps
```

如果只改后端：

```bash
docker compose up -d --build backend
```

如果改了前端代码或 `PUBLIC_BACKEND_URL`：

```bash
docker compose up -d --build frontend
```

升级前建议先备份 SQLite 和 RustFS 数据目录。

## 备份

停机冷备份最简单可靠：

```bash
docker compose stop backend
tar -czf backup-app-data-$(date +%Y%m%d-%H%M%S).tgz deployment/app-data/backend
tar -czf backup-rustfs-data-$(date +%Y%m%d-%H%M%S).tgz deployment/rustfs/data
docker compose start backend
```

备份必须同时覆盖：

- `deployment/app-data/backend`
- `deployment/rustfs/data`

只备份其中一个都不能完整恢复业务状态。

## 恢复

恢复前先停服务：

```bash
docker compose down
```

清理当前数据后解压备份：

```bash
rm -rf deployment/app-data/backend/*
rm -rf deployment/rustfs/data/*
tar -xzf backup-app-data-YYYYMMDD-HHMMSS.tgz
tar -xzf backup-rustfs-data-YYYYMMDD-HHMMSS.tgz
docker compose up -d
```

恢复后检查：

```bash
curl http://127.0.0.1:8000/api/health
docker compose logs --tail=100 backend
```

## 故障排查

后端健康检查失败：

```bash
docker compose logs --tail=200 backend
docker compose logs --tail=200 rustfs
```

常见原因：

- `.env` 中 RustFS 账号密码与已有数据目录不一致。
- RustFS 尚未 ready，等待后端自动重启。
- 服务器时间不准，S3 签名报 `RequestTimeTooSkewed`。
- `PUBLIC_S3_URL` 不是浏览器可访问地址，导致图片预览失败。
- `FAL_KEY` 未配置，真实生图任务会失败。
- `GEMINI_API_KEY` 未配置，默认方案策划会失败。
- `APP_ENV=production` 但没有设置 `APP_GATE_PASSWORD` / `APP_GATE_PASSWORD_HASH`，且没有显式设置 `APP_TRUST_REVERSE_PROXY_AUTH=true`。
- 启用内置门禁但没有设置 `APP_SESSION_SECRET`。

前端能打开但 API 失败：

```bash
docker compose logs --tail=100 frontend
curl http://127.0.0.1:8000/api/health
```

重点检查：

- `PUBLIC_BACKEND_URL` 是否是浏览器可访问地址。
- `BACKEND_CORS_ORIGINS` 是否包含前端访问地址。
- 启用内置门禁时，是否已经通过登录页拿到 session；脚本调用是否使用了可选的 `APP_API_ACCESS_TOKEN`。
- 修改 `PUBLIC_BACKEND_URL` 后是否重新构建了 frontend 镜像。

## 安全和暴露面

- 内网试点可以直接暴露 `5173/8000/9000/9001`，但生产环境至少启用 single-password app gate 或由反向代理完成访问控制。
- 对公网开放时，应放到 HTTPS 反向代理后面，并限制 RustFS Console 访问。
- 不要公开 RustFS bucket。
- 不要把 `.env`、数据库、对象数据目录提交到 Git。
- 人员变更或交付转移时轮换 RustFS 密钥，并同步更新 `.env`。
