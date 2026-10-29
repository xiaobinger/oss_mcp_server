import asyncio
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from unittest import mock

TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, TOOLS_DIR)
import oss_mcp_server as server
import oss_utils


class FakeObject:
    def __init__(self, data):
        self._data = io.BytesIO(data)

    def read(self, n=-1):
        return self._data.read(n)

    def close(self):
        pass


class FakeBatchResult:
    def __init__(self, keys):
        self.deleted_keys = keys


class FakeBucket:
    def __init__(self, bucket_name, endpoint):
        self.bucket_name = bucket_name
        self.endpoint = endpoint
        self.objects = {}
        self.put_headers = []
        self.batch_sizes = []

    def put_object(self, key, data, headers=None, progress_callback=None):
        if hasattr(data, "read"):
            data = data.read()
        if isinstance(data, str):
            data = data.encode()
        self.objects[key] = data
        self.put_headers.append(headers)
        return "ok"

    def get_object(self, key):
        return FakeObject(self.objects[key])

    def get_object_to_file(self, key, filename, **kwargs):
        with open(filename, "wb") as f:
            f.write(self.objects[key])

    def object_exists(self, key):
        return key in self.objects

    def delete_object(self, key, params=None, headers=None):
        self.objects.pop(key, None)

    def batch_delete_objects(self, keys, headers=None):
        self.batch_sizes.append(len(keys))
        for key in keys:
            self.delete_object(key)
        return FakeBatchResult(keys)

    def sign_url(self, method, key, expires, **kwargs):
        return f"https://{self.endpoint}/{self.bucket_name}/{key}?e={expires}"


FAKES = {}


def fake_create_bucket(config):
    bucket = FakeBucket(config["bucket_name"], config["endpoint"])
    FAKES[config["bucket_name"]] = bucket
    return bucket


oss_utils.setQuiet(True)

with socket.socket() as _probe:
    _probe.bind(("127.0.0.1", 0))
    HTTP_PORT = _probe.getsockname()[1]

tmp = tempfile.mkdtemp(prefix="oss_mcp_groups_")
print("tmp:", tmp)
config_path = os.path.join(tmp, "oss_config.json")
with open(config_path, "w", encoding="utf-8") as f:
    json.dump({
        "default_group": "prod",
        "groups": {
            "prod": {
                "alias": "海科",
                "access_key_id": "LTAI_PROD_12345678",
                "access_key_secret": "PROD_SECRET_SHOULD_NOT_LEAK",
                "endpoint": "oss-cn-hangzhou.aliyuncs.com",
                "bucket": "prod-bucket",
                "domain": "https://static.example.com/",
                "default_prefix": "lstd/pay/",
                "public_read": True,
            },
            "test": {
                "alias": "测试",
                "aliases": ["测试OSS"],
                "access_key_id": "LTAI_TEST_87654321",
                "access_key_secret": "TEST_SECRET_SHOULD_NOT_LEAK",
                "security_token": "sts-token-should-not-leak",
                "endpoint": "oss-cn-shanghai.aliyuncs.com",
                "bucket": "test-bucket",
                "default_prefix": "lstd/test/",
                "public_read": False,
            },
        },
    }, f, ensure_ascii=False)

with mock.patch.object(oss_utils, "createBucketClient", side_effect=fake_create_bucket):
    groups, default_group = server._load_config_file(config_path)
    server._apply_groups(groups, default_group)

print("default group:", server._default_group)
assert set(FAKES) == {"prod-bucket", "test-bucket"}, FAKES
assert "海科" in server.mcp.instructions
assert "别名 海科" in server.mcp.instructions and "别名 测试、测试OSS" in server.mcp.instructions


async def call(name, arguments):
    result = await server.mcp.call_tool(name, arguments)
    text = result[0].text if result and hasattr(result[0], "text") else str(result)
    print(name, "->", text.replace("\n", " "))
    return json.loads(text)


