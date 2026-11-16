# 跨市场设备服务质量数据平台

该 FastAPI 服务在离线环境下分析不同市场的设备运维事件。演示数据均为本地合成数据，SQLite 是唯一持久化服务。

## 启动

依赖精确版本见 requirements.txt。在预装依赖的环境中运行 uvicorn brand_signal.api:app --host 0.0.0.0 --port 8000。通过 BRAND_SIGNAL_DB 指定数据库文件，默认是 /tmp/brand-signal.sqlite3。运行 python -m brand_signal.demo 可执行一次性端到端演示。

## 数据流程

先登记市场及 IANA 时区、产品和设备安装记录。可选配置来源字段契约、外部设备号映射和服务响应目标。工单及计量事件使用请求键批量提交；每行分别返回接收或拒绝结果，相同请求键和内容重放原结果。工单变更需要预期修订号。按市场本地日期查询分析指标、跨来源对账、告警及服务复核。

发布候选固定某一源修订及预期发布版本。只有源修订和发布版本未变化时才能发布。旧版本、CSV 和 JSON 导出不可变。迟到数据须进入新候选和新发布版本。市场汇总、比较、抽样与溯源均按明确版本读取。

## 结算窗口与迟到数据

每个市场可按本地日期开结算窗口（`POST /settlements/windows`），窗口状态为 open → closed，必要时可 reopen 后再次 closed；关闭与重开均带窗口修订号的乐观并发控制（CAS），并发结算时过期修订返回 stale_window。关窗时冻结当时的发布版本号和对账问题快照。

窗口处于 closed/reopened 后接收的事件仍正常入库并立即反映在实时分析中，但会按事件的市场本地日自动登记到该窗口的迟到队列（late_arrivals，pending）。批次结果逐行给出 is_late 并汇总 late 计数，preview 同样标注但不写库。同一 (source,event_key) 只登记一次，重复上传走请求键重放、跨请求同 event_key 仍被 event_conflict 拒绝，不会产生第二条待处理记录。

分析员通过 `GET /settlements/windows/{id}/impact` 核对补报影响：以关窗时的发布版本（或问题快照）为基线，给出每个产品日在设备日覆盖率、停机占比等指标上的 before/after 差异，以及质量告警的新增与消解；`GET /settlements/{market}/pending` 汇总全市场待处理补报。核对后可重开窗口，或直接准备新候选：发布时窗口范围内的迟到事件在同一事务内标记 included 并写入版本纳入表，(market,version,late_id) 与 (late_id,version) 唯一约束保证同一事件在同一版本只计数一次，发布响应返回 late_included。已发布版本的行快照不变，旧版读数始终可复现。

## 验证

python -m unittest discover -s tests -v

python -m compileall -q brand_signal

python -m brand_signal.demo

API 错误返回稳定的 error 代码和 HTTP 4xx 状态。空分母表示为 JSON null。输入时间戳必须包含时区偏移，本地日期格式为 YYYY-MM-DD。
