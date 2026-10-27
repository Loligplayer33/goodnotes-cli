"""Compatibility exports for native annotation decoding and rendering."""

from .native.ink import decode_ink as decode_ink
from .native.objects import decode_native_objects as decode_native_objects
from .native.objects import decode_native_records as decode_native_records
from .native.records import DecodedNativeObject as DecodedNativeObject
from .native.records import Ink as Ink
from .native.records import NativeImage as NativeImage
from .native.records import NativeOverlay as NativeOverlay
from .native.records import NativePath as NativePath
from .native.records import NativePathCommand as NativePathCommand
from .rendering.overlay import render_native_overlay as render_native_overlay
