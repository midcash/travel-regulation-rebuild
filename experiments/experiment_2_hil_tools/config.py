"""只读取本实验需要的两个密钥和四个端点。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

DEFAULT_AMAP_DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"
DEFAULT_TUNIU_FLIGHT_URL = "https://openapi.tuniu.cn/mcp/flight"
DEFAULT_TUNIU_TRAIN_URL = "https://openapi.tuniu.cn/mcp/train"
DEFAULT_TUNIU_HOTEL_URL = "https://openapi.tuniu.cn/mcp/hotel"


@dataclass(frozen=True)
class Settings:
    amap_api_key: str | None = field(default=None, repr=False)
    tuniu_api_key: str | None = field(default=None, repr=False)
    amap_driving_url: str = DEFAULT_AMAP_DRIVING_URL
    tuniu_flight_url: str = DEFAULT_TUNIU_FLIGHT_URL
    tuniu_train_url: str = DEFAULT_TUNIU_TRAIN_URL
    tuniu_hotel_url: str = DEFAULT_TUNIU_HOTEL_URL

    def __post_init__(self) -> None:
        for name in (f.name for f in fields(self) if f.name.endswith("_url")):
            url = urlsplit(getattr(self, name))
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    f"{name} must be an HTTPS endpoint without credentials, query or fragment"
                )


def load_settings(env_file: Path | None = None) -> Settings:
    """环境变量优先；默认使用实验目录的 .env，不依赖工作目录。"""
    path = env_file or Path(__file__).with_name(".env")
    values = dotenv_values(path, interpolate=False) if path.is_file() else {}
    kwargs = {}
    for item in fields(Settings):
        key = item.name.upper()
        value = os.environ.get(key, values.get(key))
        if value:
            kwargs[item.name] = value.strip()
    return Settings(**kwargs)
