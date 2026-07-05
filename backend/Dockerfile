FROM python:3.12-slim@sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.constraints.txt

COPY app ./app

# 构建时注入 git commit，供 /api/version 做部署校验（缺省为空不影响运行）。
ARG GIT_COMMIT=""
ENV APP_GIT_COMMIT=${GIT_COMMIT}

RUN groupadd --system app \
  && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
  && mkdir -p /app/data/logs \
  && chown -R app:app /app/data

EXPOSE 8000

USER app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
