"""HTTP 模式下的 Bearer Token 认证组件。"""

import hmac

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
MIN_TOKEN_LENGTH = 16


def is_local_host(host):
    """判断监听地址是否仅限本机访问。"""
    return str(host or "").strip().lower() in LOCAL_HOSTS


def validate_bearer_token(token):
    """校验并规范化 Bearer Token，返回去除首尾空白后的值。"""
    if not isinstance(token, str):
        raise ValueError("HTTP Bearer Token 必须是字符串")
    token = token.strip()
    if not token:
        raise ValueError("HTTP Bearer Token 不能为空")
    if any(char.isspace() for char in token):
        raise ValueError("HTTP Bearer Token 不能包含空白字符")
    if not (token.isascii() and token.isprintable()):
        raise ValueError("HTTP Bearer Token 只能包含可打印的 ASCII 字符")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"HTTP Bearer Token 长度至少为 {MIN_TOKEN_LENGTH} 个字符")
    return token


def prepare_http_auth(host, token):
    """校验 HTTP 认证配置：本机允许无 Token，非本机必须配置 Token。"""
    if token is not None:
        if not isinstance(token, str):
            raise ValueError("HTTP Bearer Token 必须是字符串")
        if token.strip():
            return validate_bearer_token(token)
    if is_local_host(host):
        return None
    raise ValueError(
        f"HTTP/SSE 模式监听非本机地址 {host} 时必须配置 HTTP Bearer Token："
        "请使用 --bearer-token、环境变量 OSS_MCP_BEARER_TOKEN "
        "或配置文件根节点的 http_bearer_token"
    )


def resolve_http_auth(host, cli_token=None, env_token=None, config_token=None):
    """按 CLI > 环境变量 > 配置文件的顺序解析 HTTP Bearer Token。"""
    token = next(
        (value for value in (cli_token, env_token, config_token) if value and str(value).strip()),
        None,
    )
    return prepare_http_auth(host, token)


class BearerTokenMiddleware:
    """为 ASGI 应用增加 Bearer Token 校验。"""

    def __init__(self, app, token, realm="oss-mcp"):
        self.app = app
        self.token = validate_bearer_token(token)
        self.realm = realm

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        authorization = ""
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                authorization = value.decode("latin-1")
                break

        parts = authorization.split(None, 1)
        supplied = parts[1].strip() if len(parts) == 2 else ""
        if (
            len(parts) == 2
            and parts[0].lower() == "bearer"
            and supplied
            and hmac.compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8"))
        ):
            await self.app(scope, receive, send)
            return

        body = b'{"error":"unauthorized","message":"Missing or invalid Bearer token"}'
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"www-authenticate", f'Bearer realm="{self.realm}"'.encode("utf-8")),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": body})
