"""同一项目不允许启动两套完整流水线。"""

from uuid import uuid4

from videotrans.configure.config import app_cfg


def test_same_project_task_is_single_flight():
    project_id = f"test-{uuid4()}"
    try:
        assert app_cfg.acquire_project_task(project_id) is True
        assert app_cfg.acquire_project_task(project_id) is False
    finally:
        app_cfg.release_project_task(project_id)

    assert app_cfg.acquire_project_task(project_id) is True
    app_cfg.release_project_task(project_id)
