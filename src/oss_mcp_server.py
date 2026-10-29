"""阿里云 OSS MCP 服务（基于官方 MCP Python SDK）

支持多组 AccessKey 认证信息，通过 JSON 配置文件管理，例如 config/oss_config.json：
{
  "default_group": "prod",
  "groups": {
    "prod": {
      "alias": "海科",
      "access_key_id": "LTAI...",
      "access_key_secret": "...",
      "endpoint": "oss-cn-hangzhou.aliyuncs.com",
      "bucket": "prod-bucket",
      "domain": "https://static.example.com",
      "default_prefix": "lstd/pay/",
      "public_read": true
    },
    "test": {
      "alias": "测试",
      "aliases": ["测试OSS"],
      "access_key_id": "LTAI...",
      "access_key_secret": "...",
      "security_token": "STS token（可选）",
      "endpoint": "oss-cn-shanghai.aliyuncs.com",
      "bucket": "test-bucket"
    }
  }
}

启动方式（在项目根目录执行）：
    # 多分组（默认读取 config/oss_config.json）
    python src/oss_mcp_server.py
    python src/oss_mcp_server.py --config ./config/oss_config.json --default-group prod

    # 单分组（兼容旧用法）
    python src/oss_mcp_server.py --access-key-id AK --access-key-secret SK \
        --endpoint oss-cn-hangzhou.aliyuncs.com --bucket demo

    # HTTP 模式（端点 /mcp）
    python src/oss_mcp_server.py --config ./config/oss_config.json --transport streamable-http --port 8000

单分组参数也可通过环境变量 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET /
OSS_SECURITY_TOKEN / OSS_ENDPOINT / OSS_BUCKET / OSS_DOMAIN / OSS_DEFAULT_PREFIX 提供。
"""
import argparse
import json
import os
import sys
from typing import List, Optional

import oss2
import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import oss_utils

DEFAULT_CONFIG_NAME = "oss_config.json"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", DEFAULT_CONFIG_NAME)

mcp = FastMCP(
    "oss-utils",
    instructions=(
        "阿里云 OSS 工具，支持多组 AccessKey 认证配置。调用工具时可通过 group 参数指定认证分组，"
        "不传则使用默认分组；可用 list_groups 查看已配置的分组（不会返回 Secret）。"
        "支持：判断文件是否存在；上传本地文件或网络文件（可指定对象 ACL）；下载单文件、多文件或整个目录"
        "（多文件/目录自动打包为 zip）；删除单个或批量删除文件；获取文件访问地址。"
    ),
)

_OSS_ERRORS = (oss2.exceptions.OssError, requests.RequestException, ValueError, OSError)
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# 分组名 -> {"config": dict, "bucket": oss2.Bucket}
_groups = {}
_group_lookup = {}
_default_group = None


def _pick(raw, *keys):
    """按顺序取第一个非空字段，兼容 snake_case / camelCase 写法"""
    for key in keys:
        value = raw.get(key)
        if value:
            return value
    return None


def _normalize_name(value):
    return "".join(str(value).split()).casefold()


def _name_variants(value):
    """生成名称本身及常见 OSS 后缀变体，便于“海科”和“海科OSS”互相匹配"""
    name = _normalize_name(value)
    if not name:
        return set()

    suffixes = ("oss", "云存储")
    variants = {name}
    stripped = False
    for suffix in suffixes:
        if name.endswith(suffix) and len(name) > len(suffix):
            variants.add(name[:-len(suffix)])
            stripped = True
    if not stripped:
        for suffix in suffixes:
            variants.add(name + suffix)
    return variants


