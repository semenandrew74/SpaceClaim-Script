# Python Script, API Version = V251
# -*- coding: utf-8 -*-
# =============================================================================
#  idealize.py  -  SpaceClaim (IronPython 2.7) : идеализация корпусных конструкций
#
#  Запуск: SpaceClaim -> вкладка Script -> Open script -> Run
#  Совместимость: IronPython 2.7 (БЕЗ f-строк, type hints, py3-синтаксиса).
#
#  Цель: из твердотельной модели получить расчётную модель для ANSYS
#        режим "shell+beam"  (основной)  или  "solid+shell" (локализация).
#        Эти три типа общей топологией НЕ объединяются (solid / shell / beam
#        живут в отдельных группах и сшиваются только контактами в Mechanical).
#
#  Этапы:
#     1. SHEETS     листы         -> поверхность + толщина + сторона выкладки
#     2. STIFFENERS полособульбы  -> балки (штатная Beam.ExtractProfile)
#     3. SMALL      кницы и т.п.  -> поверхности
#     4. HEALING    продление поверхностей / закрытие зазоров
#
#  СТАТУС: шаблон. Функции с пометкой [TODO] требуют доработки/проверки
#  на реальной геометрии; API-вызовы SpaceClaim, в которых нет уверенности,
#  собраны в блоке "SC API ADAPTER" - править нужно только там.
# =============================================================================
import clr
import math
import os

# API SpaceClaim (GetRootPart, Selection, Body, DesignBody ...) в скрипте доступно
# БЕЗ import: версию задаёт первая строка файла "# Python Script, API Version = ...".
# Если не стартует - запишите любой скрипт через Record и скопируйте оттуда эту строку.


# =============================================================================
# 0. КОНФИГУРАЦИЯ
# =============================================================================
class Cfg(object):
    # --- режим модели ---
    MODEL_MODE = "shell+beam"        # "shell+beam" | "solid+shell"

    # --- классификация листа ---
    SHEET_RATIO = 10.0               # мин. отношение (средний габарит / наименьший)
    SHEET_MAX_T = 60.0               # мм, верхняя граница толщины листа (защита)
    # Единицы модели SpaceClaim: метры (внутри API). Ниже - пересчёт в мм.
    UNIT_TO_MM = 1000.0

    # --- сторона выкладки (offset в Mechanical) ---
    # Если команда/нормаль не определена -> используем BOT / MID / TOP
    DEFAULT_OFFSET = "MID"           # "BOT" | "MID" | "TOP"
    # Какую поверхность листа копировать:
    #   "AUTO"  - сторону, обращённую к ближайшей связи (контакт) [TODO]
    #   "MID"   - срединная
    #   "BOT"/"TOP" - по знаку нормали относительно REF_AXIS
    COPY_SIDE = "MID"
    REF_AXIS = (0.0, 0.0, 1.0)       # для BOT/TOP: TOP = грань с нормалью вдоль REF_AXIS

    # --- классификация полособульба ---
    STIFF_ASPECT_MIN = 8.0           # длина / макс. размер сечения
    STIFF_MAX_SECTION = 0.45         # м, макс. габарит сечения

    # --- балки ---
    # Полособульб -> балка штатной командой Beam.ExtractProfile (сечение = реальный
    # профиль тела). Команда выполняется НА КОПИИ тела, исходный солид сохраняется.
    BEAM_ON_COPY = True
    # [TODO] ось балки идёт через ц.т. сечения; привязка к обшивке (offset) не сделана

    # --- малые связи ---
    SMALL_MAX_AREA = 0.5             # м^2, всё что мельче и не лист/профиль -> кница

    # --- зазоры ---
    GAP_TOL = 0.010                  # м, закрывать зазоры <= 10 мм
    EXTEND_MAX = 0.050               # м, макс. величина продления

    # --- вывод ---
    OUT_DIR = None                   # None -> Documents, иначе temp
    REPORT_NAME = "idealize_report.csv"
    DRY_RUN = False                  # True: только классификация и отчёт, без правок
    HIDE_ORIGINAL = True             # скрыть исходный солид (не удаляя - обратимо)
    PROBE_API = True                 # в начале напечатать, какие вызовы API доступны

    # Для COPY_SIDE="MID": использовать штатную Midsurface.Convert (на копии тела).
    # SpaceClaim сам назначает толщину; работает и на гнутых листах.
    USE_MIDSURFACE = True

    # --- защита от лавины тел ---
    LIMIT = 5                        # не более N тел на каждый этап за запуск (None = все)
    ONLY_SELECTED = False            # True: брать только выделенные тела
    SKIP_PREFIXES = ("SHEET_", "BEAM_", "SMALL_", "ORIG_")   # уже обработанное

    # --- какие этапы выполнять (готовы 1 и 2) ---
    STAGES = {"sheets": True, "stiffeners": True, "small": False, "healing": False}


