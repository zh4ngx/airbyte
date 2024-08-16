#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import datetime
import time
from dataclasses import InitVar, dataclass
from typing import Any, Iterable, Mapping, Optional, Union

import requests
from airbyte_cdk.sources.declarative.auth.declarative_authenticator import DeclarativeAuthenticator
from airbyte_cdk.sources.declarative.auth.token import BasicHttpAuthenticator
from airbyte_cdk.sources.declarative.incremental import DeclarativeCursor
from airbyte_cdk.sources.declarative.interpolation.interpolated_string import InterpolatedString
from airbyte_cdk.sources.declarative.stream_slicers import CartesianProductStreamSlicer
from airbyte_cdk.sources.declarative.types import Config, Record, StreamSlice, StreamState
from isodate import Duration, parse_duration


@dataclass
class ShortLivedTokenAuthenticator(DeclarativeAuthenticator):
    """
    [Low-Code Custom Component] ShortLivedTokenAuthenticator
    https://github.com/airbytehq/airbyte/issues/22872

    https://docs.railz.ai/reference/authentication
    """

    client_id: Union[InterpolatedString, str]
    secret_key: Union[InterpolatedString, str]
    url: Union[InterpolatedString, str]
    config: Config
    parameters: InitVar[Mapping[str, Any]]
    token_key: Union[InterpolatedString, str] = "access_token"
    lifetime: Union[InterpolatedString, str] = "PT3600S"

    def __post_init__(self, parameters: Mapping[str, Any]):
        self._client_id = InterpolatedString.create(self.client_id, parameters=parameters)
        self._secret_key = InterpolatedString.create(self.secret_key, parameters=parameters)
        self._url = InterpolatedString.create(self.url, parameters=parameters)
        self._token_key = InterpolatedString.create(self.token_key, parameters=parameters)
        self._lifetime = InterpolatedString.create(self.lifetime, parameters=parameters)
        self._basic_auth = BasicHttpAuthenticator(
            username=self._client_id,
            password=self._secret_key,
            config=self.config,
            parameters=parameters,
        )
        self._session = requests.Session()
        self._token = None
        self._timestamp = None

    @classmethod
    def _parse_timedelta(cls, time_str) -> Union[datetime.timedelta, Duration]:
        """
        :return Parses an ISO 8601 durations into datetime.timedelta or Duration objects.
        """
        if not time_str:
            return datetime.timedelta(0)
        return parse_duration(time_str)

    def check_token(self):
        now = time.time()
        url = self._url.eval(self.config)
        token_key = self._token_key.eval(self.config)
        lifetime = self._parse_timedelta(self._lifetime.eval(self.config))
        if not self._token or now - self._timestamp >= lifetime.seconds:
            response = self._session.get(url, headers=self._basic_auth.get_auth_header())
            response.raise_for_status()
            response_json = response.json()
            if token_key not in response_json:
                raise Exception(f"token_key: '{token_key}' not found in response {url}")
            self._token = response_json[token_key]
            self._timestamp = now

    @property
    def auth_header(self) -> str:
        return "Authorization"

    @property
    def token(self) -> str:
        self.check_token()
        return f"Bearer {self._token}"


@dataclass
class NestedStateCartesianProductStreamSlicer(DeclarativeCursor, CartesianProductStreamSlicer):
    """
    [Low-Code Custom Component] NestedStateCartesianProductStreamSlicer
    https://github.com/airbytehq/airbyte/issues/22873

    Some streams require support of nested state:
    {
      "accounting_transactions": {
        "Business1": {
          "dynamicsBusinessCentral": {
            "postedDate": "2022-12-28T00:00:00.000Z"
          }
        },
        "Business2": {
          "oracleNetsuite": {
            "postedDate": "2022-12-28T00:00:00.000Z"
          }
        }
      }
    }
    """

    def __post_init__(self, parameters: Mapping[str, Any]):
        self._cursor = {}
        self._parameters = parameters

    def get_stream_state(self) -> Mapping[str, Any]:
        return self._cursor

    def set_initial_state(self, stream_state: StreamState) -> None:
        self._cursor = stream_state

    def close_slice(self, stream_slice: StreamSlice, most_recent_record: Optional[Record]) -> None:
        connection_id = str(stream_slice.get("connectionId", ""))
        if connection_id and most_recent_record:
            current_cursor_value = self._cursor.get(connection_id, {}).get("updatedAt", "")
            new_cursor_value = most_recent_record.get("updatedAt", "")

            self._cursor[connection_id] = {"updatedAt": max(current_cursor_value, new_cursor_value)}

    def stream_slices(self) -> Iterable[StreamSlice]:
        connections_slicer, datetime_slicer = self.stream_slicers
        for connection_slice in connections_slicer.stream_slices():
            connection_id = str(stream_slice.get("connectionId", ""))

            connection_state = self._cursor.get(connection_id, {})

            datetime_slicer.set_initial_state(connection_state)
            for datetime_slice in datetime_slicer.stream_slices():
                datetime_slice["connectionId"] = connection_id
                yield connection_slice | datetime_slice

    def should_be_synced(self, record: Record) -> bool:
        """
        As of 2023-06-28, the expectation is that this method will only be used for semi-incremental and data feed and therefore the
        implementation is irrelevant for railz
        """
        return True

    def is_greater_than_or_equal(self, first: Record, second: Record) -> bool:
        """
        Evaluating which record is greater in terms of cursor. This is used to avoid having to capture all the records to close a slice
        """
        first_cursor_value = first.get("updatedAt")
        second_cursor_value = second.get("updatedAt")
        if first_cursor_value and second_cursor_value:
            return first_cursor_value >= second_cursor_value
        elif first_cursor_value:
            return True
        else:
            return False
