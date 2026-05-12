# Docker Compose 部署与管理

本文是单机 Docker Compose 部署的通用入口，适用于本地完整联调、客户内网试点、有公网 IP 的云服务器试点。即使部署在 AWS、阿里云或其它云主机，也优先按本文操作；云厂商差异主要体现在安全组/防火墙、数据盘挂载和公网 URL。

## 适用边界

- 前端、后端、RustFS 由项目根目录 `docker-compose.yml` 管理。
- SQLite 和 RustFS 数据保存在宿主机 bind mount 目录。
- 单客户、单工作空间、单后端进程。
- 暂不覆盖 Kubernetes、多副本后端、外部数据库或托管对象存储部署。

## 防火墙和安全组

需要放通的端口取决于 `.env` 中的端口配置。默认：

| 端口 | 用途 | 建议 |
|---|---|---|
| `22` | SSH | 只允许运维 IP |
| `5173` | 前端 | 试点可公网开放 |
| `8000` 或自定义 `BACKEND_PORT` | 后端 API | 试点可公网开放，需 app gate |
| `9000` | RustFS S3 API | 预签名图片需要浏览器可访问；优先限制来源 |
| `9001` | RustFS Console | 不建议公网开放 |

后端还需要出站 HTTPS 访问 Gemini 和 fal。

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

如果默认 `8000` 已被同机服务占用，可以只改宿主机映射端口：

```env
BACKEND_PORT=18000
PUBLIC_BACKEND_URL=http://<server-ip-or-domain>:18000
```

修改 `PUBLIC_BACKEND_URL` 后必须重建 frontend 镜像，因为该值会写入前端静态包。

对象存储 bucket 推荐保持：

```env
APP_STORAGE_BUCKET=ruyi-dream
```

bucket 名必须符合 S3 命名规则：3-63 个字符，只用小写字母、数字、短横线和点号；不要用中文、大写、下划线或带空格的值。

当前应用使用 path-style S3：

```text
http://rustfs:9000/<bucket>
http://<PUBLIC_S3_URL>/<bucket>
```

默认部署不要设置 `RUSTFS_SERVER_DOMAINS`。该变量只用于 virtual-host style S3，例如 `http://bucket.s3.example.com`，并要求 DNS、反向代理和 RustFS Host 配置完全匹配。误设置时，后端创建 bucket 可能报 `InvalidBucketName`。

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
# RustFS 官方 Docker 镜像使用 UID 10001。bind mount 目录必须授权给该 UID。
mkdir -p deployment/rustfs/data deployment/rustfs/config deployment/rustfs/certs deployment/rustfs/logs
chown -R 10001:10001 deployment/rustfs/data deployment/rustfs/config deployment/rustfs/certs deployment/rustfs/logs
chmod -R 750 deployment/rustfs/data deployment/rustfs/config deployment/rustfs/certs deployment/rustfs/logs

# 后端镜像使用非 root app 用户。查询实际 UID/GID 后授权宿主机数据目录。
BACKEND_UID_GID=$(docker compose run --rm --no-deps --entrypoint sh backend -c 'printf "%s:%s" "$(id -u)" "$(id -g)"')
mkdir -p deployment/app-data/backend/logs
chown -R "$BACKEND_UID_GID" deployment/app-data/backend
chmod -R 750 deployment/app-data/backend

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

## 更正式的数据盘布局

仓库内 `deployment/rustfs/data` 适合试点和验收。更正式的生产部署建议使用独立 EBS/NVMe 数据盘，按 RustFS 官方建议格式化为 XFS 后挂载到固定路径，例如 `/data/rustfs0`，再把 Compose volume 从：

```yaml
./deployment/rustfs/data:/data
```

调整为：

```yaml
/data/rustfs0:/data
```

无论目录在哪里，RustFS 数据目录都必须允许 UID `10001` 写入。

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
- RustFS bind mount 目录未授权给 UID `10001`，日志包含 `Permission denied`。
- `deployment/app-data/backend` 未授权给后端容器的 `app` 用户，日志包含无法创建 `/app/data/logs`。
- RustFS 尚未 ready，等待后端自动重启。
- 服务器时间不准，S3 签名报 `RequestTimeTooSkewed`。
- `PUBLIC_S3_URL` 不是浏览器可访问地址，导致图片预览失败。
- `RUSTFS_SERVER_DOMAINS` 被误用于 path-style 部署，后端创建 bucket 时报 `InvalidBucketName`。
- `FAL_KEY` 未配置，真实生图任务会失败。
- `GEMINI_API_KEY` 未配置，默认方案策划会失败。
- `APP_ENV=production` 但没有设置 `APP_GATE_PASSWORD` / `APP_GATE_PASSWORD_HASH`，且没有显式设置 `APP_TRUST_REVERSE_PROXY_AUTH=true`。
- 启用内置门禁但没有设置 `APP_SESSION_SECRET`。

针对实际部署中遇到的典型日志：

```text
Bind for 0.0.0.0:8000 failed: port is already allocated
```

改 `BACKEND_PORT` 和 `PUBLIC_BACKEND_URL`，然后 `docker compose up -d --build`。

```text
Production object storage access key must be at least 12 characters.
```

替换 `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` 为至少 12 位强随机值。

```text
PermissionError: [Errno 13] Permission denied: '/app/data/logs'
```

按首次部署里的 `BACKEND_UID_GID` 命令修正 `deployment/app-data/backend` 权限。

```text
InvalidBucketName
```

确认 `APP_STORAGE_BUCKET` 合法，并确认没有在 path-style 部署里设置 `RUSTFS_SERVER_DOMAINS`。

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