# =============================================================================
# 1. SC API ADAPTER  (единственное место с вызовами SpaceClaim API)
#    Все функции ниже [TODO: проверить на вашей версии]
# =============================================================================
class SC(object):
    @staticmethod
    def root():
        return GetRootPart()  # noqa

    @staticmethod
    def design_bodies(part, _seen=None):
        """Все тела (DesignBody) узла, рекурсивно по компонентам (каждый Template 1 раз)."""
        seen = _seen if _seen is not None else []
        if any(part is x for x in seen):
            return []
        seen.append(part)
        out = list(part.Bodies)
        for comp in part.Components:
            out.extend(SC.design_bodies(comp.Template, seen))
        return out

    @staticmethod
    def selected_bodies():
        """Выделенные DesignBody или None, если выделение недоступно."""
        try:
            return list(Selection.GetActive().GetItems[IDesignBody]())  # noqa
        except Exception:
            return None

    @staticmethod
    def part_of(body):
        """Part, в котором создавать результат (родитель исходного тела, иначе root)."""
        p = getattr(body, "Parent", None)
        return p if isinstance(p, Part) else SC.root()  # noqa

    @staticmethod
    def is_solid(body):
        return body.Shape.IsClosed if hasattr(body.Shape, "IsClosed") else True

    @staticmethod
    def bbox_size(body):
        """Габариты по осям мирового bbox (м) -> (dx, dy, dz)."""
        box = body.Shape.GetBoundingBox(Matrix.Identity)  # noqa
        s = box.Size
        return (s.X, s.Y, s.Z)

    @staticmethod
    def face_normal(face_shape):
        """Внешняя нормаль плоской грани (Modeler.Face) с учётом IsReversed -> tuple."""
        n = face_shape.Geometry.Frame.DirZ
        if face_shape.IsReversed:
            n = -n
        return (n.X, n.Y, n.Z)

    @staticmethod
    def planar_faces(body):
        """[(face, area, unit_normal(tuple), origin Point)] только плоские грани."""
        res = []
        for f in body.Faces:
            try:
                surf = f.Shape.Geometry
                if isinstance(surf, Plane):  # noqa
                    res.append((f, f.Shape.Area, SC.face_normal(f.Shape), surf.Frame.Origin))
            except Exception:
                continue
        return res

    @staticmethod
    def extent_along(body, direction):
        """Протяжённость тела вдоль единичного вектора (м): max-min проекций вершин."""
        proj = []
        for v in body.Shape.Vertices:  # [TODO] при отсутствии Vertices -> по рёбрам
            p = v.Position
            proj.append(p.X * direction[0] + p.Y * direction[1] + p.Z * direction[2])
        return (max(proj) - min(proj)) if proj else 0.0

    @staticmethod
    def _edge_curve(edge):
        """ITrimmedCurve ребра (DesignEdge).  [TODO: проверить на V25]"""
        shp = edge.Shape
        try:
            return shp.TrimmedCurve
        except Exception:
            return CurveSegment.Create(shp.Geometry, shp.Interval)  # noqa

    @staticmethod
    def copy_face_to_surface(part, face, name, shift=None):
        """Скопировать грань (DesignFace) в новое поверхностное тело, при необходимости
        сдвинув на вектор shift=(dx,dy,dz) [м]. Возвращает (DesignBody, normal tuple).

        Плоская грань: Body.CreatePlanarBody(plane, все кромки грани) - внешний контур
        и отверстия идут одним списком кривых. Это самый предсказуемый путь.
        Неплоская грань: fallback copy/paste (см. _copy_paste_face).  [TODO]
        """
        surf = face.Shape.Geometry
        if not isinstance(surf, Plane):  # noqa
            if shift:
                raise NotImplementedError("сдвиг неплоской грани (срединная) - [TODO]")
            return SC._copy_paste_face(part, face, name)

        from System.Collections.Generic import List  # noqa
        curves = List[ITrimmedCurve]()  # noqa
        for e in face.Edges:
            curves.Add(SC._edge_curve(e))
        body = Body.CreatePlanarBody(surf, curves)  # noqa
        if shift:
            body.Transform(Matrix.CreateTranslation(  # noqa
                Vector.Create(shift[0], shift[1], shift[2])))  # noqa
        db = DesignBody.Create(part, name, body)  # noqa
        n = SC.face_normal(list(db.Faces)[0].Shape)
        return db, n

    @staticmethod
    def _copy_paste_face(part, face, name):
        """Fallback для неплоских граней через Copy/Paste.  [TODO: подобрать вызовы V25;
        проще всего записать скрипт (Record) копирования грани и вставить сюда]"""
        raise NotImplementedError("неплоская грань: copy/paste не реализован")

    @staticmethod
    def set_thickness(db, t):
        """Присвоить толщину поверхностному телу (м). Возвращает True/False.  [TODO]
        Если у DesignBody в V25 нет свойства Thickness - толщина остаётся только
        в имени тела и CSV-отчёте."""
        try:
            db.Thickness = t
            return True
        except Exception:
            return False

    @staticmethod
    def hide(body):
        """Скрыть тело (вызов взят из Record V25). Возвращает True/False."""
        try:
            ViewHelper.SetObjectVisibility(  # noqa
                BodySelection.Create(body), VisibilityType.Hide, False, False)  # noqa
            return True
        except Exception:
            return False

    @staticmethod
    def midsurface_convert(part, body, name, max_t):
        """Срединная поверхность штатной командой Midsurface.Convert (Record V25).
        Работает НА КОПИИ: исходный солид сохраняется. Подходит и для гнутых листов,
        толщину SpaceClaim назначает сам.  max_t - верхняя граница толщины, м.
        Возвращает DesignBody|None.  [TODO] проверить, что результат - 1 тело."""
        copy = DesignBody.Create(part, name, body.Shape.Copy())  # noqa
        before = set(part.Bodies)
        res = Midsurface.Convert(BodySelection.Create(copy), max_t)  # noqa
        created = None
        try:
            created = list(res.CreatedBodies)
        except Exception:
            pass
        if not created:
            created = [b for b in part.Bodies if b not in before]
        if len(created) > 1:
            raise Abort("Midsurface.Convert создала %d тел вместо 1 (тело '%s'). "
                        "Остановлено - проверьте модель (Ctrl+Z)." % (len(created), name))
        if len(created) == 1:
            return created[0]
        # команда могла преобразовать копию "на месте"
        try:
            if copy.Shape.Faces.Count > 0 and len(created) == 0:
                return copy
        except Exception:
            pass
        return None

    @staticmethod
    def probe():
        """Напечатать доступность вызовов API (диагностика под вашу сборку)."""
        checks = [
            ("Body.CreatePlanarBody", lambda: Body.CreatePlanarBody),         # noqa
            ("DesignBody.Create", lambda: DesignBody.Create),                 # noqa
            ("CurveSegment.Create", lambda: CurveSegment.Create),             # noqa
            ("Matrix.CreateTranslation", lambda: Matrix.CreateTranslation),   # noqa
            ("Vector.Create", lambda: Vector.Create),                         # noqa
            ("Body.Transform", lambda: Body.Transform),                       # noqa
            ("Midsurface.Convert", lambda: Midsurface.Convert),               # noqa
            ("BodySelection.Create", lambda: BodySelection.Create),           # noqa
            ("ViewHelper.SetObjectVisibility", lambda: ViewHelper.SetObjectVisibility),  # noqa
            ("VisibilityType.Hide", lambda: VisibilityType.Hide),             # noqa
            ("Beam.ExtractProfile", lambda: Beam.ExtractProfile),             # noqa
        ]
        for nm, fn in checks:
            try:
                fn()
                print("  [ok]   %s" % nm)
            except Exception as ex:
                print("  [нет]  %s  (%s)" % (nm, ex))

    @staticmethod
    def beams(part):
        """Балки узла (Part.Beams) списком; [] если коллекция недоступна."""
        try:
            return list(part.Beams)
        except Exception:
            return []

    @staticmethod
    def alive(obj):
        """Объект ещё существует в документе (не удалён/не поглощён командой)."""
        try:
            return not obj.IsDeleted
        except Exception:
            return True

    _beam_result_shown = False

    @staticmethod
    def extract_beam(part, body, name, on_copy):
        """Балка из солида штатной командой Beam.ExtractProfile (Record V25).
        on_copy=True: команда выполняется на копии, исходный солид не трогаем.
        Новую балку берём из результата команды, иначе - как разницу Part.Beams
        до/после (в part тела и в корне). Возвращает (beam|None, src), где src -
        тело, над которым выполнена команда (копия или исходник)."""
        src = DesignBody.Create(part, name, body.Shape.Copy()) if on_copy else body  # noqa
        parts = [part]
        if SC.root() is not part:
            parts.append(SC.root())
        before = []
        for p in parts:
            before.extend(SC.beams(p))
        res = Beam.ExtractProfile(BodySelection.Create(src))  # noqa

        if not SC._beam_result_shown:      # один раз: что вернула команда (для отладки)
            SC._beam_result_shown = True
            try:
                print("    ExtractProfile -> %s: %s" % (
                    type(res).__name__,
                    ", ".join(a for a in dir(res) if not a.startswith("_"))))
            except Exception:
                pass

        created = []
        for attr in ("CreatedBeams", "Created", "CreatedObjects"):
            try:
                created = list(getattr(res, attr))
            except Exception:
                created = []
            if created:
                break
        if not created:
            after = []
            for p in parts:
                after.extend(SC.beams(p))
            created = [b for b in after if not any(b is x or b == x for x in before)]
        if len(created) > 1:
            raise Abort("Beam.ExtractProfile создала %d балок вместо 1 (тело '%s'). "
                        "Остановлено - проверьте модель (Ctrl+Z)." % (len(created), name))
        return (created[0] if created else None), src

    @staticmethod
    def create_tracked(part, create_fn, name):
        """Выполнить создание и вернуть НОВОЕ тело по ссылке на объект, а не по имени.
        Автонумерация (Surface1, Surface2, ...) скрипту не нужна: результат берём
        как разницу множеств part.Bodies до/после вызова, сразу переименовываем
        в детерминированное имя и храним ссылку в Item.result.  [TODO] проверить
        что DesignBody корректно хешируется/сравнивается в IronPython; иначе
        сравнивать по списку индексов."""
        before = set(part.Bodies)
        ret = create_fn()
        if ret is not None and hasattr(ret, "Name"):     # если API вернул объект - берём его
            new = ret
        else:
            added = [b for b in part.Bodies if b not in before]
            new = added[0] if len(added) == 1 else None  # >1 или 0 -> ошибка, пометим в note
        if new is not None:
            SC.rename(new, name)
        return new

    @staticmethod
    def rename(body, name):
        try:
            body.Name = name
        except Exception:
            pass

    @staticmethod
    def extend_surface(face_or_edge, distance):
        """Продлить поверхность/кромку.  [TODO]  (Extend / Fill / Merge в SpaceClaim)"""
        raise NotImplementedError("extend_surface")


