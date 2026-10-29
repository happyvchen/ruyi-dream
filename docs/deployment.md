# 部署手册

单机 Docker Compose 部署的唯一权威文档,覆盖首次部署、数据迁移、日常运维、备份恢复和故障排查。适用于内网服务器、云主机(AWS/阿里云等)和本地完整联调;云厂商差异只体现在防火墙/安全组、数据盘和公网 URL。

本地开发(裸跑后端、前端热更新)见 `local-development.md`;压测见 `loadtest.md`。

## 1. 架构与端口

```text
浏览器 ─→ frontend (Nginx SPA, :5173)
       ─→ backend  (FastAPI, :8000) ─→ SQLite (deployment/app-data/backend/app.sqlite3)
       │                            ─→ 对象存储 (S3 兼容, 默认本机 RustFS http://rustfs:9000)
       │                            ─→ 文本/视觉理解 (toapis: DeepSeek V4.1 Flash / direct: Gemini)
       │                            ─→ 成片模型 (toapis: Image-2.5 Sunburst / fal: Image-2)
       └─→ 图片 presigned URL 直连对象存储 (:9000, 必须浏览器可达)
```

| 端口 | 用途 | 开放建议 |
|---|---|---|
| 5173 | 前端 | 按访问范围开放 |
| 8000 | 后端 API | 开放,靠登录门禁保护 |
| 9000 | RustFS S3(presigned 图片) | 必须对用户浏览器可达 |
| 9001 | RustFS Console | 不建议对外开放 |

后端需要出站 HTTPS 访问 `toapis.cn`（中国大陆默认通道；`direct` 通道则需访问 `googleapis.com`）。

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
GEMINI_TOAPIS_MODEL=deepseek-v4.1-flash # ToAPIs 模型 ID，保留兼容变量名
GEMINI_TOAPIS_TIMEOUT_SECONDS=300      # 文本模型(ToAPIs) 读取响应超时;慢请求可适当放大
GEMINI_TOAPIS_CONNECT_TIMEOUT_SECONDS=60 # 文本模型(ToAPIs) 连接/导入请求体超时;base64 图片上行慢时重点调它
AI_STYLIST_CONTRACT_RETRY_LIMIT=3      # AI Stylist 结果缺契约字段时的自动重问次数
AI_STYLIST_CONTRACT_RETRY_DELAY_SECONDS=15 # AI Stylist 契约重试前等待秒数
GPT_IMAGE2_PROVIDER=toapis
GPT_IMAGE2_MODEL_ID=gpt-image-2.5-sunburst
TOAPIS_API_KEY=sk-...
TOAPIS_BASE_URL=https://toapis.cn      # 只填写 origin，不要追加 /v1
TOAPIS_MAX_CONCURRENCY=5              # ToAPIs 全局并发:文本模型/Image-2/API 导入与状态查询共享
TOAPIS_IMAGE_SUBMIT_TIMEOUT_SECONDS=180 # Image-2 创建任务单次提交请求超时
GENERATION_UNKNOWN_SUBMISSION_RETRY_LIMIT=3  # 提交结果未知时，每张图最多对账/补提次数
GENERATION_UNKNOWN_SUBMISSION_RETRY_DELAY_SECONDS=15 # 提交结果未知重试前等待秒数
GENERATION_POLL_INTERVAL_SECONDS=5     # Image-2 状态轮询基础间隔
GENERATION_SUBMIT_RETRY_LIMIT=3       # Image-2 创建任务确认失败/生成失败后的重试次数
GENERATION_STATUS_RETRY_LIMIT=15      # 状态查询连续瞬时失败重试次数
```

要点:

- 文本/视觉理解模型统一由 `GEMINI_TOAPIS_MODEL` 配置，覆盖服装挑图打标、AI 选搭、姿势识别/选择及可选提示词整理。切换前等待在途任务和持久重试结束，再更新实际使用的 env 文件并重建后端容器；裸跑后端需重启进程。保留现有 Prompt、历史标签与成片，不触发批量重跑。需回滚时将模型改回 `gemini-3.8-flash`。此次仅核实模型目录与公开协议，未进行付费推理验收；思考模式和输出预算沿用模型默认值，不假定 DeepSeek 直连接口的可选参数被 ToAPIs 完整转发。

- 成片默认使用 ToAPIs `gpt-image-2.5-sunburst` 普通版，保留 `GPT_IMAGE2_*` 配置名。升级已有环境时同步修改实际 `.env` 的 `GPT_IMAGE2_MODEL_ID`，再重建后端容器。模型在提交时读取当前配置，切换应安排在生成空闲时；已提交任务继续按原 ID 查询，历史结果和快照不改写。

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

发布包在 `umask 077` 下解压后，源码可能只有所有者可读；Docker `COPY` 会保留这些权限。后端 Dockerfile 显式开放镜像内程序、Prompt 模板和维护脚本的读取/目录遍历权限，前端显式将 Nginx 配置设置为 `0644`。不要通过递归放宽生产 `.env` 或数据目录权限解决代码导入失败。上线前须以镜像默认非 root 用户验证 `from app.db import init_db` 能导入，以及前端 `nginx -t` 通过；使用 root 做数据库副本演练不能替代这些检查。

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

浏览器打开 `http://<服务器IP>:5173`,登录后导入一张测试图,确认列表和图片预览正常(预览失败几乎都是 `PUBLIC_S3_URL` 不可达)。

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

