import hashlib
import json
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from time import time
from typing import Any, Generator, Union

import numpy as np
import pandas as pd
from dateutil import parser
from pydantic import BaseModel, Field, Secret

from unstructured_ingest.error import DestinationConnectionError, SourceConnectionError
from unstructured_ingest.utils.data_prep import get_data_df, split_dataframe
from unstructured_ingest.v2.constants import RECORD_ID_LABEL
from unstructured_ingest.v2.interfaces import (
    AccessConfig,
    BatchFileData,
    BatchItem,
    ConnectionConfig,
    Downloader,
    DownloaderConfig,
    DownloadResponse,
    FileData,
    FileDataSourceMetadata,
    Indexer,
    IndexerConfig,
    Uploader,
    UploaderConfig,
    UploadStager,
    UploadStagerConfig,
    download_responses,
)
from unstructured_ingest.v2.logger import logger
from unstructured_ingest.v2.utils import get_enhanced_element_id

_COLUMNS = (
    "id",
    "element_id",
    "text",
    "embeddings",
    "type",
    "system",
    "layout_width",
    "layout_height",
    "points",
    "url",
    "version",
    "date_created",
    "date_modified",
    "date_processed",
    "permissions_data",
    "record_locator",
    "category_depth",
    "parent_id",
    "attached_filename",
    "filetype",
    "last_modified",
    "file_directory",
    "filename",
    "languages",
    "page_number",
    "links",
    "page_name",
    "link_urls",
    "link_texts",
    "sent_from",
    "sent_to",
    "subject",
    "section",
    "header_footer_type",
    "emphasized_text_contents",
    "emphasized_text_tags",
    "text_as_html",
    "regex_metadata",
    "detection_class_prob",
)

_DATE_COLUMNS = ("date_created", "date_modified", "date_processed", "last_modified")


class SqlAdditionalMetadata(BaseModel):
    table_name: str
    id_column: str


class SqlBatchFileData(BatchFileData):
    additional_metadata: SqlAdditionalMetadata


def parse_date_string(date_value: Union[str, int]) -> date:
    try:
        timestamp = float(date_value) / 1000 if isinstance(date_value, int) else float(date_value)
        return datetime.fromtimestamp(timestamp)
    except Exception as e:
        logger.debug(f"date {date_value} string not a timestamp: {e}")
    return parser.parse(date_value)


class SQLAccessConfig(AccessConfig):
    pass


class SQLConnectionConfig(ConnectionConfig, ABC):
    access_config: Secret[SQLAccessConfig] = Field(default=SQLAccessConfig(), validate_default=True)

    @abstractmethod
    @contextmanager
    def get_connection(self) -> Generator[Any, None, None]:
        pass

    @abstractmethod
    @contextmanager
    def get_cursor(self) -> Generator[Any, None, None]:
        pass


class SQLIndexerConfig(IndexerConfig):
    table_name: str
    id_column: str
    batch_size: int = 100


class SQLIndexer(Indexer, ABC):
    connection_config: SQLConnectionConfig
    index_config: SQLIndexerConfig

    def _get_doc_ids(self) -> list[str]:
        with self.connection_config.get_cursor() as cursor:
            cursor.execute(
                f"SELECT {self.index_config.id_column} FROM {self.index_config.table_name}"
            )
            results = cursor.fetchall()
            ids = sorted([result[0] for result in results])
            return ids

    def precheck(self) -> None:
        try:
            with self.connection_config.get_cursor() as cursor:
                cursor.execute("SELECT 1;")
        except Exception as e:
            logger.error(f"failed to validate connection: {e}", exc_info=True)
            raise SourceConnectionError(f"failed to validate connection: {e}")

    def run(self, **kwargs: Any) -> Generator[SqlBatchFileData, None, None]:
        ids = self._get_doc_ids()
        id_batches: list[frozenset[str]] = [
            frozenset(
                ids[
                    i
                    * self.index_config.batch_size : (i + 1)  # noqa
                    * self.index_config.batch_size
                ]
            )
            for i in range(
                (len(ids) + self.index_config.batch_size - 1) // self.index_config.batch_size
            )
        ]
        for batch in id_batches:
            # Make sure the hash is always a positive number to create identified
            yield SqlBatchFileData(
                connector_type=self.connector_type,
                metadata=FileDataSourceMetadata(
                    date_processed=str(time()),
                ),
                additional_metadata=SqlAdditionalMetadata(
                    table_name=self.index_config.table_name, id_column=self.index_config.id_column
                ),
                batch_items=[BatchItem(identifier=str(b)) for b in batch],
            )