async def tool_tests():
    tools = await server.mcp.list_tools()
    names = [t.name for t in tools]
    print("tools:", names)
    assert names == [
        "list_groups", "file_exists", "upload_file", "upload_from_url", "download_file", "download_files",
        "download_folder", "delete_file", "delete_files", "get_file_url",
    ], names
    for t in tools:
        assert t.inputSchema.get("type") == "object"
        if t.name not in ("list_groups",):
            assert "group" in t.inputSchema["properties"], (t.name, t.inputSchema)

    # list_groups 不泄露 secret / token
    groups_result = await call("list_groups", {})
    dumped = json.dumps(groups_result, ensure_ascii=False)
    assert "PROD_SECRET_SHOULD_NOT_LEAK" not in dumped
    assert "TEST_SECRET_SHOULD_NOT_LEAK" not in dumped
    assert "sts-token-should-not-leak" not in dumped
    assert groups_result["default_group"] == "prod"
    prod = next(g for g in groups_result["groups"] if g["name"] == "prod")
    assert prod["alias"] == "海科" and prod["aliases"] == ["海科"]
    assert prod["domain"] == "https://static.example.com"
    assert prod["public_read"] is True
    assert prod["access_key_id"] == "LTAI***78"
    test = next(g for g in groups_result["groups"] if g["name"] == "test")
    assert test["aliases"] == ["测试", "测试OSS"]
    assert test["uses_sts"] is True

    # file_exists：不存在 / 存在 / 别名路由
    result = await call("file_exists", {"object_name": "lstd/pay/nope.txt", "group": "海科"})
    assert result["exists"] is False and result["group"] == "prod", result
    FAKES["test-bucket"].objects["lstd/test/exists.txt"] = b"x"
    result = await call("file_exists", {"object_name": "lstd/test/exists.txt", "group": "测试OSS"})
    assert result["exists"] is True and result["group"] == "test", result

    # 别名路由：海科 / 海科OSS（自动后缀变体）都走 prod 分组
    local = os.path.join(tmp, "alias-up.txt")
    with open(local, "wb") as f:
        f.write(b"alias-upload")
    result = await call("upload_file", {
        "local_path": local, "object_name": "lstd/pay/alias.txt", "group": "海科",
    })
    assert result["group"] == "prod" and result["group_alias"] == "海科"
    assert "lstd/pay/alias.txt" in FAKES["prod-bucket"].objects

    result = await call("download_file", {
        "object_name": "lstd/pay/alias.txt", "local_path": tmp, "group": "海科OSS",
    })
    assert result["group"] == "prod" and result["group_alias"] == "海科"

    result = await call("get_file_url", {"relative_path": "alias.txt", "group": "测试OSS"})
    assert result["group"] == "test" and result["group_alias"] == "测试"

    # 分组路由：test 组写入 test-bucket
    local = os.path.join(tmp, "up.txt")
    with open(local, "wb") as f:
        f.write(b"group-upload")
    result = await call("upload_file", {
        "local_path": local, "object_name": "lstd/test/up.txt", "acl": "public-read", "group": "test",
    })
    assert result["group"] == "test" and result["bucket"] == "test-bucket"
    assert "lstd/test/up.txt" in FAKES["test-bucket"].objects
    assert FAKES["test-bucket"].put_headers[-1] == {"x-oss-object-acl": "public-read"}
    assert "prod-bucket" not in [k for k in FAKES if "lstd/test/up.txt" in FAKES[k].objects] or \
        "lstd/test/up.txt" not in FAKES["prod-bucket"].objects

    # 默认分组：prod
    class FakeResponse:
        content = b"from-url"

        def raise_for_status(self):
            pass

    with mock.patch.object(oss_utils.requests, "get", return_value=FakeResponse()):
        result = await call("upload_from_url", {
            "url": "https://example.com/a.png", "object_name": "lstd/pay/a.png",
        })
    assert result["group"] == "prod"
    assert FAKES["prod-bucket"].objects["lstd/pay/a.png"] == b"from-url"

    # download_file 走 test 组
    FAKES["test-bucket"].objects["lstd/test/dl.txt"] = b"dl"
    result = await call("download_file", {"object_name": "lstd/test/dl.txt", "group": "test", "local_path": tmp})
    assert result["group"] == "test" and result["size"] == 2

    # download_files / folder 走指定分组
    FAKES["prod-bucket"].objects.update({
        "lstd/pay/dir/a.txt": b"a",
        "lstd/pay/dir/sub/b.txt": b"b",
    })

    class FakeObj:
        def __init__(self, key):
            self.key = key

    def fake_iterator(bucket, prefix="", **kwargs):
        for key in sorted(bucket.objects):
            if key.startswith(prefix):
                yield FakeObj(key)

    with mock.patch.object(oss_utils.oss2, "ObjectIterator", fake_iterator):
        folder_result = await call("download_folder", {
            "prefix": "lstd/pay/dir", "local_dir": tmp, "zip_name": "folder.zip", "group": "prod",
        })
    assert folder_result["group"] == "prod"
    with zipfile.ZipFile(folder_result["zip_path"]) as zf:
        assert sorted(zf.namelist()) == ["a.txt", "sub/b.txt"], zf.namelist()

    result = await call("download_files", {
        "object_names": ["lstd/pay/dir/a.txt", "lstd/pay/dir/sub/b.txt"],
        "local_dir": tmp, "zip_name": "multi.zip", "group": "prod",
    })
    assert result["file_count"] == 2 and os.path.exists(result["zip_path"])

    # get_file_url：prod 公共读 + domain，默认前缀取分组配置
    result = await call("get_file_url", {"relative_path": "user/a.png"})
    assert result["url"] == "https://static.example.com/lstd/pay/user/a.png", result
    assert result["prefix"] == "lstd/pay/"

    # 显式 public=False 时仍生成签名 URL
    result = await call("get_file_url", {"relative_path": "user/a.png", "public": False})
    assert result["url"] == "https://oss-cn-hangzhou.aliyuncs.com/prod-bucket/lstd/pay/user/a.png?e=3600", result

    # test 组：私有 + 自己的默认前缀
    result = await call("get_file_url", {"relative_path": "user/b.png", "group": "test"})
    assert result["url"] == "https://oss-cn-shanghai.aliyuncs.com/test-bucket/lstd/test/user/b.png?e=3600", result

    # 删除分组路由
    await call("delete_file", {"object_name": "lstd/test/up.txt", "group": "test"})
    assert "lstd/test/up.txt" not in FAKES["test-bucket"].objects
    await call("delete_files", {"object_names": ["lstd/pay/dir/a.txt"], "group": "prod"})
    assert "lstd/pay/dir/a.txt" not in FAKES["prod-bucket"].objects

    # 未知分组
    try:
        await server.mcp.call_tool("get_file_url", {"relative_path": "x", "group": "nope"})
        raise AssertionError("unknown group should fail")
    except Exception as exc:
        print("unknown group error:", type(exc).__name__, exc)


