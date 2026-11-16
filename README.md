# 跨市场设备服务质量数据平台

该 FastAPI 服务在离线环境下分析不同市场的设备运维事件。演示数据均为本地合成数据，SQLite 是唯一持久化服务。

## 启动

依赖精确版本见 requirements.txt。在预装依赖的环境中运行 uvicorn brand_signal.api:app --host 0.0.0.0 --port 8000。通过 BRAND_SIGNAL_DB 指定数据库文件，默认是 /tmp/brand-signal.sqlite3。运行 python -m brand_signal.demo 可执行一次性端到端演示。

## 数据流程

先登记市场及 IANA 时区、产品和设备安装记录。可选配置来源字段契约、外部设备号映射和服务响应目标。工单及计量事件使用请求键批量提交；每行分别返回接收或拒绝结果，相同请求键和内容重放原结果。工单变更需要预期修订号。按市场本地日期查询分析指标、跨来源对账、告警及服务复核。

发布候选固定某一源修订及预期发布版本。只有源修订和发布版本未变化时才能发布。旧版本、CSV 和 JSON 导出不可变。迟到数据须进入新候选和新发布版本。市场汇总、比较、抽样与溯源均按明确版本读取。

## 验证

python -m unittest discover -s tests -v

python -m compileall -q brand_signal

python -m brand_signal.demo

API 错误返回稳定的 error 代码和 HTTP 4xx 状态。空分母表示为 JSON null。输入时间戳必须包含时区偏移，本地日期格式为 YYYY-MM-DD。
