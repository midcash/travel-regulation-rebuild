"""Parse the narrow natural-language request supported by the planning page."""
from datetime import date, datetime, time, timedelta
import re

from .domain import CHINA


class RequestInputError(ValueError):
    """A user-correctable request or date error."""


def parse_natural_request(text, captured_at):
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise RequestInputError("请输入不超过 2000 字的会议需求。")
    text = " ".join(text.split())
    if not isinstance(captured_at, datetime):
        raise ValueError("captured_at must be a datetime")
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=CHINA)
    else:
        captured_at = captured_at.astimezone(CHINA)

    origin_match = re.search(r"从\s*([^，,。；;\s]+?)\s*出发", text)
    location_match = re.search(
        r"在\s*([^，,。；;]{2,120}?)\s*(?:召开会议|参加会议|举行会议|开会)", text)
    location_match = location_match or re.search(
        r"会议地点(?:是|为)?\s*([^，,。；;]{2,120})", text)
    if not origin_match or not location_match:
        raise RequestInputError("请说明出发城市，以及会议地点和会议时间。")
    origin = origin_match.group(1).strip()
    meeting_location = location_match.group(1).strip()

    destination_match = re.search(r"(?:目的地(?:是|为)?|前往|去|赴)\s*([\u4e00-\u9fffA-Za-z]{2,12})", text)
    destination = destination_match.group(1).removesuffix("市") if destination_match else ""
    if not destination and "三亚湾" in meeting_location:
        # shortcut: infer only the named Sanya Bay venue; add city extraction when another venue convention is needed.
        destination = "三亚"
    if not destination:
        raise RequestInputError("无法从会场识别目的地，请在请求中补充目的地城市。")

    relative_date = re.search(r"(下周|下星期|下个星期|本周|这周|本星期|这个星期)\s*([一二三四五六日天])", text)
    iso_date = re.search(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", text)
    chinese_date = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", text)
    try:
        if relative_date:
            weekday = "一二三四五六日天".index(relative_date.group(2))
            weekday = min(weekday, 6)
            week_start = captured_at.date() - timedelta(days=captured_at.weekday())
            if relative_date.group(1) in {"下周", "下星期", "下个星期"}:
                week_start += timedelta(days=7)
            meeting_date = week_start + timedelta(days=weekday)
        elif iso_date:
            meeting_date = date(*(int(value) for value in iso_date.groups()))
        elif chinese_date:
            year, month, day = chinese_date.groups()
            meeting_date = date(int(year or captured_at.year), int(month), int(day))
        else:
            raise RequestInputError("请提供明确的会议日期，例如“下周六”。")
    except RequestInputError:
        raise
    except (ValueError, OverflowError) as exc:
        raise RequestInputError("会议日期无效，请更正后重试。") from exc

    time_match = re.search(
        r"(?P<period>早上|上午|中午|下午|晚上)?\s*(?P<hour>\d{1,2})\s*"
        r"(?:(?::|：)(?P<minute>\d{1,2})|点(?:(?P<half>半)|(?P<point_minute>\d{1,2})分?)?)",
        text)
    if not time_match:
        raise RequestInputError("请提供会议开始时间，例如“早上 9 点”。")
    hour = int(time_match.group("hour"))
    minute = int(time_match.group("minute") or time_match.group("point_minute") or (30 if time_match.group("half") else 0))
    period = time_match.group("period")
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period in {"早上", "上午"} and hour == 12:
        hour = 0
    try:
        meeting_at = datetime.combine(meeting_date, time(hour, minute), CHINA)
    except ValueError as exc:
        raise RequestInputError("会议时间无效，请更正后重试。") from exc

    requested_nights = re.search(r"(?:住宿|住)\s*([一二两三四五六七八九十\d]+)\s*晚", text)
    if requested_nights and requested_nights.group(1) not in {"一", "1"}:
        raise RequestInputError("当前请求流程支持住宿一晚，请更正住宿要求。")

    mode = "flight" if re.search(r"飞机|航班", text) else "train" if re.search(r"火车|高铁|动车", text) else "any"
    return {"origin": origin, "destination": destination, "meeting_location": meeting_location,
            "meeting_at": meeting_at.isoformat(timespec="minutes"), "transport_mode": mode,
            "arrive_previous_day": True}


def validate_request_dates(request, captured_at):
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=CHINA)
    else:
        captured_at = captured_at.astimezone(CHINA)
    meeting_at = datetime.fromisoformat(request["meeting_at"])
    if meeting_at.tzinfo is None:
        meeting_at = meeting_at.replace(tzinfo=CHINA)
    else:
        meeting_at = meeting_at.astimezone(CHINA)
    current_date = captured_at.date()
    required_arrival_date = meeting_at.date() - timedelta(days=1) if request.get("arrive_previous_day", True) else meeting_at.date()
    if meeting_at < captured_at or required_arrival_date < current_date:
        raise RequestInputError(
            f"会议或前一日抵达/入住日期早于当前日期 {current_date.isoformat()}，请更正日期或结束本次规划。")
