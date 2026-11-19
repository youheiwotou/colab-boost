# Colab Boost 控制台 — 架构与协议技术手册

## 1. 模块分层架构

```text
colab-boost/
├── recipes/                      # 声明式模型部署配方（仅在云端 Colab VM 执行，绝不在本机执行第三方脚本）
│   ├── llama-cpp-gguf/           # recipe.toml + worker.py
│   ├── vllm-openai/
│   ├── comfyui-studio/
│   ├── minimax-h3-studio/        # G4 三轨并发 MiniMax-H3 视频人物替换 + 可选 LatentSync 口型精修
│   └── faster-whisper-api/
├── src/colab_boost/
│   ├── models.py                 # 强类型领域模型（严禁将未知余额伪装为 0）
│   ├── config.py                 # 工作区状态隔离（默认落盘于 ./data，支持 COLAB_BOOST_HOME 环境变量）
│   ├── vault.py                  # 多账号 OAuth2 令牌与模型中心密钥保险箱（OS Keyring + 脱敏器）
│   ├── gguf_planner.py           # Hugging Face GGUF 扫描、二进制文件头解析与多卡显存预算求解器
│   ├── recipe_loader.py          # TOML 配方严格校验器（禁 8080 端口、防路径穿越、防环境变量冲突）
│   ├── ssh_bridge.py             # 原生 OpenSSH -L 多端口单会话复用转发 + Windows ACL 锁权 + WS 桥接器
│   ├── vm_assets/watchdog.py     # 运行在 Colab VM 内的独立无依赖看门狗（定时/空闲自动 unassign）
│   ├── backends/
│   │   ├── base.py               # ColabBackend 统一抽象协议
│   │   ├── direct.py             # DirectColabBackend：HTTPS + Jupyter Kernel WS（支持文本/二进制帧）直连引擎
│   │   └── mock.py               # MockColabBackend：确定性零计费沙盒引擎
│   ├── orchestrator.py           # 核心编排器：显存重抽、在途取消排空、护栏巡检、长任务令牌续期、任务与回执持久化
│   ├── server.py                 # FastAPI REST + WebSocket 实时推送 + 内置对话代理
│   └── web/                      # 零构建依赖的响应式双语深浅色 Web 控制台
└── tests/                        # 完整单元与端到端集成测试套件
```

## 2. 关键安全与计费不变量 (Invariants)

1. **云端实例即开即计费**：关闭本地端口转发隧道或关闭控制台窗口**不会**停止 Colab 计费；只有主动调用 `unassign` 或由 VM 内部 `watchdog.py` 触发 `POST http://$TBE_RUNTIME_ADDR/unassign` 才会结束计费。
2. **中途取消零残留**：当 `assign_runtime` 请求在途时用户点击取消，编排器在收到分配结果的第一时间立即执行 `unassign_runtime` 并写入 `reason="cancelled_cleanup"` 的关闭回执，绝不留下孤儿计费实例。
3. **Colab 内部保留端口避让**：Colab VM 内部进程默认占用 `*:8080`，配方校验器与隧道管理器均显式拦截 `8080` 端口，默认使用 `8000` / `8002` / `8188`。
4. **CUDA 静默退回 CPU 拦截**：Colab 的 `libcuda.so.1` 位于 `/usr/lib64-nvidia`（不在后台子进程默认 `LD_LIBRARY_PATH` 中）。`llama-cpp-gguf` 工作脚本显式注入驱动目录，并在启动前后双重验证 `CUDA0` 设备列表与 `nvidia-smi` 显存占用。
5. **密钥零落盘泄露**：通过 Jupyter Kernel WebSocket 执行启动代码时强制设置 `"store_history": false`，并在子进程派生后立即从内核 `os.environ` 移除注入变量；所有回传日志与状态文本均经过 `redact_secrets` 脱敏。

## 3. 自定义部署配方规范 (`recipe.toml`)

在 `data/recipes/<your-recipe-id>/` 下创建 `recipe.toml` 与 `worker.py` 即可扩展新模型服务：
- 输入项会自动注入为环境变量 `BOOST_INPUT_<UPPER_ID>`；
- 工作进程在后台独立进程组中运行，将实时进度原子写入 `$BOOST_STATUS_FILE`（`state` 取值 `running` | `ready` | `failed`），并在非 GPU 密集阶段（如下载权重）定期更新 `$BOOST_HEARTBEAT_FILE`；
- 当状态变为 `ready` 时，控制台会自动根据 `[[endpoints]]` 建立 `127.0.0.1` 本地 SSH 端口映射。
