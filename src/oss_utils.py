import argparse
import os
import posixpath
import re
import shutil
import sys
import zipfile
from datetime import datetime

import oss2
import requests

bucketClient = None

# MCP 等场景下需要关闭控制台提示，避免污染 stdout 协议流
_quiet = False

# 对象 ACL 可选值，与阿里云 OSS 控制台保持一致
ACL_MAP = {
    "default": oss2.OBJECT_ACL_DEFAULT,
    "private": oss2.OBJECT_ACL_PRIVATE,
    "public-read": oss2.OBJECT_ACL_PUBLIC_READ,
    "public-read-write": oss2.OBJECT_ACL_PUBLIC_READ_WRITE,
}

# 阿里云 OSS 单次批量删除最多支持 1000 个对象
BATCH_DELETE_LIMIT = 1000


def createBucketClient(ossConfig):
    """根据认证配置创建 Bucket 客户端（不修改全局客户端，供多分组场景使用）"""
    access_key_id = ossConfig["access_key_id"]
    access_key_secret = ossConfig["access_key_secret"]
    endpoint = ossConfig["endpoint"]
    bucket_name = ossConfig["bucket_name"]

    # 传入 security_token 时使用 STS 临时凭证
    security_token = ossConfig.get("security_token")
    if security_token:
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
    else:
        auth = oss2.Auth(access_key_id, access_key_secret)
    return oss2.Bucket(auth, endpoint, bucket_name)


def initOSSClient(ossConfig):
    # 初始化全局OSS客户端，后续不带 bucket 参数的函数都会使用它
    global bucketClient
    bucketClient = createBucketClient(ossConfig)
    return bucketClient


def _require_bucket():
    if bucketClient is None:
        raise RuntimeError("OSS 客户端未初始化，请先调用 initOSSClient()")
    return bucketClient


def setQuiet(quiet=True):
    """控制库函数是否输出提示信息；MCP 服务需调用 setQuiet(True) 避免污染 stdio 协议流"""
    global _quiet
    _quiet = quiet


def _log(message):
    if not _quiet:
        print(message)


def _build_acl_headers(acl):
    """把 ACL 名称转换为 PutObject 需要的请求头，未指定时返回 None（继承 Bucket）"""
    if not acl:
        return None
    permission = ACL_MAP.get(acl.lower())
    if permission is None:
        raise ValueError(f"不支持的 ACL 权限：{acl}，可选值：{', '.join(ACL_MAP)}")
    return {"x-oss-object-acl": permission}


def uploadFile(src_url, object_name, acl=None, bucket=None):
    """从网络地址下载文件并上传到 OSS
    :param src_url: 源文件网络地址
    :param object_name: OSS 对象路径
    :param acl: 对象 ACL（default/private/public-read/public-read-write），None 表示继承 Bucket
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    """
    bucket_client = bucket or _require_bucket()
    response = requests.get(src_url, timeout=60)
    response.raise_for_status()
    bucket_client.put_object(object_name, response.content, headers=_build_acl_headers(acl))

    _log(f'文件已成功上传到OSS，OSS路径为：{object_name}')
    return object_name


def uploadLocalFile(local_path, object_name, acl=None, bucket=None):
    """上传本地文件到 OSS
    :param local_path: 本地文件路径
    :param object_name: OSS 对象路径
    :param acl: 对象 ACL（default/private/public-read/public-read-write），None 表示继承 Bucket
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    """
    bucket_client = bucket or _require_bucket()
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"本地文件不存在：{local_path}")

    with open(local_path, "rb") as file_obj:
        bucket_client.put_object(object_name, file_obj, headers=_build_acl_headers(acl))

    _log(f'文件已成功上传到OSS，OSS路径为：{object_name}')
    return object_name


def replaceFile(local_path, object_name, keep_original_name=True, acl=None, bucket=None):
    """用本地文件替换 OSS 上已有的对象
    :param local_path: 新内容的本地文件路径
    :param object_name: 被替换的 OSS 对象路径
    :param keep_original_name: True 保留原对象路径；False 使用本地文件名放到原目录，并删除旧对象
    :param acl: 新对象 ACL（default/private/public-read/public-read-write），None 表示继承 Bucket
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    :return: 替换结果，包含新旧对象路径与是否改名、是否删除旧对象
    """
    bucket_client = bucket or _require_bucket()
    if not object_name or not object_name.strip():
        raise ValueError("对象路径不能为空")
    if object_name.endswith('/'):
        raise ValueError("对象路径不能以 / 结尾（这是目录前缀，不是文件）")
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"本地文件不存在：{local_path}")

    headers = _build_acl_headers(acl)
    if not bucket_client.object_exists(object_name):
        raise ValueError(f"待替换的 OSS 对象不存在：{object_name}，如需新建请使用上传功能")

    new_object_name = object_name
    if not keep_original_name:
        directory = posixpath.dirname(object_name)
        new_object_name = posixpath.join(directory, os.path.basename(local_path)) if directory else os.path.basename(local_path)
        if new_object_name != object_name and bucket_client.object_exists(new_object_name):
            raise ValueError(f"目标对象已存在，拒绝覆盖：{new_object_name}，请更换本地文件名或先删除目标对象")

    with open(local_path, "rb") as file_obj:
        bucket_client.put_object(new_object_name, file_obj, headers=headers)

    renamed = new_object_name != object_name
    old_deleted = False
    if renamed:
        try:
            bucket_client.delete_object(object_name)
        except Exception as exc:
            raise ValueError(
                f"新文件已上传到 {new_object_name}，但删除旧对象 {object_name} 失败：{exc}"
            ) from exc
        old_deleted = True

    _log(f'文件已替换：{object_name} -> {new_object_name}')
    return {
        "old_object_name": object_name,
        "new_object_name": new_object_name,
        "renamed": renamed,
        "old_deleted": old_deleted,
    }


