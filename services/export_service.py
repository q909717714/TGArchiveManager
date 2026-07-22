"""Export service for local Telegram archive data."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Iterable

from database.models import MessageRecord
from database.repositories import MessageRepository
from services.local_search_service import LocalSearchQuery, LocalSearchService


class ExportServiceError(RuntimeError):
    """Raised when an export cannot be generated."""

    error_code = "EX001"


@dataclass(frozen=True)
class ExportReport:
    """Report describing generated export files."""

    total_count: int
    files: tuple[str, ...]


class ExportService:
    """Export local search results to CSV, Excel, JSON, and optional HTML."""

    COLUMNS = [
        "tg_chat_id",
        "message_id",
        "sender_id",
        "sender_name",
        "date",
        "message_type",
        "has_media",
        "media_type",
        "file_name",
        "file_size",
        "is_downloaded",
        "text",
        "source_link",
        "external_urls",
        "local_path",
    ]

    def __init__(
        self,
        message_repository: MessageRepository,
        search_service: LocalSearchService,
        export_root: Path,
        logger: logging.Logger,
    ):
        self._message_repository = message_repository
        self._search_service = search_service
        self._export_root = Path(export_root)
        self._logger = logger

    def export_messages(
        self,
        query: LocalSearchQuery,
        formats: Iterable[str],
        base_name: str = "tg_archive_messages",
    ) -> ExportReport:
        """Search messages and export them in requested formats."""
        messages = self._search_service.search(query)
        return self.export_message_records(messages, formats, base_name=base_name)

    def export_message_records(
        self,
        messages: list[MessageRecord],
        formats: Iterable[str],
        base_name: str = "tg_archive_messages",
    ) -> ExportReport:
        """Export already-loaded message records in requested formats."""
        clean_formats = [str(fmt).lower().strip() for fmt in formats if str(fmt).strip()]
        if not clean_formats:
            raise ExportServiceError("请选择至少一种导出格式")

        rows = [self._message_to_row(message) for message in messages]
        self._export_root.mkdir(parents=True, exist_ok=True)
        stem = self._safe_stem(base_name)
        generated: list[str] = []
        for fmt in clean_formats:
            if fmt == "csv":
                generated.append(str(self._export_csv(rows, stem)))
            elif fmt in {"xlsx", "excel"}:
                generated.append(str(self._export_xlsx(rows, stem)))
            elif fmt == "json":
                generated.append(str(self._export_json(rows, stem)))
            elif fmt == "html":
                generated.append(str(self._export_html(rows, stem)))
            else:
                raise ExportServiceError(f"不支持的导出格式：{fmt}")

        self._logger.info("Export completed: rows=%s files=%s", len(rows), len(generated))
        return ExportReport(total_count=len(rows), files=tuple(generated))

    def _export_csv(self, rows: list[dict], stem: str) -> Path:
        path = self._export_root / f"{stem}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _export_xlsx(self, rows: list[dict], stem: str) -> Path:
        try:
            from openpyxl import Workbook
        except ImportError as exc:
            raise ExportServiceError("openpyxl 未安装，无法导出 Excel") from exc

        path = self._export_root / f"{stem}.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "messages"
        sheet.append(self.COLUMNS)
        for row in rows:
            sheet.append([row[column] for column in self.COLUMNS])
        for column_cells in sheet.columns:
            max_length = max(len(str(cell.value or "")) for cell in column_cells)
            sheet.column_dimensions[column_cells[0].column_letter].width = min(max(10, max_length + 2), 60)
        workbook.save(path)
        return path

    def _export_json(self, rows: list[dict], stem: str) -> Path:
        path = self._export_root / f"{stem}.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _export_html(self, rows: list[dict], stem: str) -> Path:
        path = self._export_root / f"{stem}.html"
        header = "".join(f"<th>{escape(column)}</th>" for column in self.COLUMNS)
        body = "\n".join(
            "<tr>" + "".join(f"<td>{escape(str(row[column]))}</td>" for column in self.COLUMNS) + "</tr>"
            for row in rows
        )
        html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>TGArchiveManager Export</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d0d7de; padding: 6px 8px; vertical-align: top; }}
    th {{ background: #f6f8fa; position: sticky; top: 0; }}
    td {{ white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <h1>TGArchiveManager Export</h1>
  <p>Rows: {len(rows)}</p>
  <table>
    <thead><tr>{header}</tr></thead>
    <tbody>
{body}
    </tbody>
  </table>
</body>
</html>
"""
        path.write_text(html, encoding="utf-8")
        return path

    @classmethod
    def _message_to_row(cls, message: MessageRecord) -> dict:
        return {
            "tg_chat_id": message.tg_chat_id,
            "message_id": message.message_id,
            "sender_id": "" if message.sender_id is None else message.sender_id,
            "sender_name": message.sender_name,
            "date": message.date,
            "message_type": message.message_type,
            "has_media": message.has_media,
            "media_type": message.media_type,
            "file_name": message.file_name,
            "file_size": "" if message.file_size is None else message.file_size,
            "is_downloaded": message.is_downloaded,
            "text": message.text,
            "source_link": message.source_link,
            "external_urls": message.external_urls,
            "local_path": message.local_path,
        }

    @staticmethod
    def _safe_stem(base_name: str) -> str:
        raw = str(base_name or "tg_archive_messages").strip() or "tg_archive_messages"
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw).strip("_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{safe or 'tg_archive_messages'}_{timestamp}"
