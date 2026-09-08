"""Constants for the AigoSmart cloud (Alibaba IoT) and local (ALCS/CoAP) protocols.

All values extracted from AigoSmart APK 2.15.6 (com.aigostar.smart).
Source class: com.aigostar.lib.aigo.api.constants.CommonValueApiConstant
"""

# ---------------------------------------------------------------------------
# Aigostar User Center (UC)
# ---------------------------------------------------------------------------
UC_BASE = "https://uc.aigostar.com"
UC_LOGIN_PATH = "/v1.0/connect/token"
UC_AUTHORIZE_PATH = "/v1.0/connect/authorize"

UC_APP_KEY = "smart-android-v1"       # header AppKey used by the OkHttp interceptor
UC_TENANT_ID = "1000"

# OAuth client credentials (public app credentials, not user secrets)
CLIENT_ID = "C28098DEE9664BABBB9AE8E6E47505B0"          # CommonValueApiConstant.APP
CLIENT_SECRET = "C3575D1E-7A5F-411F-920D-5C469AA53AB7"   # CLIENT_SECRET_VALEUE
SMARTAPP_ID = "smartapp"

# AES-256-CBC key for password encryption (SHA1 signing cert + "0000")
AES_KEY = "tCx8BA0yKVr+NbBChH928URAV90=0000"

# ---------------------------------------------------------------------------
# Verification-code endpoints (smartapi)
# ---------------------------------------------------------------------------
SMART_API_BASE = "https://smartapi.aigostar.com"
PATH_SEND_CODE = "/message/v1.1/security/sendcode/anonymous"
PATH_VERIFY_CODE = "/message/v1.1/security/verify/anonymous"

# ---------------------------------------------------------------------------
# Alibaba IoT API gateway
# ---------------------------------------------------------------------------
DEFAULT_IOT_HOST = "eu-central-1.api-iot.aliyuncs.com"
REGION_API_HOST = "https://api.link.aliyun.com"
REGION_API_PATH = "/living/account/region/get"          # apiVer 1.0.2
OA_LOGIN_PATH = "/api/prd/loginbyoauth.json"
OA_HOST_FALLBACK = "living-account.eu-central-1.aliyuncs.com"

APP_KEY = "28770785"                                     # CommonValueApiConstant.APPKEY
APP_SECRET = "41fd4a1eb18fa7ace5e2abbbe3867f93"          # APP_SECRT_AIGOSMART

# API paths (apiVer in parentheses, from APK GateWayRequest subclasses)
PATH_CREATE_SESSION = "/account/createSessionByAuthCode"  # 1.0.4
PATH_REFRESH_SESSION = "/account/checkOrRefreshSession"    # 1.0.4
PATH_LIST_DEVICES = "/uc/listBindingByAccount"            # 1.0.8
PATH_GET_BY_ACCOUNT_AND_DEV = "/uc/getByAccountAndDev"     # 1.0.2
PATH_PROPS_GET = "/thing/properties/get"                  # 1.0.0
PATH_PROPS_SET = "/thing/properties/set"                  # 1.0.0
PATH_TSL_GET = "/thing/tsl/get"                           # 1.0.0
PATH_STATUS_GET = "/thing/status/get"                      # 1.0.0
PATH_SERVICE_INVOKE = "/thing/service/invoke"
PATH_BATCH_PROPS_SET = "/living/device/properties/batch/set"

# --- Device provisioning / enrollee discovery (from APK BreezeHelper) -------
PATH_AWSS_ENROLLEE_LIST = "/awss/enrollee/list/get"
PATH_AWSS_PRODUCT_FILTER = "/awss/enrollee/product/filter"
PATH_AWSS_LCA_PRODUCT_LIST = "/awss/enrollee/lca/product/list"
PATH_AWSS_BLE_USER_BIND = "/awss/ble/user/bind"
PATH_AWSS_TIME_WINDOW_BIND = "/awss/time/window/user/bind"
PATH_AWSS_SUBDEVICE_UNBIND = "/awss/subdevice/unbind"
PATH_AWSS_CIPHER_GET = "/awss/cipher/get"

# --- BLE (Breeze) provisioning protocol constants ----------------------------
# From com.aliyun.iot.breeze.BreezeUuid / BluetoothUuid (classes16.dex)
BREEZE_SERVICE_UUID = "0000feb3-0000-1000-8000-00805f9b34fb"
BREEZE_CHAR_READ = "0000fed4-0000-1000-8000-00805f9b34fb"
BREEZE_CHAR_WRITE = "0000fed5-0000-1000-8000-00805f9b34fb"
BREEZE_CHAR_INDICATE = "0000fed6-0000-1000-8000-00805f9b34fb"
BREEZE_CHAR_WRITE_NO_RSP = "0000fed7-0000-1000-8000-00805f9b34fb"
BREEZE_CHAR_NOTIFY = "0000fed8-0000-1000-8000-00805f9b34fb"
BLE_BASE_UUID_TEMPLATE = "0000{}-0000-1000-8000-00805f9b34fb"

# Local-control key provisioning (ALCS)
PATH_ALCS_ACCESS_INFO = "/alcs/device/accessInfo/get"      # 1.0.0  (iotIdList)
PATH_LOCALCONTROL_ACCESSINFO = "/living/device/localcontrol/accessinfo/get"

CONTENT_TYPE = "application/json; charset=UTF-8"
ACCEPT = "application/json; charset=UTF-8"

# ---------------------------------------------------------------------------
# Local protocol (ALCS = Alibaba Local Channel Service, CoAP over UDP)
# Constants from com.aliyun.linksdk.alcs.AlcsConstant / AlcsCmpSDK
# ---------------------------------------------------------------------------
ALCS_DISCOVERY_ADDR = "224.0.1.187"
ALCS_DISCOVERY_PORT = 5683
ALCS_DISCOVERY_TOPIC = "/dev/core/service/dev"
ALCS_METHOD_DISCOVERY = "core.service.dev"
ALCS_METHOD_AUTH = "core.service.auth"
ALCS_METHOD_HEARTBEAT = "core.service.heartBeat"
ALCS_METHOD_PROPERTY_GET = "thing.service.property.get"
ALCS_METHOD_PROPERTY_SET = "thing.service.property.set"
ALCS_METHOD_SERVICE_INVOKE = "thing.service."

ALCS_DEFAULT_PORT = 5683
ALCS_SECURE_PORT = 5684
ALCS_SESSION_VALID_TIME = 86400
ALCS_HEARTBEAT_MS = 600000

COAP_CONTENT_FORMAT_JSON = 50  # application/json