def get_oss_image_url(
        relative_path: str,
        prefix: str = "lstd/pay/",  # OSS存储的目录前缀
        expires: int = 3600,      # URL有效期（秒），默认1小时
        is_public_bucket: bool = False,  # 是否为公开读Bucket
        bucket=None               # 指定 Bucket 客户端，不传则使用全局客户端
    ) -> str:
    """
    根据相对路径生成OSS图片的可访问URL（使用已初始化的Bucket客户端）
    :param relative_path: 图片的相对路径（如 "user/avatar.jpg"）
    :param prefix: OSS存储路径前缀（末尾需加"/"，默认为"images/"）
    :param expires: URL有效期（秒），仅私有Bucket需要
    :param is_public_bucket: Bucket是否为公共读
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    :return: 可访问的图片URL
    """
    bucket_client = bucket or _require_bucket()

    # 拼接完整OSS路径（避免双斜杠）
    full_path = f"{prefix.rstrip('/')}/{relative_path.lstrip('/')}"

    # 1. 如果是公开读Bucket，直接拼接URL
    if is_public_bucket:
        endpoint = bucket_client.endpoint
        bucket_name = bucket_client.bucket_name

        # 确保endpoint格式正确
        if not endpoint.startswith('http'):
            endpoint = f'https://{endpoint}'

        return f"{endpoint.rstrip('/')}/{bucket_name}/{full_path}"

    # 2. 非公开Bucket生成签名URL
    return bucket_client.sign_url(
        'GET',
        full_path,
        expires,
        slash_safe=True  # 保留路径中的斜杠
    )


def downloadFile(object_name, local_path=None, bucket=None):
    """下载单个文件到本地
    :param object_name: OSS 对象路径
    :param local_path: 本地保存路径或目录，不传时保存为当前目录下的文件名
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    """
    bucket_client = bucket or _require_bucket()
    file_name = os.path.basename(object_name.rstrip('/'))
    if not local_path:
        local_path = file_name
    elif os.path.isdir(local_path):
        local_path = os.path.join(local_path, file_name)
    if not local_path:
        raise ValueError(f"无法确定本地文件名：{object_name}")

    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    bucket_client.get_object_to_file(object_name, local_path)

    _log(f'文件已下载：{object_name} -> {local_path}')
    return local_path


def downloadFiles(object_names, local_dir=".", zip_name=None, bucket=None):
    """下载多个文件并打包为 zip
    :param object_names: OSS 对象路径列表
    :param local_dir: zip 文件存放目录
    :param zip_name: zip 文件名或路径，不传时自动生成
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    :return: zip 文件路径
    """
    bucket_client = bucket or _require_bucket()
    object_names = list(dict.fromkeys(name for name in object_names if name))
    if not object_names:
        raise ValueError("待下载的文件列表不能为空")

    zip_path = _resolve_zip_path(zip_name, local_dir, f"{bucket_client.bucket_name}_download_{_timestamp()}.zip")

    total = len(object_names)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for index, object_name in enumerate(object_names, 1):
            _log(f'[{index}/{total}] 正在下载：{object_name}')
            # 多个文件保留完整的对象路径，避免同名文件互相覆盖
            _add_object_to_zip(zip_file, object_name, object_name, bucket=bucket_client)

    _log(f'已打包 {total} 个文件到：{zip_path}')
    return zip_path


