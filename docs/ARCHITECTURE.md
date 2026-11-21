# Colab Boost 控制台 — 架构与协议技术手册

## 1. 模块分层架构

```text
colab-boost/
├── start_console.bat             # Windows 启动脚本（无参启动带 --open-browser，有参直接透传给 colab-boost CLI）
├── start_demo.bat                # Windows 零计费沙盒演示启动脚本（透传附加参数）
├── recipes/                      # 声明式模型部署配方（仅在云端 Colab VM 执行，绝不在本机执行第三方脚本）
│   ├── llama-cpp-gguf/           # recipe.toml + worker.py（预编译 CUDA llama.cpp + 动态 CUDA 回退）
│   ├── vllm-openai/              # recipe.toml + worker.py（vLLM OpenAI 服务）
│   ├── comfyui-studio/           # recipe.toml + worker.py（ComfyUI 工作台）
│   ├── minimax-h3-studio/        # recipe.toml + worker.py（G4 三轨并发 MiniMax-H3 + 可选 LatentSync 口型精修）
│   └── faster-whisper-api/       # recipe.toml + worker.py（Faster-Whisper 语音转写 API）
├── src/colab_boost/
│   ├── __init__.py               # 包元信息与版本导出
│   ├── cli.py                    # 服务启动入口与终端/Agent 控制子命令（status/recipes/deploy/exec/upload/download/stop/stop-all）
│   ├── models.py                 # 强类型领域模型与 GPU 显存参考表（严禁将未知余额伪装为 0）
│   ├── config.py                 # 工作区状态隔离（默认落盘于 ./data，支持 COLAB_BOOST_HOME 环境变量）
│   ├── vault.py                  # 多账号 OAuth2 令牌与模型中心密钥保险箱（OS Keyring + 脱敏器）
│   ├── gguf_planner.py           # Hugging Face GGUF 扫描、二进制文件头解析（含嵌套数组 / MLA / 滑动窗口）与多卡显存预算求解器
│   ├── recipe_loader.py          # TOML 配方严格校验器（损坏目录隔离、禁符号链接、禁 8080 与端口冲突、防路径穿越与控制字符注入）
│   ├── ssh_bridge.py             # 原生 OpenSSH -L 多端口单会话复用转发 + Windows ACL 锁权 + WS 桥接器
│   ├── vm_assets/
│   │   ├── __init__.py
│   │   └── watchdog.py           # 运行在 Colab VM 内的独立无依赖看门狗（定时/空闲自动 unassign）
│   ├── backends/
│   │   ├── __init__.py
│   │   ├── base.py               # ColabBackend 统一抽象协议
│   │   ├── direct.py             # DirectColabBackend：HTTPS + Jupyter Kernel WS（支持文本/二进制帧）直连引擎
│   │   └── mock.py               # MockColabBackend：确定性零计费沙盒引擎
│   ├── orchestrator.py           # 核心编排器：显存重抽、在途取消排空、护栏巡检、长任务令牌续期、任务与回执持久化
│   ├── server.py                 # FastAPI REST + WebSocket 实时推送 + 内置对话代理
│   └── web/                      # 零构建依赖的响应式双语深浅色 Web 控制台
│       ├── index.html
│       ├── app.js
│       └── styles.css
└── tests/                        # 完整单元与端到端集成测试套件
```

## 2. 关键安全与计费不变量 (Invariants)

1. **云端实例即开即计费**：关闭本地端口转发隧道或关闭控制台窗口**不会**停止 Colab 计费；只有主动调用 `unassign` 或由 VM 内部 `watchdog.py` 触发 `POST http://$TBE_RUNTIME_ADDR/unassign` 才会结束计费。
2. **中途取消零残留**：当 `assign_runtime` 请求在途时用户点击取消，编排器在收到分配结果的第一时间立即执行 `unassign_runtime` 并写入 `reason="cancelled_cleanup"` 的关闭回执，绝不留下孤儿计费实例。
3. **Colab 内部保留端口避让**：Colab VM 内部进程默认占用 `*:8080`，配方校验器与隧道管理器均显式拦截 `8080` 端口，并阻止多端点间端口冲突；默认使用 `8000` / `8002` / `8188`。
4. **CUDA 静默退回 CPU 拦截**：Colab 的 `libcuda.so.1` 位于 `/usr/lib64-nvidia`（不在后台子进程默认 `LD_LIBRARY_PATH` 中）。`llama-cpp-gguf` 与 `faster-whisper-api` 工作脚本显式注入驱动及 cuDNN 库路径，并校验 `CUDA0` 与实际显存占用。
5. **密钥零落盘与零命令行泄露**：通过 Jupyter Kernel WebSocket 执行启动代码时强制设置 `"store_history": false`，并在子进程派生后立即从内核 `os.environ` 移除注入变量；Worker 派生常驻推理进程时通过环境变量（如 `LLAMA_API_KEY`、`VLLM_API_KEY`）传递密钥而非 `--api-key` 命令行参数，避免暴露在 `/proc/<pid>/cmdline` 中；所有回传日志与状态文本均经过 `redact_secrets` 脱敏。