# =============================================================================
# 2. ВЕКТОРНЫЕ УТИЛИТЫ
# =============================================================================
def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def norm(a):
    l = math.sqrt(dot(a, a)) or 1.0
    return (a[0] / l, a[1] / l, a[2] / l)


# =============================================================================
# 3. МОДЕЛЬ ДАННЫХ
# =============================================================================
class Abort(Exception):
    """Аварийная остановка этапа (аномалия при создании тел)."""
    pass


class Item(object):
    """Результат классификации одного тела."""
    SHEET, STIFFENER, SMALL, SKIP = "SHEET", "STIFFENER", "SMALL", "SKIP"

    def __init__(self, body):
        self.body = body
        self.kind = Item.SKIP
        self.src_name = getattr(body, "Name", "?")   # имя исходного тела (для отчёта)
        self.name = self.src_name                    # новое имя результата
        self.idx = 0                                 # сквозной индекс в списке (уникальность)
        self.thickness = None       # м
        self.t_dir = None           # единичный вектор толщины
        self.axis = None            # для профиля: ось
        self.length = None
        self.offset = Cfg.DEFAULT_OFFSET
        self.n_out = None           # внешняя нормаль копируемой грани
        self.result = None          # созданное тело (surface/beam)
        self.note = ""


# =============================================================================
# 4. КЛАССИФИКАЦИЯ
# =============================================================================
def sorted_dims(body):
    return sorted(SC.bbox_size(body))


