# 联合目录补采采集服务

联合目录管理员对单馆书目进行补采的 OAI-PMH 采集服务。核心保证：**翻页失败绝不让半批记录入库**。

## 需求与实现对照

| 需求 | 实现 |
| --- | --- |
| FastAPI 接收截止日，首次另填起始日 | `POST /api/v1/harvest`，请求体 `until` 必填、`from` 选填 |
| 之后从上次成功采集的截止日接着采，起止日都包含 | `from` 留空时取 `harvest_progress.last_until` 作为起始日（含当天） |
| OAI-PMH ListRecords 按日期拉取 oai_dc | 首请求 `verb=ListRecords&metadataPrefix=oai_dc&from=...&until=...` |
| 续页用 resumptionToken 替代日期和格式参数 | 续页请求只带 `verb` + `resumptionToken`；样例源对混用参数返回 `badArgument` |
| 令牌失效丢弃已取页，按原区间重抓一次 | 捕获 `badResumptionToken` 后清空已取页，用原始 from/until 重新拉取；只重抓一次 |
| 按标识更新 SQLite 书目 | `bib_records.identifier` 主键 upsert，重复采集幂等 |
| 遇删除标记就撤下 | header `status=deleted` 的记录置为 `withdrawn`（库中不存在则写墓碑） |
| 整批采完才提交书目变更和截止日 | 单事务 `BEGIN IMMEDIATE`，全部页取完才写库并提交；异常整体回滚 |
| 无匹配也推进截止日 | `noRecordsMatch` 不写书目，同事务推进 `last_until` |
| 目录翻页不受两次请求之间补采影响 | 取首页时把已提交目录流式复制到临时快照，响应头返回游标；续页只读快照 |
| 保留原有 offset 调用和响应 | 无 `cursor` 时仍按 `status`/`limit`/`offset` 返回原来的 JSON 数组 |
| 整轮沿用首页的筛选和页大小 | 游标内记录首页选定的 `status`/`limit`，续页请求里的同名字段不再生效 |
| 同一游标重试返回同一页 | 快照只读且游标固定了页偏移，重复请求结果一致 |
| 游标自首页起 5 分钟有效 | 游标携带固定的过期时间；过期返回 `410`，提示从首页重来并删除该轮快照 |
| 不整份目录入内存、不跨请求持有主库读事务 | 快照按批 `fetchmany` 复制；主库读事务只在首页请求内存在 |
| Docker Compose 启动服务和本地分页样例源 | `docker compose up --build` 启动 `harvester-api` + `oai-source` |
| Swagger 发起采集并查询目录和进度 | http://localhost:8000/docs |

## 目录结构

```
app/                  FastAPI 采集服务
  main.py             路由（采集/目录/进度/运行历史/健康检查）
  catalog.py          目录首页快照、游标编解码、过期回收
  harvester.py        采集编排：翻页、令牌失效重抓、整批事务
  oai_client.py       OAI-PMH 2.0 客户端（httpx + xml.etree）
  db.py               SQLite 表结构与事务操作
  schemas.py          Pydantic 请求/响应模型
  Dockerfile
sample-oai/           本地分页样例 OAI-PMH 源（纯标准库）
  server.py           27 条样例记录（rec-013 为删除记录），PAGE_SIZE=10，3 页
  Dockerfile
tests/                pytest 端到端测试（含目录快照翻页用例）
scripts/              辅助脚本（故障回滚演示）
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集服务 API / Swagger：http://localhost:8000/docs
- 样例 OAI 源：http://localhost:8080/oai?verb=Identify
- SQLite 数据保存在命名卷 `harvester-data`（容器内 `/data/harvester.db`）

## 操作流程（Swagger 或 curl）

### 1. 首次采集：另填起始日，起止日都包含

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'
```

响应：

```json
{
  "status": "success",
  "from_date": "2026-10-01",
  "until_date": "2026-10-15",
  "pages": 2, "fetched": 15,
  "inserted": 14, "updated": 0, "withdrawn": 1,
  "retries": 0, "run_id": 1
}
```

### 2. 后续采集：只填截止日，自动从上次截止日（含）续采

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-27"}'
# 实际区间：2026-10-15 ~ 2026-10-27（last_until 含当天，避免边界漏采）
```

### 3. 查询目录与进度

```bash
curl 'http://localhost:8000/api/v1/catalog?status=active'   # 在架（默认）
curl 'http://localhost:8000/api/v1/catalog?status=all'      # 含已撤下
curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-013'
curl 'http://localhost:8000/api/v1/progress'                # 上次成功截止日
curl 'http://localhost:8000/api/v1/runs'                    # 采集运行历史
```

### 4. 固定目录并游标翻页（两页之间补采撤下也不会漏页）

不带 `cursor` 的第一次请求就是原来的 `offset` 调用，响应体仍然是书目数组；
首页响应头会多出 `X-Next-Cursor` 与 `X-Catalog-Cursor-Expires-At`：

```bash
# 首页：固定本轮目录（status=active, limit=10），并拿到游标
curl -D - 'http://localhost:8000/api/v1/catalog?status=active&limit=10&offset=0'

