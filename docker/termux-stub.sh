#!/bin/sh
# بديل خفيف لأوامر Termux (pkg / termux-wake-lock) التي تستدعيها الأداة على أنظمة posix.
# يطبع هنا فقط ما هو مثبّت فعلاً (بفحص حقيقي)، لا قائمة ثابتة وهمية،
# حتى لا يظن الكود خطأً أن أداة ما مثبّتة بينما هي غائبة فعلياً.
if [ "$(basename "$0")" = "pkg" ] && [ "$1" = "list-installed" ]; then
  for tool in java aapt2 radare2 r2; do
    command -v "$tool" >/dev/null 2>&1 && echo "$tool"
  done
fi
exit 0
