"""واجهة ويب بسيطة لـ ApkPatcher: ارفع ملف التطبيق، اختر الخيارات، نزّل الناتج.

- يحافظ على اسم الملف الأصلي (الأداة تضيف فقط اللاحقة _Patched كما في سلوكها الأصلي).
- كل مهمة تعمل في مجلد مستقل (وHOME مستقل) فلا تتعارض المهام مع بعضها.
"""
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "jobs"
MAX_JOBS = int(os.environ.get("MAX_JOBS", "1"))            # مهام متزامنة
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))   # ثواني
# مدة الاحتفاظ بالنتيجة على الاستضافة إن لم يُنزَّلها أحد (بالثواني).
# يُحذف الملف تلقائياً فور اكتمال تنزيله بنجاح، بغض النظر عن هذه القيمة؛
# هذه فقط شبكة أمان للملفات التي لم تُنزَّل إطلاقاً.
JOB_TTL = int(os.environ.get("JOB_TTL", "300"))             # 5 دقائق افتراضياً
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "20"))  # ثواني بين كل فحص
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))
LOG_LIMIT = 4000                                            # أقصى عدد أسطر محفوظة

ALLOWED_EXT = {".apk", ".apks", ".apkm", ".xapk"}
CERT_EXT = {".pem", ".crt", ".cer", ".cert", ".der"}

# الأعلام المسموحة فقط (قائمة بيضاء) — لا يُمرَّر أي نص حر من المستخدم إلى الأمر
MODE_FLAGS = {"default": [], "ads": ["-rmads"], "info": ["-r"]}
ADDON_FLAGS = {          # تعمل فقط في الوضع الافتراضي
    "flutter": "-f",
    "pairip": "-p",
    "no_screenshot_block": "-rmss",
    "no_usb_check": "-rmusb",
    "spoof_pkg": "-pkg",
}
GLOBAL_FLAGS = {
    "apkeditor": "-a",
    "unsigned": "-u",
    "emulator": "-e",
    "by_moon": "-moon",
}

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=MAX_JOBS)


