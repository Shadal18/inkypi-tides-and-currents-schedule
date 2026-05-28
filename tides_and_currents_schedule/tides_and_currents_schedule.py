from plugins.base_plugin.base_plugin import BasePlugin
import requests
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET


logger = logging.getLogger(__name__)


COOPS_STATIONS_XML_URL = "https://opendap.co-ops.nos.noaa.gov/stations/stationsXML.jsp"

COOPS_TIDE_PREDICTION_URL = (
    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?"
    "station={station_id}&product=predictions&datum=MLLW&units=english&time_zone=lst_ldt&"
    "format=json&begin_date={begin_date}&end_date={end_date}&interval=hilo"
)

COOPS_WATER_LEVEL_URL = (
    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?"
    "station={station_id}&product=water_level&datum=MLLW&units=english&time_zone=lst_ldt&"
    "format=json&begin_date={begin_date}&end_date={end_date}"
)


class TidesSchedule(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        template_params["station_id"] = {
            "required": True,
            "description": "Select NOAA CO-OPS Station ID for tide predictions",
        }
        template_params["time_zone"] = {
            "required": False,
            "description": "Timezone for display (default from device config)",
            "example": "America/New_York",
        }
        template_params["begin_date"] = {
            "required": False,
            "description": "Start date for tide predictions in YYYYMMDD format (default today)",
            "example": "20251029",
        }
        template_params["end_date"] = {
            "required": False,
            "description": "End date for tide predictions in YYYYMMDD format (default today)",
            "example": "20251029",
        }
        template_params["title"] = {
            "required": False,
            "description": "Custom title for the display",
            "example": "Local Tide Schedule",
        }
        return template_params

    def generate_image(self, settings, device_config):
        stations_list = self.get_stations_list()

        station_id = str(settings.get("station_id") or "").strip()
        if not station_id:
            raise RuntimeError("Station ID is required.")

        tz_name = settings.get("time_zone") or device_config.get_config(
            "timezone", default="America/New_York"
        )
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:
            raise RuntimeError(f"Invalid timezone: {tz_name}") from exc

        begin_date = str(settings.get("begin_date") or "").strip()
        end_date = str(settings.get("end_date") or "").strip()

        now_local = datetime.now(tz)

        if not begin_date:
            begin_date = now_local.strftime("%Y%m%d")
        if not end_date:
            end_date = begin_date

        start_date_graph = (now_local - timedelta(days=1)).strftime("%Y%m%d")
        end_date_graph = now_local.strftime("%Y%m%d")

        title = str(settings.get("title") or "").strip() or "Tide Schedule"

        try:
            tide_data = self.get_tide_predictions(station_id, begin_date, end_date)
            parsed_tides = self.parse_tide_data(tide_data)

            water_level_data = self.get_water_level(station_id, start_date_graph, end_date_graph)
            parsed_graph = self.parse_water_level_data(water_level_data)

        except Exception as e:
            logger.error("Failed to get tide data: %s", str(e))
            raise RuntimeError(f"Failed to retrieve tide data: {str(e)}") from e

        template_params = {
            "title": title,
            "tides": parsed_tides,
            "tide_graph": parsed_graph,
            "plugin_settings": settings,
            "last_refresh_time": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "stations_list": stations_list,
        }

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        image = self.render_image(
            dimensions,
            "tides_and_currents_schedule.html",
            "tides_and_currents_schedule.css",
            template_params,
        )
        if not image:
            raise RuntimeError("Failed to render tide schedule image.")
        return image

    def _get_json(self, url, error_prefix):
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            logger.error("%s timed out", error_prefix)
            raise RuntimeError(f"{error_prefix} timed out.") from exc
        except requests.exceptions.HTTPError as exc:
            content = exc.response.text if exc.response is not None else "No response content"
            logger.error("%s HTTP error: %s", error_prefix, content)
            raise RuntimeError(f"{error_prefix} failed with an HTTP error.") from exc
        except requests.exceptions.RequestException as exc:
            logger.error("%s request failed: %s", error_prefix, exc)
            raise RuntimeError(f"{error_prefix} request failed.") from exc

        try:
            data = response.json()
        except ValueError as exc:
            logger.error("%s returned invalid JSON", error_prefix)
            raise RuntimeError(f"{error_prefix} returned invalid data.") from exc

        if isinstance(data, dict) and data.get("error"):
            message = data["error"].get("message", "Unknown NOAA API error.")
            logger.error("%s NOAA error: %s", error_prefix, message)
            raise RuntimeError(message)

        return data

    def get_stations_list(self):
        try:
            response = requests.get(COOPS_STATIONS_XML_URL, timeout=15)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            station_list = []
            for station in root.findall(".//station"):
                station_id = station.findtext("id")
                station_name = station.findtext("name")
                if station_id and station_name:
                    station_list.append({
                        "id": station_id,
                        "name": station_name,
                    })
            return station_list
        except Exception as e:
            logger.error("Failed fetching station list: %s", e)
            return []

    def get_tide_predictions(self, station_id, begin_date, end_date):
        url = COOPS_TIDE_PREDICTION_URL.format(
            station_id=station_id,
            begin_date=begin_date,
            end_date=end_date,
        )
        return self._get_json(url, "Tide predictions request")

    def parse_tide_data(self, data):
        tides = []
        for entry in data.get("predictions", []):
            try:
                dt = datetime.strptime(entry["t"], "%Y-%m-%d %H:%M")
                tides.append({
                    "time": dt.strftime("%I:%M %p"),
                    "height": float(entry["v"]),
                    "type": "High" if entry.get("type") == "H" else "Low",
                })
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping invalid tide prediction entry: %r (%s)", entry, exc)
        return tides

    def get_water_level(self, station_id, begin_date, end_date):
        url = COOPS_WATER_LEVEL_URL.format(
            station_id=station_id,
            begin_date=begin_date,
            end_date=end_date,
        )
        return self._get_json(url, "Water level request")

    def parse_water_level_data(self, data):
        timestamps = []
        levels = []

        for entry in data.get("data", []):
            try:
                dt = datetime.strptime(entry["t"], "%Y-%m-%d %H:%M")
                timestamps.append(dt.strftime("%I:%M %p"))
                levels.append(float(entry["v"]))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping invalid water level entry: %r (%s)", entry, exc)

        return {
            "timestamps": timestamps,
            "levels": levels,
        }