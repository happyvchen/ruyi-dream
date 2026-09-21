# 单机部署压测与体验评估 Runbook

适用场景:所有服务(backend + frontend + RustFS)部署在同一台服务器,评估网络、存储、并发对用户体验的影响。配套脚本在 `scripts/loadtest/`。

## 压测位置原则

| 测试内容 | 在哪跑 | 原因 |
|---|---|---|
| API / 写并发 / RustFS 存储压测 | **服务器本机** | 排除 VPN/外网干扰,测服务器自身瓶颈 |
| 公网基线(ToAPIs) | **服务器本机** | AI 链路的公网出口在服务器 |
| 用户路径(延迟/带宽/端到端加载) | **用户侧电脑** | 测的就是用户到服务器这段链路 |

注意:如果你从 VPN 连入,从你电脑测出的结果是"经 VPN 的体验",是真实用户走 VPN 时的真实数据;若真实用户走内网/公网直连,该结果只能当下限参考。

## 0. 准备(服务器上)

```bash
# k6
sudo gpg -k && sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
  --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt update && sudo apt install k6
# 观测工具
sudo apt install -y sysstat iperf3
```

压测期间在另一个终端保持观测:

```bash
docker stats                 # 各容器 CPU/内存
iostat -x 2                  # 磁盘利用率(%util 接近 100 = 磁盘瓶颈)
```

## 1. 网络基线

**公网出口(服务器 → ToAPIs,影响 AI 调用与生成图下载):**

```bash
curl -o /dev/null -s -w 'connect=%{time_connect}s ttfb=%{time_starttransfer}s speed_down=%{speed_download}B/s\n' https://toapis.cn
# 上行粗测(打标/选搭会 POST 几 MB base64 图片):
dd if=/dev/urandom of=/tmp/up5m bs=1M count=5
curl -o /dev/null -s -w 'speed_up=%{speed_upload}B/s\n' -X POST --data-binary @/tmp/up5m https://toapis.cn
```

**用户路径(你的电脑 → 服务器,经 VPN):**

```bash
# 服务器: iperf3 -s
# 你的电脑:
iperf3 -c 10.16.34.11            # 上行
iperf3 -c 10.16.34.11 -R         # 下行(决定图片加载速度)
ping 10.16.34.11                  # 延迟基线
```

判读:下行 < 50 Mbps 时,一屏 12 张预览图(约 100-300KB/张)加载会有可感知延迟;延迟 > 50ms 会放大"每张图两跳"(见第 3 节)的影响。

## 2. 存储层(RustFS)

用 MinIO 的 warp(S3 兼容)在服务器本机压:

```bash
# warp v1.4+ 不再发布二进制,用 docker 镜像跑(--network host 直连本机 9000)
docker run --rm --network host minio/warp mixed \
  --host 127.0.0.1:9000 --access-key <AK> --secret-key <SK> \
  --duration 2m --objects 500 --obj.size 512KiB
# 无 docker 环境可用最后一个二进制版本:
# wget https://github.com/minio/warp/releases/download/v1.3.1/warp_Linux_x86_64.tar.gz -O - | tar xz
```

关注 GET/PUT 吞吐与 p99。单机 NVMe 一般远超业务需要;若 %util 高且吞吐低,检查磁盘类型和 Docker 存储驱动。

## 3. 只读浏览并发(核心用户体验)

```bash
k6 run -e BASE_URL=http://127.0.0.1:8000 -e USERNAME=user -e PASSWORD=<密码> \
  scripts/loadtest/k6-browse.js
```

模拟 5 → 20 → 50 并发用户浏览搭配库(列表 + 12 张封面缩略图)。阈值:列表 p95 < 800ms,缩略图 p95 < 1.5s,错误率 < 1%。

**判读要点**:图片链路是 `GET /api/media` → 后端查库 + 对象 HEAD + presign → 302 重定向 → RustFS。非下载图片响应头是 `Cache-Control: private, max-age=...`;默认 `thumb` 约 40-50 分钟、`preview` 约 30-40 分钟、非下载 `original` 约 10-20 分钟,且按 object key 稳定抖动以错开过期时间。下载请求仍使用 `private, no-store`。并发上来后,首次进页面、缓存过期和强制刷新后的图片请求仍是后端 QPS 的主要来源。若 thumb p95 严重劣化而 warp 结果健康,瓶颈在后端(事件循环/SQLite 查询),不在存储。

## 4. 写并发(SQLite 单写锁)

```bash
k6 run -e BASE_URL=http://127.0.0.1:8000 -e USERNAME=user -e PASSWORD=<密码> \
  scripts/loadtest/k6-write.js
```

并发 5 → 15 导入小图,覆盖"校验 → RustFS 写 → 衍生图生成 → SQLite 事务"全链路。关注 `garment_create_latency` p95 和 `write_conflicts` 计数(SQLite 锁冲突)。

**数据卫生**:所有测试数据 code 以 `LOADTEST-` 开头,脚本 teardown 自动清理。中途 Ctrl+C 导致残留时手动兜底:在页面按关键字 `LOADTEST` 检索删除,或调 API 逐个 `DELETE /api/styling-garments/{id}`。**对生产库运行前先备份 SQLite**(见 deployment.md 备份章节)。

## 5. 组合场景:生成中浏览是否卡顿

最贴近真实的并发体验测试:

