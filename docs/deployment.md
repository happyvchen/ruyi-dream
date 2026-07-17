# 部署手册

单机 Docker Compose 部署的唯一权威文档,覆盖首次部署、数据迁移、日常运维、备份恢复和故障排查。适用于内网服务器、云主机(AWS/阿里云等)和本地完整联调;云厂商差异只体现在防火墙/安全组、数据盘和公网 URL。

本地开发(裸跑后端、前端热更新)见 `local-development.md`;压测见 `loadtest.md`。

## 1. 架构与端口

```text
浏览器 ─→ frontend (Nginx SPA, :5173)
       ─→ backend  (FastAPI, :8000) ─→ SQLite (deployment/app-data/backend/app.sqlite3)
       │                            ─→ 对象存储 (S3 兼容, 默认本机 RustFS http://rustfs:9000)
       │                            ─→ Gemini (GEMINI_PROVIDER: toapis 中转 / direct 直连)
       │                            ─→ GPT-Image-2 (GPT_IMAGE2_PROVIDER: toapis / fal)
       └─→ 图片 presigned URL 直连对象存储 (:9000, 必须浏览器可达)
```

| 端口 | 用途 | 开放建议 |
|---|---|---|
| 5173 | 前端 | 按访问范围开放 |
| 8000 | 后端 API | 开放,靠登录门禁保护 |
| 9000 | RustFS S3(presigned 图片) | 必须对用户浏览器可达 |
| 9001 | RustFS Console | 不建议对外开放 |

后端需要出站 HTTPS 访问 toapis.com(默认通道;`direct` 通道则需 googleapis.com)。

## 2. 首次部署

### 2.1 配置 .env

```bash
git clone https://github.com/happyvchen/ruyi-dream.git && cd ruyi-dream
cp .env.example .env && vim .env
```

必须修改的项(`APP_ENV=prod` 会逐项强校验,一次配齐避免反复重启):

```env
APP_ENV=production
COMPOSE_FILE=docker-compose.yml:docker-compose.rustfs.yml   # 默认含本机 RustFS

# 对象存储凭证:≥12 字符强随机,禁止 admin/change-me 等占位值
# 生成:openssl rand -hex 8(AK) / openssl rand -hex 16(SK)
APP_S3_ACCESS_KEY_ID=<随机值>
APP_S3_SECRET_ACCESS_KEY=<随机值>

# 登录门禁:密码 ≥12 字符;推荐用 hash(echo -n '密码' | sha256sum)
APP_USER_PASSWORD_HASH=<sha256>
APP_ADMIN_PASSWORD_HASH=<sha256>
APP_SESSION_SECRET=<openssl rand -hex 16>
APP_SESSION_COOKIE_SECURE=false        # 仅 HTTPS 时改 true

# 公网/内网 URL:必须是【用户浏览器可达】的地址,不能是 127.0.0.1
PUBLIC_BACKEND_URL=http://<服务器IP或域名>:8000
PUBLIC_S3_URL=http://<服务器IP或域名>:9000
BACKEND_CORS_ORIGINS=http://<服务器IP或域名>:5173

# 媒体缓存:不影响直传和非媒体 presigned URL
APP_S3_PRESIGN_EXPIRES_SECONDS=900
APP_MEDIA_PRESIGN_EXPIRES_SECONDS=3600
APP_MEDIA_THUMB_CACHE_SECONDS=2400
APP_MEDIA_PREVIEW_CACHE_SECONDS=1800
APP_MEDIA_ORIGINAL_CACHE_SECONDS=600
APP_MEDIA_CACHE_JITTER_SECONDS=600
APP_MEDIA_PRESIGN_SAFETY_MARGIN_SECONDS=120

# AI 通道
GEMINI_PROVIDER=toapis                 # 国内无需 VPN;direct 需 GEMINI_API_KEY 且能访问 googleapis.com
GEMINI_TOAPIS_TIMEOUT_SECONDS=300      # Gemini(ToAPIs) 读取响应超时;慢请求可适当放大
GEMINI_TOAPIS_CONNECT_TIMEOUT_SECONDS=60 # Gemini(ToAPIs) 连接/上传请求体超时;base64 图片上行慢时重点调它
AI_STYLIST_CONTRACT_RETRY_LIMIT=3      # AI Stylist 结果缺契约字段时的自动重问次数
AI_STYLIST_CONTRACT_RETRY_DELAY_SECONDS=15 # AI Stylist 契约重试前等待秒数
GPT_IMAGE2_PROVIDER=toapis
TOAPIS_API_KEY=sk-...
TOAPIS_MAX_CONCURRENCY=5              # ToAPIs 全局并发:Gemini/Image-2/API 上传与状态查询共享
TOAPIS_IMAGE_SUBMIT_TIMEOUT_SECONDS=180 # Image-2 创建任务单次提交请求超时
GENERATION_UNKNOWN_SUBMISSION_RETRY_LIMIT=3  # 提交结果未知时，每张图最多对账/补提次数
GENERATION_UNKNOWN_SUBMISSION_RETRY_DELAY_SECONDS=15 # 提交结果未知重试前等待秒数
GENERATION_POLL_INTERVAL_SECONDS=5     # Image-2 状态轮询基础间隔
GENERATION_SUBMIT_RETRY_LIMIT=3       # Image-2 创建任务确认失败/生成失败后的重试次数
GENERATION_STATUS_RETRY_LIMIT=15      # 状态查询连续瞬时失败重试次数
```

