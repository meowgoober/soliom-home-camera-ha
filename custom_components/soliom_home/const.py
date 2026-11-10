"""Constants for the Soliom Home integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "soliom_home"
NAME = "Soliom Home"
VERSION = "1.1.0"

# Cloud API constants extracted from Soliom-Home 1.2.23 (Xiongmai XM cloud).
APP_KEY = "uzDAl80Pmm0Fj1hwD9"
APP_SECRET = "ptozF68ukKXHcGUNF7"
HOST_DISCOVERY_URL = "https://root.xmitech.net/v1/api/app/host"
DEFAULT_HOST = "https://app-us.xmitech.net"
API_VERSION = "1.0"
API_LANG = "en-US"
USER_AGENT = "Android/1.2.23/Soliom/1.0"
AES_IV = b"Xmitech_aes_veri"

ACTION_LOGIN = "UserMultiLoginV2"
ACTION_REFRESH = "UserMultiTokenRefresh"
ACTION_DEVICE_LIST = "GetUserDeviceList"
ACTION_EVENT_LIST = "GetEventList"
ACTION_EVENT_UNREAD = "GetEventUnReadList"
ACTION_PRODUCT_EVENT_TYPES = "GetProductEventTypes"
ACTION_MQTT_CONFIG = "GetUserMQTTS"

# Config / options.
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_REGION = "region"
CONF_COUNTRY_CODE = "country_code"
CONF_HOST = "host"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEVICES = "devices"
CONF_LOCAL_HOST = "local_host"
CONF_RTSP_PORT = "rtsp_port"
CONF_RTSP_USERNAME = "rtsp_username"
CONF_RTSP_PASSWORD = "rtsp_password"
CONF_RTSP_CHANNEL = "rtsp_channel"
CONF_RTSP_STREAM = "rtsp_stream"
CONF_RTSP_URL = "rtsp_url"
CONF_SNAPSHOT_URL = "snapshot_url"
CONF_LOCAL_HTTPS = "local_https"
CONF_VERIFY_SSL = "verify_ssl"

DEFAULT_REGION = "US"
DEFAULT_COUNTRY_CODE = 1
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 600
DEFAULT_RTSP_PORT = 554
DEFAULT_RTSP_USERNAME = "admin"
DEFAULT_RTSP_CHANNEL = 1
DEFAULT_RTSP_STREAM = 0

DEFAULT_EVENT_HOURS = 24
DEFAULT_EVENT_LIMIT = 100
MOTION_ACTIVE_WINDOW = timedelta(minutes=5)
EVENT_SCAN_OVERLAP = timedelta(minutes=10)
SNAPSHOT_CACHE_SECONDS = 10

# Local Xiongmai CGI endpoints (standard XM firmware).
CGI_SNAPSHOT = "/cgi-bin/snapshot.cgi?channel={channel}"
CGI_PTZ = (
    "/cgi-bin/ptz.cgi?action={action}&channel={channel}"
    "&code={code}&arg1=0&arg2={speed}&arg3={preset}"
)
CGI_REBOOT = "/cgi-bin/magicBox.cgi?action=reboot"
CGI_CONFIG_GET = "/cgi-bin/configManager.cgi?action=getConfig&name={name}"
CGI_CONFIG_SET = "/cgi-bin/configManager.cgi?action=setConfig&{setting}"

PTZ_COMMANDS: dict[str, str] = {
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "zoom_in": "ZoomIn",
    "zoom_out": "ZoomOut",
    "focus_near": "FocusNear",
    "focus_far": "FocusFar",
    "iris_small": "IrisSmall",
    "iris_large": "IrisLarge",
    "preset": "GotoPreset",
    "set_preset": "SetPreset",
    "clear_preset": "ClearPreset",
    "tour_start": "StartTour",
    "tour_stop": "StopTour",
}

EVENT_TYPE_NAMES: dict[int, str] = {
    1: "motion",
    2: "doorbell",
    3: "human",
    4: "face",
    5: "vehicle",
    6: "pet",
    7: "package",
    8: "cry",
    9: "line_crossing",
    10: "area_intrusion",
    11: "tamper",
    1007: "motion",
    1008: "motion",
    1009: "motion",
    1010: "motion",
    1011: "motion",
}

MANUFACTURER = "Soliom"

# Common ISO 3166-1 alpha-2 -> international calling code.  The app sends the
# calling code to the host-discovery endpoint to pick a regional cluster.
COUNTRY_CALLING_CODES: dict[str, int] = {
    "AU": 61,
    "AT": 43,
    "BE": 32,
    "BR": 55,
    "CA": 1,
    "CH": 41,
    "CL": 56,
    "CN": 86,
    "CZ": 420,
    "DE": 49,
    "DK": 45,
    "ES": 34,
    "FI": 358,
    "FR": 33,
    "GB": 44,
    "GR": 30,
    "HK": 852,
    "HU": 36,
    "IE": 353,
    "IL": 972,
    "IN": 91,
    "IT": 39,
    "JP": 81,
    "KR": 82,
    "MX": 52,
    "MY": 60,
    "NL": 31,
    "NO": 47,
    "NZ": 64,
    "PL": 48,
    "PT": 351,
    "RO": 40,
    "RU": 7,
    "SA": 966,
    "SE": 46,
    "SG": 65,
    "SK": 421,
    "TH": 66,
    "TR": 90,
    "TW": 886,
    "UA": 380,
    "US": 1,
    "ZA": 27,
}