## 3. 自定义部署配方规范 (`recipe.toml`)

在 `data/recipes/<your-recipe-id>/` 下创建 `recipe.toml` 与工作脚本（`.py` 或 `.sh`）即可扩展新模型服务：

### 3.1 目录与安全边界
- **目录名与 `id` 严格一致**：`id` 须满足 `^[a-z0-9][a-z0-9-]{0,47}$`，且不能覆盖内置同名配方。
- **故障隔离**：`discover_all_recipes` 会自动忽略隐藏目录（`.` 开头）与 `__pycache__`；单个损坏的用户配方目录会被记录告警并跳过，不会影响内置配方加载或 `/api/bootstrap`。
- **禁止符号链接与路径穿越**：配方目录、`recipe.toml` 以及 `[run].script` 的相对路径父链和脚本文件本身均不得为符号链接，且解析后必须位于配方目录内；清单上限 `64 KiB`，脚本上限 `512 KiB`。

### 3.2 清单字段与校验规则
- **基础字段**：`api = 1`、`id`、`version`、`category`（`llm` | `vision` | `audio` | `custom`）、`[name]`（必须含非空 `en`）、`[run].script`（以 `.py` 或 `.sh` 结尾）。
- **声明能力 (`needs`)**：仅允许 `hf_token`、`civitai_token`，由控制台按需注入 `BOOST_HF_TOKEN` / `BOOST_CIVITAI_TOKEN`。
- **输入项 (`[[inputs]]`)**：
  - `id` 须满足 `^[a-z][a-z0-9_-]{0,31}$`，自动映射为环境变量 `BOOST_INPUT_<UPPER_ID>`（`-` 转 `_`，拒绝环境变量名冲突）。
  - 支持类型：`text`、`number`、`bool`、`select`、`secret`、`hf-gguf`、`hf-repo`；每个输入项必须包含非空 `label.en`。
  - `number` 仅允许整数（拒绝 `float` 静默截断与 `bool`），且要求 `min <= max`、`default` 类型与区间合法。
  - `select` 必须声明非空 `options` 且 `default` 位于 `options` 内；`secret` 禁止声明 `default`。
  - `text` / `secret` 拦截 `\x00`、`\r`、`\n` 控制字符并限制最大长度为 `4096` 字符。
- **阶段 (`[[stages]]`)、端点 (`[[endpoints]]`) 与归档 (`[[keep]]`)**：
  - 所有 `stage` / `endpoint` / `keep` 均必须包含非空 `label.en`。
  - `endpoints` 拒绝重复 `id`，必须二选一指定 `port` 或 `port_input`（指向 `number` 输入），禁止 `8080` 且拒绝静态/运行时端口冲突。
  - `keep.path` 必须为不含 `..` 的 VM 绝对路径。

### 3.3 Worker 运行时契约
- 工作进程在后台独立进程组（`start_new_session=True`）中运行，通过环境变量接收 `BOOST_RUN_DIR`、`BOOST_STATUS_FILE`、`BOOST_HEARTBEAT_FILE` 与 `BOOST_INPUT_*`。
- Worker 应将实时进度原子写入 `$BOOST_STATUS_FILE`（`state` 取值 `running` | `ready` | `failed`），并在非 GPU 密集阶段（如下载权重）定期更新 `$BOOST_HEARTBEAT_FILE` 以防止空闲看门狗误触发。
- 当状态变为 `ready` 时，控制台会自动根据 `[[endpoints]]` 建立 `127.0.0.1` 本地 OpenSSH 端口映射。