### LILY 生产发布与后续升级（固定流程）

生产代码目录 `/home/lilyroot/ruyi-dream` 是发布包，不是 Git 仓库；生产数据位于独立的 `DATA_ROOT=/data/ruyi-dream`。每次发布都从已确定的 Git 提交制作版本化发布包和 backend/frontend 镜像，记录提交号、镜像 ID、配置变动与验收结果；构建时传入 `GIT_COMMIT=<完整提交号>`，以 `/api/version` 校验。禁止直接用 `git pull` 或浮动的 `latest` 充当版本标识。保留上一版镜像，RustFS 单独管理，本应用发布不重建或清理 RustFS。

1. 发布前在隔离环境运行本地测试、前端构建与 SQLite 迁移预演；不调用外部 AI/provider。检查当前容器、磁盘、`/api/health`、数据库 `PRAGMA integrity_check`、已应用迁移和生成/导入在途任务。等在途任务结束，通知使用者进入短维护窗口并停写。
2. 停止 backend 后，在 `/data/ruyi-dream/release-backups/<发布标识>/` 保存**实际** `/data/ruyi-dream/app-data/backend/`、生产 `.env`、旧发布包版本和镜像 ID。备份必须包括 SQLite 主文件及 WAL/SHM 等同目录文件；校验归档可读取、SQLite 完整性和恢复路径。发布期间 RustFS 镜像、数据目录及凭证保持不变；RustFS 对象数据有独立的定期备份与恢复演练，不能被发布备份清理逻辑触碰。
3. 先部署 backend，确认迁移与健康状态，再按本手册“百兆带宽 P0 发布”执行有界媒体修复。然后部署 frontend，检查登录、任务与搭配列表、历史结果、图片预览和下载、上传入口及浏览器控制台。验收只用现有数据和非 AI 路径，不向生产写入测试数据或触发付费生成。
4. 记录新版本与镜像 ID，观察日志和关键业务信号。验收通过后，**发布备份只保留最近一次已验证的发版前备份**，再删除更早的发布备份；清理目标严格限定为 `release-backups/`，绝不包含 RustFS 独立定期备份。若验收失败，停止发布与清理，保留全部证据和备份。

LILY 使用 `/home/lilyroot/ruyi-releases/<发布标识>/` 保存发布包，用原生产 `.env` 加两份发布包内 Compose 文件分别执行 `build backend frontend`、`up -d --no-deps backend` 和 `up -d --no-deps frontend`。每次构建通过 `GIT_COMMIT=<完整提交号>` 注入版本；切换前为旧镜像打 `pre-<发布标识>` 标签。生产 `.env` 只在备份后按发布说明修改。不要对 RustFS 执行本次应用的 `up --build`。

独立定期备份使用 `scripts/backup-production-data.sh`，在同一停写窗口复制 SQLite 数据目录、RustFS 对象目录及 `.env`，写入 `periodic-data-backups/<UTC时间>/COMPLETE` 才算完成。LILY 服务器为 XFS，脚本使用 reflink 降低对象备份耗时；它仍位于同一块数据盘，不能替代异机或离线备份。定期备份**不受发布备份的保留一份规则约束**，不得按发版清理脚本删除。定期检查容量，并把已验证副本另存到独立故障域。

恢复定期备份时，先在隔离路径验证 `COMPLETE`、SQLite 完整性和对象目录，再停写并整体恢复；RustFS 数据恢复到 `/data/ruyi-dream/rustfs/data` 后须恢复容器可写的 UID 10001 权限，后端数据目录须恢复镜像 `app` 用户的实际 UID/GID。不要直接把备份目录改作容器挂载并启动。