class SQLDownloaderConfig(DownloaderConfig):
    fields: list[str] = field(default_factory=list)


class SQLDownloader(Downloader, ABC):
    connection_config: SQLConnectionConfig
    download_config: SQLDownloaderConfig

    @abstractmethod
    def query_db(self, file_data: SqlBatchFileData) -> tuple[list[tuple], list[str]]:
        pass

    def sql_to_df(self, rows: list[tuple], columns: list[str]) -> list[pd.DataFrame]:
        data = [dict(zip(columns, row)) for row in rows]
        df = pd.DataFrame(data)
        dfs = [pd.DataFrame([row.values], columns=df.columns) for index, row in df.iterrows()]
        return dfs

    def get_data(self, file_data: SqlBatchFileData) -> list[pd.DataFrame]:
        rows, columns = self.query_db(file_data=file_data)
        return self.sql_to_df(rows=rows, columns=columns)

    def get_identifier(self, table_name: str, record_id: str) -> str:
        f = f"{table_name}-{record_id}"
        if self.download_config.fields:
            f = "{}-{}".format(
                f,
                hashlib.sha256(",".join(self.download_config.fields).encode()).hexdigest()[:8],
            )
        return f

    def generate_download_response(
        self, result: pd.DataFrame, file_data: SqlBatchFileData
    ) -> DownloadResponse:
        id_column = file_data.additional_metadata.id_column
        table_name = file_data.additional_metadata.table_name
        record_id = result.iloc[0][id_column]
        filename_id = self.get_identifier(table_name=table_name, record_id=record_id)
        filename = f"{filename_id}.csv"
        download_path = self.download_dir / Path(filename)
        logger.debug(
            f"Downloading results from table {table_name} and id {record_id} to {download_path}"
        )
        download_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(download_path, index=False)
        cast_file_data = FileData.cast(file_data=file_data)
        cast_file_data.identifier = filename_id
        return super().generate_download_response(
            file_data=cast_file_data, download_path=download_path
        )

    def run(self, file_data: FileData, **kwargs: Any) -> download_responses:
        sql_filedata = SqlBatchFileData.cast(file_data=file_data)
        data_dfs = self.get_data(file_data=sql_filedata)
        download_responses = []
        for df in data_dfs:
            download_responses.append(
                self.generate_download_response(result=df, file_data=sql_filedata)
            )
        return download_responses


class SQLUploadStagerConfig(UploadStagerConfig):
    pass


@dataclass
class SQLUploadStager(UploadStager):
    upload_stager_config: SQLUploadStagerConfig = field(default_factory=SQLUploadStagerConfig)

    def conform_dict(self, element_dict: dict, file_data: FileData) -> dict:
        data = element_dict.copy()
        metadata: dict[str, Any] = data.pop("metadata", {})
        data_source = metadata.pop("data_source", {})
        coordinates = metadata.pop("coordinates", {})

        data.update(metadata)
        data.update(data_source)
        data.update(coordinates)

        data["id"] = get_enhanced_element_id(element_dict=data, file_data=file_data)

        # remove extraneous, not supported columns
        element = {k: v for k, v in data.items() if k in _COLUMNS}
        element[RECORD_ID_LABEL] = file_data.identifier
        return element

    def conform_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        for column in filter(lambda x: x in df.columns, _DATE_COLUMNS):
            df[column] = df[column].apply(parse_date_string)
        for column in filter(
            lambda x: x in df.columns,
            ("permissions_data", "record_locator", "points", "links"),
        ):
            df[column] = df[column].apply(
                lambda x: json.dumps(x) if isinstance(x, (list, dict)) else None
            )
        for column in filter(
            lambda x: x in df.columns,
            ("version", "page_number", "regex_metadata"),
        ):
            df[column] = df[column].apply(str)
        return df

    def run(
        self,
        elements_filepath: Path,
        file_data: FileData,
        output_dir: Path,
        output_filename: str,
        **kwargs: Any,
    ) -> Path:
        elements_contents = self.get_data(elements_filepath=elements_filepath)

        df = pd.DataFrame(
            data=[
                self.conform_dict(element_dict=element_dict, file_data=file_data)
                for element_dict in elements_contents
            ]
        )
        df = self.conform_dataframe(df=df)

        output_path = self.get_output_path(output_filename=output_filename, output_dir=output_dir)

        self.write_output(output_path=output_path, data=df.to_dict(orient="records"))
        return output_path


