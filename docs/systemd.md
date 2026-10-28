# 使用 systemd 管理 Netpulse

仓库提供两个 unit：[服务端](../systemd/netpulse-server.service) 和 [客户端](../systemd/netpulse-client.service)。以下步骤分别在**公网服务端主机**和**客户端主机**执行；只安装该主机需要的角色，不必在每台机器同时安装两个服务。示例使用 `/opt/netpulse` 放代码、`/etc/netpulse` 放配置，进程以无登录权限的 `netpulse` 用户运行，日志写入 journald。

## 安装

1. 在各自机器上安装 Python 3.11 或更新版本，并确认 unit 中的 `/usr/bin/python3` 确实指向满足要求的解释器：

   ```sh
   /usr/bin/python3 --version
   ```

   若 Python 3.11+ 安装在其他路径，先修改对应 unit 的 `ExecStart=`，再安装 unit。准备本仓库代码及专用系统用户：

   ```sh
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin netpulse
   sudo install -d -o root -g root -m 0755 /opt/netpulse/netpulse
   sudo install -m 0644 netpulse/*.py /opt/netpulse/netpulse/
   sudo install -d -o root -g netpulse -m 0750 /etc/netpulse
   ```

   以上命令在仓库根目录执行；若 `netpulse` 用户已存在，不要重复创建。升级代码时重新复制 `.py` 文件并重启已安装的服务。`/opt/netpulse` 的路径和 unit 的 `WorkingDirectory=` 必须一致。

2. **公网服务端主机**安装服务端配置与 unit，编辑密钥、监听地址及端口后启动：

   ```sh
   sudo install -o root -g netpulse -m 0640 examples/server.toml /etc/netpulse/server.toml
   sudoedit /etc/netpulse/server.toml
   sudo install -o root -g root -m 0644 systemd/netpulse-server.service /etc/systemd/system/netpulse-server.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now netpulse-server.service
   sudo systemctl status netpulse-server.service
   sudo journalctl -u netpulse-server.service -n 50 --no-pager
   ```

   放行服务端配置的 TCP、UDP 端口（默认为 TCP 9009、UDP 9010）；防火墙规则应按实际部署环境限制来源地址。

3. **客户端主机**安装客户端配置与 unit，修改客户端 ID、各目标的实际公网地址、端口和匹配的密钥，再启动：

   ```sh
   sudo install -o root -g netpulse -m 0640 examples/client.toml /etc/netpulse/client.toml
   sudoedit /etc/netpulse/client.toml
   sudo install -o root -g root -m 0644 systemd/netpulse-client.service /etc/systemd/system/netpulse-client.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now netpulse-client.service
   sudo systemctl status netpulse-client.service
   sudo journalctl -u netpulse-client.service -n 50 --no-pager
   curl http://127.0.0.1:9108/metrics
   ```

   `metrics_host` 默认仅监听本机；Prometheus 从其他主机抓取时，按需修改监听地址和防火墙规则。系统服务以非 root 用户运行，监听端口应使用非特权端口。配置中的共享密钥只对 root 和 `netpulse` 组可读，**不要**使用示例密钥直接上线。配置的全部含义见[配置文件参考](configuration.md)。

## 日志、更新与故障排查

默认 `INFO` 记录启动、连接及恢复事件；网络故障转换为 `WARNING`，逐包详情在 `DEBUG`。在服务端 `[server]` 或客户端 `[client]` 内设置 `log_level = "DEBUG"` 等日志等级后，重启相应服务使之生效：

```sh
sudo systemctl restart netpulse-server.service # 只在服务端主机执行
sudo systemctl restart netpulse-client.service # 只在客户端主机执行
sudo journalctl -u netpulse-client.service -f
```

修改 unit 时先执行 `sudo systemctl daemon-reload` 再重启；仅修改 TOML 时直接重启。若服务反复重启，先检查 `sudo journalctl -u netpulse-client.service -n 100 --no-pager`（服务端改用 `netpulse-server.service`）、配置权限、Python 版本及端口占用。日志由 journald 管理，本工具不创建日志文件。在仓库根目录手动执行 `python -m netpulse.client examples/client.toml --log-level DEBUG`（服务端同理）可临时覆盖配置的日志等级，不改变 systemd unit。

## 停止、禁用与删除

只删除该主机实际安装的角色。**先停用服务，再移除 unit，并重新加载 systemd**；例如删除服务端：

```sh
sudo systemctl disable --now netpulse-server.service
sudo rm /etc/systemd/system/netpulse-server.service
sudo systemctl daemon-reload
```

删除客户端：

```sh
sudo systemctl disable --now netpulse-client.service
sudo rm /etc/systemd/system/netpulse-client.service
sudo systemctl daemon-reload
```

以上操作保留代码、配置及密钥，便于以后恢复。确认配置不再需要、已备份且同一主机上的两个角色都已卸载后，再**按需**执行下面的清理；不要删除仍被其他服务使用的文件或用户：

```sh
sudo rm /etc/netpulse/server.toml   # 只在确实存在且不再需要时执行
sudo rm /etc/netpulse/client.toml   # 同上
sudo rmdir /etc/netpulse            # 目录必须已空
sudo rm -r /opt/netpulse            # 仅在代码不再被其他服务使用时执行
sudo userdel netpulse               # 仅在这个专用用户不再被使用时执行
```

如果只卸载一个角色，保留另一个角色依赖的配置目录、代码和用户。