def sheet_thickness_dir(body):
    """Направление и толщина листа по двум самым большим параллельным плоским граням.
    Устойчиво к наклонным листам (в отличие от осевого bbox).
    Возвращает (t, unit_dir) или None, если плоских граней нет (гнутый лист -> [TODO])."""
    faces = SC.planar_faces(body)
    if not faces:
        return None
    faces.sort(key=lambda x: -x[1])
    n = norm(faces[0][2])
    return (SC.extent_along(body, n), n)


def classify(body):
    it = Item(body)
    d = sorted_dims(body)                    # d[0] <= d[1] <= d[2]
    if d[0] <= 0:
        it.note = "нулевой габарит"
        return it

    # --- лист: один размер << двух других ---
    if d[1] / d[0] >= Cfg.SHEET_RATIO and d[0] * SC_UNIT() <= Cfg.SHEET_MAX_T:
        td = sheet_thickness_dir(body)
        if td:
            it.kind, it.thickness, it.t_dir = Item.SHEET, td[0], td[1]
        else:
            it.kind, it.thickness = Item.SHEET, d[0]
            it.note = "нет плоских граней: толщина по bbox [TODO]"
        return it

    # --- профиль (полособульб): два малых размера, один длинный ---
    if d[2] / max(d[1], 1e-9) >= Cfg.STIFF_ASPECT_MIN and d[1] <= Cfg.STIFF_MAX_SECTION:
        it.kind, it.length = Item.STIFFENER, d[2]
        it.axis = longest_axis(body)         # [TODO] для наклонных - по граням
        return it

    # --- малая связь (кница и пр.) ---
    it.kind = Item.SMALL
    return it


