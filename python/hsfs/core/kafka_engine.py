#
#   Copyright 2024 Hopsworks AB
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO
from typing import TYPE_CHECKING, Any, Callable, Dict, Literal, Optional, Tuple, Union

from hopsworks_common import client
from hopsworks_common.core.constants import (
    HAS_AVRO,
    HAS_CONFLUENT_KAFKA,
    HAS_FAST_AVRO,
    HAS_NUMPY,
    HAS_PANDAS,
    avro_not_installed_message,
)
from hopsworks_common.decorators import uses_confluent_kafka
from hsfs.core import online_ingestion, online_ingestion_api, storage_connector_api
from tqdm import tqdm


if HAS_NUMPY:
    import numpy as np

if HAS_PANDAS:
    import pandas as pd

if HAS_CONFLUENT_KAFKA:
    from confluent_kafka import Consumer, KafkaError, Producer, TopicPartition

if HAS_FAST_AVRO:
    from fastavro import schemaless_writer
    from fastavro.schema import parse_schema
elif HAS_AVRO:
    import avro.io
    import avro.schema


if TYPE_CHECKING:
    from hsfs.feature_group import ExternalFeatureGroup, FeatureGroup


@uses_confluent_kafka
def init_kafka_consumer(
    feature_store_id: int,
    offline_write_options: Dict[str, Any],
) -> Consumer:
    # setup kafka consumer
    consumer_config = get_kafka_config(feature_store_id, offline_write_options)
    if "group.id" not in consumer_config:
        consumer_config["group.id"] = "hsfs_consumer_group"

    return Consumer(consumer_config)


def init_kafka_resources(
    feature_group: Union[FeatureGroup, ExternalFeatureGroup],
    offline_write_options: Dict[str, Any],
    num_entries: Optional[int] = None,
) -> Tuple[
    Producer, Dict[str, bytes], Dict[str, Callable[..., bytes]], Callable[..., bytes] :
]:
    # this function is a caching wrapper around _init_kafka_resources
    if feature_group._multi_part_insert and feature_group._kafka_producer:
        return (
            feature_group._kafka_producer,
            feature_group._kafka_headers,
            feature_group._feature_writers,
            feature_group._writer,
        )
    producer, headers, feature_writers, writer = _init_kafka_resources(
        feature_group, offline_write_options, num_entries
    )
    if feature_group._multi_part_insert:
        feature_group._kafka_producer = producer
        feature_group._kafka_headers = headers
        feature_group._feature_writers = feature_writers
        feature_group._writer = writer
    return producer, headers, feature_writers, writer


def _init_kafka_resources(
    feature_group: Union[FeatureGroup, ExternalFeatureGroup],
    offline_write_options: Dict[str, Any],
    num_entries: Optional[int] = None,
) -> Tuple[
    Producer, Dict[str, bytes], Dict[str, Callable[..., bytes]], Callable[..., bytes] :
]:
    # setup kafka producer
    producer = init_kafka_producer(
        feature_group.feature_store_id, offline_write_options
    )
    # setup headers
    headers = get_headers(feature_group, num_entries)
    # setup writers
    feature_writers, writer = get_writer_function(feature_group)

    return producer, headers, feature_writers, writer


def get_writer_function(
    feature_group: Union[FeatureGroup, ExternalFeatureGroup],
) -> Tuple[Dict[str, Callable[..., bytes]], Callable[..., bytes]]:
    # setup complex feature writers
    feature_writers = {
        feature: get_encoder_func(feature_group._get_feature_avro_schema(feature))
        for feature in feature_group.get_complex_features()
    }
    # setup row writer function
    writer = get_encoder_func(feature_group._get_encoded_avro_schema())
    return (feature_writers, writer)


def get_headers(
    feature_group: Union[FeatureGroup, ExternalFeatureGroup],
    num_entries: Optional[int] = None,
) -> Dict[str, bytes]:
    # custom headers for hopsworks onlineFS
    headers = {
        "projectId": str(feature_group.feature_store.project_id).encode("utf8"),
        "featureGroupId": str(feature_group._id).encode("utf8"),
        "subjectId": str(feature_group.subject["id"]).encode("utf8"),
    }

    if feature_group.online_enabled:
        # setup online ingestion id
        online_ingestion_instance = (
            online_ingestion_api.OnlineIngestionApi().create_online_ingestion(
                feature_group, online_ingestion.OnlineIngestion(num_entries=num_entries)
            )
        )
        headers["onlineIngestionId"] = str(online_ingestion_instance.id).encode("utf8")

    return headers


@uses_confluent_kafka
def init_kafka_producer(
    feature_store_id: int,
    offline_write_options: Dict[str, Any],
) -> Producer:
    # setup kafka producer
    return Producer(get_kafka_config(feature_store_id, offline_write_options))