class SQLUploaderConfig(UploaderConfig):
    batch_size: int = Field(default=50, description="Number of records per batch")
    table_name: str = Field(default="elements", description="which table to upload contents to")
    record_id_key: str = Field(
        default=RECORD_ID_LABEL,
        description="searchable key to find entries for the same record on previous runs",
    )


@dataclass
class SQLUploader(Uploader):
    upload_config: SQLUploaderConfig
    connection_config: SQLConnectionConfig
    values_delimiter: str = "?"

    def precheck(self) -> None:
        try:
            with self.connection_config.get_cursor() as cursor:
                cursor.execute("SELECT 1;")
        except Exception as e:
            logger.error(f"failed to validate connection: {e}", exc_info=True)
            raise DestinationConnectionError(f"failed to validate connection: {e}")

    def prepare_data(
        self, columns: list[str], data: tuple[tuple[Any, ...], ...]
    ) -> list[tuple[Any, ...]]:
        output = []
        for row in data:
            parsed = []
            for column_name, value in zip(columns, row):
                if column_name in _DATE_COLUMNS:
                    if value is None:
                        parsed.append(None)
                    else:
                        parsed.append(parse_date_string(value))
                else:
                    parsed.append(value)
            output.append(tuple(parsed))
        return output

    def _fit_to_schema(self, df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
        columns = set(df.columns)
        schema_fields = set(columns)
        columns_to_drop = columns - schema_fields
        missing_columns = schema_fields - columns

        if columns_to_drop:
            logger.warning(
                "Following columns will be dropped to match the table's schema: "
                f"{', '.join(columns_to_drop)}"
            )
        if missing_columns:
            logger.info(
                "Following null filled columns will be added to match the table's schema:"
                f" {', '.join(missing_columns)} "
            )

        df = df.drop(columns=columns_to_drop)

        for column in missing_columns:
            df[column] = pd.Series()

    def upload_dataframe(self, df: pd.DataFrame, file_data: FileData) -> None:
        if self.can_delete():
            self.delete_by_record_id(file_data=file_data)
        else:
            logger.warning(
                f"table doesn't contain expected "
                f"record id column "
                f"{self.upload_config.record_id_key}, skipping delete"
            )
        df.replace({np.nan: None}, inplace=True)
        self._fit_to_schema(df=df, columns=self.get_table_columns())

        columns = list(df.columns)
        stmt = "INSERT INTO {table_name} ({columns}) VALUES({values})".format(
            table_name=self.upload_config.table_name,
            columns=",".join(columns),
            values=",".join([self.values_delimiter for _ in columns]),
        )
        logger.info(
            f"writing a total of {len(df)} elements via"
            f" document batches to destination"
            f" table named {self.upload_config.table_name}"
            f" with batch size {self.upload_config.batch_size}"
        )
        for rows in split_dataframe(df=df, chunk_size=self.upload_config.batch_size):
            with self.connection_config.get_cursor() as cursor:
                values = self.prepare_data(columns, tuple(rows.itertuples(index=False, name=None)))
                # For debugging purposes:
                # for val in values:
                #     try:
                #         cursor.execute(stmt, val)
                #     except Exception as e:
                #         print(f"Error: {e}")
                #         print(f"failed to write {len(columns)}, {len(val)}: {stmt} -> {val}")
                logger.debug(f"running query: {stmt}")
                cursor.executemany(stmt, values)

    def get_table_columns(self) -> list[str]:
        with self.connection_config.get_cursor() as cursor:
            cursor.execute(f"SELECT * from {self.upload_config.table_name}")
            return [desc[0] for desc in cursor.description]

    def can_delete(self) -> bool:
        return self.upload_config.record_id_key in self.get_table_columns()

    def delete_by_record_id(self, file_data: FileData) -> None:
        logger.debug(
            f"deleting any content with data "
            f"{self.upload_config.record_id_key}={file_data.identifier} "
            f"from table {self.upload_config.table_name}"
        )
        stmt = f"DELETE FROM {self.upload_config.table_name} WHERE {self.upload_config.record_id_key} = {self.values_delimiter}"  # noqa: E501
        with self.connection_config.get_cursor() as cursor:
            cursor.execute(stmt, [file_data.identifier])
            rowcount = cursor.rowcount
            logger.info(f"deleted {rowcount} rows from table {self.upload_config.table_name}")

    def run_data(self, data: list[dict], file_data: FileData, **kwargs: Any) -> None:
        df = pd.DataFrame(data)
        self.upload_dataframe(df=df, file_data=file_data)

    def run(self, path: Path, file_data: FileData, **kwargs: Any) -> None:
        df = get_data_df(path=path)
        self.upload_dataframe(df=df, file_data=file_data)
