from ..ANSI_COLORS import ANSI; C = ANSI()
from ..MODULES import IMPORT; M = IMPORT()

C_Line = f"{C.CC}{'_' * 61}"

TAG_TEXT = "by moon"
MAX_SUPERCLASS_HOPS = 20


# ---------------- Find Launcher Activity Class ----------------
def Find_Launcher_Activity(manifest_path, Package_Name):

    content = open(manifest_path, 'r', encoding='utf-8', errors='ignore').read()

    # ---------------- Scan Every <activity> Block ----------------
    # ملاحظة: وسوم <activity .../> ذاتية الإغلاق (بدون intent-filter) لا تحتوي
    # إغلاقاً منفصلاً </activity>؛ يجب مطابقتها أولاً وإلا امتد البحث الجشع
    # عبرها ليدمجها خطأً مع أول نشاط بعدها.
    activity_pattern = r'<activity\b[^>]*?/>|<activity\b[^>]*>(?:(?!</activity>).)*?</activity>'

    for activity_match in M.re.finditer(activity_pattern, content, M.re.S):

        block = activity_match[0]

        if not (M.re.search(r'android:name="android\.intent\.action\.MAIN"', block)
                and M.re.search(r'android:name="android\.intent\.category\.LAUNCHER"', block)):
            continue

        name_match = M.re.search(r'<activity\b[^>]*\sandroid:name="([^"]+)"', block)

        if not name_match:
            continue

        class_name = name_match[1]

        # ---------------- Resolve Relative Class Name ----------------
        if class_name.startswith('.'):
            class_name = f"{Package_Name}{class_name}"
        elif '.' not in class_name:
            class_name = f"{Package_Name}.{class_name}"

        return f"L{class_name.replace('.', '/')};"

    return None


# ---------------- Find Smali File for a Class ( L.../...; Format ) ----------------
def Find_Smali_File(smali_folders, smali_class_name):

    rel_path = smali_class_name[1:-1] + '.smali'

    for folder in smali_folders:
        candidate = M.os.path.join(folder, rel_path)

        if M.os.path.isfile(candidate):
            return candidate

    return None


# ---------------- Locate onCreate() Method Block Inside Smali Content ----------------
def Find_OnCreate_Method(content):

    return M.re.search(
        r'(\.method\s+[^\n]*\bonCreate\(Landroid/os/Bundle;\)V\n)(.*?\n)(\.end method)',
        content, M.re.S
    )


# ---------------- Insert Toast Snippet Into an Existing onCreate Body ----------------
def Inject_Toast(method_match):

    header, body, footer = method_match[1], method_match[2], method_match[3]

    locals_match = M.re.search(r'\.locals (\d+)', body)

    if not locals_match:
        return None

    locals_count = int(locals_match[1])

    # بعض الدوال (خصوصاً في أصناف وُسِّعت بأدوات تحسين/تعتيم) تستخدم أصلاً عدداً
    # كبيراً من السجلّات. تعليمات invoke-static/invoke-virtual العادية وconst/4
    # تقبل فقط سجلّات v0-v15 (ترميز 4-بت)، فلو أضفنا سجلّات جديدة بأرقام أعلى
    # من 15 باستخدامها يرفضها المُجمِّع. لذا نستخدم هنا صيغة /range (تدعم أي رقم
    # سجل حتى 255 للوجهة)، وننقل p0 أولاً إلى سجل جديد بدل استخدامه مباشرة،
    # حتى لا يعتمد العمل على موضعه العددي الفعلي (الذي يتغيّر مع .locals نفسه).
    if locals_count + 3 > 255:
        # حالة نادرة جداً (دالة تستخدم أصلاً أكثر من 252 سجلّاً)؛ الرسالة تُطبَع من المستدعي
        return None

    reg_ctx, reg_str, reg_dur = f"v{locals_count}", f"v{locals_count + 1}", f"v{locals_count + 2}"

    toast_snippet = (
        f"\n\n    move-object/from16 {reg_ctx}, p0\n\n"
        f"    const-string {reg_str}, \"{TAG_TEXT}\"\n\n"
        f"    const/16 {reg_dur}, 0x0\n\n"
        f"    invoke-static/range {{{reg_ctx} .. {reg_dur}}}, Landroid/widget/Toast;->makeText(Landroid/content/Context;Ljava/lang/CharSequence;I)Landroid/widget/Toast;\n\n"
        f"    move-result-object {reg_ctx}\n\n"
        f"    invoke-virtual/range {{{reg_ctx} .. {reg_ctx}}}, Landroid/widget/Toast;->show()V\n"
    )

    new_body = M.re.sub(r'\.locals \d+', f'.locals {locals_count + 3}', body, count=1)

    # ---------------- Insert Right After super.onCreate(...) Call ----------------
    super_match = M.re.search(r'invoke-super \{p0, p1\}, L[^;]+;->onCreate\(Landroid/os/Bundle;\)V\n', new_body)

    if super_match:
        insert_at = super_match.end()
        new_body = new_body[:insert_at] + toast_snippet + new_body[insert_at:]
    else:
        # لا يوجد استدعاء super صريح (نادر)؛ نضيف السطر في بداية الدالة كحل بديل
        new_body = toast_snippet + new_body

    return header + new_body + footer