@uses_confluent_kafka
def kafka_get_offsets(
    topic_name: str,
    feature_store_id: int,
    offline_write_options: Dict[str, Any],
    high: bool,
) -> str:
    consumer = init_kafka_consumer(feature_store_id, offline_write_options)
    topics = consumer.list_topics(
        timeout=offline_write_options.get("kafka_timeout", 6)
    ).topics
    if topic_name in topics.keys():
        # topic exists
        offsets = ""
        tuple_value = int(high)
        for partition_metadata in topics.get(topic_name).partitions.values():
            partition = TopicPartition(
                topic=topic_name, partition=partition_metadata.id
            )
            offsets += f",{partition_metadata.id}:{consumer.get_watermark_offsets(partition)[tuple_value]}"
        consumer.close()

        return f"{topic_name + offsets}"
    return ""


def kafka_produce(
    producer: Producer,
    key: str,
    encoded_row: bytes,
    topic_name: str,
    headers: Dict[str, bytes],
    acked: callable,
    debug_kafka: bool = False,
) -> None:
    while True:
        # if BufferError is thrown, we can be sure, message hasn't been send so we retry
        try:
            # produce
            producer.produce(
                topic=topic_name,
                key=key,
                value=encoded_row,
                callback=acked,
                headers=headers,
            )

            # Trigger internal callbacks to empty op queue
            producer.poll(0)
            break
        except BufferError as e:
            if debug_kafka:
                print("Caught: {}".format(e))
            # backoff for 1 second
            producer.poll(1)


def encode_complex_features(
    feature_writers: Dict[str, callable], row: Dict[str, Any]
) -> Dict[str, Any]:
    for feature_name, writer in feature_writers.items():
        with BytesIO() as outf:
            writer(row[feature_name], outf)
            row[feature_name] = outf.getvalue()
    return row


def get_encoder_func(writer_schema: str) -> callable:
    if HAS_FAST_AVRO:
        schema = json.loads(writer_schema)
        parsed_schema = parse_schema(schema)
        return lambda record, outf: schemaless_writer(outf, parsed_schema, record)

    if not HAS_AVRO:
        raise ModuleNotFoundError(avro_not_installed_message)

    parsed_schema = avro.schema.parse(writer_schema)
    writer = avro.io.DatumWriter(parsed_schema)
    return lambda record, outf: writer.write(record, avro.io.BinaryEncoder(outf))


def encode_row(complex_feature_writers, writer, row):
    # transform special data types
    # here we might need to handle also timestamps and other complex types
    # possible optimizaiton: make it based on type so we don't need to loop over
    # all keys in the row
    if isinstance(row, dict):
        for k in row.keys():
            # for avro to be able to serialize them, they need to be python data types
            if HAS_NUMPY and isinstance(row[k], np.ndarray):
                row[k] = row[k].tolist()
            if HAS_PANDAS and isinstance(row[k], pd.Timestamp):
                row[k] = row[k].to_pydatetime()
            if isinstance(row[k], datetime) and row[k].tzinfo is None:
                row[k] = row[k].replace(tzinfo=timezone.utc)
            if HAS_PANDAS and isinstance(row[k], pd._libs.missing.NAType):
                row[k] = None
    # encode complex features
    row = encode_complex_features(complex_feature_writers, row)
    # encode feature row
    with BytesIO() as outf:
        writer(row, outf)
        encoded_row = outf.getvalue()
    return encoded_row


def get_kafka_config(
    feature_store_id: int,
    write_options: Optional[Dict[str, Any]] = None,
    engine: Literal["spark", "confluent"] = "confluent",
) -> Dict[str, Any]:
    if write_options is None:
        write_options = {}
    external = client._is_external() and not write_options.get("internal_kafka", False)

    storage_connector = storage_connector_api.StorageConnectorApi().get_kafka_connector(
        feature_store_id, external
    )

    if engine == "spark":
        config = storage_connector.spark_options()
        config.update(write_options)
    elif engine == "confluent":
        config = storage_connector.confluent_options()
        config.update(write_options.get("kafka_producer_config", {}))
    return config


@uses_confluent_kafka
def build_ack_callback_and_optional_progress_bar(
    n_rows: int, is_multi_part_insert: bool, offline_write_options: Dict[str, Any]
) -> Tuple[Callable, Optional[tqdm]]:
    if not is_multi_part_insert:
        progress_bar = tqdm(
            total=n_rows,
            bar_format="{desc}: {percentage:.2f}% |{bar}| Rows {n_fmt}/{total_fmt} | "
            "Elapsed Time: {elapsed} | Remaining Time: {remaining}",
            desc="Uploading Dataframe",
            mininterval=1,
        )
    else:
        progress_bar = None

    def acked(err: Exception, msg: Any) -> None:
        if err is not None:
            if offline_write_options.get("debug_kafka", False):
                print("Failed to deliver message: %s: %s" % (str(msg), str(err)))
            if err.code() in [
                KafkaError.TOPIC_AUTHORIZATION_FAILED,
                KafkaError._MSG_TIMED_OUT,
            ]:
                progress_bar.colour = "RED"
                raise err  # Stop producing and show error
        # update progress bar for each msg
        if not is_multi_part_insert:
            progress_bar.update()

    return acked, progress_bar
