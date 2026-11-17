# cc-retain-lab

CC_Codex 二期（26.11）**数据工程**产品线：**留存裁撤台**（FastAPI + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**分区留存策略版本 → 到期扫描裁决 → 归档或销毁执行 → 策略与实物分区对账**

与现有六线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取）。  
也不碰外仓常见指纹（Feed 归并 / 作业编排 / CDC 游标 / 质检签发 / 贴源-明细-汇总）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数仓与数据湖（分区留存 / 裁撤闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、FastAPI、Uvicorn、SQLite |
| 前端 | 原生 JS 单页（全中文），青灰底 + 铜强调，顶栏导航（非绿侧栏克隆） |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 现行留存策略（主题域 / 保留天 / 到期动作）  
2. 实物分区列表（在线 / 归档 / 销毁）  
3. 按基准日到期扫描 → 生成待执行裁决  
4. 单条裁决执行（归档写地址 / 销毁清零）  
5. 策略与实物对账报告  

## 宽限治理（宽限未到期禁止销毁）

**同源判定**：`app/services/grace.py::describe_partition` 是「分区此刻能否执行」的唯一判定源。
扫描卡片、裁决列表、分区详情、执行落库全部调用它，不存在「列表显示宽限中、却能点销毁」的分叉。

- **宽限来源**：人工登记（`partitions.manual_grace_until`）与未完成补采任务（`backfill_jobs.grace_until`）取较晚者。
- **宽限未到期**：销毁一律拦截；归档不受影响。裁决仍会开单，但标注 `can_execute=false` 与拦截原因，按钮禁用；即便直接调接口也返回 409，且不写执行流水、不改分区态、不清行数。
- **依赖列表**：`partition_dependencies` 中新生效的 blocking 依赖（补采 / 下游消费 / 跨域引用）同样拦截销毁，解除后立即放行。
- **补采编排**：发起补采自动挂 blocking 依赖并登记宽限；入库未完成时「完成」被拒；进度只增不减；完成/中止后自动解除对应依赖并同源重算宽限。
- **只读冻结**：`read_only` + 可选 `freeze_until`，只拦销毁不拦归档；有限期冻结到期自动失效。

### 两种处置二选一（互斥，不得并存）

`RETAIN_GRACE_DISPOSITION` 只接受一个值，非法值直接报错，不会出现两套策略并存：

| 取值 | 含义 | 行为 |
|------|------|------|
| `reject`（默认） | 整单拒绝销毁 | 宽限中销毁整单拒绝，分区态不动 |
| `freeze` | 只读冻结宽限登记 | 宽限中分区落 `read_only` + `freeze_until`，留宽限登记流水，销毁仍被拦 |

### 并发写入

宽限登记、补采上报、裁决执行都走 `BEGIN IMMEDIATE` 排他写事务（`app/db.py::immediate_txn`）：
`extend` 模式只增不减（短日期不会覆盖他人的长假）；`set` 覆盖式登记必须携带 `expected_rev`，
过期 rev 返回 409。每次登记/解除都写 `grace_registrations` 流水，便于本机库核对。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `PORT` | 6500 | 监听端口 |
| `DATABASE_PATH` | `instance/retain_lab.db` | SQLite 路径 |
| `RETAIN_GRACE_DISPOSITION` | `reject` | `reject` / `freeze`，二选一 |
| `RETAIN_GRACE_DAYS` | 3 | 补采默认宽限天数 |
| `RETAIN_REFERENCE_DATE` | 系统当天 | 冻结「今天」，便于复现宽限判定 |

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-retain-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6500/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6500**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