def SC_UNIT():
    return Cfg.UNIT_TO_MM


def longest_axis(body):
    """Ось профиля. Простая версия: наибольший габарит bbox.
    [TODO] для наклонных профилей: ось нормальна к торцевой грани/по экструзии."""
    s = SC.bbox_size(body)
    i = s.index(max(s))
    v = [0.0, 0.0, 0.0]
    v[i] = 1.0
    return tuple(v)


# =============================================================================
# 5. ЭТАП 1 - ЛИСТЫ
# =============================================================================
def pick_sheet_face(it):
    """Выбрать грань для копирования и определить, откуда откладывать толщину.
    Возвращает DesignFace или None. Заполняет it.n_out (внешняя нормаль грани).
    Если нормаль/команда выкладки не найдена -> BOT/MID/TOP по Cfg."""
    faces = SC.planar_faces(it.body)
    if not faces or it.t_dir is None:
        it.offset = Cfg.DEFAULT_OFFSET
        return None
    # две самые большие грани листа (противоположные стороны)
    pair = sorted(faces, key=lambda x: -x[1])[:2]
    side = Cfg.COPY_SIDE
    if side == "MID":
        it.n_out = pair[0][2]
        return pair[0][0]
    ref = norm(Cfg.REF_AXIS)
    pair.sort(key=lambda x: dot(x[2], ref))          # [0]=BOT, [1]=TOP
    chosen = pair[1] if side == "TOP" else pair[0]
    it.n_out = chosen[2]
    return chosen[0]


def offset_for(n_surface, n_out):
    """Сторона выкладки для Mechanical (Offset Type).
    Материал листа лежит в направлении -n_out (вглубь от копируемой грани).
    Если нормаль созданной поверхности смотрит наружу (вдоль n_out), материал
    с её отрицательной стороны -> "TOP" (поверхность = верх оболочки, толщина вниз).
    Иначе -> "BOT".  [TODO] подтвердить знак на одном тестовом листе в Mechanical."""
    return "TOP" if dot(n_surface, n_out) > 0 else "BOT"


def process_sheets(items):
    todo = [i for i in items if i.kind == Item.SHEET]
    if Cfg.LIMIT is not None:
        todo = todo[:Cfg.LIMIT]
    print("Листов к обработке: %d" % len(todo))
    for k, it in enumerate(todo):
        print("  [%d/%d] %s" % (k + 1, len(todo), it.src_name))
        try:
            _process_one_sheet(it)
        except Abort:
            raise
        except Exception as ex:
            it.note += " ОШИБКА: %s" % ex
        if it.result is not None:
            # исходник помечаем, чтобы повторный запуск его не обрабатывал заново
            SC.rename(it.body, "ORIG_" + it.src_name)