def downloadFolder(prefix, local_dir=".", zip_name=None, bucket=None):
    """下载整个目录（按前缀匹配）并打包为 zip，压缩包内保留目录内的相对路径
    :param prefix: OSS 目录前缀，如 "lstd/pay/2026/"
    :param local_dir: zip 文件存放目录
    :param zip_name: zip 文件名或路径，不传时自动生成
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    :return: zip 文件路径
    """
    bucket_client = bucket or _require_bucket()
    prefix = prefix.lstrip('/')
    if not prefix:
        raise ValueError("目录前缀不能为空")
    if not prefix.endswith('/'):
        prefix += '/'

    folder_name = prefix.rstrip('/').rsplit('/', 1)[-1]
    zip_path = _resolve_zip_path(zip_name, local_dir, f"{_sanitize_filename(folder_name)}_{_timestamp()}.zip")

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for obj in oss2.ObjectIterator(bucket_client, prefix=prefix):
            arcname = obj.key[len(prefix):]
            if not arcname:
                continue
            count += 1
            _log(f'[{count}] 正在下载：{obj.key}')
            _add_object_to_zip(zip_file, obj.key, arcname, bucket=bucket_client)

    if count == 0:
        os.remove(zip_path)
        raise ValueError(f"目录前缀 {prefix} 下没有可下载的文件")

    _log(f'已打包 {count} 个文件到：{zip_path}')
    return zip_path


def objectExists(object_name, bucket=None):
    """判断 OSS 对象（文件）是否存在
    :param object_name: OSS 对象路径
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    :return: True 表示存在，False 表示不存在
    """
    if not object_name or not object_name.strip():
        raise ValueError("对象路径不能为空")
    bucket_client = bucket or _require_bucket()
    return bucket_client.object_exists(object_name)


def deleteFile(object_name, bucket=None):
    """删除单个文件
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    """
    bucket_client = bucket or _require_bucket()
    bucket_client.delete_object(object_name)

    _log(f'文件已删除：{object_name}')
    return object_name


def deleteFiles(object_names, bucket=None):
    """批量删除文件，超过 1000 个时自动分批
    :param bucket: 指定 Bucket 客户端，不传则使用全局客户端
    :return: 实际删除的对象路径列表
    """
    bucket_client = bucket or _require_bucket()
    keys = list(dict.fromkeys(name for name in object_names if name))
    if not keys:
        raise ValueError("待删除的文件列表不能为空")

    deleted = []
    for start in range(0, len(keys), BATCH_DELETE_LIMIT):
        batch = keys[start:start + BATCH_DELETE_LIMIT]
        result = bucket_client.batch_delete_objects(batch)
        deleted.extend(result.deleted_keys)
        _log(f'已批量删除 {len(batch)} 个文件')

    return deleted


def _add_object_to_zip(zip_file, object_name, arcname, bucket=None):
    bucket_client = bucket or _require_bucket()
    if object_name.endswith('/'):
        # 目录占位对象，写入目录条目
        zip_file.writestr(arcname, b'')
        return

    obj = bucket_client.get_object(object_name)
    try:
        with zip_file.open(arcname, "w") as target:
            shutil.copyfileobj(obj, target)
    finally:
        obj.close()


def _resolve_zip_path(zip_name, local_dir, default_name):
    zip_path = os.path.join(local_dir, zip_name or default_name)
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    return zip_path


