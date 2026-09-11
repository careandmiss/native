# MCP Server 项目目录结构规范

> **整理时间**：2026-09-07
> **适用版本**：MCP 2024-11-05 ~ 2026-07-28 / FastMCP 3.x / Python SDK
> **目标读者**：要在本仓库（`mcp_ppt_server`）维护或新建 MCP server 的开发者
>
> 本文聚焦 **目录怎么组织** —— 哪些文件该放哪、命名怎么取、不放什么。Wire 协议细节看 `mcp-construction-spec.md`，本仓库落地清单在末尾。

---

## 1. 三种规范层级

MCP server 项目结构有几套"模板"在并行使用，混在一起容易乱。先认清三套标准来源：

| 来源 | 适用 | 特点 |
|---|---|---|
| **`create-mcp-server` 官方脚手架** | Python 极简原型 | 只有 `README + pyproject + src/server.py` 三个文件 |
| **FastMCP 官方推荐布局** | Python 生产 server | `src/` src-layout，按 tools/resources/prompts/transport 分包 |
| **官方参考实现 `modelcontextprotocol/servers`** | Python + TypeScript 全套示例 | monorepo，多 server 同仓 |

如果只写一个 server，**FastMCP 官方布局**是事实标准；如果是 monorepo 多 server，参照 `modelcontextprotocol/servers` 仓库。

---

## 2. 极简骨架（`create-mcp-server`）

官方 `uvx create-mcp-server` 生成的内容：

```
my-server/
├── README.md
├── pyproject.toml
└── src/
    └── my_server/
        ├── __init__.py
        ├── main.py
        └── server.py
```

**只适合**：写一个最小 demo 或 CLI 工具演示 MCP 概念。本仓库（`mcp_ppt_server`）远比这个复杂。

---

## 3. 标准生产布局（FastMCP 推荐 — Python）

