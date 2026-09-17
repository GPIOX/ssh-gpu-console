# SSH GPU Console

[English](./README.md) | [简体中文](./README.zh-CN.md)

一个轻量、无代理、基于 SSH 的 GPU 服务器管理工作站。

控制面运行在**你自己的电脑上**：FastAPI 后端通过复用的 SSH 连接管理任意数量的远程 Linux
GPU 服务器，React 面板渲染集群与逐机遥测。远程服务器**无需安装 agent、无需常驻守护进程、
无数据库、无持续遥测写盘** —— 本产品在不成为负载的前提下观察服务器。

```
我的电脑
├── React + TypeScript 面板
├── FastAPI 本地控制面
│    ├── 服务器注册表（JSON，仅在变更时写入）
│    ├── SSH 连接管理器（AsyncSSH，连接复用）
│    ├── 远程执行器（有界并发、超时控制）
│    ├── 按需感知的采集调度器
│    ├── 共享遥测状态（仅内存，有界）
│    └── 实时推送中心（单 WebSocket，背压）
└── SSH ──▶ Linux GPU 服务器 A / B / C  （零安装）
```

## 特性

- **集群总览**：所有服务器以"工作中的机器"呈现——状态、CPU/内存量表、逐 GPU 通道
  （利用率、显存、温度、功率、可用性、占用用户）、磁盘告警、数据新鲜度。
- **服务器详情**：总览、GPU、进程、系统、存储、网络——不常看的板块（进程/系统/存储/网络）
  可在设置中隐藏。
- **GPU 优先**：多卡支持、有界历史 sparkline、GPU 计算进程与系统进程关联。
- **上手**：从 `~/.ssh/config` 导入别名、带明确错误分类的连接测试、显式主机密钥信任
  （TOFU，信任保存在应用侧）——绝不修改你的 `known_hosts`。
- **按你的方式使用**：深色/浅色主题（简洁浅色 & 暖色，可跟随系统）、English / 简体中文
  界面、逐服务器采样间隔（0.5–600 秒）。
- **仅命名操作**：进程结束/强制结束均需显式确认。不存在任意 shell 接口。
- **节俭设计**：连接复用、批量查询、有界内存、空闲模式降采样、写盘即例外。

## 环境要求

- Python 3.12+（推荐 uv）
- Node 20+ 与 pnpm
- 通过你现有的 OpenSSH 环境（agent、config、密钥）访问服务器

## 开发模式

```bash
# 后端
cd backend
uv venv .venv && uv pip install -e ".[dev]" -p .venv/bin/python
.venv/bin/uvicorn app.main:app --port 8420

# 前端（另开终端）
cd frontend
pnpm install
pnpm dev            # http://localhost:5173，/api 代理到 :8420
```

## 生产运行

```bash
cd frontend && pnpm build
cd backend && .venv/bin/uvicorn app.main:app --port 8420   # 同时服务 frontend/dist
```

## 测试 / 质量门

```bash
cd backend && .venv/bin/python -m ruff check app tests && .venv/bin/python -m mypy app \
  && .venv/bin/python -m pytest -q
cd frontend && pnpm lint && pnpm typecheck && pnpm test -- run && pnpm build
```

## 路线图

- [ ] 统一管理项目、数据集、模型和启动配置（跨服务器）
- [ ] 跨服务器同步项目资料

## 文档

- `docs/PRODUCT_SPEC.md` —— 产品边界：做什么、不做什么
- `docs/ARCHITECTURE.md` —— 架构不变量、分层、数据流
- `docs/IMPLEMENTATION_PLAN.md` —— API 契约、采样策略、远程命令规格
- `docs/adr/` —— 架构决策（传输选型、控制面）