def _timestamp():
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def _sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip() or 'oss_download'


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="oss_utils.py",
        description="阿里云 OSS 工具：上传、替换、下载（多文件/目录自动打包 zip）、删除、获取访问地址",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python src/oss_utils.py --access-key-id AK --access-key-secret SK "
            "--endpoint oss-cn-hangzhou.aliyuncs.com --bucket demo "
            "upload --file ./a.png --key lstd/pay/a.png --acl public-read\n"
            "  python src/oss_utils.py ... replace --file ./new.png --key lstd/pay/a.png\n"
            "  python src/oss_utils.py ... replace --file ./new.png --key lstd/pay/a.png --use-local-name\n"
            "  python src/oss_utils.py ... download --key lstd/pay/a.png --key lstd/pay/b.png -d ./downloads\n"
            "  python src/oss_utils.py ... download --prefix lstd/pay/2026/ -d ./downloads\n"
            "  python src/oss_utils.py ... delete --key lstd/pay/a.png --key lstd/pay/b.png\n"
            "  python src/oss_utils.py ... url --path a.png --prefix lstd/pay/\n"
            "\n认证参数也可通过环境变量 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / "
            "OSS_SECURITY_TOKEN / OSS_ENDPOINT / OSS_BUCKET 提供。"
        ),
    )
    parser.add_argument("--access-key-id", default=os.environ.get("OSS_ACCESS_KEY_ID"),
                        help="OSS AccessKey ID（或环境变量 OSS_ACCESS_KEY_ID）")
    parser.add_argument("--access-key-secret", default=os.environ.get("OSS_ACCESS_KEY_SECRET"),
                        help="OSS AccessKey Secret（或环境变量 OSS_ACCESS_KEY_SECRET）")
    parser.add_argument("--security-token", default=os.environ.get("OSS_SECURITY_TOKEN"),
                        help="STS 临时凭证 Token，使用 STS 时传入（可选）")
    parser.add_argument("--endpoint", default=os.environ.get("OSS_ENDPOINT"),
                        help="OSS Endpoint，如 oss-cn-hangzhou.aliyuncs.com")
    parser.add_argument("--bucket", default=os.environ.get("OSS_BUCKET"),
                        help="Bucket 名称")

    subparsers = parser.add_subparsers(dest="command", required=True)

    upload = subparsers.add_parser("upload", help="上传文件到 OSS")
    source = upload.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="本地文件路径")
    source.add_argument("--url", help="网络文件地址")
    upload.add_argument("--key", required=True, help="OSS 对象路径，如 lstd/pay/a.png")
    upload.add_argument("--acl", choices=list(ACL_MAP), help="对象 ACL 权限，默认继承 Bucket")

    replace = subparsers.add_parser("replace", help="用本地文件替换 OSS 上已有文件，默认保留原对象路径")
    replace.add_argument("--file", required=True, help="新内容的本地文件路径")
    replace.add_argument("--key", required=True, help="被替换的 OSS 对象路径")
    replace.add_argument("--use-local-name", action="store_true",
                         help="使用本地文件名放到原对象所在目录，成功后删除旧对象；默认保留原对象路径")
    replace.add_argument("--acl", choices=list(ACL_MAP), help="新对象 ACL 权限，默认继承 Bucket")

    download = subparsers.add_parser("download", help="下载文件；多文件或整个目录自动打包为 zip")
    download_source = download.add_mutually_exclusive_group()
    download_source.add_argument("--key", action="append", default=[], help="OSS 对象路径，可重复传入多个")
    download_source.add_argument("--prefix", help="OSS 目录前缀，传入时下载整个目录并打包 zip（以 / 结尾的 --key 也按目录处理）")
    download.add_argument("-o", "--output", help="单文件下载时的本地文件路径或目录；多文件/目录下载时忽略")
    download.add_argument("-d", "--dir", default=".", help="zip 包存放目录，默认当前目录")
    download.add_argument("--zip-name", help="zip 文件名或路径；单文件指定时也会打包")

    delete = subparsers.add_parser("delete", help="删除文件；传入多个 --key 时批量删除")
    delete.add_argument("--key", action="append", required=True, help="OSS 对象路径，可重复传入多个")

    url = subparsers.add_parser("url", help="获取文件的可访问地址")
    url.add_argument("--path", required=True, help="相对路径，如 user/avatar.jpg")
    url.add_argument("--prefix", default="lstd/pay/", help="OSS 存储目录前缀，默认 lstd/pay/")
    url.add_argument("--expires", type=int, default=3600, help="私有 Bucket 签名 URL 有效期（秒），默认 3600")
    url.add_argument("--public", action="store_true", help="Bucket 为公共读时直接拼接 URL")

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    missing = [name for name, value in (
        ("--access-key-id", args.access_key_id),
        ("--access-key-secret", args.access_key_secret),
        ("--endpoint", args.endpoint),
        ("--bucket", args.bucket),
    ) if not value]
    if missing:
        parser.error(f"缺少认证参数：{', '.join(missing)}")

    initOSSClient({
        "access_key_id": args.access_key_id,
        "access_key_secret": args.access_key_secret,
        "security_token": args.security_token,
        "endpoint": args.endpoint,
        "bucket_name": args.bucket,
    })

    try:
        if args.command == "upload":
            if args.file:
                uploadLocalFile(args.file, args.key, acl=args.acl)
            else:
                uploadFile(args.url, args.key, acl=args.acl)
        elif args.command == "replace":
            replaceFile(
                args.file,
                args.key,
                keep_original_name=not args.use_local_name,
                acl=args.acl,
            )
        elif args.command == "download":
            if args.prefix:
                downloadFolder(args.prefix, local_dir=args.dir, zip_name=args.zip_name)
            elif len(args.key) == 1 and not args.zip_name and not args.key[0].endswith('/'):
                downloadFile(args.key[0], args.output)
            elif args.key:
                if len(args.key) == 1 and args.key[0].endswith('/'):
                    downloadFolder(args.key[0], local_dir=args.dir, zip_name=args.zip_name)
                else:
                    downloadFiles(args.key, local_dir=args.dir, zip_name=args.zip_name)
            else:
                parser.error("download 需要 --key 或 --prefix")
        elif args.command == "delete":
            if len(args.key) == 1:
                deleteFile(args.key[0])
            else:
                deleteFiles(args.key)
        elif args.command == "url":
            print(get_oss_image_url(
                args.path,
                prefix=args.prefix,
                expires=args.expires,
                is_public_bucket=args.public,
            ))
    except (oss2.exceptions.OssError, requests.RequestException, ValueError, OSError) as exc:
        print(f'执行失败：{exc}', file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