def safe_name(filename: str) -> str:
    """يبقي الاسم الأصلي كما هو قدر الإمكان، ويزيل فقط ما يضر (مسارات/محارف تحكم)."""
    name = os.path.basename((filename or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", name).strip().strip(".")
    stem, ext = os.path.splitext(name)
    return f"{stem}{ext.lower()}" if stem else ""


def run_job(job_id: str) -> None:
    job = JOBS[job_id]
    job_dir = Path(job["dir"])
    home = job_dir / "home"
    home.mkdir(exist_ok=True)

    job["status"] = "running"
    job["started"] = time.time()

    # تحقق من الملف قبل تمريره للأداة: يساعد على تمييز ملف تالف/مقطوع
    # (مثلاً بسبب حدود الرفع عند بوابة الاستضافة) عن مشكلة في الأداة نفسها.
    apk_path = job_dir / job["name"]
    try:
        size = apk_path.stat().st_size
        is_zip = zipfile.is_zipfile(apk_path)
        job["log"].append(f"[فحص الملف] الحجم: {size:,} بايت — أرشيف ZIP صالح: {'نعم' if is_zip else 'لا (الملف تالف أو غير مكتمل)'}")
        if is_zip:
            with zipfile.ZipFile(apk_path) as zf:
                bad = zf.testzip()
                names = zf.namelist()
                has_manifest = "AndroidManifest.xml" in names
                job["log"].append(
                    f"[فحص الملف] عدد الملفات داخله: {len(names)} — يحتوي AndroidManifest.xml: {'نعم' if has_manifest else 'لا'}"
                    + (f" — ملف تالف داخل الأرشيف: {bad}" if bad else "")
                )
    except Exception as e:
        job["log"].append(f"[فحص الملف] تعذر فحص الملف: {e}")

    env = os.environ.copy()
    env.update({"HOME": str(home), "TERM": "xterm", "PYTHONUNBUFFERED": "1", "COLUMNS": "100"})

    try:
        proc = subprocess.Popen(
            job["cmd"], cwd=job_dir, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        job["log"].append("تعذر العثور على الأمر ApkPatcher داخل الحاوية.")
        job["status"] = "error"
        return

    killer = threading.Timer(JOB_TIMEOUT, proc.kill)
    killer.start()
    try:
        for raw in proc.stdout:
            line = ANSI_RE.sub("", raw).rstrip("\r\n")
            if "\r" in line:                      # أشرطة التقدم: نأخذ آخر تحديث
                line = line.split("\r")[-1]
            if line.strip():
                job["log"].append(line)
                if len(job["log"]) > LOG_LIMIT:
                    del job["log"][: LOG_LIMIT // 4]
                    job["log_base"] += LOG_LIMIT // 4
        proc.wait()
    finally:
        killer.cancel()

    # الأداة قد تنهي بكود خروج غير صفري حتى عند النجاح، لذا نعتمد على وجود الملف الناتج
    outputs = sorted(job_dir.glob("*_Patched.apk"))
    if outputs:
        job["output"] = outputs[0].name
        job["status"] = "done"
    else:
        if proc.returncode in (-9, None):
            job["log"].append(f"انتهت المهلة ({JOB_TIMEOUT} ثانية) وتم إيقاف المعالجة.")
        job["status"] = "error"

    shutil.rmtree(home, ignore_errors=True)
    job["finished"] = time.time()


def cleanup_loop() -> None:
    """يحذف نتائج المهام المكتملة التي لم تُنزَّل خلال JOB_TTL ثانية (افتراضياً 5 دقائق).
    الحذف الأساسي يحدث فور نجاح التنزيل (انظر /download)؛ هذه شبكة أمان فقط."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        with LOCK:
            old = [j for j, v in JOBS.items()
                   if v.get("finished") and now - v["finished"] > JOB_TTL]
            for j in old:
                shutil.rmtree(JOBS[j]["dir"], ignore_errors=True)
                del JOBS[j]


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/jobs")
def create_job():
    f = request.files.get("apk")
    if not f or not f.filename:
        return jsonify(error="لم يتم إرسال ملف."), 400

    name = safe_name(f.filename)
    ext = os.path.splitext(name)[1]
    if ext not in ALLOWED_EXT:
        return jsonify(error="الصيغ المدعومة: " + " ".join(sorted(ALLOWED_EXT))), 400

    mode = request.form.get("mode", "default")
    if mode not in MODE_FLAGS:
        return jsonify(error="وضع غير صالح."), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = DATA_DIR / job_id
    job_dir.mkdir(parents=True)

    apk_path = job_dir / name
    f.save(apk_path)

    flags = list(MODE_FLAGS[mode])
    if mode == "default":
        flags += [fl for key, fl in ADDON_FLAGS.items() if request.form.get(key) == "1"]
        cert = request.files.get("cert")
        if cert and cert.filename:
            cname = safe_name(cert.filename)
            if os.path.splitext(cname)[1] not in CERT_EXT:
                shutil.rmtree(job_dir, ignore_errors=True)
                return jsonify(error="صيغة الشهادة المدعومة: " + " ".join(sorted(CERT_EXT))), 400
            cert_path = job_dir / ("cert_" + cname)
            cert.save(cert_path)
            flags += ["-c", str(cert_path)]
    flags += [fl for key, fl in GLOBAL_FLAGS.items() if request.form.get(key) == "1"]

    # -i يجب أن يأتي أولاً ثم مسار الملف (بنفس اسمه الأصلي)
    cmd = ["ApkPatcher", "-i", str(apk_path)] + flags

    JOBS[job_id] = {
        "id": job_id, "dir": str(job_dir), "name": name, "cmd": cmd,
        "status": "queued", "log": [], "log_base": 0, "output": None,
        "created": time.time(),
    }
    POOL.submit(run_job, job_id)
    return jsonify(id=job_id), 201


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    since = max(int(request.args.get("since", 0)), job["log_base"])
    lines = job["log"][since - job["log_base"]:]
    return jsonify(
        status=job["status"], name=job["name"], output=job["output"],
        lines=lines, next=since + len(lines),
    )


@app.get("/api/jobs/<job_id>/download")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done" or not job["output"]:
        abort(404)
    path = Path(job["dir"]) / job["output"]

    response = send_file(path, as_attachment=True, download_name=job["output"],
                         mimetype="application/vnd.android.package-archive")

    # الحذف الفعلي من الاستضافة يحدث بعد اكتمال إرسال الملف بالكامل للمتصفح
    # (الذي يكون بدوره قد حفظه على جهاز المستخدم عبر مدير التنزيلات الخاص به)،
    # وليس فور استدعاء هذه الدالة، حتى لا يُقطَع التنزيل في منتصفه.
    job_dir = job["dir"]

    @response.call_on_close
    def _delete_after_send():
        with LOCK:
            JOBS.pop(job_id, None)
        shutil.rmtree(job_dir, ignore_errors=True)

    return response


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    with LOCK:
        job = JOBS.pop(job_id, None)
    if job:
        shutil.rmtree(job["dir"], ignore_errors=True)
    return "", 204


@app.get("/healthz")
def healthz():
    return "ok"


DATA_DIR.mkdir(parents=True, exist_ok=True)
threading.Thread(target=cleanup_loop, daemon=True).start()
