[app]

title = LaTeX Notepad
package.name = latexnotepad
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ttf

version = 1.0

# Kivy is required; pyjnius is pulled in for Android storage path helpers.
requirements = python3,kivy==2.3.1,pyjnius

orientation = portrait
fullscreen = 0

icon.filename = %(source.dir)s/icon.png

android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE

# Reasonable, broadly-compatible API range for a text-editor style app.
android.api = 33
android.minapi = 24
android.ndk = 25b

# Fix #3 (from the earlier project): pin p4a to the develop branch so the
# Android build recipes are compatible with modern Python (avoids the
# Python 3.14 wheel incompatibility that broke the previous build).
p4a.branch = develop

android.archs = arm64-v8a

android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
