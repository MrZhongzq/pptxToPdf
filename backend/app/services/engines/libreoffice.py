import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from app.config import settings
from app.errors import (
    ConversionFailed,
    ConversionPageMismatch,
    ConversionTimeout,
    EngineUnavailable,
)
from app.services.engines.base import ConversionEngine
from app.services.pptx_probe import PptxMeta

logger = logging.getLogger(__name__)

DEBUG_TIMEOUT_S = 1.0
LOG_TAIL_CHARS = 2000


class LibreOfficeEngine(ConversionEngine):
    """调 soffice --headless 做转换。

    三个必须遵守的约束，每一条都对应一类真实故障：
    1. 每次调用给独立的 UserInstallation 目录——多实例共用默认 profile
       会争抢锁文件，表现为随机失败或挂死。
    2. 超时必须杀整个进程组——soffice 会 fork 出真正干活的子进程，
       只杀父进程会留下孤儿继续吃内存，累积几个就打满机器。
    3. 退出码不可信——LibreOffice 转换失败时经常照样返回 0，
       成功判定必须以输出文件的实证为准。
    """

    name = "libreoffice"

    def convert(self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float) -> None:
        if settings.debug_force_engine_failure:
            raise ConversionFailed("故障注入 DEBUG_FORCE_ENGINE_FAILURE")

        # 把超时压到 1 秒，让真实的 soffice 调用超时，从而真正走一遍
        # killpg 清理路径——如果只是 sleep 再抛异常，测的就不是这段逻辑。
        effective_timeout = DEBUG_TIMEOUT_S if settings.debug_force_timeout else timeout_s

        dest.parent.mkdir(parents=True, exist_ok=True)
        profile = Path(tempfile.mkdtemp(prefix="lo_profile_"))
        cmd = [
            settings.soffice_bin,
            "--headless",
            "--norestore",
            "--invisible",
            f"-env:UserInstallation=file://{profile.as_posix()}",
            "--convert-to",
            "pdf:impress_pdf_Export",
            "--outdir",
            str(dest.parent),
            str(src),
        ]

        logger.info(
            "convert start src=%s slides=%d size=%d timeout=%.0fs cmd=%s",
            src.name, meta.slide_count, src.stat().st_size, effective_timeout, " ".join(cmd),
        )
        started = time.monotonic()

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 独立进程组，超时时整组端掉
            )
        except FileNotFoundError as exc:
            shutil.rmtree(profile, ignore_errors=True)
            raise EngineUnavailable(f"找不到可执行文件 {settings.soffice_bin}") from exc
        except OSError as exc:
            shutil.rmtree(profile, ignore_errors=True)
            raise EngineUnavailable(f"无法启动 soffice: {exc}") from exc

        try:
            stdout, _ = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            self._kill_process_group(proc)
            elapsed = time.monotonic() - started
            logger.warning(
                "convert timeout src=%s elapsed=%.1fs limit=%.0fs 已杀进程组",
                src.name, elapsed, effective_timeout,
            )
            raise ConversionTimeout(
                f"转换超时（{effective_timeout:.0f} 秒），文件可能过于复杂"
            ) from None
        finally:
            shutil.rmtree(profile, ignore_errors=True)

        elapsed = time.monotonic() - started
        logger.info(
            "convert finished src=%s rc=%s elapsed=%.1fs soffice_output=%s",
            src.name, proc.returncode, elapsed,
            stdout.decode("utf-8", errors="replace")[:LOG_TAIL_CHARS],
        )

        self._apply_output_fault_injection(dest)
        self._settle_output(src, dest)
        self._verify_output(dest, meta, src)

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        """杀掉整个进程组并回收。父进程可能已退出，各种竞态都要吞掉。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.error("进程组 %s 在 SIGKILL 后仍未退出", proc.pid)

    @staticmethod
    def _apply_output_fault_injection(dest: Path) -> None:
        if not dest.is_file():
            return
        if settings.debug_force_empty_output:
            dest.write_bytes(b"")
            logger.warning("故障注入 DEBUG_FORCE_EMPTY_OUTPUT：输出已截断为 0 字节")
        elif settings.debug_force_page_mismatch:
            reader = PdfReader(str(dest))
            writer = PdfWriter()
            for page in reader.pages[:-1]:
                writer.add_page(page)
            with dest.open("wb") as fh:
                writer.write(fh)
            logger.warning("故障注入 DEBUG_FORCE_PAGE_MISMATCH：输出已删掉最后一页")

    @staticmethod
    def _settle_output(src: Path, dest: Path) -> None:
        """把 soffice 实际产出的文件挪到调用方要的位置。

        `--outdir` 模式下 soffice 的输出文件名是**源文件名换扩展名**，不
        接受指定。此前这里默认 dest 与 src 同 stem——生产路径上两者都是
        `{task_id}`，所以一直成立，但那是个没写进签名也没有校验的隐含契约。

        它咬过两次：六期排查媒体剥离时，一个诊断脚本用了不同的 stem，拿到
        「soffice 未产出输出文件」，据此得出了「未剥离的原件 LibreOffice
        转不了」这个错误结论（实际转出来了，只是叫别的名字）；七期的 v1
        接口写成 input.pptx -> output.pdf，直接 500。

        与其继续要求每个调用方记住这条，不如在这里把它抹平。
        """
        if dest.is_file():
            return
        produced = dest.parent / f"{src.stem}.pdf"
        if produced.is_file():
            produced.replace(dest)

    @staticmethod
    def _verify_output(dest: Path, meta: PptxMeta, src: Path) -> None:
        """成功判定的四条实证。任一不成立即失败，且删掉坏文件。"""
        if not dest.is_file():
            raise ConversionFailed(
                f"soffice 未产出输出文件（退出码不可信，以文件为准）: {dest.name}"
            )
        if dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise ConversionFailed("输出 PDF 为 0 字节")
        try:
            pages = len(PdfReader(str(dest)).pages)
        except Exception as exc:
            dest.unlink(missing_ok=True)
            raise ConversionFailed(f"输出 PDF 无法解析: {exc}") from exc
        if pages != meta.slide_count:
            dest.unlink(missing_ok=True)
            raise ConversionPageMismatch(
                f"页数不符：{src.name} 有 {meta.slide_count} 页，"
                f"转出的 PDF 只有 {pages} 页"
            )
        logger.info("convert verified dest=%s pages=%d size=%d", dest.name, pages, dest.stat().st_size)
