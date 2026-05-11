# 本地开发说明

## 推荐方式

日常开发推荐前后端裸跑，只用 Docker Compose 启动 RustFS。这样前端热更新、后端 reload 和调试都更轻。

```bash
cd /Users/fushuai/Desktop/项目/Lily/ruyi-dream
docker compose up -d rustfs
```

RustFS 默认地址：

- S3 API: `http://127.0.0.1:9000`
- Console: `http://127.0.0.1:9001`

## 后端本地启动

```bash
cd backend
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

本地 `backend/.env` 常用配置：

```env
APP_S3_ENDPOINT_URL=http://127.0.0.1:9000
APP_S3_PUBLIC_ENDPOINT_URL=http://127.0.0.1:9000
APP_STORAGE_BUCKET=ruyi-dream
RUSTFS_ACCESS_KEY=admin
RUSTFS_SECRET_KEY=change-me
BACKEND_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

需要真实策划和生图时再补：

```env
GEMINI_API_KEY=
FAL_KEY=
```

健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

## 前端本地启动

```bash
cd frontend
npm install
npm run dev
```

默认访问：

```text
http://127.0.0.1:5173
```

如果后端不是 `http://127.0.0.1:8000`，在 `frontend/.env.local` 写入：

```env
VITE_API_BASE_URL=http://127.0.0.1:8000
```

## 本地完整 Compose 启动

如果要模拟交付环境，可以直接跑完整 Compose：

```bash
cp .env.example .env
# 编辑 .env，至少设置 RUSTFS_ACCESS_KEY / RUSTFS_SECRET_KEY
docker compose up -d --build
docker compose ps
```

访问：

- 前端：`http://127.0.0.1:5173`
- 后端健康检查：`http://127.0.0.1:8000/api/health`
- RustFS Console：`http://127.0.0.1:9001`

前端镜像构建时会固化 `PUBLIC_BACKEND_URL`。如果改了 `.env` 里的 `PUBLIC_BACKEND_URL`，需要重新构建前端：

```bash
docker compose up -d --build frontend
```

## 常用检查

```bash
npm --prefix frontend run build
python3 -m compileall backend/app
docker compose config
```

## 清理本地数据

谨慎执行，下面命令会删除本地应用数据和对象存储数据：

```bash
docker compose down
rm -rf deployment/app-data/backend/*
rm -rf deployment/rustfs/data/*
```

保留 `.gitkeep` 文件不是运行必须，只是为了让空目录留在 Git 中。
