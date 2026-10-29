import io
import os
import shutil
import sys
import tempfile
import zipfile
from unittest import mock

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC_DIR)
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
    bucket_name = "test-bucket"
    endpoint = "oss-cn-test.aliyuncs.com"

    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.delete_calls = []
        self.batch_sizes = []

    def put_object(self, key, data, headers=None, progress_callback=None):
        if hasattr(data, "read"):
            data = data.read()
        if isinstance(data, str):
            data = data.encode()
        self.objects[key] = data
        self.put_calls.append((key, headers))
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
        self.delete_calls.append(key)

    def batch_delete_objects(self, keys, headers=None):
        self.batch_sizes.append(len(keys))
        for key in keys:
            self.delete_object(key)
        return FakeBatchResult(keys)

    def sign_url(self, method, key, expires, **kwargs):
        return f"https://signed.example.com/{key}?e={expires}"


fake = FakeBucket()
oss_utils.bucketClient = fake

tmp = tempfile.mkdtemp(prefix="oss_test_")
print("tmp:", tmp)

# 1. 本地上传 + ACL
local_file = os.path.join(tmp, "hello.txt")
with open(local_file, "wb") as f:
    f.write(b"hello oss")
oss_utils.uploadLocalFile(local_file, "lstd/pay/hello.txt", acl="public-read")
assert fake.put_calls[-1] == ("lstd/pay/hello.txt", {"x-oss-object-acl": "public-read"}), fake.put_calls[-1]

# 2. 非法 ACL
try:
    oss_utils.uploadLocalFile(local_file, "x.txt", acl="world-writable")
    raise AssertionError("should reject invalid acl")
except ValueError as exc:
    print("invalid acl ok:", exc)

# 3. URL 上传 + ACL 默认继承
class FakeResponse:
    content = b"from-url"

    def raise_for_status(self):
        pass

with mock.patch.object(oss_utils.requests, "get", return_value=FakeResponse()):
    oss_utils.uploadFile("https://example.com/a.png", "lstd/pay/url.png")
assert fake.put_calls[-1] == ("lstd/pay/url.png", None), fake.put_calls[-1]
assert fake.objects["lstd/pay/url.png"] == b"from-url"

# 3.1 判断对象是否存在
assert oss_utils.objectExists("lstd/pay/hello.txt") is True
assert oss_utils.objectExists("lstd/pay/missing.txt") is False
try:
    oss_utils.objectExists("")
    raise AssertionError("empty object name should fail")
except ValueError as exc:
    print("empty object ok:", exc)

# 4. 单文件下载到目录
out_dir = os.path.join(tmp, "out")
os.makedirs(out_dir)
path = oss_utils.downloadFile("lstd/pay/hello.txt", out_dir)
assert path == os.path.join(out_dir, "hello.txt")
assert open(path, "rb").read() == b"hello oss"

# 5. 多文件下载 -> zip，保留完整 key
zip_path = oss_utils.downloadFiles(["lstd/pay/hello.txt", "lstd/pay/url.png"], local_dir=out_dir, zip_name="multi.zip")
assert os.path.isfile(zip_path)
with zipfile.ZipFile(zip_path) as zf:
    names = sorted(zf.namelist())
    assert names == ["lstd/pay/hello.txt", "lstd/pay/url.png"], names
    assert zf.read("lstd/pay/url.png") == b"from-url"
print("multi zip ok:", names)

# 6. 目录下载 -> zip，保留相对路径
fake.objects.update({
    "lstd/pay/2026/01/a.txt": b"a",
    "lstd/pay/2026/01/02/b.txt": b"b",
    "lstd/pay/2026/other.txt": b"c",
    "lstd/pay/2026/": b"",
    "lstd/pay/other/outside.txt": b"no",
})

class FakeObj:
    def __init__(self, key):
        self.key = key

def fake_iterator(bucket, prefix="", **kwargs):
    for key in sorted(bucket.objects):
        if key.startswith(prefix):
            yield FakeObj(key)

with mock.patch.object(oss_utils.oss2, "ObjectIterator", fake_iterator):
    folder_zip = oss_utils.downloadFolder("lstd/pay/2026", local_dir=out_dir, zip_name="folder.zip")
with zipfile.ZipFile(folder_zip) as zf:
    names = sorted(zf.namelist())
    print("folder zip ok:", names)
    assert names == ["01/02/b.txt", "01/a.txt", "other.txt"], names
    assert zf.read("01/02/b.txt") == b"b"

# 7. 空目录 -> 报错且删除空 zip
with mock.patch.object(oss_utils.oss2, "ObjectIterator", fake_iterator):
    try:
        oss_utils.downloadFolder("lstd/pay/none/", local_dir=out_dir, zip_name="empty.zip")
        raise AssertionError("should raise for empty prefix")
    except ValueError as exc:
        print("empty folder ok:", exc)
    assert not os.path.exists(os.path.join(out_dir, "empty.zip"))

# 8. 批量删除分片 + 去重
fake.objects = {f"k/{i}": b"x" for i in range(2500)}
deleted = oss_utils.deleteFiles(list(fake.objects) + ["k/0"])
assert fake.batch_sizes == [1000, 1000, 500], fake.batch_sizes
assert len(deleted) == 2500, len(deleted)

# 9. URL 生成
oss_utils.bucketClient = fake
public_url = oss_utils.get_oss_image_url("a.png", prefix="lstd/pay/", is_public_bucket=True)
assert public_url == "https://oss-cn-test.aliyuncs.com/test-bucket/lstd/pay/a.png", public_url
signed_url = oss_utils.get_oss_image_url("a.png", expires=120)
assert signed_url == "https://signed.example.com/lstd/pay/a.png?e=120", signed_url

# 10. CLI：url 子命令 + 单文件下载 + 批量删除
def run_cli(argv):
    with mock.patch.object(oss_utils, "initOSSClient", lambda cfg: setattr(oss_utils, "bucketClient", fake)):
        return oss_utils.main(argv)

base = ["--access-key-id", "ak", "--access-key-secret", "sk",
        "--endpoint", "oss-cn-test.aliyuncs.com", "--bucket", "test-bucket"]

fake.objects = {"lstd/pay/cli.txt": b"cli"}
rc = run_cli(base + ["url", "--path", "cli.txt"])
assert rc == 0
rc = run_cli(base + ["download", "--key", "lstd/pay/cli.txt", "-o", out_dir])
assert rc == 0 and open(os.path.join(out_dir, "cli.txt"), "rb").read() == b"cli"
rc = run_cli(base + ["download", "--key", "lstd/pay/cli.txt", "--zip-name", "single.zip", "-d", out_dir])
assert rc == 0 and os.path.exists(os.path.join(out_dir, "single.zip"))
rc = run_cli(base + ["delete", "--key", "lstd/pay/cli.txt", "--key", "missing-key"])
assert rc == 0

# 11. 缺少认证参数 -> SystemExit
try:
    oss_utils.main(["url", "--path", "a.png"])
    raise AssertionError("should exit when credentials missing")
except SystemExit as exc:
    print("missing auth exit code:", exc.code)

# 12. 目录下载 CLI
fake.objects = {"lstd/pay/dir/x.txt": b"x"}
with mock.patch.object(oss_utils.oss2, "ObjectIterator", fake_iterator):
    rc = run_cli(base + ["download", "--prefix", "lstd/pay/dir", "-d", out_dir, "--zip-name", "cli-folder.zip"])
assert rc == 0 and os.path.exists(os.path.join(out_dir, "cli-folder.zip"))

print("ALL TESTS PASSED")
shutil.rmtree(tmp, ignore_errors=True)
