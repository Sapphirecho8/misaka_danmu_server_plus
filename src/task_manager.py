import asyncio
import logging
import traceback
from enum import Enum
import time
from typing import Any, Callable, Coroutine, Dict, List, Tuple, Optional # Add HTTPException, status
from uuid import uuid4, UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from .config_manager import ConfigManager

from . import models, crud

logger = logging.getLogger(__name__)
from fastapi import HTTPException, status

class TaskStatus(str, Enum):
    PENDING = "排队中"
    RUNNING = "运行中"
    COMPLETED = "已完成"
    FAILED = "失败"
    PAUSED = "已暂停"

class TaskSuccess(Exception):
    """自定义异常，用于表示任务成功完成并附带一条最终消息。"""
    pass

class Task:
    def __init__(self, task_id: str, title: str, coro_factory: Callable[[Callable], Coroutine], scheduled_task_id: Optional[str] = None, unique_key: Optional[str] = None):
        self.task_id = task_id
        self.title = title
        self.coro_factory: Callable[[AsyncSession, Callable], Coroutine] = coro_factory
        self.done_event = asyncio.Event()
        self.pause_event = asyncio.Event()
        self.running_coro_task: Optional[asyncio.Task] = None
        self.scheduled_task_id = scheduled_task_id
        self.last_update_time: float = 0.0
        self.update_lock = asyncio.Lock()
        self.unique_key = unique_key
        self.pause_event.set() # 默认为运行状态 (事件被设置)

