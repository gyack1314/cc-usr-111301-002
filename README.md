# 下传资料的纠删分片重建与来源选择

此项目保存科学资料交换的原始文件、清单及冻结HTTP记录。`capture_data.io`读取JSON、核对资产身份，提供固定字节范围读取和顺序响应重放；`capture_data.domain`核对资料类别、路径和数量；`capture_data.gf`实现冻结的 GF(256)/0x11d 系统码；`capture_data.retriever`按清单采集、核验分片并做擦除恢复。`capture_data.cli`可输出概况或逐条演示采集结果。[资料约定](docs/data-contract.md)说明时间、字段与格式范围。所有站点、观测和机构资料均为合成，不连接航天器、接收机或真实研究系统。

Python3.11以上，无第三方Python依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q capture_data tests
python -m capture_data.cli inspect data/normal.json
python -m capture_data.cli collect data/normal.json
```

`inspect`不解文件格式或选择HTTP版本，只显示资产身份及数量；`collect`按冻结清单采集，输出状态、实际使用的来源 URL 与 sha256、失败的坏副本、按代忽略的行、字节预算与缺口。只有原文件完整恢复并通过长度、零填充与摘要核对后，`file_present` 才为真。`Source.read_at`记录请求范围，`Replay.next`记录实际method、URL与headers；它不替调用者完成协议或缓存策略。

演示清单（`data/` 下，原始长度均为 11 字节，可与无缺失原文逐字节比较）：

| 清单 | 场景 | 预期 |
| --- | --- | --- |
| `normal.json` | 每 stripe 的 row 1 不可用 | 用 row 0+2 恢复，成功 |
| `duplicate-row-rank.json` | 每 stripe 只有两份相同 row 0 | 秩不足，仅给缺口 |
| `mirror-replacement.json` | s0/r0 首镜像为坏副本，次镜像正确 | 记坏副本、换镜像后成功，预算含坏字节 |
| `mixed-generation.json` | s0/r2 显式声明 g0 | 该行忽略不下载，s0 缺口 |
| `short-last-stripe.json` | 最后条带只有 3 个有效字节 | 零填充可恢复，成功 |
| `invalid-padding.json` | 前 11 字节同原文但第 12 字节非零 | 拒绝发布文件 |

`mirror-replacement` 的坏副本与 URL 覆盖表由 `capture_data.demo` 读取，适配器只把冻结 URL 映射到本地只读文件，不执行清单内容。`collect` 输出可直接核对每个分片用了哪个 URL、摘要是否与清单一致。

Dockerfile固定Linux基础镜像，`docker build -t science-material .`构建；`docker run --rm science-material`读取概况，`docker run --rm science-material python -m unittest discover -s tests -v`运行检查。资产均可离线读取，不需要下载数据或真实账号。