来自 [FastMCP Development Guidelines](https://mdgrok.com/files/215863) 与 [folderstructure.dev/mcp-server](https://folderstructure.dev/mcp-server) 的共识：

```
mcp-server/
├── src/                       # src-layout — 强制包内 import
│   └── mcp_server_name/       # 包名（snake_case，去 "mcp" 前缀）
│       ├── __init__.py        # 暴露 `mcp` 实例供 main.py 导入
│       ├── __main__.py        # `python -m mcp_server_name` 入口
│       ├── server.py          # FastMCP 实例 + mount 所有 capability
│       ├── tools/             # 按 domain 分文件，@mcp.tool() 装饰
│       │   ├── __init__.py
│       │   ├── search.py
│       │   └── analyze.py
│       ├── resources/         # @mcp.resource() 实现
│       │   ├── __init__.py
│       │   └── files.py
│       ├── prompts/           # @mcp.prompt() 模板
│       │   ├── __init__.py
│       │   └── analysis.py
│       ├── transports/        # 自定义 transport / ASGI 入口
│       ├── auth/              # 鉴权 provider
│       ├── middleware/        # 错误处理 / 日志 / 限流中间件
│       ├── utils/             # HTTP 客户端 / 校验器等共享工具
│       └── types.py           # Pydantic 模型 / 自定义类型
├── tests/                     # pytest 套件
│   ├── conftest.py        # pytest fixtures
│   ├── test_tools.py
│   └── test_resources.py
├── docs/                      # Markdown 文档
├── examples/                  # 演示脚本（CLI demo / curl 例子）
├── pyproject.toml             # PEP 621 项目元数据 + 依赖
├── uv.lock                    # uv 锁定文件（部署时强制）
├── .env.example               # 环境变量模板（不入仓实际值）
├── .gitignore
└── README.md
```

### 3.1 为什么是 src-layout（而不是裸包在根）

- 强制包前缀 import：`from mcp_server_name.tools import search`（相对 import 不允许）
- 防止 IDE 跑到根目录的同名本地包（典型 bug）
- 部署到 Docker / CI 时 import path 不会因 PWD 漂移

### 3.2 包名约定

| 来源 | 命名 |
|---|---|
| **PyPI 发布名** | `mcp-server-<verb-noun>`（例：`mcp-server-fetch`、`mcp-server-git`、`mcp-server-time`） |
| **Python import 名** | `mcp_server_<verb_noun>`（下划线版） |
| **mcpName（协议层）** | `io.github.<owner>/server-<name>` |
| **npm 发布名** | `@modelcontextprotocol/server-<verb-noun>` |
| **mcpName** | `io.github.modelcontextprotocol/server-<verb-noun>` |

### 3.3 `__init__.py` 应该暴露什么

```python
# src/mcp_server_name/__init__.py
from .server import mcp   # 让外部能从 mcp_server_name.mcp 取实例
__version__ = "0.1.0"
```

`main.py`（或仓库根的入口）从这 import，然后包装 ASGI / uvicorn：

```python
# main.py （或 src/mcp_server_name/__main__.py）
from mcp_server_name import mcp
if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8000)
```

---

## 4. 官方 monorepo 参考（`modelcontextprotocol/servers`）

适用于**多 server 同仓**的场景：

```
@modelcontextprotocol/servers/
├── package.json                # root workspace coordinator（private）
├── package-lock.json
├── tsconfig.json
├── src/                        # 每个子目录 = 一个独立 server
│   ├── everything/             # TS: @modelcontextprotocol/server-everything
│   │   ├── package.json
│   │   ├── src/
│   │   └── dist/
│   ├── filesystem/             # TS
│   ├── memory/                 # TS
│   ├── sequentialthinking/     # TS
│   ├── fetch/                  # Py: mcp-server-fetch
│   ├── git/                    # Py: mcp-server-git
│   └── time/                   # Py: mcp-server-time
└── .github/workflows/          # CI/CD
```

要点：

- 每个子目录都是**独立可发布的包**
- TypeScript 和 Python server 可以共存（npm workspaces + 独立 `pyproject.toml`）
- 每个 package 都自带 README + tests + dist（TS）或 `pyproject.toml`（Py）

本仓库不是 monorepo，可以直接走 FastMCP 单包布局。

---

## 5. 各目录放什么 / 不放什么

### 5.1 `src/<package>/server.py` — 必有的"主文件"

职责：

- 实例化 `FastMCP("server-name")`
- 注册 lifespan（DB pool / HTTP client / 全局 state）
- 装配 auth provider（如有）
- 装配 middleware（错误处理、日志、限流）
- **不写**业务逻辑

```python
# server.py
from fastmcp import FastMCP
from .tools.search import register_search_tools
from .resources.config import register_config_resources

mcp = FastMCP("ppt-master")

def setup():
    register_search_tools(mcp)
    register_config_resources(mcp)
    return mcp

setup()
```

### 5.2 `src/<package>/tools/` — 工具实现

按 **domain** 分文件，而不是按动词（`search.py` 比 `get.py` / `post.py` 好）：

```
tools/
├── __init__.py
├── ppt_generation.py   # 所有 PPT 生成的 tool
├── template.py         # 模板相关
└── file_io.py          # 文件读写
```

每个工具文件典型模式：

```python
# tools/ppt_generation.py
from ..server import mcp
from ..types import GenerateRequest, GenerateResult

@mcp.tool()
async def generate_ppt(req: GenerateRequest) -> GenerateResult:
    """Generate a PPTX from outline + optional template URL.
    
    Args:
        req: Input payload with outline (Markdown) and optional template_url.
    Returns:
        Object with status and download_url of the generated PPTX.
    """
    # 业务实现
    ...
```

**不要把几十个 tool 堆在一个文件里** — 200 行以上的 `server.py` 难维护。

### 5.3 `src/<package>/resources/` — 资源实现

```
resources/
├── __init__.py
├── config.py        # "config://settings" 之类
└── files.py         # 文件系统资源
```

注意 URI scheme 的命名：

| Scheme | 用法 |
|---|---|
| `config://...` | 配置 |
| `logs://...` | 日志（只读视图） |
| `db://...` | 数据库记录 |
| `file://...` | 本地文件 |
| `https://...` | 远程 URL 资源 |

### 5.4 `src/<package>/prompts/` — 提示模板

`prompts/` 通常最薄，因为模板简单：

```
prompts/
├── __init__.py
└── analysis.py   # "summarize"、"compare"、"review"
```

### 5.5 `src/<package>/utils/` — 共享工具

放与 MCP 协议无关的纯 helper：

- `http.py` — httpx 客户端封装
- `validators.py` — 业务校验
- `errors.py` — 自定义异常类
- `logging.py` — structlog 配置

**不要**把 LLM 调用、prompt 模板也塞进 utils — 这些属于 `strategies/` 或 `pipeline/`。

### 5.6 `tests/`

```
tests/
├── conftest.py              # pytest fixtures（mock LLM、fixture 路径等）
├── fixtures/                # 测试用静态文件
├── test_tools.py            # 单元测试
├── test_resources.py
├── test_pipeline.py         # 集成测试
└── test_server.py           # server 启动 / handshake 测试
```

Pytest 推荐加 marker 分层：

```toml
# pyproject.toml
[tool.pytest.ini_options]
markers = [
    "unit: fast tests, no I/O",
    "integration: require running server",
    "agentic: require a real LLM",
]
```

`agentic` 标记默认 skip，需要 key 才跑。

### 5.7 `docs/`

放 **给人看的 Markdown**（不是 auto-generated）：

```
docs/
├── README.md                       # docs 索引
├── architecture.md                 # 系统架构图
├── mcp-construction-spec.md        # 协议规范整理（本仓库的）
├── mcp-project-layout.md           # 本文件
├── deployment.md                   # 部署步骤
└── *.md                            # 按主题组织
```

**不要**在 `docs/` 放：

- 自动生成的 API doc（用 mkdocs / sphinx 单独生成到 `site/`）
- vendored 第三方文档 — 放 `bundled/` 或 `vendor/`

### 5.8 `examples/`

放**可直接跑的 demo**：

```
examples/
├── cli_demo_ppt_generate.py    # python -m examples 或直接 python xxx.py
├── last_generate_payload.json  # 上次 demo 的真实 payload
└── README.md
```

不要把 `examples/` 当成第二份 `src/`。

### 5.9 `bundled/` 或 `vendor/`（可选）

放**内联 vendored 第三方参考**：

- 协议的旧版本规范
- 上游项目的代码快照（带 LICENSE）
- prompt 模板库（ppt-master 的 `docs/ppt-master-v6.3.0-reference/`）

约定：

- **必须**带 LICENSE 文件
- 单独加 `.gitignore` 规则不现实 —— 整个 `bundled/` 入仓
- 文件名带版本号，便于以后升级

### 5.10 根目录的 `.gitignore`

最少覆盖这些：

```gitignore
# 运行时
*.pid
*.log
logs/

# Python
__pycache__/
*.pyc
*.pyo
.venv/
venv/
.env

# Claude Code
.claude/

# 构建
dist/
build/
*.egg-info/

# IDE
.vscode/
.idea/

# 系统
.DS_Store
Thumbs.db
```

---

## 6. 入口文件该怎么放

| 场景 | 入口位置 |
|---|---|
| `python -m mcp_server_name` | `src/mcp_server_name/__main__.py` |
| `my-server` CLI 命令 | `pyproject.toml` 配 `[project.scripts]` 指向 `mcp_server_name.cli:main` |
| FastAPI/Uvicorn 部署 | 根目录 `main.py` import `mcp` 后用 `mcp.http_app()` 包成 ASGI |
| Docker | `CMD ["python", "-m", "mcp_server_name"]` 或 `["uvicorn", "main:app"]` |

**原则**：本地开发用 `__main__.py`，部署 ASGI 用根 `main.py`，两者都只是"薄壳"。

---

## 7. `pyproject.toml` 必备字段

```toml
[project]
name = "mcp-server-<name>"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "mcp[cli]>=1.0",      # 或 fastmcp>=2.13
    "httpx>=0.27",
    "pydantic>=2.12",
]

[project.scripts]
mcp-server-<name> = "mcp_server_name.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = ["integration", "agentic"]
```

---

## 8. 本仓库（`mcp_ppt_server`）映射

对照 ppt6.2.0 分支的实际目录：

```
mcp_ppt_server/                       ← 仓库根
├── .claude/                          ← Claude Code 私有配置（应 .gitignore）
├── .env                              ← 本地密钥（应 .gitignore）✅
├── .env.example                      ✅
├── .gitignore                        ✅
├── README.md                         ✅
├── SKILL.md                          ← Claude Code skill 清单（ppt6.2.0 特性）
├── requirements.txt                  ✅
├── server.py                         ⚠️ 应改为 src/mcp_ppt_server/__main__.py 的薄壳
├── deploy/
│   ├── mcp-ppt-server.service        ← systemd unit
│   └── README.md
├── docs/                             ✅
│   ├── README.md                       ← 索引
│   ├── mcp-construction-spec.md      （新增）
│   ├── mcp-project-layout.md          （本文件）
│   ├── ppt-master-v6.3.0-reference/  ← 上游 ppt-master v6.3.0 参考（references/ + templates/*.md）
│   ├── v6.3.0-changes.md              ← v6.2.0→v6.3.0 改动清单
│   ├── template-reuse-gap-v6.2.0.md   ← [历史记录] v6.2.0 模板复用分析
│   ├── test-vs-ppt6.2.0-comparison.md ← [历史记录] 分支对比
│   └── 生产部PPT生成_全流程整合_v6.2.0.md  ← [历史记录] v6.2.0 工作流整合
├── src/
│   └── mcp_ppt_server/               ← 主包 ✅（src-layout）
│       ├── __init__.py
│       ├── server.py                 ← FastMCP 实例 ✅
│       ├── tools/                    ✅（按 domain 分文件）
│       ├── resources/
│       ├── prompts/
│       ├── ...
│       └── vendor/                   ← 第三方代码快照（带 LICENSE）
├── tests/                            ✅
├── examples/                         ✅
│   ├── cli_demo_ppt_generate.py
│   └── last_generate_payload.json
├── dev/                              ⚠️ 开发探针，建议挪到 scripts/ 或独立 git
├── bundled/                          ✅ vendored 上游脚本（ppt6.2.0 特性；未与 .skill 同步升级到 v6.3.0）
├── python/                           ⚠️ Python 工具脚本，建议挪到 scripts/
├── logs/                             ⚠️ 运行时日志（应 .gitignore）✅
├── deploy.sh                         ❌ 删除（master 分支已删，ppt6.2.0 不该有）
└── server.sh                         ⚠️ 启动脚本，建议挪到 scripts/ 或 deploy/
```

### 8.1 偏离 FastMCP 规范的点

| 偏离 | 影响 | 建议 |
|---|---|---|
| 入口是 `server.py`（根目录）而非 `src/mcp_ppt_server/__main__.py` | 不阻碍 import，ASGI 部署仍要写一层 | 可保留，ASGI thin shell 也合理 |
| `vendor/` 在 `src/mcp_ppt_server/` 下 | bundling 时会一起打进去 | 如果想 vendored 不打包，用 `MANIFEST.in` 排除 |
| `bundled/`、`dev/`、`python/` 在根目录 | 命名上不规范，IDE 可能误识别 | `dev/` → `scripts/`，`bundled/` 可以保留（语义清晰），`python/` → `scripts/python/` |
| `deploy.sh`、`server.sh` 在根目录 | 部署脚本混在源码里 | 已有 `deploy/` 目录，两个脚本应挪过去或删除 |
| `.claude/` 已 gitignore ✅ | — | 保持 |

### 8.2 ppt6.2.0 当前结构 vs 改进目标

```
当前                                  改进目标
───                                    ───
src/mcp_ppt_server/                    src/mcp_ppt_server/
  server.py                              server.py            ← FastMCP 实例
  tools.py (大文件)                       tools/               ← 按 domain 分
  strategist_*.py                        strategist/          ← PPT 战略生成
  template_*.py                          template/            ← 模板处理
  executor_*.py                          executor/            ← 执行渲染
  ...                                     ...

bundled/  (vendored 参考)              bundled/             ← 保持
docs/                                   docs/
examples/                              examples/
tests/                                 tests/
dev/  (散落的探针)                     scripts/             ← 合并
python/  (独立脚本)                    scripts/python/      ← 或 tools/
```

---

### 8.3 实测合规审计 (2026-09-07)

按 §5 / §6 / §7 / §9 表格对当前 `mcp_ppt_server` 仓库（master, HEAD `bcd9270`）做实测。命令来源：`git ls-files`、`ls -la`、`cat .gitignore`、`cat pyproject.toml`。

#### 已修正 ✅（最近 5 个 commit 范围内）

| 项 | 状态 | 提交 SHA |
|---|---|---|
| 入口是 `server.py`（根目录）而非 `src/<pkg>/__main__.py` | ✅ 改为 `src/mcp_ppt_master/__main__.py` 薄壳 | `194eb71` |
| `dev/` `python/` 在根目录 | ✅ 已挪到 `src/mcp_ppt_master/`（src-layout） | `194eb71` |
| `deploy.sh` `server.sh` 在根目录 | ✅ 已删除；无 `deploy/` 目录 | `232d872` |
| `.gitignore` 缺 `.claude/` `*.pid` `*.log` `logs/` | ✅ 已补 4 行 | `232d872` |
| 没有 `pyproject.toml` | ✅ 已加（PEP 621 + `[project.scripts]` + hatchling backend） | `232d872` |
| `pipeline.py` 2744 行单文件 | ✅ 已拆为 `pipeline/` 子包（8 模块：`generate` `llm_strategist` `llm_helpers` `prompts` `datatypes` `svg_preprocess` `template_info` `vendored_runner`） | `1be96a0` |
| `docs/` 缺 v6.3.0 上游参考 | ✅ 镜像 `docs/ppt-master-v6.3.0-reference/`（170 文件） | `bcd9270` |

#### 仍未修正 ❌（按 § 顺序）

| § 引用 | Doc 要求 | 实际状态 | 备注 |
|---|---|---|---|
| §5.1 | `src/<pkg>/server.py` 命名 | `src/mcp_ppt_master/mcp_server.py` | 文件名多 `mcp_` 前缀；功能上是 FastMCP 实例 |
| §5.2 | `src/<pkg>/tools/` 按 domain 分文件 | **无** `tools/`；改为 `io/` `patches/` `pipeline/` `validate/` 域驱动 | **有意**：本项目是 pipeline 架构而非工具集合 |
| §5.3 | `src/<pkg>/resources/` | **无** `resources/` | 同上：MCP resource 概念不适用本架构 |
| §5.4 | `src/<pkg>/prompts/` | **无** `prompts/` | 同上：prompt 模板集中在 `pipeline/prompts.py` |
| §5.6 | 顶层 `tests/` 含 `conftest.py` + `test_*.py` + pytest markers | 顶层 `tests/` 只有 `fixtures/sample-project/`（4 个 fixture 文件）；真实测试在 `src/mcp_ppt_master/tests/`（4 个 `test_*.py`，**无** `conftest.py`） | 位置颠倒：测试应在顶层 |
| §5.7 | `.env.example` 在仓；`.env` gitignore | `.env` gitignore ✅；**缺** `.env.example` 模板 | 极小工作量 |
| §7 | `pyproject.toml` 含 `[tool.pytest.ini_options]` markers / asyncio_mode | 缺失 | 起步阶段只配 `[project]` 和 `[build-system]` |
| §9 | 包名 `mcp_server_<verb_noun>`（去 `mcp` 前缀 → `ppt_master`） | `mcp_ppt_master` | **有意保留**：与上游 `ppt-master` 强绑，`mcp_` 前缀方便识别为 MCP 包 |
| §9 | `examples/last_generate_payload.json` 在仓 | **缺失** | demo 真实 payload 证据丢失 |

#### Fix 优先级

| P | 项 | 工作量 | 风险 |
|---|---|---|---|
| 🥇 P0 | 把顶层 `tests/` 改造为标准 pytest 套件：补 `conftest.py`、从 `src/mcp_ppt_master/tests/` 迁移或镜像 test_*.py、加 `__init__.py` | 1-2h | 低（fixtures 已在顶层；测试位置矫正） |
| 🥈 P1 | `pyproject.toml` 加 `[tool.pytest.ini_options]` 配 markers（`unit` / `integration` / `agentic`）和 `asyncio_mode = "auto"` | 5 min | 极低 |
| 🥈 P1 | 加 `.env.example` 模板（`MINIMAX_API_KEY=` / `OUTPUT_DIR=` / `LOG_LEVEL=`） | 5 min | 极低 |
| 🥉 P2 | 重命名 `mcp_server.py` → `server.py`（§5.1 字面对齐） | 3 min + 改 1 处 import | 低 |
| 🥉 P2 | 重新跑 `examples/cli_demo_ppt_generate.py` 留 `last_generate_payload.json` | 5 min | 极低 |

#### 未修正但保留的偏离（标 ❌ 是有意的）

- **§5.2-5.4 不用 `tools/` `resources/` `prompts/` 子目录**：本项目 MCP 工具实现横跨多个 domain（`mcp_server.py` 是薄 dispatch，`pipeline/` 是实现）。按"工具类型"切会破坏 pipeline 内的 cohesive 状态机。详见 `src/mcp_ppt_master/mcp_server.py` 与 §6 入口薄壳的设计。

- **§9 包名约定保留 `mcp_` 前缀**：避免与上游 `ppt-master` 仓库名混淆；目录名 `mcp_ppt_master` 也是与 `ppt-master` 命名的镜像对应。

#### 命令清单（审计可复现）

```bash
# 顶层目录与文件
git ls-files | head -80
ls -la  # 应见 src/ docs/ bundled/ examples/ tests/ + pyproject.toml + .gitignore
ls -la src/mcp_ppt_master/  # 应见 __init__.py __main__.py mcp_server.py + io/ patches/ pipeline/ validate/ tests/

# .gitignore 必备 4 行（§5.10）
cat .gitignore | grep -E "^(\.claude|\*\.pid|\*\.log|logs/)"

# pyproject.toml 字段（§7）
cat pyproject.toml

# 已删除项确认
ls server.py server.sh deploy.sh 2>/dev/null  # 应为空
ls dev/ python/ 2>/dev/null  # 应为空
```

---

## 9. 快速检查表

写完一个 MCP server 后，按这个清单自检：

| 检查项 | 通过 |
|---|---|
| 用 src-layout（包在 `src/<pkg>/` 下） | ⬜ |
| 包名 `mcp_server_<verb_noun>`，发布名 `mcp-server-<verb-noun>` | ⬜ |
| `server.py` 只放 FastMCP 实例 + 注册，业务在 `tools/` `resources/` `prompts/` | ⬜ |
| `__init__.py` 暴露 `mcp` 实例 | ⬜ |
| 工具按 domain 分文件，不要一锅端 | ⬜ |
| 每个 tool/resource/prompt 有 docstring + 类型注解（生成 JSON Schema 用） | ⬜ |
| `tests/` 有 `conftest.py` + 至少 1 个 unit + 1 个 integration | ⬜ |
| `pyproject.toml` 有 `[project.scripts]`、version、requires-python | ⬜ |
| `.env.example` 在仓；`.env` gitignore | ⬜ |
| `docs/` 有人写的架构 / 使用说明 | ⬜ |
| `examples/` 有可跑的 demo | ⬜ |
| 入口（`__main__.py` 或 `main.py`）只是薄壳，不写业务 | ⬜ |
| `vendor/` / `bundled/` 第三方材料带 LICENSE | ⬜ |
| `.gitignore` 覆盖 `__pycache__/`、`.env`、`logs/`、`.claude/`、`*.pid`、`*.log` | ⬜ |

---

## 10. 参考来源

- [FastMCP Development Guidelines (mdgrok)](https://mdgrok.com/files/215863)
- [MCP Server Python Project Structure (folderstructure.dev)](https://folderstructure.dev/mcp-server)
- [FastMCP — Build MCP Servers in Python (agent-skills.md)](https://agent-skills.md/skills/ovachiever/droid-tings/fastmcp)
- [modelcontextprotocol/servers — Repository Structure (DeepWiki)](https://deepwiki.com/modelcontextprotocol/servers/1.2-repository-structure-and-package-management)
- [modelcontextprotocol/python-sdk — Key Concepts & Architecture (DeepWiki)](https://deepwiki.com/modelcontextprotocol/python-sdk/1.2-key-concepts-and-architecture)
- [create-python-server 官方脚手架](https://github.com/modelcontextprotocol/create-python-server)
- [MCP Server Python: Build Production-Ready Servers with FastMCP (mcpize.com)](https://mcpize.com/blog/mcp-server-python)
- [How to Build Production-Ready MCP Servers with FastMCP (dev.to)](https://dev.to/unfairhq/how-to-build-production-ready-mcp-servers-with-fastmcp-in-python-from-complex-pydantic-input-250e)
- [MCP Server Template (mcprepository.com)](https://mcprepository.com/frankietime/mcp-template)
- [Best Practices — MCP Context Forge (ibm.github.io)](https://ibm.github.io/mcp-context-forge/1.0.0/best-practices/mcp-best-practices?q=)
- [MCP Python Server skill (skilldb.dev)](https://skilldb.dev/skills/mcp-server-skills/mcp-python-server)
- [Build an MCP Server: Complete Tutorial (mcpize.com)](https://mcpize.com/developers/build-mcp-server)
- [Building Your First FastMCP Server (cloudurable.com)](https://cloudurable.com/blog/building-your-first-fastmcp-server-a-complete-guid/)
