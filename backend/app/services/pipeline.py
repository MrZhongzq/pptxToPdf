import json

from app.config import settings
from app.db import SessionLocal
from app.errors import AppError
from app.models import Task
from app.services.engines import get_engine
from app.services.pptx_probe import probe


def _set_status(session, task: Task, status: str) -> None:
    task.status = status
    session.commit()


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
            get_engine(task.engine).convert(src, meta, dest)

            task.output_path = str(dest)
            _set_status(session, task, "done")
        except AppError as exc:
            task.error_code = exc.code
            task.error_message = exc.message
            _set_status(session, task, "failed")
        except Exception as exc:  # noqa: BLE001  兜底，避免后台任务静默吞掉
            task.error_code = "INTERNAL_ERROR"
            task.error_message = str(exc)
            _set_status(session, task, "failed")
    finally:
        session.close()
