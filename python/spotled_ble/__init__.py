"""SPOTLED BLE panel control, reverse-engineered from the official app (com.led.spotled 1.9.4)."""
from .client import SpotLED, SpotLEDError, find_device, scan
from .protocol import ColorType, Effect, Flip, PlaybackRate

__all__ = ["SpotLED", "SpotLEDError", "find_device", "scan", "Effect", "Flip", "PlaybackRate", "ColorType"]
