[app]

title = LaTeX Notepad
package.name = latexnotepad
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ttf

version = 1.0

# Kivy is required; pyjnius is pulled in for Android storage path helpers.
# charset-normalizer is pinned to an older version as a workaround: p4a
# v2026.05.09's new "prebuilt wheels" feature fetches a
# charset_normalizer isn't imported anywhere in main.py, and pinning it
# didn't stop p4a's wheel index from resolving an incompatible cp314
# Android wheel (charset_normalizer-3.5.1-...-android_24_arm64_v8a.whl
# is not a supported wheel on this platform). Simplest fix: drop it —
# nothing in this app needs it directly.
requirements = python3,kivy==2.3.1,pyjnius

orientation = portrait
fullscreen = 0

icon.filename = %(source.dir)s/icon.png

android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE

# Reasonable, broadly-compatible API range for a text-editor style app.
android.api = 33
android.minapi = 24
android.ndk = 25b

# Fix #3 (updated): p4a's `develop` branch is currently mid-migration to a
# Python 3.14 hostpython / NDK r29 toolchain, which is breaking several
# recipes for Kivy 2.3.1 right now (see kivy/python-for-android#3274) --
# including the libthorvg recipe our build was crashing on. Pin to the
# latest tagged stable release instead of floating on develop.
p4a.branch = v2026.05.09

android.archs = arm64-v8a

android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
