"""Read CSV and XLSX uploads into raw pandas DataFrames."""

from __future__ import annotations

import csv
from io import BytesIO, StringIO
from pathlib import Path
from typing import IO, Any

import pandas as pd


class DataLoadError(Exception):
    """A stable, user-facing failure raised while reading an input file."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


FileSource = str | Path | bytes | bytearray | IO[Any]


def _read_source_bytes(source: FileSource) -> bytes:
    if isinstance(source, (str, Path)):
        try:
            return Path(source).read_bytes()
        except OSError as exc:
            raise DataLoadError(
                "FILE_READ_ERROR", f"无法读取文件：{exc}"
            ) from exc

    if isinstance(source, (bytes, bytearray)):
        return bytes(source)

    if not hasattr(source, "read"):
        raise DataLoadError("FILE_READ_ERROR", "输入对象不是可读取的文件。")

    try:
        if hasattr(source, "seek"):
            source.seek(0)
        content = source.read()
    except (OSError, ValueError) as exc:
        raise DataLoadError(
            "FILE_READ_ERROR", f"读取上传文件时发生错误：{exc}"
        ) from exc

    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    raise DataLoadError("FILE_READ_ERROR", "上传文件返回了无法识别的内容类型。")


def _ensure_non_empty(content: bytes) -> None:
    if not content:
        raise DataLoadError("EMPTY_FILE", "文件为空，无法读取数据。")


def _decode_csv(content: bytes) -> str:
    last_encoding_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError as exc:
            last_encoding_error = exc

    raise DataLoadError(
        "FILE_READ_ERROR", "CSV 编码无法识别，请使用 UTF-8-SIG 或 GB18030。"
    ) from last_encoding_error


def _validate_csv_structure(content: bytes) -> str:
    text = _decode_csv(content)
    try:
        reader = csv.reader(StringIO(text, newline=""), strict=True)
        header: list[str] | None = None
        data_row_count = 0
        for record in reader:
            if not record:
                continue
            if header is None:
                header = record
                continue

            data_row_count += 1
            if len(record) != len(header):
                raise DataLoadError(
                    "MALFORMED_CSV",
                    (
                        f"CSV 逻辑记录 {data_row_count + 1} 包含 {len(record)} 个字段，"
                        f"但表头包含 {len(header)} 个字段。"
                    ),
                )
    except csv.Error as exc:
        raise DataLoadError("MALFORMED_CSV", f"CSV 结构无法解析：{exc}") from exc

    if header is None or not any(header):
        raise DataLoadError("EMPTY_FILE", "CSV 文件不包含可读取的数据。")
    if data_row_count == 0:
        raise DataLoadError("EMPTY_FILE", "CSV 文件没有数据行。")
    return text


def load_csv(source: FileSource) -> pd.DataFrame:
    """Load a CSV while preserving raw text for the validator."""

    content = _read_source_bytes(source)
    _ensure_non_empty(content)
    text = _validate_csv_structure(content)
    try:
        return pd.read_csv(
            StringIO(text),
            dtype=str,
            keep_default_na=False,
        )
    except Exception as exc:
        raise DataLoadError(
            "FILE_READ_ERROR", f"CSV 文件无法解析：{exc}"
        ) from exc


def load_excel(source: FileSource) -> pd.DataFrame:
    """Load the first non-empty worksheet from an XLSX workbook."""

    try:
        content = _read_source_bytes(source)
        _ensure_non_empty(content)
        with pd.ExcelFile(BytesIO(content), engine="openpyxl") as workbook:
            for sheet_name in workbook.sheet_names:
                dataframe = pd.read_excel(
                    workbook,
                    sheet_name=sheet_name,
                    dtype=object,
                    keep_default_na=False,
                )
                if not dataframe.empty:
                    return dataframe
    except DataLoadError:
        raise
    except Exception as exc:
        raise DataLoadError(
            "FILE_READ_ERROR", f"XLSX 文件无法解析：{exc}"
        ) from exc

    raise DataLoadError("EMPTY_FILE", "XLSX 文件中没有可读取的数据行。")


def load_file(source: FileSource, filename: str | None = None) -> pd.DataFrame:
    """Dispatch a file to the appropriate loader based on its extension."""

    resolved_name = filename
    if resolved_name is None:
        if isinstance(source, (str, Path)):
            resolved_name = str(source)
        else:
            resolved_name = getattr(source, "name", None)

    suffix = Path(resolved_name).suffix.lower() if resolved_name else ""
    if suffix == ".csv":
        return load_csv(source)
    if suffix == ".xlsx":
        return load_excel(source)

    displayed_type = suffix or "无扩展名"
    raise DataLoadError(
        "UNSUPPORTED_FILE_TYPE",
        f"不支持的文件类型：{displayed_type}。V1 仅支持 .csv 和 .xlsx。",
    )
