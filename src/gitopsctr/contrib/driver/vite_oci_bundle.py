import sys
from importlib import import_module

sys.modules[__name__] = import_module("gitopsctr.contrib.drivers.vite_oci_bundle")