def _collect_aliases(raw):
    primary = _pick(raw, "alias")
    extra = raw.get("aliases", [])
    if isinstance(extra, str):
        extra = [extra]
    if extra is None:
        extra = []
    if not isinstance(extra, list):
        raise ValueError("组字段 aliases 必须是数组")

    aliases = []
    seen = set()
    for value in ([primary] if primary else []) + extra:
        if not isinstance(value, str):
            raise ValueError("组字段 alias/aliases 必须是字符串")
        value = value.strip()
        normalized = _normalize_name(value)
        if value and normalized not in seen:
            seen.add(normalized)
            aliases.append(value)
    return aliases


def _build_group(name, raw):
    if not isinstance(raw, dict):
        raise ValueError(f"分组 {name} 的配置必须是 JSON 对象")

    access_key_id = _pick(raw, "access_key_id", "accessKeyId", "accessKey")
    access_key_secret = _pick(raw, "access_key_secret", "accessKeySecret", "accessSecret")
    endpoint = _pick(raw, "endpoint")
    bucket_name = _pick(raw, "bucket", "bucket_name", "bucketName")
    domain = _pick(raw, "domain")
    aliases = _collect_aliases(raw)

    missing = [key for key, value in (
        ("access_key_id / accessKey", access_key_id),
        ("access_key_secret / accessSecret", access_key_secret),
        ("endpoint", endpoint),
        ("bucket", bucket_name),
    ) if not value]
    if missing:
        raise ValueError(f"分组 {name} 缺少字段：{', '.join(missing)}")

    config = {
        "name": name,
        "alias": aliases[0] if aliases else None,
        "aliases": aliases,
        "access_key_id": access_key_id,
        "access_key_secret": access_key_secret,
        "security_token": _pick(raw, "security_token", "securityToken"),
        "endpoint": endpoint,
        "bucket": bucket_name,
        "domain": _normalize_domain(domain) if domain else None,
        "prefix": _pick(raw, "default_prefix", "defaultPrefix", "prefix") or "lstd/pay/",
        "public_read": bool(raw.get("public_read", raw.get("publicRead", False))),
    }
    bucket = oss_utils.createBucketClient({
        "access_key_id": config["access_key_id"],
        "access_key_secret": config["access_key_secret"],
        "security_token": config["security_token"],
        "endpoint": config["endpoint"],
        "bucket_name": config["bucket"],
    })
    return {"config": config, "bucket": bucket}


def _load_config_file(path):
    with open(path, "r", encoding="utf-8-sig") as file_obj:
        data = json.load(file_obj)

    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")

    raw_groups = data.get("groups")
    if raw_groups is None and _pick(data, "access_key_id", "accessKeyId", "accessKey"):
        # 兼容只配置一组凭证的扁平写法
        raw_groups = {"default": data}
    if not isinstance(raw_groups, dict) or not raw_groups:
        raise ValueError("配置文件缺少非空的 groups 对象")

    groups = {name: _build_group(name, raw) for name, raw in raw_groups.items()}
    return groups, data.get("default_group") or data.get("default")


def _group_help_text():
    parts = []
    for name, entry in _groups.items():
        config = entry["config"]
        aliases = "、".join(config["aliases"]) or "无"
        default_tag = "，默认" if name == _default_group else ""
        parts.append(f"{name}（别名：{aliases}{default_tag}）")
    return "可用分组：" + "；".join(parts) + "。group 可传分组名或别名。"


def _refresh_tool_help():
    """把分组别名写入工具描述和 group 参数说明，便于模型直接按别名选组"""
    help_text = _group_help_text()
    tool_manager = getattr(mcp, "_tool_manager", None)
    if tool_manager is None:
        return

    for tool in tool_manager.list_tools():
        properties = tool.parameters.get("properties", {})
        if "group" not in properties:
            continue
        properties["group"]["description"] = (
            f"认证分组名称或别名，不传则使用默认分组。{help_text}"
        )
        base_description = tool.description.split("\n\n可用分组：", 1)[0]
        tool.description = f"{base_description}\n\n{help_text}"


