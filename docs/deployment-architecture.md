# Docker Compose 部署设计

## 目标

本项目以 Docker Compose 作为 MVP 阶段的标准部署和管理方式。Compose 负责启动前端、后端和对象存储，让单机私有化交付具备稳定的启动、停止、查看日志、备份和升级路径。

Compose 部署适用于：

- 本地完整联调
- 客户内网单机试点
- 小规模私有化验收环境

暂不把 Compose 设计成多租户 SaaS、Kubernetes 或高可用集群方案。后续如果并发、容量或隔离要求提高，再拆分独立 worker、外部数据库、托管对象存储和反向代理。

## 服务拓扑

```text
Browser
  -> frontend: Nginx static SPA, port 5173
  -> backend: FastAPI API, port 8000
       -> SQLite metadata, ./deployment/app-data/backend/app.sqlite3
       -> S3 compatible object storage, RustFS container endpoint http://rustfs:9000 when full Compose is enabled
       -> Image-2 provider (default toapis, optional fal)
       -> Gemini AI Stylist / tagging
  -> S3 compatible public presigned URL, default RustFS port 9000
```

| Compose service | 容器名 | 镜像来源 | 职责 | 对外端口 |
|---|---|---|---|---|
| `frontend` | `ruyi-dream-frontend` | `frontend/Dockerfile` | 构建 React/Vite 静态资源，用非 root Nginx 提供 SPA fallback | `5173 -> 8080` |
| `backend` | `ruyi-dream-backend` | `backend/Dockerfile` | FastAPI API、SQLite、后台 planner/generation poller | `8000 -> 8000` |
| `rustfs` | `ruyi-dream-rustfs` | `rustfs/rustfs` | 可选本机 S3 兼容对象存储，保存素材、生成结果和导出 ZIP；默认通过 `COMPOSE_FILE=docker-compose.yml:docker-compose.rustfs.yml` 启用，使用外部 S3 兼容服务时不启用 | `9000 -> 9000`, `9001 -> 9001` |

## 数据持久化

| 宿主机路径 | 容器路径 | 内容 | 备份等级 |
|---|---|---|---|
| `deployment/app-data/backend` | `/app/data` | SQLite、应用事件日志 | 必须备份 |
| `deployment/rustfs/data` | `/data` | 原始素材、模特图、生成图、导出 ZIP | 必须备份 |
| `deployment/rustfs/config` | `/etc/rustfs` | RustFS 配置和运维备注 | 建议备份 |
| `deployment/rustfs/logs` | `/var/log/rustfs` | RustFS 文件日志 | 可按需保留 |
| Docker JSON logs | Docker 管理 | 服务 stdout/stderr | 已配置轮转 |

应用元数据和对象数据必须一起备份。同一个时间点的 `app.sqlite3` 和对象存储 bucket 应保持一致，否则可能出现数据库记录存在但对象丢失，或对象存在但 UI 不可见。使用默认 RustFS 时，对象数据在 `deployment/rustfs/data`；使用外部 S3 兼容服务时，按云厂商 bucket 备份、版本控制或跨区域复制策略执行。

Bind mount 目录权限按容器运行用户授权：

- RustFS 官方 Docker 镜像使用 UID `10001`，`deployment/rustfs/data`、`config`、`certs`、`logs` 需要允许 `10001:10001` 访问。
- 后端镜像使用非 root `app` 用户，`deployment/app-data/backend` 需要授权给该镜像内 `app` 用户的 UID/GID。可用 `docker compose run --rm --no-deps --entrypoint sh backend -c 'id -u; id -g'` 查询。
- 不使用 `chmod 777` 兜底；生产目录建议 `750`。

## 网络与 URL

后端访问对象存储使用 `APP_S3_ENDPOINT_URL`。根目录 `.env` 默认写宿主机可达地址，供本地裸跑后端使用：

```env
APP_S3_ENDPOINT_URL=http://127.0.0.1:9000
```

默认完整 Compose 部署启用 `docker-compose.rustfs.yml`，它会把容器内 backend 覆盖为 Compose 内部地址：

```env
APP_S3_ENDPOINT_URL=http://rustfs:9000
```

浏览器访问图片预签名 URL 使用外部可达地址：

```env
APP_S3_PUBLIC_ENDPOINT_URL=${PUBLIC_S3_URL}
```

前端静态包在构建时写入 API 地址：

```env
VITE_API_BASE_URL=${PUBLIC_BACKEND_URL}
```

因此生产或内网部署时，`.env` 中至少要确认：

