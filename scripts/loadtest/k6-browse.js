// 只读浏览压测:模拟用户浏览搭配服装库(列表 + 封面缩略图)。
// 在部署服务器本机运行,排除 VPN/外网干扰,测的是后端 + SQLite 读 + RustFS 读的综合能力。
//
// 运行(服务器上):
//   k6 run -e BASE_URL=http://127.0.0.1:8000 -e USERNAME=user -e PASSWORD=xxx scripts/loadtest/k6-browse.js
// 阶梯并发可覆盖:
//   k6 run -e STAGE_VUS=50 ... (默认 5 → 20 → 50)
//
// 说明:
// - 图片链路是 GET /api/media?key=..&variant=thumb → 后端查库+HEAD 对象+presign → 307 → RustFS。
//   k6 默认跟随重定向,因此单次"拉缩略图"完整覆盖 后端+RustFS 两跳。
// - 只读,不产生任何业务数据。

import http from "k6/http";
import { check, sleep } from "k6";
import { Trend } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://127.0.0.1:8000";
const USERNAME = __ENV.USERNAME || "user";
const PASSWORD = __ENV.PASSWORD || "";
const THUMBS_PER_PAGE = Number(__ENV.THUMBS_PER_PAGE || 12);

const listLatency = new Trend("garment_list_latency", true);
const thumbLatency = new Trend("thumb_latency", true);

export const options = {
  scenarios: {
    browse: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: 5 },
        { duration: "2m", target: 20 },
        { duration: "2m", target: Number(__ENV.STAGE_VUS || 50) },
        { duration: "1m", target: 0 },
      ],
    },
  },
  thresholds: {
    // 列表接口 p95 < 800ms、缩略图(含 redirect 两跳)p95 < 1.5s、错误率 < 1%
    garment_list_latency: ["p(95)<800"],
    thumb_latency: ["p(95)<1500"],
    http_req_failed: ["rate<0.01"],
  },
};

export function setup() {
  const res = http.post(
    `${BASE}/api/auth/login`,
    JSON.stringify({ username: USERNAME, password: PASSWORD }),
    { headers: { "Content-Type": "application/json" } }
  );
  check(res, { "login ok": (r) => r.status === 200 });
  // 免登录部署(本地开发)时 login 返回 authRequired:false,无 cookie 也能访问。
  const cookie = res.cookies["ruyi_session"];
  return { session: cookie && cookie.length ? cookie[0].value : null };
}

export default function (data) {
  const jar = http.cookieJar();
  if (data.session) jar.set(BASE, "ruyi_session", data.session);

  // 1) 列表页
  const list = http.get(`${BASE}/api/styling-garments?limit=60`, {
    tags: { name: "garment_list" },
  });
  listLatency.add(list.timings.duration);
  const ok = check(list, { "list 200": (r) => r.status === 200 });
  if (!ok) {
    sleep(1);
    return;
  }

  // 2) 拉前 N 张封面缩略图(浏览器真实行为)
  const items = (list.json("items") || []).filter((it) => it.coverUrl);
  const targets = items.slice(0, THUMBS_PER_PAGE);
  for (const it of targets) {
    const res = http.get(`${BASE}${it.coverUrl}`, {
      tags: { name: "thumb" },
      responseType: "none", // 不在 k6 内存里保留图片内容
    });
    thumbLatency.add(res.timings.duration);
    check(res, { "thumb 200": (r) => r.status === 200 });
  }

  sleep(Math.random() * 2 + 1); // 模拟用户浏览停顿 1-3s
}