asyncio.run(tool_tests())

# main() 加载配置 + 单分组兼容
run_calls = []


def fake_run(transport="stdio", mount_path=None):
    run_calls.append(transport)


with mock.patch.object(server.mcp, "run", side_effect=fake_run), \
     mock.patch.object(oss_utils, "createBucketClient", side_effect=fake_create_bucket):
    rc = server.main(["--config", config_path])
    assert rc == 0 and run_calls == ["stdio"] and server._default_group == "prod"

    rc = server.main(["--config", config_path, "--default-group", "test"])
    assert rc == 0 and server._default_group == "test"

    FAKES.clear()
    rc = server.main([
        "--access-key-id", "AK", "--access-key-secret", "SK",
        "--endpoint", "oss-cn-test.aliyuncs.com", "--bucket", "single-bucket",
    ])
    assert rc == 0 and server._default_group == "default"
    assert server._groups["default"]["config"]["bucket"] == "single-bucket"

# 配置文件非法 / 缺字段
bad_path = os.path.join(tmp, "bad.json")
with open(bad_path, "w", encoding="utf-8") as f:
    json.dump({"groups": {"x": {"endpoint": "e"}}}, f)
with mock.patch.object(server.mcp, "run", side_effect=fake_run):
    try:
        server.main(["--config", bad_path])
        raise AssertionError("bad config should exit")
    except SystemExit as exc:
        print("bad config exit:", exc.code)

# 别名冲突
dup_path = os.path.join(tmp, "dup.json")
with open(dup_path, "w", encoding="utf-8") as f:
    json.dump({"groups": {
        "a": {"alias": "海科", "access_key_id": "AKA", "access_key_secret": "SKA",
              "endpoint": "e", "bucket": "ba"},
        "b": {"alias": "海科", "access_key_id": "AKB", "access_key_secret": "SKB",
              "endpoint": "e", "bucket": "bb"},
    }}, f)
with mock.patch.object(oss_utils, "createBucketClient", side_effect=fake_create_bucket):
    try:
        groups, default_group = server._load_config_file(dup_path)
        server._apply_groups(groups, default_group)
        raise AssertionError("duplicate alias should fail")
    except ValueError as exc:
        print("duplicate alias:", exc)

# stdio 握手（配置文件模式）
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def stdio_test():
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(TOOLS_DIR, "oss_mcp_server.py"), "--config", config_path],
        cwd=TOOLS_DIR,
        encoding="utf-8",
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("instructions:", init.instructions)
            assert "海科" in init.instructions and "测试" in init.instructions
            result = await session.list_tools()
            print("stdio tools:", [t.name for t in result.tools])
            assert len(result.tools) == 10
            listed = await session.call_tool("list_groups", {})
            payload = json.loads(listed.content[0].text)
            assert [g["name"] for g in payload["groups"]] == ["prod", "test"]
            assert payload["default_group"] == "prod"


asyncio.run(stdio_test())

# HTTP 握手（配置文件模式）
http_log_path = os.path.join(tmp, "http_server.log")
http_log = open(http_log_path, "wb")
proc = subprocess.Popen(
    [
        sys.executable, os.path.join(TOOLS_DIR, "oss_mcp_server.py"), "--config", config_path,
        "--transport", "streamable-http", "--port", str(HTTP_PORT),
    ],
    cwd=TOOLS_DIR, stdout=http_log, stderr=subprocess.STDOUT,
)
try:
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/mcp", timeout=0.5):
                break
        except urllib.error.HTTPError:
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise AssertionError("HTTP server did not start")

    from mcp.client.streamable_http import streamable_http_client

    async def http_test():
        async with streamable_http_client(f"http://127.0.0.1:{HTTP_PORT}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                print("http tools:", [t.name for t in result.tools])
                assert len(result.tools) == 10

    asyncio.run(http_test())
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    http_log.close()
    with open(http_log_path, "rb") as f:
        print("server log tail:", f.read()[-300:].decode("utf-8", "replace"))

print("ALL MULTI-GROUP MCP TESTS PASSED")
shutil.rmtree(tmp, ignore_errors=True)
