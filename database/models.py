"""Data models shared by services, repositories, and UI adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Account:
    id: Optional[int]
    phone: str
    display_name: str
    username: str
    session_path: str
    last_login_at: str = ""


@dataclass(frozen=True)
class Chat:
    id: Optional[int]
    tg_chat_id: int
    title: str
    username: str
    type: str
    tag: str = ""
    telegram_folder_names: Optional[str] = None
    is_created_by_tool: bool = False
    last_message_id: Optional[int] = None
    last_backup_message_id: Optional[int] = None
    updated_at: str = ""


@dataclass(frozen=True)
class SearchResult:
    id: Optional[int]
    task_id: Optional[int]
    engine_name: str
    rank_no: int
    keyword: str
    result_type: str
    title: str
    summary: str
    url: str
    normalized_url: str
    tg_username: str = ""
    tg_message_id: Optional[int] = None
    tg_chat_id: Optional[int] = None
    is_duplicate: bool = False
    is_accessible: Optional[bool] = None
    is_protected: bool = False
    can_forward: Optional[bool] = None
    forward_status: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class TelegraphPage:
    id: Optional[int]
    search_result_id: Optional[int]
    message_db_id: Optional[int]
    url: str
    normalized_url: str
    title: str
    published_at: str = ""
    author_name: str = ""
    author_url: str = ""
    image_count: int = 0
    telegram_link_count: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class TelegraphImage:
    id: Optional[int]
    page_id: Optional[int]
    position: int
    url: str
    normalized_url: str
    local_path: str = ""
    download_status: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class TelegraphLink:
    id: Optional[int]
    page_id: Optional[int]
    position: int
    url: str
    normalized_url: str
    link_type: str
    text: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class PublicSearchTask:
    id: Optional[int]
    keyword: str
    engines: str
    max_results: int
    status: str
    total_found: int = 0
    total_saved: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    log_file: str = ""
    created_at: str = ""
    finished_at: str = ""


@dataclass(frozen=True)
class TaskSummary:
    task_id: str
    task_type: str
    title: str
    status: str
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    progress: int = 0
    log_file: str = ""


@dataclass(frozen=True)
class ForwardRecord:
    id: Optional[int]
    task_id: str
    source_type: str
    source_id: Optional[int]
    source_chat_id: Optional[int]
    source_message_id: Optional[int]
    target_chat_id: Optional[int]
    target_message_id: Optional[int]
    forward_mode: str
    status: str
    reason: str = ""
    error_code: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class MessageRecord:
    id: Optional[int]
    tg_chat_id: int
    message_id: int
    sender_id: Optional[int]
    sender_name: str
    date: str
    text: str
    text_preview: str
    message_type: str
    has_media: bool = False
    media_type: str = ""
    media_id: str = ""
    file_name: str = ""
    file_size: Optional[int] = None
    local_path: str = ""
    is_downloaded: bool = False
    is_protected: bool = False
    is_forwarded: bool = False
    source_link: str = ""
    external_urls: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class FileRecord:
    id: Optional[int]
    message_db_id: Optional[int]
    tg_chat_id: int
    message_id: int
    file_name: str
    file_ext: str
    file_size: Optional[int]
    local_path: str
    file_hash: str = ""
    download_status: str = ""
    error_code: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class DownloadRecord:
    id: Optional[int]
    task_id: str
    message_db_id: Optional[int]
    file_id: Optional[int]
    status: str
    local_path: str = ""
    error_code: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
