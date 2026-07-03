## 目标

-  企业微信机器人通过 websocket 接入 nexo 后端，nexo 后端：fastapi + Agent，其中 Agent 接 DeepSeek API, 完成基础的对话功能。

## 技术选型

- Python >= 3.12
- Agent 开发框架： pydantic AI
- LLM 接入 Deepseek API
- LUI 数据流：企业微信机器人（https://pypi.org/project/wecom-aibot-python-sdk/） ----> fastAPI ----> Agent
- 部署：Docker + docker compose 

