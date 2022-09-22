#  Copyright 2022 The Feathub Authors
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
from datetime import datetime, timedelta
from typing import Union, Optional, List, Any, Dict, Sequence, Tuple

import pandas as pd
from pyflink.table import (
    StreamTableEnvironment,
    Table as NativeFlinkTable,
    expressions as native_flink_expr,
)
from pyflink.table.types import DataType

from feathub.common.exceptions import FeathubException, FeathubTransformationException
from feathub.common.types import DType
from feathub.common.utils import to_java_date_format
from feathub.feature_tables.feature_table import FeatureTable
from feathub.feature_views.derived_feature_view import DerivedFeatureView
from feathub.feature_views.feature import Feature
from feathub.feature_views.feature_view import FeatureView
from feathub.feature_views.sliding_feature_view import SlidingFeatureView
from feathub.feature_views.transforms.expression_transform import ExpressionTransform
from feathub.feature_views.transforms.join_transform import JoinTransform
from feathub.feature_views.transforms.over_window_transform import (
    OverWindowTransform,
)
from feathub.feature_views.transforms.sliding_window_transform import (
    SlidingWindowTransform,
)
from feathub.processors.flink.flink_types_utils import to_flink_type
from feathub.processors.flink.table_builder.aggregation_utils import (
    AggregationFieldDescriptor,
    get_default_value_and_type,
)
from feathub.processors.flink.table_builder.flink_table_builder_constants import (
    EVENT_TIME_ATTRIBUTE_NAME,
)
from feathub.processors.flink.table_builder.join_utils import (
    join_table_on_key,
    full_outer_join_on_key_with_default_value,
    temporal_join,
    JoinFieldDescriptor,
)
from feathub.processors.flink.table_builder.over_window_utils import (
    evaluate_over_window_transform,
    OverWindowDescriptor,
)
from feathub.processors.flink.table_builder.sliding_window_utils import (
    evaluate_sliding_window_transform,
    SlidingWindowDescriptor,
)
from feathub.processors.flink.table_builder.source_sink_utils import (
    get_table_from_source,
)
from feathub.registries.registry import Registry
from feathub.table.table_descriptor import TableDescriptor


