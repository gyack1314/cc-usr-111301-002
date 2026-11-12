# 纠删码资料约定

所有原始块是字节向量，乘加分别在 GF256 及异或上完成。primitive polynomial=0x11d。V 的第 i 行第 j 列为 (i+1)^j，i 从 0 到 k+m-1，j 从 0 到 k-1；G=V*(V 前 k 行)^-1，固定行号不重排，k+m<=255。数据块先按 stripe 再按数据行顺序映射原文，末尾零填充，填充不计入 original_length。只做已校验分片的擦除恢复，不接收“多数看起来一致”的坏片纠错。

available 记录本次模拟来源能否返回，不改变分片内容身份。未写 generation 的分片继承父清单代；显式不同代拒绝。每行可有多个镜像，但只有同内容身份的通过副本可加入该行方程。缓存键包括 generation/stripe/row 及 sha；本实现以 generation/path/sha 为身份，同一条带内不重复使用相同 row_index。重复行场景是合法请求材料但无法形成足够独立方程。

下载适配器由可信代码传入，来源返回原始字节或失败，不执行清单命令。budget 按实际返回 body 字节记，坏副本也计；max_buffered_stripes 限制同时恢复活跃条带。

## 实现约束

`capture_data.gf` 按上式构造系统码并实际消元求逆；`select_points` 拒绝点数不足、重复或越界的行号，并真的验证所选子矩阵可逆。`capture_data.retriever` 的采集规则：

- 每个条带先用 available 且 identity 与清单 `assets` 一致的行；已核验缓存优先，其后按 `row_index`、资产路径确定排序。
- 相同 `row_index` 只取一次；来源与缓存全部失败的行释放名额，继续尝试下一行。
- 下载先逐镜像，坏副本按实际字节计预算并记 `identity-mismatch`，来源失败记 `source-failed`；只有摘要相符才入缓存。
- 他代行记入 `ignored_rows`，不下载；行内参数（k/m/block/polynomial）与清单不符即拒绝。
- 条带解码后检查尾部零填充，再核对 original_length 与原文件 sha256；未通过前 `file` 恒为 None，只返回进度与缺口。
- `Collector.resume(cache)` 只录用重算摘要相符的缓存；摘要不符或格式非法者丢弃或拒绝，不使用未封存临时块。
- `retrieve` 的 `sources` 列出实际使用 URL 与摘要，`failures` 给出逐镜像失败，`peer_reuse` 给出续采复用的来源。

原始工程只核对资产；新增部分实现采集与恢复，不连接网络、不执行清单内容。
