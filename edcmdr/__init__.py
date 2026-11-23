"""EDCMDR client: reads Elite Dangerous journals and publishes what the commander chose to EDCMDR."""

import time

NAME = 'EDCMDR-client'
try:
    from ._build_info import VERSION  # written by scripts/build.py: the YYYYMMDD tag or build date
except ImportError:
    VERSION = time.strftime('%Y%m%d', time.gmtime())