要点:

- 根目录 `.env` 是唯一配置源,不要另建 `backend/.env`。
- `PUBLIC_BACKEND_URL` 会构建进前端静态包,改动后必须重建 frontend 镜像。
- 端口冲突时只改宿主机映射:`BACKEND_PORT=18000` 并同步改 `PUBLIC_BACKEND_URL`。
- `APP_S3_BUCKET` 保持默认 `ruyi-dream`(S3 命名规则:3-63 位小写字母/数字/短横线)。
- `TOAPIS_MAX_CONCURRENCY` 控制 ToAPIs 全局并发,默认 5;批次一键成片不再使用独立并发参数。旧 `GENERATION_AUTO_RUN_MAX_CONCURRENCY` 仅作为过渡 fallback。
- 媒体图片通过 `/api/media` 返回 302 到对象存储。`APP_MEDIA_PRESIGN_EXPIRES_SECONDS` 控制媒体 GET 签名 URL 的有效期;`APP_MEDIA_*_CACHE_SECONDS` 控制浏览器缓存 TTL,且实际 TTL 会被媒体签名有效期减安全余量截断。`APP_S3_PRESIGN_EXPIRES_SECONDS` 继续控制直传和其它非媒体签名 URL。
- 不要设置 `RUSTFS_SERVER_DOMAINS`(仅 virtual-host style 需要,误设会报 `InvalidBucketName`)。
- 密码/hash 轮换后想强制所有浏览器重新登录,需同时轮换 `APP_SESSION_SECRET`。
- 由外部网关做认证时:`APP_TRUST_REVERSE_PROXY_AUTH=true` 且清空账号密码项。

### 2.2 目录权限(容器均以非 root 运行,跳过必报 Permission denied)

```bash
# RustFS 官方镜像 UID 10001
mkdir -p deployment/rustfs/{data,config,certs,logs}
sudo chown -R 10001:10001 deployment/rustfs
sudo chmod -R 750 deployment/rustfs

# backend 镜像的 app 用户,UID 以实际查询为准
docker compose build backend
BACKEND_UID_GID=$(docker compose run --rm --no-deps --entrypoint sh backend -c 'printf "%s:%s" "$(id -u)" "$(id -g)"')
mkdir -p deployment/app-data/backend/logs
sudo chown -R "$BACKEND_UID_GID" deployment/app-data/backend
sudo chmod -R 750 deployment/app-data/backend
```

### 2.3 启动与验收

```bash
docker compose up -d --build
docker compose ps          # 三个容器均 Up,无 Restarting
curl http://127.0.0.1:8000/api/health
# 期待: {"ok":true,"storage":"configured","geminiConfigured":true,
#        "geminiProvider":"toapis","imageProviderConfigured":true}
```

浏览器打开 `http://<服务器IP>:5173`,登录后上传一张测试图,确认列表和图片预览正常(预览失败几乎都是 `PUBLIC_S3_URL` 不可达)。

### 2.4 使用外部 S3 兼容存储(可选)

对象存储用阿里 OSS / AWS S3 / MinIO 集群等外部服务时,不启动本机 RustFS:

```env
COMPOSE_FILE=docker-compose.yml
APP_S3_ENDPOINT_URL=https://<后端访问的 endpoint,可走内网>
APP_S3_PUBLIC_ENDPOINT_URL=https://<浏览器可达的 endpoint>
APP_S3_BUCKET=<bucket>
APP_S3_REGION=<region>
APP_S3_FORCE_PATH_STYLE=false        # RustFS/MinIO 为 true,OSS/S3 为 false
```