def _build_instructions():
    lines = [
        "阿里云 OSS 工具，支持多组 AccessKey 认证配置。",
        "调用工具时通过 group 参数指定认证分组，可传分组名称或别名，不传则使用默认分组。",
        "用户用「从 XX OSS 下载/上传/删除…」等自然语言指定来源时，把 XX 解析为对应别名的分组后再调用工具。",
        "可用分组：",
    ]
    for name, entry in _groups.items():
        config = entry["config"]
        aliases = "、".join(config["aliases"]) if config["aliases"] else "无"
        default_tag = "（默认）" if name == _default_group else ""
        lines.append(f"- {name}{default_tag}：别名 {aliases}，bucket {config['bucket']}")
    lines.append("可调用 list_groups 查看完整分组信息（不会返回 Secret）。")
    return "\n".join(lines)


def _apply_groups(groups, default_group=None):
    global _groups, _group_lookup, _default_group

    if not groups:
        raise ValueError("至少需要配置一个认证分组")
    if not default_group:
        default_group = next(iter(groups))
    if default_group not in groups:
        raise ValueError(f"默认分组 {default_group} 不存在，可选分组：{', '.join(groups)}")

    lookup = {}
    for name, entry in groups.items():
        config = entry["config"]
        for display_name in [name] + config["aliases"]:
            for key in _name_variants(display_name):
                owner = lookup.get(key)
                if owner and owner != name:
                    raise ValueError(
                        f"认证分组名称/别名冲突：'{display_name}' 同时匹配分组 {owner} 和 {name}，请调整 alias 配置"
                    )
                lookup[key] = name

    _groups = groups
    _group_lookup = lookup
    _default_group = default_group
    # 把分组别名写入初始化信息，客户端连接后即可看到
    mcp._mcp_server.instructions = _build_instructions()
    _refresh_tool_help()


def _resolve_group(group=None):
    if not _groups:
        raise ToolError("尚未配置任何认证分组")

    requested = group or _default_group
    name = _group_lookup.get(_normalize_name(requested))
    if name is None:
        options = "；".join(
            f"{group_name}（别名：{'、'.join(entry['config']['aliases']) or '无'}）"
            for group_name, entry in _groups.items()
        )
        raise ToolError(f"未知的认证分组或别名：{requested}，可用分组：{options}")
    entry = _groups[name]
    return entry["config"], entry["bucket"]


