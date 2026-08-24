from io import BytesIO
from zipfile import ZipFile

import pandas as pd
import pytest

from src.loader import DataLoadError, load_csv, load_excel, load_file


def test_load_csv_preserves_raw_text_and_leading_zero_sku() -> None:
    content = (
        b"date,marketplace,country,sku,product_name,impressions,clicks,orders,"
        b"units_sold,sales,ad_spend,refunds,inventory\n"
        b"2026-08-24,Amazon,US,00123,Product,100,10,2,2,20.00,5.00,0,9\n"
    )

    dataframe = load_csv(content)

    assert len(dataframe) == 1
    assert dataframe.loc[0, "sku"] == "00123"
    assert dataframe.loc[0, "impressions"] == "100"


def test_load_csv_preserves_na_tokens_instead_of_treating_them_as_missing() -> None:
    dataframe = load_csv(b"country,clicks\nNA,N/A\n")

    assert dataframe.loc[0, "country"] == "NA"
    assert dataframe.loc[0, "clicks"] == "N/A"


def test_load_csv_supports_gb18030_fallback() -> None:
    content = "sku,product_name\nSKU-1,中文商品\n".encode("gb18030")

    dataframe = load_csv(content)

    assert dataframe.loc[0, "product_name"] == "中文商品"


def test_csv_row_with_extra_field_is_rejected() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        load_csv(b"first,second\n1,2,3\n")

    assert exc_info.value.code == "MALFORMED_CSV"


def test_csv_row_with_missing_field_is_rejected() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        load_csv(b"first,second,third\n1,2\n")

    assert exc_info.value.code == "MALFORMED_CSV"


def test_csv_structure_check_supports_quoted_commas_and_escaped_quotes() -> None:
    dataframe = load_csv(b'name,notes\nSKU-1,"hello,world and ""quoted"" text"\n')

    assert dataframe.loc[0, "notes"] == 'hello,world and "quoted" text'


def test_csv_structure_check_supports_quoted_newlines() -> None:
    dataframe = load_csv(b'name,notes\nSKU-1,"line one\nline two"\n')

    assert dataframe.loc[0, "notes"] == "line one\nline two"


def test_csv_unclosed_quote_is_rejected() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        load_csv(b'name,notes\nSKU-1,"unclosed\n')

    assert exc_info.value.code == "MALFORMED_CSV"


def test_csv_blank_physical_line_is_ignored() -> None:
    dataframe = load_csv(b"sku,sales\n\nSKU-1,12.50\n")

    assert dataframe.to_dict(orient="records") == [
        {"sku": "SKU-1", "sales": "12.50"}
    ]


def test_load_xlsx() -> None:
    buffer = BytesIO()
    source = pd.DataFrame({"sku": ["00123"], "sales": [12.5]})
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        source.to_excel(writer, index=False)

    dataframe = load_excel(buffer.getvalue())

    assert dataframe.to_dict(orient="records") == [
        {"sku": "00123", "sales": 12.5}
    ]


def test_load_xlsx_uses_first_non_empty_sheet() -> None:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(columns=["unused"]).to_excel(
            writer, sheet_name="Empty", index=False
        )
        pd.DataFrame({"sku": ["SKU-1"]}).to_excel(
            writer, sheet_name="Data", index=False
        )
        pd.DataFrame({"sku": ["SKU-2"]}).to_excel(
            writer, sheet_name="Later", index=False
        )

    dataframe = load_excel(buffer.getvalue())

    assert dataframe["sku"].tolist() == ["SKU-1"]


def test_load_file_dispatches_using_case_insensitive_extension() -> None:
    dataframe = load_file(b"sku\nSKU-1\n", filename="UPLOAD.CSV")

    assert dataframe.loc[0, "sku"] == "SKU-1"


def test_unsupported_file_type() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        load_file(b"data", filename="input.xls")

    assert exc_info.value.code == "UNSUPPORTED_FILE_TYPE"
    assert ".csv" in exc_info.value.message
    assert ".xlsx" in exc_info.value.message


@pytest.mark.parametrize("content", [b"", b"   \n"])
def test_empty_csv(content: bytes) -> None:
    with pytest.raises(DataLoadError) as exc_info:
        load_csv(content)

    assert exc_info.value.code == "EMPTY_FILE"


def test_header_only_csv_is_empty() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        load_csv(b"sku,sales\n")

    assert exc_info.value.code == "EMPTY_FILE"


def test_empty_xlsx() -> None:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(columns=["sku"]).to_excel(writer, index=False)

    with pytest.raises(DataLoadError) as exc_info:
        load_excel(buffer.getvalue())

    assert exc_info.value.code == "EMPTY_FILE"


def test_corrupt_xlsx_is_file_read_error() -> None:
    with pytest.raises(DataLoadError) as exc_info:
        load_excel(b"not an xlsx workbook")

    assert exc_info.value.code == "FILE_READ_ERROR"


def test_corrupt_xlsx_internal_xml_is_file_read_error() -> None:
    valid_xlsx = BytesIO()
    with pd.ExcelWriter(valid_xlsx, engine="openpyxl") as writer:
        pd.DataFrame({"sku": ["SKU-1"]}).to_excel(writer, index=False)

    corrupt_xlsx = BytesIO()
    with ZipFile(BytesIO(valid_xlsx.getvalue())) as source_zip:
        with ZipFile(corrupt_xlsx, "w") as target_zip:
            for member in source_zip.infolist():
                content = source_zip.read(member.filename)
                if member.filename == "xl/workbook.xml":
                    content = b"<broken"
                target_zip.writestr(member, content)

    with pytest.raises(DataLoadError) as exc_info:
        load_excel(corrupt_xlsx.getvalue())

    assert exc_info.value.code == "FILE_READ_ERROR"
