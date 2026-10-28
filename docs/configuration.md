# 配置文件参考

客户端和服务端各使用一个 TOML 文件，启动时分别作为命令行参数传入；配置在启动时读取，修改后须重启对应进程。运行环境为 Python 3.11+，不需要第三方依赖。

```sh
python -m netpulse.server examples/server.toml
python -m netpulse.client examples/client.toml
```

可直接复制 [服务端示例](../examples/server.toml) 与 [客户端示例](../examples/client.toml) 后修改。示例中的公网地址是文档保留地址，**不能直接用于实际连接**；示例密钥也必须替换。客户端的 `id` 要与服务端 `[clients]` 中的键相同，客户端对应目标的 `secret` 要与该键的值相同。两端的 TCP、UDP 端口也必须对应。

## 服务端配置

`[server]` 表配置监听地址、端口及会话超时；整个表可省略，此时使用全部默认值。

| 配置项 | 默认值 | 含义与用法 |
| --- | --- | --- |
| `host` | `"0.0.0.0"` | TCP 与 UDP 的监听地址。`0.0.0.0` 监听所有 IPv4 接口；仅供本机测试时可填 `"127.0.0.1"`。公网部署需保证客户端可访问该地址，并按需配置防火墙。 |
| `tcp_port` | `9009` | TCP 长连接监听端口，整数 `0`–`65535`；`0` 由操作系统临时分配端口，仅适合测试，生产环境应指定固定端口。 |
| `udp_port` | `9010` | UDP 探针监听端口，规则同 `tcp_port`。需要单独放行 UDP，而不只是放行同号 TCP 端口。 |
| `handshake_timeout` | `5` | TCP 握手各阶段读写允许的最长时间，单位秒；超时则关闭该连接。不影响已完成认证后的心跳。 |
| `heartbeat_idle_timeout` | `10` | 已认证 TCP 连接等待下一条心跳，以及写回心跳应答的超时，单位秒；超过后由服务端关闭连接。应大于相应客户端目标的 `tcp_interval`，并留出网络延迟和调度余量。 |
| `log_level` | `"INFO"` | 服务端输出到标准错误流的日志等级；可选 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`（不区分大小写）。命令行 `--log-level` 优先于此项。系统服务中由 journald 接收日志。 |

`[clients]` **必填且不能空**：其每个键是允许接入的客户端 ID，每个值是该客户端的共享密钥字符串。至少配置一项；可为多台客户端分别配置不同 ID 和密钥。ID 与密钥均须是非空、可编码为 UTF-8 的字符串。这里没有固定名称的配置项：`workstation` 和 `laptop` 只是示例 ID。

```toml
[server]
host = "0.0.0.0"
tcp_port = 9009
udp_port = 9010
handshake_timeout = 5
heartbeat_idle_timeout = 10
log_level = "INFO"

