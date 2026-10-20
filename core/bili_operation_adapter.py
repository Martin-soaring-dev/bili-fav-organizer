"""Composite Bilibili session wrapper backed by the remote-operation log.

The wrapped ``BiliSession`` remains responsible for HTTP details and rate
limiting. This adapter adds a durable operation intent around each supported
write method. Reads and other attributes are delegated to the wrapped session.

The caller owns the coordinator/repository and must have initialized its
operation-log schema through the database lifecycle/migration path. No database
path is opened here.
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import bili_api
from core.remote_operations import (
    InvalidOperationTransition,
    OperationIntent,
    OperationStatus,
    RemoteOperation,
    RemoteOperationCoordinator,
    UnsafeRetry,
)


class BiliOperationAdapterError(RuntimeError):
    """Base class for adapter-level state/result errors."""


class OperationExecutionBlocked(BiliOperationAdapterError):
    """A prior operation must be reconciled before the wrapped call can run."""


class OperationPreviouslyRejected(BiliOperationAdapterError):
    """The same stable intent already has a recorded definite rejection."""


class OperationResultUnavailable(BiliOperationAdapterError):
    """A confirmed intent exists but its safe return value was not persisted."""


class DefiniteRemoteRejection(BiliOperationAdapterError):
    """Adapter-generated explicit refusal, currently used for False add/remove."""


class BiliOperationAdapter:
    """Wrap the nine Bilibili write calls with durable operation coordination.

    ``task_id`` and ``account_id`` must be stable across task restart/resume.
    The default resource scope serializes unresolved writes for this account.
    All exposed methods preserve their wrapped method's positional arguments;
    they additionally accept explicit ``retry`` and ``validate_retry`` keyword
    arguments for a human/scheduler-authorized retry of a reconciled operation.

    By default every exception after dispatch becomes
    ``NEEDS_RECONCILIATION``. Only errors carrying an explicit Bilibili
    response code or explicit rate-limit response are classified as definite
    rejection. The wrapped ``add_with_aid`` and ``remove_from_folder`` APIs
    collapse errors to ``False``; per the adapter contract, a returned False is
    recorded as a definite rejection.
    """

    _API_RESPONSE_REJECTION_MARKERS = (
        "B站返回 code=",
        "B 站登录态失效(code=",
        "B站登录态失效(code=",
        "疑似触发风控或权限不足(code=",
        "B 站风控/频率限制(code=",
        "B 站风控校验失败(code=",
        "B站接口限流(HTTP 412)",
    )

    def __init__(
        self,
        session: Any,
        coordinator: RemoteOperationCoordinator,
        *,
        task_id: str,
        account_id: str,
    ) -> None:
        if not str(task_id).strip() or not str(account_id).strip():
            raise ValueError("task_id 和 account_id 必须非空且在任务恢复期间保持稳定")
        self._session = session
        self._coordinator = coordinator
        self._task_id = str(task_id)
        self._account_id = str(account_id)
        self._local = threading.local()
        self._retry_lock = threading.RLock()
        self._authorized_retry_ids: set[str] = set()

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def last_operation_id(self) -> str | None:
        """Most recent operation ID on the current thread, for diagnostics."""
        return getattr(self._local, "last_operation_id", None)

    def authorize_retry(self, operation_id: str) -> None:
        """Authorize one previously reviewed NOT_APPLIED intent for safe retry."""
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            raise ValueError("operation_id 不能为空")
        with self._retry_lock:
            self._authorized_retry_ids.add(operation_id)

    def __getattr__(self, name: str) -> Any:
        # Preserve the existing session's read-side API for consumers that use
        # this wrapper as a session substitute. The write methods below are
        # explicitly overridden and always pass through the operation log.
        return getattr(self._session, name)

    def create_folder(
        self, title: str, *, retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> str:
        normalized_title = str(title or "")
        intent = self._intent(
            "create_folder",
            target={"title": normalized_title},
            preconditions={"folder_title_available": normalized_title},
            postconditions={"folder_exists_with_title": normalized_title},
            request_summary={"title": normalized_title},
        )

        def send(_operation: RemoteOperation) -> str:
            folder_id = self._session.create_folder(title)
            folder_id = str(folder_id or "").strip()
            if not folder_id:
                # The request may have succeeded even though the response did
                # not contain the created folder ID.
                raise OperationExecutionBlocked("创建请求未返回可核对的收藏夹 ID")
            return folder_id

        return self._execute(intent, send, retry=retry,
                             validate_retry=validate_retry)

    def rename_folder(
        self, media_id: str, title: str, *, should_stop=None, retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> None:
        folder_id = str(media_id)
        normalized_title = str(title or "").strip()
        intent = self._intent(
            "rename_folder",
            target={"media_id": folder_id},
            preconditions={"folder_exists": True},
            postconditions={"title": normalized_title},
            request_summary={"title": normalized_title},
        )
        return self._execute(
            intent,
            lambda _op: self._session.rename_folder(
                media_id, title, should_stop=should_stop
            ),
            retry=retry, validate_retry=validate_retry,
        )

    def update_folder_intro(
        self, media_id: str, folder_info: dict, intro: str, *, should_stop=None,
        retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> dict:
        folder_id = str(media_id)
        intro_text = str(intro or "")
        intro_digest = hashlib.sha256(intro_text.encode("utf-8")).hexdigest()
        current_title = str((folder_info or {}).get("title") or "").strip()
        current_intro = str((folder_info or {}).get("intro") or "")
        current_intro_digest = hashlib.sha256(
            current_intro.encode("utf-8")
        ).hexdigest()
        try:
            privacy = int((folder_info or {}).get("attr") or 0) & 1
        except (TypeError, ValueError):
            privacy = "invalid"
        intent = self._intent(
            "update_folder_intro",
            target={"media_id": folder_id},
            preconditions={"title": current_title, "privacy": privacy,
                           "intro_sha256": current_intro_digest},
            postconditions={"intro_sha256": intro_digest},
            # The intro itself is user content; retain only a digest and length
            # in the operation record. The original value stays in the call.
            request_summary={"intro_length": len(intro_text),
                             "intro_sha256": intro_digest},
        )
        return self._execute(
            intent,
            lambda _op: self._session.update_folder_intro(
                media_id, folder_info, intro, should_stop=should_stop
            ),
            retry=retry, validate_retry=validate_retry,
        )

    def delete_folder(
        self, media_id: str, *, should_stop=None, retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> None:
        folder_id = str(media_id)
        intent = self._intent(
            "delete_folder",
            target={"media_id": folder_id},
            preconditions={"folder_is_empty": True},
            postconditions={"folder_absent": True},
            request_summary={"media_id": folder_id},
        )
        return self._execute(
            intent,
            lambda _op: self._session.delete_folder(media_id, should_stop=should_stop),
            retry=retry, validate_retry=validate_retry,
        )

    def clean_invalid_folder(
        self, media_id: str, *, should_stop=None, retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> None:
        folder_id = str(media_id)
        intent = self._intent(
            "clean_invalid_folder",
            target={"media_id": folder_id},
            preconditions={"folder_exists": True},
            postconditions={"invalid_items_cleaned": True},
            request_summary={"media_id": folder_id},
        )
        return self._execute(
            intent,
            lambda _op: self._session.clean_invalid_folder(
                media_id, should_stop=should_stop
            ),
            retry=retry, validate_retry=validate_retry,
        )

    def add_with_aid(
        self, media_id: str, aid: int, *, retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> bool:
        folder_id = str(media_id)
        video_id = int(aid)
        intent = self._intent(
            "add_video_to_folder",
            target={"media_id": folder_id},
            entity_ids=(video_id,),
            preconditions={"video_absent_from_folder": True},
            postconditions={"video_present_in_folder": True},
            request_summary={"media_id": folder_id, "aid": video_id},
        )

        def send(_operation: RemoteOperation) -> bool:
            result = self._session.add_with_aid(media_id, aid)
            if result is False:
                raise DefiniteRemoteRejection("add_with_aid 返回 False")
            if result is not True:
                raise OperationExecutionBlocked("add_with_aid 返回了无法确认的结果")
            return True

        return self._execute(intent, send, retry=retry,
                             validate_retry=validate_retry)

    def remove_from_folder(
        self, media_id: str, aid: int, *, retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> bool:
        folder_id = str(media_id)
        video_id = int(aid)
        intent = self._intent(
            "remove_video_from_folder",
            target={"media_id": folder_id},
            entity_ids=(video_id,),
            preconditions={"video_present_in_folder": True},
            postconditions={"video_absent_from_folder": True},
            request_summary={"media_id": folder_id, "aid": video_id},
        )

        def send(_operation: RemoteOperation) -> bool:
            result = self._session.remove_from_folder(media_id, aid)
            if result is False:
                raise DefiniteRemoteRejection("remove_from_folder 返回 False")
            if result is not True:
                raise OperationExecutionBlocked("remove_from_folder 返回了无法确认的结果")
            return True

        return self._execute(intent, send, retry=retry,
                             validate_retry=validate_retry)

    def move_batch(
        self, src_media_id: str, tar_media_id: str, aids: list[int], *,
        mid: str = "", should_stop=None, retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> int:
        clean_aids = self._clean_batch_aids(aids)
        source_id, target_id = str(src_media_id), str(tar_media_id)
        intent = self._intent(
            "move_videos",
            target={"source_media_id": source_id, "target_media_id": target_id},
            entity_ids=clean_aids,
            preconditions={"source_contains_all": True},
            postconditions={"source_missing_all": True, "target_contains_all": True},
            request_summary={"item_count": len(clean_aids)},
        )

        def send(_operation: RemoteOperation) -> int:
            result = self._session.move_batch(
                src_media_id, tar_media_id, aids,
                mid=mid, should_stop=should_stop,
            )
            if not isinstance(result, int) or isinstance(result, bool) or result != len(clean_aids):
                raise OperationExecutionBlocked("移动数量与请求数量不匹配，需要核对远端状态")
            return result

        return self._execute(intent, send, retry=retry,
                             validate_retry=validate_retry)

    def batch_delete(
        self, media_id: str, aids: list[int], *, should_stop=None, retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> int:
        clean_aids = self._clean_batch_aids(aids)
        folder_id = str(media_id)
        intent = self._intent(
            "delete_videos_from_folder",
            target={"media_id": folder_id},
            entity_ids=clean_aids,
            preconditions={"folder_contains_all": True},
            postconditions={"folder_missing_all": True},
            request_summary={"item_count": len(clean_aids)},
        )

        def send(_operation: RemoteOperation) -> int:
            result = self._session.batch_delete(
                media_id, aids, should_stop=should_stop
            )
            if not isinstance(result, int) or isinstance(result, bool) or result != len(clean_aids):
                raise OperationExecutionBlocked("删除数量与请求数量不匹配，需要核对远端状态")
            return result

        return self._execute(intent, send, retry=retry,
                             validate_retry=validate_retry)

    @staticmethod
    def _clean_batch_aids(aids: Sequence[int]) -> list[int]:
        # Match BiliSession._batch_resources so the logged stable ID describes
        # exactly the effective batch the wrapped method will submit.
        clean = list(dict.fromkeys(int(aid) for aid in aids if int(aid) > 0))
        if not clean:
            raise ValueError("批量操作没有有效 aid")
        if len(clean) > bili_api.BATCH_LIMIT:
            raise ValueError(f"批量操作超过上限：{len(clean)} > {bili_api.BATCH_LIMIT}")
        return clean

    def _intent(
        self,
        operation_type: str,
        *,
        target: Mapping[str, Any],
        entity_ids: Sequence[str | int] = (),
        preconditions: Mapping[str, Any],
        postconditions: Mapping[str, Any],
        request_summary: Mapping[str, Any],
    ) -> OperationIntent:
        return OperationIntent(
            task_id=self._task_id,
            account_id=self._account_id,
            operation_type=operation_type,
            target=target,
            entity_ids=entity_ids,
            preconditions=preconditions,
            postconditions=postconditions,
            request_summary=request_summary,
            resource_keys=("*",),
        )

    def _execute(
        self,
        intent: OperationIntent,
        sender: Callable[[RemoteOperation], Any],
        *,
        retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
    ) -> Any:
        operation = self._coordinator.prepare(intent)
        self._local.last_operation_id = operation.operation_id
        if operation.status is OperationStatus.NOT_APPLIED:
            with self._retry_lock:
                authorized_retry = operation.operation_id in self._authorized_retry_ids
            if authorized_retry:
                retry = True
                validate_retry = validate_retry or (
                    lambda candidate: candidate.operation_id == operation.operation_id
                )
        elif operation.status is OperationStatus.PREPARED:
            # An explicit retry flag is only meaningful after an evidenced
            # NOT_APPLIED reconciliation. A fresh PREPARED intent is a normal
            # first dispatch and must not enter the retry-only state machine.
            retry = False
            validate_retry = None
        if operation.status is OperationStatus.CONFIRMED:
            if operation.remote_response is None or "result" not in operation.remote_response:
                raise OperationResultUnavailable(
                    f"操作 {operation.operation_id} 已确认，但安全返回值不可用"
                )
            return operation.remote_response["result"]
        if operation.status is OperationStatus.REJECTED:
            if operation.operation_type in (
                "add_video_to_folder", "remove_video_from_folder"
            ):
                # Preserve BiliSession's boolean return contract on first use
                # and when a resumed task replays the same stable intent.
                return False
            raise OperationPreviouslyRejected(
                f"操作 {operation.operation_id} 已有明确拒绝记录 ({operation.error_code})"
            )
        if operation.status is not OperationStatus.PREPARED and not (
            retry and operation.status is OperationStatus.NOT_APPLIED
        ):
            raise OperationExecutionBlocked(
                f"操作 {operation.operation_id} 当前为 {operation.status.value}，需先处理核对状态"
            )
        try:
            return self._coordinator.execute(
                operation.operation_id,
                sender,
                retry=retry,
                validate_retry=validate_retry,
                is_definite_rejection=self._is_definite_rejection,
                summarize_response=lambda result: {"result": result},
            )
        except DefiniteRemoteRejection:
            # add_with_aid/remove_from_folder historically return False for a
            # definite API refusal instead of raising. Keep that caller-facing
            # behavior while the coordinator durably records REJECTED.
            if operation.operation_type in (
                "add_video_to_folder", "remove_video_from_folder"
            ):
                return False
            raise

    @classmethod
    def _is_definite_rejection(cls, exc: Exception) -> bool:
        if isinstance(exc, DefiniteRemoteRejection):
            return True
        if isinstance(exc, bili_api.WriteUncertainError):
            return False
        if not isinstance(exc, bili_api.BiliApiError):
            return False
        text = str(exc)
        return any(marker in text for marker in cls._API_RESPONSE_REJECTION_MARKERS)


__all__ = [
    "BiliOperationAdapter", "BiliOperationAdapterError", "DefiniteRemoteRejection",
    "OperationExecutionBlocked", "OperationPreviouslyRejected",
    "OperationResultUnavailable",
]
