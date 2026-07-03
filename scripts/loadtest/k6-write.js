// 写并发压测:并发创建搭配服装(multipart 上传小图),暴露 SQLite 单写锁 +
// 衍生图生成(Pillow CPU)+ RustFS 写入的瓶颈。
//
// 运行(服务器上):
//   k6 run -e BASE_URL=http://127.0.0.1:8000 -e USERNAME=user -e PASSWORD=xxx scripts/loadtest/k6-write.js
//
// 数据卫生(仓库规则:测试不得污染业务数据):
// - 所有测试数据 code 以 LOADTEST- 开头;
// - teardown 阶段自动按关键字检索并逐个 DELETE 清理;
// - 若压测中途手动停止(teardown 未执行),用 runbook 中的清理命令兜底。
// 不建议对生产库频繁运行;运行前先备份 SQLite。

import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Counter } from "k6/metrics";
import { b64decode } from "k6/encoding";

const BASE = __ENV.BASE_URL || "http://127.0.0.1:8000";
const USERNAME = __ENV.USERNAME || "user";
const PASSWORD = __ENV.PASSWORD || "";
const RUN_ID = `LOADTEST-${Date.now()}`;

// 1x1 红色 PNG(67 字节),只为触发完整链路:校验 → RustFS 写 → 衍生图 → SQLite 事务
const TINY_PNG = b64decode(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAHOgt+QAAAABJRU5ErkJggg=="
);

const createLatency = new Trend("garment_create_latency", true);
const lockErrors = new Counter("write_conflicts");

export const options = {
  scenarios: {
    writers: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: 5 },
        { duration: "1m", target: 15 },
        { duration: "30s", target: 0 },
      ],
    },
  },
  thresholds: {
    garment_create_latency: ["p(95)<3000"],
    http_req_failed: ["rate<0.05"],
  },
};

function login() {
  const res = http.post(
    `${BASE}/api/auth/login`,
    JSON.stringify({ username: USERNAME, password: PASSWORD }),
    { headers: { "Content-Type": "application/json" } }
  );
  const cookie = res.cookies["ruyi_session"];
  return cookie && cookie.length ? cookie[0].value : null;
}

export function setup() {
  return { session: login(), runId: RUN_ID };
}

export default function (data) {
  const jar = http.cookieJar();
  if (data.session) jar.set(BASE, "ruyi_session", data.session);

  const code = `${data.runId}-${__VU}-${__ITER}`;
  const res = http.post(
    `${BASE}/api/styling-garments`,
    {
      code: code,
      files: http.file(TINY_PNG, `${code}.png`, "image/png"),
    },
    { tags: { name: "garment_create" } }
  );
  createLatency.add(res.timings.duration);
  const ok = check(res, { "create 200": (r) => r.status === 200 });
  if (!ok && (res.status === 500 || String(res.body).includes("locked"))) {
    lockErrors.add(1); // SQLite 锁冲突的典型表现
  }
  sleep(Math.random() * 1 + 0.5);
}

export function teardown(data) {
  // 清理本次运行产生的所有 LOADTEST 数据
  const jar = http.cookieJar();
  const session = login();
  if (session) jar.set(BASE, "ruyi_session", session);

  let cleaned = 0;
  for (let page = 0; page < 100; page++) {
    const list = http.get(
      `${BASE}/api/styling-garments?limit=200&keyword=${data.runId}`
    );
    const items = list.json("items") || [];
    if (!items.length) break;
    for (const it of items) {
      const del = http.del(`${BASE}/api/styling-garments/${it.id}`);
      if (del.status === 200) cleaned++;
    }
  }
  console.log(`teardown: cleaned ${cleaned} LOADTEST garments (runId=${data.runId})`);
}