生产建议提前手建私有 bucket,凭证只授予该 bucket 最小读写删列权限。

### 2.5 正式数据盘(可选)

生产建议 RustFS 数据放独立数据盘(XFS),把 compose volume 从 `./deployment/rustfs/data:/data` 改为 `/data/rustfs0:/data`,同样授权 UID 10001。

## 3. 数据迁移(换服务器 / 换存储)

数据只有两块,必须取同一时间点:SQLite(`deployment/app-data/backend/`)+ 对象存储 bucket。DB 只存 bucket 内 objectKey,不含 endpoint/bucket 名,换存储只要**原样保留 key** 同步对象即可,URL 由运行时按 `.env` 重新 presign。

```bash
# ── 旧机器:停写并导出 ──
docker compose stop backend
tar -czf backup-app-data.tgz deployment/app-data/backend   # scp 到新机器

# ── 新机器:完成第 2 节部署后,先只起 RustFS ──
docker compose up -d rustfs

# ── rclone 同步对象(示例:阿里 OSS → 本机 RustFS)──
sudo apt install rclone
mkdir -p ~/.config/rclone && vim ~/.config/rclone/rclone.conf
```

```ini
[oss]
type = s3
provider = Alibaba
access_key_id = <OSS AK>
secret_access_key = <OSS SK>
endpoint = oss-cn-hangzhou.aliyuncs.com

[rustfs]
type = s3
provider = Other
access_key_id = <与 .env 的 APP_S3_ACCESS_KEY_ID 一致>
secret_access_key = <与 .env 一致>
endpoint = http://127.0.0.1:9000
force_path_style = true
```

```bash
rclone mkdir rustfs:ruyi-dream
rclone sync oss:<源bucket> rustfs:ruyi-dream --progress --checksum --transfers 16
rclone check oss:<源bucket> rustfs:ruyi-dream          # 校验一致

# ── 落 SQLite 并启动 ──
tar -xzf backup-app-data.tgz
sudo chown -R "$BACKEND_UID_GID" deployment/app-data/backend
docker compose up -d
```

注意:同步期间旧服务必须保持停写;完成后旧机器下线,避免双写。单连接跨地域下载慢属常见现象,`--transfers 16` 提并发通常有效。

## 4. 日常运维

```bash
docker compose ps                       # 状态
docker compose logs -f backend          # 日志(frontend/rustfs 同理)
docker compose restart backend          # 重启
docker compose down                     # 停止并移除容器,不删数据
```

升级发布(升级前先做一次备份):

```bash
git pull
docker compose up -d --build            # 只改后端: --build backend
# 改了前端代码或 PUBLIC_BACKEND_URL:必须重建 frontend
docker compose up -d --build frontend
```

数据库结构迁移由后端启动时自动完成;旧库跑新代码可以,反向降级不行。

## 5. 备份与恢复

SQLite 和对象数据**必须同点位一起备份**,只备一半无法完整恢复。停机冷备最可靠:

```bash
docker compose stop backend
tar -czf backup-app-data-$(date +%Y%m%d-%H%M%S).tgz deployment/app-data/backend
tar -czf backup-rustfs-data-$(date +%Y%m%d-%H%M%S).tgz deployment/rustfs/data
docker compose start backend
```

恢复:

```bash
docker compose down
rm -rf deployment/app-data/backend/* deployment/rustfs/data/*
tar -xzf backup-app-data-*.tgz && tar -xzf backup-rustfs-data-*.tgz
docker compose up -d && curl http://127.0.0.1:8000/api/health
```

外部 S3 存储则按云厂商的 bucket 备份/版本控制/跨区域复制执行。

## 6. 故障排查

先跑三板斧:`docker compose ps`、`curl http://127.0.0.1:8000/api/health`、`docker compose logs --tail=200 backend`。

### 后端起不来(Restarting)

