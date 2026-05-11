# RustFS 对象存储部署

本产品统一使用一种存储契约：S3 兼容对象存储。私有化部署默认使用 Docker 运行 RustFS。后续云部署时，同一套应用 S3 配置也可以指向阿里云 OSS、腾讯云 COS、AWS S3、GCS 兼容接口或其他 S3 兼容服务。

项目根目录的 `docker-compose.yml` 已经把 `frontend`、`backend` 和 `rustfs` 编排在一起，是当前推荐的完整部署入口。本文档保留 RustFS 单服务部署说明，用于只启动对象存储、本地开发或独立运维 RustFS。

单服务部署的容器名固定为 `ruyi-dream-rustfs`，避免和同机其它 RustFS / S3 兼容服务混淆。

本部署方案将运行状态保存在容器外：

| 宿主机路径 | 容器路径 | 用途 |
|---|---|---|
| `./data` | `/data` | 对象数据目录，最关键的持久化目录。 |
| `./config` | `/etc/rustfs` | 实例配置和运维备注目录。 |
| `./certs` | `/certs` | 可选 TLS 证书目录，只读挂载。 |
| `./logs` | `/var/log/rustfs` | RustFS 观测/审计日志目录，启用文件日志时使用。 |
| Docker JSON 日志 | Docker 管理 | 进程标准输出/错误日志，Compose 中已配置轮转。 |

## 为什么使用 RustFS

MinIO CE 已不适合作为新私有化部署的默认选项，因为公开的 `minio/minio` 仓库已经不再延续过去活跃维护的社区发行模式。RustFS 兼容 S3，采用商业友好的 Apache 2.0 许可证，相比 Ceph 这类完整分布式存储平台，也更适合作为本产品的默认私有化对象存储。

官方参考：

- RustFS Docker 安装：<https://docs.rustfs.com/installation/docker/>
- RustFS S3 兼容性：<https://docs.rustfs.com/features/s3-compatibility>

## 宿主机准备

在本目录下执行：

```bash
cd deployment/rustfs
cp .env.example .env
```

编辑 `.env`，将 `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` 替换为足够长的随机值。局域网部署时，将 `RUSTFS_SERVER_DOMAINS` 设置为浏览器可以访问到的服务器 IP 或内部域名。

RustFS 官方 Docker 指南说明，挂载目录必须能被容器运行用户写入。如果宿主机出现权限错误，可将目录所有者调整为 UID `10001`：

```bash
sudo chown -R 10001:10001 data config certs logs
sudo chmod 750 data config certs logs
```

生产环境建议使用本地 SSD/NVMe。对象数据目录 `/data` 不建议挂载在 NFS 上。

## 启动

只启动对象存储时，在本目录执行：

```bash
docker compose up -d
docker compose ps
docker compose logs -f rustfs
```

完整应用部署请回到项目根目录执行：

```bash
docker compose up -d --build
```

RustFS 默认暴露：

- S3 API：`http://<host>:9000`
- 控制台：`http://<host>:9001`

除非已经配置反向代理和 TLS 策略，否则只应在客户私有网络内开放这些端口。

如果需要健康检查，建议从宿主机或监控系统探测 S3 API，不依赖容器镜像内部工具：

```bash
curl -fsS http://localhost:9000/health/live
```

## 创建应用存储桶

可以使用任意 S3 兼容客户端。下面示例使用 MinIO 客户端作为运维命令行工具；应用自身应使用标准 AWS S3 SDK。

```bash
set -a
. ./.env
set +a
mc alias set app-rustfs http://localhost:9000 "$RUSTFS_ACCESS_KEY" "$RUSTFS_SECRET_KEY"
mc mb "app-rustfs/$APP_STORAGE_BUCKET"
mc ls app-rustfs
```

生产环境保持存储桶私有。后端完成用户鉴权后，再返回短期有效的预签名 URL 给浏览器上传和预览。

## 后端配置

后端已经统一使用 S3 兼容对象存储。单机私有化部署时，可以按下面配置连接本机 RustFS：

```env
APP_S3_ENDPOINT_URL=http://127.0.0.1:9000
APP_S3_PUBLIC_ENDPOINT_URL=http://127.0.0.1:9000
APP_STORAGE_BUCKET=ruyi-dream
APP_S3_REGION=auto
APP_S3_FORCE_PATH_STYLE=true
APP_S3_PRESIGN_EXPIRES_SECONDS=900
RUSTFS_ACCESS_KEY=admin
RUSTFS_SECRET_KEY=replace-with-secret-key
```

后端服务访问对象存储时使用内部 Docker/网络访问地址；生成浏览器访问的预签名 URL 时使用公网或局域网可访问地址。

## 生产注意事项

- 客户生产部署前，应在验证后固定 RustFS 镜像标签或摘要，不建议长期使用 `latest`。
- RustFS 如果暴露到可信局域网之外，必须启用 TLS。证书可挂载到 `./certs`，并按官方 TLS 文档设置 RustFS 环境变量。
- 保持 Docker 日志轮转开启。当前 Compose 将标准输出/错误日志限制为 `10 x 100m`。
- `./data` 和应用数据库必须一起备份。应用元数据和 RustFS 对象数据需要保持一致。
- 监控磁盘用量，在数据卷写满前告警。
- 人员变更或交付包转移时，应轮换访问密钥。
- 不要将存储桶设置为公开访问。图片读取和上传应通过后端鉴权后生成的预签名 GET/PUT URL 完成。

## 停止和升级

```bash
docker compose pull rustfs
docker compose up -d
docker compose logs -f rustfs
```

生产升级前，先快照或备份 `./data`，并记录当前镜像摘要：

```bash
docker inspect --format='{{index .RepoDigests 0}}' rustfs/rustfs:latest
```
