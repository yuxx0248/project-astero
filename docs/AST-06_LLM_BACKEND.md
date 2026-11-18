# AST-06 — LLM 对话适配器

`astero.agent.ConversationBackend.respond(text, session_id) -> str` 是统一接口。`OpenAICompatibleBackend` 对接明确配置的 Chat Completions 兼容服务；`MockConversationBackend` 是离线测试替身，其输出不是 LLM 回答。

## 配置真实后端

没有默认远程服务，构造 HTTP 适配器本身不会联网。只有在确认服务由用户授权并有权提交转录文本后，才设置端点和模型。`ASTERO_LLM_ENDPOINT` 必须是完整的 `/v1/chat/completions` URL；远程地址要求 HTTPS，本地回环地址可用 HTTP。可选密钥只从环境变量 `ASTERO_LLM_API_KEY` 读取。端点 URL 不接受内嵌用户名或密码。示例配置见 `configs/agent.example.json`。

```sh
export ASTERO_LLM_ENDPOINT='https://llm.example/v1/chat/completions'
export ASTERO_LLM_MODEL='your-authorized-model'
export ASTERO_LLM_API_KEY='从安全凭据管理器注入'
export ASTERO_LLM_TIMEOUT_SECONDS='30'
export ASTERO_LLM_SYSTEM_PROMPT='用简洁、适合语音播报的方式回答。'
```

```python
from astero.agent import OpenAICompatibleBackend, OpenAICompatibleConfig

backend = OpenAICompatibleBackend(OpenAICompatibleConfig.from_env())
answer = backend.respond(transcript, session_id)
```

适配器将 session ID 放入请求 metadata，并在进程内保留每个 session 最近 8 轮上下文；同一 session 的请求按顺序执行，不同 session 可并发。请求或响应失败时该轮不会写入上下文。关闭上下文可将 `max_history_turns=0` 传给配置。输入为空、超时、HTTP/传输错误、格式错误或空回答都会通过显式异常报告。适配器不记录转录、回答、凭据或服务响应正文，也不提供 shell 工具。

## 环境发现与验证

2026-11-18 检查当前环境：进程列表中未发现 Ollama、vLLM、LM Studio 或 Agent Gateway 等 LLM 服务；已读取 `/home/zuohang/agents/SERVICES.md`。`ss -ltnp` 显示 127.0.0.1:8080 和 :8765 在监听，按健康检查分别为本机 TTS（`:8080/health` 返回 200、`{"status":"ok"}`）和 Voice Bridge（`:8765/health` 返回 200、`status: ok`、`tts_ready: true`），都不是 LLM 对话后端。常见 LLM 配置变量 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`OPENAI_BASE_URL`、`OLLAMA_HOST` 以及本适配器的 `ASTERO_LLM_*` 均未设置。未找到已配置且授权的真实 LLM；没有向任何服务提交用户文本，因此真实问答未验证。

运行适配器测试（本地临时 HTTP 替身，不访问外网）：

```sh
python3 -m unittest tests.test_agent -v
```