class TaskManager:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], config_manager: ConfigManager):
        self._session_factory = session_factory
        self._queue: asyncio.Queue = asyncio.Queue()
        # 多工作协程以避免单任务阻塞（例如用户级流控导致的长时间等待）
        self._worker_tasks: list[asyncio.Task] = []
        self._max_workers: int = 3
        # 兼容旧接口的“当前任务”，保留最后一个被调度的引用
        self._current_task: Optional[Task] = None
        # 跟踪所有正在运行的任务，便于按ID暂停/终止
        self._running_tasks: dict[str, Task] = {}
        self._pending_titles: set[str] = set()
        self._active_unique_keys: set[str] = set()
        self._lock = asyncio.Lock()
        self.config_manager = config_manager
        self.logger = logging.getLogger(self.__class__.__name__)

    def start(self):
        """启动后台工作协程来处理任务队列。"""
        if not self._worker_tasks:
            for i in range(self._max_workers):
                t = asyncio.create_task(self._worker(i+1))
                self._worker_tasks.append(t)
            self.logger.info(f"任务管理器已启动，工作线程数: {self._max_workers}。")

    async def _run_task_wrapper(self, task: Task):
        """
        一个独立的包装器，用于在后台安全地执行单个任务。
        这可以防止单个任务的失败或阻塞影响到整个任务管理器。
        """
        self.logger.info(f"开始执行任务 '{task.title}' (ID: {task.task_id})")
        try:
            # This task is now running, remove it from pending titles
            # This is now the single point of responsibility for this cleanup.
            async with self._lock:
                self._pending_titles.discard(task.title)
                
            async with self._session_factory() as session:
                await crud.update_task_progress_in_history(
                    session, task.task_id, TaskStatus.RUNNING, 0, "正在初始化..."
                )
                progress_callback = self._get_progress_callback(task)
                actual_coroutine = task.coro_factory(session, progress_callback)

                running_task = asyncio.create_task(actual_coroutine)
                task.running_coro_task = running_task
                await running_task

                await crud.finalize_task_in_history(
                    session, task.task_id, TaskStatus.COMPLETED, "任务成功完成"
                )
                self.logger.info(f"任务 '{task.title}' (ID: {task.task_id}) 已成功完成。")
        except TaskSuccess as e:
            final_message = str(e) if str(e) else "任务成功完成"
            async with self._session_factory() as final_session:
                await crud.finalize_task_in_history(
                    final_session, task.task_id, TaskStatus.COMPLETED, final_message
                )
            self.logger.info(f"任务 '{task.title}' (ID: {task.task_id}) 已成功完成，消息: {final_message}")
        except asyncio.CancelledError:
            self.logger.info(f"任务 '{task.title}' (ID: {task.task_id}) 已被用户取消。")
            async with self._session_factory() as final_session:
                await crud.finalize_task_in_history(
                    final_session, task.task_id, TaskStatus.FAILED, "任务已被用户取消"
                )
        except Exception:
            error_message = f"任务执行失败 - {traceback.format_exc()}"
            async with self._session_factory() as final_session:
                await crud.finalize_task_in_history(
                    final_session, task.task_id, TaskStatus.FAILED, error_message.splitlines()[-1]
                )
            self.logger.error(f"任务 '{task.title}' (ID: {task.task_id}) 执行失败: {traceback.format_exc()}")
        finally:
            async with self._lock:
                if task.unique_key:
                    self._active_unique_keys.discard(task.unique_key)
                # Also remove from pending_titles again just in case of race conditions.
                self._pending_titles.discard(task.title)
            task.done_event.set()

    async def stop(self):
        """停止任务管理器。"""
        if self._worker_tasks:
            for t in self._worker_tasks:
                t.cancel()
            for t in self._worker_tasks:
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            self._worker_tasks.clear()
            self.logger.info("任务管理器已停止。")

    async def _worker(self, worker_id: int):
        """从队列中获取并执行任务。"""
        while True:
            task: Task = await self._queue.get()
            try:
                self._current_task = task
                async with self._lock:
                    self._running_tasks[task.task_id] = task
                # The wrapper now handles removing the title from the pending set.
                await self._run_task_wrapper(task)
            finally:
                async with self._lock:
                    self._running_tasks.pop(task.task_id, None)
                self._current_task = None
                self._queue.task_done()

    async def submit_task(
        self,
        coro_factory: Callable[[AsyncSession, Callable], Coroutine],
        title: str,
        scheduled_task_id: Optional[str] = None,
        unique_key: Optional[str] = None,
        run_immediately: bool = False
    ) -> Tuple[str, asyncio.Event]:
        """提交一个新任务到队列，并在数据库中创建记录。返回任务ID和完成事件。"""
        async with self._lock:
            # 检查是否有同名任务正在排队或运行
            if title in self._pending_titles:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"任务 '{title}' 已在队列中，请勿重复提交。"
                )
            if self._current_task and self._current_task.title == title:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"任务 '{title}' 已在运行中，请勿重复提交。"
                )
            
            # 新增：检查唯一键，防止同一资源的多个任务同时进行
            if unique_key:
                if unique_key in self._active_unique_keys:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"一个针对此媒体的相似任务已在队列中或正在运行，请勿重复提交。"
                    )
                self._active_unique_keys.add(unique_key)
            self._pending_titles.add(title)

        task_id = str(uuid4())
        task = Task(task_id, title, coro_factory, scheduled_task_id=scheduled_task_id, unique_key=unique_key)
        
        async with self._session_factory() as session:
            await crud.create_task_in_history(
                session, task_id, title, TaskStatus.PENDING, "等待执行...", scheduled_task_id=scheduled_task_id
            )
        
        if run_immediately:
            self.logger.info(f"立即执行任务 '{title}' (ID: {task_id})，绕过主队列。")
            asyncio.create_task(self._run_task_wrapper(task))
        else:
            await self._queue.put(task)
            self.logger.info(f"任务 '{title}' 已提交到队列，ID: {task_id}")
        return task_id, task.done_event

    def _get_progress_callback(self, task: Task) -> Callable:
        """为特定任务创建一个可暂停的回调闭包。"""
        async def pausable_callback(progress: int, description: str, status: Optional[TaskStatus] = None):
            # 核心暂停逻辑：在每次更新进度前，检查暂停事件。
            # 如果事件被清除 (cleared)，.wait() 将会阻塞，直到事件被重新设置 (set)。
            await task.pause_event.wait()

            now = time.time()
            # 只在状态改变、首次、完成或距离上次更新超过0.5秒时才更新数据库
            is_status_change = status is not None
            force_update = progress == 0 or progress >= 100 or is_status_change
            
            # 使用锁来防止并发更新 last_update_time
            async with task.update_lock:
                if not force_update and (now - task.last_update_time < 0.5):
                    return
                task.last_update_time = now

            # 数据库更新现在是同步的（在回调的协程内），但由于此逻辑，它不会频繁发生。
            # 这避免了创建大量并发任务，从而保护了数据库连接池。
            try:
                async with self._session_factory() as session:
                    await crud.update_task_progress_in_history(
                        session, task.task_id, status or TaskStatus.RUNNING, int(progress), description
                    )
            except Exception as e:
                self.logger.error(f"任务进度更新失败 (ID: {task.task_id}): {e}", exc_info=False)

        return pausable_callback

    async def cancel_pending_task(self, task_id: str) -> bool:
        """
        从队列中移除一个待处理的任务。
        注意：此操作不是线程安全的，但对于单工作线程模型是可接受的。
        """
        found_and_removed = False
        task_to_remove: Optional[Task] = None
        temp_list = []
        while not self._queue.empty():
            try:
                task = self._queue.get_nowait()
                if task.task_id == task_id:
                    found_and_removed = True
                    task_to_remove = task
                    task.done_event.set()
                    self.logger.info(f"已从队列中取消待处理任务 '{task.title}' (ID: {task_id})。")
                else:
                    temp_list.append(task)
            except asyncio.QueueEmpty:
                break

        for task in temp_list:
            await self._queue.put(task)

        # 修正：如果一个待处理任务被取消，必须同时清理其在管理器中的状态（任务标题和唯一键），
        # 以允许用户重新提交该任务。
        if found_and_removed and task_to_remove:
            async with self._lock:
                self._pending_titles.discard(task_to_remove.title)
                if task_to_remove.unique_key:
                    self._active_unique_keys.discard(task_to_remove.unique_key)
                    self.logger.info(f"已为已取消的待处理任务释放唯一键: {task_to_remove.unique_key}")

        return found_and_removed

    async def abort_current_task(self, task_id: str) -> bool:
        """中止指定ID的正在运行任务（支持多工作线程）。"""
        async with self._lock:
            task = self._running_tasks.get(task_id)
        if task and task.running_coro_task:
            self.logger.info(f"正在中止任务 '{task.title}' (ID: {task_id})")
            task.pause_event.set()
            task.running_coro_task.cancel()
            return True
        # 兼容旧逻辑
        if self._current_task and self._current_task.task_id == task_id and self._current_task.running_coro_task:
            self.logger.info(f"正在中止当前任务 '{self._current_task.title}' (ID: {task_id})")
            self._current_task.pause_event.set()
            self._current_task.running_coro_task.cancel()
            return True
        self.logger.warning(f"尝试中止任务 {task_id} 失败，因为它未在运行。")
        return False

    async def pause_task(self, task_id: str) -> bool:
        """暂停指定ID的任务（支持多工作线程）。"""
        async with self._lock:
            task = self._running_tasks.get(task_id)
        if task:
            async with self._session_factory() as session:
                task.pause_event.clear()
                await crud.update_task_status(session, task.task_id, TaskStatus.PAUSED)
            self.logger.info(f"已暂停任务 '{task.title}' (ID: {task_id})。")
            return True
        self.logger.warning(f"尝试暂停任务 {task_id} 失败，因为它未在运行。")
        return False

    async def resume_task(self, task_id: str) -> bool:
        """恢复指定ID的任务（支持多工作线程）。"""
        async with self._lock:
            task = self._running_tasks.get(task_id)
        if task:
            async with self._session_factory() as session:
                task.pause_event.set()
                await crud.update_task_status(session, task.task_id, TaskStatus.RUNNING)
            self.logger.info(f"已恢复任务 '{task.title}' (ID: {task_id})。")
            return True
        self.logger.warning(f"尝试恢复任务 {task_id} 失败，因为它未在运行。")
        return False