[clients]
workstation = "replace-with-a-long-random-secret"
laptop = "replace-with-another-long-random-secret"
```

## 客户端配置

`[client]` 表**必填**，其中 `id` 必填；其余键可省略，使用下表默认值。时间均以秒为单位，可填正整数或正浮点数，例如 `0.5`；不能填 `0`、负数、布尔值或无穷大。频率与超时按各目标独立执行；操作系统调度会影响实际时刻。

| 配置项 | 默认值 | 含义与用法 |
| --- | --- | --- |
| `id` | 必填 | 本客户端的身份字符串，非空；须与每台目标服务端 `[clients]` 的某个键一致。一个客户端进程对所有目标使用同一个 ID。 |
| `metrics_host` | `"127.0.0.1"` | Prometheus HTTP `/metrics` 的监听地址；默认只供本机访问。若设为 `"0.0.0.0"`，应另行限制指标端口访问。 |
| `metrics_port` | `9108` | Prometheus HTTP 监听端口，整数 `0`–`65535`；`0` 临时分配端口，仅适合测试，Prometheus 抓取应使用固定端口。 |
| `udp_interval` | `1` | 对每台目标发起 UDP 探针的间隔。默认每秒一次；前一个探针未完成时仍可发送下一个。 |
| `tcp_interval` | `1` | 每条已连接 TCP 槽位的心跳间隔；各槽位独立探测。若一次心跳耗时超过间隔，完成后会尽快发起下一次，不会同时发送多个未完成心跳。 |
| `udp_timeout` | `2` | UDP 探针等待有效应答的最长时间；超时计入丢包，迟到应答不回补。缩短它会更早报告丢包，也可能把高延迟应答判为丢失。 |
| `tcp_timeout` | `2` | 建立 TCP 连接、读取挑战及认证应答、等待心跳应答等操作的超时；心跳超时会将该槽位标记为断开并触发重连。 |
| `reconnect_interval` | `1` | TCP 连接失败或断开之后再次尝试连接的等待时间。UDP 尝试持续按 `udp_interval` 执行，不受此项影响。 |
| `loss_window` | `60` | `netpulse_udp_loss_ratio` 的滚动统计窗口。只统计最近该时间内**已结算**的探针（成功、超时或本地发送失败）；窗口内无已结算样本时显示 `0`。此项只在 `[client]` 中设置，不支持单台目标覆盖。 |
| `log_level` | `"INFO"` | 客户端输出到标准错误流的日志等级；选项及命令行覆盖规则同服务端。仅写在 `[client]`，不能对单台 `[[servers]]` 单独设置。 |

`[[servers]]` 是目标服务端的数组，**至少配置一项**；每写一个 `[[servers]]` 就增加一台目标。目标之间独立维护 UDP 探针与 TCP 槽位。每项的键如下：

| 配置项 | 默认值 | 含义与用法 |
| --- | --- | --- |
| `name` | 必填 | 目标的唯一名称，非空；用于 Prometheus 的 `server` 标签。即使两项指向同一个地址，名称也不得重复。 |
| `host` | 必填 | 服务端可由此客户端访问的 IP 地址或主机名，非空；不可填服务端仅在自己机器上可访问的地址。目标域名解析失败时会继续尝试探测，不会阻止其他目标工作。 |
| `tcp_port` | 必填 | 连接该目标的 TCP 端口，整数 `1`–`65535`；须对应其服务端的 `tcp_port`。 |
| `udp_port` | 必填 | 发送该目标 UDP 探针的端口，整数 `1`–`65535`；须对应其服务端的 `udp_port`。 |
| `secret` | 必填 | 该目标上 `[clients]` 中本客户端 ID 对应的共享密钥；允许每台目标使用不同密钥。 |
| `tcp_connections` | `3` | 对该目标保持的 TCP 长连接数量，正整数；每条连接各自监测、重连，并拥有从 `0` 开始的 `slot` 指标标签。提高数量会增加服务端连接数。 |
| `udp_interval` | 继承 `[client]` | 只覆盖该目标的 UDP 探针间隔；未设置时沿用客户端全局值。 |
| `tcp_interval` | 继承 `[client]` | 只覆盖该目标各 TCP 槽位的心跳间隔。 |
| `udp_timeout` | 继承 `[client]` | 只覆盖该目标 UDP 探针的应答超时。 |
| `tcp_timeout` | 继承 `[client]` | 只覆盖该目标 TCP 连接及心跳操作的超时。 |
| `reconnect_interval` | 继承 `[client]` | 只覆盖该目标 TCP 失败后的重连间隔。 |

目标级时间项与 `[client]` 同样要求正整数或正浮点数。下面示例让第二台服务端以每 0.5 秒一次的 UDP 频率探测，而第一台沿用全局每秒一次：

```toml
[client]
id = "workstation"
udp_interval = 1.0
tcp_interval = 1.0
log_level = "INFO"

[[servers]]
name = "primary"
host = "203.0.113.10" # 换成可访问的实际地址
tcp_port = 9009
udp_port = 9010
secret = "replace-with-a-long-random-secret"
tcp_connections = 3

[[servers]]
name = "backup"
host = "198.51.100.10" # 换成可访问的实际地址
tcp_port = 9009
udp_port = 9010
secret = "replace-with-a-long-random-secret"
tcp_connections = 2
udp_interval = 0.5
```

**配置与部署注意：** 密钥不要提交版本库，并限制配置文件的读取权限，例如 `chmod 600`。认证不等于加密：TCP/UDP 探测内容不是端到端加密的，已录制的有效 UDP 请求还可被重放；在不可信网络中应配合访问控制、VPN 或隧道。指标详情及 Prometheus 抓取示例见 [使用说明](../README.md#prometheus)。

日志等级只决定程序运行时输出哪些日志，不影响探测频率或 Prometheus 指标。默认 `INFO` 记录启动、连接和恢复事件，`WARNING` 记录断连及丢包状态变化，`DEBUG` 增加探针详情；持续故障不会每秒重复输出警告。命令行可临时覆盖 TOML 设置，例如 `python -m netpulse.client examples/client.toml --log-level DEBUG`。systemd 的安装与删除步骤见 [systemd 教程](systemd.md)。