def _sheet_by_midsurface(it):
    it.offset = "MID"
    t = it.thickness
    it.name = "SHEET_%04d_t%.1f_MID" % (it.idx, t * SC_UNIT())
    if Cfg.DRY_RUN:
        return
    part = SC.part_of(it.body)
    # запас 10% на верхнюю границу толщины для Midsurface.Convert
    it.result = SC.midsurface_convert(part, it.body, it.name, t * 1.1)
    if it.result is None:
        it.note += " Midsurface.Convert: результат не получен"
        return
    SC.rename(it.result, it.name)
    if Cfg.HIDE_ORIGINAL and not SC.hide(it.body):
        it.note += " исходник не скрыт"


def _process_one_sheet(it):
    if Cfg.COPY_SIDE == "MID" and Cfg.USE_MIDSURFACE:
        return _sheet_by_midsurface(it)
    face = pick_sheet_face(it)
    if face is None:
        it.note += " нет плоской грани: пропущен [TODO гнутые листы]"
        return
    t = it.thickness
    mid = (Cfg.COPY_SIDE == "MID")
    shift = None
    if mid:   # срединная поверхность: сдвиг копии на t/2 вглубь листа
        shift = (-it.n_out[0] * t / 2.0, -it.n_out[1] * t / 2.0, -it.n_out[2] * t / 2.0)
    it.offset = "MID" if mid else "?"
    it.name = "SHEET_%04d_t%.1f_%s" % (it.idx, t * SC_UNIT(), it.offset)  # предв. имя
    if Cfg.DRY_RUN:
        return

    part = SC.part_of(it.body)
    res = SC.create_tracked(part, lambda: SC.copy_face_to_surface(part, face, it.name, shift)[0], it.name)
    if res is None:
        it.note += " результат не получен"
        return
    it.result = res
    n_surf = SC.face_normal(list(res.Faces)[0].Shape)
    if not mid:
        it.offset = offset_for(n_surf, it.n_out)
    it.name = "SHEET_%04d_t%.1f_%s" % (it.idx, t * SC_UNIT(), it.offset)
    SC.rename(res, it.name)
    if not SC.set_thickness(res, t):
        it.note += " толщина только в имени/CSV (DesignBody.Thickness недоступно)"
    if Cfg.HIDE_ORIGINAL and not SC.hide(it.body):
        it.note += " исходник не скрыт (скройте вручную/подберите вызов)"


# =============================================================================
# 6. ЭТАП 2 - ПОЛОСОБУЛЬБЫ
# =============================================================================
def process_stiffeners(items):
    todo = [i for i in items if i.kind == Item.STIFFENER]
    if Cfg.LIMIT is not None:
        todo = todo[:Cfg.LIMIT]
    print("Полособульбов к обработке: %d" % len(todo))
    for k, it in enumerate(todo):
        print("  [%d/%d] %s" % (k + 1, len(todo), it.src_name))
        try:
            _process_one_stiffener(it)
        except Abort:
            raise
        except Exception as ex:
            it.note += " ОШИБКА: %s" % ex
        if it.result is not None and SC.alive(it.body):
            SC.rename(it.body, "ORIG_" + it.src_name)


def _process_one_stiffener(it):
    """Балка штатной командой Beam.ExtractProfile: сечение = реальный профиль тела
    (не упрощаем, эквиваленты не подбираем), ось - через ц.т. сечения."""
    it.name = "BEAM_%04d_L%.0f" % (it.idx, it.length * SC_UNIT())
    if Cfg.DRY_RUN:
        return
    part = SC.part_of(it.body)
    beam, src = SC.extract_beam(part, it.body, it.name, Cfg.BEAM_ON_COPY)
    if beam is None:
        it.note += " Beam.ExtractProfile: балка не получена"
        # копию, из которой ничего не вышло, убираем с глаз (имя BEAM_* -> не обработается)
        if src is not it.body and SC.alive(src):
            SC.hide(src)
        return
    it.result = beam
    SC.rename(beam, it.name)
    # команда могла не поглотить солид-копию, а оставить её рядом с балкой
    if src is not it.body and SC.alive(src):
        if not SC.hide(src):
            it.note += " копия-солид не скрыта"
    if Cfg.HIDE_ORIGINAL and SC.alive(it.body) and not SC.hide(it.body):
        it.note += " исходник не скрыт"