def _execute(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except _OSS_ERRORS as exc:
        raise ToolError(f"OSS 操作失败：{exc}") from exc


def _mask_access_key(value):
    if not value:
        return ""
    if len(value) <= 8:
        return f"{value[:2]}***"
    return f"{value[:4]}***{value[-2:]}"


def _normalize_domain(domain):
    domain = domain.strip().rstrip("/")
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    return domain


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def list_groups() -> dict:
    """列出已配置的 OSS 认证分组（不返回 AccessKey Secret 和 STS Token）。"""
    groups = []
    for name, entry in _groups.items():
        config = entry["config"]
        groups.append({
            "name": name,
            "alias": config["alias"],
            "aliases": config["aliases"],
            "default": name == _default_group,
            "bucket": config["bucket"],
            "endpoint": config["endpoint"],
            "domain": config.get("domain"),
            "default_prefix": config["prefix"],
            "public_read": config["public_read"],
            "uses_sts": bool(config.get("security_token")),
            "access_key_id": _mask_access_key(config["access_key_id"]),
        })
    return {"default_group": _default_group, "groups": groups}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def file_exists(object_name: str, group: Optional[str] = None) -> dict:
    """判断 OSS 上的文件（对象）是否存在，仅精确匹配传入的对象路径。

    :param object_name: OSS 对象路径，如 lstd/pay/a.png
    :param group: 认证分组名称或别名（可用 list_groups 查看），不传则使用默认分组
    """
    config, bucket = _resolve_group(group)
    exists = _execute(oss_utils.objectExists, object_name, bucket=bucket)
    return {
        "group": config["name"],
        "group_alias": config["alias"],
        "bucket": config["bucket"],
        "object_name": object_name,
        "exists": exists,
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def upload_file(
    local_path: str,
    object_name: str,
    acl: Optional[str] = None,
    group: Optional[str] = None,
) -> dict:
    """上传本地文件到 OSS。

    :param local_path: 本地文件路径（绝对路径或相对当前工作目录）
    :param object_name: OSS 对象路径，如 lstd/pay/a.png
    :param acl: 对象 ACL：default/private/public-read/public-read-write，不传则继承 Bucket
    :param group: 认证分组名称或别名（可用 list_groups 查看），不传则使用默认分组
    """
    config, bucket = _resolve_group(group)
    _execute(oss_utils.uploadLocalFile, local_path, object_name, acl=acl, bucket=bucket)
    result = {
        "group": config["name"],
        "group_alias": config["alias"],
        "bucket": config["bucket"],
        "object_name": object_name,
        "acl": acl or "default",
    }
    if os.path.isfile(local_path):
        result["size"] = os.path.getsize(local_path)
    return result


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def upload_from_url(
    url: str,
    object_name: str,
    acl: Optional[str] = None,
    group: Optional[str] = None,
) -> dict:
    """从网络地址下载文件并上传到 OSS。

    :param url: 源文件网络地址
    :param object_name: OSS 对象路径，如 lstd/pay/a.png
    :param acl: 对象 ACL：default/private/public-read/public-read-write，不传则继承 Bucket
    :param group: 认证分组名称或别名（可用 list_groups 查看），不传则使用默认分组
    """
    config, bucket = _resolve_group(group)
    _execute(oss_utils.uploadFile, url, object_name, acl=acl, bucket=bucket)
    return {
        "group": config["name"],
        "group_alias": config["alias"],
        "bucket": config["bucket"],
        "object_name": object_name,
        "acl": acl or "default",
        "source_url": url,
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def download_file(
    object_name: str,
    local_path: Optional[str] = None,
    group: Optional[str] = None,
) -> dict:
    """下载单个文件到本地。

    :param object_name: OSS 对象路径，如 lstd/pay/a.png
    :param local_path: 本地保存路径或目录；不传时保存为当前目录下的文件名
    :param group: 认证分组名称或别名（可用 list_groups 查看），不传则使用默认分组
    """
    config, bucket = _resolve_group(group)
    path = _execute(oss_utils.downloadFile, object_name, local_path, bucket=bucket)
    return {
        "group": config["name"],
        "group_alias": config["alias"],
        "bucket": config["bucket"],
        "object_name": object_name,
        "local_path": os.path.abspath(path),
        "size": os.path.getsize(path),
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def download_files(
    object_names: List[str],
    local_dir: str = ".",
    zip_name: Optional[str] = None,
    group: Optional[str] = None,
) -> dict:
    """下载多个文件并打包为 zip，压缩包内保留完整 OSS 对象路径。

    :param object_names: OSS 对象路径列表
    :param local_dir: zip 文件存放目录，默认当前目录
    :param zip_name: zip 文件名或路径，不传时自动生成
    :param group: 认证分组名称或别名（可用 list_groups 查看），不传则使用默认分组
    """
    config, bucket = _resolve_group(group)
    path = _execute(oss_utils.downloadFiles, object_names, local_dir=local_dir, zip_name=zip_name, bucket=bucket)
    return {
        "group": config["name"],
        "group_alias": config["alias"],
        "bucket": config["bucket"],
        "zip_path": os.path.abspath(path),
        "file_count": len({name for name in object_names if name}),
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def download_folder(
    prefix: str,
    local_dir: str = ".",
    zip_name: Optional[str] = None,
    group: Optional[str] = None,
) -> dict:
    """下载整个目录（按 OSS 前缀匹配）并打包为 zip，压缩包内保留目录内相对路径。

    :param prefix: OSS 目录前缀，如 lstd/pay/2026/
    :param local_dir: zip 文件存放目录，默认当前目录
    :param zip_name: zip 文件名或路径，不传时自动生成
    :param group: 认证分组名称或别名（可用 list_groups 查看），不传则使用默认分组
    """
    config, bucket = _resolve_group(group)
    path = _execute(oss_utils.downloadFolder, prefix, local_dir=local_dir, zip_name=zip_name, bucket=bucket)
    return {
        "group": config["name"],
        "group_alias": config["alias"],
        "bucket": config["bucket"],
        "zip_path": os.path.abspath(path),
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True),
)
def delete_file(object_name: str, group: Optional[str] = None) -> dict:
    """删除单个 OSS 文件（不可恢复，请谨慎调用）。

    :param object_name: OSS 对象路径，如 lstd/pay/a.png
    :param group: 认证分组名称或别名（可用 list_groups 查看），不传则使用默认分组
    """
    config, bucket = _resolve_group(group)
    _execute(oss_utils.deleteFile, object_name, bucket=bucket)
    return {"group": config["name"], "group_alias": config["alias"], "bucket": config["bucket"], "deleted": [object_name], "count": 1}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True),
)
def delete_files(object_names: List[str], group: Optional[str] = None) -> dict:
    """批量删除 OSS 文件（不可恢复，请谨慎调用），自动按 1000 个一批提交。

    :param object_names: OSS 对象路径列表
    :param group: 认证分组名称或别名（可用 list_groups 查看），不传则使用默认分组
    """
    config, bucket = _resolve_group(group)
    deleted = _execute(oss_utils.deleteFiles, object_names, bucket=bucket)
    return {"group": config["name"], "group_alias": config["alias"], "bucket": config["bucket"], "deleted": deleted, "count": len(deleted)}


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def get_file_url(
    relative_path: str,
    prefix: Optional[str] = None,
    expires: int = 3600,
    public: Optional[bool] = None,
    group: Optional[str] = None,
) -> dict:
    """获取 OSS 文件的可访问地址。

    :param relative_path: 相对路径，如 user/avatar.jpg
    :param prefix: OSS 存储目录前缀，不传则使用分组配置的 default_prefix（默认 lstd/pay/）
    :param expires: 私有 Bucket 签名 URL 有效期（秒），默认 3600
    :param public: 是否公共读；不传则使用分组配置的 public_read。
                   公共读且分组配置了 domain 时优先使用 domain 拼接地址
    :param group: 认证分组名称或别名（可用 list_groups 查看），不传则使用默认分组
    """
    config, bucket = _resolve_group(group)
    url_prefix = prefix or config["prefix"]
    is_public = config["public_read"] if public is None else public

    if is_public and config.get("domain"):
        full_path = f"{url_prefix.rstrip('/')}/{relative_path.lstrip('/')}"
        url = f"{config['domain']}/{full_path}"
    else:
        url = _execute(
            oss_utils.get_oss_image_url,
            relative_path,
            prefix=url_prefix,
            expires=expires,
            is_public_bucket=is_public,
            bucket=bucket,
        )

    return {"group": config["name"], "group_alias": config["alias"], "bucket": config["bucket"], "url": url, "prefix": url_prefix}


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="oss_mcp_server.py",
        description="以 MCP 服务方式对外提供阿里云 OSS 文件存在性判断、上传、下载、删除、获取地址能力（支持多分组认证）",
    )
    parser.add_argument("--config", help=f"分组配置文件路径；不传认证参数时默认读取 config/{DEFAULT_CONFIG_NAME}")
    parser.add_argument("--default-group", help="默认认证分组名称，覆盖配置文件中的 default_group")
    parser.add_argument("--access-key-id", default=os.environ.get("OSS_ACCESS_KEY_ID"),
                        help="单分组模式：OSS AccessKey ID")
    parser.add_argument("--access-key-secret", default=os.environ.get("OSS_ACCESS_KEY_SECRET"),
                        help="单分组模式：OSS AccessKey Secret")
    parser.add_argument("--security-token", default=os.environ.get("OSS_SECURITY_TOKEN"),
                        help="单分组模式：STS 临时凭证 Token（可选）")
    parser.add_argument("--endpoint", default=os.environ.get("OSS_ENDPOINT"),
                        help="单分组模式：OSS Endpoint")
    parser.add_argument("--bucket", default=os.environ.get("OSS_BUCKET"),
                        help="单分组模式：Bucket 名称")
    parser.add_argument("--domain", default=os.environ.get("OSS_DOMAIN"),
                        help="单分组模式：文件访问域名（可选，公共读时优先使用）")
    parser.add_argument("--default-prefix", default=os.environ.get("OSS_DEFAULT_PREFIX", "lstd/pay/"),
                        help="单分组模式：生成 URL 时的默认前缀，默认 lstd/pay/")
    parser.add_argument("--public-read", action="store_true",
                        default=os.environ.get("OSS_PUBLIC_READ", "").lower() in ("1", "true", "yes"),
                        help="单分组模式：Bucket 是否公共读")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
                        help="传输方式：stdio（默认，本地客户端拉起）或 HTTP 模式")
    parser.add_argument("--host", default=os.environ.get("OSS_MCP_HOST", "127.0.0.1"),
                        help="HTTP 模式监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OSS_MCP_PORT", "8000")),
                        help="HTTP 模式监听端口，默认 8000")
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.config:
            groups, default_group = _load_config_file(args.config)
            _apply_groups(groups, args.default_group or default_group)
        elif any((args.access_key_id, args.access_key_secret, args.endpoint, args.bucket)):
            missing = [name for name, value in (
                ("--access-key-id", args.access_key_id),
                ("--access-key-secret", args.access_key_secret),
                ("--endpoint", args.endpoint),
                ("--bucket", args.bucket),
            ) if not value]
            if missing:
                parser.error(f"单分组模式缺少参数：{', '.join(missing)}")
            if args.default_group and args.default_group != "default":
                parser.error("单分组模式的认证分组名称固定为 default，不能通过 --default-group 指定其他名称")
            raw = {
                "access_key_id": args.access_key_id,
                "access_key_secret": args.access_key_secret,
                "security_token": args.security_token,
                "endpoint": args.endpoint,
                "bucket": args.bucket,
                "domain": args.domain,
                "default_prefix": args.default_prefix,
                "public_read": args.public_read,
            }
            _apply_groups({"default": _build_group("default", raw)}, "default")
        elif os.path.isfile(DEFAULT_CONFIG_PATH):
            groups, default_group = _load_config_file(DEFAULT_CONFIG_PATH)
            _apply_groups(groups, args.default_group or default_group)
        else:
            parser.error(
                f"缺少认证信息：请用 --config 指定分组配置文件，或传入 --access-key-id 等单分组参数"
                f"（也可将 {DEFAULT_CONFIG_NAME} 放在项目的 config 目录）"
            )
    except (OSError, ValueError) as exc:
        parser.error(f"加载认证配置失败：{exc}")

    # 关闭库函数提示，stdio 模式下 stdout 只能用于 MCP 协议消息
    oss_utils.setQuiet(True)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return 0

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    if args.host not in _LOCAL_HOSTS:
        # 非本机监听时放开 Host 校验，否则远程客户端会被 DNS 重绑定保护拦截
        mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        print(
            "警告：服务监听在非本机地址且没有内置鉴权，任何能访问该端口的人都能操作 OSS，请确保网络访问受控。",
            file=sys.stderr,
        )

    path = "/mcp" if args.transport == "streamable-http" else "/sse"
    print(f"OSS MCP 服务已启动：http://{args.host}:{args.port}{path}（transport={args.transport}）", file=sys.stderr)
    mcp.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