# 续页：只带 cursor；整轮沿用首页的 status/limit
curl -H 'X-Next-Cursor: <上一步响应头里的游标>' \
  'http://localhost:8000/api/v1/catalog?cursor=<游标>'

# 同一游标重试仍返回同一页；末页不再返回 X-Next-Cursor
```

本轮快照自首页创建起 **5 分钟**有效。过期或快照已回收时接口返回 `410`，
提示从首页重新获取，并删除该轮临时快照。游标续页期间补采照常提交；新首页
会反映最新状态，但不会影响已经开始的那一轮。

#### 对照演示：翻页途中撤下一条

```bash
# 1) 先完成一次全区间采集，目录有 26 条在架记录
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-27"}'

# 2) 在 Swagger 取首页，记下响应头 X-Next-Cursor（limit=10 -> 共 3 页）

# 3) 让样例源把首页中的 rec-005 标记为删除，再补采该日期将其撤下
curl -X POST 'http://localhost:8080/admin/withdraw?identifier=oai:sample.lib:rec-005'
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-05", "until": "2026-10-05"}'

# 4) 用第 2 步的游标续页：仍是首页时刻的第二页，没有漏掉 rec-011
# 5) 重新取首页：rec-005 已不在架，rec-011 补进首屏
curl -X POST http://localhost:8080/admin/reset   # 恢复样例记录与令牌状态
```

可选管理端点：`/admin/withdraw?identifier=...`、`/admin/restore?identifier=...`、
`/admin/toggle?identifier=...`、`/admin/reset-records`，以及原有的
`/admin/fail-next`、`/admin/reset`。

### 5. 无匹配也推进截止日

对没有任何记录的区间发起采集，返回 `fetched=0` 且 `last_until` 照常推进。

## 令牌失效重抓的验证方法

样例源提供故障注入端点：

```bash
# 令下一次 resumptionToken 请求返回 badResumptionToken
curl -X POST http://localhost:8080/admin/fail-next
curl http://localhost:8080/admin/state

# 全区间 27 条共 3 页：第 1 页取到后续页令牌失效 -> 丢弃该页 -> 原区间重抓成功
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-27"}'
# 响应中 retries=1；运行历史与服务日志可查
curl -X POST http://localhost:8080/admin/reset   # 清除故障开关
```

若重抓过程中令牌**再次**失效（不可恢复），接口返回 `502`，本批所有书目变更回滚、
截止日保持不变，`harvest_runs` 中记录一条 `failed` 历史——任何半批数据都不会进库。

## 事务与一致性说明

- 采集过程先在内存中收集全部页面，**不向 SQLite 写任何数据**；
- 全部页成功后，在单个 `BEGIN IMMEDIATE` 事务内完成 upsert、撤下、截止日推进、运行历史更新；
- 事务中任何一步失败都会 rollback，库状态回到采集前；
- 进程内互斥锁保证同一时刻只有一个采集任务，并发请求返回 `409`；
- 目录首页在**本请求内**用一个主库读事务把已提交目录按批复制到临时快照库，
  请求结束即释放连接；后续续页只读快照，不跨请求持有主库读事务。WAL 模式下
  该读事务不会阻塞补采的写事务，补采可照常提交；
- 快照按 500 行一批流式复制，不会一次把整份目录读入内存；默认部署为单进程
  （`uvicorn app.main:app`，单 worker），游标签名与快照回收都在进程内完成。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 终端 1：样例 OAI 源
python3 sample-oai/server.py

# 终端 2：API
APP_DB_PATH=./data/harvester.db OAI_ENDPOINT=http://127.0.0.1:8080/oai \
  uvicorn app.main:app --reload

# 测试
pytest
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OAI_ENDPOINT` | `http://oai-source:8080/oai` | OAI-PMH 端点 |
| `APP_DB_PATH` | `/data/harvester.db` | SQLite 数据库路径 |
| `APP_SNAPSHOT_DIR` | 主库同目录下的 `catalog_snapshots/` | 目录快照临时目录 |
| `CATALOG_CURSOR_TTL_SECONDS` | `300` | 目录游标自首页起的有效期秒数 |
| `PAGE_SIZE`（样例源） | `10` | 样例源每页记录数 |
| `TOKEN_TTL`（样例源） | `3600` | 样例源 resumptionToken 有效期秒数 |