# =============================================================================
# 7. ЭТАП 3 - КНИЦЫ И МЕЛКИЕ СВЯЗИ
# =============================================================================
def process_small(items):
    """Кницы -> поверхности. По сути тот же путь, что и листы
    (толщина = наименьший габарит, поверхность = срединная/выбранная)."""
    for it in [i for i in items if i.kind == Item.SMALL]:
        td = sheet_thickness_dir(it.body)
        if td:
            it.thickness, it.t_dir = td
        it.name = "SMALL_%04d" % it.idx
        if Cfg.DRY_RUN:
            continue
        # [TODO] переиспользовать pick_sheet_face + copy_face_to_surface;
        #        кницы сложной формы (не листовые) -> оставить solid/локализация.


# =============================================================================
# 8. ЭТАП 4 - ПРОДЛЕНИЕ И ЗАЗОРЫ
# =============================================================================
def heal_gaps(items):
    """После устаканивания основных связей:
      a) найти пары кромок/поверхностей на расстоянии <= GAP_TOL;
      b) продлить (Extend) на величину <= EXTEND_MAX до пересечения;
      c) сшить (Merge) / Fill по необходимости.
    Идея реализации [TODO]:
      - собрать все свободные кромки поверхностных тел (edge с 1 гранью);
      - для каждой кромки искать ближайшую поверхность другого тела
        (расстояние/проекция) -> кандидаты на продление;
      - группировать по компонентам, не склеивать shell с beam/solid (разная топология).
    """
    if Cfg.DRY_RUN:
        return


# =============================================================================
# 9. ОТЧЁТ
# =============================================================================
def _report_path():
    """Папку по умолчанию (cwd = каталог установки SpaceClaim) писать нельзя ->
    пробуем OUT_DIR, Документы, temp; берём первую, где реально создаётся файл."""
    import tempfile
    import System
    try:
        docs = System.Environment.GetFolderPath(System.Environment.SpecialFolder.MyDocuments)
    except Exception:
        docs = os.path.join(os.path.expanduser("~"), "Documents")
    cands = [Cfg.OUT_DIR, docs, tempfile.gettempdir()]
    for d in cands:
        if not d:
            continue
        try:
            path = os.path.join(d, Cfg.REPORT_NAME)
            open(path, "a").close()
            return path
        except Exception:
            continue
    return None


def write_report(items):
    path = _report_path()
    if path is None:
        print("Отчёт не записан: нет доступной папки (задайте Cfg.OUT_DIR)")
        return None
    with open(path, "w") as f:
        f.write("new_name;src_name;kind;thickness_mm;offset;length_mm;note\n")
        for it in items:
            f.write("%s;%s;%s;%s;%s;%s;%s\n" % (
                it.name, it.src_name, it.kind,
                "%.2f" % (it.thickness * SC_UNIT()) if it.thickness else "",
                it.offset if it.kind == Item.SHEET else "",
                "%.0f" % (it.length * SC_UNIT()) if it.length else "",
                it.note))
    return path


# =============================================================================
# 10. MAIN
# =============================================================================
def main():
    bodies = SC.selected_bodies() if Cfg.ONLY_SELECTED else None
    if bodies is None:
        bodies = SC.design_bodies(SC.root())
    # уже обработанные/созданные скриптом тела не трогаем (защита от повторной обработки)
    bodies = [b for b in bodies
              if not str(getattr(b, "Name", "")).startswith(Cfg.SKIP_PREFIXES)]
    print("Тел в обработке: %d" % len(bodies))
    items = [classify(b) for b in bodies]
    for n, it in enumerate(items):
        it.idx = n                                   # имена результатов уникальны по idx

    if Cfg.PROBE_API:
        print("Проверка API:")
        SC.probe()
    st = Cfg.STAGES
    if st.get("sheets"):
        process_sheets(items)        # 1
    if st.get("stiffeners"):
        process_stiffeners(items)    # 2
    if st.get("small"):
        process_small(items)         # 3
    if st.get("healing"):
        heal_gaps(items)             # 4

    try:
        path = write_report(items)
    except Exception as ex:
        path = None
        print("Ошибка записи отчёта: %s" % ex)
    cnt = {}
    for it in items:
        cnt[it.kind] = cnt.get(it.kind, 0) + 1
    print("Итого: %s | DRY_RUN=%s | отчёт: %s" % (cnt, Cfg.DRY_RUN, path))


try:
    main()
except Abort as ex:
    print("ОСТАНОВЛЕНО: %s" % ex)
except Exception:
    import traceback
    print(traceback.format_exc())
