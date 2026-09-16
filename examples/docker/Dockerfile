# llm-relay 的容器镜像（可选渠道；主路径仍然是 `python3 llm_relay.py`）。
#
# 纯标准库项目 → 镜像里只需要 Python 和两个 .py，没有任何 pip 依赖、没有构建步骤。
# 所有状态（config.json / keys.env / usage.jsonl / access-key.txt）都放 /data，
# 这与代码里「没配路径就跟 config.json 同目录」的默认行为天然一致 —— 挂一个目录就够了。
FROM python:3.12-slim

# 非 root 运行；uid 固定 10001，方便在 Linux/NAS 上 chown 数据目录（见 README）。
RUN useradd --create-home --uid 10001 relay

WORKDIR /app
COPY llm_relay.py llm-relay-dashboard.py config.example.json /app/
USER relay

VOLUME ["/data"]
EXPOSE 9110 9111

# 这里只起中继；面板是 docker-compose.yml 里的第二个服务（各跑一个进程，不用 sh -c 拼）。
#
# ⚠️ 容器里的 loopback 不是宿主机的 loopback：两个服务都必须显式 --host 0.0.0.0，
#    否则 config.json 里默认的 listen.host=127.0.0.1 会让它只听容器自己，端口映射等于没接通。
CMD ["python", "llm_relay.py", "--config", "/data/config.json", "--keys", "/data/keys.env", "--host", "0.0.0.0"]
