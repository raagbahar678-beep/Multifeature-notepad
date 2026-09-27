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
# charset_normalizer isn't imported by main.py directly - it's pulled in
# because the Kivy p4a recipe hardcodes python_depends including
# 'requests', which depends on charset-normalizer. p4a resolves that
# via its own internal pip pass and currently picks
# charset_normalizer-3.5.1-*-android_24_arm64_v8a.whl, which pip then
# rejects as "not a supported wheel on this platform" (p4a's own
# wheel-compatibility check has a bug here).
#
# A version pin of charset-normalizer (with a HYPHEN) in this file does
# NOT work around it: p4a's build.py compares requirement names using
# underscores (`mname.lower().replace("-", "_")`) but stores names from
# this file with whatever separator you typed, so "charset-normalizer"
# here never matches and gets silently ignored/re-resolved to the
# broken 3.5.1. Writing it with an UNDERSCORE instead makes the name
# match, so p4a treats it as already satisfied and installs this
# pinned (pure-python wheel) version instead of re-resolving one.
requirements = python3,kivy==2.3.1,pyjnius,charset_normalizer==3.3.2

orientation = portrait
fullscreen = 0

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
