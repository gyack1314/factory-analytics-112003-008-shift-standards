# 产品换型工时标准与班次承诺重算

本目录保存工业园内的合成业务资料。历史切换与工时凭证、共用工具占用、工艺族属性与已承诺生产安排。previous/next 有方向，spec 是工艺口径版本，signed_min 是工时结束证据入档点。设备等待与工序有可能重叠。tool_logs 保留转抄产生的重复或相互矛盾的记录。

数据文件位于 `data/`，采用 UTF-8 JSON。表由对象数组组成，单份配置由对象表示。字段后缀 `min`、`day`、`hour`、`cents`、`kg` 和 `m` 标记单位。未提供的事实用 `null` 表示，绝对区间右端不包含在内。名称和编号为本目录内的资料标识。

运行环境为 Python 3.11，资料检查只使用标准库。

- 查看目录与样本数量：`python3 inspect_inputs.py`
- 运行资料测试：`python3 -m unittest discover -s tests -v`
- 编译检查：`python3 -m compileall -q inspect_inputs.py tests`