LILY 已在 `lilyroot` crontab 安排每周一 03:30（北京时间，服务器为 UTC）运行稳定路径 `/home/lilyroot/ruyi-maintenance/backup-production-data.sh`，日志写入 `/data/ruyi-dream/periodic-data-backups/backup.log`。脚本检查在途 AI attempt 和上传，存在时跳过并在日志注明，维护人员须补跑并确认新 `COMPLETE`。每周检查 `crontab -l`、日志、最新 `COMPLETE` 和数据盘空间；更换脚本时同步更新仓库与稳定路径。

数据库结构迁移由后端启动时自动完成，旧库可升新代码；不能假定新库可直接运行旧代码。应用故障优先修复后重发；需要整体回退时，停写并用同一发布备份恢复旧版本镜像、SQLite 数据目录和 `.env`。若新版本写入过对象或业务数据，先评估差异，避免用旧库覆盖新业务数据。

## 5. 备份与恢复

SQLite 和对象数据在迁机、灾难恢复时**必须同点位一起备份**，只备一半无法完整恢复。日常应用发布沿用上一节的发布备份，并依赖独立的 RustFS 定期备份；本节是完整灾难恢复备份示例。生产有 `DATA_ROOT` 时必须使用实际路径。停机冷备最可靠:

```bash
docker compose stop backend
tar -czf backup-app-data-$(date +%Y%m%d-%H%M%S).tgz -C /data/ruyi-dream app-data/backend
tar -czf backup-rustfs-data-$(date +%Y%m%d-%H%M%S).tgz -C /data/ruyi-dream rustfs/data
docker compose start backend
```

恢复时先停止写入，在隔离目录解包并校验归档、SQLite `integrity_check` 与对象数量；确认备份时间点和目标路径后，再将两部分**同一时间点**的数据一并恢复到实际 `DATA_ROOT`。先保留当前数据的副本，避免覆盖后无法复查。启动后检查 `/api/health`、登录、历史任务及图片读取。

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
- 多任务批量生成遇到 429/超时:先降低 `TOAPIS_MAX_CONCURRENCY`;它同时影响 文本模型(ToAPIs)、Image-2(ToAPIs) 和批次一键成片推进。
- UI 可对失败的单张成片重试;任务卡住时后端重启后 poller 会按租约(`GENERATION_IMAGE_TIMEOUT_SECONDS`)自动恢复。
- 升级排障时保留:任务 id、失败图 id、backend 最后 300 行日志。

### 导入失败

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

## 百兆带宽 P0 发布

1. 在当前版本先采集 5 分钟 HAR 和主机/链路指标，保存脱敏汇总（见 `loadtest.md`）。
2. 在维护窗口先运行带 074 迁移和修复脚本的后端镜像。先查询活跃媒体缺少缩略图、预览图或大小元数据的数量；有缺项再用下述命令按批修复。元数据齐全时只抽样使用 `--verify` 检查对象可读性，避免无谓遍历全部媒体。修复可重入；命令每次处理一个有界批次：

```bash
docker compose exec backend python scripts/repair_media_metadata.py --limit 50 --after-id 0
```

按输出 `next_after_id` 重复执行直到 `scanned=0`；失败 ID 单独复查。再加 `--include-archived` 处理长尾。该脚本实际访问对象存储并新增/补齐派生文件，应在发布期间执行；开发自动测试只使用临时数据库和 mock。本轮开发不自动修改当前业务素材。

3. `.env` 设置 `UPLOAD_CONCURRENCY=2`，合法范围 1–3，修改后 `docker compose up -d --build frontend`。本地 Vite 同样读取该配置。
4. API gzip 只启用后端一层（JSON/Text ≥1 KB，level 5）。上游网关对 API 关闭重复压缩；不压缩图片和 zip。后端镜像关闭 Uvicorn 原始 access log，使用 `uvicorn.error.performance` 脱敏计量日志。手动启动同样加 `--no-access-log`，日志级别保持 INFO。
5. 验证普通列表/默认检查器无 original、显式原图和下载正常、缺 preview 的占位可操作、302 与 RustFS 最终响应缓存头一致。下载必须 `private, no-store`。
6. 按 Runbook 重做同一 5 分钟窗口。应用回滚保留 074 字段与触发器、保留旧详情 GET；媒体回滚仍保持占位和显式原图，不恢复静默原图兜底。

QoS/AQM、CDN、multipart、公平队列均未作为本轮应用 P0 实施；需要生产数据和真实排队点控制权后再单独执行。
