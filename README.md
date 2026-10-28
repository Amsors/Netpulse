# Netpulse

轻量级、仅依赖 Python 3.11+ 标准库的客户端网络质量探测工具。客户端可同时监测多台服务端；每台服务端可服务多个客户端。客户端向每台服务端维持指定数量的 TCP 长连接，同时持续发送 UDP 探针；状态与计数由客户端通过 Prometheus 文本格式导出。

## 运行

在仓库根目录运行，无需安装依赖。参照 [服务端配置](examples/server.toml) 与 [客户端配置](examples/client.toml) 修改地址、密钥和端口。两端的客户端 ID 与共享密钥须对应；为不同客户端设置不同密钥。服务端放行配置的 TCP 与 UDP 端口，客户端需能主动连接这些端口。

每个 TOML 配置项的含义、默认值及逐台覆盖规则见 [配置文件参考](docs/configuration.md)。

默认将运行日志输出到标准错误流；可在 `[server]` 或 `[client]` 设置 `log_level = "DEBUG"` 等等级（默认 `INFO`），也可通过 `--log-level DEBUG` 临时覆盖。systemd 部署、查看日志及卸载步骤见 [systemd 教程](docs/systemd.md)。

```sh
python -m netpulse.server examples/server.toml
python -m netpulse.client examples/client.toml
curl http://127.0.0.1:9108/metrics
```

服务端 `[server]` 可设置 `host`（默认 `0.0.0.0`）、`tcp_port`（9009）、`udp_port`（9010）、`handshake_timeout`（5 秒）、`heartbeat_idle_timeout`（10 秒）。`[clients]` 是客户端 ID 到共享密钥的映射。客户端 `[client]` 必须设置 `id`；可设置 `metrics_host`（默认 `127.0.0.1`）、`metrics_port`（9108）、`udp_interval` / `tcp_interval`（各 1 秒）、`udp_timeout` / `tcp_timeout`（各 2 秒）、`reconnect_interval`（1 秒）、`loss_window`（60 秒）。每个 `[[servers]]` 必须设置唯一 `name`、`host`、`tcp_port`、`udp_port` 和 `secret`；`tcp_connections` 默认 3，还可逐台覆盖上述间隔、超时及重连时间。时间单位均为秒，支持小数；实际精度受操作系统调度影响。服务端的 `heartbeat_idle_timeout` 应大于客户端该目标的 `tcp_interval`。配置修改后重启相应进程。

TCP 每个连接独立认证、发心跳、检查应答和重连；应答超时或连接关闭后立即将对应连接标为断开。UDP 请求携带序号和随机数，只有经过认证且在 `udp_timeout` 内、序号与随机数都匹配的应答才记为成功；超时算丢包，迟到应答不回补。探测频率默认每秒一次；默认 UDP 丢包判定需等待 2 秒。服务端不保存客户端探测状态。目标域名暂不可解析时，该目标的 UDP 尝试记为失败并继续重试，不会阻止其他目标探测。

## Prometheus

客户端 `GET /metrics` 提供以下指标。所有 UDP 指标带 `server` 标签；所有 TCP 指标带 `server`、`slot` 标签（`slot` 从 0 开始）。每个客户端独立导出自己的观测结果；Prometheus 的 `instance` 标签可区分客户端。

| 指标 | 含义 |
| --- | --- |
| `netpulse_udp_sent_total` | UDP 探针发送尝试次数（包括本地发送失败） |
| `netpulse_udp_received_total` | 超时前收到有效应答的次数 |
| `netpulse_udp_lost_total` | 超时或本地发送失败的次数；未结算的探针尚不计入 |
| `netpulse_udp_loss_ratio` | 最近 `loss_window` 秒内**已结算**探针的丢失比例；无已结算探针时为 0 |
| `netpulse_udp_up` | 最近一次已结算 UDP 探针是否成功（1/0） |
| `netpulse_udp_rtt_seconds` | 最近一次成功的 UDP 往返时延；尚无成功样本时为 0 |
| `netpulse_tcp_up` | 该 TCP 槽位当前是否已认证并保持连接（1/0） |
| `netpulse_tcp_disconnects_total` | 该槽位已建立连接后的断开次数（正常停止客户端不计） |
| `netpulse_tcp_reconnects_total` | 该槽位首次成功建立连接之后再次成功连接的次数 |
| `netpulse_tcp_rtt_seconds` | 该槽位最近一次成功心跳的往返时延；尚无样本时为 0 |

建议 Prometheus 对客户端设置 `scrape_interval: 1s`，例如：

```yaml
scrape_configs:
  - job_name: netpulse
    scrape_interval: 1s
    static_configs:
      - targets: ['127.0.0.1:9108']
```

采样间隔决定图表能否展示短暂状态变化；累计丢包和断开次数不会因抓取间隔较长而丢失。只有 UDP 与 TCP 都跨过各自超时时间，才会报告对应的失败；0 RTT 表示尚无成功样本，不能当作真实零延迟。

**安全边界：** 密钥不要提交到版本库，配置文件应限制读取权限（例如 `chmod 600`）。服务端对 TCP 连接使用随机挑战与 HMAC 认证，对 UDP 使用 HMAC 校验；无效客户端的 UDP 请求不会得到应答。但协议**不提供加密或抗中间人保护**，录制过的有效 UDP 请求可重放；在不可信网络中应配合防火墙、VPN 或隧道。指标 HTTP 端口默认仅监听本机，若改为对外监听，应限制访问。不要在可直接暴露公网的环境中使用示例密钥。

## 测试

```sh
python -m unittest discover -s tests -v
```
