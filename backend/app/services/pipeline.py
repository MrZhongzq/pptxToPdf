import json
import logging

from app.config import settings
from app.db import SessionLocal
from app.errors import AppError
from app.models import Task
from app.services.engines import get_engine
from app.services.pptx_probe import probe

logger = logging.getLogger(__name__)


def _set_status(session, task: Task, status: str) -> None:
    task.status = status
    session.commit()


def _record_failure(session, task_id: str, code: str, message: str) -> None:
    """失败落库自身也可能失败——回滚后用干净会话重试一次，仍失败则记日志，绝不再抛。"""
    try:
        session.rollback()
        task = session.get(Task, task_id)
        if task is not None:
            task.error_code = code
            task.error_message = message
            task.status = "failed"
            session.commit()
    except Exception:
        logger.exception("无法记录任务 %s 的失败状态", task_id)


def run_task(task_id: str) -> None:
    """走完整状态机：parsing → queued → converting → done / failed。"""
    session = SessionLocal()
    try:
        task = session.get(Task, task_id)
        if task is None:
            return

        src = settings.originals_dir / f"{task_id}.pptx"
        try:
            _set_status(session, task, "parsing")
            meta = probe(src)
            task.slide_count = meta.slide_count
            task.slide_width_emu = meta.slide_width_emu
            task.slide_height_emu = meta.slide_height_emu
            task.fonts_json = json.dumps(list(meta.fonts), ensure_ascii=False)

            _set_status(session, task, "queued")
            _set_status(session, task, "converting")

            dest = settings.outputs_dir / f"{task_id}.pdf"
            get_engine(task.engine).convert(
                src, meta, dest, timeout_s=settings.convert_timeout_base_s
            )

            task.output_path = str(dest)
            _set_status(session, task, "done")
        except AppError as exc:
            _record_failure(session, task_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001  兜底，避免后台任务静默吞掉
            _record_failure(session, task_id, "INTERNAL_ERROR", str(exc))
    finally:
        session.close()
