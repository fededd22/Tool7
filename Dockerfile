FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TERM=xterm \
    DATA_DIR=/data \
    APKPATCHER_TOOLS_DIR=/opt/apkpatcher-tools \
    APKPATCHER_DESKTOP_APKTOOL=1 \
    APKPATCHER_NO_UPDATE=1

# Java مطلوب لـ APKTool / APKEditor / ApkSig
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-jre-headless ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# radare2 (لباتش Flutter فقط) غير موجود إطلاقاً في مستودعات Debian،
# لذا نثبّته من حزمة .deb الرسمية المنشورة على GitHub Releases.
# الأداة تستدعيه باسمين مختلفين (radare2 في Scan.py، و r2 في Flutter_SSL_Patch.py)،
# والحزمة الرسمية توفر الاثنين معاً، ونتأكد من ذلك صراحة ونطبعه في سجل البناء للتشخيص.
ARG RADARE2_VERSION=6.2.6
RUN arch="$(dpkg --print-architecture)" \
 && url="https://github.com/radareorg/radare2/releases/download/${RADARE2_VERSION}/radare2_${RADARE2_VERSION}_${arch}.deb" \
 && echo "== تنزيل radare2 من: $url ==" \
 && curl -fsSL "$url" -o /tmp/radare2.deb \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/radare2.deb \
 && rm -rf /tmp/radare2.deb /var/lib/apt/lists/* \
 && echo "== النتيجة: $(command -v radare2 || echo 'radare2 غير موجود')  /  $(command -v r2 || echo 'r2 غير موجود') ==" \
 && (radare2 -v 2>&1 | head -1 || true) \
 && (r2 -V 2>&1 | head -1 || true) \
 && command -v radare2 >/dev/null && command -v r2 >/dev/null

# بدائل أوامر Termux التي تستدعيها الأداة
COPY docker/termux-stub.sh /usr/local/bin/pkg
RUN chmod +x /usr/local/bin/pkg \
 && ln -s /usr/local/bin/pkg /usr/local/bin/termux-wake-lock \
 && ln -s /usr/local/bin/pkg /usr/local/bin/termux-wake-unlock

WORKDIR /src
COPY . /src
RUN pip install --no-cache-dir . -r webui/requirements.txt

# تنزيل أدوات الجافا (APKTool / APKEditor / ApkSig) أثناء البناء.
# لا نتجاهل الفشل هنا: لو تعذّر التنزيل يجب أن يفشل البناء بوضوح بدل
# أن تفشل المعالجة لاحقاً بخطأ غامض بسبب ملف jar ناقص أو فارغ.
RUN python -c "from ApkPatcher.Utils.Files_Check import FileCheck; f=FileCheck(); f.Set_Path(); f.isEmulator(); f.F_D(); f.F_D_A()" \
 && echo "== جار التحقق من ملفات الأدوات ==" \
 && find /opt/apkpatcher-tools -type f -printf '%10s  %p\n' \
 && for f in /opt/apkpatcher-tools/APKEditor.jar /opt/apkpatcher-tools/APKTool.jar /opt/apkpatcher-tools/ApkSig.jar; do \
      test -s "$f" || { echo "ملف مفقود أو فارغ: $f"; exit 1; }; \
    done

RUN useradd -m -u 10001 app \
 && mkdir -p /data \
 && chown -R app:app /data /opt/apkpatcher-tools \
 && chown -R app:app "$(python -c 'import ApkPatcher,os;print(os.path.dirname(ApkPatcher.__file__))')"

WORKDIR /src/webui
USER 10001
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

# عامل واحد فقط (الحالة محفوظة في الذاكرة) مع عدة threads
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "0", "-b", "0.0.0.0:8000", "app:app"]
