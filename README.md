# 门店周报 · 同店对比

Flask + SQLite 的单店日报导入与「两个周一」同店对比工具。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 按门店 + 日期导入日报，填写修订号与已报齐的营业小时 | 导入页 / `POST /import`、`POST /api/import` |
| 门店身份按 Unicode `casefold` 判定（`MÜN-01` 与 `mün-01` 是同一家店） | `services.store_code_key` + `stores.code_key` |
| 同店同日只接受**严格高于已生效修订号**的报送，换写法不能绕过；并发导入同规则 | `services.import_daily_report`（按 casefold 组取最高生效修订号）+ `BEGIN IMMEDIATE` 串行写 |
| 小票 CSV 两列：`小时,金额分`（表头可省） | `weekly_compare/receipts.py` |
| 只接受**更高修订号**整日替换 | `services.import_daily_report` + `report_versions` 唯一约束 |
| 更正可缩减已报齐的营业小时（整日替换，不做增量合并） | 新修订写入新的 `report_hours` 集合 |
| 同店日报分散在多种写法、日期互不重叠时，任一写法都能按日期查到；同店对比只列一行 | `_select_report_by_code` 精确优先 + casefold 回退；对比 SQL 按 `code_key` 分组 |
| 旧库同日多写法各存一份日报：各自原编码仍可查看，该店新报送一律拒收（冻结），原数据保留 | 迁移回填 `code_key`；`StoreConflict`（`store_conflict` / 409） |
| 对比所选两天只要包含冻结门店的日报，提示冲突日期并暂停出表 | `comparison.compare` 单条 SQL 内的 `conflicts` CTE |
| 对比只接受两个周一，按门店 + 星期 + 小时配对 | `comparison.validate_comparison_dates` |
| 只统计两周**都报齐**的营业小时 | `comparison` 的 `matched` CTE（小时 inner join） |
| 报齐但无小票的小时按 0 计 | 导入时为每个报齐小时写一行 `report_hours`（无小票则 0） |
| 列出各店与全店合计的可比小时数、两周销售额、差额 | 对比页 / `GET /api/compare` |
| 导入期间仍可查旧结果 | WAL + 导入只在最后一步切换 `current_reports` 指针 |
| 覆盖时营业小时与销售汇总一同生效 | 同一事务内写 `report_hours` / `report_versions` / 指针 |
| 失败保留原日报与结果 | 输入全部校验在事务前；事务内任何异常整体回滚 |
| 对比与更正都不重扫旧小票 | 对比只读 `report_hours`；更正写新版本 |
| 一次查询中的各店数字与全店合计取自同一提交状态 | 单条 SQL（`UNION ALL` 出合计与冲突行）+ 单个读事务 |

### 旧数据库升级

旧版本的库没有 `stores.code_key` 列。应用启动时 `db.migrate_schema` 会以
`ALTER TABLE` 幂等加列并用 Python `casefold` 回填，**旧库文件直接可用，无需导出
导入，原有任何一行数据都不删除、不合并**。若旧库中同一家店（casefold 相同）在
同一天以多种写法各有一份已生效日报，该店进入冻结状态：

- 每种写法的原日报仍可用各自原编码在 `/reports/<编码>/<日期>` 查看；
- 该店的任何新报送都返回 `store_conflict`（HTTP 409），原数据保持不变；
- 同店对比只要所选日期包含该店的日报，就返回冲突日期并暂停出表；
- 管理员清理掉重复日报（保留其一）后，冻结即解除。

## 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py          # http://127.0.0.1:5000
```

数据库默认 `./data/reports.db`，可用 `DATABASE_PATH` 覆盖。首次启动自动建表
（WAL 模式）。

## Docker Compose

```bash
docker compose up --build -d
# 访问 http://127.0.0.1:8000
docker compose ps         # web 为 healthy
docker compose logs -f
```

- 端口：`APP_PORT`（默认 8000）。
- 数据：命名卷 `report-data` 挂在 `/app/data`；`docker compose down` 保留数据，
  `docker compose down -v` 才删除。
- 生产部署请设置 `SECRET_KEY`：
  `SECRET_KEY=$(openssl rand -hex 32) docker compose up -d`。
- 容器以 uid 10001 运行；如需改用宿主机目录 bind mount，请确保该目录对该 uid 可写
  （SQLite 需要目录写权限，不只是文件写权限）。
- 健康检查/`HEALTHCHECK` 走 `GET /api/health`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 当前生效日报列表（可筛选日期/门店编码） |
| GET | `/import` | 导入表单（带现有修订号则预填 +1） |
| POST | `/import` | 表单导入（文件或粘贴） |
| GET | `/reports/<store_code>/<date>` | 单份日报的各小时金额 |
| GET | `/compare?date_a=&date_b=` | 同店对比页 |
| GET | `/api/compare?date_a=&date_b=` | 对比 JSON（含 `total`） |
| GET | `/api/reports?business_date=&store_code=` | 当前日报 JSON |
| POST | `/api/import` | 导入 JSON 响应 |
| GET | `/api/csrf` | 取 CSRF token（同时下发会话 cookie） |
| GET | `/api/health` | 存活/数据库检查 |

脚本调用示例：

```bash
TOKEN=$(curl -s -c jar -b jar http://127.0.0.1:8000/api/csrf | python3 -c 'import json,sys;print(json.load(sys.stdin)["csrf_token"])')
curl -s -c jar -b jar -X POST http://127.0.0.1:8000/api/import \
  -H "X-CSRF-Token: $TOKEN" \
  -F store_code=S001 -F store_name=甲店 -F business_date=2026-11-02 \
  -F revision=1 -F complete_hours=9-11 -F receipts=@receipts.csv
```

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

覆盖：CSV/小时解析、修订替换与拒绝、失败回滚（含事务中途数据库报错）、
可比小时配对、报齐无小票计零、各店合计等于全店合计、单条 SQL 快照、
WAL 下导入未提交时读旧值、HTTP 页面/API/CSRF/413。

## 已知限制

- 门店名称只能通过导入时更新；没有独立门店管理页。
- CSRF token 存在 Flask 签名 cookie 会话里；多实例部署需共享 `SECRET_KEY`。
- 对比一次只处理两个日期；跨更多周的趋势需多次查询。
- 历史修订版本保留在库中供追溯，但页面上只能看当前版本。
