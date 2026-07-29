from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PPTX2PDF_")

    # 一期既有
    storage_root: Path = Path("storage")
    chunk_size: int = 5 * MIB
    max_file_size: int = 600 * MIB
    upload_ttl_hours: int = 24
    # ready 任务（传完但没点「开始转换」）的原文件保留时长。
    # 不复用 upload_ttl_hours：那个管的是未完成的上传会话、支持断点续传，
    # 调短会让大文件传到一半、暂停超时后必须从头重传。ready 的重传成本
    # 小得多，可以更快回收——机器盘不大，单份原件可能 80-500MB。
    ready_ttl_hours: int = 1
    database_url: str = "sqlite:///./pptx2pdf.db"

    # 二期新增：基础设施
    redis_url: str = "redis://redis:6379/0"
    output_ttl_hours: int = 24
    stale_task_minutes: int = 45

    # 二期新增：转换超时（秒）
    convert_timeout_base_s: int = 180
    convert_timeout_per_slide_s: int = 4
    convert_timeout_per_mb_s: int = 2
    convert_timeout_max_s: int = 1800
    """单次转换（或三期分片路径里单个分片/汇总 merge job）的墙钟预算上限。
    必须满足 convert_timeout_max_s + JOB_TIMEOUT_MARGIN_S（app/queue.py 里
    enqueue_shards 算出的 RQ job_timeout）< stale_task_minutes × 60（当前
    1800+60=1860s < 45×60=2700s）——merge job 必须先被 RQ 自己的超时掐死，
    孤儿回收器才可能开火；反过来的话，回收器会在 merge job 仍在跑的时候把
    任务标 failed，而 merge_shards 的终态守卫只在入口检查一次，拦不住这种
    中途被翻转的窗口（任务从 failed 又被迟到的 merge 改回 done）。"""
    soffice_bin: str = "soffice"

    # 三期：Graph 引擎
    secret_key: str | None = None
    """Fernet 主密钥（32 字节 urlsafe base64）。未配置则 Graph 引擎不可用——
    不设默认值兜底，那等于没加密。"""
    # 管理入口口令的 scrypt 哈希，格式 scrypt:<salt_hex>:<hash_hex>。
    # 未配置则管理入口整体 503——与 secret_key 一样不设默认值兜底。
    admin_password_hash: str | None = None
    # 当前部署是 http://<host>:18993，非 HTTPS。写死 Secure=true 会让浏览器
    # 根本不回传 cookie，表现为「登录成功但立刻掉线」且极难排查。
    # 切到 HTTPS 后必须改成 true。
    admin_cookie_secure: bool = False
    admin_session_days: int = 3
    graph_max_pages_per_shard: int = 80
    graph_max_shard_bytes: int = 40 * MIB
    graph_request_timeout_s: int = 50
    graph_max_retries: int = 3
    graph_max_shards: int = 12
    """分片数上限。合并峰值内存正比于分片总量，13 片 × 40MB 已经能在 2GB
    内存的 worker 上触发 OOM。超过这个数宁可明确拒绝，也不能让 worker 被
    OOM killer 静默干掉。

    注意 Graph 路径的实际容量 = graph_max_shards × graph_max_shard_bytes
    = 480MiB，**低于 max_file_size 的 600MiB**。这不是配置错误，是 Graph
    的固有限制（100 页硬上限 + 45 秒同步窗口）；LibreOffice 路径不受此限。
    也不要靠调大本值去对齐 600MiB——15 × 40MiB 配合下面实测的 3.01 倍率
    会直接 OOM。正确的做法是前端在选 Graph 引擎时按这个容量做上传前预判，
    避免用户白传一次 600MB 才在规划阶段吃 422（记在 ledger，Task 10）。"""
    graph_max_merge_bytes: int = 240 * MIB
    """合并输入（各分片 PDF）总字节上限。真正决定合并峰值内存的是 PDF 的
    总字节而不是分片数（一片也可能很大），所以分片数之外还要单独卡这一条。

    倍率取 3.01×，来自审查实测：4 片共 54.1MB 的图片密集型 PDF，
    tracemalloc 测得峰值 162.9MB。240MiB × 3.01 ≈ 720MB Python 堆峰值，
    在 2GB worker 上留有余量。注意 tracemalloc 不含解释器基线与分配器碎片，
    真实 RSS 更高——四期上真实租户后应实测 RSS 再回调本值。

    另一条同样承重、目前没写在任何地方的不变量：这个上限对应的合并耗时
    必须明显小于 merge job 的 RQ job_timeout（convert_timeout_max_s +
    JOB_TIMEOUT_MARGIN_S，app/queue.py，当前 1860s）——超了的话 merge job
    会被 RQ 直接掐死，任务卡在 merging 状态，只能等孤儿回收器收尸。240MiB
    用 pypdf 合并大概率远低于 31 分钟，但两个数字之间目前没有任何代码或
    测试把它们钉在一起；四期按实测 RSS 上调本值时必须重新核对这条还成不
    成立。"""

    # 二期新增：故障注入，默认全关
    debug_force_timeout: bool = False
    debug_force_engine_failure: bool = False
    debug_force_empty_output: bool = False
    debug_force_page_mismatch: bool = False

    @field_validator("storage_root")
    @classmethod
    def _resolve_storage_root(cls, v: Path) -> Path:
        """强制绝对化。task.output_path 落库时是 outputs_dir / f"{id}.pdf"——
        若 storage_root 保持相对路径，落库的就是相对 CWD 的字符串；uvicorn
        从不同工作目录重启后，tasks.py 里 Path(task.output_path).is_file()
        会按新 CWD 重新解析，历史任务的下载全部变成 409。这里在配置层一次性
        解决，pipeline.py 不需要再单独 resolve()。"""
        return v.resolve()

    @property
    def uploads_dir(self) -> Path:
        return self.storage_root / "uploads"

    @property
    def originals_dir(self) -> Path:
        return self.storage_root / "originals"

    @property
    def outputs_dir(self) -> Path:
        return self.storage_root / "outputs"

    @property
    def shards_dir(self) -> Path:
        return self.storage_root / "shards"

    def ensure_dirs(self) -> None:
        for d in (self.uploads_dir, self.originals_dir, self.outputs_dir, self.shards_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