| 日志特征 | 原因 | 处理 |
|---|---|---|
| `access key must be at least 12 characters` / `placeholder` | prod 模式拒绝弱/占位凭证 | 换强随机值;RustFS 凭证与数据目录绑定,换凭证需清空 `deployment/rustfs/data` 重建重同步 |
| `Permission denied: '/app/data/logs'` | app-data 目录属主不对 | 按 2.2 节 chown backend UID |
| RustFS 日志 `Permission denied` | rustfs 目录属主不对 | 按 2.2 节 chown 10001 |
| `Production must configure at least one fixed app account` | prod 未配账号也未信任网关 | 配 `APP_*_PASSWORD_HASH` 或 `APP_TRUST_REVERSE_PROXY_AUTH=true` |
| `app gate requires APP_SESSION_SECRET` | 有门禁没会话密钥 | 补 `APP_SESSION_SECRET` |
| `port is already allocated` | 端口冲突 | 改 `BACKEND_PORT` + `PUBLIC_BACKEND_URL`,重建 frontend |
| `InvalidBucketName` | bucket 名不合法或误设 `RUSTFS_SERVER_DOMAINS` | 修正 bucket 名/删掉该变量 |
| RustFS 凭证不匹配 | 数据目录是旧凭证创建的 | 凭证改回,或清数据目录重建 |

### 登录 / API 401

- 业务 `/api/*` 全部要求登录 cookie;`curl http://127.0.0.1:8000/api/auth/session` 看门禁状态。
- 脚本调用用 `APP_API_ACCESS_TOKEN`(header `Authorization: Bearer <token>` 或 `X-Ruyi-Access-Token`)。
- 改密码只需 `docker compose up -d backend`;已登录浏览器仍有效,想立刻踢掉需同时轮换 `APP_SESSION_SECRET`。
- `APP_SESSION_COOKIE_SECURE=true` 只能在 HTTPS 下用,HTTP 环境会导致永远登录不上。

### 前端打开但 API 失败

依次核对:`PUBLIC_BACKEND_URL` 浏览器是否可达 → `BACKEND_CORS_ORIGINS` 是否含前端地址 → 改过 `PUBLIC_BACKEND_URL` 后是否重建了 frontend 镜像。

### 图片预览失败

- `PUBLIC_S3_URL` / `APP_S3_PUBLIC_ENDPOINT_URL` 必须浏览器可达(最常见错因:留成 127.0.0.1)。
- 服务器时钟偏移会报 `RequestTimeTooSkewed`,校时。
- 对象缺失:从同一备份点同时恢复 SQLite 和对象数据。
- 缓存异常:浏览器 Network 面板关闭 Disable cache 后,检查 `/api/media` 的 302 与最终 RustFS 图片响应是否都有 `Cache-Control`。非下载图片应是 `private, max-age=...`;下载应是 `private, no-store`。若图片短时间内仍全量回源,优先排查代理是否覆盖响应头或前端是否替换了未变化图片的 `src`。

### AI 链路失败

- 打标/选搭/拍摄剧本失败或 `geminiConfigured:false`:`toapis` 通道查 `TOAPIS_API_KEY`,`direct` 通道查 `GEMINI_API_KEY` 与 googleapis.com 连通性;报"额度不足"时到 ToAPIs 控制台看余额(多图请求预扣费较高)。
- 生图失败或 `imageProviderConfigured:false`:查 `GPT_IMAGE2_PROVIDER` 对应的 key(toapis→`TOAPIS_API_KEY`,fal→`FAL_KEY`)与出站网络。
- 多任务批量生成遇到 429/超时:先降低 `TOAPIS_MAX_CONCURRENCY`;它同时影响 Gemini(ToAPIs)、Image-2(ToAPIs) 和批次一键成片推进。
- UI 可对失败的单张成片重试;任务卡住时后端重启后 poller 会按租约(`GENERATION_IMAGE_TIMEOUT_SECONDS`)自动恢复。
- 升级排障时保留:任务 id、失败图 id、backend 最后 300 行日志。

### 上传失败

限制:仅 JPG/PNG/WebP;单图 ≤15MB(`APP_MAX_UPLOAD_IMAGE_BYTES`)、≤2500 万像素;单套搭配 ≤20 图。调整限额改 `.env` 后重启 backend。

### SQLite 异常

```bash
docker compose stop backend
cp deployment/app-data/backend/app.sqlite3 app.sqlite3.incident-copy   # 先留副本
sqlite3 deployment/app-data/backend/app.sqlite3 "PRAGMA integrity_check;"
```

integrity_check 不为 `ok` 时,从最近备份点整体恢复并上报工程。

## 7. 安全要点

- 生产必须启用登录门禁或由 HTTPS 反向代理做访问控制;RustFS Console(9001)不对公网开放。
- `.env`、SQLite、对象数据目录永不提交 Git;人员变更时轮换对象存储凭证和 `APP_SESSION_SECRET`。
- 公网部署放在 HTTPS 反向代理之后,并保持各 PUBLIC URL 与浏览器实际访问地址一致。