1. 服务器上启动第 3 节的 browse 压测(20 VU 档);
2. 同时在页面上手动发起 **1 个**真实生成任务(消耗额度,只跑一次);
3. 对比生成期间与空闲期间的 thumb/list p95。

若生成期间浏览 p95 明显劣化,常见原因依次为:后端事件循环被同步调用阻塞、CPU 被衍生图/图片下载占满、磁盘写挤占读。结合 `docker stats` / `iostat` 定位。

**不要用脚本批量触发 AI 生成来压测**——按额度计费,且违反仓库"禁止无谓消耗 provider 额度"规则。AI 链路的耗时评估用单次真实任务的分段计时(导入参考图 / 生成 / 下载落盘)即可。

## 6. 端到端体验(用户侧,经 VPN)

你的电脑上:浏览器打开搭配库页面,DevTools Network 面板记录:

- 列表接口耗时 vs 图片总加载耗时(定位是后端慢还是带宽慢);
- 普通刷新两次对比:第二次应明显受浏览器缓存影响;若短时间内两次耗时接近且 `/api/media` 全量回源,检查前端是否替换了未变化图片的 `src`、是否触发了 DevTools 的 Disable cache,或响应头是否被代理覆盖。
- Network 面板抽查图片响应头:`/api/media` 的 302 和最终 RustFS 图片响应都应带 `Cache-Control`;不同图片的 `max-age` 可不同,这是 TTL 抖动的预期行为。

## 已知架构因素(压测前心里有数)

1. SQLite 单写锁 + 同步 DB 调用:写并发的天花板,读多写少场景影响有限。
2. 图片分档私有缓存 + 每图两跳(后端 redirect + RustFS):首次加载、缓存过期和强制刷新仍是浏览类 QPS 放大器;TTL 抖动用于降低同一批图片同时过期的尖峰。
3. 衍生图生成(Pillow)是导入时同步 CPU 开销。
4. GPT-Image-2 生成几十秒 + 5s 轮询:体验主导项,与服务器性能无关。
5. 公网上行影响打标/选搭(base64 图片上行)与参考图导入;公网下行影响生成图回落。

## P0：同口径五分钟采样

浏览器开启 Preserve log，记录完整 5 分钟窗口，包含 API 302 和 RustFS 最终响应；冷缓存时清理站点 HTTP cache，热缓存时保持缓存开启。按“30 图列表→查看 10 张 preview→显式原图→8×25 MB 上传→下载→观察任务”分别导出 HAR，不把不同操作混成单个平均值。HAR 本身可能包含凭据，保留在受控目录，不提交仓库。

```bash
python3 scripts/loadtest/summarize-har.py /受控目录/before.har > /受控目录/before-summary.json
python3 scripts/loadtest/summarize-har.py /受控目录/after.har > /受控目录/after-summary.json
```

汇总工具仅输出 API、媒体控制响应、thumb、preview、original、上传、下载等分类的请求数、字节、失败数、重复 PUT 与峰值 PUT 并发，不输出 URL/请求头/正文。优先用 HAR transferSize，缺失时用 bodySize；未知字节计为 0，必须核对采样是否完整，不能把缺失值解释成零流量。302 日志字节不代表 RustFS 最终传输字节。

同时保存：主机规格、`docker stats`（CPU/RSS）、`iostat -xz 1`（磁盘）、`sar -n DEV,TCP,ETCP 1`（网卡/TCP 重传），客户端到服务器的 `ping` RTT/丢包及已获准测试端点的 `iperf3` 上下行有效吞吐。逐秒时间戳与 HAR 窗口对齐；不要在承载业务的链路直接开无界满载测试。日志仅使用脱敏路由模板，不记录签名 URL、Cookie、Authorization、Prompt 或 provider 正文。

标准门槛：status <2 KB；五分钟无变化 ≤50 次（2/5/10/15 秒退避）；20 浏览器观察总流量 ≤15 MB；默认列表/检查器 original=0；30 图列表 <2 MB；可见图片无需交互全部加载，离屏图片不提前请求；全标签页 PUT 峰值默认 ≤2。切页与搜索测试需人为延迟 A/B/C 响应并乱序返回，验证 Abort 与 request token。只使用 mock provider 和隔离数据库/对象前缀。

### 2026-09-21 本地实现验证样本

同一临时 SQLite 数据集：30 个草稿任务，每个 2 张登记图片，无 attempt；旧版取本轮开始时的 Git HEAD 序列化函数，新版取工作区实现。未访问生产数据库、对象存储或外部 provider。

| 指标 | 旧版 | P0 |
|---|---:|---:|
| 30 任务摘要 JSON | 40,412 B | 15,468 B |
| 同一摘要 gzip level 5 | 1,711 B | 839 B |
| 摘要装配 SELECT 次数 | 330 | 3 |
| 单任务 status JSON（含空 operations） | 无 | 297 B |

该样本用于验证查询和 DTO 收敛，不代表生产任务复杂度，也不是百兆网络测速。旧方案中 56.7 KB 详情、761.9 KB 列表等抽样来自另一组数据，不能与本表混算改善比例。前端使用虚拟时钟验证五分钟退避、隐藏暂停、原图显式切换、可视区图片加载和全局 PUT 并发；实际 WAN 吞吐、RTT、丢包、SSH 回显、冷/热媒体字节及存量派生图补齐尚需部署环境验收，不宣称已达到生产时延目标。