class FlinkTableBuilder:
    """FlinkTableBuilder is used to convert Feathub feature to a Flink Table."""

    def __init__(
        self,
        t_env: StreamTableEnvironment,
        registry: Registry,
    ):
        """
        Instantiate the FlinkTableBuilder.

        :param t_env: The Flink StreamTableEnvironment under which the Tables to be
                      created.
        :param registry: The Feathub registry.
        """
        self.t_env = t_env
        self.registry = registry

        # Mapping from the name of TableDescriptor to the TableDescriptor and the built
        # NativeFlinkTable. This is used as a cache to avoid re-computing the native
        # flink table from the same TableDescriptor.
        self._built_tables: Dict[str, Tuple[TableDescriptor, NativeFlinkTable]] = {}

    def build(
        self,
        features: TableDescriptor,
        keys: Union[pd.DataFrame, TableDescriptor, None] = None,
        start_datetime: Optional[datetime] = None,
        end_datetime: Optional[datetime] = None,
    ) -> NativeFlinkTable:
        """
        Convert the given features to native Flink table.

        If the given features is a FeatureView, it must be resolved, otherwise
        exception will be thrown.

        :param features: The feature to converts to native Flink table.
        :param keys: Optional. If it is not none, then the returned table only includes
                     rows whose key fields match at least one row of the keys.
        :param start_datetime: Optional. If it is not None, the `features` table should
                               have a timestamp field. And the output table will only
                               include features whose
                               timestamp >= start_datetime. If any field (e.g. minute)
                               is not specified in the start_datetime, we assume this
                               field has the minimum possible value.
        :param end_datetime: Optional. If it is not None, the `features` table should
                             have a timestamp field. And the output table will only
                             include features whose timestamp < end_datetime. If any
                             field (e.g. minute) is not specified in the end_datetime,
                             we assume this field has the maximum possible value.
        :return: The native Flink table that represents the given features.
        """
        if isinstance(features, FeatureView) and features.is_unresolved():
            raise FeathubException(
                "Trying to convert a unresolved FeatureView to native Flink table."
            )

        table = self._get_table(features)

        if keys is not None:
            table = self._filter_table_by_keys(table, keys)

        if start_datetime is not None or end_datetime is not None:
            if features.timestamp_field is None:
                raise FeathubException(
                    "Feature is missing timestamp_field. It cannot be ranged "
                    "by start_datetime."
                )
            table = self._range_table_by_time(table, start_datetime, end_datetime)

        if EVENT_TIME_ATTRIBUTE_NAME in table.get_schema().get_field_names():
            table = table.drop_columns(EVENT_TIME_ATTRIBUTE_NAME)

        return table

    def _filter_table_by_keys(
        self,
        table: NativeFlinkTable,
        keys: Union[pd.DataFrame, TableDescriptor],
    ) -> NativeFlinkTable:
        if keys is not None:
            key_table = self._get_table(keys)
            for field_name in key_table.get_schema().get_field_names():
                if field_name not in table.get_schema().get_field_names():
                    raise FeathubException(
                        f"Given key {field_name} not in the table fields "
                        f"{table.get_schema().get_field_names()}."
                    )
            table = join_table_on_key(
                key_table, table, key_table.get_schema().get_field_names()
            )
        return table

    def _get_table(
        self, features: Union[TableDescriptor, pd.DataFrame]
    ) -> NativeFlinkTable:
        if isinstance(features, pd.DataFrame):
            return self.t_env.from_pandas(features)

        if features.name in self._built_tables:
            if features != self._built_tables[features.name][0]:
                raise FeathubException(
                    f"Encounter different TableDescriptor with same name. {features} "
                    f"and {self._built_tables[features.name][0]}."
                )
            return self._built_tables[features.name][1]

        if isinstance(features, FeatureTable):
            self._built_tables[features.name] = (
                features,
                get_table_from_source(self.t_env, features),
            )
        elif isinstance(features, DerivedFeatureView):
            self._built_tables[features.name] = (
                features,
                self._get_table_from_derived_feature_view(features),
            )
        elif isinstance(features, SlidingFeatureView):
            self._built_tables[features.name] = (
                features,
                self._get_table_from_sliding_feature_view(features),
            )
        else:
            raise FeathubException(
                f"Unsupported type '{type(features).__name__}' for '{features}'."
            )

        return self._built_tables[features.name][1]

    def _get_table_from_derived_feature_view(
        self, feature_view: DerivedFeatureView
    ) -> NativeFlinkTable:
        source_table = self._get_table(feature_view.source)
        source_fields = list(source_table.get_schema().get_field_names())
        dependent_features = self._get_dependent_features(feature_view)
        tmp_table = source_table

        window_agg_map: Dict[
            OverWindowDescriptor, List[AggregationFieldDescriptor]
        ] = {}

        table_names = set(
            [
                feature.transform.table_name
                for feature in feature_view.get_resolved_features()
                if isinstance(feature.transform, JoinTransform)
            ]
        )

        table_by_names = {}
        descriptors_by_names = {}
        for name in table_names:
            descriptor = self.registry.get_features(name=name)
            descriptors_by_names[name] = descriptor
            table_by_names[name] = self._get_table(features=descriptor)

        # The right_tables map keeps track of the information of the right table to join
        # with the source table. The key is a tuple of right_table_name and join_keys
        # and the value is a map from the name of the field of the right table
        # to join to JoinFieldDescriptor.
        right_tables: Dict[
            Tuple[str, Sequence[str]], Dict[str, JoinFieldDescriptor]
        ] = {}

        for feature in dependent_features:
            if feature.name in tmp_table.get_schema().get_field_names():
                continue
            if isinstance(feature.transform, ExpressionTransform):
                tmp_table = self._evaluate_expression_transform(
                    tmp_table,
                    feature.transform,
                    feature.name,
                    feature.dtype,
                )
            elif isinstance(feature.transform, OverWindowTransform):
                if feature_view.timestamp_field is None:
                    raise FeathubException(
                        "FeatureView must have timestamp field for OverWindowTransform."
                    )
                transform = feature.transform
                window_aggs = window_agg_map.setdefault(
                    OverWindowDescriptor.from_over_window_transform(transform),
                    [],
                )
                window_aggs.append(AggregationFieldDescriptor.from_feature(feature))
            elif isinstance(feature.transform, JoinTransform):
                if feature.keys is None:
                    raise FeathubException(
                        f"FlinkProcessor cannot join feature {feature} without key."
                    )
                if not all(
                    key in source_table.get_schema().get_field_names()
                    for key in feature.keys
                ):
                    raise FeathubException(
                        f"Source table {source_table.get_schema().get_field_names()} "
                        f"doesn't have the keys of the Feature to join {feature.keys}."
                    )

                join_transform = feature.transform
                right_table_descriptor = descriptors_by_names[join_transform.table_name]
                right_timestamp_field = right_table_descriptor.timestamp_field
                if right_timestamp_field is None:
                    raise FeathubException(
                        f"FlinkProcessor cannot join with {right_table_descriptor} "
                        f"without timestamp field."
                    )
                right_table_join_field_descriptors = right_tables.setdefault(
                    (join_transform.table_name, tuple(feature.keys)), dict()
                )
                right_table_join_field_descriptors.update(
                    {
                        key: JoinFieldDescriptor.from_field_name(key)
                        for key in feature.keys
                    }
                )
                right_table_join_field_descriptors[
                    right_timestamp_field
                ] = JoinFieldDescriptor.from_field_name(right_timestamp_field)

                right_table_join_field_descriptors[
                    EVENT_TIME_ATTRIBUTE_NAME
                ] = JoinFieldDescriptor.from_field_name(EVENT_TIME_ATTRIBUTE_NAME)

                right_table_join_field_descriptors[
                    join_transform.feature_name
                ] = JoinFieldDescriptor.from_table_descriptor_and_field_name(
                    right_table_descriptor, join_transform.feature_name
                )
            else:
                raise FeathubTransformationException(
                    f"Unsupported transformation type "
                    f"{type(feature.transform).__name__} for feature {feature.name}."
                )

        for over_window_descriptor, agg_descriptor in window_agg_map.items():
            tmp_table = evaluate_over_window_transform(
                tmp_table,
                over_window_descriptor,
                agg_descriptor,
            )

        for (
            right_table_name,
            keys,
        ), right_table_join_field_descriptors in right_tables.items():
            right_table = table_by_names[right_table_name].select(
                *[
                    native_flink_expr.col(right_table_field)
                    for right_table_field in right_table_join_field_descriptors.keys()
                ]
            )
            tmp_table = temporal_join(
                self.t_env,
                tmp_table,
                right_table,
                keys,
                right_table_join_field_descriptors,
            )

        output_fields = self._get_output_fields(feature_view, source_fields)
        return tmp_table.select(
            *[native_flink_expr.col(field) for field in output_fields]
        )

    def _get_table_from_sliding_feature_view(
        self, feature_view: SlidingFeatureView
    ) -> NativeFlinkTable:
        source_table = self._get_table(feature_view.source)
        source_fields = source_table.get_schema().get_field_names()

        dependent_features = self._get_dependent_features(feature_view)

        tmp_table = source_table
        sliding_window_agg_map: Dict[
            SlidingWindowDescriptor, List[AggregationFieldDescriptor]
        ] = {}

        for feature in dependent_features:
            if feature.name in tmp_table.get_schema().get_field_names():
                continue
            if isinstance(feature.transform, ExpressionTransform):
                tmp_table = self._evaluate_expression_transform(
                    tmp_table,
                    feature.transform,
                    feature.name,
                    feature.dtype,
                )
            elif isinstance(feature.transform, SlidingWindowTransform):
                if feature_view.timestamp_field is None:
                    raise FeathubException(
                        "FeatureView must have timestamp field for "
                        "SlidingWindowTransform."
                    )
                transform = feature.transform
                window_aggs = sliding_window_agg_map.setdefault(
                    SlidingWindowDescriptor.from_sliding_window_transform(transform),
                    [],
                )
                window_aggs.append(AggregationFieldDescriptor.from_feature(feature))
            else:
                raise FeathubTransformationException(
                    f"Unsupported transformation type "
                    f"{type(feature.transform).__name__} for feature {feature.name}."
                )

        agg_table = None
        field_default_value: Dict[str, Tuple[Any, DataType]] = {}
        for window_descriptor, agg_descriptors in sliding_window_agg_map.items():
            for agg_descriptor in agg_descriptors:
                field_default_value[
                    agg_descriptor.field_name
                ] = get_default_value_and_type(agg_descriptor)
            tmp_agg_table = evaluate_sliding_window_transform(
                self.t_env,
                tmp_table,
                window_descriptor,
                agg_descriptors,
            )
            if agg_table is None:
                agg_table = tmp_agg_table
            else:
                join_keys = list(window_descriptor.group_by_keys)
                join_keys.append(EVENT_TIME_ATTRIBUTE_NAME)
                agg_table = full_outer_join_on_key_with_default_value(
                    agg_table,
                    tmp_agg_table,
                    join_keys,
                    field_default_value,
                )

        if agg_table is not None:
            tmp_table = agg_table

        # Add the timestamp field according to the timestamp format from
        # event time(window time).
        if feature_view.timestamp_field is not None:
            if feature_view.timestamp_format == "epoch":
                tmp_table = tmp_table.add_columns(
                    native_flink_expr.call_sql(
                        f"UNIX_TIMESTAMP(CAST(`{EVENT_TIME_ATTRIBUTE_NAME}` "
                        f"AS STRING))"
                    ).alias(feature_view.timestamp_field)
                )
            else:
                java_datetime_format = to_java_date_format(
                    feature_view.timestamp_format
                ).replace(
                    "'", "''"  # Escape single quote for sql
                )
                tmp_table = tmp_table.add_columns(
                    native_flink_expr.call_sql(
                        f"DATE_FORMAT(`{EVENT_TIME_ATTRIBUTE_NAME}`, "
                        f"'{java_datetime_format}')"
                    ).alias(feature_view.timestamp_field)
                )

        output_fields = self._get_output_fields(feature_view, source_fields)
        return tmp_table.select(
            *[native_flink_expr.col(field) for field in output_fields]
        )

    @staticmethod
    def _get_dependent_features(feature_view: FeatureView) -> List[Feature]:
        dependent_features = []
        for feature in feature_view.get_resolved_features():
            for input_feature in feature.input_features:
                if input_feature not in dependent_features:
                    dependent_features.append(input_feature)
            if feature not in dependent_features:
                dependent_features.append(feature)
        return dependent_features

    @staticmethod
    def _evaluate_expression_transform(
        source_table: NativeFlinkTable,
        transform: ExpressionTransform,
        result_field_name: str,
        result_type: DType,
    ) -> NativeFlinkTable:
        result_type = to_flink_type(result_type)
        return source_table.add_or_replace_columns(
            native_flink_expr.call_sql(transform.expr)
            .cast(result_type)
            .alias(result_field_name)
        )

    def _range_table_by_time(
        self,
        table: NativeFlinkTable,
        start_datetime: Optional[datetime],
        end_datetime: Optional[datetime],
    ) -> NativeFlinkTable:
        if start_datetime is not None:
            table = table.filter(
                native_flink_expr.col(EVENT_TIME_ATTRIBUTE_NAME).__ge__(
                    native_flink_expr.lit(
                        start_datetime.strftime("%Y-%m-%d %H:%M:%S")
                    ).to_timestamp
                )
            )
        if end_datetime is not None:
            table = table.filter(
                native_flink_expr.col(EVENT_TIME_ATTRIBUTE_NAME).__lt__(
                    native_flink_expr.lit(
                        end_datetime.strftime("%Y-%m-%d %H:%M:%S")
                    ).to_timestamp
                )
            )
        return table

    def _get_output_fields(
        self, feature_view: FeatureView, source_fields: List[str]
    ) -> List[str]:
        output_fields = feature_view.get_output_fields(source_fields)
        if EVENT_TIME_ATTRIBUTE_NAME not in output_fields:
            output_fields.append(EVENT_TIME_ATTRIBUTE_NAME)
        return output_fields

    @staticmethod
    def _get_feature_valid_time(
        table_descriptor: TableDescriptor, feature_name: str
    ) -> Optional[timedelta]:
        if not isinstance(table_descriptor, SlidingFeatureView):
            return None

        right_feature_transform = table_descriptor.get_feature(feature_name).transform

        if not isinstance(right_feature_transform, SlidingWindowTransform):
            return None

        return right_feature_transform.step_size