```env
PUBLIC_BACKEND_URL=http://<server-ip-or-domain>:8000
PUBLIC_S3_URL=http://<server-ip-or-domain>:9000
BACKEND_CORS_ORIGINS=http://<server-ip-or-domain>:5173
```

如果前端、后端、RustFS 通过反向代理统一到一个域名，仍然保持这三个 public URL 与浏览器实际访问地址一致。

默认 RustFS 部署使用 path-style 请求：

```env
APP_S3_FORCE_PATH_STYLE=true
APP_S3_ENDPOINT_URL=http://127.0.0.1:9000
```

因此 RustFS 不需要 `RUSTFS_SERVER_DOMAINS`。该变量只用于 virtual-host style S3 域名模式；误配为公网 IP 或与容器内 Host 不一致时，RustFS 可能把 bucket 解析错并返回 `InvalidBucketName`。

外部 S3 兼容服务使用同一套 `APP_S3_*` 配置。要求 virtual-hosted style 的服务应设置：

```env
APP_S3_ENDPOINT_URL=https://<provider-endpoint>
APP_S3_PUBLIC_ENDPOINT_URL=https://<browser-reachable-provider-endpoint>
APP_S3_REGION=<provider-region>
APP_S3_FORCE_PATH_STYLE=false
```

如果后端部署在云厂商同地域内网，可把 `APP_S3_ENDPOINT_URL` 配成内网 endpoint，同时保持 `APP_S3_PUBLIC_ENDPOINT_URL` 为浏览器可访问的公网或自定义域名 endpoint。

## 启动生命周期

`backend` 启动时会：

1. 读取环境变量。
2. 连接 S3 兼容对象存储并确保 bucket 存在。
3. 初始化 SQLite schema 和默认模特数据。
4. 启动 generation poller。
5. 启动 planner poller。

如果本机 RustFS 尚未完全 ready，后端可能短暂启动失败，Compose 会按 `restart: unless-stopped` 自动重启。使用外部 S3 兼容服务时，应确认 endpoint、bucket、region、addressing style 和凭证均已生效。运维侧应以 `docker compose ps` 和 `/api/health` 为最终状态判断。

## 生产边界

- 根目录 `.env.example` 是唯一提交到 Git 的配置模板；根目录 `.env` 是本地/Compose 主配置源，不提交 Git。
- 后端裸跑同样读取根目录 `.env`；`backend/.env.local` 仅允许作为本机临时覆盖文件，不作为共享配置源。
- `.env` 不提交 Git，生产必须使用强随机 `APP_S3_ACCESS_KEY_ID` / `APP_S3_SECRET_ACCESS_KEY`；即使默认 RustFS 部署也使用这组通用 S3 命名。
- 生产环境设置 `APP_ENV=production`；后端启动时会拒绝缺失或默认对象存储密钥。
- 生产环境必须设置固定账号密码或 hash：`APP_USER_PASSWORD_HASH` / `APP_ADMIN_PASSWORD_HASH`，并设置 `APP_SESSION_SECRET`；如果访问控制由 HTTPS 反向代理或企业网关完成，则必须显式设置 `APP_TRUST_REVERSE_PROXY_AUTH=true`。
- `APP_SESSION_COOKIE_SECURE` 在 HTTPS 部署中应设置为 `true`；本地或 HTTP 测试环境才允许为 `false`。
- 轮换访问密码时，如果需要立即踢出所有已登录浏览器，必须同步轮换 `APP_SESSION_SECRET`。
- `APP_API_ACCESS_TOKEN` 只作为脚本/运维调用的可选 fallback，不作为浏览器前端门禁，也不注入前端静态包。
- `TOAPIS_API_KEY`、`FAL_KEY` 和 `GEMINI_API_KEY` 只放在部署环境，不写入文档或镜像。
- 对象存储 bucket 保持私有，浏览器只通过后端生成的短期预签名 URL 访问对象。
- `APP_S3_BUCKET` 必须符合 S3 bucket 命名规则：3-63 个字符，只用小写字母、数字、短横线和点号；`APP_STORAGE_BUCKET` 仅作为旧配置别名保留。
- 对公网开放时必须接入 HTTPS 反向代理，并把 public URL 改为 HTTPS。
- 当前 SQLite 适合 MVP 单机部署；多人高并发或多副本后端需要迁移到外部数据库。
- 当前 planner/generation poller 运行在后端进程内；多后端副本会造成重复扫描风险，扩展前应拆成单独 worker 或加分布式锁。