# ---------------- Patch "by moon" Branding ----------------
def Patch_Branding(decompile_dir, smali_folders, manifest_path, Package_Name):

    print(
        f"\n{C_Line}\n\n"
        f"\n{C.X}{C.C} Branding Patch ( by moon )..."
    )

    launcher_class = Find_Launcher_Activity(manifest_path, Package_Name)

    if not launcher_class:
        print(f"\n{C.WARN} Could not find Launcher Activity in Manifest, Skipping Branding Patch.\n")
        return

    # ---------------- Walk the Superclass Chain Until a Real onCreate() Body is Found ----------------
    # لا نُنشئ أبداً دالة onCreate جديدة من الصفر: بعض التطبيقات (خصوصاً المُحسَّنة
    # بـ R8/ProGuard) تضع صفة final على onCreate الموروثة، فإضافة override جديد
    # لها يُفشل التحقق من الكود (VerifyError) ويُغلق التطبيق فوراً عند فتحه.
    # لذا نبحث دوماً عن أول صنف في سلسلة الوراثة يملك onCreate حقيقية بجسم كود
    # وندمج التعديل داخلها مباشرة، مهما بَعُد هذا الصنف عن شاشة الدخول نفسها.
    current_class = launcher_class
    visited = set()

    for _ in range(MAX_SUPERCLASS_HOPS):

        if current_class in visited:
            break
        visited.add(current_class)

        smali_path = Find_Smali_File(smali_folders, current_class)

        if not smali_path:
            print(
                f"\n{C.WARN} Could not find Smali File for {C.Y}'{current_class[1:-1].replace('/', '.')}'{C.C} "
                f"( Reached Platform/Library Class Outside the App's Dex ), Skipping Branding Patch.\n"
            )
            return

        content = open(smali_path, 'r', encoding='utf-8', errors='ignore').read()

        method_match = Find_OnCreate_Method(content)

        if method_match:
            new_method = Inject_Toast(method_match)

            if not new_method:
                print(f"\n{C.WARN} Could not safely patch onCreate() in {C.Y}'{M.os.path.basename(smali_path)}'{C.C}, Skipping Branding Patch.\n")
                return

            content = content[:method_match.start()] + new_method + content[method_match.end():]

            open(smali_path, 'w', encoding='utf-8', errors='ignore').write(content)

            where = (
                f"{launcher_class[1:-1].replace('/', '.')}"
                if current_class == launcher_class else
                f"{launcher_class[1:-1].replace('/', '.')} {C.P}➸❥ {C.Y}{current_class[1:-1].replace('/', '.')} {C.P}( Superclass )"
            )

            print(
                f"\n{C.S} Branding Tag {C.E} {C.OG}➸❥ {C.P}'{C.G}{TAG_TEXT}{C.P}' {C.OG}➸❥ {C.Y}{where}{C.G} ✔\n"
            )
            return

        super_match = M.re.search(r'\.super\s+(L[^;]+;)', content)

        if not super_match:
            print(f"\n{C.WARN} Could not read .super in {C.Y}'{M.os.path.basename(smali_path)}'{C.C}, Skipping Branding Patch.\n")
            return

        current_class = super_match[1]

    print(f"\n{C.WARN} Could not find a concrete onCreate() in the superclass chain, Skipping Branding Patch.\n")
