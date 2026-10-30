<div align="center">

# OSS MCP Server

**把阿里云 OSS 变成 AI 可以直接调用的文件工具集**

上传 · 替换 · 下载 · 目录打包 · 删除 · 访问地址 · 多分组认证 · 跨平台托管

<p>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/MCP-1.29%2B-6E56CF" alt="MCP 1.29+">
  <img src="https://img.shields.io/badge/Auth-Bearer%20Token-2EA043" alt="Bearer Token">
  <img src="https://img.shields.io/badge/Aliyun-OSS-FF6A00" alt="Aliyun OSS">
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-2F81F7" alt="Platform">
</p>

</div>

> 在 MCP 客户端里说一句「从生产 OSS 下载 lstd/pay/a.png」，模型会自动把「生产」匹配到对应认证分组和 Bucket，然后调用下载工具。

---

## 目录

- [功能特性](#功能特性)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [认证分组配置](#认证分组配置)
- [MCP 工具](#mcp-工具)
- [接入 MCP 客户端](#接入-mcp-客户端)
- [HTTP Bearer Token 认证](#http-bearer-token-认证)
- [服务管理脚本](#服务管理脚本)
- [命令行工具](#命令行工具)
- [测试](#测试)
- [日志与排错](#日志与排错)
- [安全建议](#安全建议)
- [已知限制](#已知限制)

## 功能特性

- **文件存在性判断**：基于 OSS HEAD 请求精确判断对象是否存在，不把目录前缀误判为文件。
- **上传**：支持本地文件和网络 URL，上传时可指定对象 ACL（`private` / `public-read` / `public-read-write` / `default`）。
- **替换**：用本地文件替换 OSS 上已有对象；默认保留原对象路径（URL 不变），也可按本地文件名改名并删除旧对象。
- **下载**：支持单文件、多文件和整个目录；多文件与目录自动打包为 ZIP，避免大量小文件逐个落盘。
- **删除**：支持单个删除和批量删除；批量删除自动去重，并按 OSS 限制每 1000 个一批提交。
- **访问地址**：公共读 Bucket 可使用自定义域名直链；私有 Bucket 自动生成带有效期的签名 URL。
- **多分组认证**：一个配置文件管理多套 AccessKey、Endpoint、Bucket，并通过别名做自然语言路由。
- **HTTP Bearer 认证**：HTTP/SSE 模式支持 Bearer Token；监听非本机地址时强制配置，避免 OSS 能力裸奔。
- **两种接入方式**：MCP 客户端本地拉起（stdio），或作为后台 HTTP 服务运行（streamable-http / SSE）。
- **跨平台托管**：提供 Linux / macOS 和 Windows 的启动、停止、重启、状态、日志脚本。
- **可离线验证**：冒烟测试使用假的 Bucket 客户端，不依赖真实 OSS 凭证即可运行。

## 项目结构

```text
oss_mcp_server/
├── config/
│   └── oss_config.example.json    # 分组配置模板（唯一可提交的配置）
├── scripts/
│   ├── mcp_service.sh             # Linux / macOS 服务管理
│   ├── mcp_service.ps1            # Windows PowerShell 服务管理
│   └── mcp_service.bat            # Windows 双击 / CMD 入口
├── src/
│   ├── http_auth.py               # HTTP Bearer Token 中间件与启动策略
│   ├── oss_mcp_server.py          # MCP 服务层：配置、分组路由、11 个工具
│   └── oss_utils.py               # OSS 原子操作层 + 命令行入口
├── tests/
│   ├── smoke_http_auth.py         # Bearer Token 校验与强制策略测试
│   ├── smoke_oss_utils.py         # OSS 操作冒烟测试
│   ├── smoke_mcp_server.py        # MCP stdio / HTTP 冒烟测试
│   ├── smoke_service_scripts.sh   # 启动脚本环境准备与依赖检测测试
│   └── smoke_service_scripts.ps1  # PowerShell 启动流程编排测试
├── requirements.txt
├── README.md
└── .gitignore
```

分层约定：

| 目录 | 职责 |
| --- | --- |
| `config/` | 只放配置模板；真实的 `oss_config.json` 已被 `.gitignore` 排除 |
| `src/oss_utils.py` | 封装 oss2，提供可复用的上传、下载、删除、URL、存在性判断能力 |
| `src/http_auth.py` | Bearer Token 中间件、Token 校验和监听地址安全策略 |
| `src/oss_mcp_server.py` | 读取配置、解析分组别名、注册 MCP 工具并选择传输方式 |
| `scripts/` | 进程托管、日志、PID、端口检查，不包含业务逻辑 |
| `tests/` | 无真实凭证的冒烟测试，覆盖原子操作、HTTP 认证、MCP 协议链路和启动脚本环境准备 |

运行时还会自动生成 `logs/`、`run/` 和 Python 的 `__pycache__/`，它们都不会进入 Git。

## 快速开始

### 1. 环境要求

- Python **3.10 及以上**
- 能访问阿里云 OSS 的网络环境
- 具备目标 Bucket 读写权限的 AccessKey，或 STS 临时凭证

### 2. 准备运行环境

推荐直接使用服务管理脚本，`start` / `restart` 前会自动完成：

1. 检测项目虚拟环境 `.venv` 或 `venv`；
2. 都不存在时，用 PATH 中的 Python 3.10+ 创建 `.venv`；
3. 升级该虚拟环境的 pip；
4. 检测 `oss2` / `mcp` / `uvicorn`，缺失时执行 `pip install -r requirements.txt`；
5. 依赖齐全后启动服务。

依赖已经齐全时不会重复执行 pip，正常启动不会额外访问网络。设置 `OSS_MCP_AUTO_INSTALL=0` 可关闭创建虚拟环境、升级 pip 和安装依赖；`PYTHON_BIN` 为严格模式，只使用指定解释器，不会修改环境。

也可以手动安装：

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果使用 PyCharm / IDEA 打开项目，IDE 可能会自动创建一个只有 `pip` 的空 `venv`。启动脚本会检测到缺失依赖并自动补装；若关闭了自动安装，请手动对该解释器执行 `python -m pip install -r requirements.txt`，否则会报 `ModuleNotFoundError: No module named 'oss2'` 或 `'mcp'`。

### 3. 准备配置

```bash
# Linux / macOS
cp config/oss_config.example.json config/oss_config.json

# Windows PowerShell
Copy-Item config\oss_config.example.json config\oss_config.json
```

编辑 `config/oss_config.json`，填入真实的 AccessKey、Endpoint、Bucket 和别名。对外提供 HTTP 服务时，还要填写 `http_bearer_token` 或设置 `OSS_MCP_BEARER_TOKEN`。真实配置文件包含凭证，**不要提交到 Git**。

### 4. 启动

本地 MCP 客户端接入（由客户端拉起进程，使用 stdio）见 [接入 MCP 客户端](#接入-mcp-客户端)。

直接启动 HTTP 服务：

```bash
# 仅本机访问：未配置 Token 时允许启动
python src/oss_mcp_server.py --config config/oss_config.json \
  --transport streamable-http --host 127.0.0.1 --port 8000

# 对外监听：必须配置 Bearer Token
python src/oss_mcp_server.py --config config/oss_config.json \
  --transport streamable-http --host 0.0.0.0 --port 8000 \
  --bearer-token "<至少 16 位的随机 Token>"
```

后台托管：

```bash
# Linux / macOS
chmod +x scripts/mcp_service.sh
./scripts/mcp_service.sh start

# Windows
.\scripts\mcp_service.bat start
```

默认访问地址为 `http://127.0.0.1:8000/mcp`。HTTP 认证细节见 [HTTP Bearer Token 认证](#http-bearer-token-认证)。

## 认证分组配置

`config/oss_config.json` 使用 `groups` 管理多套认证信息：

```json
{
  "default_group": "prod",
  "http_bearer_token": "",
  "groups": {
    "prod": {
      "alias": "生产",
      "access_key_id": "LTAI5tXXXXXXXXXXXXXX",
      "access_key_secret": "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
      "endpoint": "oss-cn-hangzhou.aliyuncs.com",
      "bucket": "prod-bucket",
      "domain": "https://static.example.com",
      "default_prefix": "lstd/pay/",
      "public_read": true
    },
    "test": {
      "alias": "测试",
      "aliases": ["测试OSS"],
      "access_key_id": "LTAI5tYYYYYYYYYYYYYY",
      "access_key_secret": "YYYYYYYYYYYYYYYYYYYYYYYYYYYYYY",
      "security_token": "",
      "endpoint": "oss-cn-shanghai.aliyuncs.com",
      "bucket": "test-bucket",
      "default_prefix": "lstd/test/",
      "public_read": false
    }
  }
}
```

### 字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `default_group` | 否 | 默认分组名，必须是 `groups` 中的键；不填时使用第一个分组 |
| `http_bearer_token` | 对外监听时必填 | HTTP/SSE 模式的 Bearer Token，至少 16 个可打印 ASCII 字符且不能含空白；仅本机监听时可留空 |
| `groups` | 是 | 非空对象，键为分组名称，值为该分组的配置 |
| `alias` | 否 | 分组的主别名，便于自然语言指定，如「生产」 |
| `aliases` | 否 | 额外别名数组，可与 `alias` 同时使用 |
| `access_key_id` | 是 | 兼容 `accessKeyId`、`accessKey` |
| `access_key_secret` | 是 | 兼容 `accessKeySecret`、`accessSecret` |
| `security_token` | 否 | STS 临时凭证 Token，兼容 `securityToken`；不使用时留空或删除 |
| `endpoint` | 是 | 如 `oss-cn-hangzhou.aliyuncs.com` |
| `bucket` | 是 | 兼容 `bucket_name`、`bucketName` |
| `domain` | 否 | 自定义访问域名；未写协议时自动补 `https://`，公共读时优先用于拼接 URL |
| `default_prefix` | 否 | 生成 URL 时的默认目录前缀，兼容 `defaultPrefix`、`prefix`，默认 `lstd/pay/` |
| `public_read` | 否 | JSON 布尔值，兼容 `publicRead`；默认 `false`（签名 URL） |

`http_bearer_token` 是配置文件根节点字段，不要放进某个分组内部。`public_read` 等布尔字段请使用 JSON 布尔值，不要写成字符串。

生成一个足够随机的 Token：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 别名是如何匹配的

分组名称和别名都可以传给工具的 `group` 参数。匹配时：

1. 去掉所有空白字符，并忽略大小写；
2. 自动兼容常见后缀变体，例如「生产」也可以匹配「生产 OSS」「生产OSS」「生产云存储」；
3. 启动时检查名称/别名冲突，若两个分组被同一个别名命中会直接报错，避免路由到错误的 Bucket；
4. 不传 `group` 时使用默认分组。

`list_groups` 只返回脱敏后的 AccessKey，不会返回 Secret 或 STS Token。

### 兼容的单分组写法

配置文件根节点直接放认证字段时，会作为一个名为 `default` 的分组加载：

```json
{
  "access_key_id": "LTAI...",
  "access_key_secret": "...",
  "endpoint": "oss-cn-hangzhou.aliyuncs.com",
  "bucket": "demo-bucket"
}
```

MCP 服务也支持完全不用配置文件，直接传入单分组参数：

```bash
python src/oss_mcp_server.py \
  --access-key-id LTAI... --access-key-secret ... \
  --endpoint oss-cn-hangzhou.aliyuncs.com --bucket demo-bucket
```

单分组模式的分组名固定为 `default`。此时也可使用环境变量 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_SECURITY_TOKEN`、`OSS_ENDPOINT`、`OSS_BUCKET`、`OSS_DOMAIN`、`OSS_DEFAULT_PREFIX`、`OSS_PUBLIC_READ`。

## MCP 工具

服务共暴露 11 个工具。除 `list_groups` 外，其余工具都接受可选的 `group` 参数，传分组名称或别名均可。

| 工具 | 功能 | 主要参数 | 属性 |
| --- | --- | --- | --- |
| `list_groups` | 列出分组、别名、Bucket、默认前缀，凭证脱敏 | 无 | 只读 |
| `file_exists` | 精确判断对象是否存在 | `object_name`、`group` | 只读 |
| `upload_file` | 上传本地文件 | `local_path`、`object_name`、`acl`、`group` | 写 |
| `upload_from_url` | 下载网络文件并上传 | `url`、`object_name`、`acl`、`group` | 写、联网 |
| `replace_file` | 用本地文件替换已有对象，可保留原名或按本地文件名改名 | `local_path`、`object_name`、`keep_original_name`、`acl`、`group` | 写、可能删除旧对象 |
| `download_file` | 下载单个对象到本地 | `object_name`、`local_path`、`group` | 写本地 |
| `download_files` | 下载多个对象并打包 ZIP，保留完整对象路径 | `object_names`、`local_dir`、`zip_name`、`group` | 写本地 |
| `download_folder` | 按前缀下载整个目录并打包 ZIP，保留相对路径 | `prefix`、`local_dir`、`zip_name`、`group` | 写本地 |
| `delete_file` | 删除单个对象 | `object_name`、`group` | 删除，不可恢复 |
| `delete_files` | 批量删除对象，自动分批 | `object_names`、`group` | 删除，不可恢复 |
| `get_file_url` | 生成公共读直链或私有签名 URL | `relative_path`、`prefix`、`expires`、`public`、`group` | 只读 |

### 参数补充

- **`acl`**：可选 `default`、`private`、`public-read`、`public-read-write`。不传时继承 Bucket 的 ACL。
- **`keep_original_name`**：仅 `replace_file` 使用。默认 `true`，表示覆盖原对象内容、对象路径和 URL 不变；设为 `false` 时使用本地文件名放到原对象所在目录，上传成功后删除旧对象。
- **`local_path`**：可以是文件路径，也可以是已存在的目录；传入目录时保留 OSS 文件名。不传时保存到当前工作目录。
- **`local_dir`**：ZIP 的存放目录，默认当前工作目录。
- **`zip_name`**：ZIP 文件名或路径。不传时自动生成，例如 `prod-bucket_download_20261029_153000.zip`。
- **`prefix`（下载）**：未以 `/` 结尾时会自动补齐；空目录会报错，并把已创建的空 ZIP 删除。
- **`prefix`（获取 URL）**：不传时使用分组的 `default_prefix`。
- **`expires`**：私有签名 URL 的有效期，单位秒，默认 3600。
- **`public`**：不传时使用分组的 `public_read`。

工具返回值会带上实际命中的分组信息，便于确认路由是否正确：

```json
{
  "group": "prod",
  "group_alias": "生产",
  "bucket": "prod-bucket",
  "object_name": "lstd/pay/a.png",
  "exists": true
}
```

下载类工具返回本地绝对路径和大小；批量删除返回实际删除的对象列表。`replace_file` 返回 `old_object_name`、`new_object_name`、`renamed`、`old_deleted`，便于确认是否改名以及旧对象是否已删除。`delete_file`、`delete_files` 与 `replace_file` 带有 destructive 注解，支持该能力的客户端通常会请求二次确认。

### 替换规则

- 原对象必须存在；不存在会拒绝执行，避免对象路径写错时静默变成新建。
- 保留原名：直接 `PutObject` 覆盖原对象内容，对象 ACL 由 `acl` 参数决定，不传则继承 Bucket。
- 改名：新对象路径为“原对象所在目录 + 本地文件名”；如果该路径已存在其他对象，直接报错，不覆盖第三方文件。
- 执行顺序是先上传新内容，成功后再删除旧对象。删除失败会保留两个对象并明确报错，不会假装成功。
- 单次覆盖在 OSS 层是原子的；改名模式包含“上传 + 删除”两步，不是跨对象原子操作，建议开启 Bucket 版本控制。

## 接入 MCP 客户端

### 方式一：stdio（推荐本地使用）

由 MCP 客户端拉起 Python 进程，不需要常驻端口。通用 `mcpServers` 配置如下（不同客户端字段可能略有差异）：

```json
{
  "mcpServers": {
    "oss": {
      "command": "python",
      "args": [
        "<项目绝对路径>/src/oss_mcp_server.py",
        "--config",
        "<项目绝对路径>/config/oss_config.json"
      ]
    }
  }
}
```

建议把 `command` 换成项目虚拟环境中的 Python 绝对路径，确保客户端能加载 `mcp`、`oss2` 和 `requests`：

- Linux / macOS：`<项目绝对路径>/.venv/bin/python`
- Windows：`<项目绝对路径>/.venv/Scripts/python.exe`

`--config` 也可以省略，此时服务会读取项目下的 `config/oss_config.json`。stdio 模式下不要手动启动服务，客户端会自行管理进程。

### 方式二：streamable-http

先启动服务。本机未配置 Token 时可直接访问；启用认证后，客户端必须带 `Authorization` 请求头：

```bash
# 本机免 Token（默认）
python src/oss_mcp_server.py --config config/oss_config.json \
  --transport streamable-http --host 127.0.0.1 --port 8000

# 启用 Bearer Token
python src/oss_mcp_server.py --config config/oss_config.json \
  --transport streamable-http --host 127.0.0.1 --port 8000 \
  --bearer-token "$OSS_MCP_BEARER_TOKEN"
```

MCP 端点为：

```text
http://127.0.0.1:8000/mcp
```

支持 URL 和自定义请求头的客户端可以这样配置：

```json
{
  "mcpServers": {
    "oss-http": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer <TOKEN>"
      }
    }
  }
}
```

如果客户端不支持自定义请求头，就无法连接已启用 Bearer 认证的 HTTP 服务，请改用 stdio，或通过支持注入请求头的反向代理/客户端插件接入。若客户端要求显式指定传输类型，请按其文档补充 `streamable-http` 类型字段。

### 方式三：SSE（兼容旧客户端）

```bash
python src/oss_mcp_server.py --config config/oss_config.json \
  --transport sse --host 127.0.0.1 --port 8000 \
  --bearer-token "$OSS_MCP_BEARER_TOKEN"
```

端点为 `http://127.0.0.1:8000/sse`，同样需要通过 `Authorization: Bearer <TOKEN>` 请求头认证。新客户端优先使用 streamable-http。

### 自然语言路由示例

配置了 `prod`（别名「生产」）和 `test`（别名「测试」）后：

| 用户说法 | 模型应传入的参数 |
| --- | --- |
| 从生产 OSS 下载 `lstd/pay/a.png` | `group="生产"` |
| 看看测试 OSS 上有没有 `lstd/test/b.png` | `group="测试"` |
| 上传到 OSS 的 `lstd/pay/c.png` | 不传 `group`，使用默认分组 |

服务初始化信息中会列出所有分组和别名，工具的 `group` 参数描述也会动态写入别名列表，模型不需要猜测。

## HTTP Bearer Token 认证

Bearer Token 只作用于 HTTP/SSE 传输，stdio 由本地客户端直接拉起进程，不经过网络。启用后，`/mcp`、`/sse` 及后续会话请求都必须携带：

```http
Authorization: Bearer <TOKEN>
```

### Token 来源与优先级

| 优先级 | 来源 | 示例 |
| --- | --- | --- |
| 1 | 命令行参数 | `--bearer-token "<TOKEN>"` |
| 2 | 环境变量 | `OSS_MCP_BEARER_TOKEN="<TOKEN>"` |
| 3 | 配置文件根节点 | `"http_bearer_token": "<TOKEN>"` |

推荐把 Token 放入环境变量或已被 `.gitignore` 排除的 `config/oss_config.json`，不要写进脚本、README 或提交记录。命令行参数可能出现在进程列表中，生产环境优先使用环境变量或配置文件。Token 至少 16 个字符，只能包含可打印 ASCII 字符（不能使用中文等非 ASCII 字符），不能包含空白字符；日志只记录“认证已启用”，不会输出 Token。

### 强制策略

| 监听地址 | 是否配置 Token | 行为 |
| --- | --- | --- |
| `127.0.0.1` / `localhost` / `::1` | 否 | 警告后允许启动，方便本地开发 |
| 本机地址 | 是 | 所有 HTTP 请求校验 Token |
| `0.0.0.0` / 局域网 IP / 公网 IP | 否 | 拒绝启动并提示配置 Token |
| 非本机地址 | 是 | 允许启动，所有 HTTP 请求校验 Token |

服务没有 TLS。对外监听时应放在反向代理之后，由代理提供 HTTPS，并限制来源 IP；Bearer Token 只解决“调用方是否持有令牌”，不能替代网络边界和 TLS。

### 验证认证是否生效

```bash
# 未携带 Token：应返回 401
curl -i http://127.0.0.1:8000/mcp

# 携带 Token：认证通过后返回 MCP 协议层的响应
curl -i -H "Authorization: Bearer $OSS_MCP_BEARER_TOKEN" \
  http://127.0.0.1:8000/mcp
```

未认证响应示例：

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="oss-mcp"
Content-Type: application/json; charset=utf-8

{"error":"unauthorized","message":"Missing or invalid Bearer token"}
```

如果普通 GET 在认证通过后返回 `406 Not Acceptable`，说明已经通过 Token 校验，只是请求不符合 MCP 会话握手要求；用 MCP 客户端初始化会话即可。

### 常见配置示例

Linux / macOS 环境变量：

```bash
export OSS_MCP_BEARER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
./scripts/mcp_service.sh restart
```

Windows PowerShell：

```powershell
$env:OSS_MCP_BEARER_TOKEN = "请替换为至少16位的随机Token"
.\scripts\mcp_service.ps1 restart
```

配置文件方式：

```json
{
  "default_group": "prod",
  "http_bearer_token": "请替换为至少16位的随机Token",
  "groups": {
    "prod": {
      "access_key_id": "LTAI...",
      "access_key_secret": "...",
      "endpoint": "oss-cn-hangzhou.aliyuncs.com",
      "bucket": "prod-bucket"
    }
  }
}
```

客户端配置中的请求头：

```json
"headers": {
  "Authorization": "Bearer 请替换为实际Token"
}
```

## 服务管理脚本

### 命令一览

两个平台的脚本行为一致：

| 命令 | 作用 |
| --- | --- |
| `start` | 后台启动服务；已在运行则直接返回，不重复启动 |
| `stop` | 根据 PID 文件停止服务 |
| `restart` | 先停止再启动 |
| `status` | 查看运行状态、PID、配置、监听地址和日志位置 |
| `logs` | 实时跟踪日志，按 `Ctrl+C` 退出 |

### Linux / macOS

```bash
chmod +x scripts/mcp_service.sh

./scripts/mcp_service.sh start
./scripts/mcp_service.sh status
./scripts/mcp_service.sh logs
./scripts/mcp_service.sh restart
./scripts/mcp_service.sh stop
```

脚本使用 `nohup` 在后台运行，默认：

- 配置文件：`config/oss_config.json`
- 监听地址：`127.0.0.1:8000`
- 传输方式：`streamable-http`
- 端点：`/mcp`

`start` / `restart` 前会按以下顺序准备运行环境，并逐个验证是否为 Python 3.10+ 且能导入 `oss2` / `mcp` / `uvicorn`：

1. `PYTHON_BIN`：严格模式，必须可用，不会自动创建环境、升级 pip 或安装依赖，也不会回退；
2. 项目虚拟环境：Windows 为 `.venv\Scripts\python.exe`、`venv\Scripts\python.exe`，其他系统为 `.venv/bin/python`、`venv/bin/python`；
3. 找不到可用项目虚拟环境时，若允许自动安装，用 PATH 中的 Python 创建 `.venv`（Windows 优先 `python`，避免应用商店的 `python3` 占位程序）；
4. 虚拟环境缺依赖时，先执行 `pip install --upgrade pip`，再执行 `pip install -r requirements.txt`，然后重试；
5. 项目虚拟环境仍不可用时，回退到 PATH 中依赖齐全的 Python。

健康环境会直接使用，不会重复升级 pip 或安装依赖；pip 升级失败时只提示并继续安装依赖，不会直接中断启动。设置 `OSS_MCP_AUTO_INSTALL=0` 可关闭自动创建 `.venv`、升级 pip 和安装依赖。环境准备完成后会检查服务脚本、配置文件和端口占用，并最多等待 20 秒确认端口就绪。

### Windows

双击或在 CMD 中执行：

```bat
scripts\mcp_service.bat start
scripts\mcp_service.bat stop
scripts\mcp_service.bat restart
scripts\mcp_service.bat status
scripts\mcp_service.bat logs
```

直接双击 `.bat` 时默认执行 `start`，结束后会暂停窗口以便查看结果。

也可以直接调用 PowerShell 脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\mcp_service.ps1 start
powershell -ExecutionPolicy Bypass -File .\scripts\mcp_service.ps1 status
```

Windows 脚本使用隐藏窗口的 `Start-Process` 托管进程，并设置 `PYTHONIOENCODING=utf-8` 避免日志乱码。每次启动会把旧的 `mcp.log`、`mcp.out.log` 轮转为 `.prev`，避免日志无限增长。虚拟环境创建、pip 升级和依赖安装流程与 Linux 脚本一致，新建的虚拟环境同样位于项目根目录的 `.venv`。

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PYTHON_BIN` | 自动探测 | 严格指定 Python 解释器（绝对路径或命令名，如 `python`）；必须为 3.10+ 且已安装依赖，否则直接报错，不会创建虚拟环境或安装依赖 |
| `OSS_MCP_AUTO_INSTALL` | `1` | 自动创建 `.venv`、升级 pip 并安装 `requirements.txt`；设为 `0` 时只使用现有可用环境，不创建、不升级、不安装 |
| `OSS_MCP_CONFIG` | `config/oss_config.json` | 配置文件路径 |
| `OSS_MCP_HOST` | `127.0.0.1` | 监听地址 |
| `OSS_MCP_PORT` | `8000` | 监听端口 |
| `OSS_MCP_TRANSPORT` | `streamable-http` | `streamable-http` 或 `sse` |
| `OSS_MCP_BEARER_TOKEN` | 无 | HTTP/SSE 的 Bearer Token；监听非本机地址时必填，会继承给服务进程 |

示例：

```bash
OSS_MCP_PORT=8010 ./scripts/mcp_service.sh restart
```

```powershell
$env:OSS_MCP_PORT = "8010"
.\scripts\mcp_service.ps1 restart
```

> 修改 `OSS_MCP_HOST` 前请阅读[HTTP Bearer Token 认证](#http-bearer-token-认证)和[安全建议](#安全建议)。监听非本机地址必须配置 Token，且应放在 TLS 反向代理之后。

### 运行时文件

| 路径 | 说明 |
| --- | --- |
| `logs/mcp.log` | Linux：stdout + stderr；Windows：stderr / 应用日志 |
| `logs/mcp.out.log` | Windows：stdout |
| `logs/*.prev` | Windows：上一次启动的日志备份 |
| `run/mcp.pid` | 当前服务进程 PID |

这些路径都已加入 `.gitignore`。

## 命令行工具

`src/oss_utils.py` 可以脱离 MCP 单独使用，适合脚本、定时任务和临时排查。认证参数是全局参数，必须放在子命令之前。

### 认证参数

| 参数 | 环境变量 | 说明 |
| --- | --- | --- |
| `--access-key-id` | `OSS_ACCESS_KEY_ID` | AccessKey ID |
| `--access-key-secret` | `OSS_ACCESS_KEY_SECRET` | AccessKey Secret |
| `--security-token` | `OSS_SECURITY_TOKEN` | STS Token（可选） |
| `--endpoint` | `OSS_ENDPOINT` | 如 `oss-cn-hangzhou.aliyuncs.com` |
| `--bucket` | `OSS_BUCKET` | Bucket 名称 |

### 子命令

| 子命令 | 用途 | 关键选项 |
| --- | --- | --- |
| `upload` | 上传本地文件或网络文件 | `--file` / `--url`、`--key`、`--acl` |
| `replace` | 用本地文件替换已有对象 | `--file`、`--key`、`--use-local-name`、`--acl` |
| `download` | 下载单文件、多文件或目录 | `--key`（可重复）、`--prefix`、`-o`、`-d`、`--zip-name` |
| `delete` | 删除一个或多个文件 | `--key`（可重复） |
| `url` | 生成访问地址 | `--path`、`--prefix`、`--expires`、`--public` |

### 常用示例

```bash
# 上传本地文件并设为公共读
python src/oss_utils.py \
  --access-key-id LTAI... --access-key-secret ... \
  --endpoint oss-cn-hangzhou.aliyuncs.com --bucket demo-bucket \
  upload --file ./a.png --key lstd/pay/a.png --acl public-read

# 从 URL 上传
python src/oss_utils.py <认证参数> \
  upload --url https://example.com/logo.png --key lstd/pay/logo.png

# 替换已有文件，保留原对象路径（URL 不变）
python src/oss_utils.py <认证参数> \
  replace --file ./new-logo.png --key lstd/pay/logo.png

# 替换并按本地文件名改名，成功后删除旧对象
python src/oss_utils.py <认证参数> \
  replace --file ./new-logo.png --key lstd/pay/logo.png --use-local-name

# 下载单个文件到目录
python src/oss_utils.py <认证参数> \
  download --key lstd/pay/a.png -o ./downloads/

# 下载多个文件并打包
python src/oss_utils.py <认证参数> \
  download --key lstd/pay/a.png --key lstd/pay/b.png \
  -d ./downloads --zip-name batch.zip

# 下载整个目录（自动打包，ZIP 内保留相对路径）
python src/oss_utils.py <认证参数> \
  download --prefix lstd/pay/2026/ -d ./downloads

# 批量删除
python src/oss_utils.py <认证参数> \
  delete --key lstd/pay/a.png --key lstd/pay/b.png

# 获取私有签名 URL
python src/oss_utils.py <认证参数> \
  url --path a.png --prefix lstd/pay/ --expires 600
```

### 下载行为

- 单文件、没有传 `--zip-name`、且对象路径不以 `/` 结尾：直接下载为本地文件。
- 传了多个 `--key`、使用 `--prefix`，或对象路径以 `/` 结尾：打包为 ZIP。
- 单文件传了 `--zip-name`：也会打包为 ZIP。
- 多文件 ZIP 内保留**完整 OSS 对象路径**，避免不同目录下的同名文件互相覆盖。
- 目录 ZIP 内保留**相对目录前缀的路径**，例如 `lstd/pay/2026/01/a.txt` 会变成 `01/a.txt`。
- 批量删除会自动去重，并每 1000 个对象提交一次。
- `replace` 默认保留原对象路径；`--use-local-name` 使用本地文件名并删除旧对象。原对象不存在或改名目标已存在时返回 `1`。
- 命令成功返回码为 `0`，OSS 或网络异常返回 `1`，参数错误由 argparse 返回非零退出码。

## 测试

在项目根目录执行：

```bash
python tests/smoke_http_auth.py
python tests/smoke_oss_utils.py
python tests/smoke_mcp_server.py
bash tests/smoke_service_scripts.sh
```

测试覆盖：

- Bearer Token 缺失/错误返回 401，正确 Token 可完成 MCP 会话；
- Token 强度、来源优先级、本机免 Token 和非本机强制 Token 策略；
- 认证日志不会泄露 Token；
- 启动脚本的 Windows / POSIX 虚拟环境路径识别、自动创建 `.venv`、pip 升级与依赖安装顺序、健康环境跳过 pip、`PYTHON_BIN` 严格模式、缺依赖回退，以及 PowerShell / Bash 流程一致性；
- 本地上传、URL 上传、ACL 请求头、非法 ACL 拒绝；
- 替换文件：保留原名、按本地文件名改名、原对象不存在、改名目标重名、删除旧对象失败；
- 单文件下载、多文件 ZIP、目录 ZIP、空目录报错；
- 对象存在性判断（存在 / 不存在 / 空路径）；
- 分组别名路由、默认分组、未知分组报错；
- 批量删除去重与 1000 个一批的分片；
- 公共读直链与私有签名 URL；
- MCP stdio 与 streamable-http 两种传输方式，HTTP 端口动态分配；
- 命令行上传、替换、下载、删除、URL 生成和缺少认证参数的情况。

测试使用内置的假 Bucket 和临时配置，不需要真实 AccessKey，也不会访问真实 OSS。真实环境的权限、网络、签名和 Bucket 策略仍建议上线前用测试 Bucket 联调一次。

## 日志与排错

| 现象 | 处理方式 |
| --- | --- |
| `未找到配置文件` | 复制模板：`cp config/oss_config.example.json config/oss_config.json`，Windows 使用 `Copy-Item` |
| `ModuleNotFoundError: No module named 'oss2' / 'mcp'` | IDE 选中的虚拟环境没有安装依赖；直接使用启动脚本（会检测并安装到项目 venv），或对同一个解释器执行 `python -m pip install -r requirements.txt` |
| `当前 Python 缺少依赖` / `未找到依赖齐全的 Python` | 脚本已尝试创建 `.venv`、升级 pip 并安装 `requirements.txt`，也检查了 PATH；请确认能访问 PyPI，或按提示手动安装后用 `PYTHON_BIN` 指定解释器 |
| 脚本选中了应用商店的 `python3` | 在 Windows 上优先使用 `python`；也可用 `PYTHON_BIN` 指向项目 `venv\Scripts\python.exe` |
| `端口 8000 已被其他进程占用` | 停止占用进程，或设置 `OSS_MCP_PORT=8010` 后重启 |
| 启动后端口 20 秒未就绪 | 执行 `scripts/mcp_service.sh logs`（Windows 使用 `.bat logs`）查看异常 |
| PowerShell 提示禁止运行脚本 | 使用 `powershell -ExecutionPolicy Bypass -File .\scripts\mcp_service.ps1 start` |
| MCP 客户端看不到工具 | 检查客户端配置中的脚本路径、Python 路径和配置文件路径是否为绝对路径；确认依赖安装在同一 Python 环境 |
| HTTP 客户端连接失败 | 确认服务监听地址、端口和端点 `/mcp`；远程访问时检查防火墙和 Host 校验设置 |
| HTTP 返回 `401 Unauthorized` | 客户端没有携带或携带了错误的 `Authorization: Bearer <TOKEN>`；检查 Token 来源优先级和客户端请求头 |
| 监听非本机地址启动失败 | 未配置 Bearer Token；设置 `--bearer-token`、`OSS_MCP_BEARER_TOKEN` 或配置 `http_bearer_token` |
| `Token 长度至少为 16 个字符` / `不能包含空白字符` / `只能包含可打印的 ASCII 字符` | 重新生成合规 Token：`python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| 修改 Token 后客户端仍 401 | 重启服务并更新客户端请求头；环境变量优先级高于配置文件 |
| `AccessDenied` / `NoSuchBucket` | 检查 AccessKey 权限、Bucket 名称、Endpoint 区域是否匹配 |
| `未知的认证分组或别名` | 调用 `list_groups` 查看实际名称；检查多个分组是否存在别名冲突 |
| Windows 日志中文乱码 | 使用脚本启动（已设置 UTF-8）；直接运行时设置 `PYTHONIOENCODING=utf-8` |

## 安全建议

- **不要提交真实配置**：只提交 `config/oss_config.example.json`；`config/oss_config.json` 已被 `.gitignore` 排除，其中可能同时包含 AccessKey 和 Bearer Token。
- **最小权限**：为 MCP 单独创建 RAM 用户或 STS 临时凭证，只授予需要的 Bucket / 前缀权限，避免使用主账号 AccessKey。
- **谨慎开放删除能力**：`delete_file`、`delete_files` 不可恢复；`replace_file` 在改名模式下也会删除旧对象，并可能覆盖原对象内容。建议开启 Bucket 版本控制或定期备份，并在客户端启用人工确认。
- **HTTP 服务优先只监听本机**：`127.0.0.1` 不会暴露到局域网。监听非本机地址时服务强制要求 Bearer Token，但仍应通过反向代理增加 TLS、来源限制和访问日志。
- **妥善保管 Bearer Token**：使用至少 16 位可打印 ASCII 的随机值，优先放在环境变量或已被忽略的配置文件中；怀疑泄露时立即轮换 Token 并重启服务。
- **令牌不是用户体系**：当前 Bearer Token 是所有客户端共享的静态令牌，只表示“持有令牌”，不提供用户身份、权限分级或审计归属。
- **避免公共读写**：除非确有需要，不要使用 `public-read-write`；公共读文件应确认不包含敏感信息。
- **及时轮换凭证**：一旦发现泄露，立即在阿里云控制台禁用并轮换 AccessKey。
- **保护日志**：日志可能包含对象路径和错误详情，分享前先检查并脱敏；服务不会主动记录 Bearer Token。

## 已知限制

- 目录下载当前按对象顺序串行下载，超大目录耗时取决于网络和对象数量。
- URL 上传会先把远程内容读入内存，超大文件建议先落盘再使用 `upload_file`。
- 不提供断点续传、分片上传进度展示或下载限速。
- `replace_file` 改名模式由“上传新对象 + 删除旧对象”两步完成，不是跨对象原子操作；删除失败时新旧对象会同时存在，并返回明确错误。
- `replace_file` 不传 `acl` 时，新对象继承 Bucket ACL，不会自动沿用被替换对象的 ACL；替换公共读文件时请显式传 `acl=public-read`。
- HTTP 模式使用单个静态 Bearer Token，不区分用户、不提供按工具授权、用量限流或 Token 自动轮换。
- 启动脚本自动创建虚拟环境和安装依赖需要可访问 PyPI；Debian / Ubuntu 的系统 Python 还需要 `python3-venv`（`sudo apt install python3-venv`）。离线环境请先手动安装 `requirements.txt`，或设置 `OSS_MCP_AUTO_INSTALL=0` 后自行管理环境。
- `file_exists` 只做精确对象匹配；判断“目录是否存在”需要按前缀列出对象，本工具不提供该语义。
- 冒烟测试使用假 Bucket，不能替代真实 OSS 的权限和网络联调。
